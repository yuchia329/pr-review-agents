"""The Budget ledger: what is left to spend, moving shares between Claims, and work that was
under way when a limit was reached."""

from collections.abc import Callable

import pytest
from conftest import Clock
from hypothesis import given
from hypothesis import strategies as st
from test_budget import NOTHING, REQUEST, LocalModel, everything_recorded, steps

from pr_verification.budget import Budget, BudgetSpent, Ledger
from pr_verification.model import ModelRequest
from pr_verification.report import BudgetLimit, BudgetSpend

THIRDS = Budget(seconds=90.0, dollars=9.0, steps=30)
"""Shared between three Claims, each has 30 seconds, 3 dollars, and 10 steps."""

SHARE = BudgetSpend(seconds=30.0, dollars=3.0, steps=10)


def shared_between_three() -> Ledger:
    ledger = Ledger(THIRDS)
    ledger.share_out(3)
    return ledger


def everything_left(ledger: Ledger, claims: int = 3) -> list[BudgetSpend]:
    return [ledger.left(), *(ledger.left(claim=claim) for claim in range(claims))]


# What is left


def test_what_is_left_is_each_limit_less_what_was_recorded_against_it() -> None:
    ledger = shared_between_three()

    ledger.record(BudgetSpend(seconds=4.0, dollars=1.0, steps=1))
    ledger.record(BudgetSpend(seconds=10.0, dollars=0.5, steps=4), claim=0)

    assert ledger.left() == BudgetSpend(seconds=76.0, dollars=7.5, steps=25)
    assert ledger.left(claim=0) == BudgetSpend(seconds=20.0, dollars=2.5, steps=6)
    assert ledger.left(claim=1) == SHARE


def test_nothing_is_left_of_a_limit_that_was_passed_or_for_a_claim_with_no_share() -> None:
    unshared = Ledger(THIRDS)
    ledger = shared_between_three()

    ledger.record(BudgetSpend(seconds=45.0, dollars=0.0, steps=10), claim=0)

    assert ledger.left(claim=0) == BudgetSpend(seconds=0.0, dollars=3.0, steps=0)
    assert (ledger.left(claim=3), unshared.left(claim=0)) == (NOTHING, NOTHING)
    assert unshared.left() == BudgetSpend(seconds=90.0, dollars=9.0, steps=30)


# Moving shares


def test_moving_lowers_the_limits_of_one_share_raises_anothers_and_records_nothing() -> None:
    ledger = shared_between_three()
    ledger.record(BudgetSpend(seconds=10.0, dollars=1.0, steps=4), claim=0)
    recorded = everything_recorded(ledger, 3)

    ledger.move_share(BudgetSpend(seconds=5.0, dollars=0.5, steps=2), source=0, destination=1)

    assert everything_left(ledger) == [
        BudgetSpend(seconds=80.0, dollars=8.0, steps=26),
        BudgetSpend(seconds=15.0, dollars=1.5, steps=4),
        BudgetSpend(seconds=35.0, dollars=3.5, steps=12),
        SHARE,
    ]
    assert everything_recorded(ledger, 3) == recorded
    assert [ledger.stopped_by(claim=claim) for claim in range(3)] == [None, None, None]


def test_share_that_everything_left_in_one_unit_is_moved_out_of_is_spent() -> None:
    ledger = shared_between_three()
    ledger.record(steps(4), claim=0)

    ledger.move_share(steps(6), source=0, destination=1)

    assert ledger.stopped_by(claim=0) is BudgetLimit.STEPS
    with pytest.raises(BudgetSpent) as stopped:
        ledger.record(steps(1), claim=0)
    assert (stopped.value.limit, stopped.value.claim) == (BudgetLimit.STEPS, 0)
    # The other share has the 16 steps it was given, and the Budget is as it was.
    ledger.record(steps(15), claim=1)
    assert (ledger.stopped_by(claim=1), ledger.stopped_by()) == (None, None)


@pytest.mark.parametrize(
    ("given", "stopped_by"),
    [
        (BudgetSpend(seconds=1.0, dollars=0.0, steps=1), None),
        (BudgetSpend(seconds=0.0, dollars=0.0, steps=1), BudgetLimit.TIME),
        (BudgetSpend(seconds=1.0, dollars=0.0, steps=0), BudgetLimit.STEPS),
    ],
    ids=["more of both limits reached", "more steps only", "more seconds only"],
)
def test_spent_share_given_more_is_spent_no_longer_once_it_is_under_every_one_of_its_limits(
    given: BudgetSpend, stopped_by: BudgetLimit | None
) -> None:
    ledger = shared_between_three()
    ledger.record(BudgetSpend(seconds=30.0, dollars=0.0, steps=10), claim=0)
    assert ledger.stopped_by(claim=0) is BudgetLimit.TIME

    ledger.move_share(given, source=1, destination=0)

    assert ledger.stopped_by(claim=0) is stopped_by
    if stopped_by is None:
        ledger.record(steps(1), claim=0)  # Work for the Claim can go on.


@pytest.mark.parametrize(
    ("amount", "source", "destination"),
    [
        (steps(1), 0, 0),
        (steps(1), 3, 1),
        (steps(1), 0, -1),
        (NOTHING, 0, 1),
        (BudgetSpend(seconds=-1.0, dollars=0.0, steps=1), 0, 1),
        (BudgetSpend(seconds=float("nan"), dollars=0.0, steps=1), 0, 1),
        (BudgetSpend(seconds=0.0, dollars=float("inf"), steps=1), 0, 1),
        (steps(7), 0, 1),
        (BudgetSpend(seconds=30.5, dollars=0.0, steps=0), 0, 1),
    ],
    ids=[
        "to the same Claim",
        "from a Claim with no share",
        "to a Claim with no share",
        "nothing",
        "an amount under zero",
        "an amount that is no number",
        "an amount without end",
        "more steps than are left",
        "more seconds than are left",
    ],
)
def test_move_that_cannot_be_made_is_refused_and_nothing_is_moved(
    amount: BudgetSpend, source: int, destination: int
) -> None:
    ledger = shared_between_three()
    ledger.record(steps(4), claim=0)
    left = everything_left(ledger)

    with pytest.raises(ValueError):
        ledger.move_share(amount, source=source, destination=destination)

    assert everything_left(ledger) == left


