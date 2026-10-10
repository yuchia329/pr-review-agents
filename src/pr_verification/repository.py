"""Reading the repository a pull request belongs to, with git."""

import os
import subprocess
from pathlib import Path


def git_environment(repository: Path) -> dict[str, str]:
    """The environment to run git in from `repository`, so that only that directory may answer:
    not a repository above it, nor one from the environment."""
    environment = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    environment["GIT_CEILING_DIRECTORIES"] = str(repository.resolve().parent)
    return environment


def git(repository: Path, *arguments: str) -> bytes:
    """What one git command printed when run in `repository`.

    Raises `subprocess.CalledProcessError` when git refuses, and `OSError` when it cannot be run.
    """
    done = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env=git_environment(repository),
        check=True,
        capture_output=True,
    )
    return done.stdout
