"""A role stopped for going round in circles: what each thing that runs a role makes of it, and
the loops the fixed pipeline gives a Claim for the work done for it.

Every model is a stand-in, and the Sandbox and the Ecosystem are the stand-ins of the fixed
pipeline's tests.
"""

import json
from dataclasses import replace
from itertools import repeat
from pathlib import Path

import pytest
from conftest import Clock, FixtureRepository, GathersNothing, asks
from test_judge import StandInSandbox as RoundsSandbox
from test_judge import WriterAndJudge, judged
from test_pipeline import (
    ACCEPTS,
    READS,
    READS_ANOTHER,
    REJECTS,
    RENAMES,
    GoesRoundUntilTold,
    Sandboxes,
    WritesFor,
    a_pipeline,
    cap_reached,
    gather_within,
    renaming,
    writes,
)
from test_pipeline import CLAIM as ADDS_TAX

from pr_verification import verdict
from pr_verification.agent import REPEAT_LENGTH, STOPPED_LOOPING, Loop, LoopKind, Stopped
from pr_verification.budget import SHARE_SPENT, Budget, Ledger
from pr_verification.core import PullRequest, run
from pr_verification.decision import NO, ScriptedDecider
from pr_verification.judge import NO_REASON_GIVEN, judge_reproduction
from pr_verification.model import ModelFailed, ModelRequest, ScriptedModel
from pr_verification.orchestration import FixedPipeline, Gathered
from pr_verification.reading import locate
from pr_verification.report import (
    AcceptedReproduction,
    ClaimKind,
    FindingKind,
    LoopCounts,
    PullRequestPart,
    ReportWithLoops,
    ReviewFinding,
    Verdict,
)
from pr_verification.reproduction import ATTEMPT_CAP, COULD_NOT_FINISH, AttemptResult
from pr_verification.review import REVIEW_UNFINISHED
from pr_verification.structural import verify_structural_claim

EXACT_REPEAT = LoopKind.EXACT_REPEAT
STOPPED_AT = REPEAT_LENGTH + 1
"""The step at which a role that takes one step again and again is stopped."""

UNREACHABLE = "the model could not be reached"

LISTS = asks("after_list_files")
"""A step of the Structural verifier."""


def stopped_looping(role: str) -> str:
    """Why work ended when this role took one step again and again until it was stopped."""
    repeat = EXACT_REPEAT.value
    return STOPPED_LOOPING.format(role=role, first=repeat, second=repeat)


@pytest.fixture
def accounts(tmp_path: Path) -> FixtureRepository:
    """A pull request that renames a class."""
    return renaming(tmp_path / "repository")


# What is made of a role that was stopped


def test_judge_stopped_in_a_round_ends_the_work_there_with_the_round_kept_and_the_reason(
    accounts: FixtureRepository,
) -> None:
    model = WriterAndJudge(writer=repeat(writes("fixed")), judge=repeat("what?"))
    sandbox = RoundsSandbox(accounts)

    run = judged(accounts, model, sandbox, judge_step_cap=STOPPED_AT + 2)

    (only,) = run.rounds
    assert only.judgement is not None and not only.judgement.accepted
    assert only.judgement.run.stopped is Stopped.LOOP
    assert run.evidence is None
    assert run.inconclusive == stopped_looping("reproduction-judge")
    # No further round was made, though rounds were left: nothing more was asked or run.
    assert [role for role, _ in model.asked] == ["writer", *["judge"] * STOPPED_AT]
    assert len(sandbox.runs) == 4


@pytest.mark.parametrize("judge_step_cap", [0, -1])
def test_step_cap_for_the_judge_under_one_is_rejected_before_anything_is_asked_or_run(
    accounts: FixtureRepository, judge_step_cap: int
) -> None:
    model, sandbox = WriterAndJudge(writer=[]), RoundsSandbox(accounts)

    with pytest.raises(ValueError):
        judged(accounts, model, sandbox, judge_step_cap=judge_step_cap)

    assert (model.asked, sandbox.runs) == ([], [])


def test_judge_stopped_for_going_round_in_circles_accepts_nothing() -> None:
    model = ScriptedModel(["what?"] * (STOPPED_AT + 2))

    judgement = judge_reproduction(ADDS_TAX, "fixed", model=model, step_cap=STOPPED_AT + 2)

    assert judgement.run.stopped is Stopped.LOOP
    assert (judgement.accepted, judgement.reason) == (False, NO_REASON_GIVEN)
    assert len(model.requests) == STOPPED_AT


