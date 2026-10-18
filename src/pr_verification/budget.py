"""The Budget: the fixed limit on the effort spent on one pull request, and the ledger that
keeps to it.

A Budget is measured in time, money, and steps. The ledger records what is spent, for the whole
pull request and for each Claim, and says when a limit has been reached. The limits are enforced
here, by code: no model is asked whether any Budget is left, and nothing a model says changes a
limit or what has been recorded (ADR 0003).
"""

import math
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from pr_verification.model import (
    Model,
    ModelRequest,
    PricedModel,
    PricedResponse,
    ask,
    ask_priced,
    offers_parallel_tool_calls,
)
from pr_verification.report import BudgetLimit, BudgetSpend
from pr_verification.sandbox import (
    Command,
    Limits,
    Phase,
    RunResult,
    SandboxWithHomes,
    run_in,
)

DEFAULT_SECONDS = 900.0
"""The time limit when none is configured: fifteen minutes."""

DEFAULT_DOLLARS = 3.0
"""The money limit when none is configured: a few dollars."""

DEFAULT_STEPS = 300
"""The step limit when none is configured."""


@dataclass(frozen=True)
class Budget:
    """The limits on the effort spent on one pull request. The first one reached stops the work.

    Each limit is more than 0. The defaults are provisional: they are to be set again from the
    first baseline measurements.
    """

    seconds: float = DEFAULT_SECONDS
    """The time limit: the most seconds on the clock during which work for the pull request
    is under way. Work done at the same time counts once against it. A Claim's share of it is
    in seconds of work, in which work done at the same time each counts (`Ledger`)."""

    dollars: float = DEFAULT_DOLLARS
    """The money limit: the most dollars, summed across every model at each model's price."""

    steps: int = DEFAULT_STEPS
    """The step limit: the most steps. A step is one request made to a model through the
    ledger."""


Clock = Callable[[], float]
"""Says what time it is, in seconds. Only the difference between two readings is used, so where
it counts from does not matter. `time.monotonic` is one."""

BUDGET_SPENT = (
    "The Budget for the pull request was spent while this Claim was still unresolved: the "
    "{limit} limit was reached."
)
"""Why a Claim is Inconclusive when the Budget was spent before Evidence that could decide the
Claim was gathered. `limit` is the value of the `BudgetLimit` that stopped the run: `time`,
`money`, or `steps`."""

SHARE_SPENT = (
    "This Claim's share of the Budget was spent while the Claim was still unresolved: the "
    "{limit} limit of its share was reached."
)
"""Why a Claim is Inconclusive when its own share of the Budget was spent before Evidence that
could decide the Claim was gathered. `limit` is the value of the `BudgetLimit` of the share
that was reached."""


class BudgetSpent(Exception):
    """Work was asked for after a limit had been reached, so it was not done.

    Its message is `BUDGET_SPENT` filled in when `claim` is None, and `SHARE_SPENT` filled in
    otherwise. It is written by the project and holds no Untrusted Content.
    """

    limit: BudgetLimit
    """The limit that had been reached."""

    claim: int | None
    """None when the limit is the Budget's own, which stops all work. Otherwise the Claim whose
    share was spent, which stops the work on that Claim alone."""

    def __init__(self, limit: BudgetLimit, claim: int | None = None) -> None:
        message = BUDGET_SPENT if claim is None else SHARE_SPENT
        super().__init__(message.format(limit=limit.value))
        self.limit = limit
        self.claim = claim


_NOTHING = BudgetSpend(seconds=0.0, dollars=0.0, steps=0)


def _reached(spent: BudgetSpend, limits: Budget) -> BudgetLimit | None:
    """The first limit, in the order time, money, steps, that `spent` is at or past."""
    if spent.seconds >= limits.seconds:
        return BudgetLimit.TIME
    if spent.dollars >= limits.dollars:
        return BudgetLimit.MONEY
    if spent.steps >= limits.steps:
        return BudgetLimit.STEPS
    return None


