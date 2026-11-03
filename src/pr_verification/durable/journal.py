"""The Journal of a durable run, and the core's own code replayed against it.

Nothing here uses Temporal, so all of it can be run and tested as plain code. A workflow
(`pr_verification.durable.workflows`) holds a Journal and calls `replay_run` or `replay_claim`
with it again after each Effect it makes.

The Effects
    Each Effect is one call behind a seam that `pr_verification.effects` names, and an `Effect`
    says which, with what it was asked. Its `Answer` holds what the seam gave, or what it
    raised. The kinds, what each is asked, and what answers it:

    ================  ==================================================  =====================
    Kind              `asked`                                             Answered by
    ================  ==================================================  =====================
    CAPABILITIES      ()                                                  `Capabilities`
    MODEL             (role or None, `ModelRequest`)                      `PricedResponse`
    DECISION          (state, `Question`)                                 `Decision`
    DISCOVER          (repository, commit)                                `Commands` or
                                                                          `Unsupported`
    GIT               (repository, arguments, stdin, environment)         `bytes`
    TOKEN             (size,)                                             `str`
    PROFILE_LOAD      (repository,)                                       `RepoProfile` or None
    PROFILE_SAVE      (repository, `RepoProfile`)                         None
    PROFILE_DISCARD   (repository,)                                       None
    ARCHIVE           (repository, image, lock digest)                    `Path`
    ARCHIVE_HOLDS     (archive,)                                          `bool`
    ARCHIVE_DISCARD   (archive,)                                          None
    SANDBOX_RUN       (Sandbox name, repository, image, command, commit,  `RunResult`
                      `Phase`, `Limits`)
    HOME_SAVE         (Sandbox name, repository, image, commit, archive)  None
    HOME_RESTORE      (Sandbox name, repository, image, commit, archive)  None
    CLAIM             (`ClaimInput`,)                                     `ClaimOutcome`
    ================  ==================================================  =====================

    A repository and an archive are a `Path`, arguments and a command a tuple of `str`, and
    the environment of a git command its items as pairs, sorted. A MODEL Effect asked for no
    role is a request to the model of no role, as `RoleModels.complete` makes one. Its answer
    has `dollars` 0.0 when the worker's model has no price, as the ledger counts a request to
    such a model.

    What is not an Effect
        The clock. A replay's clock is `Replay.clock`: the seconds its Effects took where they
        were made, added up. So the Budget's time is spent by the time Effects took, and not
        by time in which no worker was running, or in which the code was replayed (ADR 0010).

        Opening and closing a Sandbox. A Sandbox is named for the run, and the Effects that
        use it name it, so that the worker making each one finds it (`ContainerSandbox` with
        `name`). Closing it in a replay does nothing. The workflow removes it once the run is
        over, however the run ended.
"""

import hashlib
import io
import pickle
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Any, Self

from pr_verification.budget import Budget, BudgetSpent, Ledger, LedgerState
from pr_verification.context_bound import ContextLimits, shown_within
from pr_verification.core import PullRequest, run
from pr_verification.decision import Decision, Question
from pr_verification.ecosystem import Commands, ReproductionEcosystem, Unsupported
from pr_verification.effects import effects_through
from pr_verification.model import Model, ModelRequest, PricedResponse, RoleModels, RoleName
from pr_verification.orchestration import FixedPipeline, Gathered, Ready, WorkOnClaim
from pr_verification.repo_profile import RepoProfile
from pr_verification.report import ClaimKind, Report
from pr_verification.sandbox import Command, Limits, Phase, RunResult
from pr_verification.tracing import paused

PAYLOAD_BYTES = 1_500_000
"""The most one Effect, or one answer, may take once encoded (`codec.encoded_size`). Temporal
refuses a payload over 2 MB, and a refusal is retried without end, so the limit is kept here,
below Temporal's."""

JOURNAL_BYTES = 30_000_000
"""The most the Effects and answers of one workflow's Journal may take together, once encoded.
Temporal ends a workflow whose history passes 50 MB, and replaying a long Journal takes long, so
the limit is kept here, below Temporal's and below the 40 MB at which the measurement of
durability stops."""

