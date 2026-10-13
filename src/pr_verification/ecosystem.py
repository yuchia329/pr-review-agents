"""The Ecosystem boundary: everything specific to one language and its tooling sits behind it.

An Ecosystem works out how to install and test a repository, reads test results into a
structured form, and says how a Reproduction is written, placed, and run. Nothing here names a
language: an Ecosystem for Python, for TypeScript/JavaScript, or for Go answers the same
questions in the same terms. Each Ecosystem lives in its own module, and this module is the only
one in the package that imports an Ecosystem's module: everything else reaches an Ecosystem
through `find_commands`.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from pr_verification.sandbox import Command, RunResult


@dataclass(frozen=True)
class Commands:
    """How to install and test one repository at one commit inside the Sandbox."""

    ecosystem: "Ecosystem"
    """The Ecosystem that worked these out, and so the one to read the test results with."""

    image: str
    """The container image that holds the Ecosystem's tooling, for a Sandbox to run in."""

    install: tuple[Command, ...]
    """Commands to run in order in `Phase.INSTALL`, from the root of the working copy. Each
    must exit with status 0 for the next to be worth running. May be empty."""

    test: Command
    """The command that runs the repository's tests in `Phase.TEST`, from the root of the
    working copy, after the install commands. Its result is what `read_results` reads."""

    test_framework: str = ""
    """The name of the test framework `test` runs, as that framework's own project writes it,
    such as `pytest`. Empty when the Ecosystem's test tooling has no name."""

    test_layout: tuple[str, ...] = ()
    """Where the commit keeps its tests: the directories that hold its test files, as paths
    from the root of the repository with `/` between their parts and `.` for the root itself,
    sorted, each once. Each Ecosystem states what it takes for a test file. Empty when the
    commit holds none."""

    lock_files: tuple[str, ...] = ()
    """The dependency lock file of the repository, as the files that make it up: the paths from
    the root of the repository of every file the Ecosystem reads to decide `image` and
    `install`, sorted, each once. A path is named whether or not the commit holds a file
    there, so that a file added later counts as a change. Two commits that agree on every
    path, each holding the same contents there or each holding no file, get the same `image`
    and the same `install`. These files are what the system takes the lock file to be: what
    the install commands go on to read for themselves, such as a file that one of these
    includes, is not among them. A repository that pins nothing still has them: they are
    then the files that declare its dependencies. Empty when the Ecosystem names none, and
    the lock file is then never seen to change."""


@dataclass(frozen=True)
class Unsupported:
    """An Ecosystem could not work out how to install or test the repository.

    This is an answer, not an error. A Claim that needed code run in this repository cannot be
    verified and is Inconclusive, with this reason.
    """

    reason: str
    """One sentence a pull request author can act on, saying what was not found. Each
    Ecosystem states its exact wording."""


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
    """One sentence saying why. Its wording is not part of the contract."""


