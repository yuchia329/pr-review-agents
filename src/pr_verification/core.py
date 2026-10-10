"""The core's entry point: a pull request goes in, a structured report comes out."""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from pr_verification.claims import extract_claims
from pr_verification.model import Model
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


def run(pull_request: PullRequest, model: Model) -> Report:
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
