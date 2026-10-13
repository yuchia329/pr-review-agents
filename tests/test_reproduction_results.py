"""Where a Reproduction's results are read from, and what an attempt leaves behind.

The tests down to `shop` use a stand-in Sandbox and a stand-in Ecosystem. The ones from there
on install pytest from the package index and run Reproductions in a real Sandbox.
"""

import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from conftest import FixtureRepository, commit_snapshots, finishes
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification.ecosystem import (
    Commands,
    Placement,
    ReproductionEcosystem,
    Results,
    ResultsFileEcosystem,
    Unreadable,
    Unsupported,
    find_commands,
)
from pr_verification.model import ScriptedModel
from pr_verification.python_ecosystem import REPRODUCTION_PATH, PythonEcosystem
from pr_verification.reproduction import (
    ATTEMPT_CAP_REACHED,
    COULD_NOT_FINISH,
    ENDINGS,
    AttemptResult,
    ReproductionRun,
    write_reproduction,
)
from pr_verification.sandbox import (
    Command,
    ContainerSandbox,
    LimitHit,
    Limits,
    Phase,
    RunResult,
    Sandbox,
    SandboxFailed,
)

CLAIM = "Adds tax to the total"
LIMITS = Limits(seconds=120.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)
INSTALL_LIMITS = Limits(seconds=600.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)
COULD_NOT_RUN = AttemptResult.COULD_NOT_RUN
SHOWS_NOTHING = AttemptResult.SHOWS_NOTHING
SHOWS_A_DIFFERENCE = AttemptResult.SHOWS_A_DIFFERENCE


def ended(output: str, exit_status: int = 0) -> RunResult:
    return RunResult(output=output, exit_status=exit_status, limit_hit=None)


def writes(text: str) -> str:
    return finishes(reproduction=text)


def write(
    repository: Path,
    before: str,
    after: str,
    *texts: str,
    sandbox: Sandbox,
    ecosystem: ReproductionEcosystem,
) -> tuple[ReproductionRun, ScriptedModel]:
    """What the writer records when the model writes these Reproductions, one for each attempt,
    and what the model was sent."""
    model = ScriptedModel([writes(text) for text in texts])
    run = write_reproduction(
        CLAIM,
        model=model,
        sandbox=sandbox,
        ecosystem=ecosystem,
        repository=repository,
        before=before,
        after=after,
        limits=LIMITS,
        attempt_cap=len(texts),
    )
    return run, model


# The writer, with a stand-in Sandbox and a stand-in Ecosystem that keeps a results file


