import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from pr_verification.model import ScriptedModel


@dataclass(frozen=True)
class FixtureRepository:
    path: Path
    before: str
    after: str


def _git(path: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _commit(path: Path, message: str) -> str:
    _git(path, "add", "-A")
    _git(
        path,
        "-c", "user.name=fixture",
        "-c", "user.email=fixture@example.invalid",
        "-c", "commit.gpgsign=false",
        "commit", "-m", message,
    )  # fmt: skip
    return _git(path, "rev-parse", "HEAD")


@pytest.fixture
def fixture_repository(tmp_path: Path) -> FixtureRepository:
    """A small repository with one commit before the pull request and one after."""
    path = tmp_path / "repository"
    path.mkdir()
    _git(path, "init", "--quiet")
    (path / "greeting.py").write_text('def greet():\n    return "hello"\n')
    before = _commit(path, "Add greeting")
    (path / "greeting.py").write_text('def greet():\n    return "hello there"\n')
    after = _commit(path, "Tidy greeting")
    return FixtureRepository(path=path, before=before, after=after)


def model_listing(*claims: str) -> ScriptedModel:
    """A stand-in for the model whose one response to the Claim extractor lists these Claims."""
    return ScriptedModel([json.dumps({"claims": claims})])
