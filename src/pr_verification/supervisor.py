"""The LLM supervisor: the orchestration in which a model decides how effort is spent.

The supervisor is the second implementation of the orchestration boundary (ADR 0003). A model
chooses which Claim is worked on, in what order, how many at once, and with how much of the
Budget. Its power ends there. It is shown structured Claims and structured summaries of
results, and never raw Untrusted Content. It gathers Evidence and decides nothing: each Verdict
is computed by `compute_verdict` and the Gate by `compute_gate`, in the core's entry point,
whatever the supervisor says. Its step cap, the Budget, and the rule against finishing early
are enforced here, by code, from outside the model.
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pr_verification.budget import Ledger
from pr_verification.decision import Decider
from pr_verification.model import Model
from pr_verification.orchestration import FixedPipeline, Gathered
from pr_verification.report import ClaimKind, Verdict

SUPERVISOR_STEP_CAP = 24
"""The most steps the supervisor may take for one pull request unless given another. A step is
one request to the supervising model and what comes of its response."""

DISPATCH_CAP = 4
"""The most dispatches the supervisor may make in one step, and so the most roles at work at
the same time."""

CLAIMS_SHOWN_CAP = 12
"""The most Claims the supervisor is shown in one request, however many the pull request
has."""

CLAIM_TEXT_CAP = 300
"""The most characters of a Claim's text the supervisor is shown."""

SUPERVISOR_UNREADABLE = (
    "That response was not a request the supervisor may make, so nothing was done. Answer with "
    "one JSON object that holds exactly one of dispatch, move, and finish."
)
"""What the supervisor is told after a response that could not be read."""

DISPATCH_REFUSED = (
    "Nothing was dispatched. A step may hold from 1 to {cap} dispatches, each for a different "
    "Claim of the pull request and with a strategy listed for that Claim."
)
"""What the supervisor is told after a dispatch that was refused. `cap` is `DISPATCH_CAP`."""

MOVE_REFUSED = (
    "Nothing was moved. A move takes from the share of a Claim that is not still to have a "
    "Verification attempt, gives to another Claim of the pull request, and takes no more than "
    "is left of the share it takes from."
)
"""What the supervisor is told after a move that was refused."""

FINISH_REFUSED = (
    "The run cannot finish yet. Claims that have had no Verification attempt: {unattempted}. "
    "The Budget is not spent."
)
"""What the supervisor is told after a finish that was refused. `unattempted` is how many
Claims are still to have a Verification attempt."""


class Strategy(StrEnum):
    """A way of gathering Evidence for one Claim that the supervisor may ask for.

    `strategies_for` says which of them a Claim may be given.
    """

    REPRODUCTION = "reproduction"
    """Write a Reproduction and have the Reproduction judge decide on it, as
    `write_judged_reproduction` does."""

    READING = "reading"
    """Read the code before and after the pull request, as `verify_structural_claim` does."""


def strategies_for(kind: ClaimKind | None) -> tuple[Strategy, ...]:
    """The strategies the supervisor may choose between for a Claim of `kind`.

    - `(Strategy.REPRODUCTION,)` for a Behavioural Claim, and for a Claim that was not
      classified, for which `kind` is None and which is verified as a Behavioural Claim.
    - `(Strategy.READING,)` for a Structural Claim.

    So each kind has one strategy, and what the supervisor chooses is which Claim is worked
    on, when, alongside which others, with how much of the Budget, and whether to try again.

    This is the rule the project owner settled for the supervisor on 2026-10-12. A
    Reproduction stays a test written by the system, and nothing else can decide a
    Behavioural Claim. Running the repository's existing tests on both commits, for a Claim
    that behaviour did not change, is not a strategy, and nor is running a benchmark, for a
    Claim about performance. Such a Claim goes on to a Reproduction like any other
    Behavioural Claim, and is Inconclusive unless a Reproduction that the Reproduction judge
    accepted decides it. A strategy added later is added here, together with the rule by
    which `compute_verdict` reads the Evidence it gathers.
    """
    raise NotImplementedError


@dataclass(frozen=True)
class ResultSummary:
    """What the supervisor is shown of the result of one dispatch, in place of the result.

    It holds numbers and words the project wrote, and no text taken from the result: no
    observation, no output, no path, nothing of a Reproduction, and nothing of
    `Gathered.inconclusive`. So it holds no Untrusted Content, and is no larger for a large
    result. The Evidence of the result is kept for the report, as `Supervisor.gather_within`
    says.
    """

    claim: int
    """The place of the Claim the dispatch was for."""

    strategy: Strategy
    """The strategy that was dispatched."""

    evidence_count: int
    """How many pieces of Evidence the dispatch gave."""

    verdict: Verdict
    """What `compute_verdict` gives from the Evidence the dispatch gave and the Claim's kind.
    It is computed by code, and tells the supervisor whether the work decided anything. The
    Claim's own Verdict is not this one: the core's entry point computes it, from all the
    Evidence gathered for the Claim."""

    reason: str
    """The reason `compute_verdict` gave with it, which is written by the project."""


