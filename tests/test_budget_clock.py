"""The Budget ledger in seconds: on the clock for the whole pull request, in seconds of work for
each Claim (ADR 0007).

Work done at the same time is made with stand-in models that ask the ledger for other work
while their own request is under way, or with threads that wait for each other. Time passes
only on a controlled clock.
"""

import threading
from collections.abc import Callable
from dataclasses import replace
from functools import partial

import pytest
from conftest import Clock
from hypothesis import given
from hypothesis import strategies as st
from test_budget import (
    ARCHIVE,
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
from pr_verification.sandbox import SandboxFailed

ROOMY = Budget(seconds=1000.0, dollars=100.0, steps=1000)
ENDLESS = Budget(seconds=1e9, dollars=1e9, steps=10**6)


class CountingClock(Clock):
    """A controlled clock that counts how often it is read."""

    def __init__(self) -> None:
        super().__init__()
        self.readings = 0

    def __call__(self) -> float:
        self.readings += 1
        return self.now


class AroundOther:
    """A stand-in for a model whose request takes `before` seconds on the clock, then waits
    while `other` is done, then takes `between` seconds, then `after` more. What the ledger had
    recorded for the pull request, and had left of the Budget, once `between` had passed is
    kept in `seen` and `seen_left`, with how often the clock had been read until then."""

    def __init__(
        self,
        clock: Clock,
        ledger: Ledger,
        other: Callable[[], object],
        *,
        before: float,
        after: float,
        between: float = 0.0,
    ) -> None:
        self._clock, self._ledger, self._other = clock, ledger, other
        self._before, self._between, self._after = before, between, after
        self.seen: BudgetSpend | None = None
        self.seen_left: BudgetSpend | None = None

    def complete(self, request: ModelRequest) -> str:
        self._clock.now += self._before
        try:
            self._other()
        except (ModelFailed, SandboxFailed):
            pass
        self._clock.now += self._between
        self.seen = self._ledger.spent()
        self.seen_left = self._ledger.left()
        self._clock.now += self._after
        return "a response"


def seconds(ledger: Ledger, claims: int) -> list[float]:
    return [spent.seconds for spent in everything_recorded(ledger, claims)]


class Threads:
    """Work started on threads of its own, which raises again what any of them raised."""

    def __init__(self) -> None:
        self._threads: list[threading.Thread] = []
        self._raised: list[BaseException] = []

    def start(self, work: Callable[[], object]) -> None:
        def run() -> None:
            try:
                work()
            except BaseException as raised:
                self._raised.append(raised)

        thread = threading.Thread(target=run, daemon=True)
        self._threads.append(thread)
        thread.start()

    def join(self) -> None:
        for thread in self._threads:
            thread.join(timeout=10)
            assert not thread.is_alive()
        if self._raised:
            raise self._raised[0]


# Work done at the same time


def test_work_done_at_the_same_time_counts_once_for_the_pull_request_and_in_full_for_a_claim() -> (
    None
):
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    inner = ledger.model(LocalModel(clock, seconds=3.0), claim=1)
    outer = AroundOther(
        clock, ledger, lambda: inner.complete(REQUEST), before=2.0, between=1.0, after=4.0
    )

    # Work for no Claim from 0 to 10 seconds, and inside it work for Claim 1 from 2 to 5.
    ledger.model(outer).complete(REQUEST)

    # At 6 seconds, the work under way since the inner work ended is not yet counted.
    assert outer.seen is not None and outer.seen.seconds == pytest.approx(5.0)
    assert outer.seen_left is not None and outer.seen_left.seconds == pytest.approx(995.0)
    assert seconds(ledger, 2) == pytest.approx([10.0, 0.0, 3.0])


def test_work_for_two_claims_at_the_same_time_counts_in_full_against_each_share() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    inner = ledger.model(LocalModel(clock, seconds=6.0), claim=1)
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=1.0, after=1.0)

    ledger.model(outer, claim=0).complete(REQUEST)

    assert seconds(ledger, 2) == pytest.approx([8.0, 8.0, 6.0])


