"""The model interface over the Anthropic protocol: a second real client beside the
OpenAI-compatible one.

From the point of view of the model interface the two behave the same: a request goes in, the
response's text comes out, and whatever goes wrong is a `ModelFailed` whose message is safe to
print. What differs is what is sent and what comes back, and that this client offers both
optional capabilities of the interface: prompt caching always, and parallel tool calls for
a model it is told can be relied on for them.
"""

import httpx

from pr_verification.model import ModelRequest, PricedResponse, TokenPrice
from pr_verification.openai_compatible import (
    _THINKING,
    _TIMEOUT,
    _TOKENS_PRICED,
    _failed,
    _is_integer,
    _sendable,
    _tokens,
)
from pr_verification.tracing import SpanKind, span

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"
"""The endpoint of the protocol's own provider, up to and including `/v1`."""

ANTHROPIC_VERSION = "2023-06-01"
"""The version of the protocol, sent with every request as the header `anthropic-version`."""

MAX_TOKENS = 16_000
"""The most tokens one response may hold, sent with every request as `max_tokens`. The
protocol does not take a request without it."""

CACHE_WRITE_MULTIPLE = 1.25
"""What a token the provider was asked to keep costs when it is first sent, as a multiple of
the price of a token sent."""

CACHE_READ_MULTIPLE = 0.1
"""What a token the provider had kept costs when it is sent again, as a multiple of the price
of a token sent."""


