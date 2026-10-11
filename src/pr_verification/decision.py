"""The decision layer: bounded decisions, answered with a probability for each option.

A decision is a typed question asked of a state. The interface is the project's own, so
that what answers can be changed by configuration: Laya, a decision model run in this
process, which is the default; Jev, a decision model reached over the network; or a small
LLM with structured output. A decision Laya or Jev is not confident of is passed to the
small LLM.

No decision is a Verdict. What is decided here steers the work, such as which kind a Claim
is; Verdicts and the Gate are computed by code from Evidence (ADR 0002).
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from pr_verification.model import Model, ModelFailed

YES = "yes"
NO = "no"

DEFAULT_THRESHOLD = 0.75
"""The threshold used when none is configured. A starting value: it is to be chosen again
from a measured curve, so nothing may rely on the number itself."""


@dataclass(frozen=True)
class YesOrNo:
    """A question with two options: `YES` and `NO`, in that order."""

    text: str
    """The question, written by the project."""


@dataclass(frozen=True)
class Choice:
    """A question with the options it names."""

    text: str
    """The question, written by the project."""

    options: tuple[str, ...]
    """The options to choose between, written by the project: at least two, each different
    from the others and none blank."""


Question = YesOrNo | Choice


def options_of(question: Question) -> tuple[str, ...]:
    """The options a question asks about, in order: `(YES, NO)` for a `YesOrNo`, and
    `options` as given for a `Choice`.

    Raises `ValueError` for a `Choice` with fewer than two options, with two options the
    same, or with a blank option: one that is empty or holds only whitespace.
    """
    raise NotImplementedError


class Implementation(StrEnum):
    """What can answer a decision."""

    LAYA = "laya"
    """Laya, run in this process."""

    JEV = "jev"
    """Jev, reached over the network."""

    SMALL_LLM = "small_llm"
    """A small LLM with structured output, reached through the model interface."""

    STAND_IN = "stand_in"
    """The scripted stand-in. It cannot be configured."""


class PassedOn(StrEnum):
    """Why a decision was passed to the small LLM."""

    LOW_CONFIDENCE = "low_confidence"
    """The top probability of the first answer was under the threshold."""

    INPUT_TOO_LONG = "input_too_long"
    """The input was beyond the input size limit of the implementation first asked."""


@dataclass(frozen=True)
class Decision:
    """The answer to one question: a probability for each option, and the path it took.

    Whatever implementation made it, `probabilities` has exactly the options of the
    question, `options_of(question)`, in that order; each probability is from 0 to 1; and
    together they sum to 1, to within 1e-9.
    """

    question: Question
    """The question that was asked."""

    probabilities: Mapping[str, float]
    """The probability of each option."""

    made_by: Implementation
    """The implementation whose probabilities these are."""

    passed_on: PassedOn | None = None
    """Why the decision was passed to the small LLM, or None when it was not passed on. With
    `made_by`, this is the path the decision took."""

    dollars: float = 0.0
    """What making the decision cost, as the implementations asked know it: 0.0 for one made
    in this process."""

    @property
    def top(self) -> str:
        """The option with the highest probability. Of several that share it, the one that
        comes first in the question."""
        raise NotImplementedError

    @property
    def confidence(self) -> float:
        """The top probability: that of `top`."""
        raise NotImplementedError


class DecisionFailed(ModelFailed):
    """What was asked gave no usable answer to a decision. Its message is safe to print.

    It is a `ModelFailed`, so wherever a failing model is handled, a failing decision is
    handled the same way.
    """


class InputTooLong(Exception):
    """The input is beyond an implementation's input size limit, so it was not asked.

    Raised rather than cutting the input short: an answer to part of the input would pass
    for an answer to all of it.
    """

    size: int
    """The size of the input, in the implementation's own unit."""

    limit: int
    """The most the implementation takes, in the same unit."""

    def __init__(self, size: int, limit: int) -> None:
        """Keeps `size` and `limit`. The message holds both numbers."""
        raise NotImplementedError


