"""The Budget ledger: what it records, when a limit is reached, and what stops then.

The models and the Sandbox are stand-ins, and time passes only on a controlled clock.
"""

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import Clock
from hypothesis import given
from hypothesis import strategies as st

from pr_verification.budget import (
    BUDGET_SPENT,
    DEFAULT_DOLLARS,
    DEFAULT_SECONDS,
    DEFAULT_STEPS,
    DOLLARS_VARIABLE,
    SECONDS_VARIABLE,
    SHARE_SPENT,
    STEPS_VARIABLE,
    Budget,
    BudgetSpent,
    InvalidBudget,
    Ledger,
    budget_from_environment,
)
from pr_verification.model import ModelFailed, ModelRequest, PricedResponse
from pr_verification.report import BudgetLimit, BudgetSpend
from pr_verification.sandbox import (
    Command,
    Limits,
    Phase,
    RunResult,
    Sandbox,
    SandboxFailed,
    SandboxWithHomes,
)

REQUEST = ModelRequest(instructions="Write a Reproduction.", data='{"claim": "Adds tax"}')
ROOMY = Budget(seconds=100.0, dollars=10.0, steps=10)
NOTHING = BudgetSpend(seconds=0.0, dollars=0.0, steps=0)


def steps(number: int) -> BudgetSpend:
    return BudgetSpend(seconds=0.0, dollars=0.0, steps=number)


class LocalModel:
    """A stand-in for a model with no price, as a local one has none. Each request takes
    `seconds` on the clock, and is answered with `answer` or raises it."""

    def __init__(
        self, clock: Clock, seconds: float = 0.0, answer: str | Exception = "a response"
    ) -> None:
        self._clock, self._seconds, self._answer = clock, seconds, answer
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        self._clock.now += self._seconds
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


class PricedAt(LocalModel):
    """A stand-in for a model with a price: it says each request cost `dollars`."""

    def __init__(
        self,
        dollars: float,
        clock: Clock,
        seconds: float = 0.0,
        answer: str | Exception = "a response",
    ) -> None:
        super().__init__(clock, seconds, answer)
        self._dollars = dollars

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        return PricedResponse(self.complete(request), self._dollars)


# What is recorded


def test_ledger_starts_with_nothing_spent_and_no_limit_reached() -> None:
    ledger = Ledger(ROOMY)

    assert (ledger.spent(), ledger.stopped_by()) == (NOTHING, None)


def test_spend_for_a_claim_is_recorded_against_that_claim_and_the_whole_pull_request() -> None:
    ledger = Ledger(ROOMY)
    ledger.share_out(2)

    ledger.record(BudgetSpend(seconds=1.0, dollars=0.25, steps=1))
    ledger.record(BudgetSpend(seconds=2.0, dollars=0.5, steps=2), claim=0)
    ledger.record(BudgetSpend(seconds=4.0, dollars=1.0, steps=1), claim=1)
    ledger.record(BudgetSpend(seconds=8.0, dollars=0.0, steps=1), claim=1)

    assert ledger.spent() == BudgetSpend(seconds=15.0, dollars=1.75, steps=5)
    assert ledger.spent(claim=0) == BudgetSpend(seconds=2.0, dollars=0.5, steps=2)
    assert ledger.spent(claim=1) == BudgetSpend(seconds=12.0, dollars=1.0, steps=2)


def test_claim_no_share_was_set_aside_for_has_nothing_spent_and_no_limit_reached() -> None:
    ledger = Ledger(ROOMY)
    ledger.share_out(2)

    assert (ledger.spent(claim=2), ledger.stopped_by(claim=2)) == (NOTHING, None)


@pytest.mark.parametrize(
    "spend",
    [
        BudgetSpend(seconds=-1.0, dollars=0.0, steps=0),
        BudgetSpend(seconds=0.0, dollars=-0.25, steps=0),
        BudgetSpend(seconds=0.0, dollars=0.0, steps=-1),
    ],
    ids=["seconds", "dollars", "steps"],
)
def test_spend_with_an_amount_under_zero_is_refused_and_nothing_is_recorded(
    spend: BudgetSpend,
) -> None:
    ledger = Ledger(ROOMY)

    with pytest.raises(ValueError):
        ledger.record(spend)

    assert ledger.spent() == NOTHING


