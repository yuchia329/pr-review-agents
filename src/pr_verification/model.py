"""The model interface the project owns, and a scripted stand-in that needs no network."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelRequest:
    """Instructions are written by the project. Data may be Untrusted Content and is kept apart."""

    instructions: str
    data: str


class Model(Protocol):
    def complete(self, request: ModelRequest) -> str: ...


class ScriptExhausted(Exception):
    """A scripted stand-in was asked for more responses than were prepared for it."""


class ScriptedModel:
    """Replays prepared responses in order and records the requests it was sent."""

    def __init__(self, responses: Iterable[str]) -> None:
        self._responses = tuple(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        if len(self.requests) > len(self._responses):
            raise ScriptExhausted(
                f"request {len(self.requests)} has no response: "
                f"only {len(self._responses)} prepared"
            )
        return self._responses[len(self.requests) - 1]