def test_sandbox_work_done_at_the_same_time_counts_once_for_the_pull_request() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    sandbox = ledger.sandbox(TimedSandbox(clock, seconds=3.0), claim=1)

    def run_and_save() -> None:
        run_in(sandbox)
        sandbox.save_home("a-commit", ARCHIVE)

    outer = AroundOther(clock, ledger, run_and_save, before=1.0, after=2.0)

    # Work for Claim 0 from 0 to 9, and inside it a run from 1 to 4 and a save from 4 to 7.
    ledger.model(outer, claim=0).complete(REQUEST)

    assert seconds(ledger, 2) == pytest.approx([9.0, 9.0, 6.0])


def test_sandbox_work_that_raises_is_under_way_until_it_raises() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(1)
    failing = TimedSandbox(clock, seconds=3.0, failure=SandboxFailed("no"))
    through = ledger.sandbox(failing, claim=0)
    outer = AroundOther(clock, ledger, lambda: run_in(through), before=1.0, after=2.0)

    ledger.model(outer).complete(REQUEST)
    clock.now += 10.0
    ledger.model(LocalModel(clock, seconds=1.0)).complete(REQUEST)

    assert seconds(ledger, 1) == pytest.approx([7.0, 3.0])


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

    threads = Threads()
    for claim in range(2):
        model = ledger.model(Together(), claim=claim)
        threads.start(partial(model.complete, REQUEST))
    threads.join()

    assert seconds(ledger, 2) == pytest.approx([4.0, 4.0, 4.0])
    assert ledger.spent().steps == 2


def test_threads_whose_work_overlaps_count_the_time_either_was_under_way() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)

    class Waits:
        def __init__(self) -> None:
            self.started, self.may_end = threading.Event(), threading.Event()

        def complete(self, request: ModelRequest) -> str:
            self.started.set()
            assert self.may_end.wait(timeout=10)
            return "a response"

    first, second = Waits(), Waits()
    threads = Threads()
    # The first is under way from 0 to 5 seconds, and the second from 3 to 8.
    threads.start(lambda: ledger.model(first, claim=0).complete(REQUEST))
    assert first.started.wait(timeout=10)
    clock.now = 3.0
    threads.start(lambda: ledger.model(second, claim=1).complete(REQUEST))
    assert second.started.wait(timeout=10)
    clock.now = 5.0
    first.may_end.set()
    while ledger.spent().steps < 1:
        threading.Event().wait(0.001)

    assert ledger.spent().seconds == pytest.approx(5.0)

    clock.now = 8.0
    second.may_end.set()
    threads.join()

    assert seconds(ledger, 2) == pytest.approx([8.0, 5.0, 5.0])


def test_readings_of_the_clock_from_many_threads_count_the_time_any_work_was_under_way() -> None:
    class Ticking:
        """A clock that moves on by one second at each reading, and keeps every reading with
        the thread that made it. It lets other threads run before it gives the reading back,
        as a reading interrupted by them would."""

        def __init__(self) -> None:
            self._lock = threading.Lock()
            self._now = 0.0
            self.readings: list[tuple[int, float]] = []

        def __call__(self) -> float:
            with self._lock:
                self._now += 1.0
                self.readings.append((threading.get_ident(), self._now))
                now = self._now
            threading.Event().wait(0.0002)
            return now

    clock = Ticking()
    ledger = Ledger(ENDLESS, clock=clock)
    ledger.share_out(4)

    class Yields:
        def complete(self, request: ModelRequest) -> str:
            threading.Event().wait(0.0001)
            return "a response"

    threads = Threads()
    idents: list[int] = []

    def work(claim: int) -> None:
        idents.append(threading.get_ident())
        model = ledger.model(Yields(), claim=claim)
        for _ in range(25):
            model.complete(REQUEST)

    for claim in range(4):
        threads.start(partial(work, claim))
    threads.join()

    # Each thread does one piece of work after another, and the ledger reads the clock once
    # as each starts and once as it ends, so a thread's readings pair up in order.
    intervals: dict[int, list[tuple[float, float]]] = {}
    for ident in set(idents):
        own = [reading for thread, reading in clock.readings if thread == ident]
        intervals[ident] = list(zip(own[::2], own[1::2], strict=True))
    union, reached = 0.0, 0.0
    for start, end in sorted(pair for pairs in intervals.values() for pair in pairs):
        union += max(0.0, end - max(start, reached))
        reached = max(reached, end)
    assert ledger.spent().seconds == pytest.approx(union)
    assert sorted(spent.seconds for spent in everything_recorded(ledger, 4)[1:]) == pytest.approx(
        sorted(sum(end - start for start, end in pairs) for pairs in intervals.values())
    )


