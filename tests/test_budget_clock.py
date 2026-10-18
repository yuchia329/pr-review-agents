"""The Budget ledger in seconds: on the clock for the whole pull request, in seconds of work for
each Claim (ADR 0007).

Work done at the same time is made with stand-in models that ask the ledger for other work
while their own request is under way, or with threads that wait for each other. Time passes
only on a controlled clock.
"""

import threading
from collections.abc import Callable
from dataclasses import replace

import pytest
from conftest import Clock
from hypothesis import given
from hypothesis import strategies as st
from test_budget import (
    LIMITS,
    REQUEST,
    LocalModel,
    PricedAt,
    TimedSandbox,
    everything_recorded,
    run_in,
)

from pr_verification.budget import Budget, BudgetSpent, Ledger
from pr_verification.model import ModelFailed, ModelRequest
from pr_verification.report import BudgetLimit, BudgetSpend

ROOMY = Budget(seconds=1000.0, dollars=100.0, steps=100)


class AroundOther:
    """A stand-in for a model whose request takes `before` seconds on the clock, then waits
    while `other` is done, then takes `after` seconds more. What the ledger had recorded for
    the pull request just after `other` was done is kept in `seen`."""

    def __init__(
        self,
        clock: Clock,
        ledger: Ledger,
        other: Callable[[], object],
        *,
        before: float,
        after: float,
    ) -> None:
        self._clock, self._ledger, self._other = clock, ledger, other
        self._before, self._after = before, after
        self.seen: BudgetSpend | None = None

    def complete(self, request: ModelRequest) -> str:
        self._clock.now += self._before
        try:
            self._other()
        except ModelFailed:
            pass
        self.seen = self._ledger.spent()
        self._clock.now += self._after
        return "a response"


def seconds(ledger: Ledger, claims: int) -> list[float]:
    return [spent.seconds for spent in everything_recorded(ledger, claims)]


# Work done at the same time


def test_work_done_at_the_same_time_counts_once_for_the_pull_request_and_in_full_for_a_claim() -> (
    None
):
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    inner = ledger.model(LocalModel(clock, seconds=3.0), claim=1)
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=2.0, after=5.0)

    # Work for no Claim from 0 to 10 seconds, and inside it work for Claim 1 from 2 to 5.
    ledger.model(outer).complete(REQUEST)

    assert outer.seen is not None and outer.seen.seconds == pytest.approx(5.0)
    assert seconds(ledger, 2) == pytest.approx([10.0, 0.0, 3.0])


def test_work_for_two_claims_at_the_same_time_counts_in_full_against_each_share() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    inner = ledger.model(LocalModel(clock, seconds=6.0), claim=1)
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=1.0, after=1.0)

    ledger.model(outer, claim=0).complete(REQUEST)

    assert seconds(ledger, 2) == pytest.approx([8.0, 8.0, 6.0])


def test_dollars_and_steps_of_work_done_at_the_same_time_are_summed_as_ever() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    inner = ledger.model(PricedAt(0.25, clock, seconds=3.0), claim=1)
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=2.0, after=2.0)

    ledger.model(outer, claim=0).complete(REQUEST)

    spent = ledger.spent()
    assert spent.dollars == pytest.approx(0.25)
    assert spent.steps == 2
    assert ledger.spent(claim=0) == BudgetSpend(seconds=7.0, dollars=0.0, steps=1)
    assert ledger.spent(claim=1) == BudgetSpend(seconds=3.0, dollars=0.25, steps=1)


def test_threads_at_work_at_the_same_time_count_once_for_the_pull_request() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    both_started, time_passed = threading.Barrier(2), threading.Barrier(2)

    class Together:
        def complete(self, request: ModelRequest) -> str:
            # One of the two moves the clock on once both requests are under way.
            if both_started.wait(timeout=10) == 0:
                clock.now += 4.0
            time_passed.wait(timeout=10)
            return "a response"

    threads = [
        threading.Thread(target=ledger.model(Together(), claim=claim).complete, args=(REQUEST,))
        for claim in range(2)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert seconds(ledger, 2) == pytest.approx([4.0, 4.0, 4.0])
    assert ledger.spent().steps == 2


def test_work_that_raises_is_under_way_until_it_raises() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(1)
    failing = ledger.model(LocalModel(clock, seconds=3.0, answer=ModelFailed("no")), claim=0)
    outer = AroundOther(clock, ledger, lambda: failing.complete(REQUEST), before=1.0, after=2.0)

    ledger.model(outer).complete(REQUEST)

    assert seconds(ledger, 1) == pytest.approx([6.0, 3.0])


# Time that counts, and time that does not


def test_time_in_which_no_work_was_under_way_is_not_counted() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    model = ledger.model(LocalModel(clock, seconds=2.0))

    model.complete(REQUEST)
    clock.now += 50.0
    model.complete(REQUEST)

    assert ledger.spent().seconds == pytest.approx(4.0)


def test_seconds_given_to_record_count_in_full_though_work_is_under_way() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(1)

    def record_three_seconds() -> None:
        ledger.record(BudgetSpend(seconds=3.0, dollars=0.0, steps=0), claim=0)

    outer = AroundOther(clock, ledger, record_three_seconds, before=2.0, after=2.0)
    ledger.model(outer).complete(REQUEST)

    assert seconds(ledger, 1) == pytest.approx([7.0, 3.0])


def test_work_refused_once_the_budget_is_spent_adds_no_seconds() -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=5.0), clock=clock)
    ledger.model(LocalModel(clock, seconds=5.0)).complete(REQUEST)
    refused = LocalModel(clock, seconds=7.0)

    with pytest.raises(BudgetSpent):
        ledger.model(refused).complete(REQUEST)

    assert refused.requests == []
    assert ledger.spent().seconds == pytest.approx(5.0)