@pytest.fixture(scope="module")
def pricing(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    path = tmp_path_factory.mktemp("pricing") / "repository"
    before, after = commit_snapshots(
        path,
        {"pricing.py": "def total(net):\n    return net\n"},
        {"pricing.py": "def total(net):\n    return round(net * 1.2, 2)\n"},
    )
    return FixtureRepository(path=path, before=before, after=after)


@dataclass(frozen=True)
class Behaves:
    """What running one Reproduction prints, and what its results file then holds, on the code
    before and the code after."""

    printed: tuple[str, str]
    filed: tuple[str, str]


WRITE_FAILED = ended("read-only file system", 1)
NOTHING_TO_TAKE = ended("no results file", 1)
BEHAVIOUR: Mapping[str, Behaves] = {
    "fixed": Behaves(
        printed=("failed test_tax PRINTED", "ok test_tax PRINTED"),
        filed=("failed test_tax", "ok test_tax"),
    ),
    # The code under test prints that the test passes after. The results file says it fails.
    "prints a difference": Behaves(
        printed=("failed test_tax PRINTED", "ok test_tax PRINTED"),
        filed=("failed test_tax", "failed test_tax"),
    ),
    "prints the same": Behaves(
        printed=("ok test_tax PRINTED", "ok test_tax PRINTED"),
        filed=("failed test_tax", "ok test_tax"),
    ),
    # After the pull request, the repository's settings keep the Reproduction from being run.
    "not collected after": Behaves(
        printed=("failed test_tax PRINTED", "ok test_theirs PRINTED"),
        filed=("failed test_tax", ""),
    ),
}


def read_lines(output: str) -> Results:
    lines = [line.split() for line in output.splitlines()]
    return Results(
        passed=tuple(name for word, name, *_ in lines if word == "ok"),
        failed=tuple(name for word, name, *_ in lines if word == "failed"),
        could_not_run=(),
    )


class LineEcosystem:
    """A stand-in with nothing of Python in it. A run prints one line for each test, and the
    results file holds one line for each test."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        return Unsupported("A stand-in installs nothing.")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        return read_lines(run.output)

    def reproduction_form(self) -> str:
        return "A Reproduction is a list of the lines its tests print."

    def place(self, reproduction: str) -> Placement:
        return Placement(
            "reproduction.tap", ("write", reproduction), ("run-reproduction",), ("take-results",)
        )


class FileEcosystem(LineEcosystem):
    """The stand-in as a `ResultsFileEcosystem`. It keeps what it was asked to read."""

    def __init__(self) -> None:
        self.read: list[tuple[RunResult, RunResult]] = []

    def read_results_file(self, ran: RunResult, taken: RunResult) -> Results | Unreadable:
        self.read.append((ran, taken))
        if ran.limit_hit is not None or taken.exit_status != 0:
            return Unreadable("There is no results file to read.")
        return read_lines(taken.output)


class StandInSandbox:
    """Keeps what is written for each commit, and runs it as `BEHAVIOUR` says it behaves."""

    def __init__(self, repository: FixtureRepository, unwritable: str | None = None) -> None:
        self.runs: list[tuple[Command, str, Phase, Limits]] = []
        self._before, self._unwritable = repository.before, unwritable
        self._written: dict[str, str] = {}

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self.runs.append((command, commit, phase, limits))
        if command[0] == "write":
            if commit == self._unwritable:
                return WRITE_FAILED
            self._written[commit] = command[1]
            return ended("")
        if commit not in self._written:
            return NOTHING_TO_TAKE
        behaves = BEHAVIOUR[self._written[commit]]
        where = 0 if commit == self._before else 1
        if command[0] == "take-results":
            del self._written[commit]
            return ended(behaves.filed[where])
        return ended(behaves.printed[where])


class ThenNoRuntime:
    """A stand-in Sandbox that does some runs as another would and then cannot do any."""

    def __init__(self, sandbox: Sandbox, runs: int) -> None:
        self.asked = 0
        self._sandbox, self._runs = sandbox, runs

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self.asked += 1
        if self.asked > self._runs:
            raise SandboxFailed("no container runtime")
        return self._sandbox.run(command, commit=commit, phase=phase, limits=limits)


def write_standing_in(
    pricing: FixtureRepository,
    *texts: str,
    sandbox: Sandbox | None = None,
    ecosystem: FileEcosystem | None = None,
) -> tuple[ReproductionRun, ScriptedModel]:
    return write(
        pricing.path,
        pricing.before,
        pricing.after,
        *texts,
        sandbox=sandbox or StandInSandbox(pricing),
        ecosystem=ecosystem or FileEcosystem(),
    )


def test_results_are_taken_on_before_then_after_once_the_reproduction_has_run_on_both(
    pricing: FixtureRepository,
) -> None:
    sandbox = StandInSandbox(pricing)

    write_standing_in(pricing, "fixed", sandbox=sandbox)

    assert sandbox.runs == [
        (("write", "fixed"), pricing.before, Phase.TEST, LIMITS),
        (("write", "fixed"), pricing.after, Phase.TEST, LIMITS),
        (("run-reproduction",), pricing.before, Phase.TEST, LIMITS),
        (("run-reproduction",), pricing.after, Phase.TEST, LIMITS),
        (("take-results",), pricing.before, Phase.TEST, LIMITS),
        (("take-results",), pricing.after, Phase.TEST, LIMITS),
    ]


def test_each_commits_run_is_read_together_with_what_was_taken_for_that_commit(
    pricing: FixtureRepository,
) -> None:
    ecosystem = FileEcosystem()

    write_standing_in(pricing, "fixed", ecosystem=ecosystem)

    printed, filed = BEHAVIOUR["fixed"].printed, BEHAVIOUR["fixed"].filed
    # The contract does not say which commit is read first.
    assert sorted(ecosystem.read, key=lambda read: read[0].output == printed[1]) == [
        (ended(printed[0]), ended(filed[0])),
        (ended(printed[1]), ended(filed[1])),
    ]


@pytest.mark.parametrize(
    ("text", "result"),
    [("prints a difference", SHOWS_NOTHING), ("prints the same", SHOWS_A_DIFFERENCE)],
)
def test_result_follows_the_results_file_whatever_the_run_printed(
    pricing: FixtureRepository, text: str, result: AttemptResult
) -> None:
    run, _ = write_standing_in(pricing, text)

    (attempt,) = run.attempts
    assert attempt.result is result
    assert (run.evidence is not None) == (result is SHOWS_A_DIFFERENCE)


def test_evidence_holds_the_results_read_from_the_file_and_the_runs_of_the_reproduction(
    pricing: FixtureRepository,
) -> None:
    run, _ = write_standing_in(pricing, "prints the same")

    printed, filed = BEHAVIOUR["prints the same"].printed, BEHAVIOUR["prints the same"].filed
    evidence = run.evidence
    assert evidence is not None
    assert evidence.command == ("run-reproduction",)
    assert (evidence.before, evidence.after) == (ended(printed[0]), ended(printed[1]))
    assert evidence.before_results == read_lines(filed[0])
    assert evidence.after_results == read_lines(filed[1])


def test_attempt_records_the_runs_of_the_reproduction_and_the_role_is_shown_only_those(
    pricing: FixtureRepository,
) -> None:
    run, model = write_standing_in(pricing, "prints a difference", "fixed")

    printed = BEHAVIOUR["prints a difference"].printed
    assert len(run.attempts) == 2
    first = run.attempts[0]
    assert (first.before, first.after) == (ended(printed[0]), ended(printed[1]))
    (shown,) = json.loads(model.requests[1].data)["task"]["attempts"]
    assert (shown["before"], shown["after"]) == printed


def test_results_naming_none_of_the_reproductions_tests_on_one_commit_are_never_evidence(
    pricing: FixtureRepository,
) -> None:
    run, _ = write_standing_in(pricing, "not collected after")

    assert [attempt.result for attempt in run.attempts] == [COULD_NOT_RUN]
    assert run.evidence is None
    assert run.inconclusive == ATTEMPT_CAP_REACHED.format(attempts=1, ending=ENDINGS[COULD_NOT_RUN])


@pytest.mark.parametrize("unwritable", ["before", "after"])
def test_results_are_taken_on_both_commits_straight_after_a_placing_that_failed_and_not_read(
    pricing: FixtureRepository, unwritable: str
) -> None:
    commits = {"before": pricing.before, "after": pricing.after}
    sandbox = StandInSandbox(pricing, unwritable=commits[unwritable])
    ecosystem = FileEcosystem()

    run, _ = write_standing_in(pricing, "fixed", sandbox=sandbox, ecosystem=ecosystem)

    assert [(command, commit) for command, commit, *_ in sandbox.runs] == [
        (("write", "fixed"), pricing.before),
        (("write", "fixed"), pricing.after),
        (("take-results",), pricing.before),
        (("take-results",), pricing.after),
    ]
    (attempt,) = run.attempts
    placing = {"before": ended(""), "after": ended(""), unwritable: WRITE_FAILED}
    assert attempt.result is COULD_NOT_RUN
    assert {"before": attempt.before, "after": attempt.after} == placing
    assert ecosystem.read == []


def test_each_attempt_has_its_results_taken_before_the_next_is_placed(
    pricing: FixtureRepository,
) -> None:
    sandbox = StandInSandbox(pricing)

    write_standing_in(pricing, "prints a difference", "fixed", sandbox=sandbox)

    assert [command[0] for command, *_ in sandbox.runs] == [
        *["write"] * 2, *["run-reproduction"] * 2, *["take-results"] * 2,
        *["write"] * 2, *["run-reproduction"] * 2, *["take-results"] * 2,
    ]  # fmt: skip


@pytest.mark.parametrize("runs", [4, 5], ids=["taking", "taking on after"])
def test_sandbox_that_fails_while_results_are_taken_ends_the_work(
    pricing: FixtureRepository, runs: int
) -> None:
    sandbox = ThenNoRuntime(StandInSandbox(pricing), runs)

    run, model = write_standing_in(pricing, "fixed", "fixed", sandbox=sandbox)

    assert run.attempts == ()
    assert run.evidence is None
    assert run.inconclusive == COULD_NOT_FINISH.format(failure="no container runtime")
    assert sandbox.asked == runs + 1
    assert len(model.requests) == 1


def test_results_command_from_an_ecosystem_that_cannot_read_a_results_file_is_a_fault(
    pricing: FixtureRepository,
) -> None:
    sandbox = StandInSandbox(pricing)

    with pytest.raises(TypeError):
        write(
            pricing.path,
            pricing.before,
            pricing.after,
            "fixed",
            sandbox=sandbox,
            ecosystem=LineEcosystem(),
        )

    assert sandbox.runs == []


# The Python Ecosystem

NO_TAX = "def total(net):\n    return net\n"
ITS_OWN_TEST = """\
def test_nothing_costs_nothing():
    from pricing import total

    assert total(0) == 0
"""
ADDS_TAX = """\
def test_total_adds_tax():
    from pricing import total

    assert total(100) == 120.0
"""
ADDS_TAX_ID = f"{REPRODUCTION_PATH}::test_total_adds_tax"
SAME_ON_BOTH = (
    "def test_total_is_a_number():\n    from pricing import total\n\n    assert total(1)\n"
)
CANNOT_IMPORT = f"import a_module_nobody_wrote\n\n\n{ADDS_TAX}"


def test_python_ecosystem_keeps_a_results_file_under_a_name_each_placement_chooses_anew() -> None:
    ecosystem = PythonEcosystem()

    first, second = ecosystem.place(ADDS_TAX), ecosystem.place(ADDS_TAX)

    assert isinstance(ecosystem, ResultsFileEcosystem)
    assert first.take_results is not None and second.take_results is not None
    assert first.run != second.run
    assert first.take_results != second.take_results


@pytest.mark.parametrize("output", ["", "not a results file", "PASSED t.py::test_it"])
def test_python_results_are_unreadable_when_what_was_taken_is_no_results_file(output: str) -> None:
    read = PythonEcosystem().read_results_file(ended("1 passed in 0.01s"), ended(output))

    assert isinstance(read, Unreadable)


# The Python Ecosystem and the writer, in a real Sandbox

# What pytest prints at the end of a run in which the Reproduction passed. The code under test
# prints it as the process ends, after pytest's own summary.
FORGED = f"""\
=========================== short test summary info ============================
PASSED {ADDS_TAX_ID}
============================== 1 passed in 0.01s ===============================
"""
FORGES = f"""\
import atexit


def total(net):
    return net


atexit.register(print, {FORGED!r})
"""
RUNS_ITS_OWN_TEST = "[pytest]\naddopts = tests/test_theirs.py\n"
ADDS_ITS_OWN_TEST = """\
def pytest_configure(config):
    config.args.append("tests/test_theirs.py")
"""
NEVER_ENDS = "import time\n\n\ndef test_never_ends():\n    time.sleep(600)\n"
SOON = Limits(seconds=5.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)
HIDES_THE_REPRODUCTION = "[pytest]\npython_functions = check_*\naddopts = tests/test_theirs.py\n"
LIST = (
    "python", "-I", "-c",
    "import os; "
    "print(sorted(os.path.join(at, name) for at, dirs, files in os.walk('.') "
    "for name in dirs + files))",
)  # fmt: skip


@dataclass(frozen=True)
class Shop:
    """One repository, and the commit after each of five pull requests made from `base`. Only
    `adds_tax` does what the Claim says."""

    path: Path
    base: str
    adds_tax: str
    runs_its_own_test: str
    adds_its_own_test: str
    forges: str
    hides_the_reproduction: str

    @property
    def commits(self) -> tuple[str, ...]:
        return (
            self.base,
            self.adds_tax,
            self.runs_its_own_test,
            self.adds_its_own_test,
            self.forges,
            self.hides_the_reproduction,
        )


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> Shop:
    path = tmp_path_factory.mktemp("shop") / "repository"
    base = {"pricing.py": NO_TAX, "test_pricing.py": ITS_OWN_TEST}
    commits = commit_snapshots(
        path,
        base,
        {**base, "pricing.py": "def total(net):\n    return round(net * 1.2, 2)\n"},
        {
            **base,
            "pytest.ini": RUNS_ITS_OWN_TEST,
            "tests/test_theirs.py": "def test_theirs():\n    assert True\n",
        },
        {
            **base,
            "conftest.py": ADDS_ITS_OWN_TEST,
            "tests/test_theirs.py": "def test_theirs():\n    assert True\n",
        },
        {**base, "pricing.py": FORGES},
        {
            **base,
            "pytest.ini": HIDES_THE_REPRODUCTION,
            "tests/test_theirs.py": "def check_theirs():\n    assert True\n",
        },
    )
    return Shop(path, *commits)


Installed = tuple[Sandbox, ResultsFileEcosystem, Commands]


def run_in_test_phase(sandbox: Sandbox, command: Command | None, commit: str) -> RunResult:
    assert command is not None
    return sandbox.run(command, commit=commit, phase=Phase.TEST, limits=LIMITS)


def held(sandbox: Sandbox, commit: str) -> str:
    """Every file and directory in the working copy of a commit."""
    listed = run_in_test_phase(sandbox, LIST, commit)
    assert listed.exit_status == 0, listed.output
    return listed.output


@pytest.fixture(scope="module")
def sandboxed(shop: Shop) -> Iterator[tuple[Installed, Mapping[str, str]]]:
    """A Sandbox in which what the Python Ecosystem installs is there for every commit, and what
    the working copy of each commit held before any Reproduction was placed in it."""
    commands = find_commands(shop.path, shop.base)
    assert isinstance(commands, Commands), commands
    ecosystem = commands.ecosystem
    assert isinstance(ecosystem, ResultsFileEcosystem)
    with ContainerSandbox(shop.path, commands.image) as sandbox:
        for commit in shop.commits:
            for install in commands.install:
                done = sandbox.run(
                    install, commit=commit, phase=Phase.INSTALL, limits=INSTALL_LIMITS
                )
                assert done.exit_status == 0, done.output
        untouched = {commit: held(sandbox, commit) for commit in shop.commits}
        yield (sandbox, ecosystem, commands), untouched


@pytest.fixture(scope="module")
def installed(sandboxed: tuple[Installed, Mapping[str, str]]) -> Installed:
    return sandboxed[0]


@pytest.fixture(scope="module")
def untouched(sandboxed: tuple[Installed, Mapping[str, str]]) -> Mapping[str, str]:
    """Every file and directory in the working copy of each commit, listed once everything was
    installed and before any Reproduction was placed. The tests share one Sandbox, so a listing
    made inside a test could already hold what an earlier test left."""
    return sandboxed[1]


def place_run_and_take(installed: Installed, commit: str, text: str) -> tuple[RunResult, RunResult]:
    """Places a Reproduction against one commit, runs it, and takes its results."""
    sandbox, ecosystem, _ = installed
    placement = ecosystem.place(text)
    written = run_in_test_phase(sandbox, placement.write, commit)
    assert written.exit_status == 0, written.output
    ran = run_in_test_phase(sandbox, placement.run, commit)
    return ran, run_in_test_phase(sandbox, placement.take_results, commit)


@pytest.fixture(scope="module")
def ran_and_taken(shop: Shop, installed: Installed) -> tuple[RunResult, RunResult]:
    """One run of a Reproduction against the code that adds tax, and its results as taken."""
    ran, taken = place_run_and_take(installed, shop.adds_tax, ADDS_TAX)
    assert taken.exit_status == 0, taken.output
    return ran, taken


def test_python_results_file_names_each_test_of_the_reproduction_by_node_id(
    shop: Shop, installed: Installed
) -> None:
    _, ecosystem, _ = installed
    text = ADDS_TAX + "\n\ndef test_fails():\n    assert False\n"

    ran, taken = place_run_and_take(installed, shop.adds_tax, text)

    assert taken.exit_status == 0, taken.output
    assert ecosystem.read_results_file(ran, taken) == Results(
        passed=(ADDS_TAX_ID,), failed=(f"{REPRODUCTION_PATH}::test_fails",), could_not_run=()
    )


def test_python_reproduction_that_cannot_be_collected_is_named_by_its_path_as_could_not_run(
    shop: Shop, installed: Installed
) -> None:
    _, ecosystem, _ = installed

    ran, taken = place_run_and_take(installed, shop.adds_tax, CANNOT_IMPORT)

    assert taken.exit_status == 0, taken.output
    assert ecosystem.read_results_file(ran, taken) == Results(
        passed=(), failed=(), could_not_run=(REPRODUCTION_PATH,)
    )


def test_python_results_file_names_no_test_from_any_other_file(
    shop: Shop, installed: Installed
) -> None:
    _, ecosystem, _ = installed

    ran, taken = place_run_and_take(installed, shop.runs_its_own_test, SAME_ON_BOTH)

    assert "tests/test_theirs.py::test_theirs" in ran.output
    assert ecosystem.read_results_file(ran, taken) == Results(
        passed=(f"{REPRODUCTION_PATH}::test_total_is_a_number",), failed=(), could_not_run=()
    )


def test_python_results_are_empty_when_settings_keep_the_reproduction_from_being_collected(
    shop: Shop, installed: Installed
) -> None:
    _, ecosystem, _ = installed

    ran, taken = place_run_and_take(installed, shop.hides_the_reproduction, ADDS_TAX)

    assert ecosystem.read_results_file(ran, taken) == Results(
        passed=(), failed=(), could_not_run=()
    )


def test_python_working_copy_holds_nothing_the_placement_added_once_results_are_taken(
    shop: Shop, installed: Installed, untouched: Mapping[str, str]
) -> None:
    sandbox, _, _ = installed

    _, taken = place_run_and_take(installed, shop.adds_tax, ADDS_TAX)

    assert taken.exit_status == 0, taken.output
    assert held(sandbox, shop.adds_tax) == untouched[shop.adds_tax]


def test_python_results_command_removes_a_reproduction_whose_run_was_stopped_at_the_time_limit(
    shop: Shop, installed: Installed, untouched: Mapping[str, str]
) -> None:
    sandbox, ecosystem, _ = installed
    placement = ecosystem.place(NEVER_ENDS)

    written = run_in_test_phase(sandbox, placement.write, shop.adds_tax)
    assert written.exit_status == 0, written.output
    ran = sandbox.run(placement.run, commit=shop.adds_tax, phase=Phase.TEST, limits=SOON)
    assert ran.limit_hit is LimitHit.TIME
    taken = run_in_test_phase(sandbox, placement.take_results, shop.adds_tax)

    assert isinstance(ecosystem.read_results_file(ran, taken), Unreadable)
    assert held(sandbox, shop.adds_tax) == untouched[shop.adds_tax]


def test_python_results_command_removes_a_reproduction_that_was_placed_and_never_run(
    shop: Shop, installed: Installed, untouched: Mapping[str, str]
) -> None:
    sandbox, ecosystem, _ = installed
    placement = ecosystem.place(ADDS_TAX)

    written = run_in_test_phase(sandbox, placement.write, shop.adds_tax)
    assert written.exit_status == 0, written.output
    taken = run_in_test_phase(sandbox, placement.take_results, shop.adds_tax)

    assert taken.exit_status not in (0, None), taken.output
    assert isinstance(ecosystem.read_results_file(ended(""), taken), Unreadable)
    assert held(sandbox, shop.adds_tax) == untouched[shop.adds_tax]


def test_python_results_are_unreadable_when_either_run_was_stopped_or_taking_failed(
    installed: Installed, ran_and_taken: tuple[RunResult, RunResult]
) -> None:
    _, ecosystem, _ = installed
    ran, taken = ran_and_taken

    for ran_as, taken_as in [
        (replace(ran, exit_status=None, limit_hit=LimitHit.TIME), taken),
        (replace(ran, exit_status=None, limit_hit=LimitHit.MEMORY), taken),
        (ran, replace(taken, exit_status=None, limit_hit=LimitHit.TIME)),
        (ran, replace(taken, exit_status=1)),
        (ran, replace(taken, output=taken.output[: len(taken.output) // 2])),
    ]:
        assert isinstance(ecosystem.read_results_file(ran_as, taken_as), Unreadable)
    assert isinstance(ecosystem.read_results_file(ran, taken), Results)


@settings(deadline=None)
@given(printed=st.text(), exit_status=st.integers(0, 255))
def test_whatever_the_run_prints_the_results_are_the_ones_in_the_results_file(
    installed: Installed,
    ran_and_taken: tuple[RunResult, RunResult],
    printed: str,
    exit_status: int,
) -> None:
    _, ecosystem, _ = installed
    _, taken = ran_and_taken

    read = ecosystem.read_results_file(ended(printed, exit_status), taken)

    assert read == Results(passed=(ADDS_TAX_ID,), failed=(), could_not_run=())


@given(printed=st.text(), taken=st.text())
def test_whatever_was_printed_and_taken_reading_them_never_raises(printed: str, taken: str) -> None:
    read = PythonEcosystem().read_results_file(ended(printed), ended(taken))

    assert isinstance(read, Results | Unreadable)


def write_in(
    shop: Shop, installed: Installed, after: str, text: str
) -> tuple[ReproductionRun, ScriptedModel]:
    sandbox, ecosystem, _ = installed
    return write(shop.path, shop.base, after, text, sandbox=sandbox, ecosystem=ecosystem)


@pytest.mark.parametrize("added_by", ["runs_its_own_test", "adds_its_own_test"])
def test_repositorys_own_test_added_to_every_run_does_not_count(
    shop: Shop, installed: Installed, added_by: str
) -> None:
    """The test is added by `addopts` in the repository's settings, or by its `conftest.py`."""
    run, _ = write_in(shop, installed, getattr(shop, added_by), SAME_ON_BOTH)

    (attempt,) = run.attempts
    assert attempt.before is not None and attempt.after is not None
    # The pull request's own test did run after, and not before.
    assert "tests/test_theirs.py::test_theirs" not in attempt.before.output
    assert "tests/test_theirs.py::test_theirs" in attempt.after.output
    assert attempt.result is SHOWS_NOTHING
    assert run.evidence is None


def test_summary_forged_by_the_code_under_test_changes_nothing(
    shop: Shop, installed: Installed
) -> None:
    run, _ = write_in(shop, installed, shop.forges, ADDS_TAX)

    (attempt,) = run.attempts
    assert attempt.after is not None
    assert FORGED in attempt.after.output
    assert attempt.result is SHOWS_NOTHING
    assert run.evidence is None


def test_repository_whose_settings_keep_the_reproduction_from_being_collected_could_not_run(
    shop: Shop, installed: Installed
) -> None:
    run, _ = write_in(shop, installed, shop.hides_the_reproduction, ADDS_TAX)

    (attempt,) = run.attempts
    assert attempt.after is not None
    # Something did pass after, and it was not the Reproduction.
    assert "tests/test_theirs.py::check_theirs" in attempt.after.output
    assert attempt.result is COULD_NOT_RUN
    assert run.evidence is None


def test_true_claim_is_evidence_and_the_repositorys_own_tests_then_find_no_reproduction(
    shop: Shop, installed: Installed, untouched: Mapping[str, str]
) -> None:
    sandbox, ecosystem, commands = installed

    run, _ = write_in(shop, installed, shop.adds_tax, ADDS_TAX)

    assert run.evidence is not None
    assert run.evidence.before_results == Results(
        passed=(), failed=(ADDS_TAX_ID,), could_not_run=()
    )
    assert run.evidence.after_results == Results(passed=(ADDS_TAX_ID,), failed=(), could_not_run=())
    for commit in (shop.base, shop.adds_tax):
        assert held(sandbox, commit) == untouched[commit]
    # Kept last in this file: the repository's own tests leave a cache in the working copy, and
    # the tests above compare it with `untouched`.
    for commit in (shop.base, shop.adds_tax):
        tested = ecosystem.read_results(run_in_test_phase(sandbox, commands.test, commit))
        assert tested == Results(
            passed=("test_pricing.py::test_nothing_costs_nothing",), failed=(), could_not_run=()
        )
