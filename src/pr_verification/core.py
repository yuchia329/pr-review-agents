"""The core's entry point: a pull request goes in, a structured report comes out."""

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from pr_verification.report import BudgetSpend, Report


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


def run(pull_request: PullRequest) -> Report:
    started = time.monotonic()
    before = _resolve_commit(pull_request.repository, "before", pull_request.before)
    after = _resolve_commit(pull_request.repository, "after", pull_request.after)
    return Report(
        before=before,
        after=after,
        claims=(),
        findings=(),
        budget_spend=BudgetSpend(seconds=time.monotonic() - started, dollars=0.0, steps=0),
    )


def _resolve_commit(repository: Path, role: str, commit: str) -> str:
    # Only the named directory may answer: not a repository above it, nor one from the environment.
    environment = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    environment["GIT_CEILING_DIRECTORIES"] = str(repository.resolve().parent)
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--end-of-options", f"{commit}^{{commit}}"],
            cwd=repository,
            env=environment,
            capture_output=True,
            text=True,
        )
    except OSError as error:
        raise InvalidPullRequest(f"cannot run git in {repository}: {error}") from error
    if result.returncode != 0:
        raise InvalidPullRequest(
            f"cannot resolve the {role} commit {commit!r} in {repository}: {result.stderr.strip()}"
        )
    return result.stdout.strip()
