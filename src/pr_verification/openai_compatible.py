"""The model interface over the OpenAI-compatible protocol: a hosted endpoint or a local Ollama."""

import math
import re
from collections.abc import Mapping
from typing import TypeGuard

import httpx

from pr_verification.model import ModelFailed, ModelRequest, PricedResponse, TokenPrice
from pr_verification.tracing import SpanKind, span

DEFAULT_ENDPOINT = "https://api.minimax.io/v1"
DEFAULT_MODEL = "MiniMax-M3"

# A local model may first have to be loaded, and httpx on its own gives up after five seconds.
_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# Some models put their thinking in front of the answer, in the same text.
_THINKING = re.compile(r"\A\s*<think>.*?</think>", re.DOTALL)
_LONGEST_FAILURE = 500
# A price is for this many tokens.
_TOKENS_PRICED = 1_000_000


class OpenAICompatibleModel:
    def __init__(
        self,
        endpoint: str,
        model: str,
        key: str,
        transport: httpx.BaseTransport | None = None,
        price: TokenPrice | None = None,
        *,
        parallel_tool_calls: bool = False,
    ) -> None:
        """`price` is what the provider charges for this model, or None for a model that has no
        price, as a local one has none. It changes nothing about `complete`.

        `parallel_tool_calls` is the model's attribute of that name: whether it offers
        parallel tool calls, as `offers_parallel_tool_calls` (`pr_verification.model`) reads
        it. It is false unless given, since not every model behind this protocol can be
        relied on to ask for several tools at once, and it changes nothing about a request.

        This client does not offer prompt caching. It sends the same request whatever
        `ModelRequest.cache_instructions` is, so on a provider that offers neither
        capability every request, and all that comes of it, is as it was before either
        existed."""
        if parallel_tool_calls:
            raise NotImplementedError
        self._price = price
        self._url = endpoint.rstrip("/") + "/chat/completions"
        self._model = model
        self._name = f"{model} at {endpoint}"
        self._key = key.strip()
        if not (self._key.isascii() and self._key.isprintable()):
            # Sent as it is, the HTTP layer would refuse it with an error that quotes it.
            raise ModelFailed(f"the key for {self._name} holds characters that cannot be sent")
        headers = {"Authorization": f"Bearer {self._key}"} if self._key else {}
        self._client = httpx.Client(headers=headers, timeout=_TIMEOUT, transport=transport)

    def complete(self, request: ModelRequest) -> str:
        return self._ask(request)[0]

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        """Make the one request `complete` makes, and say what it cost at the model's price.

        The response is the one `complete` gives, and whatever `complete` raises is raised.

        The protocol reports the tokens of a request in the `usage` of its answer. `dollars`
        is `usage.prompt_tokens` at `price.input` plus `usage.completion_tokens` at
        `price.output`, each price being for a million tokens: 1,000 tokens sent and 500
        returned, at 2.0 and 6.0, cost 0.005.

        `dollars` is 0.0 when the model was given no price, and when the answer has no `usage`
        holding both counts as whole numbers that are 0 or more: what is not known is not
        charged for.
        """
        answer, usage = self._ask(request)
        if self._price is None or not isinstance(usage, dict):
            return PricedResponse(answer, 0.0)
        sent = _tokens(usage.get("prompt_tokens"))
        returned = _tokens(usage.get("completion_tokens"))
        if sent is None or returned is None:
            return PricedResponse(answer, 0.0)
        dollars = (sent * self._price.input + returned * self._price.output) / _TOKENS_PRICED
        return PricedResponse(answer, dollars)

    def _ask(self, request: ModelRequest) -> tuple[str, object]:
        """Make one request. The answer's text, and its `usage` when it has one.

        The request is recorded with the model's name, and with the tokens when `usage` holds
        both counts as integers (`pr_verification.tracing`)."""
        with span(SpanKind.MODEL_CALL, "model") as known:
            known["gen_ai.request.model"] = self._model
            answer, usage = self._post(request)
            if isinstance(usage, dict):
                sent, returned = usage.get("prompt_tokens"), usage.get("completion_tokens")
                if _is_integer(sent) and _is_integer(returned):
                    known["gen_ai.usage.input_tokens"] = sent
                    known["gen_ai.usage.output_tokens"] = returned
            return answer, usage

    def _post(self, request: ModelRequest) -> tuple[str, object]:
        """Send one request to the endpoint, and read its answer as `_ask` gives it."""
        # Instructions and data travel as separate messages, so Untrusted Content stays data.
        body = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": request.instructions},
                {"role": "user", "content": request.data},
            ],
        }
        try:
            response = self._client.post(self._url, json=body)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            # Not chained: the cause would be printed with any traceback, unredacted.
            raise self._failed(f"could not be reached: {error}") from None
        if not response.is_success:
            raise self._failed(f"answered with HTTP {response.status_code}: {response.text}")
        try:
            answered = response.json()
            text = answered["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError, RecursionError):
            text = None
        answer = _THINKING.sub("", text).strip() if isinstance(text, str) else ""
        if not answer:
            raise self._failed(f"gave a response that holds no text: {response.text}")
        return answer, answered.get("usage")

    def _failed(self, what: str) -> ModelFailed:
        """One line, without the key: a provider may repeat the key back in its error."""
        message = " ".join(f"{self._name} {what}".split())
        if self._key:
            message = message.replace(self._key, "[key removed]")
        if len(message) > _LONGEST_FAILURE:
            message = message[:_LONGEST_FAILURE] + " [cut short]"
        return ModelFailed(message)


def model_from_environment(
    environment: Mapping[str, str], transport: httpx.BaseTransport | None = None
) -> OpenAICompatibleModel | None:
    """None when neither a key nor an endpoint is set. A pipeline sets a missing secret to "".

    The model's price is read from `PR_VERIFICATION_INPUT_PRICE` and
    `PR_VERIFICATION_OUTPUT_PRICE`, each in dollars for a million tokens, and is given to the
    model as its `TokenPrice`. One that is unset or set to the empty string is 0.0. When both
    are, the model is given no price, as suits a local model. The two say nothing of whether a
    model is configured: with neither a key nor an endpoint the answer is still None.

    Raises `ModelFailed`, with a message that names the variable, when one of the two is set to
    anything but a number that is 0 or more: a model that cannot be made as configured, as
    with a key that cannot be sent.
    """
    endpoint = environment.get("PR_VERIFICATION_ENDPOINT", "")
    key = environment.get("PR_VERIFICATION_API_KEY", "")
    if not endpoint and not key:
        return None
    sent = _price(environment, "PR_VERIFICATION_INPUT_PRICE")
    returned = _price(environment, "PR_VERIFICATION_OUTPUT_PRICE")
    price = None
    if sent is not None or returned is not None:
        price = TokenPrice(input=sent or 0.0, output=returned or 0.0)
    return OpenAICompatibleModel(
        endpoint=endpoint or DEFAULT_ENDPOINT,
        model=environment.get("PR_VERIFICATION_MODEL") or DEFAULT_MODEL,
        key=key,
        transport=transport,
        price=price,
    )


def _price(environment: Mapping[str, str], variable: str) -> float | None:
    """The dollars for a million tokens that `variable` sets, or None when it sets none."""
    text = environment.get(variable, "")
    if not text:
        return None
    try:
        price = float(text)
    except ValueError:
        price = math.nan
    if not 0 <= price < math.inf:
        raise ModelFailed(f"{variable} is set to {text!r}, and must be a number that is 0 or more")
    return price


def _tokens(count: object) -> int | None:
    """A count of tokens as `usage` gives one, or None when it is no whole number of 0 or more."""
    return count if _is_integer(count) and count >= 0 else None


def _is_integer(count: object) -> TypeGuard[int]:
    """Whether `count` is an integer as JSON has one."""
    # In Python a truth value is a whole number too, and in JSON it is not.
    return isinstance(count, int) and not isinstance(count, bool)