LIMIT_REACHED = (
    "The work on this Claim grew past what one durable run can keep, so it was stopped: {limit}"
)
"""Why a Claim is Inconclusive when the workflow doing its work went past `DurableLimits`.
`limit` says which limit, and by how much. It holds nothing of what the Claim's work made."""


class EffectKind(StrEnum):
    """Which seam an Effect is made behind. The table in this module's docstring says what each
    is asked and what answers it."""

    CAPABILITIES = "capabilities"
    MODEL = "model"
    DECISION = "decision"
    DISCOVER = "discover"
    GIT = "git"
    TOKEN = "token"
    PROFILE_LOAD = "profile-load"
    PROFILE_SAVE = "profile-save"
    PROFILE_DISCARD = "profile-discard"
    ARCHIVE = "archive"
    ARCHIVE_HOLDS = "archive-holds"
    ARCHIVE_DISCARD = "archive-discard"
    SANDBOX_RUN = "sandbox-run"
    HOME_SAVE = "home-save"
    HOME_RESTORE = "home-restore"
    CLAIM = "claim"


@dataclass(frozen=True)
class Effect:
    """One Effect a replay asked for: its kind, and what it was asked."""

    kind: EffectKind
    asked: tuple[object, ...]


@dataclass(frozen=True)
class Answer:
    """What answered one Effect where it was made."""

    value: object = None
    """What the seam gave. None when it raised."""

    raised: Exception | None = None
    """What the seam raised, or None. A replay raises it again where the Effect was asked, so
    a failing model is a `ModelFailed` in the replay as in a run on one machine."""

    seconds: float = 0.0
    """How long the Effect took where it was made. 0 or more. For a CLAIM Effect, 0.0: the time
    the Claim's work took is in the ledger its outcome holds."""


@dataclass(frozen=True)
class Capabilities:
    """What the worker's models and decider offer, which the code asks of them without making a
    request. Asked once, as a run's first Effect, since the worker is what makes the
    requests."""

    parallel_tool_calls: tuple[RoleName, ...]
    """The roles whose model offers parallel tool calls (`offers_parallel_tool_calls`), in the
    order of `RoleName`."""

    decider: bool
    """Whether the worker has a decider. Without one, the run is given none, as the core's
    entry point is when it has none."""


@dataclass(frozen=True)
class DurableLimits:
    """The limits a durable run keeps to, so that it ends where Temporal would otherwise retry
    without end. A test can give smaller ones."""

    payload_bytes: int = PAYLOAD_BYTES
    journal_bytes: int = JOURNAL_BYTES


class NeedsEffect(BaseException):  # noqa: N818 - it is no error: the replay has gone as far as it can
    """A replay asked for an Effect that its Journal holds no answer for.

    It derives from `BaseException`, so no `except Exception` in the code replayed takes it for
    a failure and swallows it.
    """

    def __init__(self, place: int, effect: Effect) -> None:
        super().__init__(place, effect)
        self.place = place
        """The Effect's place in the Journal, counted from 0: the number of answers it holds."""
        self.effect = effect


class JournalMismatch(Exception):  # noqa: N818 - named for what it finds
    """A replay asked, at some place, for another Effect than the one its Journal holds there.

    The code replayed is not what made the Journal, or it is not deterministic. Either is a
    fault in the system, never a failing model or Sandbox.
    """


def effect_id(place: int, effect: Effect) -> str:
    """The ID of the activity that makes `effect` at `place` in a Journal.

    It is `place`, then the kind, then a digest of what was asked, joined by `-`, and it holds
    only lowercase letters, digits, and `-`. The digest is the same in every process and on
    every machine for what is equal, and differs for what is asked differently. Temporal
    compares activity IDs, and not what an activity was given, when it replays a workflow's
    history, so with the digest in the ID a replay that asks for another Effect at a place than
    its history holds fails, where without it the history's answer would be given.
    """
    digest = hashlib.sha256(_canonical(effect.asked)).hexdigest()[:16]
    return f"{place}-{effect.kind.value}-{digest}"