def test_structural_verifier_stopped_for_going_round_in_circles_leaves_no_evidence_and_the_reason(
    accounts: FixtureRepository,
) -> None:
    model = ScriptedModel([LISTS] * (STOPPED_AT + 2))

    verified = verify_structural_claim(
        RENAMES,
        repository=accounts.path,
        before=accounts.before,
        after=accounts.after,
        model=model,
    )

    assert verified.run is not None and verified.run.stopped is Stopped.LOOP
    assert (verified.evidence, len(model.requests)) == ((), STOPPED_AT)
    assert verified.inconclusive == stopped_looping("structural-verifier")


def test_locator_stopped_for_going_round_in_circles_gives_no_evidence(
    accounts: FixtureRepository,
) -> None:
    model = ScriptedModel([asks("list_files")] * (STOPPED_AT + 2))

    located = locate(RENAMES, repository=accounts.path, commit=accounts.before, model=model)

    assert (located.run.stopped, located.evidence) == (Stopped.LOOP, ())
    assert len(model.requests) == STOPPED_AT


def test_reviewer_stopped_for_going_round_in_circles_costs_a_finding_and_its_loop_is_not_counted(
    accounts: FixtureRepository,
) -> None:
    searches = asks("search", text="UserManager")
    model = ScriptedModel([json.dumps({"claims": [RENAMES]}), *[searches] * (STOPPED_AT + 2)])
    decider = ScriptedDecider(["structural", *[NO] * 30])
    pull_request = PullRequest(accounts.path, accounts.before, accounts.after, "Rename a class")

    report = run(pull_request, model, decider, orchestration=GathersNothing(), review=True)

    unfinished = ReviewFinding(
        REVIEW_UNFINISHED.format(failure=stopped_looping("reviewer")),
        kind=FindingKind.UNFINISHED_REVIEW,
        part=PullRequestPart.WHOLE,
    )
    assert unfinished in report.findings
    # One request listed the Claims, and the Reviewer was not asked again once it was stopped.
    assert len(model.requests) == 1 + STOPPED_AT
    # Only the work done for the Claims is counted.
    assert isinstance(report, ReportWithLoops) and report.loops == LoopCounts()


# The loops the fixed pipeline gives a Claim


def test_each_claim_is_given_the_loops_of_the_runs_made_for_it_and_none_of_work_a_limit_ended(
    accounts: FixtureRepository, tmp_path: Path
) -> None:
    model = WritesFor({RENAMES: LISTS, ADDS_TAX: READS, "Keeps": writes("broken")})
    kinds = (ClaimKind.STRUCTURAL, ClaimKind.BEHAVIOURAL, ClaimKind.BEHAVIOURAL)
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(accounts))

    by_reading, by_reproduction, untroubled = pipeline.gather_within(
        Ledger(clock=Clock()),
        model.claims,
        repository=accounts.path,
        before=accounts.before,
        after=accounts.after,
        model=model,
        kinds=kinds,
    )

    verifier, writer = "structural-verifier", "reproduction-writer"
    assert by_reading == Gathered(
        (), stopped_looping(verifier), loops=(Loop(verifier, EXACT_REPEAT, EXACT_REPEAT),)
    )
    assert by_reproduction == Gathered(
        (), stopped_looping(writer), loops=(Loop(writer, EXACT_REPEAT, EXACT_REPEAT),)
    )
    assert untroubled.loops == () and isinstance(untroubled.evidence[0], AcceptedReproduction)


class ThenUnreachable(GoesRoundUntilTold):
    """A `GoesRoundUntilTold` that cannot be reached once it has been told in an attempt after
    its first."""

    def complete(self, request: ModelRequest) -> str:
        asked = json.loads(request.data)
        if asked.get("task", {}).get("attempts") and any(
            step.get("told") for step in asked["steps"]
        ):
            raise ModelFailed(UNREACHABLE)
        return super().complete(request)


def test_run_cut_short_by_a_failing_model_or_by_a_limit_leaves_no_loop_among_its_claims(
    accounts: FixtureRepository, tmp_path: Path
) -> None:
    # The writer is told in its first attempt, which ends and shows nothing. It is told
    # again in its second, and the model fails before that run ends.
    (failed,) = a_pipeline(tmp_path / "failed", Sandboxes(accounts)).gather(
        (ADDS_TAX,),
        repository=accounts.path,
        before=accounts.before,
        after=accounts.after,
        model=ThenUnreachable([READS], "same"),
    )
    # The share of each of two Claims is as many steps as it takes for the writer to be told.
    # The work on the first ends there, at the limit.
    cut_short = WritesFor({ADDS_TAX: READS, "Keeps": writes("broken")})
    ledger = Ledger(Budget(steps=2 * REPEAT_LENGTH), clock=Clock())
    pipeline = a_pipeline(tmp_path / "limited", Sandboxes(accounts))
    limited, _ = gather_within(pipeline, ledger, accounts, cut_short)

    assert failed == Gathered(
        (),
        COULD_NOT_FINISH.format(failure=UNREACHABLE),
        loops=(Loop("reproduction-writer", EXACT_REPEAT, None),),
    )
    assert limited == Gathered((), SHARE_SPENT.format(limit="steps"))
    assert len(cut_short.tasks(ADDS_TAX)) == REPEAT_LENGTH


