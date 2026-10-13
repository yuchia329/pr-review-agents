"""The core's entry point: a pull request goes in, a structured report comes out."""

import subprocess
import tempfile
import time
from collections.abc import Iterable, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from pr_verification.agent import Loop
from pr_verification.budget import Budget, Clock, Ledger
from pr_verification.claims import classify_claim, extract_claims
from pr_verification.decision import Decider, Decision, Question
from pr_verification.model import Model
from pr_verification.orchestration import (
    BudgetedOrchestration,
    Gathered,
    Orchestration,
    fixed_pipeline,
)
from pr_verification.report import (
    BudgetSpend,
    Claim,
    ClaimKind,
    ClaimSpend,
    ClassifiedClaim,
    Finding,
    LoopCounts,
    PullRequestSpend,
    Report,
    Verdict,
)
from pr_verification.repository import resolve_commit
from pr_verification.review import review_pull_request
from pr_verification.tracing import SpanKind, span
from pr_verification.verdict import compute_verdict


@dataclass(frozen=True)
class PullRequest:
    repository: Path
    before: str
    after: str
    title: str
    description: str = ""
    linked_issue: str = ""


class InvalidPullRequest(Exception):
    """The pull request as given cannot be verified at all, so there is no report."""


def count_loops(found: Iterable[tuple[Sequence[Loop], Verdict]]) -> LoopCounts:
    """Count the loops detected in one run, each under the one outcome it came to.

    `found` holds, for each Claim of a pull request, the loops detected in the work done for
    it, as `Gathered.loops` has them, and the Verdict the Claim was given. Each `Loop` in it
    is counted once in `detected` and once under one outcome. The rung of the ladder its role
    reached and the Verdict of its Claim settle which:

    - `recovered_after_told` when the Verdict is Confirmed or Refuted and the loop has no
      `second`: the role was told, was not found going round again, and the Claim was
      decided.
    - `recovered_after_replanning` when the Verdict is Confirmed or Refuted and the loop has
      a `second`: the role was stopped, and the Claim was decided all the same. The work
      that was stopped gave no Evidence, so what decided the Claim was dispatched for it
      afterwards.
    - `ended_inconclusive` when the Verdict is Inconclusive, whichever rung was reached.

    So `detected` is the number of loops in `found`, and the three outcomes always add up to
    it. With no Claims, or no loops, every count is 0. Nothing is read but whether each loop
    has a `second`, and each Verdict.

    A loop is counted by what came of its Claim, and not by what came of its own run. One
    whose role was told and then finished is counted under `ended_inconclusive` when its
    Claim was left Inconclusive for any other cause. One whose role was told, and whose
    Claim was decided only by work dispatched for it afterwards, is counted under
    `recovered_after_told`: the rung is the one the loop's own run reached.
    """
    raise NotImplementedError


