"""The Budget: the fixed limit on the effort spent on one pull request, and the ledger that
keeps to it.

A Budget is measured in time, money, and steps. The ledger records what is spent, for the whole
pull request and for each Claim, and says when a limit has been reached. The limits are enforced
here, by code: no model is asked whether any Budget is left, and nothing a model says changes a
limit or what has been recorded (ADR 0003).
"""

import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from pr_verification.model import Model
from pr_verification.report import BudgetLimit, BudgetSpend
from pr_verification.sandbox import SandboxWithHomes

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
    """The time limit: the most seconds of work."""

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
        raise NotImplementedError


class Ledger:
    """What has been spent on one pull request, held against its Budget and each Claim's share.

    A ledger is for one pull request and starts with nothing spent. A Claim is named by its
    place among the Claims of the pull request, counted from 0.

    What is recorded
        Every spend is recorded against the whole pull request. A spend made for one Claim is
        recorded against that Claim as well. A spend made for no one Claim, such as listing
        the Claims or installing dependencies, is recorded against the pull request alone. So
        what was spent on the whole pull request is always the sum of what was recorded against
        each Claim and what was recorded against no Claim: exactly in steps, and to within the
        rounding of a floating-point sum in seconds and dollars.

    When a limit is reached
        A limit is reached when what has been recorded in its unit is the limit or more. The
        Budget's limits are held against what was recorded for the whole pull request, and
        the limits of a Claim's share against what was recorded against that Claim.

        The Budget is spent once one of its limits is reached, and a share likewise. The limit
        that stopped the work is the first one reached, and it stays so. When one spend
        reaches more than one limit at once, the first of them in the order time, money, steps
        is taken.

        A spend is recorded whole, even when it passes a limit: what was spent was spent. So
        what is recorded can pass a limit by no more than the one piece of work that reached it.

    What stops
        Once the Budget is spent, nothing more is recorded and no further work is done through
        the ledger, for any Claim or for none. Once a Claim's share is spent, the same holds
        for that Claim alone: work for the other Claims, and for no one Claim, goes on.
    """

    def __init__(self, budget: Budget | None = None, *, clock: Clock = time.monotonic) -> None:
        """`budget` is the Budget to keep to, and `Budget()` when None. `clock` times the work
        done through `model` and `sandbox`, and the ledger reads it at no other time.

        Raises `ValueError` when a limit of `budget` is not more than 0.
        """
        raise NotImplementedError

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
        raise NotImplementedError

    def record(self, spend: BudgetSpend, *, claim: int | None = None) -> None:
        """Record one spend: against Claim `claim`, or against no Claim when `claim` is None.

        Raises `BudgetSpent`, having recorded nothing, when a limit had already been reached
        before the call: the Budget's, whatever `claim` is, or else that of the share of Claim
        `claim`. So for any spends recorded in any order, nothing is recorded after the Budget
        is spent, and nothing against a Claim after its share is.

        Raises `ValueError`, having recorded nothing, when an amount in `spend` is not 0 or
        more, and when `claim` names no Claim a share was set aside for. This is looked at
        first: a spend that could never be recorded is a `ValueError` whatever has been spent.
        """
        raise NotImplementedError

    def spent(self, *, claim: int | None = None) -> BudgetSpend:
        """What has been recorded so far: for the whole pull request when `claim` is None, and
        against Claim `claim` otherwise. For a Claim no share was set aside for, that is
        nothing: 0.0 seconds, 0.0 dollars, and 0 steps."""
        raise NotImplementedError

    def stopped_by(self, *, claim: int | None = None) -> BudgetLimit | None:
        """The limit that was reached first: of the Budget when `claim` is None, and of the
        share of Claim `claim` otherwise. None while none has been reached, and for a Claim no
        share was set aside for.

        The share of a Claim says nothing of the Budget: when the Budget is spent and a Claim's
        share is not, this is None for that Claim.
        """
        raise NotImplementedError

    def model(self, model: Model, *, claim: int | None = None) -> Model:
        """A model that asks `model` through the ledger: each request is counted, and none is
        made once nothing is left to spend.

        Each call to `complete` on the model returned:

        1. Raises `BudgetSpent`, without asking `model`, when the Budget is spent, or else
           when the share of Claim `claim` is.
        2. Otherwise asks `model` once, with the request exactly as given: through
           `complete_priced` when `model` is a `PricedModel`, and through `complete` when not.
        3. Records one spend against `claim`, as `record` does. Its `seconds` are the time
           between a reading of the clock just before `model` is asked and one just after. Its
           `dollars` are those of the `PricedResponse`, and 0.0 for a model that is no
           `PricedModel`: it has no price, so it costs time and no money, as a local model
           does. Its `steps` are 1: each request is one step, whatever the model.
        4. Returns the response exactly as `model` gave it.

        When `model` raises, the request is recorded all the same, with its seconds, one step,
        and no dollars, and what was raised is raised from here.

        Any number of models can be asked through one ledger. Each request is charged at the
        price of the model that was asked, so the dollars recorded are summed across models at
        each model's price.

        Raises `ValueError`, from this call and not from a later request, when `claim` names
        no Claim a share was set aside for.
        """
        raise NotImplementedError

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
        3. Records one spend against `claim`, as `record` does: the seconds between a reading
           of the clock just before `sandbox` is asked and one just after, with no dollars and
           no steps. Running code costs time only.
        4. Returns what `sandbox` returned.

        When `sandbox` raises, the time is recorded all the same, and what was raised is
        raised from here.

        Raises `ValueError`, from this call and not from a later use, when `claim` names no
        Claim a share was set aside for.
        """
        raise NotImplementedError


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
    raise NotImplementedError
