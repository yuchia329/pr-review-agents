"""The Reproduction writer: what it records for whatever the model writes and the runs show.

The tests down to `installed` use a stand-in Sandbox and a stand-in Ecosystem. The ones from
there on install pytest from the package index and run Reproductions in a real Sandbox.
"""

import hashlib
import json
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from conftest import FixtureRepository, asks, commit_snapshots, finishes
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification.agent import (
    NO_SUCH_TOOL,
    PROGRESS,
    PROGRESS_INTERVAL,
    REPEAT_LENGTH,
    TOLD,
    LoopKind,
    StepOutcome,
    Stopped,
    Trigger,
)
from pr_verification.decision import NO, YES, DecisionFailed, ScriptedDecider
from pr_verification.ecosystem import (
    LONGEST_REPRODUCTION,
    Commands,
    Placement,
    ReproductionEcosystem,
    Results,
    Unreadable,
    Unsupported,
    find_commands,
)
from pr_verification.model import Model, ModelFailed, ModelRequest, ScriptedModel
from pr_verification.python_ecosystem import REPRODUCTION_PATH
from pr_verification.reading import reading_tools
from pr_verification.report import BudgetSpend, Claim, EvidenceSource, Report, Verdict
from pr_verification.reproduction import (
    ATTEMPT_CAP,
    ATTEMPT_CAP_REACHED,
    COULD_NOT_FINISH,
    ENDINGS,
    WRITER_STEP_CAP,
    AttemptResult,
    ReproductionRun,
    compare_runs,
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
NO_REPRODUCTION = AttemptResult.NO_REPRODUCTION
COULD_NOT_RUN = AttemptResult.COULD_NOT_RUN
SHOWS_NOTHING = AttemptResult.SHOWS_NOTHING
SHOWS_A_DIFFERENCE = AttemptResult.SHOWS_A_DIFFERENCE

PRICING = "def total(net):\n    return round(net * 1.2, 2)\n"
ITS_OWN_TEST = """\
def test_nothing_costs_nothing():
    from pricing import total

    assert total(0) == 0
"""


@pytest.fixture(scope="module")
def pricing(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """Plain Python modules with a test of their own. The pull request makes `total` add tax."""
    path = tmp_path_factory.mktemp("pricing") / "repository"
    before, after = commit_snapshots(
        path,
        {"pricing.py": "def total(net):\n    return net\n", "test_pricing.py": ITS_OWN_TEST},
        {"pricing.py": PRICING, "test_pricing.py": ITS_OWN_TEST},
    )
    return FixtureRepository(path=path, before=before, after=after)


# What the runs on the two commits show


def results(passed: str = "", failed: str = "", errors: str = "") -> Results:
    """Results in which each letter names one test."""
    return Results(passed=tuple(passed), failed=tuple(failed), could_not_run=tuple(errors))


UNREADABLE = Unreadable("The run was stopped at the time limit.")


@pytest.mark.parametrize(
    ("before", "after", "shown"),
    [
        (results(failed="t"), results(passed="t"), SHOWS_A_DIFFERENCE),
        (results(passed="t"), results(failed="t"), SHOWS_A_DIFFERENCE),
        (results(passed="a"), results(passed="ab"), SHOWS_A_DIFFERENCE),
        (results(passed="a"), results(passed="b"), SHOWS_A_DIFFERENCE),
        (results(passed="t"), results(passed="t"), SHOWS_NOTHING),
        (results(failed="t"), results(failed="t"), SHOWS_NOTHING),
        (results(passed="ab", failed="cd"), results(passed="ba", failed="dc"), SHOWS_NOTHING),
        (UNREADABLE, results(passed="t"), COULD_NOT_RUN),
        (results(failed="t"), UNREADABLE, COULD_NOT_RUN),
        (results(errors="t"), results(passed="t"), COULD_NOT_RUN),
        (results(failed="t"), results(passed="t", errors="u"), COULD_NOT_RUN),
        (results(), results(passed="t"), COULD_NOT_RUN),
        (results(failed="t"), results(), COULD_NOT_RUN),
        (results(), results(), COULD_NOT_RUN),
    ],
)
def test_runs_show_a_difference_only_when_both_ran_and_their_tests_ended_differently(
    before: Results | Unreadable, after: Results | Unreadable, shown: AttemptResult
) -> None:
    assert compare_runs(before, after) is shown


def read_as(ended: dict[str, str]) -> Results:
    """Results in which each test ended as the dictionary says it did."""
    tests = {"passed": "", "failed": "", "errors": ""}
    for test, how in ended.items():
        tests[how] += test
    return results(**tests)


ENDED = st.dictionaries(st.sampled_from("abcd"), st.sampled_from(["passed", "failed", "errors"]))
READ = st.just(UNREADABLE) | ENDED.map(read_as)


def ran(read: Results | Unreadable) -> bool:
    return isinstance(read, Results) and not read.could_not_run and bool(read.passed + read.failed)


@given(before=READ, after=READ)
def test_whatever_the_results_a_run_that_did_not_reach_an_answer_is_never_a_difference(
    before: Results | Unreadable, after: Results | Unreadable
) -> None:
    shown = compare_runs(before, after)

    assert shown is not NO_REPRODUCTION
    assert (shown is COULD_NOT_RUN) == (not (ran(before) and ran(after)))
    assert compare_runs(before, before) is not SHOWS_A_DIFFERENCE


# The writer, with a stand-in Sandbox and a stand-in Ecosystem


def ended(output: str, exit_status: int = 0) -> RunResult:
    return RunResult(output=output, exit_status=exit_status, limit_hit=None)


FORM = "A Reproduction is a list of the lines its tests print."
WRITTEN, NOT_WRITTEN = ended(""), ended("read-only file system", 1)
SAME = (ended("ok test_tax"), ended("ok test_tax"))
# What each Reproduction a scripted model writes does on the code before and the code after.
BEHAVIOUR: Mapping[str, tuple[RunResult, RunResult]] = {
    "fixed": (ended("failed test_tax", 1), ended("ok test_tax")),
    "broken": (ended("ok test_tax"), ended("failed test_tax", 1)),
    "same": SAME,
    "cannot import": (ended("error reproduction.tap SANDBOX-OUTPUT", 2), ended("ok test_tax")),
    "cannot set up": (ended("failed test_tax", 1), ended("error test_tax", 1)),
    "no tests": (ended("collected nothing", 5), ended("collected nothing", 5)),
    "timed out": (RunResult("ok test_", None, LimitHit.TIME), ended("ok test_tax")),
}


class LineEcosystem:
    """A stand-in with nothing of Python in it: a run prints one line for each test."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        return Unsupported("A stand-in installs nothing.")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        if run.limit_hit is not None:
            return Unreadable("The run was stopped.")
        lines = [line.split() for line in run.output.splitlines()]
        return Results(
            passed=tuple(name for word, name, *_ in lines if word == "ok"),
            failed=tuple(name for word, name, *_ in lines if word == "failed"),
            could_not_run=tuple(name for word, name, *_ in lines if word == "error"),
        )

    def reproduction_form(self) -> str:
        return FORM

    def place(self, reproduction: str) -> Placement:
        return Placement("reproduction.tap", ("write", reproduction), ("run-reproduction",))


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
                return NOT_WRITTEN
            self._written[commit] = command[1]
            return WRITTEN
        before, after = BEHAVIOUR.get(self._written[commit], SAME)
        return before if commit == self._before else after


def write_with(
    model: Model,
    repository: FixtureRepository,
    sandbox: Sandbox | None = None,
    ecosystem: ReproductionEcosystem | None = None,
    **caps: int,
) -> ReproductionRun:
    return write_reproduction(
        CLAIM,
        model=model,
        sandbox=sandbox or StandInSandbox(repository),
        ecosystem=ecosystem or LineEcosystem(),
        repository=repository.path,
        before=repository.before,
        after=repository.after,
        limits=LIMITS,
        **caps,
    )


def write(
    repository: FixtureRepository,
    *responses: str,
    sandbox: Sandbox | None = None,
    ecosystem: ReproductionEcosystem | None = None,
    **caps: int,
) -> tuple[ReproductionRun, ScriptedModel]:
    """What the writer records when the model gives these responses, and what it was sent."""
    model = ScriptedModel(responses)
    return write_with(model, repository, sandbox, ecosystem, **caps), model


def writes(text: object) -> str:
    return finishes(reproduction=text)


def task(request: ModelRequest) -> dict[str, object]:
    sent: dict[str, object] = json.loads(request.data)["task"]
    return sent


@pytest.mark.parametrize("text", ["fixed", "broken"])
def test_reproduction_that_behaves_differently_is_evidence_and_ends_the_work(
    pricing: FixtureRepository, text: str
) -> None:
    run, model = write(pricing, writes(text), writes("same"))

    before, after = BEHAVIOUR[text]
    assert run.evidence is not None
    assert run.evidence.source is EvidenceSource.RAN_CODE
    assert run.evidence.command == ("run-reproduction",)
    assert (run.evidence.before, run.evidence.after) == (before, after)
    assert run.evidence.observation == (
        f"before: exit status {before.exit_status}; after: exit status {after.exit_status}"
    )
    assert (run.evidence.path, run.evidence.reproduction) == ("reproduction.tap", text)
    assert run.inconclusive is None
    (attempt,) = run.attempts
    assert attempt.result is SHOWS_A_DIFFERENCE
    assert (attempt.reproduction, attempt.before, attempt.after) == (text, before, after)
    assert len(model.requests) == 1


TAX_PASSED = Results(passed=("test_tax",), failed=(), could_not_run=())
TAX_FAILED = Results(passed=(), failed=("test_tax",), could_not_run=())


@pytest.mark.parametrize(
    ("text", "before", "after"),
    [("fixed", TAX_FAILED, TAX_PASSED), ("broken", TAX_PASSED, TAX_FAILED)],
)
def test_evidence_holds_what_the_ecosystem_read_of_the_last_attempts_run_on_each_commit(
    pricing: FixtureRepository, text: str, before: Results, after: Results
) -> None:
    run, _ = write(pricing, writes("cannot set up"), writes(text))

    assert run.evidence is not None
    assert (run.evidence.before_results, run.evidence.after_results) == (before, after)


@pytest.mark.parametrize("text", ["fixed", "timed out"])
def test_reproduction_is_placed_then_run_on_before_then_after_through_the_sandbox_alone(
    pricing: FixtureRepository, text: str
) -> None:
    sandbox = StandInSandbox(pricing)

    write(pricing, writes(text), sandbox=sandbox, attempt_cap=1)

    assert sandbox.runs == [
        (("write", text), pricing.before, Phase.TEST, LIMITS),
        (("write", text), pricing.after, Phase.TEST, LIMITS),
        (("run-reproduction",), pricing.before, Phase.TEST, LIMITS),
        (("run-reproduction",), pricing.after, Phase.TEST, LIMITS),
    ]


@pytest.mark.parametrize(
    ("first", "result"),
    [
        ("cannot import", COULD_NOT_RUN),
        ("cannot set up", COULD_NOT_RUN),
        ("no tests", COULD_NOT_RUN),
        ("timed out", COULD_NOT_RUN),
        ("same", SHOWS_NOTHING),
    ],
)
def test_writer_revises_a_reproduction_that_could_not_run_or_shows_nothing_and_is_shown_why(
    pricing: FixtureRepository, first: str, result: AttemptResult
) -> None:
    run, model = write(pricing, writes(first), writes("fixed"))

    before, after = BEHAVIOUR[first]
    earlier, last = run.attempts
    assert (earlier.result, last.result) == (result, SHOWS_A_DIFFERENCE)
    assert (earlier.reproduction, earlier.before, earlier.after) == (first, before, after)
    assert run.evidence is not None and run.evidence.reproduction == "fixed"
    shown = {"reproduction": first, "result": result.value}
    assert [task(request) for request in model.requests] == [
        {"claim": CLAIM, "attempts": []},
        {"claim": CLAIM, "attempts": [{**shown, "before": before.output, "after": after.output}]},
    ]


@pytest.mark.parametrize(
    "answer",
    [
        finishes(),
        finishes(test="fixed"),
        writes(7),
        writes(["fixed"]),
        writes(""),
        writes(" \n\t"),
        writes("x" * (LONGEST_REPRODUCTION + 1)),
    ],
    ids=["nothing", "another member", "number", "list", "empty", "blank", "too long"],
)
def test_answer_that_holds_no_reproduction_runs_nothing_and_is_revised(
    pricing: FixtureRepository, answer: str
) -> None:
    sandbox = StandInSandbox(pricing)

    run, model = write(pricing, answer, writes("fixed"), sandbox=sandbox)

    first, second = run.attempts
    assert (first.result, second.result) == (NO_REPRODUCTION, SHOWS_A_DIFFERENCE)
    assert (first.reproduction, first.before, first.after) == (None, None, None)
    assert {command for command, *_ in sandbox.runs} == {("write", "fixed"), ("run-reproduction",)}
    assert len(sandbox.runs) == 4
    assert task(model.requests[1])["attempts"] == [
        {"reproduction": None, "result": "no_reproduction", "before": None, "after": None}
    ]


def test_answer_that_holds_more_than_its_reproduction_still_gives_one(
    pricing: FixtureRepository,
) -> None:
    run, _ = write(pricing, finishes(reproduction="fixed", note="It fails before the fix."))

    assert run.evidence is not None and run.evidence.reproduction == "fixed"


def test_reproduction_of_the_longest_length_is_placed_exactly_as_the_role_gave_it(
    pricing: FixtureRepository,
) -> None:
    text = " " + "x" * (LONGEST_REPRODUCTION - 2) + "\n"
    sandbox = StandInSandbox(pricing)

    run, _ = write(pricing, writes(text), sandbox=sandbox, attempt_cap=1)

    assert sandbox.runs[0][0] == ("write", text)
    assert run.attempts[0].reproduction == text


@pytest.mark.parametrize("unwritable", ["before", "after"])
def test_reproduction_that_cannot_be_placed_on_both_commits_is_not_run(
    pricing: FixtureRepository, unwritable: str
) -> None:
    commits = {"before": pricing.before, "after": pricing.after}
    sandbox = StandInSandbox(pricing, unwritable=commits[unwritable])

    run, model = write(pricing, writes("fixed"), writes("same"), sandbox=sandbox, attempt_cap=2)

    assert [(command, commit) for command, commit, *_ in sandbox.runs] == [
        (("write", "fixed"), pricing.before),
        (("write", "fixed"), pricing.after),
        (("write", "same"), pricing.before),
        (("write", "same"), pricing.after),
    ]
    placing = {"before": WRITTEN, "after": WRITTEN, unwritable: NOT_WRITTEN}
    for attempt in run.attempts:
        assert attempt.result is COULD_NOT_RUN
        assert {"before": attempt.before, "after": attempt.after} == placing
    assert task(model.requests[1])["attempts"] == [
        {
            "reproduction": "fixed",
            "result": "could_not_run",
            "before": placing["before"].output,
            "after": placing["after"].output,
        }
    ]


def test_writers_role_reads_the_code_after_the_pull_request_and_has_no_tool_that_runs_anything(
    pricing: FixtureRepository,
) -> None:
    sandbox = StandInSandbox(pricing)
    reads, runs = asks("read_file", path="pricing.py"), asks("run_command", command="pytest")

    run, _ = write(pricing, reads, runs, writes("same"), sandbox=sandbox, attempt_cap=1)

    role = run.attempts[0].run
    (read_file,) = (t for t in reading_tools(pricing.path, pricing.after) if t.name == "read_file")
    assert role.role == "reproduction-writer"
    assert [(step.outcome, step.result) for step in role.steps[:2]] == [
        (StepOutcome.RAN_TOOL, read_file.run({"path": "pricing.py"})),
        (StepOutcome.REFUSED, NO_SUCH_TOOL),
    ]
    assert "round(net * 1.2, 2)" in role.steps[0].result
    assert {command[0] for command, *_ in sandbox.runs} == {"write", "run-reproduction"}
    assert len(sandbox.runs) == 4


def test_role_that_reaches_its_step_cap_gave_no_reproduction_and_the_next_attempt_starts_afresh(
    pricing: FixtureRepository,
) -> None:
    reads = asks("read_file", path="pricing.py")

    capped, model = write(pricing, reads, reads, writes("fixed"), step_cap=2)
    by_default, _ = write(pricing, *[reads] * WRITER_STEP_CAP, writes("fixed"))

    assert [attempt.result for attempt in capped.attempts] == [NO_REPRODUCTION, SHOWS_A_DIFFERENCE]
    assert capped.attempts[0].run.stopped is Stopped.STEP_CAP
    assert json.loads(model.requests[2].data)["steps"] == []
    assert len(by_default.attempts[0].run.steps) == WRITER_STEP_CAP
    assert by_default.attempts[0].result is NO_REPRODUCTION


def test_instructions_hold_the_ecosystems_form_and_nothing_the_writer_reads(
    pricing: FixtureRepository,
) -> None:
    reads = asks("read_file", path="pricing.py")

    _, model = write(pricing, reads, writes("cannot import"), writes("fixed"))

    assert "SANDBOX-OUTPUT" in model.requests[2].data
    for request in model.requests:
        assert FORM in request.instructions
        for untrusted in (CLAIM, "SANDBOX-OUTPUT", "round(net * 1.2, 2)"):
            assert untrusted not in request.instructions


@pytest.mark.parametrize(
    ("answer", "last"),
    [
        (writes("same"), SHOWS_NOTHING),
        (writes("cannot import"), COULD_NOT_RUN),
        (finishes(), NO_REPRODUCTION),
    ],
    ids=["shows nothing", "could not run", "no reproduction"],
)
def test_claim_is_inconclusive_with_the_reason_when_the_cap_on_attempts_is_reached(
    pricing: FixtureRepository, answer: str, last: AttemptResult
) -> None:
    run, model = write(pricing, writes("no tests"), *[answer] * 9, attempt_cap=3)

    assert [attempt.result for attempt in run.attempts] == [COULD_NOT_RUN, last, last]
    assert run.evidence is None
    assert run.inconclusive == ATTEMPT_CAP_REACHED.format(attempts=3, ending=ENDINGS[last])
    assert len(model.requests) == 3


def test_writer_makes_the_default_number_of_attempts_when_no_cap_is_given(
    pricing: FixtureRepository,
) -> None:
    run, model = write(pricing, *[writes("same")] * (ATTEMPT_CAP + 2))

    assert len(run.attempts) == len(model.requests) == ATTEMPT_CAP
    assert run.inconclusive is not None


@pytest.mark.parametrize("caps", [{"attempt_cap": 0}, {"attempt_cap": -1}, {"step_cap": 0}])
def test_cap_under_one_is_rejected_before_anything_is_asked_or_run(
    pricing: FixtureRepository, caps: dict[str, int]
) -> None:
    sandbox, model = StandInSandbox(pricing), ScriptedModel([writes("fixed")])

    with pytest.raises(ValueError):
        write_with(model, pricing, sandbox, None, **caps)

    assert (model.requests, sandbox.runs) == ([], [])


class ThenUnreachable:
    """A stand-in for a model that gives its responses and then cannot be reached."""

    def __init__(self, responses: list[str]) -> None:
        self.requests = 0
        self._responses = responses

    def complete(self, request: ModelRequest) -> str:
        self.requests += 1
        if self.requests > len(self._responses):
            raise ModelFailed("the model could not be reached")
        return self._responses[self.requests - 1]


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


READS = asks("read_file", path="pricing.py")


@pytest.mark.parametrize(
    ("responses", "kept"),
    [
        ([], []),
        ([READS], []),
        ([writes("same")], [SHOWS_NOTHING]),
        ([writes("same"), writes("no tests"), READS], [SHOWS_NOTHING, COULD_NOT_RUN]),
    ],
    ids=["at once", "during the first attempt", "after one attempt", "during the third"],
)
def test_model_that_fails_ends_the_work_and_the_attempts_before_it_are_kept(
    pricing: FixtureRepository, responses: list[str], kept: list[AttemptResult]
) -> None:
    sandbox, model = StandInSandbox(pricing), ThenUnreachable(responses)

    run = write_with(model, pricing, sandbox, attempt_cap=5)

    assert [attempt.result for attempt in run.attempts] == kept
    assert run.evidence is None
    assert run.inconclusive == COULD_NOT_FINISH.format(failure="the model could not be reached")
    assert model.requests == len(responses) + 1
    assert len(sandbox.runs) == 4 * len(kept)


@pytest.mark.parametrize(
    ("runs", "kept"),
    [(0, []), (1, []), (2, []), (3, []), (4, [SHOWS_NOTHING]), (6, [SHOWS_NOTHING])],
    ids=["placing", "placing on after", "running", "running on after", "second", "running second"],
)
def test_sandbox_that_fails_ends_the_work_and_the_attempts_before_it_are_kept(
    pricing: FixtureRepository, runs: int, kept: list[AttemptResult]
) -> None:
    sandbox = ThenNoRuntime(StandInSandbox(pricing), runs)

    run, model = write(pricing, *[writes("same")] * 5, sandbox=sandbox, attempt_cap=5)

    assert [attempt.result for attempt in run.attempts] == kept
    assert run.evidence is None
    assert run.inconclusive == COULD_NOT_FINISH.format(failure="no container runtime")
    assert sandbox.asked == runs + 1
    assert len(model.requests) == len(kept) + 1


ANSWERS = st.one_of(
    st.sampled_from([*BEHAVIOUR, "", 7]).map(writes),
    st.text().map(writes),
    st.text(),
    st.just(asks("read_file", path="pricing.py")),
    st.just(asks("run_command", command="pytest")),
)


@settings(deadline=None)
@given(data=st.data(), attempt_cap=st.integers(1, 4), step_cap=st.integers(1, 3))
def test_whatever_the_model_writes_no_more_attempts_are_made_than_the_cap_and_each_is_kept(
    pricing: FixtureRepository, data: st.DataObject, attempt_cap: int, step_cap: int
) -> None:
    most = attempt_cap * step_cap
    responses = data.draw(st.lists(ANSWERS, min_size=most, max_size=most))

    run, model = write(pricing, *responses, attempt_cap=attempt_cap, step_cap=step_cap)

    shown = [attempt.result for attempt in run.attempts]
    assert 1 <= len(run.attempts) <= attempt_cap
    assert len(model.requests) == sum(len(attempt.run.steps) for attempt in run.attempts)
    assert SHOWS_A_DIFFERENCE not in shown[:-1]
    assert (run.evidence is None) != (run.inconclusive is None)
    assert (run.evidence is not None) == (shown[-1] is SHOWS_A_DIFFERENCE)
    assert run.evidence is not None or len(run.attempts) == attempt_cap


# The writer, watched for loops


def write_watched(
    guard: ScriptedDecider, repository: FixtureRepository, *responses: str, **caps: int
) -> tuple[ReproductionRun, ScriptedModel]:
    """As `write`, with a progress guard, which the writer takes by position."""
    model = ScriptedModel(responses)
    run = write_reproduction(
        CLAIM,
        guard,
        model=model,
        sandbox=StandInSandbox(repository),
        ecosystem=LineEcosystem(),
        repository=repository.path,
        before=repository.before,
        after=repository.after,
        limits=LIMITS,
        **caps,
    )
    return run, model


def looks(count: int, differing: bool = True) -> list[str]:
    """Steps that read a file, each a different one unless they are all to be the same step."""
    return [asks("read_file", path=f"{number if differing else 0}.py") for number in range(count)]


def test_each_attempt_is_watched_for_loops_on_its_own_and_starts_with_nothing_found(
    pricing: FixtureRepository,
) -> None:
    again = [READS] * (REPEAT_LENGTH - 1)

    run, model = write(
        pricing,
        *[*again, READS, writes("same"), *again, writes("fixed")],
        step_cap=REPEAT_LENGTH + 1,
    )

    first, second = run.attempts
    assert first.run.triggers == (Trigger(LoopKind.EXACT_REPEAT, step=REPEAT_LENGTH),)
    assert second.run.triggers == ()
    assert (first.result, second.result) == (SHOWS_NOTHING, SHOWS_A_DIFFERENCE)
    shown = json.loads(model.requests[REPEAT_LENGTH].data)["steps"]
    assert shown[-1]["told"] == TOLD[LoopKind.EXACT_REPEAT]


def test_progress_guard_given_to_the_writer_is_asked_about_the_run_of_each_attempt(
    pricing: FixtureRepository,
) -> None:
    guard = ScriptedDecider([NO, YES])
    steps = looks(PROGRESS_INTERVAL)

    run, model = write_watched(
        guard, pricing, *steps, writes("same"), *steps, writes("fixed"), step_cap=len(steps) + 1
    )

    first, second = run.attempts
    assert first.run.triggers == (Trigger(LoopKind.NO_PROGRESS, step=PROGRESS_INTERVAL),)
    assert second.run.triggers == ()
    assert [(json.loads(state)["task"], question) for state, question in guard.asked] == [
        (task(model.requests[0]), PROGRESS),
        (task(model.requests[len(steps) + 1]), PROGRESS),
    ]
    assert run.evidence is not None


@pytest.mark.parametrize("differing", [False, True], ids=["one step", "steps that all differ"])
def test_whatever_is_detected_the_writer_makes_no_more_attempts_or_steps_than_its_caps(
    pricing: FixtureRepository, differing: bool
) -> None:
    step_cap = 2 * PROGRESS_INTERVAL + REPEAT_LENGTH
    steps = looks(2 * step_cap + 5, differing)
    guard = ScriptedDecider([NO] * len(steps))

    run, model = write_watched(guard, pricing, *steps, attempt_cap=2, step_cap=step_cap)

    assert [len(attempt.run.steps) for attempt in run.attempts] == [step_cap, step_cap]
    assert all(len(attempt.run.triggers) > 1 for attempt in run.attempts)
    assert len(model.requests) == 2 * step_cap
    assert run.inconclusive == ATTEMPT_CAP_REACHED.format(
        attempts=2, ending=ENDINGS[NO_REPRODUCTION]
    )


def test_progress_guard_that_fails_ends_the_work_as_a_failing_model_does(
    pricing: FixtureRepository,
) -> None:
    guard = ScriptedDecider([DecisionFailed("the decision gave no usable answer")])
    steps = looks(PROGRESS_INTERVAL)

    run, model = write_watched(
        guard, pricing, writes("same"), *steps, writes("fixed"), step_cap=len(steps) + 1
    )

    assert [attempt.result for attempt in run.attempts] == [SHOWS_NOTHING]
    assert run.evidence is None
    assert run.inconclusive == COULD_NOT_FINISH.format(failure="the decision gave no usable answer")
    assert len(model.requests) == 1 + len(steps)


# The writer and the Python Ecosystem, in a real Sandbox

Installed = tuple[Sandbox, ReproductionEcosystem]


@pytest.fixture(scope="module")
def installed(pricing: FixtureRepository) -> Iterator[Installed]:
    """A Sandbox in which what the Python Ecosystem installs is there for both commits."""
    commands = find_commands(pricing.path, pricing.after)
    assert isinstance(commands, Commands), commands
    ecosystem = commands.ecosystem
    assert isinstance(ecosystem, ReproductionEcosystem)
    with ContainerSandbox(pricing.path, commands.image) as sandbox:
        for commit in (pricing.before, pricing.after):
            for install in commands.install:
                done = sandbox.run(
                    install, commit=commit, phase=Phase.INSTALL, limits=INSTALL_LIMITS
                )
                assert done.exit_status == 0, done.output
        yield sandbox, ecosystem


ADDS_TAX = """\
def test_total_adds_tax():
    from pricing import total

    assert total(100) == 120.0
"""
CANNOT_IMPORT = f"import a_module_nobody_wrote\n\n\n{ADDS_TAX}"
CANNOT_SET_UP = """\
import pytest


@pytest.fixture
def rates():
    raise RuntimeError("no rates")


def test_total_adds_tax(rates):
    assert True
"""
SAME_ON_BOTH = (
    "def test_total_is_a_number():\n    from pricing import total\n\n    assert total(1)\n"
)
SHA256_OF = (
    "import hashlib, pathlib, sys; "
    "print(hashlib.sha256(pathlib.Path(sys.argv[1]).read_bytes()).hexdigest())"
)


@pytest.mark.parametrize(
    "text",
    [
        ADDS_TAX,
        "'single' \"double\" $HOME `id` $(id) \\n %s ; && | > /dev/null\n",
        "caf\N{LATIN SMALL LETTER E WITH ACUTE} \N{GRINNING FACE}\r\nwith a \0 and no line feed",
        "\N{GRINNING FACE}" * LONGEST_REPRODUCTION,
    ],
    ids=["a test", "shell characters", "not ASCII", "longest"],
)
def test_python_reproduction_is_written_to_its_file_at_the_root_holding_exactly_its_text(
    pricing: FixtureRepository, installed: Installed, text: str
) -> None:
    sandbox, ecosystem = installed

    placement = ecosystem.place(text)
    for write_it in (ecosystem.place("something else\n").write, placement.write):
        written = sandbox.run(write_it, commit=pricing.after, phase=Phase.TEST, limits=LIMITS)
        assert written.exit_status == 0, written.output
    read = ("python", "-c", SHA256_OF, placement.path)
    held = sandbox.run(read, commit=pricing.after, phase=Phase.TEST, limits=LIMITS)

    assert placement.path == REPRODUCTION_PATH
    assert held.output.strip() == hashlib.sha256(text.encode()).hexdigest()


def test_python_reproduction_is_run_alone_and_its_tests_are_named_by_node_id(
    pricing: FixtureRepository, installed: Installed
) -> None:
    sandbox, ecosystem = installed
    placement = ecosystem.place(ADDS_TAX + "\n\ndef test_fails():\n    assert False\n")

    sandbox.run(placement.write, commit=pricing.after, phase=Phase.TEST, limits=LIMITS)
    ran = sandbox.run(placement.run, commit=pricing.after, phase=Phase.TEST, limits=LIMITS)

    assert ecosystem.read_results(ran) == Results(
        passed=(f"{REPRODUCTION_PATH}::test_total_adds_tax",),
        failed=(f"{REPRODUCTION_PATH}::test_fails",),
        could_not_run=(),
    )


def test_python_ecosystem_says_what_a_reproduction_is_the_same_every_time(
    installed: Installed,
) -> None:
    _, ecosystem = installed

    assert ecosystem.reproduction_form().strip() != ""
    assert ecosystem.reproduction_form() == ecosystem.reproduction_form()


def test_reproduction_and_its_output_on_both_commits_appear_in_the_report(
    pricing: FixtureRepository, installed: Installed
) -> None:
    sandbox, ecosystem = installed
    reads = asks("read_file", path="pricing.py")

    run, _ = write(pricing, reads, writes(ADDS_TAX), sandbox=sandbox, ecosystem=ecosystem)

    evidence = run.evidence
    assert evidence is not None
    assert (evidence.before.exit_status, evidence.after.exit_status) == (1, 0)
    assert evidence.observation == "before: exit status 1; after: exit status 0"
    assert "test_total_adds_tax" in evidence.before.output
    report = Report(
        before=pricing.before,
        after=pricing.after,
        claims=(Claim(CLAIM, Verdict.INCONCLUSIVE, "Not judged yet.", (evidence,)),),
        findings=(),
        budget_spend=BudgetSpend(seconds=1.0, dollars=0.0, steps=2),
    )
    (printed,) = json.loads(report.to_json())["claims"][0]["evidence"]
    assert printed["source"] == "ran_code"
    assert printed["command"] == list(evidence.command)
    assert (printed["before"]["exit_status"], printed["after"]["exit_status"]) == (1, 0)
    assert printed["before"]["limit_hit"] is None
    assert (printed["path"], printed["reproduction"]) == (REPRODUCTION_PATH, ADDS_TAX)
    assert printed["before"]["output"] == evidence.before.output
    assert printed["after"]["output"] == evidence.after.output


@pytest.mark.parametrize(
    ("first", "result", "shown"),
    [
        (CANNOT_IMPORT, COULD_NOT_RUN, "a_module_nobody_wrote"),
        (CANNOT_SET_UP, COULD_NOT_RUN, "no rates"),
        (SAME_ON_BOTH, SHOWS_NOTHING, "passed"),
    ],
    ids=["import error", "set-up error", "shows nothing"],
)
def test_python_reproduction_that_does_not_settle_the_claim_is_revised_and_succeeds_second_time(
    pricing: FixtureRepository, installed: Installed, first: str, result: AttemptResult, shown: str
) -> None:
    sandbox, ecosystem = installed

    run, model = write(
        pricing, writes(first), writes(ADDS_TAX), sandbox=sandbox, ecosystem=ecosystem
    )

    assert [attempt.result for attempt in run.attempts] == [result, SHOWS_A_DIFFERENCE]
    assert run.evidence is not None and run.evidence.reproduction == ADDS_TAX
    (earlier,) = json.loads(model.requests[1].data)["task"]["attempts"]
    assert shown in earlier["before"] and shown in earlier["after"]


def test_python_reproduction_that_never_shows_a_difference_leaves_the_claim_inconclusive(
    pricing: FixtureRepository, installed: Installed
) -> None:
    sandbox, ecosystem = installed
    same = [writes(SAME_ON_BOTH)] * 5

    run, _ = write(pricing, *same, sandbox=sandbox, ecosystem=ecosystem, attempt_cap=2)

    assert len(run.attempts) == 2
    assert run.evidence is None
    assert run.inconclusive == ATTEMPT_CAP_REACHED.format(attempts=2, ending=ENDINGS[SHOWS_NOTHING])


def test_what_the_model_writes_is_run_only_inside_the_sandbox(
    pricing: FixtureRepository,
    installed: Installed,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sandbox, ecosystem = installed
    monkeypatch.setenv("PR_VERIFICATION_API_KEY", "sk-not-for-the-sandbox")
    mark = tmp_path / "ran-outside-the-sandbox"
    # What a test prints is shown when it fails, as this one does on the code before.
    reaches_out = f"""\
def test_total_adds_tax():
    import os
    from pricing import total

    try:
        open({str(mark)!r}, "w").close()
    except OSError:
        pass
    print("key:", os.environ.get("PR_VERIFICATION_API_KEY"))
    assert total(100) == 120.0
"""

    run, _ = write(pricing, writes(reaches_out), sandbox=sandbox, ecosystem=ecosystem)

    assert run.evidence is not None
    assert not mark.exists()
    assert "key: None" in run.evidence.before.output
    assert "sk-not-for-the-sandbox" not in run.evidence.before.output + run.evidence.after.output
