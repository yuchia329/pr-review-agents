"""The Reproduction judge: what it is shown, what counts as accepting, and the rounds between it
and the Reproduction writer.

Every test uses a stand-in model. The rounds also use a stand-in Sandbox and a stand-in Ecosystem.
"""

import json
from collections.abc import Iterable, Mapping
from itertools import count, repeat
from pathlib import Path

import pytest
from conftest import (
    FixtureRepository,
    UnreachableModel,
    asks,
    assert_shows_only_whole_responses,
    commit_snapshots,
    finishes,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification.agent import NO_SUCH_TOOL, PROGRESS, PROGRESS_INTERVAL, StepOutcome, Stopped
from pr_verification.decision import YES, Decider, DecisionFailed, ScriptedDecider
from pr_verification.ecosystem import Commands, Placement, Results, Unreadable, Unsupported
from pr_verification.judge import (
    JUDGE_COULD_NOT_FINISH,
    JUDGE_STEP_CAP,
    NO_REASON_GIVEN,
    ROUND_CAP,
    ROUND_CAP_REACHED,
    JudgedRun,
    judge_reproduction,
    write_judged_reproduction,
)
from pr_verification.model import ModelFailed, ModelRequest, ScriptedModel
from pr_verification.report import AcceptedReproduction
from pr_verification.reproduction import (
    ATTEMPT_CAP,
    ATTEMPT_CAP_REACHED,
    COULD_NOT_FINISH,
    ENDINGS,
    AttemptResult,
    Rejected,
    rewrite_reproduction,
)
from pr_verification.sandbox import Command, Limits, Phase, RunResult

CLAIM = "Adds tax to the total"
REPRODUCTION = "\ndef test_total_adds_tax():\n    assert total(100) == 120.0\n"
WHY = "It asserts nothing about the total."
UNREACHABLE = "the model could not be reached"
LIMITS = Limits(seconds=120.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)

ACCEPTS = finishes(accepted=True)


def rejects(reason: object = WHY) -> str:
    """A response in which the judge rejects a Reproduction and gives this as its reason."""
    return finishes(accepted=False, reason=reason)


def wanders(steps: int) -> list[str]:
    """Responses in which a role asks for a different file at each step, and never finishes."""
    return [asks("read_file", path=f"notes/{number}.txt") for number in range(steps)]


# The judge


def test_judge_is_a_role_with_no_tools_whose_task_holds_the_claim_and_the_reproduction_alone() -> (
    None
):
    reads, runs = asks("read_file", path="pricing.py"), asks("run_command", command="pytest")
    model = ScriptedModel([reads, runs, ACCEPTS])

    judgement = judge_reproduction(CLAIM, REPRODUCTION, model=model, step_cap=3)

    assert judgement.run.role == "reproduction-judge"
    assert [(step.outcome, step.result) for step in judgement.run.steps[:2]] == [
        (StepOutcome.REFUSED, NO_SUCH_TOOL),
        (StepOutcome.REFUSED, NO_SUCH_TOOL),
    ]
    assert json.loads(model.requests[0].data) == {
        "task": {"claim": CLAIM, "reproduction": REPRODUCTION},
        "steps": [],
    }
    for request in model.requests:
        assert CLAIM not in request.instructions
        assert "test_total_adds_tax" not in request.instructions


@pytest.mark.parametrize(
    "answer",
    [ACCEPTS, finishes(accepted=True, reason=WHY), finishes(accepted=True, verdict="confirmed")],
    ids=["accepted", "with a reason", "with another member"],
)
def test_reproduction_is_accepted_when_the_answer_holds_true_under_accepted_and_has_no_reason(
    answer: str,
) -> None:
    judgement = judge_reproduction(CLAIM, REPRODUCTION, model=ScriptedModel([answer]))

    assert (judgement.accepted, judgement.reason) == (True, None)
    assert judgement.run.stopped is Stopped.FINISHED


@pytest.mark.parametrize(
    ("answer", "reason"),
    [
        (rejects(WHY), WHY),
        (rejects(f"  {WHY}\n"), f"  {WHY}\n"),
        (finishes(accepted="true", reason=WHY), WHY),
        (finishes(reason=WHY), WHY),
        (finishes(accepted=False), NO_REASON_GIVEN),
        (rejects(""), NO_REASON_GIVEN),
        (rejects(" \n\t"), NO_REASON_GIVEN),
        (rejects(7), NO_REASON_GIVEN),
        (rejects([WHY]), NO_REASON_GIVEN),
        (finishes(accepted="true"), NO_REASON_GIVEN),
        (finishes(accepted=1), NO_REASON_GIVEN),
        (finishes(accepted=None), NO_REASON_GIVEN),
        (finishes(accepted=[True]), NO_REASON_GIVEN),
        (finishes(), NO_REASON_GIVEN),
        (finishes(verdict="accepted"), NO_REASON_GIVEN),
    ],
    ids=[
        "false with a reason",
        "a reason kept exactly as given",
        "the word true with a reason",
        "no accepted with a reason",
        "false with no reason",
        "an empty reason",
        "a blank reason",
        "a number for a reason",
        "a list for a reason",
        "the word true",
        "the number 1",
        "null",
        "a list holding true",
        "an empty answer",
        "another member",
    ],
)
def test_anything_but_true_under_accepted_is_a_rejection_with_the_reason_given_or_none_given(
    answer: str, reason: str
) -> None:
    judgement = judge_reproduction(CLAIM, REPRODUCTION, model=ScriptedModel([answer]))

    assert (judgement.accepted, judgement.reason) == (False, reason)


def test_judge_that_does_not_finish_within_its_step_cap_accepts_nothing() -> None:
    by_default = judge_reproduction(
        CLAIM, REPRODUCTION, model=ScriptedModel([*wanders(JUDGE_STEP_CAP), ACCEPTS])
    )
    capped = judge_reproduction(
        CLAIM, REPRODUCTION, model=ScriptedModel([*wanders(1), ACCEPTS]), step_cap=1
    )

    for judgement, steps in ((by_default, JUDGE_STEP_CAP), (capped, 1)):
        assert judgement.run.stopped is Stopped.STEP_CAP
        assert len(judgement.run.steps) == steps
        assert (judgement.accepted, judgement.reason) == (False, NO_REASON_GIVEN)


def test_judge_raises_as_the_agent_loop_does_for_a_cap_under_one_and_for_a_model_that_fails() -> (
    None
):
    model = ScriptedModel([ACCEPTS])

    with pytest.raises(ValueError):
        judge_reproduction(CLAIM, REPRODUCTION, model=model, step_cap=0)
    with pytest.raises(ModelFailed):
        judge_reproduction(CLAIM, REPRODUCTION, model=UnreachableModel())

    assert model.requests == []


def test_judges_run_is_watched_by_the_progress_guard_it_is_given() -> None:
    guard = ScriptedDecider([YES])
    model = ScriptedModel([*wanders(PROGRESS_INTERVAL), ACCEPTS])

    judgement = judge_reproduction(
        CLAIM, REPRODUCTION, model=model, step_cap=PROGRESS_INTERVAL + 1, progress_guard=guard
    )

    ((state, question),) = guard.asked
    assert question == PROGRESS
    assert json.loads(state)["task"] == {"claim": CLAIM, "reproduction": REPRODUCTION}
    assert judgement.accepted


# The rounds, with a stand-in Sandbox and a stand-in Ecosystem


def ended(output: str, exit_status: int = 0) -> RunResult:
    return RunResult(output=output, exit_status=exit_status, limit_hit=None)


PASSES, FAILS = ended("ok test_tax SANDBOX-OUTPUT"), ended("failed test_tax SANDBOX-OUTPUT", 1)
# What each Reproduction a stand-in model writes does on the code before and the code after. The
# planted ones show a difference, as a test of the Claim does, and are tests of something else.
BEHAVIOUR: Mapping[str, tuple[RunResult, RunResult]] = {
    "fixed": (FAILS, PASSES),
    "broken": (PASSES, FAILS),
    "planted": (FAILS, PASSES),
    "planted again": (FAILS, PASSES),
    "same": (PASSES, PASSES),
}


class LineEcosystem:
    """A stand-in with nothing of Python in it: a run prints one line for each test."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        return Unsupported("A stand-in installs nothing.")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        lines = [line.split() for line in run.output.splitlines()]
        return Results(
            passed=tuple(name for word, name, *_ in lines if word == "ok"),
            failed=tuple(name for word, name, *_ in lines if word == "failed"),
            could_not_run=(),
        )

    def reproduction_form(self) -> str:
        return "A Reproduction is one word."

    def place(self, reproduction: str) -> Placement:
        return Placement("reproduction.txt", ("write", reproduction), ("run-reproduction",))


class StandInSandbox:
    """Keeps what is written for each commit, and runs it as `BEHAVIOUR` says it behaves."""

    def __init__(self, repository: FixtureRepository) -> None:
        self.runs: list[Command] = []
        self._before = repository.before
        self._written: dict[str, str] = {}

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self.runs.append(command)
        if command[0] == "write":
            self._written[commit] = command[1]
            return ended("")
        before, after = BEHAVIOUR[self._written[commit]]
        return before if commit == self._before else after


Task = dict[str, object]


class WriterAndJudge:
    """A stand-in model that gives the Reproduction writer and the Reproduction judge each its
    own prepared responses, in order, or raises them. It records each role's requests."""

    def __init__(
        self, writer: Iterable[str | Exception], judge: Iterable[str | Exception] = ()
    ) -> None:
        self._responses = {"writer": iter(writer), "judge": iter(judge)}
        self.asked: list[tuple[str, ModelRequest]] = []

    def complete(self, request: ModelRequest) -> str:
        role = "judge" if "reproduction" in json.loads(request.data)["task"] else "writer"
        self.asked.append((role, request))
        response = next(self._responses[role])
        if isinstance(response, Exception):
            raise response
        return response

    def tasks(self, role: str) -> list[Task]:
        """The task of each request made by one role, in order."""
        return [json.loads(request.data)["task"] for asked, request in self.asked if asked == role]


def writes(reproduction: str) -> str:
    """A response in which the Reproduction writer finishes with a Reproduction."""
    return finishes(reproduction=reproduction)


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A repository with nothing of any Ecosystem in it, and a pull request that changes it."""
    path = tmp_path_factory.mktemp("judge") / "repository"
    before, after = commit_snapshots(
        path, {"total.txt": "net\n"}, {"total.txt": "CODE-OF-THE-PULL-REQUEST\n"}
    )
    return FixtureRepository(path=path, before=before, after=after)


def judged(
    repository: FixtureRepository,
    model: WriterAndJudge,
    sandbox: StandInSandbox | None = None,
    progress_guard: Decider | None = None,
    **caps: int,
) -> JudgedRun:
    return write_judged_reproduction(
        CLAIM,
        model=model,
        sandbox=sandbox or StandInSandbox(repository),
        ecosystem=LineEcosystem(),
        repository=repository.path,
        before=repository.before,
        after=repository.after,
        limits=LIMITS,
        progress_guard=progress_guard,
        **caps,
    )


@pytest.mark.parametrize("text", ["fixed", "broken"])
def test_reproduction_the_judge_accepts_is_the_evidence_and_ends_the_work(
    shop: FixtureRepository, text: str
) -> None:
    model = WriterAndJudge(writer=[writes(text), writes("same")], judge=[ACCEPTS])

    run = judged(shop, model)

    (only,) = run.rounds
    written = only.written.evidence
    assert written is not None and not isinstance(written, AcceptedReproduction)
    assert isinstance(run.evidence, AcceptedReproduction)
    assert vars(run.evidence) == vars(written)
    assert written.reproduction == text
    assert only.judgement is not None and only.judgement.accepted
    assert run.inconclusive is None
    assert [role for role, _ in model.asked] == ["writer", "judge"]


def test_judge_is_shown_the_claim_and_the_reproduction_and_nothing_of_its_runs_or_of_the_code(
    shop: FixtureRepository,
) -> None:
    reads = asks("read_file", path="total.txt")
    model = WriterAndJudge(writer=[reads, writes("fixed")], judge=[ACCEPTS])

    judged(shop, model)

    ((_, request),) = (asked for asked in model.asked if asked[0] == "judge")
    assert json.loads(request.data) == {
        "task": {"claim": CLAIM, "reproduction": "fixed"},
        "steps": [],
    }
    assert any("CODE-OF-THE-PULL-REQUEST" in asked.data for _, asked in model.asked)
    for untrusted in (CLAIM, "CODE-OF-THE-PULL-REQUEST", "SANDBOX-OUTPUT"):
        assert untrusted not in request.instructions


def test_rejection_goes_back_to_the_writer_with_its_reason_beside_every_earlier_one(
    shop: FixtureRepository,
) -> None:
    model = WriterAndJudge(
        writer=[writes("planted"), writes("planted again"), writes("fixed")],
        judge=[rejects(WHY), finishes(accepted=False), ACCEPTS],
    )

    run = judged(shop, model)

    first = {"reproduction": "planted", "reason": WHY}
    second = {"reproduction": "planted again", "reason": NO_REASON_GIVEN}
    assert model.tasks("writer") == [
        {"claim": CLAIM, "attempts": []},
        {"claim": CLAIM, "attempts": [], "rejected": [first]},
        {"claim": CLAIM, "attempts": [], "rejected": [first, second]},
    ]
    assert model.tasks("judge") == [
        {"claim": CLAIM, "reproduction": text} for text in ("planted", "planted again", "fixed")
    ]
    judgements = [made.judgement for made in run.rounds if made.judgement is not None]
    assert [(judgement.accepted, judgement.reason) for judgement in judgements] == [
        (False, WHY),
        (False, NO_REASON_GIVEN),
        (True, None),
    ]
    assert run.evidence is not None and run.evidence.reproduction == "fixed"


@pytest.mark.parametrize(
    ("round_cap", "rounds"),
    [(None, ROUND_CAP), (1, 1), (2, 2)],
    ids=["by default", "one round", "two rounds"],
)
def test_claim_is_inconclusive_with_the_last_reason_when_the_cap_on_rounds_is_reached(
    shop: FixtureRepository, round_cap: int | None, rounds: int
) -> None:
    reasons = (rejects(f"Reason {number}.") for number in count(1))
    model = WriterAndJudge(writer=repeat(writes("planted")), judge=reasons)

    run = judged(shop, model) if round_cap is None else judged(shop, model, round_cap=round_cap)

    assert len(run.rounds) == len(model.tasks("judge")) == len(model.tasks("writer")) == rounds
    assert [made.judgement and made.judgement.accepted for made in run.rounds] == [False] * rounds
    assert run.evidence is None
    assert run.inconclusive == ROUND_CAP_REACHED.format(rounds=rounds, reason=f"Reason {rounds}.")


NEVER_SHOWED_A_DIFFERENCE = ATTEMPT_CAP_REACHED.format(
    attempts=ATTEMPT_CAP, ending=ENDINGS[AttemptResult.SHOWS_NOTHING]
)


@pytest.mark.parametrize(
    ("writer", "judge", "rounds", "inconclusive"),
    [
        ([writes("same")] * ATTEMPT_CAP, [], 1, NEVER_SHOWED_A_DIFFERENCE),
        ([ModelFailed(UNREACHABLE)], [], 1, COULD_NOT_FINISH.format(failure=UNREACHABLE)),
        (
            [writes("planted"), *[writes("same")] * ATTEMPT_CAP],
            [rejects()],
            2,
            NEVER_SHOWED_A_DIFFERENCE,
        ),
    ],
    ids=["shows nothing", "the model fails", "shows nothing after a rejection"],
)
def test_writer_that_gives_no_evidence_ends_the_work_with_its_own_reason_and_nothing_is_judged(
    shop: FixtureRepository,
    writer: list[str | Exception],
    judge: list[str | Exception],
    rounds: int,
    inconclusive: str,
) -> None:
    model = WriterAndJudge(writer, judge)

    run = judged(shop, model)

    assert len(run.rounds) == rounds
    last = run.rounds[-1]
    assert (last.written.evidence, last.judgement) == (None, None)
    assert (run.evidence, run.inconclusive) == (None, inconclusive)
    assert last.written.inconclusive == inconclusive
    assert len(model.tasks("judge")) == len(judge)


@pytest.mark.parametrize(
    "rejected",
    [[], [Rejected("planted", WHY), Rejected("planted again", NO_REASON_GIVEN)]],
    ids=["nothing rejected", "two rejected"],
)
def test_writer_is_shown_what_the_judge_rejected_on_every_attempt_of_a_later_round(
    shop: FixtureRepository, rejected: list[Rejected]
) -> None:
    model = WriterAndJudge(writer=[writes("same"), writes("fixed")])

    run = rewrite_reproduction(
        CLAIM,
        rejected,
        model=model,
        sandbox=StandInSandbox(shop),
        ecosystem=LineEcosystem(),
        repository=shop.path,
        before=shop.before,
        after=shop.after,
        limits=LIMITS,
    )

    shown = [{"reproduction": each.reproduction, "reason": each.reason} for each in rejected]
    earlier = {"reproduction": "same", "result": AttemptResult.SHOWS_NOTHING.value}
    assert model.tasks("writer") == [
        {"claim": CLAIM, "attempts": [], "rejected": shown},
        {
            "claim": CLAIM,
            "attempts": [{**earlier, "before": PASSES.output, "after": PASSES.output}],
            "rejected": shown,
        },
    ]
    assert run.evidence is not None and run.evidence.reproduction == "fixed"
    assert not isinstance(run.evidence, AcceptedReproduction)


# A failing progress guard raises a `DecisionFailed`. Within the rounds the judge stops before its
# guard comes due, so the failure is raised here by the stand-in model in its place.
@pytest.mark.parametrize(
    "failure",
    [ModelFailed(UNREACHABLE), DecisionFailed(UNREACHABLE)],
    ids=["a model that fails", "a decision that fails"],
)
def test_model_that_fails_while_the_judge_is_at_work_ends_the_work_and_the_round_is_kept(
    shop: FixtureRepository, failure: ModelFailed
) -> None:
    sandbox = StandInSandbox(shop)
    model = WriterAndJudge(writer=repeat(writes("fixed")), judge=[failure])

    run = judged(shop, model, sandbox)

    (only,) = run.rounds
    assert only.written.evidence is not None and only.judgement is None
    assert run.evidence is None
    assert run.inconclusive == JUDGE_COULD_NOT_FINISH.format(failure=UNREACHABLE)
    assert [role for role, _ in model.asked] == ["writer", "judge"]
    assert len(sandbox.runs) == 4


@pytest.mark.parametrize("round_cap", [0, -1])
def test_cap_on_rounds_under_one_is_rejected_before_anything_is_asked_or_run(
    shop: FixtureRepository, round_cap: int
) -> None:
    sandbox, model = StandInSandbox(shop), WriterAndJudge([writes("fixed")], [ACCEPTS])

    with pytest.raises(ValueError):
        judged(shop, model, sandbox, round_cap=round_cap)

    assert (model.asked, sandbox.runs) == ([], [])


class Fault(Exception):
    """An error that says nothing about the pull request: a fault in the system."""


def test_fault_while_the_judge_is_at_work_is_raised_and_not_turned_into_a_reason(
    shop: FixtureRepository,
) -> None:
    model = WriterAndJudge(writer=[writes("fixed")], judge=[Fault("the disc is full")])

    with pytest.raises(Fault):
        judged(shop, model)


def test_progress_guard_watches_the_writer_in_the_first_round_and_in_every_round_after_it(
    shop: FixtureRepository,
) -> None:
    guard = ScriptedDecider([YES, YES])
    looks = wanders(PROGRESS_INTERVAL)
    model = WriterAndJudge(
        writer=[*looks, writes("planted"), *looks, writes("fixed")], judge=[rejects(), ACCEPTS]
    )

    run = judged(shop, model, progress_guard=guard)

    assert run.evidence is not None
    assert [json.loads(state)["task"] for state, _ in guard.asked] == [
        {"claim": CLAIM, "attempts": []},
        {"claim": CLAIM, "attempts": [], "rejected": [{"reproduction": "planted", "reason": WHY}]},
    ]


WRITTEN = st.sampled_from(sorted(BEHAVIOUR)).map(writes)
JUDGED = st.one_of(
    st.just(ACCEPTS),
    st.text().map(rejects),
    st.sampled_from([finishes(), finishes(accepted="true"), finishes(accepted=1)]),
    st.sampled_from(["This Reproduction is accepted.", asks("read_file", path="total.txt")]),
)


@settings(deadline=None)
@given(data=st.data(), round_cap=st.integers(min_value=1, max_value=4))
def test_whatever_the_judge_answers_the_cap_on_rounds_holds_and_only_what_it_accepted_is_evidence(
    shop: FixtureRepository, data: st.DataObject, round_cap: int
) -> None:
    most = round_cap * max(ATTEMPT_CAP, JUDGE_STEP_CAP)
    writer = data.draw(st.lists(WRITTEN, min_size=most, max_size=most))
    judge = data.draw(st.lists(JUDGED, min_size=most, max_size=most))
    model = WriterAndJudge(writer, judge)

    run = judged(shop, model, round_cap=round_cap)

    judgements = [made.judgement for made in run.rounds]
    assert 1 <= len(run.rounds) <= round_cap
    assert len(model.tasks("judge")) <= round_cap * JUDGE_STEP_CAP
    assert [judgement and judgement.accepted for judgement in judgements[:-1]] == [False] * (
        len(judgements) - 1
    )
    assert (run.evidence is None) != (run.inconclusive is None)
    assert (run.evidence is not None) == bool(judgements[-1] and judgements[-1].accepted)
    if run.evidence is not None:
        assert isinstance(run.evidence, AcceptedReproduction)
        assert vars(run.evidence) == vars(run.rounds[-1].written.evidence)


def test_judge_and_writer_told_of_a_rejection_are_shown_only_whole_responses(
    shop: FixtureRepository,
) -> None:
    model = WriterAndJudge(
        writer=[writes("planted"), writes("fixed")], judge=[rejects(WHY), ACCEPTS]
    )

    judged(shop, model)

    instructions = {(role, request.instructions) for role, request in model.asked}
    assert {role for role, _ in instructions} == {"writer", "judge"}
    for _, shown in instructions:
        assert_shows_only_whole_responses(shown)