@pytest.mark.parametrize("claim", [-1, 2], ids=["before the first", "past the last"])
def test_spend_for_a_claim_no_share_was_set_aside_for_is_refused(claim: int) -> None:
    ledger = Ledger(ROOMY)
    ledger.share_out(2)

    with pytest.raises(ValueError):
        ledger.record(steps(1), claim=claim)

    assert ledger.spent() == NOTHING


def test_until_shares_are_set_aside_nothing_can_be_recorded_against_a_claim() -> None:
    ledger = Ledger(ROOMY)

    with pytest.raises(ValueError):
        ledger.record(steps(1), claim=0)

    assert ledger.spent() == NOTHING


def test_spend_that_could_never_be_recorded_is_refused_as_one_whatever_has_been_spent() -> None:
    ledger = Ledger(Budget(steps=1))
    ledger.record(steps(1))

    with pytest.raises(ValueError):
        ledger.record(steps(-1))
    with pytest.raises(ValueError):
        ledger.record(steps(1), claim=0)


# When a limit is reached


@pytest.mark.parametrize(
    ("spend", "limit", "recorded"),
    [
        (
            BudgetSpend(seconds=9.0, dollars=0.0, steps=0),
            BudgetLimit.TIME,
            BudgetSpend(seconds=10.0, dollars=0.25, steps=1),
        ),
        (
            BudgetSpend(seconds=0.0, dollars=1.5, steps=0),
            BudgetLimit.MONEY,
            BudgetSpend(seconds=1.0, dollars=1.75, steps=1),
        ),
        (
            BudgetSpend(seconds=0.0, dollars=0.0, steps=4),
            BudgetLimit.STEPS,
            BudgetSpend(seconds=1.0, dollars=0.25, steps=5),
        ),
    ],
    ids=["time, met exactly", "money, passed", "steps, met exactly"],
)
def test_first_limit_reached_stops_the_work_and_nothing_more_is_recorded(
    spend: BudgetSpend, limit: BudgetLimit, recorded: BudgetSpend
) -> None:
    ledger = Ledger(Budget(seconds=10.0, dollars=1.0, steps=5))
    ledger.record(BudgetSpend(seconds=1.0, dollars=0.25, steps=1))
    assert ledger.stopped_by() is None

    ledger.record(spend)
    with pytest.raises(BudgetSpent) as stopped:
        ledger.record(BudgetSpend(seconds=1.0, dollars=0.25, steps=1))

    # The spend that reached the limit is recorded whole, and the one after it not at all.
    assert (ledger.stopped_by(), ledger.spent()) == (limit, recorded)
    assert (stopped.value.limit, stopped.value.claim) == (limit, None)
    assert str(stopped.value) == BUDGET_SPENT.format(limit=limit.value)


@pytest.mark.parametrize(
    ("spend", "limit"),
    [
        (BudgetSpend(seconds=10.0, dollars=1.0, steps=5), BudgetLimit.TIME),
        (BudgetSpend(seconds=0.0, dollars=1.0, steps=5), BudgetLimit.MONEY),
    ],
    ids=["all three", "money and steps"],
)
def test_spend_that_reaches_several_limits_at_once_is_stopped_by_the_first_of_time_money_steps(
    spend: BudgetSpend, limit: BudgetLimit
) -> None:
    ledger = Ledger(Budget(seconds=10.0, dollars=1.0, steps=5))

    ledger.record(spend)

    assert ledger.stopped_by() is limit


@pytest.mark.parametrize(
    ("spend", "limit"),
    [
        (BudgetSpend(seconds=DEFAULT_SECONDS - 1, dollars=0.0, steps=0), None),
        (BudgetSpend(seconds=DEFAULT_SECONDS, dollars=0.0, steps=0), BudgetLimit.TIME),
        (BudgetSpend(seconds=0.0, dollars=DEFAULT_DOLLARS, steps=0), BudgetLimit.MONEY),
        (BudgetSpend(seconds=0.0, dollars=0.0, steps=DEFAULT_STEPS), BudgetLimit.STEPS),
    ],
    ids=["under fifteen minutes", "fifteen minutes", "a few dollars", "the default steps"],
)
def test_ledger_given_no_budget_keeps_to_the_provisional_defaults(
    spend: BudgetSpend, limit: BudgetLimit | None
) -> None:
    ledger = Ledger()

    ledger.record(spend)

    assert ledger.stopped_by() is limit
    assert DEFAULT_SECONDS == 15 * 60
    assert Budget() == Budget(DEFAULT_SECONDS, DEFAULT_DOLLARS, DEFAULT_STEPS)