def run(
    pull_request: PullRequest,
    model: Model,
    decider: Decider | None = None,
    *,
    orchestration: Orchestration | None = None,
    budget: Budget | None = None,
    clock: Clock = time.monotonic,
    review: bool = False,
) -> Report:
    """Verify one pull request end to end and return its report.

    1. Resolves `pull_request.before` and `pull_request.after` to full commit IDs, which are
       the report's `before` and `after`. Raises `InvalidPullRequest` when either cannot be
       resolved, before any request to the model.
    2. Extracts the Claims with `extract_claims`, from the title, description, and linked
       issue and nothing else.
    3. Classifies the Claims, as described below.
    4. Gathers Evidence with the orchestration, asked once, with the Claims in the order
       extracted, the repository, the two full commit IDs, and `model`: through `gather`, or
       through `gather_within` as The Budget says below. With no Claims it is not asked.
    5. Computes each Claim's Verdict with `compute_verdict`, from the Evidence gathered for
       that Claim and its kind, and nothing else.
    6. With `review` true, reviews the pull request, as described below.
    7. Counts the loops, as described below.

    Classifying
        Each Claim the extractor lists is labelled by `classify_claim(decider, text)`, once,
        in the report's order, and is in the report as a `ClassifiedClaim` with that `kind`,
        unless the Budget is spent first, as The Budget says below.
        `decider` is the only thing asked: the classifier makes no request to `model` of its
        own, and a pull request with no Claims asks for no decision.

        When `decider` is None nothing is classified and each Claim is in the report as a
        plain `Claim`, with no `kind`. The command line always gives one.

        A `DecisionFailed` from the decider is not caught. It is a `ModelFailed`, so there is
        no report, as when the Claim extractor's model fails. `decider` is expected to be one
        that passes on an input beyond a size limit, as a configured decider is: an
        `InputTooLong` is not caught either, and is a fault in the caller.

    Routing
        A Claim's kind settles how Evidence is gathered for it and which Evidence can decide
        it. A Behavioural Claim goes on to a Reproduction. A Structural Claim is verified by
        reading, and is not sent to a Sandbox. A Claim with no kind is verified as a
        Behavioural Claim.

        An orchestration that is a `BudgetedOrchestration`, as the fixed pipeline is, is told
        the kinds: `gather_within` is given, as `kinds`, the kind of each Claim in the order
        extracted, with None for a Claim that has none. It routes as its `gather_within`
        says. Any other orchestration is asked through `gather`, which is told no kinds.

        In step 5 the Verdict of a Structural Claim is
        `compute_verdict(evidence, ClaimKind.STRUCTURAL)`, and that of every other Claim is
        `compute_verdict(evidence, ClaimKind.BEHAVIOURAL)`. So whatever an orchestration
        gathers, a Structural Claim is decided only by what a search of the code found, and
        a Behavioural Claim only by a Reproduction that the Reproduction judge accepted.

    The report holds one Claim for each Claim extracted, in that order, so every Claim
    receives a Verdict. Each has its text, the Verdict computed, and all the Evidence gathered
    for it, in the order gathered. Its reason is the one `compute_verdict` gave, except that
    when the Verdict is Inconclusive and the orchestration gave an `inconclusive` for the
    Claim, the reason is that. So the report of a Claim with a Reproduction shows the
    Reproduction and its output on the code before and after the pull request, as
    `ReproductionEvidence` is printed, and the report of a Structural Claim shows each text
    searched for and the lines that hold it, as `SearchEvidence` is printed.

    Neither a model nor the orchestration can give a Verdict. Whatever a model says, and
    whatever `inconclusive` holds, a Behavioural Claim is Inconclusive unless a Reproduction
    that the Reproduction judge accepted ran on both commits, and a Structural Claim is
    Inconclusive unless the code was searched. The report's Gate is computed from the Verdicts
    alone, by `compute_gate`.

    The Budget
        `budget` is the Budget for the call, and `Budget()`, with its provisional defaults,
        when None. One `Ledger` is made for the call, with `budget` and `clock`, before step
        1. What the call spends is recorded in it as said here, and `clock` is read for
        nothing else:

        - The Claim extractor's one request is made through `ledger.model(model)`, for no
          Claim, so listing the Claims is one step.
        - Each decision that classifies a Claim is recorded for no Claim: the seconds
          `classify_claim` took, between a reading of `clock` just before it and one just
          after, the `dollars` of the `Decision` that `decider` gave for the Claim, and no
          step, since whatever a decider asks of a model it does not ask through the ledger.
          So the dollars spent are summed across `model` and whatever answers the decisions,
          each at its own price. Once the Budget is spent no further decision is asked for,
          and each Claim not yet classified is in the report as a plain `Claim`, with no
          `kind`: the one case in which a Claim goes unclassified though a decider was given.
        - An orchestration that is a `BudgetedOrchestration`, as the fixed pipeline is, is
          asked in step 4 through `gather_within`, called once, with the ledger, then what
          `gather` is given, and `kinds` as Routing says above. Its `gather` is not called. It
          records what it spends, sets aside the share of each Claim, and stops as
          `gather_within` says.
        - Any other orchestration is asked through `gather`, with `model` itself. Nothing it
          spends is recorded and no limit stops it, so the Budget holds only for an
          orchestration that keeps to one. The fixed pipeline does; a stand-in in a test
          need not.

        The report's `budget_spend` is a `PullRequestSpend`. Its `seconds`, `dollars`, and
        `steps` are those of `ledger.spent()`, and its `stopped_by` is `ledger.stopped_by()`.
        Its `claims` holds one `ClaimSpend` for each Claim of the report, in the report's
        order, with the `seconds`, `dollars`, and `steps` of `ledger.spent(claim=place)` and
        the `stopped_by` of `ledger.stopped_by(claim=place)`, where `place` is the Claim's
        place in that order, counted from 0. So the report shows the spend in each unit and
        the limit that stopped the run, if one did, for the pull request and for each Claim.

        The limits are enforced by the ledger and the code that asks it, never by a model.
        Reaching one is not a failure: nothing is raised for it, and the report is given with
        every Claim in it. A Claim still unresolved when the Budget was spent, or when its own
        share was, has no Evidence that decides it, so its Verdict is Inconclusive like any
        other such Claim's, and its reason is the one the orchestration gave: `BUDGET_SPENT`
        or `SHARE_SPENT`, filled in. A Claim whose share was spent does not stop the others
        from receiving their Verdicts.

    Reviewing
        With `review` true, `review_pull_request` (from `pr_verification.review`) is called
        once, after step 5. It is given the Claims in the order extracted; the pull request's
        title, description, and linked issue; all the Evidence gathered, Claim by Claim in
        the report's order and each Claim's in the order gathered; the repository; the two
        full commit IDs; `model`; and `decider`. The report's `findings` are the `findings`
        it gives, in that order. A Review asks the decision layer: `ValueError` is raised
        before anything else is done when `review` is true and `decider` is None.

        With `review` false, as it is unless given, nothing is reviewed: `findings` is empty,
        and nothing more is asked of the model or of the decider.

        Findings are advisory. The Review is given no Verdict and comes after every Verdict
        is computed, and nothing it gives is read to compute a Verdict or the Gate. So a
        report has the same Claims, with the same Verdicts, reasons, and Evidence, and the
        same Gate, whatever its Findings are and whether or not there was a Review. That
        holds for a detection by the injection detector as for any Finding: it is an extra
        signal and changes nothing else (ADR 0002). A Review in which the model or the
        decision layer failed costs a Finding that says so, and never the report: a
        `ModelFailed` is not raised from step 6.

        No Claim is made from the diff. The Review reads the diff and the Claim extractor
        never does: `claims` holds the Claims extracted in step 2 and no other, whatever the
        Review finds. A change that no Claim covers is a Finding.

        Nothing a Review spends is recorded in the ledger, and no limit of the Budget stops
        it: until it is given a share, a Review is bounded by its own caps alone.

    Loops
        The report is a `ReportWithLoops`. Its `loops` are `count_loops` of, for each Claim
        in the report's order, the `loops` of the `Gathered` the orchestration gave for it
        and the Verdict computed for it in step 5. So each run records how many loops were
        detected, how many recovered after the role was told, how many recovered after the
        supervisor planned again, and how many ended Inconclusive. Every count is 0 when no
        loop was found, as for a pull request with no Claims, and for an orchestration that
        gives no `loops`.

        A Claim whose role was stopped for going round in circles has no Evidence from that
        work. Unless work dispatched for it afterwards decides it, its Verdict is
        Inconclusive and its reason is the one the orchestration gave, which is
        `STOPPED_LOOPING` filled in. A Claim that was never worked on because the supervisor
        repeated itself is Inconclusive too, and its reason is `SUPERVISOR_REPEATED`.

        Only the work the orchestration did for the Claims is counted. A loop found in the
        Reviewer's run in step 6 is in no Claim's `loops`, and is not counted. Nothing of
        the counts is read to compute a Verdict or the Gate.

    With `orchestration` None, the fixed pipeline is used and nothing is kept between runs: it
    is `fixed_pipeline` on a temporary directory, which is removed before the report is
    returned or an exception goes on. Its `progress_guard` is `decider`, so the decider that
    classifies the Claims also answers the progress guard of the roles the pipeline runs, and
    with `decider` None they have none. An orchestration that is given is used as it is.

    While a recording is open, the call is recorded as the spans `pr_verification.tracing`
    describes, all under one span for the call itself. Recording changes nothing in the report.

    Raises `InvalidPullRequest`, `ClaimExtractionFailed`, and `ModelFailed` from steps 1 to
    3, where there is no report to give. Raises `ValueError` when the orchestration gives
    other than one `Gathered` for each Claim, and whatever else the orchestration raises.
    Raises `ValueError` as `Ledger` does for `budget`, before step 1.
    Raises `ValueError` when `review` is true and there is no `decider`, and whatever
    `review_pull_request` raises: a fault in the system, and never a failing model.
    """
    with span(SpanKind.RUN, "pr-verification"):
        return _run(pull_request, model, decider, orchestration, budget, clock, review)