class ToldInEachRound:
    """A stand-in model that makes one Claim. As the Reproduction writer it takes one step
    again and again in the first round, and goes back and forth between two in every later
    one, until it is told, and then writes a Reproduction that shows a difference. As the
    Reproduction judge it gives the responses prepared, in turn, and the last of them from
    then on."""

    def __init__(self, *judge: str) -> None:
        self._judge = judge
        self._judged = 0

    def complete(self, request: ModelRequest) -> str:
        asked = json.loads(request.data)
        task = asked.get("task")
        if task is None:
            return json.dumps({"claims": [ADDS_TAX]})
        if "reproduction" in task:
            self._judged += 1
            return self._judge[min(self._judged, len(self._judge)) - 1]
        if any(step.get("told") for step in asked["steps"]):
            return writes("fixed")
        steps = [READS, READS_ANOTHER] if "rejected" in task else [READS]
        return steps[len(asked["steps"]) % len(steps)]


def test_loops_of_a_claim_are_in_the_order_its_runs_were_made_a_rounds_writer_and_then_its_judge(
    accounts: FixtureRepository, tmp_path: Path
) -> None:
    def gathered(pipeline: FixedPipeline, model: ToldInEachRound) -> Gathered:
        (given,) = pipeline.gather(
            (ADDS_TAX,),
            repository=accounts.path,
            before=accounts.before,
            after=accounts.after,
            model=model,
        )
        return given

    writer, judge = "reproduction-writer", "reproduction-judge"
    told = Loop(writer, EXACT_REPEAT, None)
    two_rounds = a_pipeline(tmp_path / "rounds", Sandboxes(accounts))
    stops_the_judge = replace(
        a_pipeline(tmp_path / "judge", Sandboxes(accounts)), judge_step_cap=STOPPED_AT + 2
    )

    accepted = gathered(two_rounds, ToldInEachRound(REJECTS, ACCEPTS))
    judge_stopped = gathered(stops_the_judge, ToldInEachRound("what?"))

    assert accepted.loops == (told, Loop(writer, LoopKind.ALTERNATION, None))
    assert isinstance(accepted.evidence[0], AcceptedReproduction)
    assert judge_stopped == Gathered(
        (), stopped_looping(judge), loops=(told, Loop(judge, EXACT_REPEAT, EXACT_REPEAT))
    )


@pytest.mark.parametrize(
    ("reproduction", "expected", "reason", "counted"),
    [
        ("fixed", Verdict.CONFIRMED, verdict.CONFIRMED, LoopCounts(1, recovered_after_told=1)),
        (
            "same",
            Verdict.INCONCLUSIVE,
            cap_reached(AttemptResult.SHOWS_NOTHING),
            LoopCounts(ATTEMPT_CAP, ended_inconclusive=ATTEMPT_CAP),
        ),
    ],
    ids=["a Claim that is decided", "a Claim left Inconclusive for another cause"],
)
def test_loop_of_a_role_that_was_told_and_went_on_is_counted_by_what_came_of_its_claim(
    accounts: FixtureRepository,
    tmp_path: Path,
    reproduction: str,
    expected: Verdict,
    reason: str,
    counted: LoopCounts,
) -> None:
    # The writer goes round until it is told, in each of its attempts, and then writes.
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(accounts))
    pull_request = PullRequest(accounts.path, accounts.before, accounts.after, "Add tax")

    report = run(pull_request, GoesRoundUntilTold([READS], reproduction), orchestration=pipeline)
    (gathered,) = pipeline.gather(
        (ADDS_TAX,),
        repository=accounts.path,
        before=accounts.before,
        after=accounts.after,
        model=GoesRoundUntilTold([READS], reproduction),
    )

    assert [(claim.verdict, claim.reason) for claim in report.claims] == [(expected, reason)]
    assert isinstance(report, ReportWithLoops) and report.loops == counted
    # One loop for each run of the writer, and none for the judge, which found nothing.
    assert gathered.loops == (Loop("reproduction-writer", EXACT_REPEAT, None),) * counted.detected