@pytest.mark.parametrize(
    "budget",
    [Budget(seconds=0.0), Budget(dollars=-1.0), Budget(steps=0)],
    ids=["no seconds", "dollars under zero", "no steps"],
)
def test_budget_with_a_limit_that_is_not_more_than_zero_is_refused(budget: Budget) -> None:
    with pytest.raises(ValueError):
        Ledger(budget)


@pytest.mark.parametrize(
    ("claim", "message"),
    [(None, BUDGET_SPENT.format(limit="money")), (2, SHARE_SPENT.format(limit="money"))],
    ids=["the Budget", "the share of a Claim"],
)
def test_work_refused_says_which_limit_was_reached_and_whether_of_the_budget_or_a_share(
    claim: int | None, message: str
) -> None:
    refused = BudgetSpent(BudgetLimit.MONEY, claim)

    assert (refused.limit, refused.claim, str(refused)) == (BudgetLimit.MONEY, claim, message)


# The share of each Claim


@pytest.mark.parametrize(
    ("most", "rest", "limit"),
    [
        (
            BudgetSpend(seconds=29.0, dollars=0.0, steps=0),
            BudgetSpend(seconds=1.0, dollars=0.0, steps=0),
            BudgetLimit.TIME,
        ),
        (
            BudgetSpend(seconds=0.0, dollars=0.75, steps=0),
            BudgetSpend(seconds=0.0, dollars=0.25, steps=0),
            BudgetLimit.MONEY,
        ),
        (steps(2), steps(1), BudgetLimit.STEPS),
    ],
    ids=["time", "money", "steps, rounded down"],
)
def test_share_of_each_claim_is_what_is_left_of_each_limit_divided_equally_between_the_claims(
    most: BudgetSpend, rest: BudgetSpend, limit: BudgetLimit
) -> None:
    ledger = Ledger(Budget(seconds=100.0, dollars=4.0, steps=11))
    ledger.record(BudgetSpend(seconds=10.0, dollars=1.0, steps=1))

    # What is left is 90 seconds, 3 dollars, and 10 steps: 30 seconds, 1 dollar, and 3 steps each.
    ledger.share_out(3)

    for claim in range(3):
        ledger.record(most, claim=claim)
        assert ledger.stopped_by(claim=claim) is None
        ledger.record(rest, claim=claim)
        assert ledger.stopped_by(claim=claim) is limit


def test_claim_that_spends_its_share_is_stopped_while_work_on_the_others_goes_on() -> None:
    ledger = Ledger(Budget(steps=9))
    ledger.share_out(3)

    ledger.record(steps(4), claim=0)
    with pytest.raises(BudgetSpent) as stopped:
        ledger.record(steps(1), claim=0)
    ledger.record(steps(2), claim=1)
    ledger.record(BudgetSpend(seconds=5.0, dollars=0.0, steps=0))

    assert (stopped.value.limit, stopped.value.claim) == (BudgetLimit.STEPS, 0)
    assert str(stopped.value) == SHARE_SPENT.format(limit="steps")
    # The spend that reached the share's limit is recorded whole, and the one after it not at all.
    assert (ledger.spent(claim=0), ledger.stopped_by(claim=0)) == (steps(4), BudgetLimit.STEPS)
    # It took nothing from the share of another Claim: a step of that Claim's three is left.
    assert (ledger.stopped_by(claim=1), ledger.stopped_by()) == (None, None)
    ledger.record(steps(1), claim=1)
    assert ledger.stopped_by(claim=1) is BudgetLimit.STEPS