def test_work_that_raises_is_under_way_until_it_raises() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(1)
    failing = ledger.model(LocalModel(clock, seconds=3.0, answer=ModelFailed("no")), claim=0)
    outer = AroundOther(clock, ledger, lambda: failing.complete(REQUEST), before=1.0, after=2.0)

    ledger.model(outer).complete(REQUEST)
    clock.now += 10.0
    ledger.model(LocalModel(clock, seconds=1.0)).complete(REQUEST)

    assert seconds(ledger, 1) == pytest.approx([7.0, 3.0])


# Time that counts, and time that does not


def test_time_in_which_no_work_was_under_way_is_not_counted() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    model = ledger.model(LocalModel(clock, seconds=2.0))

    model.complete(REQUEST)
    clock.now += 50.0
    model.complete(REQUEST)

    assert ledger.spent().seconds == pytest.approx(4.0)


def test_what_was_recorded_is_read_without_reading_the_clock() -> None:
    clock = CountingClock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(1)

    def read_everything() -> None:
        before = clock.readings
        ledger.spent()
        ledger.spent(claim=0)
        ledger.left()
        ledger.left(claim=0)
        ledger.stopped_by()
        ledger.stopped_by(claim=0)
        assert clock.readings == before

    outer = AroundOther(clock, ledger, read_everything, before=5.0, after=1.0)
    ledger.model(outer).complete(REQUEST)


def test_seconds_given_to_record_count_in_full_though_work_is_under_way() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(1)

    def record_three_seconds() -> None:
        ledger.record(BudgetSpend(seconds=3.0, dollars=0.0, steps=0), claim=0)

    outer = AroundOther(clock, ledger, record_three_seconds, before=2.0, after=2.0)
    ledger.model(outer).complete(REQUEST)

    assert seconds(ledger, 1) == pytest.approx([7.0, 3.0])


def test_work_refused_once_the_budget_is_spent_adds_no_seconds_and_reads_no_clock() -> None:
    clock = CountingClock()
    ledger = Ledger(Budget(seconds=5.0), clock=clock)
    ledger.model(LocalModel(clock, seconds=5.0)).complete(REQUEST)
    refused = LocalModel(clock, seconds=7.0)
    readings = clock.readings

    with pytest.raises(BudgetSpent):
        ledger.model(refused).complete(REQUEST)

    assert refused.requests == []
    assert clock.readings == readings
    assert ledger.spent().seconds == pytest.approx(5.0)


def test_work_refused_for_a_spent_share_is_never_under_way() -> None:
    clock = CountingClock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    ledger.record(BudgetSpend(seconds=500.0, dollars=0.0, steps=0), claim=0)
    readings = clock.readings

    with pytest.raises(BudgetSpent):
        ledger.model(LocalModel(clock, seconds=7.0), claim=0).complete(REQUEST)
    assert clock.readings == readings
    clock.now += 10.0
    ledger.model(LocalModel(clock, seconds=1.0), claim=1).complete(REQUEST)

    assert seconds(ledger, 2) == pytest.approx([501.0, 500.0, 1.0])


def test_work_is_refused_by_what_was_recorded_and_not_by_work_still_under_way() -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=10.0), clock=clock)
    inner = LocalModel(clock, seconds=1.0)
    through = ledger.model(inner)

    # Work under way from 0, and other work asked for at 12, before any has ended.
    outer = AroundOther(clock, ledger, lambda: through.complete(REQUEST), before=12.0, after=1.0)
    ledger.model(outer).complete(REQUEST)

    assert inner.requests == [REQUEST]
    assert ledger.spent().seconds == pytest.approx(14.0)
    assert ledger.stopped_by() is BudgetLimit.TIME


