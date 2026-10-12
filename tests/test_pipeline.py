"""The fixed pipeline: what it gathers for each Claim, and the report that comes of it.

The tests down to `pricing` use a stand-in Sandbox, a stand-in Ecosystem, and a stand-in model.
The ones from there on verify a Python pull request in a real Sandbox.
"""

import json
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from types import TracebackType
from typing import Self

import pytest
from conftest import Clock, FixtureRepository, asks, commit_snapshots, finishes

from pr_verification import verdict
from pr_verification.agent import (
    ALTERNATION_LENGTH,
    PROGRESS,
    PROGRESS_INTERVAL,
    REPEAT_LENGTH,
    TOLD,
    LoopKind,
)
from pr_verification.budget import BUDGET_SPENT, SHARE_SPENT, Budget, Ledger
from pr_verification.cli import main
from pr_verification.core import PullRequest, run
from pr_verification.decision import NO, YES, ScriptedDecider
from pr_verification.ecosystem import (
    Commands,
    Ecosystem,
    Placement,
    Results,
    Unreadable,
    Unsupported,
    find_commands,
)
from pr_verification.judge import ROUND_CAP, ROUND_CAP_REACHED, write_judged_reproduction
from pr_verification.model import Model, ModelFailed, ModelRequest
from pr_verification.orchestration import (
    COULD_NOT_PREPARE,
    LIMITS,
    NO_REPRODUCTION_ECOSYSTEM,
    FixedPipeline,
    Gathered,
    fixed_pipeline,
)
from pr_verification.python_ecosystem import REPRODUCTION_PATH
from pr_verification.repo_profile import (
    CommandFailed,
    DependencyCache,
    DirectoryStore,
    RepoProfile,
)
from pr_verification.report import (
    AcceptedReproduction,
    BudgetLimit,
    BudgetSpend,
    ClaimKind,
    ClassifiedClaim,
    GateDecision,
    PullRequestSpend,
    Report,
    ReproductionEvidence,
    SearchEvidence,
    Verdict,
)
from pr_verification.reproduction import (
    ATTEMPT_CAP,
    ATTEMPT_CAP_REACHED,
    COULD_NOT_FINISH,
    ENDINGS,
    WRITER_STEP_CAP,
    AttemptResult,
)
from pr_verification.sandbox import (
    Command,
    ContainerSandbox,
    LimitHit,
    Limits,
    Phase,
    RunResult,
    SandboxFailed,
)
from pr_verification.structural import (
    VERIFIER_COULD_NOT_FINISH,
    VERIFIER_STEP_CAP,
    VERIFIER_STEP_CAP_REACHED,
)
from pr_verification.tracing import SpanKind, record

IMAGE = "lines:1"
FETCH: Command = ("fetch", "--locked")
TEST: Command = ("run-tests",)
WITHIN = Limits(seconds=30.0, memory_bytes=256 * 1024 * 1024, processors=1.0)


def ended(output: str, exit_status: int = 0) -> RunResult:
    return RunResult(output=output, exit_status=exit_status, limit_hit=None)


# What each Reproduction the stand-in model writes prints on the code before and the code after.
BEHAVIOUR: Mapping[str, tuple[RunResult, RunResult]] = {
    "fixed": (ended("failed test_total", 1), ended("ok test_total")),
    "broken": (ended("ok test_total"), ended("failed test_total", 1)),
    "same": (ended("ok test_total OUTPUT-OF-SAME"), ended("ok test_total OUTPUT-OF-SAME")),
    "cannot import": (ended("error test_total", 2), ended("error test_total", 2)),
}


class LineEcosystem:
    """A stand-in with nothing of Python in it: a run prints one line for each test."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        raise AssertionError("discovery is asked for through `discover`")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        if run.limit_hit is not None:
            return Unreadable("The run was stopped.")
        lines = [line.split() for line in run.output.splitlines()]
        return Results(
            passed=tuple(name for word, name, *_ in lines if word == "ok"),
            failed=tuple(name for word, name, *_ in lines if word == "failed"),
            could_not_run=tuple(name for word, name, *_ in lines if word == "error"),
        )

    def reproduction_form(self) -> str:
        return "A Reproduction is one word."

    def place(self, reproduction: str) -> Placement:
        return Placement("reproduction.txt", ("write", reproduction), ("run-reproduction",))


class ReadsResultsOnly:
    """A stand-in Ecosystem that cannot say how a Reproduction is written, placed, or run."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        raise AssertionError("discovery is asked for through `discover`")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        return Unreadable("A stand-in reads nothing.")


def commands(ecosystem: Ecosystem | None = None) -> Commands:
    return Commands(
        ecosystem=ecosystem or LineEcosystem(),
        image=IMAGE,
        install=(FETCH,),
        test=TEST,
        lock_files=("deps.lock",),
    )


class Discovery:
    """A stand-in for discovery that gives one answer and records what it was asked."""

    def __init__(self, answer: Commands | Unsupported) -> None:
        self.answer = answer
        self.asked: list[tuple[Path, str]] = []

    def __call__(self, repository: Path, commit: str) -> Commands | Unsupported:
        self.asked.append((repository, commit))
        return self.answer


Prepared = Mapping[tuple[Command, str], RunResult | SandboxFailed]
"""How a command ends against the code `before` or `after` the pull request, where that is not
with exit status 0."""


class LineSandbox:
    """A stand-in Sandbox that keeps the Reproduction written for each commit and runs it as
    `BEHAVIOUR` says it behaves. It records each run, and whether it has been closed."""

    def __init__(self, repository: Path, image: str, before: str, prepared: Prepared) -> None:
        self.repository, self.image = repository, image
        self.runs: list[tuple[Command, str, Phase, Limits]] = []
        self.closed = False
        self._before, self._prepared = before, prepared
        self._written: dict[str, str] = {}

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        assert not self.closed, "a Sandbox was asked for a run after it was closed"
        self.runs.append((command, commit, phase, limits))
        answer = self._prepared.get((command, "before" if commit == self._before else "after"))
        if isinstance(answer, SandboxFailed):
            raise answer
        if answer is not None:
            return answer
        if command[0] == "write":
            self._written[commit] = command[1]
        if command != ("run-reproduction",):
            return ended("")
        if self._written[commit] == "no runtime":
            raise SandboxFailed("no container runtime")
        before, after = BEHAVIOUR[self._written[commit]]
        return before if commit == self._before else after

    def save_home(self, commit: str, archive: Path) -> None:
        archive.write_bytes(b"a saved home directory")

    def restore_home(self, commit: str, archive: Path) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        kind: type[BaseException] | None,
        error: BaseException | None,
        at: TracebackType | None,
    ) -> None:
        self.closed = True


class Sandboxes:
    """A stand-in for opening Sandboxes: each call gives a new one, and all are kept."""

    def __init__(self, repository: FixtureRepository, prepared: Prepared | None = None) -> None:
        self._before, self._prepared = repository.before, prepared or {}
        self.opened: list[LineSandbox] = []

    def __call__(self, repository: Path, image: str) -> LineSandbox:
        self.opened.append(LineSandbox(repository, image, self._before, self._prepared))
        return self.opened[-1]


def writes(reproduction: str) -> str:
    """A response in which the Reproduction writer finishes with a Reproduction."""
    return finishes(reproduction=reproduction)