def test_with_fewer_steps_left_than_claims_each_share_is_one_step_and_the_budget_still_holds() -> (
    None
):
    ledger = Ledger(Budget(steps=2))
    ledger.share_out(3)

    ledger.record(steps(1), claim=0)
    ledger.record(steps(1), claim=1)
    with pytest.raises(BudgetSpent) as stopped:
        ledger.record(steps(1), claim=2)

    assert [ledger.stopped_by(claim=claim) for claim in range(3)] == [
        BudgetLimit.STEPS,
        BudgetLimit.STEPS,
        None,  # The share of a Claim says nothing of the Budget.
    ]
    assert (ledger.stopped_by(), stopped.value.claim) == (BudgetLimit.STEPS, None)


@pytest.mark.parametrize("claims", [0, -1])
def test_shares_for_fewer_than_one_claim_are_refused(claims: int) -> None:
    with pytest.raises(ValueError):
        Ledger(ROOMY).share_out(claims)


def test_shares_are_set_aside_once() -> None:
    ledger = Ledger(ROOMY)
    ledger.share_out(2)

    with pytest.raises(ValueError):
        ledger.share_out(2)


def test_no_shares_are_set_aside_once_the_budget_is_spent() -> None:
    ledger = Ledger(Budget(dollars=1.0))
    ledger.record(BudgetSpend(seconds=0.0, dollars=1.0, steps=0))

    with pytest.raises(BudgetSpent) as stopped:
        ledger.share_out(2)

    assert (stopped.value.limit, stopped.value.claim) == (BudgetLimit.MONEY, None)


# For any spends

AMOUNTS = st.builds(
    BudgetSpend, seconds=st.floats(0, 40), dollars=st.floats(0, 2), steps=st.integers(0, 4)
)
History = tuple[Budget, int, list[tuple[BudgetSpend, int | None]]]


@st.composite
def histories(draw: st.DrawFn) -> History:
    """A Budget, a number of Claims, and spends to record: each for one of the Claims or none."""
    budget = draw(
        st.builds(
            Budget, seconds=st.floats(1, 100), dollars=st.floats(0.5, 5), steps=st.integers(1, 12)
        )
    )
    claims = draw(st.integers(1, 4))
    spends = st.tuples(AMOUNTS, st.none() | st.integers(0, claims - 1))
    return budget, claims, draw(st.lists(spends, max_size=20))


def everything_recorded(ledger: Ledger, claims: int) -> list[BudgetSpend]:
    return [ledger.spent(), *(ledger.spent(claim=claim) for claim in range(claims))]


@given(histories())
def test_for_any_spends_nothing_is_recorded_after_a_limit_has_been_reached(
    history: History,
) -> None:
    budget, claims, spends = history
    ledger = Ledger(budget)
    ledger.share_out(claims)

    for spend, claim in spends:
        reached = ledger.stopped_by() is not None or (
            claim is not None and ledger.stopped_by(claim=claim) is not None
        )
        before = everything_recorded(ledger, claims)
        if not reached:
            ledger.record(spend, claim=claim)
            continue
        with pytest.raises(BudgetSpent):
            ledger.record(spend, claim=claim)
        assert everything_recorded(ledger, claims) == before


@given(histories())
def test_for_any_spends_the_spend_on_the_pull_request_is_the_sum_of_the_spend_on_its_parts(
    history: History,
) -> None:
    budget, claims, spends = history
    ledger = Ledger(budget)
    ledger.share_out(claims)
    for_no_claim = []

    for spend, claim in spends:
        try:
            ledger.record(spend, claim=claim)
        except BudgetSpent:
            continue
        if claim is None:
            for_no_claim.append(spend)

    parts = [*(ledger.spent(claim=claim) for claim in range(claims)), *for_no_claim]
    assert ledger.spent().steps == sum(part.steps for part in parts)
    assert ledger.spent().seconds == pytest.approx(sum(part.seconds for part in parts))
    assert ledger.spent().dollars == pytest.approx(sum(part.dollars for part in parts))