def _run(
    pull_request: PullRequest,
    model: Model,
    decider: Decider | None,
    orchestration: Orchestration | None,
    budget: Budget | None,
    clock: Clock,
    review: bool,
) -> Report:
    """The call that `run` records."""
    if review and decider is None:
        raise ValueError("a Review asks the decision layer, and no decider was given")
    ledger = Ledger(budget, clock=clock)
    before = _resolve_commit(pull_request.repository, "before", pull_request.before)
    after = _resolve_commit(pull_request.repository, "after", pull_request.after)
    claims = extract_claims(
        ledger.model(model),
        pull_request.title,
        pull_request.description,
        pull_request.linked_issue,
    )
    kinds = _classify(decider, claims, ledger, clock)
    gathered = _gather(
        orchestration, ledger, claims, kinds, pull_request.repository, before, after, model, decider
    )
    if any(answer.loops for answer in gathered):
        raise NotImplementedError
    verified = tuple(
        _verified(text, kind, answer)
        for text, kind, answer in zip(claims, kinds, gathered, strict=True)
    )
    # Every Verdict is computed by now, and nothing below is read to compute one or the Gate.
    # The Review asks `model` itself, so nothing it spends is recorded in the ledger.
    findings: tuple[Finding, ...] = ()
    if review and decider is not None:
        findings = review_pull_request(
            claims,
            title=pull_request.title,
            description=pull_request.description,
            linked_issue=pull_request.linked_issue,
            evidence=tuple(evidence for claim in verified for evidence in claim.evidence),
            repository=pull_request.repository,
            before=before,
            after=after,
            model=model,
            decider=decider,
        ).findings
    return Report(
        before=before,
        after=after,
        claims=verified,
        findings=findings,
        budget_spend=_spend(ledger, len(claims)),
    )


