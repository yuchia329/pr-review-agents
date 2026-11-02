"""A run replayed from its Effects gives the report the run gave.

Each run here goes through the core's entry point with the fixed pipeline, a stand-in model,
a stand-in Sandbox, and the real stores and git. Every seam that `pr_verification.effects`
names is wrapped so that each Effect, with what answered it, is kept in a journal in the order
made. The run is then made again with nothing behind the seams but the journal: no model, no
Sandbox, no stores, no clock, and the repository deleted, so no git either. The second run must
make the same Effects in the same order and give an equal report.
"""

import shutil
import tempfile
import time
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from types import TracebackType
from typing import Self, cast

from conftest import FixtureRepository, commit_snapshots
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_pipeline import (
    BEHAVIOUR,
    REJECTS,
    WITHIN,
    Discovery,
    Sandboxes,
    WritesFor,
    commands,
    writes,
)
from test_pipeline import DECLARES as DECLARES_A_VERDICT

from pr_verification.budget import Budget
from pr_verification.core import PullRequest, run
from pr_verification.decision import Decision, Question, ScriptedDecider
from pr_verification.effects import OnThisMachine, effects_through
from pr_verification.model import ModelRequest
from pr_verification.orchestration import FixedPipeline
from pr_verification.repo_profile import DependencyCache, DirectoryStore, RepoProfile
from pr_verification.report import Report
from pr_verification.sandbox import Command, Limits, Phase, RunResult, SandboxWithHomes


@dataclass
class Journal:
    """The Effects of a run, each with what it asked and what answered it, in the order made.

    While recording, each Effect is made and kept. While replaying, each is answered from the
    next one kept, which must have asked the same; an Effect beyond those kept fails."""

    entries: list[tuple[str, tuple[object, ...], object, BaseException | None]] = field(
        default_factory=list
    )
    replaying: bool = False
    at: int = 0

    def made[Answer](
        self, what: str, asked: tuple[object, ...], make: Callable[[], Answer] | None
    ) -> Answer:
        if not self.replaying:
            assert make is not None
            try:
                answer = make()
            except Exception as raised:
                self.entries.append((what, asked, None, raised))
                raise
            self.entries.append((what, asked, answer, None))
            return answer
        assert self.at < len(self.entries), f"an Effect the run did not make: {what} {asked}"
        kept_what, kept_asked, kept_answer, kept_raised = self.entries[self.at]
        self.at += 1
        assert (what, asked) == (kept_what, kept_asked), f"Effect {self.at} is not the one made"
        if kept_raised is not None:
            raise kept_raised
        return cast(Answer, kept_answer)


class JournaledModel:
    def __init__(self, journal: Journal, model: WritesFor | None) -> None:
        self._journal, self._model = journal, model

    def complete(self, request: ModelRequest) -> str:
        model = self._model
        return self._journal.made(
            "model", (request,), None if model is None else lambda: model.complete(request)
        )


class JournaledDecider:
    def __init__(self, journal: Journal, decider: ScriptedDecider | None) -> None:
        self._journal, self._decider = journal, decider

    def decide(self, state: str, question: Question) -> Decision:
        decider = self._decider
        return self._journal.made(
            "decide",
            (state, question),
            None if decider is None else lambda: decider.decide(state, question),
        )


class JournaledEffects:
    def __init__(self, journal: Journal, effects: OnThisMachine | None) -> None:
        self._journal, self._effects = journal, effects

    def git(
        self,
        repository: Path,
        arguments: tuple[str, ...],
        *,
        stdin: bytes | None,
        environment: Mapping[str, str],
    ) -> bytes:
        effects = self._effects
        asked = (repository, arguments, stdin, tuple(sorted(environment.items())))
        return self._journal.made(
            "git",
            asked,
            None
            if effects is None
            else lambda: effects.git(repository, arguments, stdin=stdin, environment=environment),
        )

    def token_hex(self, size: int) -> str:
        effects = self._effects
        return self._journal.made(
            "token", (size,), None if effects is None else lambda: effects.token_hex(size)
        )


class JournaledProfiles:
    def __init__(self, journal: Journal, store: DirectoryStore | None) -> None:
        self._journal, self._store = journal, store

    def load(self, repository: Path) -> RepoProfile | None:
        store = self._store
        return self._journal.made(
            "load", (repository,), None if store is None else lambda: store.load(repository)
        )

    def save(self, repository: Path, profile: RepoProfile) -> None:
        store = self._store
        self._journal.made(
            "save",
            (repository, profile),
            None if store is None else lambda: store.save(repository, profile),
        )

    def discard(self, repository: Path) -> None:
        store = self._store
        self._journal.made(
            "discard profile",
            (repository,),
            None if store is None else lambda: store.discard(repository),
        )


class JournaledDependencies:
    def __init__(self, journal: Journal, cache: DependencyCache | None) -> None:
        self._journal, self._cache = journal, cache

    def archive(self, repository: Path, image: str, lock_digest: str) -> Path:
        cache = self._cache
        return self._journal.made(
            "archive",
            (repository, image, lock_digest),
            None if cache is None else lambda: cache.archive(repository, image, lock_digest),
        )

    def holds(self, archive: Path) -> bool:
        cache = self._cache
        return self._journal.made(
            "holds", (archive,), None if cache is None else lambda: cache.holds(archive)
        )

    def discard(self, archive: Path) -> None:
        cache = self._cache
        self._journal.made(
            "discard archive", (archive,), None if cache is None else lambda: cache.discard(archive)
        )