@given(histories())
def test_for_any_spends_the_budget_is_spent_once_a_limit_is_reached_and_never_before(
    history: History,
) -> None:
    budget, claims, spends = history
    ledger = Ledger(budget)
    ledger.share_out(claims)

    for spend, claim in spends:
        try:
            ledger.record(spend, claim=claim)
        except BudgetSpent:
            continue
        spent = ledger.spent()
        reached = [
            limit
            for limit, recorded, most in (
                (BudgetLimit.TIME, spent.seconds, budget.seconds),
                (BudgetLimit.MONEY, spent.dollars, budget.dollars),
                (BudgetLimit.STEPS, spent.steps, budget.steps),
            )
            if recorded >= most
        ]
        assert ledger.stopped_by() == (reached[0] if reached else None)


@given(
    budget=st.builds(
        Budget, seconds=st.floats(50, 100), dollars=st.floats(3, 5), steps=st.integers(5, 40)
    ),
    before=AMOUNTS,
    claims=st.integers(1, 6),
)
def test_for_any_budget_the_share_of_a_claim_is_what_is_left_of_each_limit_divided_by_the_claims(
    budget: Budget, before: BudgetSpend, claims: int
) -> None:
    seconds = (budget.seconds - before.seconds) / claims
    dollars = (budget.dollars - before.dollars) / claims
    whole = max(1, (budget.steps - before.steps) // claims)
    halves = [
        (BudgetSpend(seconds=seconds / 2, dollars=0.0, steps=0), BudgetLimit.TIME),
        (BudgetSpend(seconds=0.0, dollars=dollars / 2, steps=0), BudgetLimit.MONEY),
    ]

    for half, limit in halves:
        ledger = Ledger(budget)
        ledger.record(before)
        ledger.share_out(claims)
        ledger.record(half, claim=claims - 1)
        assert ledger.stopped_by(claim=claims - 1) is None
        ledger.record(half, claim=claims - 1)
        assert ledger.stopped_by(claim=claims - 1) is limit
    ledger = Ledger(budget)
    ledger.record(before)
    ledger.share_out(claims)
    ledger.record(steps(whole - 1), claim=0)
    assert ledger.stopped_by(claim=0) is None
    ledger.record(steps(1), claim=0)
    assert ledger.stopped_by(claim=0) is BudgetLimit.STEPS


# Asking a model through the ledger


def test_request_through_the_ledger_is_one_step_timed_by_the_clock_at_the_models_price() -> None:
    clock = Clock()
    model = PricedAt(0.25, clock, seconds=2.5)
    ledger = Ledger(ROOMY, clock=clock)

    response = ledger.model(model).complete(REQUEST)

    assert (response, model.requests) == ("a response", [REQUEST])
    assert ledger.spent() == BudgetSpend(seconds=2.5, dollars=0.25, steps=1)


def test_local_model_costs_time_and_a_step_and_no_money() -> None:
    clock = Clock()
    model = LocalModel(clock, seconds=2.5)
    ledger = Ledger(ROOMY, clock=clock)

    response = ledger.model(model).complete(REQUEST)

    assert (response, model.requests) == ("a response", [REQUEST])
    assert ledger.spent() == BudgetSpend(seconds=2.5, dollars=0.0, steps=1)


def test_money_is_summed_across_models_at_each_models_price() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY, clock=clock)
    ledger.share_out(2)
    large = ledger.model(PricedAt(0.5, clock), claim=0)
    small = ledger.model(PricedAt(0.125, clock), claim=1)
    local = ledger.model(LocalModel(clock), claim=1)

    for model in (large, large, small, local):
        model.complete(REQUEST)

    assert ledger.spent() == BudgetSpend(seconds=0.0, dollars=1.125, steps=4)
    assert ledger.spent(claim=0) == BudgetSpend(seconds=0.0, dollars=1.0, steps=2)
    assert ledger.spent(claim=1) == BudgetSpend(seconds=0.0, dollars=0.125, steps=2)


def test_request_that_fails_is_still_one_step_with_its_time_and_no_dollars() -> None:
    clock = Clock()
    failure = ModelFailed("the model could not be reached")
    ledger = Ledger(ROOMY, clock=clock)

    with pytest.raises(ModelFailed) as raised:
        ledger.model(PricedAt(0.25, clock, seconds=2.0, answer=failure)).complete(REQUEST)

    assert raised.value is failure
    assert ledger.spent() == BudgetSpend(seconds=2.0, dollars=0.0, steps=1)