def test_work_under_way_when_the_time_limit_is_reached_is_recorded_whole() -> None:
    clock = Clock()
    ledger = Ledger(Budget(seconds=10.0), clock=clock)
    inner = ledger.model(LocalModel(clock, seconds=8.0))
    outer = AroundOther(clock, ledger, lambda: inner.complete(REQUEST), before=2.0, after=2.0)

    # The inner work reaches the limit at 10 seconds, and the outer work ends at 12.
    ledger.model(outer).complete(REQUEST)

    assert ledger.spent().seconds == pytest.approx(12.0)
    assert ledger.stopped_by() is BudgetLimit.TIME


whole_seconds = st.integers(min_value=0, max_value=50).map(float)


@given(
    st.lists(
        st.tuples(
            st.sampled_from(["model", "sandbox", "record"]),
            st.sampled_from([None, 0, 1, 2]),
            whole_seconds,
            whole_seconds,
        ),
        max_size=20,
    )
)
def test_for_any_work_done_one_piece_at_a_time_the_pull_request_has_the_sum_of_its_parts(
    work: list[tuple[str, int | None, float, float]],
) -> None:
    clock = Clock()
    ledger = Ledger(ENDLESS, clock=clock)
    ledger.share_out(3)

    for kind, claim, taking, idle in work:
        if kind == "model":
            ledger.model(LocalModel(clock, seconds=taking), claim=claim).complete(REQUEST)
        elif kind == "sandbox":
            run_in(ledger.sandbox(TimedSandbox(clock, seconds=taking), claim=claim))
        else:
            ledger.record(BudgetSpend(seconds=taking, dollars=0.0, steps=0), claim=claim)
        clock.now += idle

    recorded = everything_recorded(ledger, 3)
    assert recorded[0].seconds == pytest.approx(sum(taking for _, _, taking, _ in work))
    for claim in range(3):
        for_claim = [taking for _, of, taking, _ in work if of == claim]
        assert recorded[1 + claim].seconds == pytest.approx(sum(for_claim))


@given(
    st.lists(st.tuples(whole_seconds, whole_seconds), min_size=1, max_size=6),
    st.lists(st.sampled_from([None, 0, 1]), min_size=6, max_size=6),
)
def test_for_any_work_one_inside_another_the_pull_request_has_the_outermost(
    takes: list[tuple[float, float]], claims: list[int | None]
) -> None:
    # Each piece of work takes `before` seconds, then the one inside it, then `after` seconds.
    clock = Clock()
    ledger = Ledger(ENDLESS, clock=clock)
    ledger.share_out(2)
    pieces: list[AroundOther] = []

    def piece(depth: int) -> Callable[[], object]:
        if depth == len(takes):
            return lambda: None
        before, after = takes[depth]
        model = AroundOther(clock, ledger, piece(depth + 1), before=before, after=after)
        pieces.insert(0, model)
        through = ledger.model(model, claim=claims[depth])
        return lambda: through.complete(REQUEST)

    piece(0)()

    lasting = [0.0] * (len(takes) + 1)
    for depth in reversed(range(len(takes))):
        lasting[depth] = takes[depth][0] + lasting[depth + 1] + takes[depth][1]
    starts = [sum(before for before, _ in takes[:depth]) for depth in range(len(takes) + 1)]
    recorded = everything_recorded(ledger, 2)
    assert recorded[0].seconds == pytest.approx(lasting[0])
    # Once the piece inside one has ended, the pull request has the time up to its end.
    for depth, model in enumerate(pieces):
        inside_ended = starts[depth + 1] + lasting[depth + 1] if depth + 1 < len(takes) else 0.0
        assert model.seen is not None and model.seen.seconds == pytest.approx(inside_ended)
    claimed = [
        sum(lasting[depth] for depth in range(len(takes)) if claims[depth] == claim)
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