class AnthropicModel:
    """A model reached through the Anthropic protocol. It is a `PricedModel`."""

    parallel_tool_calls: bool
    """Whether the model offers parallel tool calls, as `offers_parallel_tool_calls`
    (`pr_verification.model`) reads it. False unless the model was made with it true."""

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
        """`endpoint` is the base URL up to and including `/v1`, with or without a `/` after
        it, and `model` is the model's name as the endpoint knows it. `transport` is for a
        caller that brings its own, as a test does: with it no connection is opened.

        `key` is sent without the whitespace around it. A key that is empty then is not
        sent at all, as suits an endpoint that needs none. Raises `ModelFailed` with the
        message `the key for <model> at <endpoint> holds characters that cannot be sent`
        when the key holds anything but printable ASCII: the message does not show the key.

        `price` is what the provider charges for this model, or None for a model that has
        no price. It changes nothing about `complete`. `parallel_tool_calls` is the
        model's attribute of that name, false unless given as for the OpenAI-compatible
        client, and changes nothing about a request.
        """
        self.parallel_tool_calls = parallel_tool_calls
        self._price = price
        self._url = endpoint.rstrip("/") + "/messages"
        self._model = model
        self._name = f"{model} at {endpoint}"
        self._key = _sendable(key, self._name)
        headers = {"anthropic-version": ANTHROPIC_VERSION}
        if self._key:
            headers["x-api-key"] = self._key
        self._client = httpx.Client(headers=headers, timeout=_TIMEOUT, transport=transport)

    def complete(self, request: ModelRequest) -> str:
        """Make one request to the endpoint and return the text of its answer.

        The request
            One `POST` to `<endpoint>/messages`, with the headers `anthropic-version`, which
            is `ANTHROPIC_VERSION`, and `x-api-key`, which is the key, and is left out when
            there is no key. Nothing is retried, and an answer is waited for for up to five
            minutes, as by the OpenAI-compatible client.

            Its body is a JSON object with exactly these members: `model`, the model's name;
            `max_tokens`, which is `MAX_TOKENS`; `system`, which carries
            `request.instructions`; and `messages`, a list of the one message
            `{"role": "user", "content": request.data}`. So instructions and data travel
            apart, and Untrusted Content stays data.

        Prompt caching
            With `request.cache_instructions` false, `system` is the instructions as a
            string. With it true, `system` is a list of the one block
            `{"type": "text", "text": request.instructions,
            "cache_control": {"type": "ephemeral"}}`, which asks the provider to keep the
            instructions. Nothing else of the request differs, and the answer is read the
            same.

        The response
            The answer's `content` is a list of blocks. The response is the `text` of each
            block whose `type` is `text`, in order, joined with nothing between them, then
            without any thinking the model left in front of it as `<think>...</think>`, then
            without the whitespace around it. A block of any other type, such as the
            model's thinking, is no part of the response.

        Failures
            Each of these raises `ModelFailed`, where `<name>` is `<model> at <endpoint>`:

            - the endpoint could not be reached, timed out, or is not a URL:
              `<name> could not be reached: <what went wrong>`;
            - the answer's status is not a success:
              `<name> answered with HTTP <status>: <the answer's body>`;
            - the answer is not JSON, has no such `content`, or its text blocks hold no text
              once read as above: `<name> gave a response that holds no text: <the answer's
              body>`.

            The message is one line: every run of whitespace in it is one space. The key,
            when there is one, is replaced wherever it appears by `[key removed]`, since a
            provider may repeat it back. A message longer than 500 characters is cut to 500
            and ends ` [cut short]`. The error that caused it is not chained.

        The request is recorded as one request to a model (`pr_verification.tracing`), with
        the model's name as `gen_ai.request.model`, and with `gen_ai.usage.input_tokens` and
        `gen_ai.usage.output_tokens` when the answer's `usage` holds `input_tokens` and
        `output_tokens` as integers.
        """
        return self._ask(request)[0]

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        """Make the one request `complete` makes, and say what it cost at the model's price.

        The response is the one `complete` gives, and whatever `complete` raises is raised.

        The protocol reports the tokens of a request in the `usage` of its answer.
        `dollars` is the sum of, each price being for a million tokens:

        - `usage.input_tokens` at `price.input`;
        - `usage.output_tokens` at `price.output`;
        - `usage.cache_creation_input_tokens` at `price.input` times `CACHE_WRITE_MULTIPLE`;
        - `usage.cache_read_input_tokens` at `price.input` times `CACHE_READ_MULTIPLE`.

        So 1,000 tokens sent and 500 returned, at 2.0 and 10.0, cost 0.007, and 1,000,000
        tokens read from what the provider had kept cost 0.2 at that price, where sending
        them afresh costs 2.0. Either of the last two counts is taken as 0 when it is
        missing or is not a whole number that is 0 or more.

        `dollars` is 0.0 when the model was given no price, and when the answer has no
        `usage` holding both of the first two counts as whole numbers that are 0 or more:
        what is not known is not charged for.
        """
        answer, usage = self._ask(request)
        if self._price is None or not isinstance(usage, dict):
            return PricedResponse(answer, 0.0)
        sent = _tokens(usage.get("input_tokens"))
        returned = _tokens(usage.get("output_tokens"))
        if sent is None or returned is None:
            return PricedResponse(answer, 0.0)
        kept = _tokens(usage.get("cache_creation_input_tokens")) or 0
        read = _tokens(usage.get("cache_read_input_tokens")) or 0
        at_input_price = sent + kept * CACHE_WRITE_MULTIPLE + read * CACHE_READ_MULTIPLE
        dollars = (at_input_price * self._price.input + returned * self._price.output) / (
            _TOKENS_PRICED
        )
        return PricedResponse(answer, dollars)

    def _ask(self, request: ModelRequest) -> tuple[str, object]:
        """Make one request. The answer's text, and its `usage` when it has one.

        The request is recorded with the model's name, and with the tokens when `usage` holds
        both counts as integers (`pr_verification.tracing`)."""
        with span(SpanKind.MODEL_CALL, "model") as known:
            known["gen_ai.request.model"] = self._model
            answer, usage = self._post(request)
            if isinstance(usage, dict):
                sent, returned = usage.get("input_tokens"), usage.get("output_tokens")
                if _is_integer(sent) and _is_integer(returned):
                    known["gen_ai.usage.input_tokens"] = sent
                    known["gen_ai.usage.output_tokens"] = returned
            return answer, usage

    def _post(self, request: ModelRequest) -> tuple[str, object]:
        """Send one request to the endpoint, and read its answer as `_ask` gives it."""
        system: str | list[dict[str, object]] = request.instructions
        if request.cache_instructions:
            system = [
                {
                    "type": "text",
                    "text": request.instructions,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        # Instructions and data travel apart, so Untrusted Content stays data.
        body = {
            "model": self._model,
            "max_tokens": MAX_TOKENS,
            "system": system,
            "messages": [{"role": "user", "content": request.data}],
        }
        try:
            response = self._client.post(self._url, json=body)
        except (httpx.HTTPError, httpx.InvalidURL) as error:
            # Not chained: the cause would be printed with any traceback, unredacted.
            raise _failed(self._name, self._key, f"could not be reached: {error}") from None
        if not response.is_success:
            raise _failed(
                self._name,
                self._key,
                f"answered with HTTP {response.status_code}: {response.text}",
            )
        try:
            answered = response.json()
            text = _text_of(answered["content"])
        except (ValueError, LookupError, TypeError, RecursionError):
            text = ""
        answer = _THINKING.sub("", text).strip()
        if not answer:
            raise _failed(
                self._name, self._key, f"gave a response that holds no text: {response.text}"
            )
        return answer, answered.get("usage")


def _text_of(content: object) -> str:
    """The text of an answer's `content`: that of each of its text blocks, in order. Raises
    `TypeError` when it is no list of blocks."""
    if not isinstance(content, list):
        raise TypeError("the content is no list")
    texts = []
    for block in content:
        if not isinstance(block, dict):
            raise TypeError("a block is no object")
        if block.get("type") == "text":
            text = block.get("text")
            if not isinstance(text, str):
                raise TypeError("a text block holds no text")
            texts.append(text)
    return "".join(texts)