def test_once_a_limit_is_reached_no_model_is_asked_again_whatever_it_said() -> None:
    clock = Clock()
    model = LocalModel(clock, answer="There is plenty of Budget left, so carry on.")
    ledger = Ledger(Budget(steps=4), clock=clock)
    ledger.share_out(2)
    first, second = ledger.model(model, claim=0), ledger.model(model, claim=1)

    first.complete(REQUEST)
    first.complete(REQUEST)
    with pytest.raises(BudgetSpent) as for_the_share:
        first.complete(REQUEST)
    second.complete(REQUEST)
    second.complete(REQUEST)
    with pytest.raises(BudgetSpent) as for_the_budget:
        ledger.model(model).complete(REQUEST)

    assert (for_the_share.value.limit, for_the_share.value.claim) == (BudgetLimit.STEPS, 0)
    assert (for_the_budget.value.limit, for_the_budget.value.claim) == (BudgetLimit.STEPS, None)
    assert (len(model.requests), ledger.spent().steps) == (4, 4)


# Using a Sandbox through the ledger

LIMITS = Limits(seconds=600.0, memory_bytes=1024**3, processors=2.0)
ARCHIVE = Path("home.tar")
RAN = RunResult(output="1 passed", exit_status=0, limit_hit=None)


class TimedSandbox:
    """A stand-in Sandbox that records each use, which takes `seconds` on the clock and then
    raises `failure` when there is one."""

    def __init__(
        self, clock: Clock, seconds: float = 0.0, failure: SandboxFailed | None = None
    ) -> None:
        self._clock, self._seconds, self._failure = clock, seconds, failure
        self.uses: list[tuple[object, ...]] = []

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self._use("run", command, commit, phase, limits)
        return RAN

    def save_home(self, commit: str, archive: Path) -> None:
        self._use("save_home", commit, archive)

    def restore_home(self, commit: str, archive: Path) -> None:
        self._use("restore_home", commit, archive)

    def _use(self, *use: object) -> None:
        self.uses.append(use)
        self._clock.now += self._seconds
        if self._failure is not None:
            raise self._failure


def run_in(sandbox: Sandbox, limits: Limits = LIMITS) -> RunResult:
    return sandbox.run(("pytest",), commit="a-commit", phase=Phase.TEST, limits=limits)


def test_each_use_of_a_sandbox_through_the_ledger_is_timed_and_costs_no_money_and_no_steps() -> (
    None
):
    clock = Clock()
    sandbox = TimedSandbox(clock, seconds=1.5)
    ledger = Ledger(ROOMY, clock=clock)
    through = ledger.sandbox(sandbox)

    ran = run_in(through)
    through.save_home("a-commit", ARCHIVE)
    through.restore_home("a-commit", ARCHIVE)

    # A run for no Claim is given its limits as they are.
    assert sandbox.uses == [
        ("run", ("pytest",), "a-commit", Phase.TEST, LIMITS),
        ("save_home", "a-commit", ARCHIVE),
        ("restore_home", "a-commit", ARCHIVE),
    ]
    assert (ran, ledger.spent()) == (RAN, BudgetSpend(seconds=4.5, dollars=0.0, steps=0))


def test_run_for_a_claim_is_given_no_longer_than_is_left_of_its_share_or_of_the_budget() -> None:
    clock = Clock()
    sandbox = TimedSandbox(clock, seconds=20.0)
    ledger = Ledger(Budget(seconds=100.0), clock=clock)
    ledger.share_out(2)
    for_claim = ledger.sandbox(sandbox, claim=0)

    run_in(for_claim)  # All 50 seconds of the share are left.
    run_in(for_claim, replace(LIMITS, seconds=10.0))  # 30 are left, and the limit is never raised.
    ledger.record(BudgetSpend(seconds=55.0, dollars=0.0, steps=0))
    run_in(for_claim)  # 10 are left of the share, and 5 of the Budget.

    allowed = [replace(LIMITS, seconds=seconds) for seconds in (50.0, 10.0, 5.0)]
    assert [use[-1] for use in sandbox.uses] == allowed
    assert ledger.spent(claim=0) == BudgetSpend(seconds=60.0, dollars=0.0, steps=0)


