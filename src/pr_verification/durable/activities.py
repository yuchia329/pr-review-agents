"""The Effects of a durable run, made on the machine of a worker.

Every Effect but a CLAIM is made by one activity, `make_effect`, so the code that makes Effects
is in one place, and each Effect's kind and digest are in its activity's ID
(`journal.effect_id`). A failing model or Sandbox is an answer, recorded and not retried, so a
Claim still ends Inconclusive as it does in a run on one machine. Only a lost worker or an
activity that ran out of time is retried: the Effect is then made again, from the start.
"""

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from temporalio import activity

from pr_verification.budget import Clock
from pr_verification.decision import Decider
from pr_verification.durable.journal import Answer, Effect
from pr_verification.effects import Effects, OnThisMachine
from pr_verification.model import Model
from pr_verification.repo_profile import DependencyStore, ProfileStore
from pr_verification.sandbox import ContainerSandbox, SandboxWithHomes, remove_sandbox

LIMIT_REACHED = "DurableLimitReached"
"""The type of the `ApplicationError` that ends an activity or a workflow that went past a limit
of `DurableLimits`. It is never retried."""


class OpenNamedSandbox(Protocol):
    """Opens the named Sandbox for one step, as `ContainerSandbox` does with `name` and `step`.
    Leaving the `with` closes this instance and keeps the Sandbox."""

    def __call__(
        self, repository: Path, image: str, *, name: str, step: str
    ) -> AbstractContextManager[SandboxWithHomes]: ...


@dataclass(frozen=True)
class Resources:
    """What a worker makes Effects with. Made once for a worker, from its own environment, so
    nothing secret is in a workflow's input or history."""

    model: Model
    """The model of every role, or a `RoleModels` with one for each."""

    decider: Decider | None
    """The decider, or None for a worker that has none."""

    store: ProfileStore
    cache: DependencyStore

    open_sandbox: OpenNamedSandbox = ContainerSandbox
    remove_sandbox: Callable[[str], None] = remove_sandbox

    effects: Effects = field(default_factory=OnThisMachine)
    """Makes git commands and random names."""

    clock: Clock = time.monotonic
    """Times each Effect, for its `Answer.seconds`."""

    heartbeat_seconds: float = 10.0
    """How often an activity waiting on a model or a decider tells Temporal it is alive."""


@dataclass(frozen=True)
class EffectTask:
    """What `make_effect` is given."""

    place: int
    effect: Effect
    payload_bytes: int
    """The most the answer may take once encoded, as `DurableLimits.payload_bytes`."""


class Activities:
    """The activities of a worker, each making what it makes with the worker's `Resources`."""

    def __init__(self, resources: Resources) -> None:
        self.resources = resources

    @activity.defn(name="make_effect")
    def make_effect(self, task: EffectTask) -> Answer:
        """Make `task.effect` with the worker's resources, and answer it as the table of
        `pr_verification.durable.journal` says.

        - CAPABILITIES is answered from the worker's models and decider, with no request.
        - MODEL asks `model_for(model, role)`, or the model itself for no role, through
          `ask_priced` when that model has a price and through `ask` otherwise, with
          `dollars` 0.0.
        - DECISION asks the decider.
        - GIT and TOKEN go to `effects`, PROFILE_ to `store`, and ARCHIVE to `cache`.
        - SANDBOX_RUN, HOME_SAVE, and HOME_RESTORE open the Sandbox the Effect names with
          `open_sandbox(repository, image, name=..., step=...)`, where the step is this
          activity's ID, do the one thing asked through `run_in` or the Sandbox's own method,
          and close the instance, which keeps the Sandbox.

        The answer's `seconds` are those between two readings of `clock`, just before and just
        after. An `Exception` the seam raises is the answer's `raised`, and is not raised here.
        While a MODEL or DECISION Effect is under way, the activity heartbeats every
        `heartbeat_seconds` from a thread of its own, so that a lost worker is found within
        the heartbeat timeout and not only at the end of the start-to-close timeout.

        Raises `ValueError` for a CLAIM Effect, which no activity makes. Raises an
        `ApplicationError` of type `LIMIT_REACHED`, not retryable, when the answer would take
        more than `task.payload_bytes` once encoded.
        """
        raise NotImplementedError

    @activity.defn(name="close_sandbox")
    def close_sandbox(self, name: str) -> None:
        """Remove the named Sandbox, with `remove_sandbox`. Removing one already gone does
        nothing, so the activity can be tried again."""
        raise NotImplementedError
