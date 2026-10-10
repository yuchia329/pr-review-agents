"""The Python Ecosystem: pip to install, pytest to test."""

import os
import re
import subprocess
import tomllib
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from pr_verification.ecosystem import Commands, Results, Unreadable, Unsupported
from pr_verification.repository import git
from pr_verification.sandbox import RunResult

NOT_A_PYTHON_PROJECT = (
    "No Python project was found at this commit: the repository has no packaging metadata, "
    "no requirements.txt, and no .py file."
)

PYTHON_VERSIONS = ("3.14", "3.13", "3.12", "3.11", "3.10")
"""The Python versions a repository can be run on, newest first."""

_TEST_NAMES = ("test", "tests", "testing", "dev")
"""What an extra or a dependency group that holds a project's test dependencies is named."""

_REQUIREMENTS = (
    "requirements.txt",
    "requirements-dev.txt",
    "requirements-test.txt",
    "dev-requirements.txt",
    "test-requirements.txt",
)
"""The root files that list what a project needs: the first to run, the rest to be tested."""

# Into the home directory, which is the one place the Sandbox's user can install to.
_INSTALL = ("python", "-m", "pip", "install", "--user")

# `-rA` has pytest end with one line for each test, which is what `read_results` reads, and
# `--verbosity=0` keeps those lines as they are read whatever the repository's own settings ask.
# One file that cannot be collected would otherwise stop every other file's tests from running.
_TEST = ("python", "-m", "pytest", "-rA", "--verbosity=0", "--continue-on-collection-errors")

_SUMMARY_STARTS = re.compile(r"^=+ short test summary info =+$", re.MULTILINE)
_SESSION_ENDED = re.compile(r"^=+ .+ in \d[\d.]*s( \([\d:]+\))? =+$", re.MULTILINE)


class PythonEcosystem:
    """Works out how to install and test a Python repository, and reads pytest's results.

    Layouts covered, recognised from the files of the commit:

    - A packaged project: at the root, a `pyproject.toml` with a `[project]` or
      `[build-system]` table, or a `setup.py`, or a `setup.cfg`. Its packages may sit at the
      root or under `src/`.
    - A project with requirements files: a `requirements.txt` at the root and no packaging.
    - Plain modules: `.py` files anywhere and neither of the above.

    A repository that is none of these is not a Python project.
    """

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        """See `Ecosystem.commands`.

        The install commands, once all have exited with status 0, leave these importable in
        the test phase:

        - the project itself and its dependencies, for a packaged project;
        - what `requirements.txt` lists, when there is one at the root;
        - the project's test dependencies, where it declares them in a usual place: an extra
          or a dependency group named `test`, `tests`, `testing`, or `dev` in `pyproject.toml`,
          or a root file named `requirements-dev.txt`, `requirements-test.txt`,
          `dev-requirements.txt`, or `test-requirements.txt`;
        - pytest, always.

        The test command runs pytest over the whole repository. pytest also runs unittest-style
        tests, so a Python project always has a test command, and a repository with no tests
        yet is still supported.

        `image` is the official Python image `python:<version>-slim`, where the version is the
        first in `PYTHON_VERSIONS` that `requires-python` in `pyproject.toml` allows, or the
        first of all when the commit does not state one there.

        Returns `Unsupported` with the reason `NOT_A_PYTHON_PROJECT` when the commit matches
        none of the layouts.
        """
        listing = git(repository, "ls-tree", "-r", "-z", "--name-only", "--end-of-options", commit)
        files = os.fsdecode(listing).split("\0")
        at_root = {name for name in files if "/" not in name}
        pyproject = _pyproject(repository, commit) if "pyproject.toml" in at_root else {}
        packaged = (
            "project" in pyproject
            or "build-system" in pyproject
            or "setup.py" in at_root
            or "setup.cfg" in at_root
        )
        if not (packaged or "requirements.txt" in at_root or any(f.endswith(".py") for f in files)):
            return Unsupported(NOT_A_PYTHON_PROJECT)

        to_install = ["pytest"]
        if packaged:
            extras = _named_for_tests(_table(_table(pyproject, "project"), "optional-dependencies"))
            to_install.append(f".[{','.join(extras)}]" if extras else ".")
        for group in _named_for_tests(_table(pyproject, "dependency-groups")):
            to_install += ["--group", group]
        for requirements in _REQUIREMENTS:
            if requirements in at_root:
                to_install += ["--requirement", requirements]
        return Commands(
            ecosystem=self,
            image=_image(pyproject),
            # pip reads dependency groups only from 25.1 on, and some of the images hold an older.
            install=((*_INSTALL, "--upgrade", "pip"), (*_INSTALL, *to_install)),
            test=_TEST,
        )

    def read_results(self, run: RunResult) -> Results | Unreadable:
        """See `Ecosystem.read_results`.

        Each test is named by its pytest node ID, such as `tests/test_login.py::test_empty` or
        `tests/test_login.py::test_user[admin]`.

        - `passed`: tests pytest reports as passed.
        - `failed`: tests pytest reports as failed.
        - `could_not_run`: tests pytest reports as errors, such as a failing fixture. A file
          pytest could not collect, such as one with an import error, is named by its path,
          as in `tests/test_login.py`.

        Tests pytest reports as skipped, xfailed, or xpassed are in none of the three.
        """
        if run.limit_hit is not None:
            return Unreadable(f"The run was stopped at the {run.limit_hit} limit.")
        if not _SESSION_ENDED.search(run.output):
            return Unreadable("The output does not hold the end of a pytest session.")
        _, *summaries = _SUMMARY_STARTS.split(run.output)
        named: dict[str, set[str]] = {"PASSED": set(), "FAILED": set(), "ERROR": set()}
        for line in summaries[-1].splitlines() if summaries else ():
            word, _, rest = line.partition(" ")
            if word == "PASSED":
                named[word].add(rest)
            elif word in named:
                # After a test that did not pass come ` - ` and the start of the error's message.
                named[word].add(rest.split(" - ")[0])
        # pytest can report one test twice, as when it passes and its tear-down then fails.
        could_not_run = named["ERROR"]
        failed = named["FAILED"] - could_not_run
        passed = named["PASSED"] - failed - could_not_run
        return Results(
            passed=tuple(sorted(passed)),
            failed=tuple(sorted(failed)),
            could_not_run=tuple(sorted(could_not_run)),
        )


def _pyproject(repository: Path, commit: str) -> dict[str, Any]:
    """The tables of the commit's `pyproject.toml`, or none when it is not a file of TOML."""
    try:
        text = git(repository, "cat-file", "blob", f"{commit}:pyproject.toml").decode()
        parsed = tomllib.loads(text)
    except (subprocess.CalledProcessError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return {}
    return {name: table for name, table in parsed.items() if isinstance(table, dict)}


def _table(tables: dict[str, Any], name: str) -> dict[str, Any]:
    """The table of that name, or an empty one when there is none or it is not a table."""
    table = tables.get(name)
    return table if isinstance(table, dict) else {}


def _named_for_tests(table: dict[str, Any]) -> list[str]:
    """The extras or dependency groups in a table that hold test dependencies, as written."""
    return [name for name in table if name.lower() in _TEST_NAMES]


def _image(pyproject: dict[str, Any]) -> str:
    stated = _table(pyproject, "project").get("requires-python")
    try:
        allowed = SpecifierSet(stated if isinstance(stated, str) else "")
    except InvalidSpecifier:
        # Not something that states a version, so every version is allowed.
        allowed = SpecifierSet()
    version = next(filter(allowed.contains, PYTHON_VERSIONS), PYTHON_VERSIONS[0])
    return f"python:{version}-slim"
