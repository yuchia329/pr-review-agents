"""Keeps tests/ locked while on an impl/ branch.

    tests_lock.py hook                                 judge a Claude Code hook event on stdin
    tests_lock.py pull-request --branch B --base REF   judge the pull request checked out here

Exits 2, with the reason on stderr, when it refuses or cannot tell; 0 otherwise.

Standard library only: the hook runs under whatever python3 is on the path, outside the
project's environment.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

EXIT_REFUSED = 2
LOCAL_BASES = ("refs/heads/main", "refs/remotes/origin/main")
REFUSAL = (
    "tests/ is locked on an impl/ branch. These files must stay as main has them; "
    "restore any that have changed:\n{files}\n"
    "If a test looks wrong or contradicts the contract, stop and report it instead."
)


def is_locked_branch(branch: str) -> bool:
    return branch.startswith("impl/")


def is_test_path(path: str, root: str) -> bool:
    """Whether a path, absolute or relative to the repository root, lies under its tests/.

    Letter case is ignored, because some file systems ignore it and Tests/ is then tests/.
    """
    tests = os.path.realpath(os.path.join(root, "tests")).casefold()
    target = os.path.realpath(os.path.join(root, path)).casefold()
    return os.path.commonpath([tests, target]) == tests


def _git(directory: str, *args: str) -> str:
    command = ["git", "-C", directory, "-c", "core.quotePath=off", *args]
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout.strip()


def _repository_root(path: str) -> str | None:
    """The root of the working tree that holds a path, whether or not the path exists yet."""
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    try:
        return _git(path, "rev-parse", "--show-toplevel")
    except subprocess.CalledProcessError:
        return None


def changed_test_files(root: str, *bases: str) -> list[str]:
    """Files under tests/ that no longer match where the checked-out branch left the bases."""
    fork = _git(root, "merge-base", "HEAD", *bases)
    tracked = _git(root, "diff", "--name-only", fork, "--", "tests")
    untracked = _git(root, "ls-files", "--others", "--exclude-standard", "--", "tests")
    return sorted({*tracked.splitlines(), *untracked.splitlines()})


def touched_by_event(event: dict[str, Any]) -> list[str]:
    """The locked files one Claude Code hook event is about to write, or has left changed."""
    tool_input = event["tool_input"]
    path = tool_input.get("file_path") or tool_input.get("notebook_path")
    target = os.path.realpath(os.path.join(event["cwd"], path or ""))
    root = _repository_root(target)
    if root is None or not is_locked_branch(_git(root, "branch", "--show-current")):
        return []
    if path is None:
        # A shell command names no file and has already run, so compare the whole of tests/.
        bases = _git(root, "for-each-ref", "--format=%(refname)", *LOCAL_BASES).split()
        if not bases:
            raise LookupError("no main branch to compare tests/ with")
        return changed_test_files(root, *bases)
    return [os.path.relpath(target, root)] if is_test_path(target, root) else []


def touched_by_pull_request(branch: str, base: str) -> list[str]:
    """The locked files changed by the pull request whose merge is checked out here."""
    if not is_locked_branch(branch):
        return []
    return changed_test_files(_git(os.getcwd(), "rev-parse", "--show-toplevel"), base)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Keeps tests/ locked while on an impl/ branch.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("hook")
    pull_request = commands.add_parser("pull-request")
    pull_request.add_argument("--branch", required=True)
    pull_request.add_argument("--base", required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "hook":
            touched = touched_by_event(json.load(sys.stdin))
        else:
            touched = touched_by_pull_request(args.branch, args.base)
    except Exception as error:
        reason = getattr(error, "stderr", "").strip() or repr(error)
        print(f"The lock on tests/ could not be checked, so it refuses: {reason}", file=sys.stderr)
        return EXIT_REFUSED
    if touched:
        print(REFUSAL.format(files="\n".join(touched)), file=sys.stderr)
        return EXIT_REFUSED
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