def sandbox_name(run: str, place: int) -> str:
    """The name of the Sandbox that run `run` opened at `place` among the Sandboxes it opened,
    counted from 0. Each Sandbox a run opens is a new one, as `SandboxFor` says, so the place
    is in the name. Holds only lowercase letters, digits, and `-`, as `run` does."""
    return f"{run}-sandbox-{place}"


@dataclass
class Replay:
    """One replay of the core's code against a Journal: the Effects made so far in a run, each
    with its answer, in the order made.

    Asking
        `made` is asked for each Effect in the order the code asks for them. While the Journal
        holds an answer at the next place, the Effect asked must be the one held there, and
        the answer is given: its `value` is returned, or its `raised` raised. Past the last
        answer, `made` raises `NeedsEffect` for the Effect asked, at the place after the last.

        No Effect during unwinding
            Once `made` has raised `NeedsEffect`, every later call raises that same
            `NeedsEffect` again, whatever it is asked. So a `finally`, a `__exit__`, or an
            `ExitStack` that asks for an Effect while the `NeedsEffect` unwinds the code does
            not put its own Effect in place of the one the run needs: the run's next Effect is
            always the first one the Journal could not answer.
    """

    journal: Sequence[tuple[Effect, Answer]]

    used: int = field(default=0, init=False)
    """How many answers have been given."""

    needed: NeedsEffect | None = field(default=None, init=False)
    """The `NeedsEffect` raised, once one has been."""

    _seconds: float = field(default=0.0, init=False, repr=False)

    def made(self, effect: Effect) -> object:
        """The answer to `effect`, which is the next Effect the code asks for, as Asking says.

        Raises `JournalMismatch` when the Journal holds another Effect at that place. Raises
        the answer's `raised` when it has one. Raises `NeedsEffect` past the Journal's end.
        """
        if self.needed is not None:
            raise self.needed
        if self.used == len(self.journal):
            self.needed = NeedsEffect(self.used, effect)
            raise self.needed
        held, answer = self.journal[self.used]
        if held != effect:
            raise JournalMismatch(
                f"the Journal holds a {held.kind.value} Effect at {self.used}, and the code asked "
                f"for a {effect.kind.value} Effect, or for one asked differently"
            )
        self.used += 1
        self._seconds += answer.seconds
        if answer.raised is not None:
            raise answer.raised.with_traceback(None)
        return answer.value

    def clock(self) -> float:
        """The replay's clock: the `seconds` of the answers given so far, added up. Starts at
        0.0 and never goes back."""
        return self._seconds


@dataclass(frozen=True)
class RunInput:
    """What `VerifyPullRequest` is given: no secret, since a workflow's input is kept in its
    history. Models, decider, and stores are the worker's."""

    run: str
    """Names the run, and each Sandbox it opens (`sandbox_name`): the workflow's ID. Holds
    only lowercase letters, digits, and `-`."""

    pull_request: PullRequest
    """The pull request, with its repository as a directory on the workers' machine."""

    budget: Budget = field(default_factory=Budget)

    shown: ContextLimits = field(default_factory=ContextLimits)
    """What a role is shown of any one thing it reads (`pr_verification.context_bound`). In
    the input, and not read from a worker's environment, so that every replay is bounded
    alike."""

    limits: DurableLimits = field(default_factory=DurableLimits)


@dataclass(frozen=True)
class SandboxReady:
    """What a Claim's work is given of the `Ready` that preparing gave: the Sandbox by its name,
    since a Sandbox object does not cross to another workflow."""

    name: str
    image: str
    ecosystem: ReproductionEcosystem


