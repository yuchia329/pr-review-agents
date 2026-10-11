"""The agent loop: a role asks the model what to do, one step at a time, until it finishes.

The loop is the project's own code on top of the model interface. No agent framework is used,
because the caps and the handling of Untrusted Content need control of the loop itself.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum

from pr_verification.model import Model

NO_SUCH_TOOL = "This role has no tool of that name, so nothing was run."
"""What a role is told when it asks for a tool it was not given."""

UNREADABLE_RESPONSE = (
    "The response could not be read. Reply with one JSON object and nothing else: "
    'either {"tool": "<name>", "arguments": {...}} or {"finish": {...}}.'
)
"""What a role is told when its response is neither a request for a tool nor a finish."""


@dataclass(frozen=True)
class Tool:
    """One thing a role may ask to have done. A role has only the tools it is declared with."""

    name: str
    """What the model calls the tool by."""

    description: str
    """What the tool does and the arguments it takes, written by the project for the model."""

    run: Callable[[Mapping[str, object]], str]
    """Does the work for the arguments the model gave and returns what the model is shown.

    The arguments are whatever JSON object the model wrote, so they are checked here. A tool
    answers every request with text: when it cannot do what was asked, the text says why. It
    does not raise for what the arguments hold. What it returns may be Untrusted Content."""


@dataclass(frozen=True)
class Role:
    """An agent with one job: its own instructions, its own tools, and its own step cap."""

    name: str
    """What the role is called in the record of its run."""

    instructions: str
    """What the role is for and what it finishes with, written by the project."""

    tools: tuple[Tool, ...]
    """The tools this role may use, each with a different name. It can use no other."""

    step_cap: int
    """The most steps one run of the role may take. At least 1."""


class StepOutcome(StrEnum):
    """What came of one step."""

    RAN_TOOL = "ran_tool"
    """The response asked for one of the role's tools, and it was run."""

    REFUSED = "refused"
    """The response asked for a tool the role was not given. Nothing was run."""

    UNREADABLE = "unreadable"
    """The response was neither a request for a tool nor a finish. Nothing was run."""

    FINISHED = "finished"
    """The response was a finish, which ends the run."""


@dataclass(frozen=True)
class Step:
    """One request to the model and what came of its response."""

    response: str
    """The model's response, exactly as it gave it."""

    outcome: StepOutcome
    """What came of the response."""

    tool: str | None
    """The name of the tool that was run, or None when the outcome is not `RAN_TOOL`."""

    result: str
    """What the role is shown for this step: what the tool returned for `RAN_TOOL`, the
    text `NO_SUCH_TOOL` for `REFUSED`, the text `UNREADABLE_RESPONSE` for `UNREADABLE`, and
    the empty string for `FINISHED`."""


class Stopped(StrEnum):
    """Why a run of a role ended."""

    FINISHED = "finished"
    """The model finished."""

    STEP_CAP = "step_cap"
    """The run had taken `Role.step_cap` steps without the model finishing."""


@dataclass(frozen=True)
class RoleRun:
    """The record of one run of a role: every step it took and why it stopped."""

    role: str
    """The role's name."""

    steps: tuple[Step, ...]
    """Every step, in the order taken. Never more than the role's step cap."""

    stopped: Stopped
    """Why the run ended."""

    answer: Mapping[str, object] | None
    """The JSON object the model finished with, or None when the run stopped at the step cap.
    It is the model's word, shaped by whatever the role read: data to check, never a Verdict."""


def run_role(role: Role, task: Mapping[str, object], model: Model) -> RoleRun:
    """Run `role` on `task` until the model finishes or the step cap is reached.

    `task` is what the role is asked to work on, such as its Claim, as a JSON object. It may
    hold Untrusted Content. Each call is a run of its own: its context holds only `task` and
    what this run's own steps gave, and nothing carries over from any other call.

    A step is one request to the model and what comes of the response. In every request:

    - `instructions` hold `role.instructions` whole, and the name and the description of
      each of `role.tools` whole, beside the loop's own fixed wording on how to answer. They
      are written from nothing else: they are the same on every step of a run, and hold
      nothing of `task`, of any response, or of anything a tool returned.
    - `data` is a JSON object with exactly two members: `task`, as given, and `steps`, a list
      with one object for each step taken so far, in order, whose only members are that
      step's `response` and `result`. Everything the role reads reaches the model here, as
      data.

    The response is read as a JSON object and decides the step's outcome. A response can be
    read when it is one of these and has no other member:

    - `{"tool": "<name>", "arguments": {...}}` asks for a tool, where the name is a string
      and the arguments are an object. `arguments` may be left out, which gives the tool an
      empty object. When the name is that of one of `role.tools`, that tool is run once with
      the arguments and the outcome is `RAN_TOOL`. When it is not, nothing is run and the
      outcome is `REFUSED`.
    - `{"finish": {...}}` ends the run, where what it finishes with is an object. The
      outcome is `FINISHED` and that object is the run's `answer`.

    Anything else is `UNREADABLE`, and that is settled before any tool is looked up: text
    that is not JSON, JSON that is not an object, an object with any other member or with
    both `tool` and `finish`, a `tool` that is not a string, or `arguments` or `finish` that
    is not an object.

    Every outcome counts as a step, and `Step` says what each records. After a step that
    does not finish, the role is asked again, unless the run has taken `role.step_cap`
    steps: then it stops with `Stopped.STEP_CAP` and makes no further request. The cap is
    enforced here, by code, whatever the model says, so a run never makes more than
    `role.step_cap` requests to the model.

    Raises `ValueError` before any request when `role.step_cap` is less than 1 or two of
    `role.tools` share a name. `ModelFailed` and whatever a tool raises are not caught.
    """
    raise NotImplementedError
