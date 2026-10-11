"""The core's entry point: a pull request goes in, a structured report comes out."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from pr_verification.claims import extract_claims
from pr_verification.decision import Decider
from pr_verification.model import Model
from pr_verification.orchestration import Orchestration
from pr_verification.report import BudgetSpend, Claim, Report, Verdict
from pr_verification.repository import resolve_commit

NOT_VERIFIED_YET = "This Claim has not been verified yet: no Evidence was gathered for it."


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
) -> Report:
    """Verify one pull request end to end and return its report.

    1. Resolves `pull_request.before` and `pull_request.after` to full commit IDs, which are
       the report's `before` and `after`. Raises `InvalidPullRequest` when either cannot be
       resolved, before any request to the model.
    2. Extracts the Claims with `extract_claims`, from the title, description, and linked
       issue and nothing else.
    3. Classifies the Claims, as described below.
    4. Gathers Evidence with `orchestration.gather`, called once, with the Claims in the order
       extracted, the repository, the two full commit IDs, and `model`. With no Claims it is
       not called.
    5. Computes each Claim's Verdict with `compute_verdict`, from the Evidence gathered for
       that Claim and nothing else.

    Classifying
        Each Claim the extractor lists is labelled by `classify_claim(decider, text)`, once,
        in the report's order, and is in the report as a `ClassifiedClaim` with that `kind`.
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

    `findings` is empty. In `budget_spend`, `seconds` is how long the call took, and `dollars`
    and `steps` are 0 until the Budget is built.

    With `orchestration` None, the fixed pipeline is used and nothing is kept between runs: it
    is `fixed_pipeline` on a temporary directory, which is removed before the report is
    returned or an exception goes on.

    Raises `InvalidPullRequest`, `ClaimExtractionFailed`, and `ModelFailed` from steps 1 to
    3, where there is no report to give. Raises `ValueError` when the orchestration gives
    other than one `Gathered` for each Claim, and whatever else the orchestration raises.
    """
    if orchestration is not None:
        raise NotImplementedError
    started = time.monotonic()
    before = _resolve_commit(pull_request.repository, "before", pull_request.before)
    after = _resolve_commit(pull_request.repository, "after", pull_request.after)
    claims = extract_claims(
        model, pull_request.title, pull_request.description, pull_request.linked_issue
    )
    return Report(
        before=before,
        after=after,
        claims=tuple(Claim(text, Verdict.INCONCLUSIVE, NOT_VERIFIED_YET) for text in claims),
        findings=(),
        budget_spend=BudgetSpend(seconds=time.monotonic() - started, dollars=0.0, steps=0),
    )


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