@dataclass(frozen=True)
class ClaimInput:
    """What `VerifyClaim` is given: all that the work on one Claim reads."""

    run: str
    pull_request: PullRequest
    """The pull request, with `before` and `after` as full commit IDs."""

    place: int
    """The Claim's place among the Claims of the pull request, counted from 0."""

    claim: str
    kind: ClaimKind | None
    ready: SandboxReady | None
    """Where a Reproduction is run, or None when preparing gave no `Ready`."""

    ledger: LedgerState
    """The run's ledger as it was when the work on the Claim was asked for."""

    capabilities: Capabilities
    shown: ContextLimits = field(default_factory=ContextLimits)
    limits: DurableLimits = field(default_factory=DurableLimits)


@dataclass(frozen=True)
class ClaimOutcome:
    """What came of the work on one Claim: what it is given, or the `BudgetSpent` that ended the
    work, never both. `ledger` is the run's ledger as the work left it."""

    gathered: Gathered | None
    spent: BudgetSpent | None
    ledger: LedgerState


def replay_run(given: RunInput, replay: Replay) -> Report:
    """The report of the core's entry point, run on `given.pull_request` against `replay`.

    The run's first Effect is CAPABILITIES. Then `run` (`pr_verification.core`) is called with
    the pull request, `given.budget`, `replay.clock` as its clock, a `RoleModels` that holds a
    model for every role, a decider when the Capabilities say the worker has one and None
    otherwise, and the fixed pipeline. Each model, the decider, and the pipeline's store,
    cache, and Sandboxes ask `replay` for an Effect in place of each call they would make, as
    the table of this module says, and the model of each role offers parallel tool calls when
    the Capabilities say so. Every git command and random name is asked for through
    `effects_through`, nothing is recorded while the code runs (`tracing.paused`), and what a
    role is shown is bounded by `given.shown` (`shown_within`). The Review is not asked for.

    The pipeline is `FixedPipeline` with the defaults it has, but for those seams, its
    `discover`, which asks for a DISCOVER Effect, its `progress_guard`, which is the decider,
    and its `work_on_claim`. That asks for one CLAIM
    Effect for each Claim worked on, with a `ClaimInput` that holds `ledger.snapshot()`, makes
    the ledger hold the outcome's `ledger` (`Ledger.adopt`), and then gives the outcome's
    `gathered` or raises its `spent`. So the Claims are worked on one after another, as
    `FixedPipeline` works on them, each in a workflow of its own.

    So given a Journal of the answers a run on one machine had, with the same Capabilities,
    the report is equal to that run's report, but for seconds, which are those the Effects
    took.

    Raises `NeedsEffect` while the Journal does not hold every answer the run needs, and
    `JournalMismatch` as `Replay.made` raises it. Raises what the core's entry point raises.
    """
    capabilities = replay.made(Effect(EffectKind.CAPABILITIES, ()))
    assert isinstance(capabilities, Capabilities)
    seams = _Seams(replay, given.run, capabilities)
    with effects_through(seams), paused(), shown_within(given.shown):
        pipeline = seams.pipeline(_ClaimElsewhere(replay, given, capabilities))
        return run(
            given.pull_request,
            seams.model,
            seams.decider,
            orchestration=pipeline,
            budget=given.budget,
            clock=replay.clock,
        )


def replay_claim(given: ClaimInput, replay: Replay) -> ClaimOutcome:
    """What came of the work on Claim `given.place`, done against `replay`.

    The ledger is `Ledger.restored(given.ledger, clock=replay.clock)`, and the work is
    `FixedPipeline.work_in_place` with that ledger, the Claim, its place and kind, a `Ready`
    whose Sandbox is the one `given.ready` names, and the models and decider that `replay_run`
    makes, through the seams it makes. The outcome holds what that gives, or the
    `BudgetSpent` it raises, with `snapshot()` of the ledger after it.

    So `replay_run` given each CLAIM Effect's answer from here gives the report a run on one
    machine gives, as it says.

    Raises `NeedsEffect` and `JournalMismatch` as `replay_run` does. Raises what
    `work_in_place` raises but `BudgetSpent`.
    """
    seams = _Seams(replay, given.run, given.capabilities)
    ready = None
    if given.ready is not None:
        sandbox = _Sandbox(
            replay, given.ready.name, given.pull_request.repository, given.ready.image
        )
        ready = Ready(sandbox, given.ready.ecosystem)
    with effects_through(seams), paused(), shown_within(given.shown):
        ledger = Ledger.restored(given.ledger, clock=replay.clock)
        try:
            gathered = seams.pipeline(None).work_in_place(
                ledger,
                given.place,
                given.claim,
                given.kind,
                ready,
                repository=given.pull_request.repository,
                before=given.pull_request.before,
                after=given.pull_request.after,
                model=seams.model,
            )
        except BudgetSpent as spent:
            return ClaimOutcome(None, spent, ledger.snapshot())
        return ClaimOutcome(gathered, None, ledger.snapshot())


