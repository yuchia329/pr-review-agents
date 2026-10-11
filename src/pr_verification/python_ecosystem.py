"""The Python Ecosystem: pip to install, pytest to test."""

import base64
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet

from pr_verification.ecosystem import Commands, Placement, Results, Unreadable, Unsupported
from pr_verification.repository import git
from pr_verification.sandbox import RunResult

NOT_A_PYTHON_PROJECT = (
    "No Python project was found at this commit: the repository has no packaging metadata, "
    "no requirements.txt, and no .py file."
)

REPRODUCTION_PATH = "test_pr_verification_reproduction.py"
"""Where a Reproduction is placed: a test file at the root of the working copy."""

PYTHON_VERSIONS = ("3.14", "3.13", "3.12", "3.11", "3.10")
"""The Python versions a repository can be run on, newest first."""

LOCK_FILES = (
    "dev-requirements.txt",
    "pyproject.toml",
    "requirements-dev.txt",
    "requirements-test.txt",
    "requirements.txt",
    "setup.cfg",
    "setup.py",
    "test-requirements.txt",
)
"""The root files that decide how a Python repository is installed, sorted. A file that one of
them includes in turn, as a requirements file can, is not followed."""

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

# Run in isolated mode, so that no file of the repository can pose as a module of the standard
# library. The text arrives as base64 in an argument of its own: no shell reads it, and Python
# reads it only as the content of the file.
_WRITE = (
    "python", "-I", "-c",
    "import base64, pathlib, sys; "
    "pathlib.Path(sys.argv[1]).write_bytes(base64.b64decode(sys.argv[2]))",
)  # fmt: skip

_REPRODUCTION_FORM = """\
A Reproduction here is one pytest test file: Python source that holds at least one test function
whose name starts with `test_`. Each test function asserts one thing about how the code behaves.

The file is placed at the root of the repository and pytest runs it from there, alone, so it
imports the repository's code as a test file at the root would. Import the repository's code
inside each test function and never at the top of the file. A name that the code before the pull
request lacks then fails that one test, where an import at the top of the file would stop every
test from running.

The tests run with no network. They can use the standard library, pytest, and whatever the
repository's own tests can import, and nothing else."""

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

        `test_framework` is `pytest`. `test_layout` names each directory that holds a file of
        the commit named `test_*.py` or `*_test.py`, which is what pytest takes for a test
        file. `lock_files` is `LOCK_FILES`, whichever of them the commit holds.

        Returns `Unsupported` with the reason `NOT_A_PYTHON_PROJECT` when the commit matches
        none of the layouts.
        """
        # Each entry is `<mode> <type> <object>\t<path>`. A submodule is listed and is not a file.
        listing = os.fsdecode(git(repository, "ls-tree", "-r", "-z", "--end-of-options", commit))
        files = [
            path
            for details, _, path in (entry.partition("\t") for entry in listing.split("\0"))
            if " blob " in details
        ]
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

    def reproduction_form(self) -> str:
        """See `ReproductionEcosystem.reproduction_form`.

        A Reproduction is one pytest test file that holds at least one test function. The
        model is told to import the repository's code inside each test function, so that a
        name the code before the pull request lacks fails that test, where an import at the
        top of the file would stop any test from running.
        """
        return _REPRODUCTION_FORM

    def place(self, reproduction: str) -> Placement:
        """See `ReproductionEcosystem.place`.

        `path` is `REPRODUCTION_PATH`, at the root of the working copy, so a Reproduction
        imports the repository's code as a test file of its own at the root would. `run`
        runs pytest over that file alone, and `read_results` names each of its tests by node
        ID, as in `test_pr_verification_reproduction.py::test_total_adds_tax`.
        """
        # Half of a character that takes two code units in UTF-16 has no UTF-8 of its own, and a
        # model can write one. It is written as it stands, and Python then refuses the file.
        content = base64.b64encode(reproduction.encode(errors="surrogatepass")).decode("ascii")
        return Placement(
            path=REPRODUCTION_PATH,
            write=(*_WRITE, REPRODUCTION_PATH, content),
            run=(*_TEST, REPRODUCTION_PATH),
        )


def _pyproject(repository: Path, commit: str) -> dict[str, Any]:
    """The tables of the commit's `pyproject.toml`, or none when it cannot be parsed."""
    try:
        text = git(repository, "cat-file", "blob", f"{commit}:pyproject.toml").decode()
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
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