DECLARES = finishes(verdict="confirmed", reason="The fix is plainly right, so no test is needed.")
"""A response in which the model declares a Verdict and gives no Reproduction."""

ACCEPTS = finishes(accepted=True)
"""A response in which the Reproduction judge accepts a Reproduction."""

WHY = "The test asserts nothing about what the Claim says."
REJECTS = finishes(accepted=False, reason=WHY)
"""A response in which the Reproduction judge rejects a Reproduction."""


class WritesFor:
    """A stand-in model that lists the Claims prepared when asked by the Claim extractor, and
    answers a role with the response prepared for the Claim in its task, or raises it. The
    Reproduction judge, whose task holds a Reproduction, is answered with what was prepared in
    `judged` for the Claim, and accepts when nothing was."""

    def __init__(
        self,
        responses: Mapping[str, str | Exception],
        judged: Mapping[str, str | Exception] | None = None,
    ) -> None:
        self._responses, self._judged = responses, judged or {}
        self.claims = tuple(responses)
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        asked = json.loads(request.data)
        if "task" not in asked:
            return json.dumps({"claims": self.claims})
        task = asked["task"]
        response = self._responses[task["claim"]]
        if "reproduction" in task:
            response = self._judged.get(task["claim"], ACCEPTS)
        if isinstance(response, Exception):
            raise response
        return response

    def tasks(self, claim: str) -> list[dict[str, object]]:
        """The task of each request made for the work on one Claim, in order."""
        tasks = [json.loads(request.data).get("task") for request in self.requests]
        return [task for task in tasks if task is not None and task["claim"] == claim]


NOT_FOUND = "Nothing here says how the code is tested."


class Fault(Exception):
    """An error that says nothing about the pull request: a fault in the system."""


@pytest.fixture
def shop(tmp_path: Path) -> FixtureRepository:
    """A repository with nothing of any Ecosystem in it, and a pull request that changes it."""
    path = tmp_path / "repository"
    before, after = commit_snapshots(
        path,
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net\n"},
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net and tax\n"},
    )
    return FixtureRepository(path=path, before=before, after=after)


def a_pipeline(
    state: Path,
    sandboxes: Sandboxes,
    discover: Discovery | None = None,
    round_cap: int = ROUND_CAP,
) -> FixedPipeline:
    return FixedPipeline(
        store=DirectoryStore(state / "profiles"),
        cache=DependencyCache(state / "dependencies"),
        limits=WITHIN,
        open_sandbox=sandboxes,
        discover=discover or Discovery(commands()),
        round_cap=round_cap,
    )


def gather(
    pipeline: FixedPipeline, repository: FixtureRepository, model: WritesFor
) -> list[Gathered]:
    """What the pipeline gathers for the Claims the model has a response for, in their order."""
    return list(
        pipeline.gather(
            model.claims,
            repository=repository.path,
            before=repository.before,
            after=repository.after,
            model=model,
        )
    )


def cap_reached(last: AttemptResult) -> str:
    return ATTEMPT_CAP_REACHED.format(attempts=ATTEMPT_CAP, ending=ENDINGS[last])


# What the pipeline gathers


