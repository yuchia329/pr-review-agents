"""A Ledger's state taken whole, and a Ledger made again from it."""

from dataclasses import dataclass

import pytest
from conftest import Clock
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification.budget import Budget, BudgetSpent, Ledger, LedgerState
from pr_verification.model import ModelRequest
from pr_verification.report import BudgetLimit, BudgetSpend

NOTHING = BudgetSpend(seconds=0.0, dollars=0.0, steps=0)


@dataclass(frozen=True)
class ShareOut:
    claims: int


@dataclass(frozen=True)
class Record:
    spend: BudgetSpend
    claim: int | None


@dataclass(frozen=True)
class Move:
    amount: BudgetSpend
    source: int
    destination: int


Operation = ShareOut | Record | Move

amounts = st.builds(
    BudgetSpend,
    seconds=st.floats(min_value=0.0, max_value=50.0, allow_nan=False),
    dollars=st.floats(min_value=0.0, max_value=1.0, allow_nan=False),
    steps=st.integers(min_value=0, max_value=20),
)
budgets = st.builds(
    Budget,
    seconds=st.floats(min_value=1.0, max_value=200.0, allow_nan=False),
    dollars=st.floats(min_value=0.01, max_value=5.0, allow_nan=False),
    steps=st.integers(min_value=1, max_value=60),
)
claims = st.none() | st.integers(min_value=0, max_value=3)
operations = st.lists(
    st.one_of(
        st.builds(ShareOut, st.integers(min_value=1, max_value=4)),
        st.builds(Record, amounts, claims),
        st.builds(Move, amounts, st.integers(min_value=0, max_value=3), st.integers(0, 3)),
    ),
    max_size=12,
)


def apply(ledger: Ledger, operation: Operation) -> str:
    """Do one operation on `ledger`, and say what came of it: done, or what it raised."""
    try:
        match operation:
            case ShareOut(claims=count):
                ledger.share_out(count)
            case Record(spend=spend, claim=claim):
                ledger.record(spend, claim=claim)
            case Move(amount=amount, source=source, destination=destination):
                ledger.move_share(amount, source=source, destination=destination)
    except (ValueError, IndexError, BudgetSpent) as raised:
        return type(raised).__name__
    return "done"


def answers(ledger: Ledger) -> list[object]:
    """What the ledger answers about the pull request and each Claim that could have a share."""
    places: list[int | None] = [None, 0, 1, 2, 3]
    return [
        (ledger.spent(claim=place), ledger.left(claim=place), ledger.stopped_by(claim=place))
        for place in places
    ]


@settings(max_examples=200)
@given(budgets, operations, operations)
def test_restored_ledger_answers_and_goes_on_as_the_ledger_its_state_was_taken_from(
    budget: Budget, before: list[Operation], after: list[Operation]
) -> None:
    ledger = Ledger(budget)
    for operation in before:
        apply(ledger, operation)
    state = ledger.snapshot()

    restored = Ledger.restored(state)

    assert restored.snapshot() == state
    assert answers(restored) == answers(ledger)
    for operation in after:
        assert apply(restored, operation) == apply(ledger, operation)
    assert restored.snapshot() == ledger.snapshot()
    assert answers(restored) == answers(ledger)


def test_state_of_a_new_ledger_holds_its_budget_and_nothing_spent_or_shared() -> None:
    budget = Budget(seconds=60.0, dollars=0.5, steps=10)

    assert Ledger(budget).snapshot() == LedgerState(
        budget=budget, spent=NOTHING, stopped=None, shares=(), claims=()
    )


def test_state_holds_the_shares_what_each_claim_spent_and_the_limit_first_reached() -> None:
    budget = Budget(seconds=60.0, dollars=0.5, steps=10)
    ledger = Ledger(budget)
    ledger.share_out(2)
    spend = BudgetSpend(seconds=1.0, dollars=0.0, steps=10)
    ledger.record(spend, claim=1)

    state = ledger.snapshot()

    assert state.shares == (Budget(seconds=30.0, dollars=0.25, steps=5),) * 2
    assert state.claims == (NOTHING, spend)
    assert state.spent == spend
    assert state.stopped is BudgetLimit.STEPS


class TakesState:
    """A model that, while it is being asked through a ledger, tries to take its state."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self.raised: list[Exception] = []

    def complete(self, request: ModelRequest) -> str:
        try:
            self._ledger.snapshot()
        except ValueError as raised:
            self.raised.append(raised)
        return "{}"


def test_state_is_refused_while_work_is_under_way_through_the_ledger() -> None:
    ledger = Ledger(Budget())
    model = TakesState(ledger)

    ledger.model(model).complete(ModelRequest("instructions", "{}"))

    assert len(model.raised) == 1


class Advances:
    """A model that moves a controlled clock on by `seconds` while it answers."""

    def __init__(self, clock: Clock, seconds: float) -> None:
        self._clock, self._seconds = clock, seconds

    def complete(self, request: ModelRequest) -> str:
        self._clock.now += self._seconds
        return "{}"


def test_restored_ledger_times_its_work_with_the_clock_it_is_given() -> None:
    ledger = Ledger(Budget(seconds=100.0))
    ledger.record(BudgetSpend(seconds=7.0, dollars=0.0, steps=1))
    clock = Clock()
    clock.now = 1000.0

    restored = Ledger.restored(ledger.snapshot(), clock=clock)
    restored.model(Advances(clock, 5.0)).complete(ModelRequest("instructions", "{}"))

    assert restored.spent().seconds == pytest.approx(12.0)
    assert restored.spent().steps == 2


def test_restored_ledger_that_holds_shares_refuses_to_set_them_aside_again() -> None:
    ledger = Ledger(Budget())
    ledger.share_out(3)

    restored = Ledger.restored(ledger.snapshot())

    with pytest.raises(ValueError):
        restored.share_out(3)


def test_restored_ledger_with_no_shares_sets_them_aside() -> None:
    restored = Ledger.restored(Ledger(Budget(steps=9)).snapshot())

    restored.share_out(3)

    assert restored.left(claim=2).steps == 3


@pytest.mark.parametrize(
    "budget",
    [Budget(seconds=0.0), Budget(dollars=0.0), Budget(steps=0)],
    ids=["no seconds", "no dollars", "no steps"],
)
def test_state_whose_budget_has_a_limit_not_more_than_0_is_refused(budget: Budget) -> None:
    state = LedgerState(budget=budget, spent=NOTHING, stopped=None, shares=(), claims=())

    with pytest.raises(ValueError):
        Ledger.restored(state)


def test_state_with_not_as_many_claims_as_shares_is_refused() -> None:
    state = LedgerState(
        budget=Budget(), spent=NOTHING, stopped=None, shares=(Budget(), Budget()), claims=()
    )

    with pytest.raises(ValueError):
        Ledger.restored(state)
