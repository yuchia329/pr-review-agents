"""How the loop ladder was used: the count of the loops of one run, in its report, and their
totals over the cases of an evaluation.

Nothing here reaches a model: the orchestration and the entry point are stand-ins that give what
was prepared.
"""

import json
from collections.abc import Sequence
from dataclasses import asdict, fields, replace

import pytest
from conftest import FixtureRepository, GathersNothing
from hypothesis import given
from hypothesis import strategies as st
from test_core import FIXED, REGRESSED, Gathers, verify
from test_evaluation import CONFIRMED, HELD_OUT, INCONCLUSIVE, MODEL, REFUTED, SPEND, a_case

from pr_verification.agent import Loop, LoopKind
from pr_verification.core import PullRequest, count_loops
from pr_verification.evaluation import CaseRun, PartEvaluation, evaluate
from pr_verification.model import Model, ModelFailed
from pr_verification.orchestration import Gathered
from pr_verification.report import LoopCounts, Report, ReportWithLoops, Verdict

TOLD_ONCE = Loop("reproduction-writer", LoopKind.EXACT_REPEAT, None)
"""The loop of a role that was told once and was not found going round again."""

STOPPED_AGAIN = Loop("structural-verifier", LoopKind.NO_PROGRESS, LoopKind.ALTERNATION)
"""The loop of a role that was told, and stopped when something was found again."""

OUTCOMES = ("recovered_after_told", "recovered_after_replanning", "ended_inconclusive")


# Counting the loops of one run


@pytest.mark.parametrize(
    ("loop", "verdict", "outcome"),
    [
        (TOLD_ONCE, CONFIRMED, "recovered_after_told"),
        (TOLD_ONCE, REFUTED, "recovered_after_told"),
        (STOPPED_AGAIN, CONFIRMED, "recovered_after_replanning"),
        (STOPPED_AGAIN, REFUTED, "recovered_after_replanning"),
        (TOLD_ONCE, INCONCLUSIVE, "ended_inconclusive"),
        (STOPPED_AGAIN, INCONCLUSIVE, "ended_inconclusive"),
    ],
    ids=[
        "told, and Confirmed",
        "told, and Refuted",
        "stopped, and Confirmed",
        "stopped, and Refuted",
        "told, and Inconclusive",
        "stopped, and Inconclusive",
    ],
)
def test_loop_is_counted_under_the_outcome_that_the_rung_it_reached_and_its_claims_verdict_settle(
    loop: Loop, verdict: Verdict, outcome: str
) -> None:
    assert count_loops(iter([((loop,), verdict)])) == LoopCounts(detected=1, **{outcome: 1})


def test_with_no_claims_or_no_loops_every_count_is_zero() -> None:
    assert count_loops([]) == LoopCounts(0, 0, 0, 0)
    assert count_loops([((), CONFIRMED), ((), INCONCLUSIVE)]) == LoopCounts(0, 0, 0, 0)


LOOPS = st.builds(
    Loop,
    st.sampled_from(["reproduction-writer", "reproduction-judge", "structural-verifier"]),
    st.sampled_from(LoopKind),
    st.none() | st.sampled_from(LoopKind),
)


@given(found=st.lists(st.tuples(st.lists(LOOPS, max_size=4), st.sampled_from(Verdict)), max_size=6))
def test_in_any_run_every_detected_loop_is_counted_under_exactly_one_outcome(
    found: list[tuple[list[Loop], Verdict]],
) -> None:
    counted = count_loops(found)

    each = [(loop, verdict) for loops, verdict in found for loop in loops]
    decided = [loop for loop, verdict in each if verdict is not INCONCLUSIVE]
    assert counted.detected == len(each) == sum(getattr(counted, name) for name in OUTCOMES)
    assert counted.ended_inconclusive == len(each) - len(decided)
    assert counted.recovered_after_told == sum(loop.second is None for loop in decided)
    assert counted.recovered_after_replanning == sum(loop.second is not None for loop in decided)
    # Every loop moves exactly one outcome, by one, whatever else was found.
    for place, (loops, verdict) in enumerate(found):
        for without in range(len(loops)):
            rest = [*loops[:without], *loops[without + 1 :]]
            fewer = count_loops([*found[:place], (rest, verdict), *found[place + 1 :]])
            moved = [getattr(counted, name) - getattr(fewer, name) for name in OUTCOMES]
            assert (counted.detected - fewer.detected, sorted(moved)) == (1, [0, 0, 1])


# The report of one run