def test_each_claim_is_given_the_evidence_of_its_own_reproduction_in_the_order_of_the_claims(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model = WritesFor({"Adds tax": writes("fixed"), "Keeps the net price": writes("broken")})

    fixed, broken = gather(a_pipeline(tmp_path / "state", Sandboxes(shop)), shop, model)

    for gathered, text in ((fixed, "fixed"), (broken, "broken")):
        (evidence,) = gathered.evidence
        assert isinstance(evidence, ReproductionEvidence)
        assert (evidence.reproduction, evidence.path) == (text, "reproduction.txt")
        assert (evidence.before, evidence.after) == BEHAVIOUR[text]
        assert gathered.inconclusive is None
    (evidence,) = fixed.evidence
    assert isinstance(evidence, ReproductionEvidence)
    assert evidence.before_results == Results(passed=(), failed=("test_total",), could_not_run=())
    assert evidence.after_results == Results(passed=("test_total",), failed=(), could_not_run=())


def test_both_commits_are_prepared_in_one_sandbox_before_any_reproduction_is_written(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes, discovery = Sandboxes(shop), Discovery(commands())
    pipeline = a_pipeline(tmp_path / "state", sandboxes, discovery)

    gather(pipeline, shop, WritesFor({"Adds tax": writes("fixed"), "Rounds": writes("broken")}))

    (sandbox,) = sandboxes.opened
    assert (sandbox.repository, sandbox.image, sandbox.closed) == (shop.path, IMAGE, True)
    assert set(discovery.asked) == {(shop.path, shop.before)}
    assert sandbox.runs == [
        (FETCH, shop.before, Phase.INSTALL, WITHIN),
        (TEST, shop.before, Phase.TEST, WITHIN),
        (FETCH, shop.after, Phase.INSTALL, WITHIN),
        *(
            (command, commit, Phase.TEST, WITHIN)
            for text in ("fixed", "broken")
            for command in (("write", text), ("run-reproduction",))
            for commit in (shop.before, shop.after)
        ),
    ]


def test_repo_profile_is_kept_in_the_pipelines_store_and_used_on_the_next_run(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    first, second = Sandboxes(shop), Sandboxes(shop)
    gather(a_pipeline(tmp_path / "state", first), shop, WritesFor({"Adds tax": writes("fixed")}))

    (gathered,) = gather(
        a_pipeline(tmp_path / "state", second), shop, WritesFor({"Adds tax": writes("fixed")})
    )

    kept = DirectoryStore(tmp_path / "state" / "profiles").load(shop.path)
    assert kept is not None and (kept.image, kept.install, kept.test) == (IMAGE, (FETCH,), TEST)
    # A profile that still holds is not proven again: the test command is not run.
    (sandbox,) = second.opened
    assert [run[0] for run in sandbox.runs[:2]] == [FETCH, FETCH]
    assert len(gathered.evidence) == 1


def test_with_no_claims_nothing_is_discovered_and_no_sandbox_is_opened(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes, discovery = Sandboxes(shop), Discovery(commands())

    gathered = gather(a_pipeline(tmp_path / "state", sandboxes, discovery), shop, WritesFor({}))

    assert (gathered, discovery.asked, sandboxes.opened) == ([], [], [])


@pytest.mark.parametrize(
    ("found", "reason"),
    [
        (Unsupported(NOT_FOUND), NOT_FOUND),
        (commands(ReadsResultsOnly()), NO_REPRODUCTION_ECOSYSTEM),
    ],
    ids=["no Ecosystem", "an Ecosystem that cannot run a Reproduction"],
)
def test_repository_no_reproduction_can_be_run_in_leaves_every_claim_without_evidence(
    shop: FixtureRepository, tmp_path: Path, found: Commands | Unsupported, reason: str
) -> None:
    sandboxes = Sandboxes(shop)
    model = WritesFor({"Adds tax": writes("fixed"), "Rounds": writes("broken")})

    gathered = gather(a_pipeline(tmp_path / "state", sandboxes, Discovery(found)), shop, model)

    assert gathered == [Gathered((), reason), Gathered((), reason)]
    assert (sandboxes.opened, model.requests) == ([], [])


FAILED = ended("OUTPUT-OF-THE-COMMAND", 1)
TIMED_OUT = RunResult("OUTPUT-OF-THE-COMMAND", None, LimitHit.TIME)
OUT_OF_MEMORY = RunResult("OUTPUT-OF-THE-COMMAND", None, LimitHit.MEMORY)
NO_RUNTIME = SandboxFailed("no container runtime")


@pytest.mark.parametrize(
    ("failing", "how", "failure"),
    [
        ((FETCH, "before"), FAILED, "the command fetch --locked ended with exit status 1"),
        ((FETCH, "after"), FAILED, "the command fetch --locked ended with exit status 1"),
        (
            (FETCH, "after"),
            TIMED_OUT,
            "the command fetch --locked ended with stopped at the time limit",
        ),
        (
            (TEST, "before"),
            OUT_OF_MEMORY,
            "the command run-tests ended with stopped at the memory limit",
        ),
        ((FETCH, "before"), NO_RUNTIME, "no container runtime"),
        ((FETCH, "after"), NO_RUNTIME, "no container runtime"),
    ],
)
def test_commit_that_cannot_be_prepared_leaves_every_claim_without_evidence_and_writes_nothing(
    shop: FixtureRepository,
    tmp_path: Path,
    failing: tuple[Command, str],
    how: RunResult | SandboxFailed,
    failure: str,
) -> None:
    sandboxes = Sandboxes(shop, {failing: how})
    model = WritesFor({"Adds tax": writes("fixed"), "Rounds": writes("broken")})

    gathered = gather(a_pipeline(tmp_path / "state", sandboxes), shop, model)

    reason = COULD_NOT_PREPARE.format(commit=failing[1], failure=failure)
    assert gathered == [Gathered((), reason), Gathered((), reason)]
    assert model.requests == []
    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)
    assert all(run[0][0] != "write" for sandbox in sandboxes.opened for run in sandbox.runs)


@pytest.mark.parametrize(
    ("response", "failure"),
    [
        (ModelFailed("the model could not be reached"), "the model could not be reached"),
        (writes("no runtime"), "no container runtime"),
    ],
    ids=["the model fails", "the Sandbox fails"],
)
def test_failure_while_working_on_one_claim_does_not_stop_work_on_the_others(
    shop: FixtureRepository, tmp_path: Path, response: str | Exception, failure: str
) -> None:
    model = WritesFor(
        {"Adds tax": writes("fixed"), "Rounds": response, "Keeps the net price": writes("broken")}
    )

    first, unfinished, last = gather(a_pipeline(tmp_path / "state", Sandboxes(shop)), shop, model)

    assert unfinished == Gathered((), COULD_NOT_FINISH.format(failure=failure))
    assert [len(gathered.evidence) for gathered in (first, last)] == [1, 1]
    assert (first.inconclusive, last.inconclusive) == (None, None)


def test_nothing_of_one_claim_reaches_the_role_working_on_another(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model = WritesFor({"Adds tax": writes("same"), "Rounds": writes("fixed")})

    gather(a_pipeline(tmp_path / "state", Sandboxes(shop)), shop, model)

    assert len(model.tasks("Adds tax")) == ATTEMPT_CAP
    assert model.tasks("Rounds") == [
        {"claim": "Rounds", "attempts": []},
        {"claim": "Rounds", "reproduction": "fixed"},
    ]
    for request in model.requests:
        if '"Rounds"' in request.data:
            assert "Adds tax" not in request.data and "OUTPUT-OF-SAME" not in request.data


def test_fault_is_raised_and_every_sandbox_opened_is_closed_first(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes = Sandboxes(shop)
    model = WritesFor({"Adds tax": Fault("the disc is full"), "Rounds": writes("fixed")})

    with pytest.raises(Fault):
        gather(a_pipeline(tmp_path / "state", sandboxes), shop, model)

    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)
    assert model.tasks("Rounds") == []


def test_place_to_keep_things_that_lies_inside_the_repository_is_refused(
    shop: FixtureRepository,
) -> None:
    pipeline = a_pipeline(shop.path / "kept", Sandboxes(shop))

    with pytest.raises(ValueError):
        gather(pipeline, shop, WritesFor({"Adds tax": writes("fixed")}))


def test_fixed_pipeline_keeps_the_repo_profile_and_dependencies_under_its_state_directory(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    state = tmp_path / "not" / "there" / "yet"
    profile = RepoProfile(IMAGE, (FETCH,), TEST, "lines", (".",), ("deps.lock",), "a digest")

    pipeline = fixed_pipeline(state)

    pipeline.store.save(shop.path, profile)
    assert DirectoryStore(state / "profiles").load(shop.path) == profile
    kept = DependencyCache(state / "dependencies").archive(shop.path, IMAGE, "a digest")
    assert pipeline.cache.archive(shop.path, IMAGE, "a digest") == kept
    assert pipeline.limits == LIMITS
    assert pipeline.open_sandbox is ContainerSandbox
    assert pipeline.discover is find_commands


def test_fixed_pipeline_has_the_progress_guard_it_is_given_and_none_unless_given(
    tmp_path: Path,
) -> None:
    guard = ScriptedDecider([])

    assert fixed_pipeline(tmp_path / "state", progress_guard=guard).progress_guard is guard
    assert fixed_pipeline(tmp_path / "state").progress_guard is None


# A commit of the target branch, prepared with no pull request to verify


def test_target_commit_is_prepared_and_its_repo_profile_and_dependencies_are_kept(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes = Sandboxes(shop)
    pipeline = a_pipeline(tmp_path / "state", sandboxes)

    profile = pipeline.prepare_target_commit(shop.path, shop.before)

    assert isinstance(profile, RepoProfile)
    assert profile == DirectoryStore(tmp_path / "state" / "profiles").load(shop.path)
    (sandbox,) = sandboxes.opened
    assert (sandbox.repository, sandbox.image, sandbox.closed) == (shop.path, IMAGE, True)
    assert sandbox.runs == [
        (FETCH, shop.before, Phase.INSTALL, WITHIN),
        (TEST, shop.before, Phase.TEST, WITHIN),
    ]
    assert pipeline.cache.archive(shop.path, IMAGE, profile.lock_digest).exists()


def test_target_commit_that_no_ecosystem_can_run_is_answered_for_with_no_sandbox_opened(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes = Sandboxes(shop)
    pipeline = a_pipeline(tmp_path / "state", sandboxes, Discovery(Unsupported(NOT_FOUND)))

    assert pipeline.prepare_target_commit(shop.path, shop.before) == Unsupported(NOT_FOUND)
    assert sandboxes.opened == []


def test_target_commit_whose_install_fails_is_answered_for_with_the_command_and_nothing_is_kept(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes = Sandboxes(shop, {(FETCH, "before"): FAILED})
    pipeline = a_pipeline(tmp_path / "state", sandboxes)

    answer = pipeline.prepare_target_commit(shop.path, shop.before)

    assert answer == CommandFailed(FETCH, FAILED)
    assert pipeline.store.load(shop.path) is None
    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)


def test_sandbox_is_closed_before_a_failure_to_prepare_a_target_commit_goes_on(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes = Sandboxes(shop, {(FETCH, "before"): NO_RUNTIME})

    with pytest.raises(SandboxFailed, match="no container runtime"):
        a_pipeline(tmp_path / "state", sandboxes).prepare_target_commit(shop.path, shop.before)

    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)


def test_trace_of_preparing_a_target_commit_holds_its_sandbox_runs_each_with_no_parent(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(shop))

    with record() as recording:
        pipeline.prepare_target_commit(shop.path, shop.before)

    spans = recording.trace().spans
    assert [(span.kind, span.name, span.parent_id) for span in spans] == [
        (SpanKind.SANDBOX_RUN, "sandbox", None)
    ] * 2
    assert [span.attributes["pr_verification.phase"] for span in spans] == ["install", "test"]


# The judge, and Claims routed by their kind


def test_claim_is_given_a_reproduction_only_when_the_judge_accepted_it(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model = WritesFor(
        {"Adds tax": writes("fixed"), "Keeps the net price": writes("fixed")},
        judged={"Keeps the net price": REJECTS},
    )

    accepted, rejected = gather(a_pipeline(tmp_path / "state", Sandboxes(shop)), shop, model)

    (evidence,) = accepted.evidence
    assert isinstance(evidence, AcceptedReproduction) and accepted.inconclusive is None
    assert rejected == Gathered((), ROUND_CAP_REACHED.format(rounds=ROUND_CAP, reason=WHY))


TITLE, DESCRIPTION, LINKED_ISSUE = (
    "TITLE-OF-THE-PULL-REQUEST",
    "ITS-DESCRIPTION",
    "ITS-LINKED-ISSUE",
)


def report_of(
    repository: FixtureRepository, model: Model, pipeline: FixedPipeline, *kinds: str
) -> Report:
    """The report from the core's entry point, where the Claim classifier labels the Claims
    with these kinds, in order."""
    pull_request = PullRequest(
        repository.path, repository.before, repository.after, TITLE, DESCRIPTION, LINKED_ISSUE
    )
    return run(pull_request, model, ScriptedDecider(kinds), orchestration=pipeline)


@pytest.mark.parametrize(
    ("text", "round_cap"),
    [("fixed", ROUND_CAP), ("broken", 1)],
    ids=["fails then passes", "passes then fails"],
)
def test_reproduction_the_judge_rejects_leaves_its_claim_inconclusive_whatever_its_runs_showed(
    shop: FixtureRepository, tmp_path: Path, text: str, round_cap: int
) -> None:
    model = WritesFor({"Adds tax": writes(text)}, judged={"Adds tax": REJECTS})
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(shop), round_cap=round_cap)

    report = report_of(shop, model, pipeline, "behavioural")

    (claim,) = report.claims
    assert (claim.verdict, claim.evidence) == (Verdict.INCONCLUSIVE, ())
    assert claim.reason == ROUND_CAP_REACHED.format(rounds=round_cap, reason=WHY)
    assert report.gate.decision is GateDecision.PASS
    judged = [task for task in model.tasks("Adds tax") if "reproduction" in task]
    assert judged == [{"claim": "Adds tax", "reproduction": text}] * round_cap


def test_judge_is_shown_the_claim_and_the_reproduction_and_none_of_the_pull_request(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model = WritesFor({"Adds tax": writes("fixed")})

    report_of(shop, model, a_pipeline(tmp_path / "state", Sandboxes(shop)), "behavioural")

    judged = [request for request in model.requests if '"reproduction"' in request.data]
    assert [json.loads(request.data) for request in judged] == [
        {"task": {"claim": "Adds tax", "reproduction": "fixed"}, "steps": []}
    ]
    for untrusted in (TITLE, DESCRIPTION, LINKED_ISSUE, "net and tax", "test_total", "Adds tax"):
        assert untrusted not in judged[0].instructions


RENAMES = "Renames UserManager to AccountManager"
BEFORE_THE_RENAME = "class UserManager:\n    pass\n\n\nmanager = UserManager()\n"
CARRIED_OUT = BEFORE_THE_RENAME.replace("UserManager", "AccountManager")
LEFT_BEHIND = "class AccountManager:\n    pass\n\n\nmanager = UserManager()\n"
NAMES_BOTH = finishes(
    texts=[
        {"text": "UserManager", "change": "removed"},
        {"text": "AccountManager", "change": "added", "path": "accounts.txt"},
    ]
)
"""A response in which the Structural verifier names what `RENAMES` says is removed and added."""


def renaming(path: Path, after: str = CARRIED_OUT) -> FixtureRepository:
    """A repository with nothing of any Ecosystem in it, and a pull request that renames a class
    and leaves the file holding it as given."""
    lock = {"deps.lock": "left-pad 1.0\n"}
    commits = commit_snapshots(
        path, lock | {"accounts.txt": BEFORE_THE_RENAME}, lock | {"accounts.txt": after}
    )
    return FixtureRepository(path, *commits)


@pytest.mark.parametrize(
    ("after", "still_there", "expected", "reason", "gate"),
    [
        (CARRIED_OUT, [], Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED, GateDecision.PASS),
        (
            LEFT_BEHIND,
            ["accounts.txt:5:manager = UserManager()"],
            Verdict.REFUTED,
            verdict.STRUCTURAL_REFUTED,
            GateDecision.BLOCK,
        ),
    ],
    ids=["a rename fully carried out", "a rename that leaves the old name"],
)
def test_structural_claim_is_verified_by_reading_with_no_sandbox_and_decided_by_what_code_found(
    tmp_path: Path,
    after: str,
    still_there: list[str],
    expected: Verdict,
    reason: str,
    gate: GateDecision,
) -> None:
    accounts = renaming(tmp_path / "repository", after)
    sandboxes, discovery = Sandboxes(accounts), Discovery(commands())
    model = WritesFor({RENAMES: NAMES_BOTH})

    report = report_of(
        accounts, model, a_pipeline(tmp_path / "state", sandboxes, discovery), "structural"
    )

    (claim,) = report.claims
    assert isinstance(claim, ClassifiedClaim) and claim.kind is ClaimKind.STRUCTURAL
    assert (claim.verdict, claim.reason, report.gate.decision) == (expected, reason, gate)
    assert (sandboxes.opened, discovery.asked) == ([], [])
    assert model.tasks(RENAMES) == [{"claim": RENAMES}]
    old_name, new_name = json.loads(report.to_json())["claims"][0]["evidence"]
    assert old_name == {
        "source": "read_code",
        "observation": f"lines holding the text: 2 before, {len(still_there)} after",
        "text": "UserManager",
        "change": "removed",
        "path": None,
        "before_lines": [
            "accounts.txt:1:class UserManager:",
            "accounts.txt:5:manager = UserManager()",
        ],
        "after_lines": still_there,
    }
    assert (new_name["path"], new_name["before_lines"]) == ("accounts.txt", [])
    assert new_name["after_lines"][0] == "accounts.txt:1:class AccountManager:"


def test_each_claim_is_worked_on_in_its_turn_as_its_kind_calls_for_and_only_a_reproduction_is_run(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    sandboxes = Sandboxes(accounts)
    model = WritesFor({RENAMES: NAMES_BOTH, "Adds tax": writes("fixed"), "Drops": NAMES_BOTH})

    report = report_of(
        accounts,
        model,
        a_pipeline(tmp_path / "state", sandboxes),
        "structural",
        "behavioural",
        "structural",
    )

    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
        (Verdict.CONFIRMED, verdict.CONFIRMED),
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
    ]
    renamed, taxed, dropped = (claim.evidence for claim in report.claims)
    assert all(isinstance(evidence, SearchEvidence) for evidence in (*renamed, *dropped))
    assert [len(renamed), len(taxed), len(dropped)] == [2, 1, 2]
    assert isinstance(taxed[0], AcceptedReproduction)
    asked = [json.loads(request.data)["task"] for request in model.requests[1:]]
    assert asked == [
        {"claim": RENAMES},
        {"claim": "Adds tax", "attempts": []},
        {"claim": "Adds tax", "reproduction": "fixed"},
        {"claim": "Drops"},
    ]
    (sandbox,) = sandboxes.opened
    # The first three runs prepare the two commits.
    assert [command for command, *_ in sandbox.runs[3:]] == [
        ("write", "fixed"),
        ("write", "fixed"),
        ("run-reproduction",),
        ("run-reproduction",),
    ]
    assert isinstance(report.budget_spend, PullRequestSpend)
    assert [spend.steps for spend in report.budget_spend.claims] == [1, 2, 1]


@pytest.mark.parametrize(
    ("found", "prepared", "reason"),
    [
        (Unsupported(NOT_FOUND), {}, NOT_FOUND),
        (
            commands(),
            {(FETCH, "before"): SandboxFailed("no container runtime")},
            COULD_NOT_PREPARE.format(commit="before", failure="no container runtime"),
        ),
    ],
    ids=["no Ecosystem", "a commit that cannot be prepared"],
)
def test_structural_claim_is_still_verified_when_no_reproduction_can_be_run(
    tmp_path: Path, found: Commands | Unsupported, prepared: Prepared, reason: str
) -> None:
    accounts = renaming(tmp_path / "repository")
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(accounts, prepared), Discovery(found))
    model = WritesFor({"Adds tax": writes("fixed"), RENAMES: NAMES_BOTH})

    report = report_of(accounts, model, pipeline, "behavioural", "structural")

    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.INCONCLUSIVE, reason),
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
    ]
    assert model.tasks("Adds tax") == []


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        (
            ModelFailed("the model could not be reached"),
            VERIFIER_COULD_NOT_FINISH.format(failure="the model could not be reached"),
        ),
        (
            asks("after_list_files"),
            VERIFIER_STEP_CAP_REACHED.format(steps=VERIFIER_STEP_CAP),
        ),
    ],
    ids=["the model fails", "the verifier never finishes"],
)
def test_structural_claim_the_verifier_did_not_finish_is_inconclusive_and_the_next_is_verified(
    tmp_path: Path, response: str | Exception, reason: str
) -> None:
    accounts = renaming(tmp_path / "repository")
    model = WritesFor({"Moves the manager": response, RENAMES: NAMES_BOTH})

    report = report_of(
        accounts,
        model,
        a_pipeline(tmp_path / "state", Sandboxes(accounts)),
        "structural",
        "structural",
    )

    assert [(claim.verdict, claim.reason, len(claim.evidence)) for claim in report.claims] == [
        (Verdict.INCONCLUSIVE, reason, 0),
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED, 2),
    ]


class NeverFinishes(WritesFor):
    """A stand-in model whose roles ask for a different file at every step and never finish, so
    that none is given the response prepared for its Claim."""

    def complete(self, request: ModelRequest) -> str:
        if "task" not in json.loads(request.data):
            return super().complete(request)
        self.requests.append(request)
        return asks("after_read_file", path=f"notes/{len(self.requests)}.txt")


def test_progress_guard_of_the_pipeline_watches_the_structural_verifier_and_the_writer(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    guard = ScriptedDecider([YES] * 99)
    pipeline = replace(a_pipeline(tmp_path / "state", Sandboxes(accounts)), progress_guard=guard)
    model = NeverFinishes({RENAMES: NAMES_BOTH, "Adds tax": writes("fixed")})

    report_of(accounts, model, pipeline, "structural", "behavioural")

    watched = [json.loads(state)["task"] for state, _ in guard.asked]
    # The verifier's task holds its Claim alone, and the writer's holds its attempts as well.
    assert watched[0] == {"claim": RENAMES}
    assert watched[-1]["claim"] == "Adds tax" and "attempts" in watched[-1]


@pytest.mark.parametrize(
    "kinds",
    [(), (ClaimKind.STRUCTURAL,), (ClaimKind.STRUCTURAL, None, ClaimKind.BEHAVIOURAL)],
    ids=["none", "too few", "too many"],
)
def test_kinds_that_do_not_hold_one_for_each_claim_are_refused_before_anything_else_is_done(
    tmp_path: Path, kinds: tuple[ClaimKind | None, ...]
) -> None:
    accounts = renaming(tmp_path / "repository")
    sandboxes, discovery = Sandboxes(accounts), Discovery(commands())
    model = WritesFor({RENAMES: NAMES_BOTH, "Adds tax": writes("fixed")})
    ledger = Ledger()

    with pytest.raises(ValueError):
        a_pipeline(tmp_path / "state", sandboxes, discovery).gather_within(
            ledger,
            model.claims,
            repository=accounts.path,
            before=accounts.before,
            after=accounts.after,
            model=model,
            kinds=kinds,
        )

    assert (model.requests, sandboxes.opened, discovery.asked) == ([], [], [])


# The report, through the core's entry point


def verify(
    repository: FixtureRepository, model: Model, pipeline: FixedPipeline
) -> list[tuple[Verdict, str]]:
    """The Verdict and the reason of each Claim, from the core's entry point."""
    pull_request = PullRequest(repository.path, repository.before, repository.after, "Add tax")
    report = run(pull_request, model, orchestration=pipeline)
    assert len(report.claims) > 0
    return [(claim.verdict, claim.reason) for claim in report.claims]


@pytest.mark.parametrize(
    ("response", "expected", "reason"),
    [
        (writes("fixed"), Verdict.CONFIRMED, verdict.CONFIRMED),
        (writes("broken"), Verdict.REFUTED, verdict.REFUTED),
        (writes("same"), Verdict.INCONCLUSIVE, cap_reached(AttemptResult.SHOWS_NOTHING)),
        (writes("cannot import"), Verdict.INCONCLUSIVE, cap_reached(AttemptResult.COULD_NOT_RUN)),
        (DECLARES, Verdict.INCONCLUSIVE, cap_reached(AttemptResult.NO_REPRODUCTION)),
        (
            ModelFailed("the model could not be reached"),
            Verdict.INCONCLUSIVE,
            COULD_NOT_FINISH.format(failure="the model could not be reached"),
        ),
    ],
    ids=[
        "confirmed",
        "refuted",
        "shows nothing",
        "import error",
        "a model that declares Confirmed without a Reproduction",
        "the model fails",
    ],
)
def test_pull_request_is_verified_end_to_end_and_each_verdict_comes_from_what_was_run(
    shop: FixtureRepository,
    tmp_path: Path,
    response: str | Exception,
    expected: Verdict,
    reason: str,
) -> None:
    model = WritesFor({"Adds tax to the total": response})

    verified = verify(shop, model, a_pipeline(tmp_path / "state", Sandboxes(shop)))

    assert verified == [(expected, reason)]


class LastClaimFirst:
    """Another orchestration: no Repo Profile, and the Claims worked on from the last to the
    first, by the same Reproduction writer and Reproduction judge the fixed pipeline runs."""

    def __init__(self, repository: FixtureRepository) -> None:
        self._sandbox = Sandboxes(repository)(repository.path, IMAGE)

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        written = {
            claim: write_judged_reproduction(
                claim,
                model=model,
                sandbox=self._sandbox,
                ecosystem=LineEcosystem(),
                repository=repository,
                before=before,
                after=after,
                limits=WITHIN,
            )
            for claim in reversed(claims)
        }
        return tuple(
            Gathered(() if run.evidence is None else (run.evidence,), run.inconclusive)
            for run in map(written.__getitem__, claims)
        )


def test_another_orchestration_takes_the_place_of_the_pipeline_with_the_roles_unchanged(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    responses = {"Adds tax": writes("fixed"), "Keeps": writes("broken"), "Rounds": writes("same")}
    by_the_pipeline, by_another = WritesFor(responses), WritesFor(responses)
    pull_request = PullRequest(shop.path, shop.before, shop.after, "Add tax")

    pipeline = a_pipeline(tmp_path / "state", Sandboxes(shop))
    report = run(pull_request, by_the_pipeline, orchestration=pipeline)
    another = run(pull_request, by_another, orchestration=LastClaimFirst(shop))

    assert another.claims == report.claims
    verdicts = [Verdict.CONFIRMED, Verdict.REFUTED, Verdict.INCONCLUSIVE]
    assert [claim.verdict for claim in another.claims] == verdicts
    asked = [json.loads(request.data)["task"]["claim"] for request in by_another.requests[1:]]
    assert asked == [*["Rounds"] * ATTEMPT_CAP, *["Keeps"] * 2, *["Adds tax"] * 2]


@pytest.mark.parametrize(
    ("responses", "code", "decision"),
    [
        ({"Adds tax": writes("fixed"), "Rounds": DECLARES}, 0, "pass"),
        ({"Adds tax": writes("fixed"), "Rounds": DECLARES, "Keeps": writes("broken")}, 1, "block"),
    ],
    ids=["no Claim is Refuted", "one Claim is Refuted"],
)
def test_command_line_prints_the_whole_report_and_its_exit_code_is_the_gates(
    shop: FixtureRepository,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    responses: dict[str, str | Exception],
    code: int,
    decision: str,
) -> None:
    arguments = ["--repo", str(shop.path), "--before", shop.before, "--after", shop.after]

    exited = main(
        [*arguments, "--title", "Add tax"],
        WritesFor(responses),
        ScriptedDecider(["behavioural"] * len(responses)),
        orchestration=a_pipeline(tmp_path / "state", Sandboxes(shop)),
    )

    printed = json.loads(capsys.readouterr().out)
    assert exited == code
    assert printed["gate"]["decision"] == decision
    assert [claim["text"] for claim in printed["claims"]] == list(responses)
    assert [claim["verdict"] for claim in printed["claims"]][:2] == ["confirmed", "inconclusive"]
    (shown,) = printed["claims"][0]["evidence"]
    assert (shown["reproduction"], shown["path"]) == ("fixed", "reproduction.txt")
    assert (shown["before"]["output"], shown["after"]["output"]) == (
        "failed test_total",
        "ok test_total",
    )
    assert shown["after_results"] == {"passed": ["test_total"], "failed": [], "could_not_run": []}
    assert printed["claims"][1]["evidence"] == []


# Within a Budget


class SlowSandbox(LineSandbox):
    """A `LineSandbox` in which time passes on a controlled clock: each run of a command that
    `takes` names takes that many seconds, however it ends."""

    def __init__(
        self,
        clock: Clock,
        takes: Mapping[Command, float],
        repository: Path,
        image: str,
        before: str,
        prepared: Prepared,
    ) -> None:
        super().__init__(repository, image, before, prepared)
        self._clock, self._takes = clock, takes

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self._clock.now += self._takes.get(command, 0.0)
        return super().run(command, commit=commit, phase=phase, limits=limits)


class SlowSandboxes(Sandboxes):
    """A stand-in for opening Sandboxes that gives a `SlowSandbox` each time."""

    def __init__(
        self,
        repository: FixtureRepository,
        clock: Clock,
        takes: Mapping[Command, float],
        prepared: Prepared | None = None,
    ) -> None:
        super().__init__(repository, prepared)
        self._clock, self._takes = clock, takes

    def __call__(self, repository: Path, image: str) -> LineSandbox:
        self.opened.append(
            SlowSandbox(self._clock, self._takes, repository, image, self._before, self._prepared)
        )
        return self.opened[-1]


class SlowFor(WritesFor):
    """A `WritesFor` in which time passes on a controlled clock: each request made for a Claim
    that `takes` names takes that many seconds."""

    def __init__(
        self, responses: Mapping[str, str | Exception], clock: Clock, takes: Mapping[str, float]
    ) -> None:
        super().__init__(responses)
        self._clock, self._takes = clock, takes

    def complete(self, request: ModelRequest) -> str:
        task = json.loads(request.data).get("task")
        if task is not None:
            self._clock.now += self._takes.get(task["claim"], 0.0)
        return super().complete(request)


INSTALLING = {FETCH: 20.0}
"""Installing dependencies takes twenty seconds for each of the two commits: forty in all."""

THREE = {"Adds tax": writes("fixed"), "Rounds": writes("same"), "Keeps": writes("broken")}


def gather_within(
    pipeline: FixedPipeline, ledger: Ledger, repository: FixtureRepository, model: WritesFor
) -> list[Gathered]:
    """What the pipeline gathers within `ledger` for the Claims the model has a response for."""
    return list(
        pipeline.gather_within(
            ledger,
            model.claims,
            repository=repository.path,
            before=repository.before,
            after=repository.after,
            model=model,
        )
    )


def test_within_a_budget_with_room_to_spare_each_claim_is_given_what_gather_gives_it(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(clock=Clock())

    within = gather_within(a_pipeline(tmp_path / "within", Sandboxes(shop)), ledger, shop, model)

    pipeline = a_pipeline(tmp_path / "state", Sandboxes(shop))
    assert within == gather(pipeline, shop, WritesFor(THREE))
    # Every request made for a Claim is one step against that Claim.
    assert [ledger.spent(claim=place).steps for place in range(3)] == [
        len(model.tasks(claim)) for claim in model.claims
    ]
    assert (ledger.spent().steps, ledger.stopped_by()) == (len(model.requests), None)


def test_within_a_budget_with_no_claims_nothing_is_done_and_the_ledger_is_not_used(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes, discovery = Sandboxes(shop), Discovery(commands())
    ledger = Ledger(clock=Clock())

    gathered = gather_within(
        a_pipeline(tmp_path / "state", sandboxes, discovery), ledger, shop, WritesFor({})
    )

    assert (gathered, discovery.asked, sandboxes.opened) == ([], [], [])
    ledger.share_out(1)  # No share had been set aside.


def test_preparing_is_timed_against_the_pull_request_alone_and_comes_out_of_no_claims_share(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    clock = Clock()
    sandboxes = SlowSandboxes(shop, clock, INSTALLING)
    ledger = Ledger(Budget(seconds=100.0), clock=clock)

    gather_within(a_pipeline(tmp_path / "state", sandboxes), ledger, shop, WritesFor(THREE))

    assert ledger.spent().seconds == 40.0
    assert [ledger.spent(claim=place).seconds for place in range(3)] == [0.0, 0.0, 0.0]
    # Preparing is given the pipeline's limits whole. Sixty seconds are left for three Claims,
    # and a run for a Claim is given no longer than the twenty of its share.
    (sandbox,) = sandboxes.opened
    preparing = {limits for command, _, _, limits in sandbox.runs if command in (FETCH, TEST)}
    for_a_claim = {limits for command, _, _, limits in sandbox.runs if command not in (FETCH, TEST)}
    assert (preparing, for_a_claim) == ({WITHIN}, {replace(WITHIN, seconds=20.0)})


def test_claim_that_spends_its_share_is_inconclusive_and_the_others_still_receive_their_verdicts(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    clock = Clock()
    sandboxes = SlowSandboxes(shop, clock, {**INSTALLING, ("run-reproduction",): 1.0})
    # The share of each Claim is twenty seconds, and one request made for `Rounds` takes longer.
    model = SlowFor(THREE, clock, {"Rounds": 25.0})
    pull_request = PullRequest(shop.path, shop.before, shop.after, "Add tax")

    report = run(
        pull_request,
        model,
        orchestration=a_pipeline(tmp_path / "state", sandboxes),
        budget=Budget(seconds=100.0),
        clock=clock,
    )

    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.CONFIRMED, verdict.CONFIRMED),
        (Verdict.INCONCLUSIVE, SHARE_SPENT.format(limit="time")),
        (Verdict.REFUTED, verdict.REFUTED),
    ]
    spend = report.budget_spend
    assert isinstance(spend, PullRequestSpend)
    # Each of the other two ran its Reproduction once against each commit, a second a run, and
    # no run is timed twice: 40 seconds of installing and 29 for the Claims.
    # The stand-in model has no price, as a local one has none: it costs time and no money.
    assert (spend.seconds, spend.dollars, spend.stopped_by) == (69.0, 0.0, None)
    assert spend.steps == len(model.requests)
    assert [(claim.seconds, claim.stopped_by) for claim in spend.claims] == [
        (2.0, None),
        (25.0, BudgetLimit.TIME),
        (2.0, None),
    ]
    assert [claim.steps for claim in spend.claims] == [
        len(model.tasks(claim)) for claim in model.claims
    ]
    # Once its share was spent, nothing more was asked or run for the Claim.
    (sandbox,) = sandboxes.opened
    assert len(model.tasks("Rounds")) == 1
    assert ("write", "same") not in [command for command, *_ in sandbox.runs]


def test_claims_still_unresolved_when_the_budget_is_spent_are_inconclusive_and_no_work_is_started(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    clock = Clock()
    sandboxes = SlowSandboxes(shop, clock, INSTALLING)
    # Sixty seconds are left once both commits are prepared, and one request takes them all.
    model = SlowFor(THREE, clock, {"Rounds": 60.0})
    pull_request = PullRequest(shop.path, shop.before, shop.after, "Add tax")

    report = run(
        pull_request,
        model,
        orchestration=a_pipeline(tmp_path / "state", sandboxes),
        budget=Budget(seconds=100.0),
        clock=clock,
    )

    # Reaching the limit is not a failure. The Claim answered for before then keeps its Verdict.
    spent = BUDGET_SPENT.format(limit="time")
    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.CONFIRMED, verdict.CONFIRMED),
        (Verdict.INCONCLUSIVE, spent),
        (Verdict.INCONCLUSIVE, spent),
    ]
    assert report.gate.decision is GateDecision.PASS
    spend = report.budget_spend
    assert isinstance(spend, PullRequestSpend)
    assert (spend.seconds, spend.stopped_by) == (100.0, BudgetLimit.TIME)
    # The share of a Claim says nothing of the Budget: that of the Claim never worked on is whole.
    assert [claim.stopped_by for claim in spend.claims] == [None, BudgetLimit.TIME, None]
    (sandbox,) = sandboxes.opened
    assert (len(model.tasks("Rounds")), model.tasks("Keeps")) == (1, [])
    assert sandbox.closed
    assert {"same", "broken"}.isdisjoint(command[-1] for command, *_ in sandbox.runs)


def test_budget_already_spent_on_the_call_leaves_every_claim_unresolved_and_starts_nothing(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes, discovery, model = Sandboxes(shop), Discovery(commands()), WritesFor(THREE)
    ledger = Ledger(Budget(dollars=1.0), clock=Clock())
    ledger.record(BudgetSpend(seconds=0.0, dollars=1.0, steps=0))

    gathered = gather_within(
        a_pipeline(tmp_path / "state", sandboxes, discovery), ledger, shop, model
    )

    assert gathered == [Gathered((), BUDGET_SPENT.format(limit="money"))] * 3
    assert (discovery.asked, sandboxes.opened, model.requests) == ([], [], [])


@pytest.mark.parametrize("seconds", [15.0, 40.0], ids=["by the first install", "by the last"])
def test_budget_spent_while_preparing_leaves_every_claim_unresolved_and_asks_the_model_nothing(
    shop: FixtureRepository, tmp_path: Path, seconds: float
) -> None:
    clock = Clock()
    sandboxes, model = SlowSandboxes(shop, clock, INSTALLING), WritesFor(THREE)
    ledger = Ledger(Budget(seconds=seconds), clock=clock)

    gathered = gather_within(a_pipeline(tmp_path / "state", sandboxes), ledger, shop, model)

    assert gathered == [Gathered((), BUDGET_SPENT.format(limit="time"))] * 3
    assert model.requests == []
    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)


@pytest.mark.parametrize(
    ("how", "failure"),
    [
        (FAILED, "the command fetch --locked ended with exit status 1"),
        (NO_RUNTIME, "no container runtime"),
    ],
    ids=["the install fails", "the Sandbox fails"],
)
def test_commit_that_cannot_be_prepared_is_answered_for_as_one_whatever_was_spent_by_then(
    shop: FixtureRepository, tmp_path: Path, how: RunResult | SandboxFailed, failure: str
) -> None:
    clock = Clock()
    sandboxes = SlowSandboxes(shop, clock, INSTALLING, {(FETCH, "before"): how})
    ledger = Ledger(Budget(seconds=15.0), clock=clock)

    gathered = gather_within(
        a_pipeline(tmp_path / "state", sandboxes), ledger, shop, WritesFor(THREE)
    )

    reason = COULD_NOT_PREPARE.format(commit="before", failure=failure)
    assert gathered == [Gathered((), reason)] * 3
    assert ledger.stopped_by() is BudgetLimit.TIME


# A role that goes round in circles, through the core's entry point
#
# The pipeline gives the writer its own step cap, so these tests need a loop to be found, and
# the progress guard to come due, within `WRITER_STEP_CAP` steps.

CLAIM = "Adds tax"
READS, READS_ANOTHER = asks("read_file", path="total.txt"), asks("read_file", path="deps.lock")
NEVER_THE_SAME = [asks("read_file", path=f"{number}.txt") for number in range(WRITER_STEP_CAP)]


class GoesRoundUntilTold:
    """A stand-in model that makes one Claim. As the Reproduction writer it takes the steps
    prepared, round and round, until the loop tells it something, and then writes the
    Reproduction prepared. As the Reproduction judge it accepts what it is shown."""

    told: list[str | None]
    """What the writer was shown it was told after each of its steps, when it stopped going
    round: None for a step it was told nothing after. Empty while it has been told nothing."""

    def __init__(self, steps: Sequence[str], reproduction: str = "fixed") -> None:
        self._steps, self._reproduction = steps, reproduction
        self.told = []

    def complete(self, request: ModelRequest) -> str:
        asked = json.loads(request.data)
        if "task" not in asked:
            return json.dumps({"claims": [CLAIM]})
        if "reproduction" in asked["task"]:
            return finishes(accepted=True)
        told = [step.get("told") for step in asked["steps"]]
        if not any(told):
            return self._steps[len(told) % len(self._steps)]
        self.told = told
        return writes(self._reproduction)


@pytest.mark.parametrize(
    ("steps", "kind", "found"),
    [
        ([READS], LoopKind.EXACT_REPEAT, REPEAT_LENGTH),
        ([READS, READS_ANOTHER], LoopKind.ALTERNATION, ALTERNATION_LENGTH),
    ],
    ids=["the same step again and again", "back and forth between two steps"],
)
def test_role_that_goes_round_in_circles_is_told_what_was_detected_and_goes_on_to_a_verdict(
    shop: FixtureRepository, tmp_path: Path, steps: list[str], kind: LoopKind, found: int
) -> None:
    model = GoesRoundUntilTold(steps)

    verified = verify(shop, model, a_pipeline(tmp_path / "state", Sandboxes(shop)))

    assert [given for given, _ in verified] == [Verdict.CONFIRMED]
    assert model.told == [*[None] * (found - 1), TOLD[kind]]


def test_progress_guard_is_a_decision_that_tells_a_role_whose_steps_all_differ_it_gets_no_closer(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, guard = GoesRoundUntilTold(NEVER_THE_SAME), ScriptedDecider([NO])
    pipeline = replace(a_pipeline(tmp_path / "state", Sandboxes(shop)), progress_guard=guard)

    verified = verify(shop, model, pipeline)

    ((state, question),) = guard.asked
    assert question == PROGRESS
    assert json.loads(state)["task"]["claim"] == CLAIM
    assert model.told == [*[None] * (PROGRESS_INTERVAL - 1), TOLD[LoopKind.NO_PROGRESS]]
    assert [given for given, _ in verified] == [Verdict.CONFIRMED]


def test_role_that_goes_round_whatever_it_is_told_is_stopped_at_its_caps_and_its_claim_inconclusive(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model = WritesFor({CLAIM: READS})

    verified = verify(shop, model, a_pipeline(tmp_path / "state", Sandboxes(shop)))

    last = json.loads(model.requests[-1].data)["steps"]
    assert verified == [(Verdict.INCONCLUSIVE, cap_reached(AttemptResult.NO_REPRODUCTION))]
    assert len(model.tasks(CLAIM)) == ATTEMPT_CAP * WRITER_STEP_CAP
    assert [step.get("told") for step in last] == [
        *[None] * (REPEAT_LENGTH - 1),
        *[TOLD[LoopKind.EXACT_REPEAT]] * (WRITER_STEP_CAP - REPEAT_LENGTH),
    ]


# A Python pull request, in a real Sandbox

ITS_OWN_TEST = """\
def test_nothing_costs_nothing():
    from pricing import total

    assert total(0) == 0
"""
ADDS_TAX = """\
def test_total_adds_tax():
    from pricing import total

    assert total(100) == 120.0
"""
KEEPS_THE_NET_PRICE = """\
def test_total_is_the_net_price():
    from pricing import total

    assert total(100) == 100
"""
CANNOT_IMPORT = f"import a_module_nobody_wrote\n\n\n{ADDS_TAX}"
PYTHON = {
    "Adds tax to the total": writes(ADDS_TAX),
    "Leaves the total at the net price": writes(KEEPS_THE_NET_PRICE),
    "Rounds the total": writes(CANNOT_IMPORT),
}


@pytest.fixture(scope="module")
def pricing(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """Plain Python modules with a test of their own. The pull request makes `total` add tax."""
    path = tmp_path_factory.mktemp("pricing") / "repository"
    before, after = commit_snapshots(
        path,
        {"pricing.py": "def total(net):\n    return net\n", "test_pricing.py": ITS_OWN_TEST},
        {
            "pricing.py": "def total(net):\n    return round(net * 1.2, 2)\n",
            "test_pricing.py": ITS_OWN_TEST,
        },
    )
    return FixtureRepository(path=path, before=before, after=after)


def test_command_line_verifies_a_python_pull_request_and_keeps_its_repo_profile_in_the_state_dir(
    pricing: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    state = tmp_path / "state"
    arguments = ["--repo", str(pricing.path), "--before", pricing.before, "--after", pricing.after]

    exited = main(
        [*arguments, "--title", "Add tax to the total", "--state-dir", str(state)],
        WritesFor(PYTHON),
        ScriptedDecider(["behavioural"] * 3),
    )

    printed = json.loads(capsys.readouterr().out)
    confirmed, refuted, inconclusive = printed["claims"]
    assert exited == 1
    assert printed["gate"]["decision"] == "block"
    assert (confirmed["verdict"], confirmed["reason"]) == ("confirmed", verdict.CONFIRMED)
    assert (refuted["verdict"], refuted["reason"]) == ("refuted", verdict.REFUTED)
    assert (inconclusive["verdict"], inconclusive["evidence"]) == ("inconclusive", [])
    assert inconclusive["reason"] == cap_reached(AttemptResult.COULD_NOT_RUN)
    (shown,) = confirmed["evidence"]
    test = f"{REPRODUCTION_PATH}::test_total_adds_tax"
    assert (shown["path"], shown["reproduction"]) == (REPRODUCTION_PATH, ADDS_TAX)
    assert (shown["before"]["exit_status"], shown["after"]["exit_status"]) == (1, 0)
    assert "test_total_adds_tax" in shown["before"]["output"]
    assert shown["before_results"] == {"passed": [], "failed": [test], "could_not_run": []}
    assert shown["after_results"] == {"passed": [test], "failed": [], "could_not_run": []}
    assert DirectoryStore(state / "profiles").load(pricing.path) is not None


def test_python_pull_request_is_verified_with_no_orchestration_given_and_nothing_is_kept(
    pricing: FixtureRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    pull_request = PullRequest(pricing.path, pricing.before, pricing.after, "Add tax to the total")

    report = run(pull_request, WritesFor({"Adds tax to the total": writes(ADDS_TAX)}))

    (claim,) = report.claims
    assert (claim.verdict, claim.reason) == (Verdict.CONFIRMED, verdict.CONFIRMED)
    assert report.gate.decision is GateDecision.PASS
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("kept", [True, False], ids=["with a state directory", "with none"])
def test_command_line_gives_its_decider_to_the_fixed_pipeline_as_the_progress_guard(
    pricing: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str], kept: bool
) -> None:
    arguments = ["--repo", str(pricing.path), "--before", pricing.before, "--after", pricing.after]
    state = ["--state-dir", str(tmp_path / "state")] if kept else []
    model = GoesRoundUntilTold(NEVER_THE_SAME, ADDS_TAX)
    decider = ScriptedDecider(["behavioural", NO])

    exited = main([*arguments, "--title", "Add tax to the total", *state], model, decider)

    (claim,) = json.loads(capsys.readouterr().out)["claims"]
    assert (exited, claim["verdict"]) == (0, "confirmed")
    assert [question for _, question in decider.asked[1:]] == [PROGRESS]
    assert model.told == [*[None] * (PROGRESS_INTERVAL - 1), TOLD[LoopKind.NO_PROGRESS]]