def summarise(
    claim: int, strategy: Strategy, kind: ClaimKind | None, gathered: Gathered
) -> ResultSummary:
    """The summary of `gathered`, the result of dispatching `strategy` for the Claim at place
    `claim`, whose kind is `kind`.

    Its `claim` and `strategy` are those given, and its `evidence_count` is how many pieces
    of Evidence `gathered` holds. Its `verdict` and `reason` are those of
    `compute_verdict(gathered.evidence, kind)`, with `ClaimKind.BEHAVIOURAL` for a `kind` of
    None. Nothing else of `gathered` is read, and no model is asked: the same arguments
    always give the same summary.
    """
    raise NotImplementedError


@dataclass(frozen=True)
class Supervisor:
    """The `BudgetedOrchestration` in which a model decides how effort is spent on the Claims.

    It does for a Claim the work the fixed pipeline does for it, with the same roles. What
    the model decides is when that work is done, alongside what, and with how much of the
    Budget.
    """

    pipeline: FixedPipeline
    """Prepares the two commits, and holds what every role is run with."""

    model: Model | None = None
    """The supervising model. With None, the model `gather_within` is given supervises as well
    as doing the roles' work."""

    step_cap: int = SUPERVISOR_STEP_CAP
    """The most steps the supervisor may take for one pull request. At least 1."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        """Gather Evidence for each Claim as `gather_within` does, with a `Ledger()` made for
        the call, which has the provisional defaults for its Budget, and with no kinds."""
        raise NotImplementedError

    def gather_within(
        self,
        ledger: Ledger,
        claims: tuple[str, ...],
        *,
        repository: Path,
        before: str,
        after: str,
        model: Model,
        kinds: tuple[ClaimKind | None, ...] | None = None,
    ) -> tuple[Gathered, ...]:
        """Gather Evidence for each Claim, with the supervising model deciding how the effort
        is spent, within the Budget.

        A Claim is named by its place in `claims`, counted from 0, here as in `ledger`. Its
        kind is the one `kinds` has for it, and None when `kinds` is None. The supervising
        model is `self.model`, or `model` when that is None. The roles are always run with
        `model`.

        Before the supervisor is asked
            With no Claims nothing is done and `ledger` is not used. Otherwise the two commits
            are prepared as `self.pipeline.gather_within` says of its steps 1 to 3: the steps
            are taken when a Claim goes on to a Reproduction, and what they spend is recorded
            for no Claim.

            When one of those steps ends with `Unsupported`, `CommandFailed`, or
            `SandboxFailed`, each Claim that goes on to a Reproduction is stranded. A
            stranded Claim is given what the pipeline gives it for that, and no dispatch can
            be made for it, since nothing can be tried for it.

            Unless every Claim is stranded, `ledger.share_out(len(claims))` then sets aside a
            share for every Claim, of whichever kind. The supervisor is never asked when
            every Claim is stranded, or when the Budget is spent by then.

        A step
            A step is one request to the supervising model and what comes of its response.
            The request is made through `ledger.model` for no Claim, so each step of the
            supervisor is one step of the Budget, recorded against the pull request alone.

            `instructions` are written by the project and are the same on every step. They
            hold nothing of any Claim or of any result.

            `data` is a JSON object with exactly these members:

            - `claims`, a list of at most `CLAIMS_SHOWN_CAP` objects, one for each Claim
              shown. Shown first are the Claims still to have a Verification attempt, then
              every other Claim whose `verdict` is `inconclusive`, then the rest, and those
              of each group are in the order of `claims`. Each object has exactly: `claim`,
              its place; `kind`, which is `behavioural`, `structural`, or null; `text`, the
              first `CLAIM_TEXT_CAP` characters of the Claim; `strategies`, a list of the
              values of the strategies open to it, as said below; `attempts`, how many
              dispatches have been run for it; `verdict`, the value of the Verdict that
              `compute_verdict` gives from all the Evidence gathered for it so far, for the
              kind `summarise` would take it to have; and `left`, an object with the
              `seconds`, `dollars`, and `steps` of `ledger.left(claim=place)`.
            - `unattempted`, how many Claims of the pull request are still to have a
              Verification attempt, shown or not.
            - `budget`, an object with the `seconds`, `dollars`, and `steps` of
              `ledger.left()`.
            - `supervisor_steps_left`, how many more steps the supervisor may take after
              this one.
            - `results`, a list with the `ResultSummary` that `summarise` gives for each
              dispatch of the step before, in the order they were dispatched in. Each is an
              object with exactly `claim`, `strategy`, `evidence_count`, `verdict`, and
              `reason`. It is empty when the step before dispatched nothing, and on the
              first step.
            - `refused`, what the supervisor is told when the step before was refused, and
              null when it was not.

            The supervisor receives nothing else. The only text in a request that the
            project did not write is the `text` of each Claim shown: a structured Claim, as
            the Claim extractor listed it. No request holds the pull request's title,
            description, linked issue, or diff, anything a role read or a Sandbox printed,
            or anything of a result beyond its summary.

            The supervisor's context is one request. A request holds what is so now, and of
            earlier steps only the results or the refusal of the step before. So it is
            bounded by the caps, however many Claims the pull request has and however many
            steps were taken: no more than `CLAIMS_SHOWN_CAP` Claims, each with no more
            than `CLAIM_TEXT_CAP` characters of its text, and no more than `DISPATCH_CAP`
            results.

        A Verification attempt
            A Claim has had a Verification attempt once a dispatch for it has been run,
            whatever came of it. A Claim is still to have one when it has had none and is
            not stranded.

            The strategies open to a Claim are those of `strategies_for` its kind. None is
            open to a stranded Claim, and none to a Claim that any Evidence has been
            gathered for. So the Evidence a Claim is decided on comes of one dispatch, as in
            the fixed pipeline it comes of one turn: trying again never adds to Evidence,
            and the supervisor can try again only where an attempt gave none.

        What the supervisor may ask for
            The response is read as a JSON object with exactly one member, which is one of:

            - `{"dispatch": [{"claim": 0, "strategy": "reproduction"}, ...]}`, a list of
              objects that each have exactly a `claim`, a whole number, and a `strategy`, a
              string;
            - `{"move": {"from": 0, "to": 1, "seconds": 30.0, "dollars": 0.5, "steps": 4}}`,
              an object with no other member, where `from`, `to`, and `steps` are whole
              numbers, `seconds` and `dollars` are numbers, and each of `seconds`,
              `dollars`, and `steps` may be left out, which is 0;
            - `{"finish": {...}}`, where what it finishes with is an object.

            A whole number is a JSON number written with no fraction and no exponent, as `4`
            is and `4.0` is not. A number is a finite JSON number: `true`, `NaN`, and
            `Infinity` are not numbers. Anything else is unreadable: nothing is done, and
            the supervisor is told `SUPERVISOR_UNREADABLE`.

            A dispatch is run when the list holds from 1 to `DISPATCH_CAP` dispatches, each
            names a different Claim by its place, and each `strategy` is the value of a
            strategy open to that Claim. Otherwise the whole response is refused: nothing
            is run for any of them, and the supervisor is told `DISPATCH_REFUSED`, filled
            in.

            A move is made when `from` and `to` are the places of two different Claims, the
            Claim at `from` is not still to have a Verification attempt, no amount is less
            than 0 and at least one is more than 0, and no amount is more than
            `ledger.left(claim=from)` has of it. It is made with `ledger.move_share`, which
            enforces the totals. Otherwise nothing is moved and the supervisor is told
            `MOVE_REFUSED`. So the supervisor moves only what a Claim has left unused after
            it was tried, or what a stranded Claim can never use, and cannot take a Claim's
            share before its first attempt.

            A finish is accepted when no Claim is still to have a Verification attempt.
            Otherwise it is refused: the run goes on, and the supervisor is told
            `FINISH_REFUSED`, filled in. What `finish` holds is never read.

            Every response is a step, whatever came of it, a refusal included.

        Dispatches
            The dispatches of one step are run concurrently. Each is run on a thread of its
            own, all of them are started without waiting for any of them to end, and the step
            ends when every one of them has ended. Each does for its Claim what step 4 of
            `self.pipeline.gather_within` does for the Claim at that place: with
            `ledger.model(model, claim=place)` and `ledger.sandbox(sandbox, claim=place)`,
            with the pipeline's own settings, and ending as it says when the Claim's share
            or the Budget is spent. So a `Strategy.REPRODUCTION` is a call of
            `write_judged_reproduction` and a `Strategy.READING` one of
            `verify_structural_claim`. What a dispatch comes to is its result: a `Gathered`.
            A dispatch for a Claim whose share is spent is run like any other, and ends at
            once with `SHARE_SPENT`, filled in.

            A `Strategy.REPRODUCTION` has a Sandbox to itself for as long as it runs, since
            a Sandbox has one working copy for each commit: no other dispatch uses that
            Sandbox until it has ended. So nothing of one Claim reaches a role working on
            another, though they work at the same time. A `Strategy.READING` uses no
            Sandbox.

            The Sandbox prepared before the supervisor was asked is the first. A step that
            dispatches more Reproductions than there are Sandboxes made ready makes ready
            one more for each beyond that, before any dispatch of the step is started. They
            are made ready one after another, on the thread the supervisor is asked on,
            each as steps 2 and 3 of the pipeline say, with the pipeline's `open_sandbox`,
            `store`, and `cache`, and what that spends is recorded for no Claim. A Sandbox
            made ready is kept and used again in later steps, so no more are made ready
            than the most Reproductions dispatched in one step, which is never more than
            `DISPATCH_CAP`. The Reproductions of a step take the Sandboxes in the order of
            the list: first those made ready before, then each further one.

            A further Sandbox is not kept when it cannot be made ready, because step 2 or 3
            ended with `Unsupported`, `CommandFailed`, or `SandboxFailed`. The dispatch it
            was for then runs no role: its result is no Evidence and what the pipeline
            gives a Claim for that failure, and it is a dispatch that was run, like any
            other. When the Budget is spent while a Sandbox is made ready, no dispatch of
            the step is run.

            The time each dispatch takes is recorded as the ledger says, so work done at the
            same time is each counted: the Budget's seconds are seconds of work. While a
            recording is open, what each dispatch does is recorded as
            `pr_verification.tracing` says of a thread started for a dispatch.

        Nothing the supervisor says is a Verdict
            No response is read for a Verdict or for the Gate, and none has a member that
            could give one: a Verdict written in a `finish` is not read, and one written
            anywhere else makes the response unreadable. What each Claim is given holds only
            the Evidence the roles recorded, so the report's Verdicts and its Gate are what
            code computes from that Evidence, whatever the supervising model said.

        When the supervisor is stopped
            The supervisor is stopped when `self.step_cap` steps have been taken, and when
            the supervising model raises `ModelFailed`. It is asked nothing more. Each Claim
            still to have a Verification attempt is then given one by code: one at a time,
            in the order of `claims`, each as a dispatch of the first of `strategies_for`
            its kind, for as long as the Budget is not spent. So a supervisor that wastes
            its steps, or whose model fails, costs the choices it would have made, and costs
            no Claim its attempt.

        When the run ends
            The supervisor is asked for one step after another until the first of these:

            - a finish is accepted;
            - the Budget is spent, which is when `ledger.stopped_by()` gives a limit. That
              is looked at before each request and again once its response has come, so a
              response that comes after the Budget is spent is not acted on: no further
              request is made to any model, and nothing more is run;
            - the supervisor is stopped, and the run ends once code has given each Claim
              its attempt, or the Budget is spent.

            So whatever the supervising model does, when the run ends either no Claim is
            still to have a Verification attempt or the Budget is spent. All of this is
            enforced here, by code. The supervising model is not asked whether any Budget or
            any step is left, and nothing it says changes a cap or a limit.

        What each Claim is given
            One `Gathered` for each Claim, in the order of `claims`. A stranded Claim is
            given what the pipeline gives it. For any other Claim, `evidence` is the
            Evidence of the one dispatch that gave it any, in the order gathered, and none
            when no dispatch did. So all the Evidence is kept for the report, and only
            summaries reached the supervisor. Its `inconclusive` is that of the last
            dispatch run for it. A Claim that no dispatch
            was run for is given no Evidence and `BUDGET_SPENT`, filled in with the limit
            `ledger.stopped_by()` gives: the Budget being spent is the only way a Claim is
            left without an attempt.

        Every Sandbox opened is closed before the answer is given or an exception goes on.

        Raises `ValueError`, before anything else is done, when `self.step_cap` is less than
        1, and when `kinds` is given and holds other than one kind for each Claim. Raises
        what `self.pipeline.gather_within` raises: a fault in the system that a dispatch
        raises goes on from here once every dispatch of its step has ended, and when more
        than one of them raised, it is that of the first in the order dispatched.
        `BudgetSpent` is never raised from here, and nor is `ModelFailed`: a failing model
        costs Evidence, and never the answer.
        """
        raise NotImplementedError


def supervisor(
    state: Path, *, progress_guard: Decider | None = None, model: Model | None = None
) -> Supervisor:
    """The supervisor that keeps what it remembers between runs under `state`.

    Its `pipeline` is `fixed_pipeline(state, progress_guard=progress_guard)`, and its `model`
    is the one given. Everything else is as `Supervisor` has it.
    """
    raise NotImplementedError