class Decider(Protocol):
    """The decision interface: a state and a typed question go in, a `Decision` comes out."""

    def decide(self, state: str, question: Question) -> Decision:
        """Answer `question` about `state`.

        `state` is what the question is asked of, such as the text of a Claim. It may be
        Untrusted Content and is only ever read as data. Every implementation raises
        `ValueError`, as `options_of` does, before doing anything else.
        """
        ...


class ScriptedDecider:
    """The stand-in for tests: replays prepared answers in order and records what was asked.

    It loads no model and reaches no network.
    """

    asked: list[tuple[str, Question]]
    """The state and the question of every call to `decide`, in order, whatever came of it,
    except a call refused for its question as `options_of` refuses it."""

    def __init__(
        self,
        answers: Iterable[Mapping[str, float] | str | Exception],
        made_by: Implementation = Implementation.STAND_IN,
    ) -> None:
        """Each of `answers` is what one call to `decide` gives, in order:

        - a mapping gives a `Decision` with those probabilities, each exactly as prepared,
          in the question's order;
        - a string names an option, which is given probability 1.0 and every other 0.0;
        - an exception is raised.

        Every `Decision` it gives has `made_by` as given here, no `passed_on`, and 0.0
        `dollars`.
        """
        raise NotImplementedError

    def decide(self, state: str, question: Question) -> Decision:
        """Give the next prepared answer.

        Raises `ScriptExhausted` (from `pr_verification.model`) when asked for more answers
        than were prepared. Raises `ValueError` when the prepared answer does not fit the
        question: a mapping whose options are not exactly the question's, in any order,
        or whose probabilities are not each from 0 to 1 and summing to 1 to within 1e-9; or
        a string that is not one of the question's options.
        """
        raise NotImplementedError


class SmallLLMDecider:
    """The decision interface answered by a small LLM with structured output."""

    def __init__(self, model: Model, dollars_per_request: float = 0.0) -> None:
        """`model` is the small LLM. `dollars_per_request` is what one request to it costs:
        the model interface does not report cost, so it is given here."""
        raise NotImplementedError

    def decide(self, state: str, question: Question) -> Decision:
        """Ask the model once and read the probabilities from its response.

        Makes exactly one request. Its `instructions` are written by the project and are
        the same whatever the state and the question. Its `data` is a JSON object with exactly
        three members: `state`, as given; `question`, the question's text; and `options`,
        the list `options_of(question)`. So Untrusted Content reaches the model only as
        data.

        The response is read as a JSON object of the form
        `{"probabilities": {"<option>": <number>, ...}}`. It can be read when its
        `probabilities` name exactly the options of the question, in any order, and every
        number is 0 or more and at least one is more than 0. The numbers are then divided by
        their sum, so the `Decision` keeps the shares the model gave and its probabilities
        sum to 1. Its `made_by` is `SMALL_LLM`, it has no `passed_on`, and its `dollars` are
        `dollars_per_request`.

        Raises `DecisionFailed` when the response cannot be read so: text that is not JSON,
        JSON that is not such an object, an option missing, an option that was not asked
        about, or a number that is negative, not finite, or not a number, as true and false
        are not. Nothing is asked again. `ModelFailed` from the model is not caught.
        """
        raise NotImplementedError


class LayaDecider:
    """The decision interface answered by Laya, run in this process.

    Nothing leaves the machine and no request is made to any model interface.
    """

    input_limit: int
    """The largest input Laya takes, in its own tokens."""

    def __init__(self) -> None:
        """Loads nothing. Laya is loaded into this process by the first call to `decide`
        and kept for the later ones, so a pull request that needs no decision never waits
        for it."""
        raise NotImplementedError

    def decide(self, state: str, question: Question) -> Decision:
        """Answer with Laya's probabilities, in one pass and without generating text.

        The `Decision` has `made_by` `LAYA`, no `passed_on`, and 0.0 `dollars`.

        Raises `InputTooLong` when the state and the question together are beyond
        `input_limit`: the input is never cut short to fit. Raises `DecisionFailed` when
        Laya cannot be loaded.
        """
        raise NotImplementedError