class JournaledSandbox:
    """A Sandbox whose opening, runs, saves, restores, and closing are each an Effect."""

    def __init__(
        self,
        journal: Journal,
        opening: Callable[[], AbstractContextManager[SandboxWithHomes]] | None,
        asked: tuple[object, ...],
    ) -> None:
        self._journal, self._opening, self._asked = journal, opening, asked
        self._sandbox: SandboxWithHomes | None = None
        self._entered: AbstractContextManager[SandboxWithHomes] | None = None

    def __enter__(self) -> Self:
        opening = self._opening

        def opened() -> None:
            assert opening is not None
            made = opening()
            self._entered = made
            self._sandbox = made.__enter__()

        self._journal.made("open", self._asked, None if opening is None else opened)
        return self

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        sandbox = self._sandbox
        return self._journal.made(
            "run",
            (command, commit, phase, limits),
            None
            if sandbox is None
            else lambda: sandbox.run(command, commit=commit, phase=phase, limits=limits),
        )

    def save_home(self, commit: str, archive: Path) -> None:
        sandbox = self._sandbox
        self._journal.made(
            "save_home",
            (commit, archive),
            None if sandbox is None else lambda: sandbox.save_home(commit, archive),
        )

    def restore_home(self, commit: str, archive: Path) -> None:
        sandbox = self._sandbox
        self._journal.made(
            "restore_home",
            (commit, archive),
            None if sandbox is None else lambda: sandbox.restore_home(commit, archive),
        )

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        at: TracebackType | None,
    ) -> None:
        entered = self._entered
        self._journal.made(
            "close",
            (),
            None if entered is None else lambda: entered.__exit__(kind, error, at),
        )


class JournaledClock:
    def __init__(self, journal: Journal, clock: Callable[[], float] | None) -> None:
        self._journal, self._clock = journal, clock

    def __call__(self) -> float:
        return self._journal.made("clock", (), self._clock)


@dataclass(frozen=True)
class ClaimScript:
    """How the stand-in model answers for one Claim."""

    response: str
    judged: str | None
    kind: str


claim_scripts = st.builds(
    ClaimScript,
    response=st.sampled_from([writes(behaviour) for behaviour in BEHAVIOUR] + [DECLARES_A_VERDICT]),
    judged=st.sampled_from([None, REJECTS]),
    kind=st.sampled_from(["behavioural", "structural"]),
)


def verify(
    journal: Journal,
    repository: FixtureRepository,
    state: Path,
    scripts: list[ClaimScript],
    budget: Budget,
) -> Report:
    """One run through the core's entry point: recorded when `journal` is recording, with the
    real things behind each seam, and replayed when it is replaying, with nothing behind them."""
    live = not journal.replaying
    responses = {f"Claim {place} holds": script.response for place, script in enumerate(scripts)}
    judged = {
        f"Claim {place} holds": script.judged
        for place, script in enumerate(scripts)
        if script.judged is not None
    }
    sandboxes = Sandboxes(repository)

    def open_sandbox(path: Path, image: str) -> JournaledSandbox:
        return JournaledSandbox(
            journal, (lambda: sandboxes(path, image)) if live else None, (path, image)
        )

    pipeline = FixedPipeline(
        store=JournaledProfiles(journal, DirectoryStore(state / "profiles") if live else None),
        cache=JournaledDependencies(
            journal, DependencyCache(state / "dependencies") if live else None
        ),
        limits=WITHIN,
        open_sandbox=open_sandbox,
        discover=Discovery(commands()),
    )
    pull_request = PullRequest(
        repository.path,
        repository.before,
        repository.after,
        "Tidy the totals",
        "\n".join(responses),
        "",
    )
    model = JournaledModel(journal, WritesFor(responses, judged) if live else None)
    decider = JournaledDecider(
        journal, ScriptedDecider([script.kind for script in scripts]) if live else None
    )
    clock = JournaledClock(journal, time.monotonic if live else None)
    with effects_through(JournaledEffects(journal, OnThisMachine() if live else None)):
        return run(pull_request, model, decider, orchestration=pipeline, budget=budget, clock=clock)


@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    st.lists(claim_scripts, min_size=1, max_size=3),
    st.sampled_from([Budget(), Budget(steps=4), Budget(steps=12)]),
)
def test_run_replayed_from_its_effects_makes_the_same_effects_and_gives_an_equal_report(
    scripts: list[ClaimScript], budget: Budget
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "repository"
        before, after = commit_snapshots(
            path,
            {"deps.lock": "left-pad 1.0\n", "total.txt": "net\n"},
            {"deps.lock": "left-pad 1.0\n", "total.txt": "net and tax\n"},
        )
        repository = FixtureRepository(path=path, before=before, after=after)
        journal = Journal()

        recorded = verify(journal, repository, Path(directory) / "state", scripts, budget)
        shutil.rmtree(path)
        journal.replaying = True
        replayed = verify(journal, repository, Path(directory) / "elsewhere", scripts, budget)

    assert replayed == recorded
    assert journal.at == len(journal.entries), "the replay made fewer Effects than the run"
    assert any(what == "git" for what, *_ in journal.entries)