def _canonical(value: object) -> bytes:
    """`value` written so that what is equal is written the same in every process: sets and
    dicts in a sorted order, and nothing written once and referred to after."""
    buffer = io.BytesIO()
    pickler = _CanonicalPickler(buffer, protocol=5)
    pickler.fast = True
    pickler.dump(value)
    return buffer.getvalue()


# The pure-Python pickler, since the one in C does not ask `reducer_override` about sets and dicts.
class _CanonicalPickler(pickle._Pickler):
    def reducer_override(self, obj: object) -> Any:
        if type(obj) in (set, frozenset):
            return type(obj), (sorted(obj, key=_canonical),)  # type: ignore[call-overload]
        if type(obj) is dict:
            items = sorted(obj.items(), key=lambda item: _canonical(item[0]))
            return dict, (items,)
        return NotImplemented


class _Model:
    """A role's model, whose every request is a MODEL Effect. It has a price, which is 0.0 when
    the worker's model has none, as the ledger counts a request to such a model."""

    def __init__(self, replay: Replay, role: RoleName | None, parallel: bool) -> None:
        self._replay, self._role = replay, role
        self.parallel_tool_calls = parallel

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        answer = self._replay.made(Effect(EffectKind.MODEL, (self._role, request)))
        assert isinstance(answer, PricedResponse)
        return answer

    def complete(self, request: ModelRequest) -> str:
        return self.complete_priced(request).response


class _Decider:
    def __init__(self, replay: Replay) -> None:
        self._replay = replay

    def decide(self, state: str, question: Question) -> Decision:
        answer = self._replay.made(Effect(EffectKind.DECISION, (state, question)))
        assert isinstance(answer, Decision)
        return answer


class _Profiles:
    def __init__(self, replay: Replay) -> None:
        self._replay = replay

    def load(self, repository: Path) -> RepoProfile | None:
        answer = self._replay.made(Effect(EffectKind.PROFILE_LOAD, (repository,)))
        assert answer is None or isinstance(answer, RepoProfile)
        return answer

    def save(self, repository: Path, profile: RepoProfile) -> None:
        self._replay.made(Effect(EffectKind.PROFILE_SAVE, (repository, profile)))

    def discard(self, repository: Path) -> None:
        self._replay.made(Effect(EffectKind.PROFILE_DISCARD, (repository,)))


class _Dependencies:
    def __init__(self, replay: Replay) -> None:
        self._replay = replay

    def archive(self, repository: Path, image: str, lock_digest: str) -> Path:
        answer = self._replay.made(Effect(EffectKind.ARCHIVE, (repository, image, lock_digest)))
        assert isinstance(answer, Path)
        return answer

    def holds(self, archive: Path) -> bool:
        answer = self._replay.made(Effect(EffectKind.ARCHIVE_HOLDS, (archive,)))
        assert isinstance(answer, bool)
        return answer

    def discard(self, archive: Path) -> None:
        self._replay.made(Effect(EffectKind.ARCHIVE_DISCARD, (archive,)))


