import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_verification.model import ScriptedModel


@dataclass(frozen=True)
class FixtureRepository:
    path: Path
    before: str
    after: str


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
    """A model configured on the developer's machine must not be reached from a test."""
    for name in ("PR_VERIFICATION_ENDPOINT", "PR_VERIFICATION_MODEL", "PR_VERIFICATION_API_KEY"):
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