class Ledger:
    """What has been spent on one pull request, held against its Budget and each Claim's share.

    A ledger is for one pull request and starts with nothing spent. A Claim is named by its
    place among the Claims of the pull request, counted from 0.

    What is recorded
        Every spend is recorded against the whole pull request. A spend made for one Claim is
        recorded against that Claim as well. A spend made for no one Claim, such as listing
        the Claims or installing dependencies, is recorded against the pull request alone.

        In dollars and steps, what was spent on the whole pull request is always the sum of
        what was recorded against each Claim and what was recorded against no Claim: exactly
        in steps, and to within the rounding of a floating-point sum in dollars.

        In seconds, the two are measured differently. What is recorded against a Claim, and
        against no Claim, is seconds of work: each piece of work counts in full, though others
        were done at the same time. What is recorded against the whole pull request is seconds
        on the clock during which work was under way: a piece of work done through `model` or
        `sandbox` is under way from the reading of the clock just before it is started to the
        one just after it ends, and time in which two or more were under way counts once. Time
        in which none was under way is not counted. The seconds of a spend given to `record`
        were not timed by the ledger, so they count in full against the pull request as well.

        So the seconds of the pull request are never more than the sum of the seconds recorded
        against each Claim and against no Claim, and they are that sum, to within the rounding
        of a floating-point sum, when one piece of work is done at a time. They are brought up
        to date each time a piece of work ends: they are then the time up to that reading of
        the clock during which work was under way, and the seconds of every spend given to
        `record`. Work still under way when they are read has counted only up to the last
        piece of work that ended.

    When a limit is reached
        A limit is reached when what has been recorded in its unit is the limit or more. The
        Budget's limits are held against what was recorded for the whole pull request, and
        the limits of a Claim's share against what was recorded against that Claim. So the
        Budget's time limit is held against seconds on the clock, and that of a share against
        seconds of work.

        The Budget is spent once one of its limits is reached, and a share likewise. The limit
        that stopped the work is the first one reached, and it stays so, unless shares are
        moved, as `move_share` says. When one spend reaches more than one limit at once,
        the first of them in the order time, money, steps is taken.

        A spend is recorded whole, even when it passes a limit: what was spent was spent. So
        what is recorded can pass a limit by no more than the one piece of work that reached
        it, and by the other pieces of work under way at that moment when several are done at
        the same time.

    What stops
        Once the Budget is spent, nothing more is recorded and no further work is done through
        the ledger, for any Claim or for none, but for work under way, as said below. Once a
        Claim's share is spent, the same holds for that Claim alone: work for the other Claims,
        and for no one Claim, goes on.

    At the same time
        One ledger may be used from several threads at once, as it is when several roles work
        at the same time. Each spend is recorded whole and each move is made whole, one after
        another, so the sums hold whatever the threads do. Each piece of work is timed on its
        own, so two pieces of work done at the same time both count in full against their
        Claims, or against no Claim, and once against the pull request, as said above.

        Work is refused when it is asked for, and never when it ends. A piece of work that
        `model` or `sandbox` had started before a limit was reached is let end, though other
        work reached the limit meanwhile: it is recorded whole, and what it gave is returned,
        or what it raised is raised. That is the one case in which something is recorded
        after a limit is reached, and it cannot arise while one piece of work is done at a
        time. `record` itself refuses as it says.
    """

    def __init__(self, budget: Budget | None = None, *, clock: Clock = time.monotonic) -> None:
        """`budget` is the Budget to keep to, and `Budget()` when None. `clock` times the work
        done through `model` and `sandbox`, and the ledger reads it at no other time.

        Raises `ValueError` when a limit of `budget` is not more than 0.
        """
        budget = Budget() if budget is None else budget
        if not (budget.seconds > 0 and budget.dollars > 0 and budget.steps > 0):
            raise ValueError(f"a limit of the Budget is not more than 0: {budget}")
        self._budget = budget
        self._clock = clock
        self._spent = _NOTHING
        # The limit of the Budget that was reached first. It stays so whatever is recorded
        # later for work that was under way.
        self._stopped: BudgetLimit | None = None
        # The share of each Claim, with what was recorded against it. They start the same.
        self._shares: list[Budget] = []
        self._claims: list[BudgetSpend] = []
        # Held while a spend is recorded or a move is made, so that each is made whole.
        self._lock = threading.Lock()

    def share_out(self, claims: int) -> None:
        """Set aside a share of the Budget for each of `claims` Claims.

        Every share is the same. For each limit it is what is left of that limit now, which is
        the limit less what has been recorded for the whole pull request, divided by `claims`:
        exactly for time and money, and rounded down to a whole number for steps, though never
        to less than 1 step. So what was spent before the shares are set aside comes out of no
        Claim's share, and one Claim that spends its whole share takes nothing from the share
        of another, but for one case.

        With fewer steps left than there are Claims, each share is 1 step, and the shares come
        to more steps than are left. The Budget's own limit still holds, so the Claims worked
        on first take the steps there are. No Claim is left without an attempt while a step
        remains.

        Shares are set aside once for a pull request. Until they are, nothing can be recorded
        against a Claim.

        Raises `ValueError` when `claims` is less than 1, and when shares have already been set
        aside. Otherwise raises `BudgetSpent` when the Budget is spent, since nothing is left
        to share.
        """
        if claims < 1:
            raise ValueError(f"shares cannot be set aside for {claims} Claims")
        if self._shares:
            raise ValueError("the shares of the Budget have already been set aside")
        self._refuse_once_spent(None)
        share = Budget(
            seconds=(self._budget.seconds - self._spent.seconds) / claims,
            dollars=(self._budget.dollars - self._spent.dollars) / claims,
            steps=max(1, (self._budget.steps - self._spent.steps) // claims),
        )
        self._shares = [share] * claims
        self._claims = [_NOTHING] * claims

    def record(self, spend: BudgetSpend, *, claim: int | None = None) -> None:
        """Record one spend: against Claim `claim`, or against no Claim when `claim` is None.
        Its seconds count in full against the whole pull request as well, since the ledger did
        not time them.

        Raises `BudgetSpent`, having recorded nothing, when a limit had already been reached
        before the call: the Budget's, whatever `claim` is, or else that of the share of Claim
        `claim`. So for any spends recorded in any order, nothing is recorded after the Budget
        is spent, and nothing against a Claim after its share is.

        Raises `ValueError`, having recorded nothing, when an amount in `spend` is not 0 or
        more, and when `claim` names no Claim a share was set aside for. This is looked at
        first: a spend that could never be recorded is a `ValueError` whatever has been spent.
        """
        _require_amounts(spend)
        self._require_share(claim)
        with self._lock:
            self._refuse_once_spent(claim)
            self._add(spend, claim)

    def spent(self, *, claim: int | None = None) -> BudgetSpend:
        """What has been recorded so far: for the whole pull request when `claim` is None, and
        against Claim `claim` otherwise. For a Claim no share was set aside for, that is
        nothing: 0.0 seconds, 0.0 dollars, and 0 steps.

        The seconds of the whole pull request are seconds on the clock during which work was
        under way, and those of a Claim seconds of work, as the ledger says."""
        if claim is None:
            return self._spent
        return self._claims[claim] if self._has_share(claim) else _NOTHING

    def stopped_by(self, *, claim: int | None = None) -> BudgetLimit | None:
        """The limit that was reached first: of the Budget when `claim` is None, and of the
        share of Claim `claim` otherwise. None while none has been reached, and for a Claim no
        share was set aside for.

        The share of a Claim says nothing of the Budget: when the Budget is spent and a Claim's
        share is not, this is None for that Claim.
        """
        if claim is None:
            return self._stopped
        if not self._has_share(claim):
            return None
        return _reached(self._claims[claim], self._shares[claim])

    def left(self, *, claim: int | None = None) -> BudgetSpend:
        """What is left to spend: of the Budget when `claim` is None, and of the share of
        Claim `claim` otherwise.

        Each amount is the limit in that unit less what has been recorded against it, and 0
        when what was recorded is past the limit. So an amount is never less than 0, and one
        that is 0 means that limit has been reached. For a Claim no share was set aside for,
        nothing is left: 0.0 seconds, 0.0 dollars, and 0 steps.

        What is left of a Claim's share says nothing of the Budget, and can be more than is
        left of the Budget.
        """
        if claim is None:
            limits, spent = self._budget, self._spent
        elif self._has_share(claim):
            limits, spent = self._shares[claim], self._claims[claim]
        else:
            return _NOTHING
        return BudgetSpend(
            seconds=max(0.0, limits.seconds - spent.seconds),
            dollars=max(0.0, limits.dollars - spent.dollars),
            steps=max(0, limits.steps - spent.steps),
        )

    def move_share(self, amount: BudgetSpend, *, source: int, destination: int) -> None:
        """Move `amount` from the share of Claim `source` to the share of Claim `destination`.

        `amount` holds how much to move in each unit, and is no spend: nothing is recorded.

        Each limit of the share of `source` is lowered by the amount in its unit, and the same
        limit of the share of `destination` is raised by it. Nothing recorded changes, and nor
        does any limit of the Budget or of any other share. So for any moves made in any
        order, the limits of all the shares together come to what they came to when the
        shares were set aside, exactly in steps and to within the rounding of a floating-point
        sum in seconds and dollars: moving shares never makes more to spend, and the Budget's
        own limits hold whatever is moved.

        Only what is left can be moved, so no limit of a share is ever lowered below what has
        been recorded against it. A share that everything left in one unit is moved out of
        has reached that limit and is spent. A share that was spent and is given more is
        spent no longer, once what has been recorded against it is under every one of its
        limits again: `stopped_by(claim=destination)` is then None, and work for that Claim
        can go on. After a move, `stopped_by` for either Claim is the first, in the order
        time, money, steps, of the limits its share has reached as the share now is.

        Raises `ValueError`, having moved nothing, when:

        - `source` and `destination` are the same Claim, or either names no Claim a share
          was set aside for;
        - an amount in `amount` is not a finite number that is 0 or more, or every one of
          them is 0;
        - an amount is more than `left(claim=source)` has in its unit, so that more would be
          moved than is there.

        This is looked at first. Otherwise raises `BudgetSpent`, having moved nothing, when
        the Budget is spent.
        """
        if source == destination or not (self._has_share(source) and self._has_share(destination)):
            raise ValueError(f"no share can be moved from Claim {source} to Claim {destination}")
        amounts = (amount.seconds, amount.dollars, amount.steps)
        # Compared, since `math.isfinite` raises for a whole number larger than any float.
        if not all(0 <= moved < math.inf for moved in amounts) or not any(amounts):
            raise ValueError(
                f"an amount to move is not a finite number, or none is over 0: {amount}"
            )
        with self._lock:
            left = self.left(claim=source)
            if (
                amount.seconds > left.seconds
                or amount.dollars > left.dollars
                or amount.steps > left.steps
            ):
                raise ValueError(f"{amount} is more than Claim {source} has left: {left}")
            self._refuse_once_spent(None)
            taken_from, given_to = self._shares[source], self._shares[destination]
            self._shares[source] = Budget(
                seconds=taken_from.seconds - amount.seconds,
                dollars=taken_from.dollars - amount.dollars,
                steps=taken_from.steps - amount.steps,
            )
            self._shares[destination] = Budget(
                seconds=given_to.seconds + amount.seconds,
                dollars=given_to.dollars + amount.dollars,
                steps=given_to.steps + amount.steps,
            )

    def model(self, model: Model, *, claim: int | None = None) -> Model:
        """A model that asks `model` through the ledger: each request is counted, and none is
        made once nothing is left to spend.

        Each call to `complete` on the model returned:

        1. Raises `BudgetSpent`, without asking `model`, when the Budget is spent, or else
           when the share of Claim `claim` is.
        2. Otherwise asks `model` once, with the request exactly as given: through
           `complete_priced` when `model` is a `PricedModel`, and through `complete` when not.
        3. Records one spend against `claim`, as `record` does, though it is recorded
           whatever limit other work reached meanwhile, and its seconds count against the
           whole pull request as work done at the same time does, as the ledger says. Its
           `seconds` are the time between a reading of the clock just before `model` is asked
           and one just after. Its
           `dollars` are those of the `PricedResponse`, and 0.0 for a model that is no
           `PricedModel`: it has no price, so it costs time and no money, as a local model
           does. Its `steps` are 1: each request is one step, whatever the model.
        4. Returns the response exactly as `model` gave it.

        When `model` raises, the request is recorded all the same, with its seconds, one step,
        and no dollars, and what was raised is raised from here.

        Any number of models can be asked through one ledger. Each request is charged at the
        price of the model that was asked, so the dollars recorded are summed across models at
        each model's price.

        The model returned offers parallel tool calls when, and only when, `model` does, as
        `offers_parallel_tool_calls` (`pr_verification.model`) reads it. Prompt caching needs
        nothing here: the request reaches `model` exactly as given, with its
        `cache_instructions`.

        Raises `ValueError`, from this call and not from a later request, when `claim` names
        no Claim a share was set aside for.
        """
        self._require_share(claim)
        return _ModelThroughLedger(self, model, claim)

    def sandbox(self, sandbox: SandboxWithHomes, *, claim: int | None = None) -> SandboxWithHomes:
        """A Sandbox that uses `sandbox` through the ledger: each use is timed, and none is
        made once nothing is left to spend.

        Each call to `run`, `save_home`, or `restore_home` on the Sandbox returned:

        1. Raises `BudgetSpent`, without asking `sandbox`, when the Budget is spent, or else
           when the share of Claim `claim` is.
        2. Otherwise asks `sandbox` once, with what it was given, but for one thing. A `run`
           for a Claim is given no longer than the time that is left: its `limits` are as
           given, except that `seconds` is lowered to what is left of the time limit of the
           Claim's share, or of the Budget's when less is left of that. It is never raised.
           So one run cannot take a Claim far past its share. A `run` for no Claim is given
           its `limits` as they are: an install cut short would leave nothing for a later run
           to use.
        3. Records one spend against `claim`, as `record` does, though it is recorded
           whatever limit other work reached meanwhile, and its seconds count against the
           whole pull request as work done at the same time does, as the ledger says: the
           seconds between a reading of the clock just before `sandbox` is asked and one just
           after, with no dollars and no steps. Running code costs time only.
        4. Returns what `sandbox` returned.

        When `sandbox` raises, the time is recorded all the same, and what was raised is
        raised from here.

        Raises `ValueError`, from this call and not from a later use, when `claim` names no
        Claim a share was set aside for.
        """
        self._require_share(claim)
        return _SandboxThroughLedger(self, sandbox, claim)

    def _do[Done](
        self,
        work: Callable[[], Done],
        *,
        claim: int | None,
        steps: int,
        dollars: Callable[[Done], float] | None = None,
    ) -> Done:
        """Do one piece of work through the ledger, as `model` and `sandbox` say: it is not
        done once nothing is left to spend, and is otherwise timed and recorded, whether it
        raises or not. `dollars` says what work that was done cost."""
        self._refuse_once_spent(claim)
        cost = 0.0
        started = self._clock()
        try:
            done = work()
            if dollars is not None:
                cost = dollars(done)
            return done
        finally:
            # Work that was started is recorded whole, whatever limit was reached meanwhile.
            spend = BudgetSpend(self._clock() - started, cost, steps)
            _require_amounts(spend)
            with self._lock:
                self._add(spend, claim)

    def _no_longer_than_is_left(self, limits: Limits, claim: int | None) -> Limits:
        """The limits of a run for `claim`, as `sandbox` says: as given for no Claim, and for
        a Claim with `seconds` lowered to what is left of the time limit of its share, or of
        the Budget's when less is left of that."""
        if claim is None:
            return limits
        left = min(
            self._shares[claim].seconds - self._claims[claim].seconds,
            self._budget.seconds - self._spent.seconds,
        )
        # Other work done at the same time may have taken the last of it since the run was
        # let go ahead. The run is then given no time, and never less than none.
        return replace(limits, seconds=min(limits.seconds, max(0.0, left)))

    def _add(self, spend: BudgetSpend, claim: int | None) -> None:
        """Add one spend to what is recorded, as `record` says. The lock is held."""
        self._spent = _sum(self._spent, spend)
        if self._stopped is None:
            self._stopped = _reached(self._spent, self._budget)
        if claim is not None:
            self._claims[claim] = _sum(self._claims[claim], spend)

    def _has_share(self, claim: int) -> bool:
        return 0 <= claim < len(self._claims)

    def _require_share(self, claim: int | None) -> None:
        if claim is not None and not self._has_share(claim):
            raise ValueError(f"no share of the Budget was set aside for Claim {claim}")

    def _refuse_once_spent(self, claim: int | None) -> None:
        """Raise `BudgetSpent` when the Budget is spent, or else when the share of `claim`
        is."""
        limit = self.stopped_by()
        if limit is not None:
            raise BudgetSpent(limit)
        if claim is not None:
            limit = self.stopped_by(claim=claim)
            if limit is not None:
                raise BudgetSpent(limit, claim)


def _require_amounts(spend: BudgetSpend) -> None:
    """Raise `ValueError` when an amount in `spend` is not 0 or more."""
    if not (spend.seconds >= 0 and spend.dollars >= 0 and spend.steps >= 0):
        raise ValueError(f"an amount of a spend is not 0 or more: {spend}")


def _sum(one: BudgetSpend, other: BudgetSpend) -> BudgetSpend:
    return BudgetSpend(
        seconds=one.seconds + other.seconds,
        dollars=one.dollars + other.dollars,
        steps=one.steps + other.steps,
    )


class _ModelThroughLedger:
    """The model `Ledger.model` gives."""

    def __init__(self, ledger: Ledger, model: Model, claim: int | None) -> None:
        self._ledger, self._model, self._claim = ledger, model, claim
        self.parallel_tool_calls = offers_parallel_tool_calls(model)

    def complete(self, request: ModelRequest) -> str:
        def priced() -> PricedResponse:
            if isinstance(self._model, PricedModel):
                return ask_priced(self._model, request)
            return PricedResponse(ask(self._model, request), dollars=0.0)

        answered = self._ledger._do(
            priced, claim=self._claim, steps=1, dollars=lambda answered: answered.dollars
        )
        return answered.response


class _SandboxThroughLedger:
    """The Sandbox `Ledger.sandbox` gives."""

    def __init__(self, ledger: Ledger, sandbox: SandboxWithHomes, claim: int | None) -> None:
        self._ledger, self._sandbox, self._claim = ledger, sandbox, claim

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        def within_what_is_left() -> RunResult:
            # Worked out once the run is known to go ahead, so there is time left to give it.
            within = self._ledger._no_longer_than_is_left(limits, self._claim)
            return run_in(self._sandbox, command, commit=commit, phase=phase, limits=within)

        return self._timed(within_what_is_left)

    def save_home(self, commit: str, archive: Path) -> None:
        self._timed(lambda: self._sandbox.save_home(commit, archive))

    def restore_home(self, commit: str, archive: Path) -> None:
        self._timed(lambda: self._sandbox.restore_home(commit, archive))

    def _timed[Done](self, use: Callable[[], Done]) -> Done:
        return self._ledger._do(use, claim=self._claim, steps=0)


class InvalidBudget(Exception):
    """The Budget is configured with a limit it cannot have. Its message is safe to print: it
    names the variable at fault."""


SECONDS_VARIABLE = "PR_VERIFICATION_BUDGET_SECONDS"
DOLLARS_VARIABLE = "PR_VERIFICATION_BUDGET_DOLLARS"
STEPS_VARIABLE = "PR_VERIFICATION_BUDGET_STEPS"


def budget_from_environment(environment: Mapping[str, str]) -> Budget:
    """Read the Budget from three environment variables.

    A variable that is unset or set to the empty string takes its default.

    - `PR_VERIFICATION_BUDGET_SECONDS` is the time limit, a number of seconds. The default is
      `DEFAULT_SECONDS`.
    - `PR_VERIFICATION_BUDGET_DOLLARS` is the money limit, a number of dollars. The default is
      `DEFAULT_DOLLARS`.
    - `PR_VERIFICATION_BUDGET_STEPS` is the step limit, a whole number of steps. The default
      is `DEFAULT_STEPS`.

    Raises `InvalidBudget`, with a message that names the variable at fault, for a limit that is
    not a number, is not finite, or is not more than 0, and for a step limit that is not a
    whole number, as `2.5` is not.
    """
    return Budget(
        seconds=_limit(environment, SECONDS_VARIABLE, float, DEFAULT_SECONDS),
        dollars=_limit(environment, DOLLARS_VARIABLE, float, DEFAULT_DOLLARS),
        steps=_limit(environment, STEPS_VARIABLE, int, DEFAULT_STEPS),
    )


def _limit[Number: (int, float)](
    environment: Mapping[str, str], variable: str, read: Callable[[str], Number], default: Number
) -> Number:
    """The limit `variable` sets, as `read` reads a number, or `default` when it sets none."""
    text = environment.get(variable, "")
    if not text:
        return default
    try:
        limit = read(text)
    except ValueError:
        limit = None
    # Compared, since `math.isfinite` raises for a whole number larger than any float.
    if limit is None or not 0 < limit < math.inf:
        number = "a whole number" if read is int else "a number"
        raise InvalidBudget(f"{variable} is set to {text!r}, and must be {number} more than 0")
    return limit
