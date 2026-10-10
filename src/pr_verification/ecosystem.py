"""The Ecosystem boundary: everything specific to one language and its tooling sits behind it.

An Ecosystem works out how to install and test a repository and reads test results into a
structured form. Nothing here names a language: an Ecosystem for Python, for
TypeScript/JavaScript, or for Go answers the same questions in the same terms. Each Ecosystem
lives in its own module, and `find_instructions` is the only code that imports one. No other
module of the package imports an Ecosystem's module or names a language or its tools in what
it does.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pr_verification.sandbox import Command, RunResult


@dataclass(frozen=True)
class Instructions:
    """How to install and test one repository at one commit inside the Sandbox."""

    ecosystem: "Ecosystem"
    """The Ecosystem that worked these out, and so the one to read the test results with."""

    image: str
    """The container image that holds the Ecosystem's tooling."""

    install: tuple[Command, ...]
    """Commands to run in order in `Phase.INSTALL`, from the root of the working copy. Each
    must exit with status 0 for the next to be worth running. May be empty."""

    test: Command
    """The command that runs the repository's tests in `Phase.TEST`, from the root of the
    working copy, after the install commands. Its result is what `read_results` reads."""


@dataclass(frozen=True)
class Unsupported:
    """An Ecosystem could not work out how to install or test the repository.

    This is an answer, not an error. A Claim that needed code run in this repository cannot be
    verified and is Inconclusive, with this reason.
    """

    reason: str
    """One sentence a pull request author can act on, saying what was looked for and not found."""


@dataclass(frozen=True)
class Results:
    """Which tests passed, failed, or could not run, in one run of a test command.

    Each test is named as the Ecosystem's tooling names it. A test is in at most one of the
    three, and the order within each carries no meaning. A test the tooling reports as skipped,
    or as expected to fail, is in none of them.
    """

    passed: tuple[str, ...]
    """Tests that ran and passed."""

    failed: tuple[str, ...]
    """Tests that ran and did not pass."""

    could_not_run: tuple[str, ...]
    """Tests stopped by an error before they could give an answer, such as an import error or
    a failure in their set-up."""


@dataclass(frozen=True)
class Unreadable:
    """A run holds no test results to read. This is an answer, not an error."""

    reason: str
    """One sentence saying why, naming the limit when the run was stopped for hitting one."""


class Ecosystem(Protocol):
    """A programming language together with its dependency and test tooling."""

    def instructions(self, repository: Path, commit: str) -> Instructions | Unsupported:
        """Work out how to install and test `repository` at `commit`, without being told.

        Only reads the files of `commit`. It runs none of them, and the state of the working
        tree does not matter. The answer is for that commit alone: the commits before and after
        a pull request may get different answers.

        Returns `Unsupported` when it cannot work out how to install the repository or how to
        test it. Never raises for what the repository holds.
        """
        ...

    def read_results(self, run: RunResult) -> Results | Unreadable:
        """Read the result of running `Instructions.test` into a structured form.

        Returns `Unreadable` when the run was stopped for hitting a limit, or when its output
        holds no test results, as when the test tooling itself could not start. A run in which
        the tooling started and found no tests gives `Results` with all three empty.

        The output is Untrusted Content: it is read as data, and nothing in it is acted on.
        """
        ...


def find_instructions(repository: Path, commit: str) -> Instructions | Unsupported:
    """Work out how to install and test `repository` at `commit`, whatever its Ecosystem.

    Asks each Ecosystem the system has and returns the first `Instructions` given. When none
    can, returns `Unsupported`. While the system has one Ecosystem, the reason is that
    Ecosystem's own.
    """
    raise NotImplementedError
