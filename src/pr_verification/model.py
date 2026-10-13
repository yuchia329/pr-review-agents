"""The model interface the project owns, and a scripted stand-in that needs no network."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, runtime_checkable

from pr_verification.tracing import SpanKind, span


@dataclass(frozen=True)
class ModelRequest:
    """Instructions are written by the project. Data may be Untrusted Content and is kept apart."""

    instructions: str
    data: str

    cache_instructions: bool = False
    """Whether `instructions` are worth the provider's keeping: true when later requests will
    send the very same instructions again, as each step of the supervisor does.

    Prompt caching is an optional capability of the model interface. A model that offers it
    asks its provider to keep the instructions, so that sending them again costs less, and
    says how in its own `complete`. A model that does not offer it ignores this: it makes the
    request it would make with this false. Either way the response is read the same, so
    nothing but the cost and the time of a request depends on it."""


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


def offers_parallel_tool_calls(model: Model) -> bool:
    """Whether `model` may ask for several tools in one response.

    Parallel tool calls are an optional capability of the model interface. A model offers
    them by having an attribute `parallel_tool_calls` that is `True`: this is true then, and
    false for any other model, among them one with no such attribute, and one whose
    attribute is anything but `True` itself. So a model that says nothing is asked for one
    tool at a time, as every model was before the capability existed.

    The model `Ledger.model` gives offers them when, and only when, the model it asks does.
    What a role may do with the capability is said by `run_role` (`pr_verification.agent`).
    """
    return getattr(model, "parallel_tool_calls", None) is True


def ask(model: Model, request: ModelRequest) -> str:
    """Ask `model` through `complete`, recorded as one request to a model
    (`pr_verification.tracing`). Every request the system makes is made through this or
    through `ask_priced`."""
    with span(SpanKind.MODEL_CALL, "model"):
        return model.complete(request)


def ask_priced(model: PricedModel, request: ModelRequest) -> PricedResponse:
    """Ask `model` through `complete_priced`, recorded as `ask` records a request, and with
    what the request cost once the model has answered."""
    with span(SpanKind.MODEL_CALL, "model") as known:
        priced = model.complete_priced(request)
        known["pr_verification.dollars"] = priced.dollars
        return priced


class RoleName(StrEnum):
    """A role the system runs that asks a model. Each value is the name the role's work is
    recorded under. The `locator` of `pr_verification.reading` is not among them: nothing
    runs it yet, and whatever does will say which model it is asked of."""

    CLAIM_EXTRACTOR = "claim-extractor"
    """Lists the Claims of a pull request. Light generation."""

    REPRODUCTION_WRITER = "reproduction-writer"
    """Writes and revises a Reproduction. Hard reasoning."""

    REPRODUCTION_JUDGE = "reproduction-judge"
    """Decides whether a Reproduction exercises its Claim. Medium reasoning."""

    STRUCTURAL_VERIFIER = "structural-verifier"
    """Verifies a Structural Claim by reading. Medium reasoning."""

    REVIEWER = "reviewer"
    """Reviews a pull request and gives Findings. Medium reasoning."""

    SUPERVISOR = "supervisor"
    """Decides how effort is spent on the Claims. Hard reasoning."""


REASONING_ROLES = frozenset(RoleName) - {RoleName.CLAIM_EXTRACTOR}
"""The roles that do hard or medium reasoning: every role but the Claim extractor."""


@dataclass(frozen=True)
class RoleModels:
    """A model for each role. It is itself a `Model`, so it goes wherever one model went.

    Given as the `model` of the core's entry point, it has each role's requests made to that
    role's model, as `run` (`pr_verification.core`) says. Anything that asks it directly,
    through `complete`, is asking for no role, and is answered by `default`: so is the small
    LLM behind a configured decider, which is given the same `model`.
    """

    default: Model
    """The model of every role that `roles` does not name, and of whatever is no role's."""

    roles: Mapping[RoleName, Model] = field(default_factory=dict)
    """The model of each role that has one of its own."""

    def complete(self, request: ModelRequest) -> str:
        """Ask `default` once, with the request exactly as given, and return its response
        exactly as given. Raises whatever `default` raises."""
        return self.default.complete(request)


def model_for(model: Model, role: RoleName) -> Model:
    """The model that does the work of `role`.

    When `model` is a `RoleModels`, the model its `roles` holds for `role`, and its
    `default` when they hold none. Any other model is the model of every role, and is
    returned itself. So what is returned is never a `RoleModels`, and it has the price and
    the capabilities of the one model that will be asked.
    """
    # A model held for a role may itself hold a model for each role.
    while isinstance(model, RoleModels):
        model = model.roles.get(role, model.default)
    return model


class ScriptExhausted(Exception):
    """A scripted stand-in was asked for more responses than were prepared for it."""


class ScriptedModel:
    """Replays prepared responses in order and records the requests it was sent."""

    def __init__(
        self,
        responses: Iterable[str],
        dollars_per_request: float = 0.0,
        *,
        parallel_tool_calls: bool = False,
    ) -> None:
        """`dollars_per_request` is the stand-in's price: what `complete_priced` says each
        request cost. It changes nothing about `complete`.

        `parallel_tool_calls` is the stand-in's attribute of that name, so it says whether
        the stand-in offers parallel tool calls, as `offers_parallel_tool_calls` reads it.
        It changes nothing about `complete` or `complete_priced`.

        The stand-in does not offer prompt caching: a request is recorded in `requests`
        exactly as given, with its `cache_instructions`, and answered the same whichever
        that is."""
        self.parallel_tool_calls = parallel_tool_calls
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