class _Costed:
    """Asks a decider, and keeps what its decisions cost."""

    def __init__(self, decider: Decider) -> None:
        self._decider = decider
        self.dollars = 0.0

    def decide(self, state: str, question: Question) -> Decision:
        decision = self._decider.decide(state, question)
        self.dollars += decision.dollars
        return decision


def _classify(
    decider: Decider | None, claims: tuple[str, ...], ledger: Ledger, clock: Clock
) -> tuple[ClaimKind | None, ...]:
    """The kind of each Claim, with None for one that is not classified: every Claim when there
    is no decider, and each Claim reached after the Budget was spent. What each decision cost
    is recorded in `ledger` for no Claim."""
    kinds: list[ClaimKind | None] = []
    for text in claims:
        if decider is None or ledger.stopped_by() is not None:
            kinds.append(None)
            continue
        costed = _Costed(decider)
        started = clock()
        kinds.append(classify_claim(costed, text))
        ledger.record(BudgetSpend(seconds=clock() - started, dollars=costed.dollars, steps=0))
    return tuple(kinds)


def _spend(ledger: Ledger, claims: int) -> PullRequestSpend:
    """What `ledger` recorded, for the pull request and for each of its `claims` Claims."""

    def spend_on(claim: int) -> ClaimSpend:
        spent = ledger.spent(claim=claim)
        return ClaimSpend(spent.seconds, spent.dollars, spent.steps, ledger.stopped_by(claim=claim))

    spent = ledger.spent()
    return PullRequestSpend(
        spent.seconds,
        spent.dollars,
        spent.steps,
        stopped_by=ledger.stopped_by(),
        claims=tuple(spend_on(claim) for claim in range(claims)),
    )


def _gather(
    orchestration: Orchestration | None,
    ledger: Ledger,
    claims: tuple[str, ...],
    kinds: tuple[ClaimKind | None, ...],
    repository: Path,
    before: str,
    after: str,
    model: Model,
    decider: Decider | None,
) -> tuple[Gathered, ...]:
    """What the orchestration gathered for each Claim, with the fixed pipeline when none is
    given, whose progress guard `decider` then answers. One that keeps to a Budget is asked
    within `ledger` and told `kinds`. Raises `ValueError` when it does not answer for each
    Claim."""
    if not claims:
        return ()
    with ExitStack() as kept:
        if orchestration is None:
            # Nothing is kept between runs: what the pipeline remembers goes with the directory.
            state = kept.enter_context(tempfile.TemporaryDirectory())
            orchestration = fixed_pipeline(Path(state), progress_guard=decider)
        if isinstance(orchestration, BudgetedOrchestration):
            gathered = orchestration.gather_within(
                ledger,
                claims,
                repository=repository,
                before=before,
                after=after,
                model=model,
                kinds=kinds,
            )
        else:
            gathered = orchestration.gather(
                claims, repository=repository, before=before, after=after, model=model
            )
    if len(gathered) != len(claims):
        raise ValueError(
            f"the orchestration answered for {len(gathered)} Claims of the {len(claims)} asked"
        )
    return gathered


def _verified(text: str, kind: ClaimKind | None, gathered: Gathered) -> Claim:
    """One Claim of the report: its Verdict, computed from the Evidence gathered for it and
    its kind, which it has when it was classified. With none it is verified as a Behavioural
    Claim."""
    computed = compute_verdict(gathered.evidence, ClaimKind.BEHAVIOURAL if kind is None else kind)
    reason = computed.reason
    # What the orchestration says can explain an Inconclusive Verdict, and can never give one.
    if computed.verdict is Verdict.INCONCLUSIVE and gathered.inconclusive is not None:
        reason = gathered.inconclusive
    if kind is None:
        return Claim(text, computed.verdict, reason, gathered.evidence)
    return ClassifiedClaim(text, computed.verdict, reason, gathered.evidence, kind=kind)


def _resolve_commit(repository: Path, role: str, commit: str) -> str:
    try:
        return resolve_commit(repository, commit)
    except OSError as error:
        raise InvalidPullRequest(f"cannot run git in {repository}: {error}") from error
    except subprocess.CalledProcessError as error:
        refusal = error.stderr.decode(errors="replace").strip()
        raise InvalidPullRequest(
            f"cannot resolve the {role} commit {commit!r} in {repository}: {refusal}"
        ) from error