CLAIMS = ("Adds tax", "Keeps prices", "Rounds", "Sorts the lines")
GATHERED = (
    Gathered((FIXED,), loops=(TOLD_ONCE,)),
    Gathered((REGRESSED,), loops=(STOPPED_AGAIN, TOLD_ONCE)),
    Gathered((), "The share of the Claim was spent.", loops=(TOLD_ONCE, STOPPED_AGAIN)),
    Gathered((FIXED,)),
)
"""What an orchestration gathered for each of `CLAIMS`: Confirmed after a telling, Refuted by
work dispatched after a stop, Inconclusive, and Confirmed with no loop found."""


def test_report_records_the_loops_of_the_work_for_each_claim_under_what_came_of_the_claim(
    fixture_repository: FixtureRepository,
) -> None:
    report = verify(fixture_repository, Gathers(*GATHERED), *CLAIMS)

    counted = LoopCounts(
        detected=5, recovered_after_told=2, recovered_after_replanning=1, ended_inconclusive=2
    )
    assert isinstance(report, ReportWithLoops)
    assert report.loops == counted
    assert json.loads(report.to_json())["loops"] == asdict(counted)
    assert set(asdict(counted)) == {"detected", *OUTCOMES}
    # Nothing of the loops is read to compute a Verdict or the Gate.
    without = (replace(gathered, loops=()) for gathered in GATHERED)
    unwatched = verify(fixture_repository, Gathers(*without), *CLAIMS)
    assert (report.claims, report.gate) == (unwatched.claims, unwatched.gate)


def test_report_of_a_run_in_which_no_loop_was_found_records_every_count_as_zero(
    fixture_repository: FixtureRepository,
) -> None:
    no_claims = verify(fixture_repository, GathersNothing())
    no_loops = verify(fixture_repository, Gathers(Gathered((FIXED,))), "Adds tax")

    for report in (no_claims, no_loops):
        assert isinstance(report, ReportWithLoops)
        assert report.loops == LoopCounts(0, 0, 0, 0)
        assert json.loads(report.to_json())["loops"] == dict.fromkeys(("detected", *OUTCOMES), 0)


# The totals of an evaluation


class Reports:
    """A stand-in for the core's entry point that gives the reports prepared, in order, or
    raises what was prepared in the place of one."""

    def __init__(self, prepared: Sequence[Report | Exception]) -> None:
        self._prepared = iter(prepared)

    def __call__(self, pull_request: PullRequest, model: Model) -> Report:
        answer = next(self._prepared)
        if isinstance(answer, Exception):
            raise answer
        return answer


def says(loops: LoopCounts) -> ReportWithLoops:
    """A report of a run that makes no Claims and says this of its loops."""
    return ReportWithLoops("before", "after", (), (), SPEND, loops=loops)


def test_harness_records_the_loops_each_report_says_and_totals_them_over_the_cases_of_a_part() -> (
    None
):
    cases = [a_case("tuned-on"), a_case("kept-back", HELD_OUT), a_case("tuned-on-too")]
    first, second, third = LoopCounts(3, 1, 1, 1), LoopCounts(2, 2, 0, 0), LoopCounts(4, 0, 3, 1)
    kept_back = LoopCounts(1, 0, 0, 1)
    says_nothing = Report("before", "after", (), (), SPEND)
    unreachable = ModelFailed("the model could not be reached")
    system = Reports(
        [says(first), says(kept_back), says_nothing, says(second), unreachable, says(third)]
    )

    evaluation = evaluate(cases, MODEL, repetitions=2, entry_point=system)

    development, held_out = evaluation.development, evaluation.held_out
    assert [run.loops for run in development.runs] == [first, None, second, third]
    assert [run.loops for run in held_out.runs] == [kept_back, None]
    assert development.loops == LoopCounts(9, 3, 4, 2)
    assert held_out.loops == kept_back
    printed = json.loads(evaluation.to_json())
    assert printed["development"]["loops"] == asdict(development.loops)
    assert printed["held_out"]["loops"] == asdict(kept_back)


COUNTS = st.builds(
    lambda told, replanned, inconclusive: LoopCounts(
        told + replanned + inconclusive, told, replanned, inconclusive
    ),
    *[st.integers(0, 20)] * 3,
)
"""Counts as `count_loops` gives them: every loop under one outcome."""


@given(said=st.lists(st.none() | COUNTS, max_size=8))
def test_loops_of_a_part_are_the_sums_over_its_runs_that_say_and_keep_each_loop_under_one_outcome(
    said: list[LoopCounts | None],
) -> None:
    runs = tuple(
        CaseRun(f"case {number}", 1, CONFIRMED, (), SPEND, None, loops)
        for number, loops in enumerate(said)
    )

    total = PartEvaluation(runs).loops

    for field in fields(LoopCounts):
        assert getattr(total, field.name) == sum(
            getattr(loops, field.name) for loops in said if loops is not None
        )
    assert total.detected == sum(getattr(total, name) for name in OUTCOMES)
