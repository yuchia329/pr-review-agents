"""The Effects of a durable run, made on the machine of a worker.

Every Effect but a CLAIM is made by one activity, `make_effect`, so the code that makes Effects
is in one place, and each Effect's kind and digest are in its activity's ID
(`journal.effect_id`). A failing model or Sandbox is an answer, recorded and not retried, so a
Claim still ends Inconclusive as it does in a run on one machine. Only a lost worker or an
activity that ran out of time is retried: the Effect is then made again, from the start.
"""

import contextvars
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from temporalio import activity
from temporalio.exceptions import ApplicationError

from pr_verification.budget import Clock
from pr_verification.decision import Decider
from pr_verification.durable.codec import encoded_size
from pr_verification.durable.journal import Answer, Capabilities, Effect, EffectKind
from pr_verification.ecosystem import find_commands
from pr_verification.effects import Effects, OnThisMachine
from pr_verification.model import (
    Model,
    ModelRequest,
    PricedModel,
    PricedResponse,
    RoleName,
    ask,
    ask_priced,
    model_for,
    offers_parallel_tool_calls,
)
from pr_verification.repo_profile import DependencyStore, Discover, ProfileStore
from pr_verification.sandbox import ContainerSandbox, SandboxWithHomes, remove_sandbox, run_in

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

    discover: Discover = find_commands
    """Works out the repository's Ecosystem and how to install and test it."""

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
        - DECISION asks the decider, and DISCOVER `discover`.
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
        effect = task.effect
        if effect.kind is EffectKind.CLAIM:
            raise ValueError("the work on a Claim is made by a workflow, not an activity")
        started = self.resources.clock()
        try:
            if effect.kind in (EffectKind.MODEL, EffectKind.DECISION):
                with _beating(self.resources.heartbeat_seconds):
                    value = self._answer(effect)
            else:
                value = self._answer(effect)
            answer = Answer(value=value, seconds=self.resources.clock() - started)
        except Exception as raised:
            answer = Answer(raised=raised, seconds=self.resources.clock() - started)
        size = encoded_size(answer)
        if size > task.payload_bytes:
            raise ApplicationError(
                f"the answer to a {effect.kind.value} Effect takes {size} bytes, past the "
                f"limit of {task.payload_bytes}",
                type=LIMIT_REACHED,
                non_retryable=True,
            )
        return answer

    def _answer(self, effect: Effect) -> object:
        """What the seam gives for `effect`, or what it raises."""
        made = self.resources
        asked: tuple[Any, ...] = effect.asked
        match effect.kind:
            case EffectKind.CAPABILITIES:
                return Capabilities(
                    parallel_tool_calls=tuple(
                        role
                        for role in RoleName
                        if offers_parallel_tool_calls(model_for(made.model, role))
                    ),
                    decider=made.decider is not None,
                )
            case EffectKind.MODEL:
                role, request = asked
                model = made.model if role is None else model_for(made.model, role)
                assert isinstance(request, ModelRequest)
                if isinstance(model, PricedModel):
                    return ask_priced(model, request)
                return PricedResponse(ask(model, request), 0.0)
            case EffectKind.DECISION:
                if made.decider is None:
                    raise ValueError("this worker has no decider")
                state, question = asked
                return made.decider.decide(state, question)
            case EffectKind.DISCOVER:
                repository, commit = asked
                return made.discover(repository, commit)
            case EffectKind.GIT:
                repository, arguments, stdin, environment = asked
                return made.effects.git(
                    repository,
                    arguments,
                    stdin=stdin,
                    environment=dict(environment),
                )
            case EffectKind.TOKEN:
                (size,) = asked
                return made.effects.token_hex(size)
            case EffectKind.PROFILE_LOAD:
                return made.store.load(*asked)
            case EffectKind.PROFILE_SAVE:
                made.store.save(*asked)
                return None
            case EffectKind.PROFILE_DISCARD:
                made.store.discard(*asked)
                return None
            case EffectKind.ARCHIVE:
                return made.cache.archive(*asked)
            case EffectKind.ARCHIVE_HOLDS:
                return made.cache.holds(*asked)
            case EffectKind.ARCHIVE_DISCARD:
                made.cache.discard(*asked)
                return None
            case EffectKind.SANDBOX_RUN | EffectKind.HOME_SAVE | EffectKind.HOME_RESTORE:
                return self._in_sandbox(effect)
        raise ValueError(f"no activity makes a {effect.kind.value} Effect")

    def _in_sandbox(self, effect: Effect) -> object:
        asked: tuple[Any, ...] = effect.asked
        name, repository, image, *rest = asked
        step = activity.info().activity_id
        with self.resources.open_sandbox(
            repository,
            image,
            name=name,
            step=step,
        ) as sandbox:
            if effect.kind is EffectKind.SANDBOX_RUN:
                command, commit, phase, limits = rest
                return run_in(sandbox, command, commit=commit, phase=phase, limits=limits)
            commit, archive = rest
            if effect.kind is EffectKind.HOME_SAVE:
                sandbox.save_home(commit, archive)
            else:
                sandbox.restore_home(commit, archive)
            return None

    @activity.defn(name="close_sandbox")
    def close_sandbox(self, name: str) -> None:
        """Remove the named Sandbox, with `remove_sandbox`. Removing one already gone does
        nothing, so the activity can be tried again."""
        self.resources.remove_sandbox(name)


@contextmanager
def _beating(every: float) -> Iterator[None]:
    """Heartbeat every `every` seconds from a thread of its own, while the `with` runs."""
    done = threading.Event()
    beat = activity.heartbeat

    def keep_beating() -> None:
        while not done.wait(every):
            beat()

    # The thread is given the activity's context, which is where Temporal keeps the activity.
    beating = threading.Thread(target=contextvars.copy_context().run, args=(keep_beating,))
    beating.start()
    try:
        yield
    finally:
        done.set()
        beating.join()
