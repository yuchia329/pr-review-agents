"""Loops handed to the supervisor: a Claim whose role was stopped for going round in circles goes
back to the supervisor, which may try again or give the Claim up, and a supervisor that repeats
itself is stopped.

The supervising model is a scripted stand-in, and the roles' model is one that goes round in
circles where a test says. The Sandbox and the Ecosystem are the stand-ins of the fixed
pipeline's tests.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from conftest import Clock, FixtureRepository, asks, commit_snapshots
from hypothesis import given
from hypothesis import strategies as st
from test_pipeline import (
    NAMES_BOTH,
    NOT_FOUND,
    READS,
    READS_ANOTHER,
    RENAMES,
    Discovery,
    Fault,
    Sandboxes,
    WritesFor,
    a_pipeline,
    renaming,
    writes,
)
from test_supervisor import (
    CONFIRMS,
    FINISH,
    REFUTES,
    SHOWS_NOTHING,
    FailsAfter,
    decided,
    dispatch,
    gather_within,
    left_of_each,
    move,
    requests_of,
    result,
    verify,
)

from pr_verification import verdict
from pr_verification.agent import (
    ALTERNATION_LENGTH,
    REPEAT_LENGTH,
    STOPPED_LOOPING,
    Loop,
    LoopKind,
)
from pr_verification.budget import BUDGET_SPENT, SHARE_SPENT, Budget, Ledger
from pr_verification.ecosystem import Unsupported
from pr_verification.model import Model, ModelRequest, ScriptedModel
from pr_verification.orchestration import Gathered
from pr_verification.report import (
    BudgetLimit,
    LoopCounts,
    Report,
    ReportWithLoops,
    Verdict,
)
from pr_verification.reproduction import ATTEMPT_CAP
from pr_verification.supervisor import (
    DISPATCH_CAP,
    DISPATCH_REFUSED,
    SUPERVISOR_REPEATED,
    FailureSummary,
    ResultSummary,
    Strategy,
    Supervisor,
    summarise,
)

WRITER = "reproduction-writer"
EXACT_REPEAT, ALTERNATION = LoopKind.EXACT_REPEAT, LoopKind.ALTERNATION

REPEATS, ALTERNATES = [READS], [READS, READS_ANOTHER]
"""Steps a role takes round and round: it is told at an exact repeat or at an alternation, and
is stopped at the step after for the same."""

REPEATS_THEN_ALTERNATES = [*[READS] * REPEAT_LENGTH, READS_ANOTHER, READS, READS_ANOTHER]
"""Steps a role is told at for an exact repeat, as with `REPEATS`, and stopped at for an
alternation: its failure differs from that of `REPEATS` only in what was found again."""

STEPS_UNTIL_STOPPED = {EXACT_REPEAT: REPEAT_LENGTH + 1, ALTERNATION: ALTERNATION_LENGTH + 1}
"""How many steps a run of each takes before it is stopped."""


Runs = Mapping[str, Sequence[str | list[str] | Exception]]
"""What a role does in each of its runs for each Claim, as `InTurn` takes it."""


class InTurn(WritesFor):
    """A `WritesFor` in which a role does something else in each of its runs for a Claim: what
    was prepared for the Claim, in turn, and the last of it in every run after. A list is steps
    it takes round and round whatever it is told, a string is the response it gives at once, and
    an exception is raised. The Reproduction judge accepts what it is shown."""

    def __init__(self, runs: Runs) -> None:
        super().__init__({claim: "" for claim in runs})
        self._runs = runs
        self._started = {claim: 0 for claim in runs}

    def complete(self, request: ModelRequest) -> str:
        asked = json.loads(request.data)
        task = asked.get("task")
        if task is None or "reproduction" in task:
            return super().complete(request)
        self.requests.append(request)
        claim, taken = task["claim"], len(asked["steps"])
        if taken == 0:
            self._started[claim] += 1
        prepared = self._runs[claim]
        doing = prepared[min(self._started[claim], len(prepared)) - 1]
        if isinstance(doing, Exception):
            raise doing
        return doing if isinstance(doing, str) else doing[taken % len(doing)]


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A repository with nothing of any Ecosystem in it, and a pull request that changes it."""
    path = tmp_path_factory.mktemp("shop") / "repository"
    before, after = commit_snapshots(
        path,
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net\n"},
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net and tax\n"},
    )
    return FixtureRepository(path=path, before=before, after=after)


