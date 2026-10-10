"""The Python Ecosystem: pip to install, pytest to test."""

from pathlib import Path

from pr_verification.ecosystem import Instructions, Results, Unreadable, Unsupported
from pr_verification.sandbox import RunResult

NOT_A_PYTHON_PROJECT = (
    "No Python project was found at this commit: the repository has no pyproject.toml, "
    "setup.py, setup.cfg, requirements.txt, or .py file."
)


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

    def instructions(self, repository: Path, commit: str) -> Instructions | Unsupported:
        """See `Ecosystem.instructions`.

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

        `image` is an official Python image, named `python:<version>-slim`.

        Returns `Unsupported` with the reason `NOT_A_PYTHON_PROJECT` when the commit matches
        none of the layouts.
        """
        raise NotImplementedError

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
        raise NotImplementedError
