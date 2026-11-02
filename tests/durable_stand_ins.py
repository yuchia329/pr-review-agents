"""Stand-ins shared by the tests of durable runs, and a run on one machine to hold them against.

A durable run is given the same stand-ins as `test_pipeline` gives the fixed pipeline: the
model `WritesFor`, the Discovery of `LineEcosystem`, and `LineSandbox`. Here they sit behind a
worker's `Resources`, and each Sandbox is opened by its name, as a worker opens one.
"""

import json
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, nullcontext
from pathlib import Path

from conftest import FixtureRepository, commit_snapshots
from temporalio.testing import ActivityEnvironment
from test_pipeline import BEHAVIOUR, REJECTS, Discovery, Sandboxes, WritesFor, commands, writes
from test_pipeline import DECLARES as DECLARES_A_VERDICT
from test_replay import ClaimScript

from pr_verification.budget import Budget
from pr_verification.core import PullRequest, run
from pr_verification.decision import Decider, ScriptedDecider
from pr_verification.durable.activities import Activities, EffectTask, Resources
from pr_verification.durable.journal import (
    PAYLOAD_BYTES,
    Answer,
    ClaimInput,
    Effect,
    EffectKind,
    NeedsEffect,
    Replay,
    replay_claim,
)
from pr_verification.model import Model
from pr_verification.orchestration import FixedPipeline
from pr_verification.repo_profile import DependencyCache, DirectoryStore
from pr_verification.report import Report
from pr_verification.sandbox import SandboxWithHomes

ALLOWED = ("test_pipeline", "conftest", "durable_stand_ins")
"""The test modules whose classes cross to Temporal: the stand-in Ecosystem among them."""


def still() -> float:
    """A clock that never moves, so no Effect takes any time and seconds are 0 everywhere."""
    return 0.0


class NamedSandboxes:
    """Opens each Sandbox by its name, as a worker does: every step that names one Sandbox
    is given the same `LineSandbox`, made the first time the name is opened. Records each step
    opened, and each name removed."""

    def __init__(self, repository: FixtureRepository) -> None:
        self._made = Sandboxes(repository)
        self.by_name: dict[str, SandboxWithHomes] = {}
        self.steps: list[tuple[str, str]] = []
        self.removed: list[str] = []

    def __call__(
        self, repository: Path, image: str, *, name: str, step: str
    ) -> AbstractContextManager[SandboxWithHomes]:
        self.steps.append((name, step))
        if name not in self.by_name:
            self.by_name[name] = self._made(repository, image)
        return nullcontext(self.by_name[name])

    def remove(self, name: str) -> None:
        self.removed.append(name)


SCRIPTS = [
    ClaimScript(writes("fixed"), None, "behavioural"),
    ClaimScript(writes("same"), REJECTS, "behavioural"),
    ClaimScript(DECLARES_A_VERDICT, None, "structural"),
]
"""Claims of each kind, one confirmed by a Reproduction the judge accepted."""

RESPONSES = [writes(behaviour) for behaviour in BEHAVIOUR] + [DECLARES_A_VERDICT]
"""Every response the stand-in model can give a Claim, for strategies to draw on."""


def a_repository(directory: Path) -> FixtureRepository:
    path = directory / "repository"
    before, after = commit_snapshots(
        path,
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net\n"},
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net and tax\n"},
    )
    return FixtureRepository(path=path, before=before, after=after)


def claims_of(scripts: Sequence[ClaimScript]) -> list[str]:
    return [f"Claim {place} holds" for place in range(len(scripts))]


def a_model(scripts: Sequence[ClaimScript]) -> WritesFor:
    claims = claims_of(scripts)
    return WritesFor(
        {claim: script.response for claim, script in zip(claims, scripts, strict=True)},
        {
            claim: script.judged
            for claim, script in zip(claims, scripts, strict=True)
            if script.judged is not None
        },
    )


def a_decider(scripts: Sequence[ClaimScript]) -> ScriptedDecider:
    return ScriptedDecider([script.kind for script in scripts])


def a_pull_request(repository: FixtureRepository, scripts: Sequence[ClaimScript]) -> PullRequest:
    return PullRequest(
        repository.path,
        repository.before,
        repository.after,
        "Tidy the totals",
        "\n".join(claims_of(scripts)),
        "",
    )


def resources(
    state: Path,
    model: Model,
    decider: Decider | None,
    sandboxes: NamedSandboxes,
    *,
    heartbeat_seconds: float = 10.0,
) -> Resources:
    """A worker's resources: the stand-ins, with the real stores under `state`."""
    return Resources(
        model=model,
        decider=decider,
        store=DirectoryStore(state / "profiles"),
        cache=DependencyCache(state / "dependencies"),
        discover=Discovery(commands()),
        open_sandbox=sandboxes,
        remove_sandbox=sandboxes.remove,
        clock=still,
        heartbeat_seconds=heartbeat_seconds,
    )


def on_one_machine(
    state: Path,
    repository: FixtureRepository,
    scripts: Sequence[ClaimScript],
    *,
    budget: Budget | None = None,
    with_decider: bool = True,
) -> Report:
    """The report of the core's entry point, run here with the same stand-ins and the fixed
    pipeline as a durable run has it: the defaults, but for its seams and the decider as its
    progress guard."""
    decider = a_decider(scripts) if with_decider else None
    pipeline = FixedPipeline(
        store=DirectoryStore(state / "profiles"),
        cache=DependencyCache(state / "dependencies"),
        open_sandbox=Sandboxes(repository),
        discover=Discovery(commands()),
        progress_guard=decider,
    )
    return run(
        a_pull_request(repository, scripts),
        a_model(scripts),
        decider,
        orchestration=pipeline,
        budget=budget,
        clock=still,
    )


def made_by(activities: Activities) -> Callable[[int, Effect], Answer]:
    """Makes each Effect as the activity `make_effect` makes it, outside Temporal."""

    def make(place: int, effect: Effect) -> Answer:
        task = EffectTask(place=place, effect=effect, payload_bytes=PAYLOAD_BYTES)
        return ActivityEnvironment().run(activities.make_effect, task)

    return make


def driven[Given, Result](
    replay: Callable[[Given, Replay], Result],
    given: Given,
    make: Callable[[int, Effect], Answer],
    journal: list[tuple[Effect, Answer]] | None = None,
) -> Result:
    """What `replay` gives once its Journal holds every answer it needs: each Effect it needs
    is made with `make`, and each CLAIM Effect by driving `replay_claim` in the same way. The
    Journal is kept in `journal` when given."""
    journal = [] if journal is None else journal
    while True:
        try:
            return replay(given, Replay(journal))
        except NeedsEffect as needed:
            if needed.effect.kind is EffectKind.CLAIM:
                (asked,) = needed.effect.asked
                assert isinstance(asked, ClaimInput)
                answer = Answer(driven(replay_claim, asked, make))
            else:
                answer = make(needed.place, needed.effect)
            journal.append((needed.effect, answer))


def tasks_of(model: WritesFor) -> Iterator[str]:
    """The Claim of the task of each request the model was asked, in order: `-` for the Claim
    extractor's."""
    for request in model.requests:
        task = json.loads(request.data).get("task")
        yield "-" if task is None else str(task["claim"])