def test_use_of_a_sandbox_that_fails_still_has_its_time_recorded() -> None:
    clock = Clock()
    failure = SandboxFailed("no container runtime")
    ledger = Ledger(ROOMY, clock=clock)

    with pytest.raises(SandboxFailed) as raised:
        run_in(ledger.sandbox(TimedSandbox(clock, seconds=2.0, failure=failure)))

    assert raised.value is failure
    assert ledger.spent() == BudgetSpend(seconds=2.0, dollars=0.0, steps=0)


USES: list[Callable[[SandboxWithHomes], object]] = [
    run_in,
    lambda sandbox: sandbox.save_home("a-commit", ARCHIVE),
    lambda sandbox: sandbox.restore_home("a-commit", ARCHIVE),
]


@pytest.mark.parametrize("use", USES, ids=["run", "save_home", "restore_home"])
def test_once_a_limit_is_reached_the_sandbox_is_not_used_again(
    use: Callable[[SandboxWithHomes], object],
) -> None:
    clock = Clock()
    sandbox = TimedSandbox(clock)
    ledger = Ledger(Budget(steps=4), clock=clock)
    ledger.share_out(2)

    ledger.record(steps(2), claim=0)
    with pytest.raises(BudgetSpent) as for_the_share:
        use(ledger.sandbox(sandbox, claim=0))
    use(ledger.sandbox(sandbox, claim=1))
    ledger.record(steps(2), claim=1)
    with pytest.raises(BudgetSpent) as for_the_budget:
        use(ledger.sandbox(sandbox))

    assert (for_the_share.value.claim, for_the_budget.value.claim) == (0, None)
    assert len(sandbox.uses) == 1


def test_model_or_sandbox_for_a_claim_no_share_was_set_aside_for_is_refused_at_once() -> None:
    clock = Clock()
    ledger = Ledger(ROOMY)
    ledger.share_out(2)

    with pytest.raises(ValueError):
        ledger.model(LocalModel(clock), claim=2)
    with pytest.raises(ValueError):
        ledger.sandbox(TimedSandbox(clock), claim=2)


# The Budget the environment configures


@pytest.mark.parametrize(
    "environment",
    [{}, {SECONDS_VARIABLE: "", DOLLARS_VARIABLE: "", STEPS_VARIABLE: ""}],
    ids=["nothing set", "set but empty"],
)
def test_budget_is_the_provisional_defaults_unless_the_environment_says_otherwise(
    environment: dict[str, str],
) -> None:
    assert budget_from_environment(environment) == Budget()


@pytest.mark.parametrize(
    ("environment", "budget"),
    [
        (
            {SECONDS_VARIABLE: "120.5", DOLLARS_VARIABLE: "0.5", STEPS_VARIABLE: "40"},
            Budget(seconds=120.5, dollars=0.5, steps=40),
        ),
        ({SECONDS_VARIABLE: "60"}, Budget(seconds=60.0)),
        ({DOLLARS_VARIABLE: "10"}, Budget(dollars=10.0)),
        ({STEPS_VARIABLE: "5"}, Budget(steps=5)),
    ],
    ids=["all three", "seconds alone", "dollars alone", "steps alone"],
)
def test_each_limit_is_read_from_its_own_environment_variable(
    environment: dict[str, str], budget: Budget
) -> None:
    assert budget_from_environment(environment) == budget


@pytest.mark.parametrize(
    ("variable", "value"),
    [
        (SECONDS_VARIABLE, "soon"),
        (SECONDS_VARIABLE, "0"),
        (SECONDS_VARIABLE, "inf"),
        (SECONDS_VARIABLE, "nan"),
        (DOLLARS_VARIABLE, "free"),
        (DOLLARS_VARIABLE, "-3"),
        (DOLLARS_VARIABLE, "inf"),
        (STEPS_VARIABLE, "many"),
        (STEPS_VARIABLE, "0"),
        (STEPS_VARIABLE, "2.5"),
    ],
)
def test_limit_the_budget_cannot_have_is_refused_naming_the_variable_at_fault(
    variable: str, value: str
) -> None:
    with pytest.raises(InvalidBudget, match=variable):
        budget_from_environment({variable: value})