def stopped(kind: LoopKind = EXACT_REPEAT) -> tuple[Verdict, str]:
    """The Verdict and the reason of a Claim whose writer was last stopped for this."""
    why = STOPPED_LOOPING.format(role=WRITER, first=kind.value, second=kind.value)
    return Verdict.INCONCLUSIVE, why


def repeated_on(kind: LoopKind = EXACT_REPEAT) -> tuple[Verdict, str]:
    """The Verdict and the reason of a Claim the supervisor was stopped for repeating itself
    on, when its writer was stopped for this in the dispatch that was the repeat."""
    return Verdict.INCONCLUSIVE, SUPERVISOR_REPEATED.format(failure=stopped(kind)[1])


def failed(place: int, kind: LoopKind = EXACT_REPEAT) -> dict[str, object]:
    """The summary of a dispatch whose writer was stopped for this, as a request shows it."""
    failure = {"role": WRITER, "first": kind.value, "second": kind.value}
    summary = result(place, 0, Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)
    return summary | {"failure": failure}


def loops_of(report: Report) -> LoopCounts:
    """What the report of a run records of its loops."""
    assert isinstance(report, ReportWithLoops)
    return report.loops


# A Claim handed back


LOOPS = st.builds(
    Loop,
    st.sampled_from([WRITER, "reproduction-judge"]),
    st.sampled_from(LoopKind),
    st.none() | st.sampled_from(LoopKind),
)


@given(loops=st.lists(LOOPS, max_size=4))
def test_summary_of_any_dispatch_is_a_failure_summary_only_when_its_last_loop_ended_with_a_stop(
    loops: list[Loop],
) -> None:
    gathered = Gathered((), "TEXT-OF-A-FAILURE", loops=tuple(loops))

    summary = summarise(2, Strategy.REPRODUCTION, None, gathered)

    computed = (2, Strategy.REPRODUCTION, 0, Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)
    if loops and loops[-1].second is not None:
        assert summary == FailureSummary(*computed, failure=loops[-1])
    else:
        assert summary == ResultSummary(*computed)


