"""The core's entry point: a pull request goes in, a structured report comes out."""

import subprocess
import tempfile
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path

from pr_verification.budget import Budget, Clock
from pr_verification.claims import classify_claim, extract_claims
from pr_verification.decision import Decider
from pr_verification.model import Model
from pr_verification.orchestration import Gathered, Orchestration, fixed_pipeline
from pr_verification.report import (
    BudgetSpend,
    Claim,
    ClaimKind,
    ClassifiedClaim,
    Report,
    Verdict,
)
from pr_verification.repository import resolve_commit
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
       that Claim and nothing else.
    6. With `review` true, reviews the pull request, as described below.

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

        Whatever its kind, every Claim is verified as a Behavioural Claim until the
        verification of Structural Claims is built: the kind changes nothing in steps 4 and 5.

    The report holds one Claim for each Claim extracted, in that order, so every Claim
    receives a Verdict. Each has its text, the Verdict computed, and all the Evidence gathered
    for it, in the order gathered. Its reason is the one `compute_verdict` gave, except that
    when the Verdict is Inconclusive and the orchestration gave an `inconclusive` for the
    Claim, the reason is that. So the report of a Claim with a Reproduction shows the
    Reproduction and its output on the code before and after the pull request, as
    `ReproductionEvidence` is printed.

    Neither the model nor the orchestration can give a Verdict. Whatever a model says, and
    whatever `inconclusive` holds, a Claim for which no Reproduction ran on both commits is
    Inconclusive. The report's Gate is computed from the Verdicts alone, by `compute_gate`.

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
          asked in step 4 through `gather_within`, called once, with the ledger and then what
          `gather` is given. Its `gather` is not called. It records what it spends, sets aside
          the share of each Claim, and stops as `gather_within` says.
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

    With `orchestration` None, the fixed pipeline is used and nothing is kept between runs: it
    is `fixed_pipeline` on a temporary directory, which is removed before the report is
    returned or an exception goes on. Its `progress_guard` is `decider`, so the decider that
    classifies the Claims also answers the progress guard of the roles the pipeline runs, and
    with `decider` None they have none. An orchestration that is given is used as it is.

    Raises `InvalidPullRequest`, `ClaimExtractionFailed`, and `ModelFailed` from steps 1 to
    3, where there is no report to give. Raises `ValueError` when the orchestration gives
    other than one `Gathered` for each Claim, and whatever else the orchestration raises.
    Raises `ValueError` as `Ledger` does for `budget`, before step 1.
    Raises `ValueError` when `review` is true and there is no `decider`, and whatever
    `review_pull_request` raises: a fault in the system, and never a failing model.
    """
    if budget is not None or clock is not time.monotonic or review:
        raise NotImplementedError
    started = time.monotonic()
    before = _resolve_commit(pull_request.repository, "before", pull_request.before)
    after = _resolve_commit(pull_request.repository, "after", pull_request.after)
    claims = extract_claims(
        model, pull_request.title, pull_request.description, pull_request.linked_issue
    )
    kinds: tuple[ClaimKind | None, ...] = tuple(
        None if decider is None else classify_claim(decider, text) for text in claims
    )
    gathered = _gather(orchestration, claims, pull_request.repository, before, after, model)
    return Report(
        before=before,
        after=after,
        claims=tuple(
            _verified(text, kind, answer)
            for text, kind, answer in zip(claims, kinds, gathered, strict=True)
        ),
        findings=(),
        budget_spend=BudgetSpend(seconds=time.monotonic() - started, dollars=0.0, steps=0),
    )


def _gather(
    orchestration: Orchestration | None,
    claims: tuple[str, ...],
    repository: Path,
    before: str,
    after: str,
    model: Model,
) -> tuple[Gathered, ...]:
    """What the orchestration gathered for each Claim, with the fixed pipeline when none is
    given. Raises `ValueError` when it does not answer for each Claim."""
    if not claims:
        return ()
    with ExitStack() as kept:
        if orchestration is None:
            # Nothing is kept between runs: what the pipeline remembers goes with the directory.
            state = kept.enter_context(tempfile.TemporaryDirectory())
            orchestration = fixed_pipeline(Path(state))
        gathered = orchestration.gather(
            claims, repository=repository, before=before, after=after, model=model
        )
    if len(gathered) != len(claims):
        raise ValueError(
            f"the orchestration answered for {len(gathered)} Claims of the {len(claims)} asked"
        )
    return gathered


def _verified(text: str, kind: ClaimKind | None, gathered: Gathered) -> Claim:
    """One Claim of the report: its Verdict, computed from the Evidence gathered for it, and
    its kind when it was classified."""
    computed = compute_verdict(gathered.evidence)
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
