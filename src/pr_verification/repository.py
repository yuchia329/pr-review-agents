"""Reading the repository a pull request belongs to, and making one for a case, with git."""

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

# Who a commit made here is by, whatever the machine it is made on is set up to say.
_COMMITTER = (
    "-c", "user.name=pr-verification",
    "-c", "user.email=pr-verification@example.invalid",
    "-c", "commit.gpgsign=false",
)  # fmt: skip


def git_environment(repository: Path) -> dict[str, str]:
    """The environment to run git in from `repository`, so that only that directory may answer:
    not a repository above it, nor one from the environment."""
    environment = {k: v for k, v in os.environ.items() if k not in ("GIT_DIR", "GIT_WORK_TREE")}
    environment["GIT_CEILING_DIRECTORIES"] = str(repository.resolve().parent)
    return environment


def git(
    repository: Path,
    *arguments: str,
    stdin: bytes | None = None,
    environment: Mapping[str, str] | None = None,
) -> bytes:
    """What one git command printed when run in `repository`, given `stdin` to read if any.

    `environment` is added to the environment git is run in. It is where to put what must not
    be among the arguments, which the error git's refusal raises repeats.

    Raises `subprocess.CalledProcessError` when git refuses, and `OSError` when it cannot be run.
    """
    done = subprocess.run(
        ["git", *arguments],
        cwd=repository,
        env={**git_environment(repository), **(environment or {})},
        input=stdin,
        check=True,
        capture_output=True,
    )
    return done.stdout


def resolve_commit(repository: Path, commit: str) -> str:
    """The full ID of the commit that `commit` names in `repository`. Raises as `git` does."""
    name = f"{commit}^{{commit}}"
    return git(repository, "rev-parse", "--verify", "--end-of-options", name).decode().strip()


def fetch_commit(
    repository: Path, source: str, commit: str, *, environment: Mapping[str, str] | None = None
) -> None:
    """Fetch the commit with the full ID `commit`, and its ancestors, into `repository` from
    `source`, which is a remote of the repository, a URL, or a path.

    Nothing is checked out and no ref is made or moved. git's own record of a fetch names
    `source`, so none is written. `environment` is as `git` takes it. Raises as `git` does.
    """
    # Protocol 2 hands over a commit asked for by its ID whether or not a ref points at it.
    asking = ("-c", "protocol.version=2", "fetch", "--quiet", "--no-tags", "--no-write-fetch-head")
    naming = ("--no-recurse-submodules", "--end-of-options", source, commit)
    git(repository, *asking, *naming, environment=environment)


def new_repository(directory: Path) -> Path:
    """Make an empty repository in a new directory of its own under `directory`.

    It has no working copy. What it holds is put there and read through git, as the Sandbox
    reads it, so no file of a commit is written to this machine's disk, where a file system
    that folds letter case or a filter the machine has set up could change it.

    The directory's name says nothing of what the repository is for: the system is shown the
    path of a case's repository, and must not be able to read the case's answer from it.
    """
    repository = Path(tempfile.mkdtemp(prefix="repository-", dir=directory))
    git(repository, "init", "--quiet", "--bare")
    return repository


def repository_at(source: Path, commit: str, directory: Path) -> Path:
    """Make a repository under `directory` as `new_repository` does, with `commit` staged.

    It holds `commit` and its ancestors, taken from `source`, and no other commit, no ref, and
    no note of where they were taken from. `source` is only read. Raises as `git` does.
    """
    commit = resolve_commit(source, commit)
    repository = new_repository(directory)
    fetch_commit(repository, str(source.resolve()), commit)
    git(repository, "read-tree", commit)
    return repository


def commit_staged(repository: Path, message: str, *parents: str) -> str:
    """Commit what is staged in `repository`, with these parents, and return its full ID.

    The new commit is what `HEAD` names afterwards. No branch is made or moved.
    """
    tree = git(repository, "write-tree").decode().strip()
    onto = [argument for parent in parents for argument in ("-p", parent)]
    made = git(repository, *_COMMITTER, "commit-tree", tree, *onto, "-m", message)
    commit = made.decode().strip()
    git(repository, "update-ref", "--no-deref", "HEAD", commit)
    return commit