def test_claim_whose_role_was_stopped_goes_back_to_the_supervisor_with_an_account_of_what_failed(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = InTurn({"Adds tax": [REPEATS], "Keeps": [writes("broken")]})
    supervising = ScriptedModel([dispatch(0, 1), FINISH])

    verify(shop, roles, supervising, tmp_path)

    _, after = requests_of(supervising)
    assert after["results"] == [failed(0), result(1, 1, *REFUTES)]
    # The Claim has had its attempt, and its strategy is still open.
    handed_back = after["claims"][0]
    assert (handed_back["claim"], handed_back["attempts"]) == (0, 1)
    assert handed_back["strategies"] == ["reproduction"]
    # The account holds nothing the role read or wrote.
    assert "total.txt" not in json.dumps(after["results"])


def test_supervisor_that_does_not_dispatch_the_claim_again_gives_it_up_and_it_is_inconclusive(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = InTurn({"Adds tax": [REPEATS], "Keeps": [writes("broken")]})
    supervising = ScriptedModel([dispatch(0, 1), FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    # The finish was not refused: asked a third time, the stand-in would raise.
    assert len(supervising.requests) == 2
    assert decided(report) == [stopped(), REFUTES]
    assert len(roles.tasks("Adds tax")) == STEPS_UNTIL_STOPPED[EXACT_REPEAT]
    assert loops_of(report) == LoopCounts(detected=1, ended_inconclusive=1)


def test_claim_dispatched_again_is_worked_on_from_nothing_and_decided_by_what_that_work_gathered(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = InTurn({"Adds tax": [REPEATS, writes("fixed")]})
    supervising = ScriptedModel([dispatch(0), dispatch(0), FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    _, handed_back, tried_again = requests_of(supervising)
    assert handed_back["results"] == [failed(0)]
    assert tried_again["results"] == [result(0, 1, *CONFIRMS)]
    assert tried_again["claims"][0]["attempts"] == 2
    # The writer dispatched again is shown what the first was, and nothing of the stopped run.
    asked = [json.loads(request.data) for request in roles.requests[1:]]
    again = asked[STEPS_UNTIL_STOPPED[EXACT_REPEAT]]
    assert again == asked[0] and again["steps"] == []
    assert decided(report) == [CONFIRMS]
    assert loops_of(report) == LoopCounts(detected=1, recovered_after_replanning=1)


# The supervisor's own repeats

THREE: Runs = {"Adds tax": [REPEATS], "Keeps": [writes("broken")], "Rounds": [writes("same")]}
"""Three Claims, the first of which has a writer that is stopped however often it is run."""

REPEATS_ITSELF: Mapping[str, tuple[Runs, list[str], list[tuple[Verdict, str]]]] = {
    "at the next step": (
        THREE,
        [dispatch(0), dispatch(0, 1)],
        [repeated_on(), REFUTES, SHOWS_NOTHING],
    ),
    "after other work, a move, and a step that was refused": (
        THREE,
        [dispatch(0), dispatch(1), move(0, 1, steps=1), "I would rather not.", dispatch(0)],
        [repeated_on(), REFUTES, SHOWS_NOTHING],
    ),
    "on two Claims in one step": (
        {"Adds tax": [REPEATS], "Keeps": [ALTERNATES], "Rounds": [writes("same")]},
        [dispatch(0, 1), dispatch(1, 0)],
        [repeated_on(), repeated_on(ALTERNATION), SHOWS_NOTHING],
    ),
    "on the one Claim there is": (
        {"Adds tax": [REPEATS]},
        [dispatch(0), dispatch(0)],
        [repeated_on()],
    ),
}


@pytest.mark.parametrize("case", REPEATS_ITSELF)
def test_supervisor_that_repeats_itself_is_stopped_and_code_gives_each_claim_it_left_its_attempt(
    shop: FixtureRepository, tmp_path: Path, case: str
) -> None:
    runs, said, expected = REPEATS_ITSELF[case]
    roles, sandboxes = InTurn(runs), Sandboxes(shop)
    supervising = ScriptedModel(said)

    report = verify(shop, roles, supervising, tmp_path, sandboxes=sandboxes)

    # The supervisor was asked nothing more: the stand-in would raise.
    assert len(supervising.requests) == len(said)
    # What was gathered by then is kept, the Claim repeated on is given the reason, and code
    # gave the Claim left untried its attempt.
    assert decided(report) == expected
    # No further dispatch was run for a Claim repeated on.
    took = STEPS_UNTIL_STOPPED[EXACT_REPEAT]
    assert len(roles.tasks("Adds tax")) == 2 * took
    stopped_twice = 2 * expected.count(repeated_on()) + 2 * expected.count(repeated_on(ALTERNATION))
    assert loops_of(report) == LoopCounts(detected=stopped_twice, ended_inconclusive=stopped_twice)
    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)


NOT_A_REPEAT: Mapping[str, tuple[Runs, list[str], LoopCounts]] = {
    "the one before it failed in another way, though an earlier one failed in the same": (
        {"Adds tax": [REPEATS, REPEATS_THEN_ALTERNATES, REPEATS, writes("fixed")]},
        [dispatch(0), dispatch(0), dispatch(0), dispatch(0)],
        LoopCounts(detected=3, recovered_after_replanning=3),
    ),
    "the dispatch before it ended with no failure": (
        # The second dispatch is every attempt the writer may make, each of which shows nothing.
        {"Adds tax": [REPEATS, *[writes("same")] * ATTEMPT_CAP, REPEATS]},
        [dispatch(0), dispatch(0), dispatch(0)],
        LoopCounts(detected=2, ended_inconclusive=2),
    ),
    "the same failure was of another Claim": (
        {"Adds tax": [REPEATS], "Keeps": [REPEATS]},
        [dispatch(0), dispatch(1)],
        LoopCounts(detected=2, ended_inconclusive=2),
    ),
}


@pytest.mark.parametrize("case", NOT_A_REPEAT)
def test_dispatch_is_no_repeat_unless_the_one_before_it_for_its_claim_failed_in_the_same_way(
    shop: FixtureRepository, tmp_path: Path, case: str
) -> None:
    runs, said, counted = NOT_A_REPEAT[case]
    roles, supervising = InTurn(runs), ScriptedModel([*said, FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    # The supervisor was asked again after every dispatch, and its finish ended the run.
    assert len(supervising.requests) == len(said) + 1
    assert all(request["refused"] is None for request in requests_of(supervising))
    assert loops_of(report) == counted
    decided_at_last = counted.recovered_after_replanning > 0
    assert decided(report) == [CONFIRMS if decided_at_last else stopped()] * len(runs)


def test_dispatch_that_code_makes_for_a_claim_left_untried_is_its_first_and_so_never_a_repeat(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = InTurn({"Adds tax": [REPEATS], "Keeps": [REPEATS], "Rounds": [writes("same")]})
    supervising: Model = FailsAfter()

    report = verify(shop, roles, supervising, tmp_path)

    assert decided(report) == [stopped(), stopped(), SHOWS_NOTHING]
    assert loops_of(report) == LoopCounts(detected=2, ended_inconclusive=2)


def test_fault_in_a_dispatch_of_the_step_of_a_repeat_is_raised_and_not_lost_to_the_stop(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, sandboxes = InTurn({"Adds tax": [REPEATS], "Keeps": [Fault()]}), Sandboxes(shop)
    supervising = ScriptedModel([dispatch(0), dispatch(0, 1)])

    with pytest.raises(Fault):
        verify(shop, roles, supervising, tmp_path, sandboxes=sandboxes)

    # The other dispatch of the step had ended, with its role stopped the same way again.
    assert len(roles.tasks("Adds tax")) == 2 * STEPS_UNTIL_STOPPED[EXACT_REPEAT]
    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)


def test_repeat_in_the_step_that_spends_the_budget_is_reported_and_no_more_work_is_started(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    took = STEPS_UNTIL_STOPPED[EXACT_REPEAT]
    roles = InTurn({"Adds tax": [REPEATS], "Keeps": [writes("broken")]})
    # Each of two Claims has half the steps, and the first spends its half in two dispatches.
    # The supervisor's own steps spend the rest, so the dispatch that is a repeat is also the
    # one that spends the Budget.
    wasted = ["I would rather not."] * (2 * took - 2)
    supervising = ScriptedModel([*wasted, dispatch(0), dispatch(0)])
    ledger = Ledger(Budget(steps=4 * took), clock=Clock())
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising)

    gathered = gather_within(orchestration, ledger, shop, roles)

    assert (ledger.spent().steps, ledger.stopped_by()) == (4 * took, BudgetLimit.STEPS)
    assert gathered == [
        Gathered((), repeated_on()[1], loops=(Loop(WRITER, EXACT_REPEAT, EXACT_REPEAT),) * 2),
        Gathered((), BUDGET_SPENT.format(limit="steps")),
    ]
    assert roles.tasks("Keeps") == []


def test_claim_no_reproduction_can_be_run_for_keeps_its_own_reason_when_the_supervisor_is_stopped(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    lists = asks("after_list_files")
    roles = InTurn({"Adds tax": [writes("fixed")], RENAMES: [[lists]], "Moves it": [NAMES_BOTH]})
    supervising = ScriptedModel([dispatch(1, reading=[1]), dispatch(1, reading=[1])])

    report = verify(
        accounts,
        roles,
        supervising,
        tmp_path / "state",
        "behavioural",
        "structural",
        "structural",
        discover=Discovery(Unsupported(NOT_FOUND)),
    )

    repeat = EXACT_REPEAT.value
    verifier_stopped = STOPPED_LOOPING.format(
        role="structural-verifier", first=repeat, second=repeat
    )
    assert len(supervising.requests) == 2
    # Code gives the Claim left untried its attempt, and none to the one that is stranded.
    assert decided(report) == [
        (Verdict.INCONCLUSIVE, NOT_FOUND),
        (Verdict.INCONCLUSIVE, SUPERVISOR_REPEATED.format(failure=verifier_stopped)),
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
    ]
    assert roles.tasks("Adds tax") == []


# The caps, kept throughout


def test_no_cap_is_moved_for_a_claim_dispatched_again_and_its_share_is_not_given_back(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = InTurn({"Adds tax": [REPEATS, ALTERNATES, REPEATS, ALTERNATES]})
    supervising = ScriptedModel([dispatch(0)] * 3)
    ledger = Ledger(Budget(steps=300), clock=Clock())
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising, step_cap=3)

    gathered = gather_within(orchestration, ledger, shop, roles)

    # Asked a fourth time, the stand-in would raise: the supervisor's step cap held.
    requests = requests_of(supervising)
    assert [request["supervisor_steps_left"] for request in requests] == [2, 1, 0]
    # Each run was stopped where any other would be, and spent from what its Claim had left.
    took = [STEPS_UNTIL_STOPPED[kind] for kind in (EXACT_REPEAT, ALTERNATION, EXACT_REPEAT)]
    left = [left_of_each(request)[0]["steps"] for request in requests]
    assert [before - after for before, after in zip(left, left[1:], strict=False)] == took[:2]
    assert ledger.spent(claim=0).steps == len(roles.tasks("Adds tax")) == sum(took)
    # The Claim is given what its last dispatch came to, and the loops of every one, in order.
    assert gathered == [
        Gathered(
            (),
            stopped()[1],
            loops=tuple(
                Loop(WRITER, kind, kind) for kind in (EXACT_REPEAT, ALTERNATION, EXACT_REPEAT)
            ),
        )
    ]


def test_claim_dispatched_again_with_its_share_all_but_spent_is_ended_by_the_share_as_any_other(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    took = STEPS_UNTIL_STOPPED[EXACT_REPEAT]
    roles = InTurn({"Adds tax": [REPEATS], "Keeps": [writes("broken")]})
    # Each of two Claims has 150 steps. The first is left two, too few for its role to be
    # stopped again.
    supervising = ScriptedModel(
        [dispatch(0), move(0, 1, steps=150 - took - 2), dispatch(0), dispatch(1), FINISH]
    )
    ledger = Ledger(Budget(steps=300), clock=Clock())
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising)

    first, second = gather_within(orchestration, ledger, shop, roles)

    requests = requests_of(supervising)
    assert len(requests) == 5
    assert left_of_each(requests[2])[0]["steps"] == 2
    # Work that the share ended left no record, so its summary is of no failure.
    assert requests[3]["results"] == [
        result(0, 0, Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)
    ]
    assert len(roles.tasks("Adds tax")) == took + 2
    assert first == Gathered(
        (),
        SHARE_SPENT.format(limit="steps"),
        loops=(Loop(WRITER, EXACT_REPEAT, EXACT_REPEAT),),
    )
    assert len(second.evidence) == 1


def test_claim_dispatched_again_is_one_of_no_more_dispatches_than_a_step_may_make(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    others = {f"Claim {number}": [writes("fixed")] for number in range(DISPATCH_CAP)}
    roles = InTurn({"Adds tax": [REPEATS, writes("fixed")], **others})
    every = range(DISPATCH_CAP + 1)
    supervising = ScriptedModel(
        [dispatch(0), dispatch(*every), dispatch(*every[:-1]), dispatch(every[-1]), FINISH]
    )

    report = verify(shop, roles, supervising, tmp_path)

    requests = requests_of(supervising)
    assert [request["refused"] for request in requests] == [
        None,
        None,
        DISPATCH_REFUSED.format(cap=DISPATCH_CAP),
        None,
        None,
    ]
    assert requests[1]["results"] == [failed(0)]
    assert len(requests[3]["results"]) == DISPATCH_CAP
    assert decided(report) == [CONFIRMS] * (DISPATCH_CAP + 1)
    assert loops_of(report) == LoopCounts(detected=1, recovered_after_replanning=1)
