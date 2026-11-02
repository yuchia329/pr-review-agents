"""A ledger made to hold a state taken from another, while it goes on being the same ledger."""

import pytest
from conftest import Clock
from hypothesis import given, settings
from test_ledger_state import Advances, Operation, answers, apply, budgets, operations

from pr_verification.budget import Budget, Ledger
from pr_verification.model import ModelRequest
from pr_verification.report import BudgetSpend


@settings(max_examples=200)
@given(budgets, operations, operations, operations)
def test_ledger_that_adopts_a_state_answers_and_goes_on_as_one_restored_from_it(
    budget: Budget, own: list[Operation], elsewhere: list[Operation], after: list[Operation]
) -> None:
    ledger = Ledger(budget)
    for operation in own:
        apply(ledger, operation)
    other = Ledger.restored(ledger.snapshot())
    for operation in elsewhere:
        apply(other, operation)
    state = other.snapshot()
    restored = Ledger.restored(state)

    ledger.adopt(state)

    assert ledger.snapshot() == state
    assert answers(ledger) == answers(restored)
    for operation in after:
        assert apply(ledger, operation) == apply(restored, operation)
    assert ledger.snapshot() == restored.snapshot()


def test_ledger_that_adopts_a_state_goes_on_timing_its_work_with_its_own_clock() -> None:
    clock = Clock()
    clock.now = 500.0
    ledger = Ledger(Budget(seconds=100.0), clock=clock)
    other = Ledger.restored(ledger.snapshot())
    other.record(BudgetSpend(seconds=7.0, dollars=0.0, steps=1))

    ledger.adopt(other.snapshot())
    ledger.model(Advances(clock, 5.0)).complete(ModelRequest("instructions", "{}"))

    assert ledger.spent().seconds == pytest.approx(12.0)
    assert ledger.spent().steps == 2


class AdoptsWhileAsked:
    """A model that, while it is being asked through a ledger, tries to make it adopt a state."""

    def __init__(self, ledger: Ledger) -> None:
        self._ledger = ledger
        self._state = Ledger.restored(ledger.snapshot()).snapshot()
        self.raised: list[Exception] = []

    def complete(self, request: ModelRequest) -> str:
        try:
            self._ledger.adopt(self._state)
        except ValueError as raised:
            self.raised.append(raised)
        return "{}"


def test_state_is_not_adopted_while_work_is_under_way_through_the_ledger() -> None:
    ledger = Ledger(Budget())
    model = AdoptsWhileAsked(ledger)

    ledger.model(model).complete(ModelRequest("instructions", "{}"))

    assert len(model.raised) == 1
    assert ledger.spent().steps == 1


def test_state_of_another_budget_is_refused_and_changes_nothing() -> None:
    ledger = Ledger(Budget(steps=10))
    ledger.record(BudgetSpend(seconds=1.0, dollars=0.0, steps=2))
    before = ledger.snapshot()
    other = Ledger(Budget(steps=11)).snapshot()

    with pytest.raises(ValueError):
        ledger.adopt(other)

    assert ledger.snapshot() == before


def test_state_no_ledger_could_hold_is_refused_and_changes_nothing() -> None:
    ledger = Ledger(Budget())
    ledger.share_out(2)
    before = ledger.snapshot()
    broken = ledger.snapshot().__class__(
        budget=before.budget,
        spent=before.spent,
        stopped=None,
        shares=before.shares,
        claims=before.claims[:1],
    )

    with pytest.raises(ValueError):
        ledger.adopt(broken)

    assert ledger.snapshot() == before