@given(
    st.lists(
        st.tuples(
            st.sampled_from([None, 0, 1, 2]),
            st.floats(min_value=0.0, max_value=50.0),
            st.floats(min_value=0.0, max_value=50.0),
        ),
        max_size=20,
    )
)
def test_for_any_work_done_one_piece_at_a_time_the_pull_request_has_the_sum_of_its_parts(
    work: list[tuple[int | None, float, float]],
) -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=1e9, dollars=1e9, steps=10**6), clock=clock)
    ledger.share_out(3)

    for claim, taking, idle in work:
        ledger.model(LocalModel(clock, seconds=taking), claim=claim).complete(REQUEST)
        clock.now += idle

    recorded = everything_recorded(ledger, 3)
    assert recorded[0].seconds == pytest.approx(sum(taking for _, taking, _ in work))
    for claim in range(3):
        for_claim = [taking for of, taking, _ in work if of == claim]
        assert recorded[1 + claim].seconds == pytest.approx(sum(for_claim))


@given(
    st.lists(st.floats(min_value=0.0, max_value=50.0), min_size=1, max_size=6),
    st.lists(st.sampled_from([None, 0, 1]), min_size=6, max_size=6),
)
def test_for_any_work_one_inside_another_the_pull_request_has_the_longest_and_no_more(
    takes: list[float], claims: list[int | None]
) -> None:
    # Each piece of work takes `takes[i]` seconds before and after the one inside it.
    clock = Clock()
    ledger = Ledger(Budget(seconds=1e9, dollars=1e9, steps=10**6), clock=clock)
    ledger.share_out(2)

    def piece(depth: int) -> Callable[[], object]:
        if depth == len(takes):
            return lambda: None
        model = AroundOther(clock, ledger, piece(depth + 1), before=takes[depth], after=0.0)
        through = ledger.model(model, claim=claims[depth])
        return lambda: through.complete(REQUEST)

    piece(0)()

    recorded = everything_recorded(ledger, 2)
    outermost = sum(takes)
    assert recorded[0].seconds == pytest.approx(outermost)
    work = sum(sum(takes[depth:]) for depth in range(len(takes)))
    assert recorded[0].seconds <= work + 1e-6
    claimed = [
        sum(sum(takes[depth:]) for depth in range(len(takes)) if claims[depth] == claim)
        for claim in (0, 1)
    ]
    assert [part.seconds for part in recorded[1:]] == pytest.approx(claimed)


# The limits


def test_budget_time_limit_is_held_against_seconds_on_the_clock() -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=10.0), clock=clock)
    inner = ledger.model(LocalModel(clock, seconds=6.0))
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=1.0, after=1.0)

    # Fourteen seconds of work in eight on the clock: the time limit is not reached.
    ledger.model(outer).complete(REQUEST)

    assert ledger.stopped_by() is None
    assert ledger.left().seconds == pytest.approx(2.0)

    ledger.model(LocalModel(clock, seconds=3.0)).complete(REQUEST)

    assert ledger.stopped_by() is BudgetLimit.TIME


def test_share_time_limit_is_held_against_seconds_of_work() -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=100.0), clock=clock)
    ledger.share_out(2)
    inner = ledger.model(LocalModel(clock, seconds=30.0), claim=0)
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=5.0, after=5.0)

    # Seventy seconds of work for Claim 0, its share being 50, in forty on the clock.
    ledger.model(outer, claim=0).complete(REQUEST)

    assert ledger.stopped_by(claim=0) is BudgetLimit.TIME
    assert ledger.stopped_by() is None
    assert ledger.left().seconds == pytest.approx(60.0)
    with pytest.raises(BudgetSpent):
        ledger.model(LocalModel(clock), claim=0).complete(REQUEST)
    ledger.model(LocalModel(clock, seconds=1.0), claim=1).complete(REQUEST)


def test_shares_are_set_aside_from_what_is_left_on_the_clock() -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=100.0), clock=clock)
    inner = ledger.model(LocalModel(clock, seconds=50.0))
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=0.0, after=0.0)

    # A hundred seconds of work in fifty on the clock: fifty are left to share.
    ledger.model(outer).complete(REQUEST)
    ledger.share_out(2)

    assert ledger.left(claim=0).seconds == pytest.approx(25.0)
    sandbox = TimedSandbox(clock)
    run_in(ledger.sandbox(sandbox, claim=1))
    assert sandbox.uses[0][-1] == replace(LIMITS, seconds=25.0)