class JevDecider:
    """The decision interface answered by Jev, a decision model reached over the network."""

    def __init__(self, key: str, dollars_per_decision: float = 0.0) -> None:
        """`key` is the key for Jev. It is sent to Jev and nowhere else, and is in no error.
        `dollars_per_decision` is what one decision costs."""
        raise NotImplementedError

    def decide(self, state: str, question: Question) -> Decision:
        """Answer with Jev's probabilities.

        The `Decision` has `made_by` `JEV`, no `passed_on`, and `dollars_per_decision` as
        its `dollars`.

        Raises `DecisionFailed` when Jev cannot be reached or gives no usable answer, and
        `InputTooLong` when the input is beyond what Jev takes. The input is never cut short
        to fit.
        """
        raise NotImplementedError


class FallbackDecider:
    """Asks one implementation first and passes the decision to the small LLM when the
    first is not confident of it or cannot take its input."""

    def __init__(self, first: Decider, small_llm: Decider, threshold: float) -> None:
        """Raises `ValueError` when `threshold` is not from 0 to 1."""
        raise NotImplementedError

    def decide(self, state: str, question: Question) -> Decision:
        """Ask `first`, and pass the decision on only when the rule below says so.

        `first` is asked once, with `state` and `question` as given.

        - When its `confidence` is `threshold` or more, its `Decision` is returned unchanged
          and `small_llm` is not asked.
        - When its `confidence` is under `threshold`, `small_llm` is asked once with the
          same `state` and `question`. What is returned is the `Decision` of `small_llm`
          with `passed_on` set to `LOW_CONFIDENCE` and with the `dollars` of both.
        - When `first` raises `InputTooLong`, `small_llm` is asked once the same way. What
          is returned is its `Decision` with `passed_on` set to `INPUT_TOO_LONG`.

        Nothing else is caught: `DecisionFailed` from `first`, and anything `small_llm`
        raises, is raised from here.
        """
        raise NotImplementedError


class InvalidDecisionConfiguration(Exception):
    """The decision layer is configured with something it cannot use. Its message is safe to
    print: it names the variable and never holds a key."""


@dataclass(frozen=True)
class DecisionConfiguration:
    """Which implementation answers decisions, and when a decision is passed on."""

    implementation: Implementation = Implementation.LAYA
    """What is asked first. Never `STAND_IN`."""

    threshold: float = DEFAULT_THRESHOLD
    """The top probability under which a decision is passed to the small LLM."""

    jev_key: str = field(default="", repr=False)
    """The key for Jev. Not in the printed form of the configuration."""


def decision_configuration(environment: Mapping[str, str]) -> DecisionConfiguration:
    """Read the configuration of the decision layer from three environment variables.

    A variable that is unset or set to the empty string takes its default.

    - `PR_VERIFICATION_DECISION` is the implementation: `laya`, `jev`, or `small_llm`.
      The default is `laya`.
    - `PR_VERIFICATION_DECISION_THRESHOLD` is the threshold, a number from 0 to 1. The
      default is `DEFAULT_THRESHOLD`.
    - `PR_VERIFICATION_JEV_API_KEY` is the key for Jev. The default is no key.

    Raises `InvalidDecisionConfiguration` for any other implementation, `stand_in`
    included; for a threshold that is not a number from 0 to 1; and when the implementation
    is `jev` and there is no key. The message names the variable at fault, which for a
    missing key is `PR_VERIFICATION_JEV_API_KEY`.
    """
    raise NotImplementedError


def configured_decider(
    configuration: DecisionConfiguration, model: Model, *, first: Decider | None = None
) -> Decider:
    """The decider a configuration asks for. `model` is the small LLM.

    - `SMALL_LLM`: a `SmallLLMDecider` on `model`. Nothing is passed on, the threshold is
      not used, and neither Laya nor Jev is loaded or reached.
    - `LAYA` or `JEV`: a `FallbackDecider` with the configuration's threshold, which asks
      that implementation first and passes on to a `SmallLLMDecider` on `model`.

    `first` stands in for Laya or Jev, so that a test loads no model and reaches no
    network. When it is None, a `LayaDecider()` or a `JevDecider(configuration.jev_key)` is
    made here, which loads nothing and reaches nothing until a decision is asked for. Under
    `SMALL_LLM`, `first` is not asked.

    No price is configured, so the decisions of a configured decider all record 0.0
    `dollars`. A comparison of cost is made with deciders that were given their prices.
    """
    raise NotImplementedError