def test_once_the_budget_is_spent_no_move_is_made_and_one_that_never_could_be_is_still_one() -> (
    None
):
    ledger = shared_between_three()
    ledger.record(steps(30))
    left = everything_left(ledger)

    with pytest.raises(BudgetSpent) as stopped:
        ledger.move_share(steps(1), source=0, destination=1)
    with pytest.raises(ValueError):
        ledger.move_share(steps(11), source=0, destination=1)

    assert (stopped.value.limit, stopped.value.claim) == (BudgetLimit.STEPS, None)
    assert everything_left(ledger) == left


# For any moves

FRACTIONS = st.floats(0, 1.5)
Entry = tuple[bool, int, int, float, float, float]
"""Whether it is a move, the Claim it takes from, the Claim it gives to, and how much of what
the first has left of its seconds, dollars, and steps: more than all of it past 1."""


@st.composite
def share_histories(draw: st.DrawFn) -> tuple[int, list[Entry]]:
    """A number of Claims, and moves of share between them with spends in between."""
    claims = draw(st.integers(2, 4))
    places = st.integers(0, claims - 1)
    entry = st.tuples(st.booleans(), places, places, FRACTIONS, FRACTIONS, FRACTIONS)
    return claims, draw(st.lists(entry, max_size=20))


def limits_of_each(ledger: Ledger, claims: int) -> list[BudgetSpend]:
    """The limits of each share, while no spend has passed one: what was recorded against the
    Claim and what is left of its share."""
    return [
        BudgetSpend(
            ledger.spent(claim=claim).seconds + ledger.left(claim=claim).seconds,
            ledger.spent(claim=claim).dollars + ledger.left(claim=claim).dollars,
            ledger.spent(claim=claim).steps + ledger.left(claim=claim).steps,
        )
        for claim in range(claims)
    ]


@given(share_histories())
def test_for_any_moves_the_shares_come_to_what_they_did_and_none_goes_below_nothing(
    history: tuple[int, list[Entry]],
) -> None:
    claims, entries = history
    ledger = Ledger(Budget(seconds=120.0, dollars=12.0, steps=60))
    ledger.share_out(claims)
    set_aside = limits_of_each(ledger, claims)

    for is_move, source, destination, seconds, dollars, in_steps in entries:
        left = ledger.left(claim=source)
        amount = BudgetSpend(
            seconds * left.seconds, dollars * left.dollars, int(in_steps * left.steps)
        )
        if not is_move:
            # No more than a third of what is left, so no spend passes a limit.
            part = BudgetSpend(amount.seconds / 3, amount.dollars / 3, amount.steps // 3)
            if ledger.stopped_by(claim=source) is None:
                ledger.record(part, claim=source)
            continue
        everything = [ledger.spent(), *everything_left(ledger, claims)]
        more_than_is_left = (
            amount.seconds > left.seconds
            or amount.dollars > left.dollars
            or amount.steps > left.steps
        )
        if source == destination or amount == NOTHING or more_than_is_left:
            with pytest.raises(ValueError):
                ledger.move_share(amount, source=source, destination=destination)
            assert [ledger.spent(), *everything_left(ledger, claims)] == everything
            continue
        given_to = ledger.left(claim=destination)
        ledger.move_share(amount, source=source, destination=destination)
        assert ledger.left(claim=destination).steps == given_to.steps + amount.steps
        assert ledger.left(claim=destination).seconds == pytest.approx(
            given_to.seconds + amount.seconds
        )
        assert (ledger.spent(), ledger.left()) == (everything[0], everything[1])

    shares = limits_of_each(ledger, claims)
    assert sum(share.steps for share in shares) == sum(share.steps for share in set_aside)
    assert sum(share.seconds for share in shares) == pytest.approx(120.0)
    assert sum(share.dollars for share in shares) == pytest.approx(12.0)
    for left in everything_left(ledger, claims):
        assert left.seconds >= 0 and left.dollars >= 0 and left.steps >= 0


# Work under way when a limit is reached


class Meanwhile:
    """A stand-in for a model that is at work while something else happens: `happens` is
    called during each request, as other work done at the same time would be."""

    def __init__(self, happens: Callable[[], None]) -> None:
        self._happens = happens

    def complete(self, request: ModelRequest) -> str:
        self._happens()
        return "a response"


@pytest.mark.parametrize(
    ("claim", "limit"), [(None, 30), (0, 10)], ids=["the Budget", "the share of its Claim"]
)
def test_work_started_before_a_limit_was_reached_is_let_end_and_is_recorded_whole(
    claim: int | None, limit: int
) -> None:
    ledger = shared_between_three()
    # Other work reaches the limit while the request is being made.
    meanwhile = Meanwhile(lambda: ledger.record(steps(limit), claim=claim))

    assert ledger.model(meanwhile, claim=claim).complete(REQUEST) == "a response"

    assert ledger.spent(claim=claim).steps == limit + 1
    refused_next = LocalModel(Clock())
    with pytest.raises(BudgetSpent):
        ledger.model(refused_next, claim=claim).complete(REQUEST)
    assert refused_next.requests == []