class Ecosystem(Protocol):
    """A programming language together with its dependency and test tooling."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        """Work out how to install and test `repository` at `commit`, without being told.

        Only reads the files of `commit`. It runs none of them, and the state of the working
        tree does not matter. The answer is for that commit alone: the commits before and after
        a pull request may get different answers, and different images.

        Returns `Unsupported` when it cannot work out how to install the repository or how to
        test it. Never raises for what the repository holds.
        """
        ...

    def read_results(self, run: RunResult) -> Results | Unreadable:
        """Read the result of running `Commands.test` into a structured form.

        Returns `Unreadable` when the run was stopped for hitting a limit, or when its output
        holds no test results, as when the test tooling itself could not start. A run in which
        the tooling started and found no tests gives `Results` with all three empty.

        The output is Untrusted Content: it is read as data, and nothing in it is acted on.
        """
        ...


LONGEST_REPRODUCTION = 20_000
"""The most characters the text of one Reproduction may hold and still be placed. A command
carries the text in an argument, and this many characters fit in one as UTF-8 in base64."""


@dataclass(frozen=True)
class Placement:
    """Where one Reproduction goes in a working copy, and how it is put there and run."""

    path: str
    """The file the Reproduction is written to, by its path from the root of the working copy."""

    write: Command
    """Run from the root of the working copy, writes the Reproduction to `path`, replacing
    whatever is there, and exits with status 0 when it has. It needs no network."""

    run: Command
    """Run from the root of the working copy after `write`, in `Phase.TEST`, runs the tests
    of the Reproduction. The repository's own settings for the test tooling still apply, so
    the tooling may run other tests along with them, and the code under test prints what it
    likes. Which tests count, and where their results are read from, is said under
    `take_results`."""

    take_results: Command | None = None
    """Run from the root of the working copy after `run`, in `Phase.TEST`, however `run`
    ended: prints what the results file of that run holds and nothing else, then removes the
    results file and the file at `path`. It needs no network.

    The results file is written by the Ecosystem's test tooling during `run`, and what it
    holds is the Ecosystem's own business: `ResultsFileEcosystem.read_results_file` reads it.
    The command exits with status 0 when it printed the file and neither file is left in the
    working copy. With no results file to print, or a file it could not remove, it exits
    with another status. Either way it removes what it can, so it also serves where `write`
    failed or `run` never happened.

    None when the Ecosystem's tooling keeps no results file. The result of `run` is then
    read with `Ecosystem.read_results`, as the result of `Commands.test` is, and nothing is
    removed. An Ecosystem that gives this command is a `ResultsFileEcosystem`."""


@runtime_checkable
class ReproductionEcosystem(Ecosystem, Protocol):
    """An Ecosystem that also says how a Reproduction is written, placed, and run.

    The Ecosystem that `find_commands` gives in `Commands.ecosystem` is asked with
    `isinstance` whether it is one.
    """

    def reproduction_form(self) -> str:
        """What a Reproduction is in this Ecosystem, told to the model that writes one.

        Written by the project and the same every time: it holds nothing of any repository
        or pull request, so it can be given to a model as instructions.
        """
        ...

    def place(self, reproduction: str) -> Placement:
        """Work out where the text of one Reproduction goes and the commands to run it.

        Runs nothing and writes nothing itself: the commands are for a Sandbox to run. The
        same placement serves the commits before and after a pull request, each in its own
        working copy, once the install commands have been run there.

        Any text of up to `LONGEST_REPRODUCTION` characters can be placed, whatever
        characters it holds: the file written holds exactly that text, as UTF-8, and nothing
        in the text is read as anything but the content of the file.
        """
        ...


@runtime_checkable
class ResultsFileEcosystem(ReproductionEcosystem, Protocol):
    """A ReproductionEcosystem whose test tooling writes a Reproduction's results to a file.

    What a run prints is written by everything that ran: the test tooling, and the code
    under test, which can print an imitation of the tooling's results. So the results of a
    Reproduction are never read from what its run printed. They are read from a results
    file that the tooling writes, under a name chosen at random for the placement, and that
    a second command prints: every Placement such an Ecosystem gives has `take_results`.
    Only the tests of the Reproduction itself are read from it.

    The Ecosystem that the Reproduction writer is given is asked with `isinstance` whether
    it is one.

    This is not airtight. The code under test runs in the same process as the test tooling
    and as the same user, so a pull request that sets out to can learn the name of the
    results file, write the file itself, or change what the tooling writes into it. What is
    closed is the pull request that only prints, or that writes to a name known before the
    run. The worst a manipulated run gives is still a false Confirmed (ADR 0002).
    """

    def read_results_file(self, ran: RunResult, taken: RunResult) -> Results | Unreadable:
        """Read the results of one run of a Reproduction from its results file.

        `ran` is the result of a Placement's `run` against one commit, and `taken` the
        result of the same Placement's `take_results` against that commit afterwards.

        Returns `Unreadable` when `ran` was stopped for hitting a limit, when `taken` was
        stopped for hitting a limit or did not exit with status 0, or when what `taken`
        printed is not a results file the tooling wrote whole.

        Otherwise returns `Results` that name the tests of the Reproduction's own file and
        no other, each as `read_results` names it. A test the tooling ran from any other
        file is in none of the three. When the tooling ran none of the Reproduction's
        tests, as when the repository's own settings stop it from being collected, all
        three are empty.

        Nothing `ran` printed is read. For the same `taken`, the answer is the same whatever
        `ran.output` holds and whatever status `ran` exited with.

        Both runs are Untrusted Content: they are read as data, and nothing in them is acted
        on. Never raises for what they hold.
        """
        ...


def find_commands(repository: Path, commit: str) -> Commands | Unsupported:
    """Work out how to install and test `repository` at `commit`, whatever its Ecosystem.

    Asks each Ecosystem the system has and returns the first `Commands` given. When none
    can, returns `Unsupported`. While the system has one Ecosystem, the reason is that
    Ecosystem's own.
    """
    # Imported here because each Ecosystem's module imports this one.
    from pr_verification.python_ecosystem import PythonEcosystem

    ecosystems: tuple[Ecosystem, ...] = (PythonEcosystem(),)
    for ecosystem in ecosystems:
        answer = ecosystem.commands(repository, commit)
        if isinstance(answer, Commands):
            break
    return answer
