"""The model interface over the OpenAI-compatible protocol: a hosted endpoint or a local Ollama."""

import re
from collections.abc import Mapping

import httpx

from pr_verification.model import ModelFailed, ModelRequest

DEFAULT_ENDPOINT = "https://api.minimax.io/v1"
DEFAULT_MODEL = "MiniMax-M3"

# A local model may first have to be loaded, and httpx on its own gives up after five seconds.
_TIMEOUT = httpx.Timeout(300.0, connect=10.0)
# Some models put their thinking in front of the answer, in the same text.
_THINKING = re.compile(r"\A\s*<think>.*?</think>", re.DOTALL)
_LONGEST_FAILURE = 500


class OpenAICompatibleModel:
    def __init__(
        self,
        endpoint: str,
        model: str,
        key: str,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
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
            text = response.json()["choices"][0]["message"]["content"]
        except (ValueError, LookupError, TypeError, RecursionError):
            text = None
        answer = _THINKING.sub("", text).strip() if isinstance(text, str) else ""
        if not answer:
            raise self._failed(f"gave a response that holds no text: {response.text}")
        return answer

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
    """None when neither a key nor an endpoint is set. A pipeline sets a missing secret to ""."""
    endpoint = environment.get("PR_VERIFICATION_ENDPOINT", "")
    key = environment.get("PR_VERIFICATION_API_KEY", "")
    if not endpoint and not key:
        return None
    return OpenAICompatibleModel(
        endpoint=endpoint or DEFAULT_ENDPOINT,
        model=environment.get("PR_VERIFICATION_MODEL") or DEFAULT_MODEL,
        key=key,
        transport=transport,
    )
