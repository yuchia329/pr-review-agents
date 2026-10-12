"""The model interface the project owns, and a scripted stand-in that needs no network."""

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@dataclass(frozen=True)
class ModelRequest:
    """Instructions are written by the project. Data may be Untrusted Content and is kept apart."""

    instructions: str
    data: str


class ModelFailed(Exception):
    """The model could not be reached or gave no usable response. Its message is safe to print."""


class Model(Protocol):
    def complete(self, request: ModelRequest) -> str: ...


@dataclass(frozen=True)
class TokenPrice:
    """A model's price: what its provider charges, in dollars for each million tokens."""

    input: float
    """Dollars for a million tokens of what a request sends. 0 or more."""

    output: float
    """Dollars for a million tokens of the response. 0 or more."""


@dataclass(frozen=True)
class PricedResponse:
    """A model's response, and what the request for it cost."""

    response: str
    """The response, exactly as `Model.complete` gives it for the same request."""

    dollars: float
    """What the request cost, at the model's own price. 0 or more."""


@runtime_checkable
class PricedModel(Model, Protocol):
    """A model that says what each request cost, at its own price.

    An optional capability of the model interface. A model without it has no price: a request
    to it is counted as costing no money, as with a local model, which costs time only.
    """

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        """Make the one request `complete` makes, and say what it cost.

        Raises whatever `complete` raises for the same request. What a request that failed
        cost is not known, and nothing is said of it.
        """
        ...


class ScriptExhausted(Exception):
    """A scripted stand-in was asked for more responses than were prepared for it."""


class ScriptedModel:
    """Replays prepared responses in order and records the requests it was sent."""

    def __init__(self, responses: Iterable[str], dollars_per_request: float = 0.0) -> None:
        """`dollars_per_request` is the stand-in's price: what `complete_priced` says each
        request cost. It changes nothing about `complete`."""
        self._responses = tuple(responses)
        self._dollars_per_request = dollars_per_request
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        if len(self.requests) > len(self._responses):
            raise ScriptExhausted(
                f"request {len(self.requests)} has no response: "
                f"only {len(self._responses)} prepared"
            )
        return self._responses[len(self.requests) - 1]

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        """Replay the next prepared response as `complete` does, at the stand-in's price.

        The request is recorded in `requests` exactly as `complete` records it, and the two
        share one script: each call to either takes the next response. `dollars` is the
        `dollars_per_request` the stand-in was made with. Raises `ScriptExhausted` as
        `complete` does.
        """
        return PricedResponse(self.complete(request), self._dollars_per_request)
