import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_verification.ecosystem import Results, Unreadable
from pr_verification.model import Model, ModelFailed, ModelRequest, ScriptedModel
from pr_verification.orchestration import Gathered
from pr_verification.report import EvidenceSource, ReproductionEvidence
from pr_verification.sandbox import RunResult


@dataclass(frozen=True)
class FixtureRepository:
    path: Path
    before: str
    after: str


class Clock:
    """A controlled clock: it reads `now`, and time passes only when a stand-in moves it on."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def commit(path: Path, message: str) -> str:
    git(path, "add", "-A")
    git(
        path,
        "-c", "user.name=fixture",
        "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
    )  # fmt: skip
    return git(path, "rev-parse", "HEAD")


def commit_snapshots(path: Path, *snapshots: Mapping[str, str]) -> tuple[str, ...]:
    """Make a repository at `path` with one commit per snapshot, each holding exactly its files.

    A snapshot maps a file's path within the repository to its text. Returns the commits in order.
    """
    path.mkdir(parents=True)
    git(path, "init", "--quiet")
    commits = []
    for number, snapshot in enumerate(snapshots, start=1):
        for entry in path.iterdir():
            if entry.name != ".git":
                shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
        for name, text in snapshot.items():
            (path / name).parent.mkdir(parents=True, exist_ok=True)
            (path / name).write_text(text)
        commits.append(commit(path, f"Snapshot {number}"))
    return tuple(commits)


@pytest.fixture(autouse=True)
def no_model_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """A model configured on the developer's machine must not be reached from a test, and a
    Budget or a price configured there must not change one."""
    for name in (
        "PR_VERIFICATION_ENDPOINT",
        "PR_VERIFICATION_MODEL",
        "PR_VERIFICATION_API_KEY",
        "PR_VERIFICATION_DECISION",
        "PR_VERIFICATION_DECISION_THRESHOLD",
        "PR_VERIFICATION_JEV_API_KEY",
        "PR_VERIFICATION_BUDGET_SECONDS",
        "PR_VERIFICATION_BUDGET_DOLLARS",
        "PR_VERIFICATION_BUDGET_STEPS",
        "PR_VERIFICATION_INPUT_PRICE",
        "PR_VERIFICATION_OUTPUT_PRICE",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def fixture_repository(tmp_path: Path) -> FixtureRepository:
    """A small repository with one commit before the pull request and one after."""
    path = tmp_path / "repository"
    path.mkdir()
    git(path, "init", "--quiet")
    (path / "greeting.py").write_text('def greet():\n    return "hello"\n')
    before = commit(path, "Add greeting")
    (path / "greeting.py").write_text('def greet():\n    return "hello there"\n')
    after = commit(path, "Tidy greeting")
    return FixtureRepository(path=path, before=before, after=after)


def model_listing(*claims: str) -> ScriptedModel:
    """A stand-in for the model whose one response to the Claim extractor lists these Claims."""
    return ScriptedModel([json.dumps({"claims": claims})])


class UnreachableModel:
    """A stand-in for a model that cannot be reached."""

    def complete(self, request: ModelRequest) -> str:
        raise ModelFailed("the model could not be reached")


def asks(tool: str, **arguments: object) -> str:
    """A response in which a role asks for a tool."""
    return json.dumps({"tool": tool, "arguments": arguments})


def finishes(**answer: object) -> str:
    """A response in which a role finishes with an answer."""
    return json.dumps({"finish": answer})


def results(passed: str = "", failed: str = "", errors: str = "") -> Results:
    """Results in which each letter names one test."""
    return Results(passed=tuple(passed), failed=tuple(failed), could_not_run=tuple(errors))


def a_reproduction(
    before: Results | Unreadable | None,
    after: Results | Unreadable | None,
    *,
    text: str = "assert total(100) == 120.0",
    output: str = "",
    exit_status: int = 0,
    observation: str = "before: exit status 0; after: exit status 0",
) -> ReproductionEvidence:
    """Evidence from a Reproduction whose tests ended on the two commits as given. What both
    runs printed and how they exited is the same, and says nothing of how the tests ended."""
    ran = RunResult(output=output, exit_status=exit_status, limit_hit=None)
    return ReproductionEvidence(
        source=EvidenceSource.RAN_CODE,
        observation=observation,
        command=("run-reproduction",),
        before=ran,
        after=ran,
        path="reproduction.txt",
        reproduction=text,
        before_results=before,
        after_results=after,
    )


class GathersNothing:
    """A stand-in orchestration that gathers no Evidence for any Claim and records each call."""

    def __init__(self) -> None:
        self.calls: list[tuple[tuple[str, ...], Path, str, str, Model]] = []

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        self.calls.append((claims, repository, before, after, model))
        return tuple(Gathered(()) for _ in claims)