class _Sandbox:
    """The Sandbox named `name`, whose every use is an Effect. Closing it does nothing: the
    workflow removes it once the run is over."""

    def __init__(self, replay: Replay, name: str, repository: Path, image: str) -> None:
        self._replay = replay
        self.name, self.repository, self.image = name, repository, image

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        asked = (self.name, self.repository, self.image, command, commit, phase, limits)
        answer = self._replay.made(Effect(EffectKind.SANDBOX_RUN, asked))
        assert isinstance(answer, RunResult)
        return answer

    def save_home(self, commit: str, archive: Path) -> None:
        asked = (self.name, self.repository, self.image, commit, archive)
        self._replay.made(Effect(EffectKind.HOME_SAVE, asked))

    def restore_home(self, commit: str, archive: Path) -> None:
        asked = (self.name, self.repository, self.image, commit, archive)
        self._replay.made(Effect(EffectKind.HOME_RESTORE, asked))

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        at: TracebackType | None,
    ) -> None:
        pass


class _Seams:
    """Everything behind the seams of a replay, each call an Effect asked of `replay`."""

    def __init__(self, replay: Replay, run: str, capabilities: Capabilities) -> None:
        self._replay, self._run = replay, run
        self._opened = 0
        parallel = set(capabilities.parallel_tool_calls)
        self.model = RoleModels(
            _Model(replay, None, False),
            {role: _Model(replay, role, role in parallel) for role in RoleName},
        )
        self.decider = _Decider(replay) if capabilities.decider else None

    def git(
        self,
        repository: Path,
        arguments: tuple[str, ...],
        *,
        stdin: bytes | None,
        environment: Mapping[str, str],
    ) -> bytes:
        asked = (repository, tuple(arguments), stdin, tuple(sorted(environment.items())))
        answer = self._replay.made(Effect(EffectKind.GIT, asked))
        assert isinstance(answer, bytes)
        return answer

    def token_hex(self, size: int) -> str:
        answer = self._replay.made(Effect(EffectKind.TOKEN, (size,)))
        assert isinstance(answer, str)
        return answer

    def discover(self, repository: Path, commit: str) -> Commands | Unsupported:
        answer = self._replay.made(Effect(EffectKind.DISCOVER, (repository, commit)))
        assert isinstance(answer, Commands | Unsupported)
        return answer

    def open_sandbox(self, repository: Path, image: str) -> _Sandbox:
        name = sandbox_name(self._run, self._opened)
        self._opened += 1
        return _Sandbox(self._replay, name, repository, image)

    def pipeline(self, work_on_claim: WorkOnClaim | None) -> FixedPipeline:
        return FixedPipeline(
            store=_Profiles(self._replay),
            cache=_Dependencies(self._replay),
            open_sandbox=self.open_sandbox,
            discover=self.discover,
            progress_guard=self.decider,
            work_on_claim=work_on_claim,
        )


class _ClaimElsewhere:
    """Has the work on each Claim done by a CLAIM Effect: a workflow of its own."""

    def __init__(self, replay: Replay, given: RunInput, capabilities: Capabilities) -> None:
        self._replay, self._given, self._capabilities = replay, given, capabilities

    def __call__(
        self,
        ledger: Ledger,
        place: int,
        claim: str,
        kind: ClaimKind | None,
        ready: Ready | None,
        *,
        repository: Path,
        before: str,
        after: str,
        model: Model,
    ) -> Gathered:
        named = None
        if ready is not None:
            sandbox = ready.sandbox
            assert isinstance(sandbox, _Sandbox)
            named = SandboxReady(sandbox.name, sandbox.image, ready.ecosystem)
        given = ClaimInput(
            run=self._given.run,
            pull_request=replace(
                self._given.pull_request, repository=repository, before=before, after=after
            ),
            place=place,
            claim=claim,
            kind=kind,
            ready=named,
            ledger=ledger.snapshot(),
            capabilities=self._capabilities,
            shown=self._given.shown,
            limits=self._given.limits,
        )
        outcome = self._replay.made(Effect(EffectKind.CLAIM, (given,)))
        assert isinstance(outcome, ClaimOutcome)
        ledger.adopt(outcome.ledger)
        if outcome.spent is not None:
            raise outcome.spent
        assert outcome.gathered is not None
        return outcome.gathered
