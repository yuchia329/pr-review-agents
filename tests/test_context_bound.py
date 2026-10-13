"""The context bound: how much a role is shown of any one thing it reads, and what a cut says."""

import contextvars
import json
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import FixtureRepository, asks, commit_snapshots, finishes, model_listing
from hypothesis import given
from hypothesis import strategies as st

from pr_verification.agent import (
    NO_SUCH_TOOL,
    PROGRESS_INTERVAL,
    REPEAT_LENGTH,
    UNREADABLE_RESPONSE,
    Role,
    RoleRun,
    Tool,
    run_role,
)
from pr_verification.cli import main
from pr_verification.context_bound import (
    CUT_AFTER,
    CUT_BEFORE,
    SANDBOX_OUTPUT_LIMIT,
    SANDBOX_OUTPUT_VARIABLE,
    SMALLEST_LIMIT,
    TOOL_RESULT_LIMIT,
    TOOL_RESULT_VARIABLE,
    ContextLimits,
    InvalidContextLimits,
    Keep,
    bounded,
    limits_from_environment,
    limits_in_use,
    shown_within,
)
from pr_verification.decision import YES, ScriptedDecider
from pr_verification.ecosystem import Commands, Placement, Results, Unreadable, Unsupported
from pr_verification.model import Model, ModelRequest, ScriptedModel
from pr_verification.orchestration import Gathered
from pr_verification.reading import (
    BAD_ARGUMENTS,
    NO_SUCH_FILE,
    NO_SUCH_LINES,
    reading_tools,
)
from pr_verification.reproduction import AttemptResult, ReproductionRun, write_reproduction
from pr_verification.sandbox import Command, Limits, Phase, RunResult

LIMITS_TO_CUT_TO = st.integers(min_value=SMALLEST_LIMIT, max_value=600)
TEXTS = st.text(max_size=1500)
KEEPS = st.sampled_from(Keep)


# The rule that cuts a text


def cut(text: str, limit: int, keep: Keep) -> str:
    """What the contract says a text longer than `limit` is cut to."""
    whole = len(text)

    def with_kept(kept: int) -> str:
        if keep is Keep.START:
            return text[:kept] + CUT_AFTER.format(left_out=whole - kept, whole=whole)
        return CUT_BEFORE.format(left_out=whole - kept, whole=whole) + text[whole - kept :]

    return with_kept(max(kept for kept in range(whole) if len(with_kept(kept)) <= limit))


@given(text=TEXTS, limit=LIMITS_TO_CUT_TO, keep=KEEPS)
def test_whatever_the_text_what_is_shown_is_never_longer_than_the_limit(
    text: str, limit: int, keep: Keep
) -> None:
    assert len(bounded(text, limit, keep)) <= limit


@given(text=TEXTS, limit=LIMITS_TO_CUT_TO, keep=KEEPS)
def test_text_within_the_limit_is_never_changed(text: str, limit: int, keep: Keep) -> None:
    text = text[:limit]

    assert bounded(text, limit, keep) == text


@given(text=st.text(min_size=SMALLEST_LIMIT + 1, max_size=1500), keep=KEEPS, data=st.data())
def test_longer_text_keeps_as_much_of_one_end_as_fits_and_says_how_much_was_left_out(
    text: str, keep: Keep, data: st.DataObject
) -> None:
    limit = data.draw(st.integers(min_value=SMALLEST_LIMIT, max_value=len(text) - 1))

    assert bounded(text, limit, keep) == cut(text, limit, keep)


def test_cut_that_keeps_the_start_ends_with_the_words_that_say_so() -> None:
    text = "".join(f"line {number}\n" for number in range(1, 101))

    shown = bounded(text, SMALLEST_LIMIT, Keep.START)

    kept, _, said = shown.partition("\n[Cut here")
    assert text.startswith(kept) and kept.startswith("line 1\nline 2\n")
    left_out = len(text) - len(kept)
    assert "\n[Cut here" + said == CUT_AFTER.format(left_out=left_out, whole=len(text))
    assert left_out > 0


def test_cut_that_keeps_the_end_starts_with_the_words_that_say_so() -> None:
    text = "".join(f"line {number}\n" for number in range(1, 101))

    shown = bounded(text, SMALLEST_LIMIT, Keep.END)

    said, _, kept = shown.partition("]\n")
    assert text.endswith(kept) and kept.endswith("line 99\nline 100\n")
    left_out = len(text) - len(kept)
    assert said + "]\n" == CUT_BEFORE.format(left_out=left_out, whole=len(text))
    assert left_out > 0


@pytest.mark.parametrize("limit", [SMALLEST_LIMIT - 1, 0, -1])
def test_limit_under_the_smallest_is_rejected(limit: int) -> None:
    with pytest.raises(ValueError):
        bounded("short", limit, Keep.START)


# The limits in use


def test_default_limits_are_in_use_where_none_was_set() -> None:
    assert limits_in_use() == ContextLimits()
    assert ContextLimits() == ContextLimits(
        tool_result=TOOL_RESULT_LIMIT, sandbox_output=SANDBOX_OUTPUT_LIMIT
    )


def test_limits_are_in_use_inside_the_with_and_what_was_before_is_put_back_after() -> None:
    outer, inner = ContextLimits(300, 400), ContextLimits(500, 600)

    with shown_within(outer):
        assert limits_in_use() == outer
        with shown_within(inner):
            assert limits_in_use() == inner
        assert limits_in_use() == outer
    assert limits_in_use() == ContextLimits()


def test_limits_before_are_put_back_when_the_with_is_left_by_an_exception() -> None:
    with pytest.raises(KeyError), shown_within(ContextLimits(300, 400)):
        raise KeyError

    assert limits_in_use() == ContextLimits()


@pytest.mark.parametrize(
    "limits",
    [
        ContextLimits(tool_result=SMALLEST_LIMIT - 1),
        ContextLimits(sandbox_output=SMALLEST_LIMIT - 1),
    ],
)
def test_limit_under_the_smallest_is_rejected_before_anything_is_set(
    limits: ContextLimits,
) -> None:
    with pytest.raises(ValueError), shown_within(limits):
        pytest.fail("the limits were set")

    assert limits_in_use() == ContextLimits()


def test_smallest_limit_can_be_set() -> None:
    smallest = ContextLimits(SMALLEST_LIMIT, SMALLEST_LIMIT)

    with shown_within(smallest):
        assert limits_in_use() == smallest


def test_thread_started_with_a_copy_of_the_context_has_the_same_limits_in_use() -> None:
    set_here, seen = ContextLimits(300, 400), []

    with shown_within(set_here):
        thread = threading.Thread(
            target=contextvars.copy_context().run, args=(lambda: seen.append(limits_in_use()),)
        )
        thread.start()
        thread.join()

    assert seen == [set_here]


def test_limits_set_in_one_thread_are_not_in_use_in_another() -> None:
    seen = []

    def set_and_look() -> None:
        with shown_within(ContextLimits(300, 400)):
            seen.append(limits_in_use())

    thread = threading.Thread(target=set_and_look)
    thread.start()
    thread.join()

    assert seen == [ContextLimits(300, 400)]
    assert limits_in_use() == ContextLimits()


# The limits the environment configures


@pytest.mark.parametrize("unset", [{}, {TOOL_RESULT_VARIABLE: "", SANDBOX_OUTPUT_VARIABLE: ""}])
def test_environment_that_sets_no_limit_gives_the_defaults(unset: Mapping[str, str]) -> None:
    assert limits_from_environment(unset) == ContextLimits()


def test_environment_sets_each_limit_by_its_own_variable() -> None:
    assert (TOOL_RESULT_VARIABLE, SANDBOX_OUTPUT_VARIABLE) == (
        "PR_VERIFICATION_TOOL_RESULT_LIMIT",
        "PR_VERIFICATION_SANDBOX_OUTPUT_LIMIT",
    )
    assert limits_from_environment({TOOL_RESULT_VARIABLE: "5000"}) == ContextLimits(
        tool_result=5000
    )
    assert limits_from_environment({SANDBOX_OUTPUT_VARIABLE: str(SMALLEST_LIMIT)}) == ContextLimits(
        sandbox_output=SMALLEST_LIMIT
    )


@pytest.mark.parametrize("variable", [TOOL_RESULT_VARIABLE, SANDBOX_OUTPUT_VARIABLE])
@pytest.mark.parametrize("value", ["2.5", "many", "-5", "0", str(SMALLEST_LIMIT - 1)])
def test_limit_the_environment_cannot_set_is_refused_with_the_variable_named(
    variable: str, value: str
) -> None:
    with pytest.raises(InvalidContextLimits, match=variable):
        limits_from_environment({variable: value})


# What a role is shown of a tool's result

TASK = {"claim": "Fixes the tax on orders sent abroad"}


class Returning:
    """A tool that returns one prepared text for each request made of it, in order, and the
    last of them from then on."""

    def __init__(self, *returns: str, keeps: Keep | None = None) -> None:
        self._returns, self._calls = returns, 0
        description = "Reads something."
        self.tool = (
            Tool("read", description, self._run)
            if keeps is None
            else Tool("read", description, self._run, keeps=keeps)
        )

    def _run(self, arguments: Mapping[str, object]) -> str:
        self._calls += 1
        return self._returns[min(self._calls, len(self._returns)) - 1]


def run_reading(
    tool: Returning, *responses: str, step_cap: int = 5
) -> tuple[RoleRun, list[ModelRequest]]:
    model = ScriptedModel(responses)
    role = Role("reader", "Find the code.", (tool.tool,), step_cap)
    return run_role(role, TASK, model), model.requests


def results_shown(request: ModelRequest) -> list[str]:
    return [step["result"] for step in json.loads(request.data)["steps"]]


LONG = "".join(f"{number}:value_{number} = {number}\n" for number in range(1, 400))


def test_tool_keeps_the_start_of_its_result_unless_it_says_otherwise() -> None:
    assert Tool("read", "Reads something.", lambda arguments: "").keeps is Keep.START


@pytest.mark.parametrize("keep", list(Keep))
def test_result_over_the_limit_is_shown_cut_keeping_the_part_its_tool_says_matters(
    keep: Keep,
) -> None:
    with shown_within(ContextLimits(tool_result=300)):
        run, requests = run_reading(Returning(LONG, keeps=keep), asks("read"), finishes())

    assert results_shown(requests[1]) == [bounded(LONG, 300, keep)]
    assert results_shown(requests[1]) != [LONG]
    assert run.steps[0].result == LONG


def test_result_within_the_limit_is_shown_whole() -> None:
    with shown_within(ContextLimits(tool_result=len(LONG))):
        _, requests = run_reading(Returning(LONG), asks("read"), finishes())

    assert results_shown(requests[1]) == [LONG]


def test_result_is_bounded_by_the_default_limit_when_no_limit_was_set() -> None:
    returned = "x" * (TOOL_RESULT_LIMIT + 1)

    run, requests = run_reading(Returning(returned), asks("read"), finishes())

    assert results_shown(requests[1]) == [bounded(returned, TOOL_RESULT_LIMIT, Keep.START)]
    assert run.steps[0].result == returned


def test_step_is_shown_the_same_way_in_every_request_after_it() -> None:
    reading = Returning(LONG, "short", "brief")
    responses = [asks("read", part=part) for part in (1, 2, 3)]

    with shown_within(ContextLimits(tool_result=300)):
        _, requests = run_reading(reading, *responses, finishes())

    first = bounded(LONG, 300, Keep.START)
    assert [results_shown(request) for request in requests] == [
        [],
        [first],
        [first, "short"],
        [first, "short", "brief"],
    ]


def test_steps_that_ran_no_tool_are_shown_their_fixed_text_whole() -> None:
    with shown_within(ContextLimits(tool_result=SMALLEST_LIMIT)):
        _, requests = run_reading(Returning(LONG), asks("delete"), "not JSON", finishes())

    assert results_shown(requests[2]) == [NO_SUCH_TOOL, UNREADABLE_RESPONSE]


def test_task_and_the_models_own_responses_are_not_bounded_by_the_agent_loop() -> None:
    task = {"claim": "c" * 1000}
    asked = asks("read", note="n" * 1000)
    model = ScriptedModel([asked, finishes()])
    role = Role("reader", "Find the code.", (Returning("short").tool,), 5)

    with shown_within(ContextLimits(tool_result=SMALLEST_LIMIT)):
        run_role(role, task, model)

    sent = json.loads(model.requests[1].data)
    assert sent["task"] == task
    assert sent["steps"][0]["response"] == asked


def test_repeat_detection_compares_results_whole_whatever_the_role_was_shown() -> None:
    # Each result differs from the others only in what is cut off, so all are shown alike.
    returns = ["x" * 400 + str(number) for number in range(REPEAT_LENGTH + 1)]
    responses = [asks("read")] * (REPEAT_LENGTH + 1)

    with shown_within(ContextLimits(tool_result=300)):
        run, requests = run_reading(Returning(*returns), *responses, step_cap=REPEAT_LENGTH + 1)

    assert len(set(results_shown(requests[-1]))) == 1
    assert run.triggers == ()


def test_progress_guard_is_shown_each_result_as_the_role_is_shown_it() -> None:
    guard = ScriptedDecider([YES])
    responses = [asks("read", part=part) for part in range(PROGRESS_INTERVAL)]
    role = Role("reader", "Find the code.", (Returning(LONG).tool,), PROGRESS_INTERVAL + 1)

    with shown_within(ContextLimits(tool_result=300)):
        run_role(role, TASK, ScriptedModel([*responses, finishes()]), progress_guard=guard)

    ((state, _),) = guard.asked
    shown = [step["result"] for step in json.loads(state)["steps"]]
    assert shown == [bounded(LONG, 300, Keep.START)] * PROGRESS_INTERVAL


@given(
    returns=st.lists(st.text(max_size=1500), min_size=1, max_size=4),
    limit=LIMITS_TO_CUT_TO,
    keep=KEEPS,
)
def test_whatever_a_tool_returns_no_request_holds_more_of_it_than_the_limit(
    returns: list[str], limit: int, keep: Keep
) -> None:
    responses = [asks("read", part=part) for part in range(len(returns))]

    with shown_within(ContextLimits(tool_result=limit)):
        run, requests = run_reading(Returning(*returns, keeps=keep), *responses, finishes())

    assert [step.result for step in run.steps[:-1]] == returns
    for request in requests:
        assert all(len(shown) <= limit for shown in results_shown(request))
    assert results_shown(requests[-1]) == [bounded(returned, limit, keep) for returned in returns]


# Reading a file in parts

LINES = [f"entry {number}" for number in range(1, 11)]


@pytest.fixture(scope="module")
def ledger(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """One commit, with a file of ten lines and an empty file."""
    path = tmp_path_factory.mktemp("context-bound") / "repository"
    (only,) = commit_snapshots(
        path, {"ledger.txt": "".join(f"{line}\n" for line in LINES), "empty.txt": ""}
    )
    return FixtureRepository(path=path, before=only, after=only)


def read_file(ledger: FixtureRepository, **arguments: object) -> str:
    (tool,) = (
        tool for tool in reading_tools(ledger.path, ledger.after) if tool.name == "read_file"
    )
    return tool.run(arguments)


def numbered(first: int, last: int) -> str:
    return "\n".join(f"{number}:{LINES[number - 1]}" for number in range(first, last + 1))


@pytest.mark.parametrize(
    ("part", "first", "last"),
    [
        ({"first_line": 3, "last_line": 5}, 3, 5),
        ({"first_line": 4, "last_line": 4}, 4, 4),
        ({"first_line": 8}, 8, 10),
        ({"last_line": 2}, 1, 2),
        ({"first_line": 9, "last_line": 400}, 9, 10),
        ({"last_line": 400}, 1, 10),
        ({"first_line": 1, "last_line": 10}, 1, 10),
    ],
)
def test_part_of_a_file_is_read_by_line_numbers_and_each_line_keeps_its_own_number(
    ledger: FixtureRepository, part: dict[str, object], first: int, last: int
) -> None:
    assert read_file(ledger, path="ledger.txt", **part) == numbered(first, last)


@given(split=st.integers(min_value=1, max_value=len(LINES) - 1))
def test_file_read_in_two_parts_is_the_file_read_whole(
    ledger: FixtureRepository, split: int
) -> None:
    parts = [
        read_file(ledger, path="ledger.txt", last_line=split),
        read_file(ledger, path="ledger.txt", first_line=split + 1),
    ]

    assert "\n".join(parts) == read_file(ledger, path="ledger.txt")


@pytest.mark.parametrize(
    ("path", "part"),
    [
        ("ledger.txt", {"first_line": 11}),
        ("ledger.txt", {"first_line": 11, "last_line": 12}),
        ("empty.txt", {"first_line": 1}),
        ("empty.txt", {"last_line": 5}),
    ],
)
def test_part_that_holds_none_of_the_files_lines_is_answered_as_having_none(
    ledger: FixtureRepository, path: str, part: dict[str, object]
) -> None:
    assert read_file(ledger, path=path, **part) == NO_SUCH_LINES


def test_path_that_names_no_file_is_answered_before_the_part_is_looked_at(
    ledger: FixtureRepository,
) -> None:
    assert read_file(ledger, path="missing.txt", first_line=50) == NO_SUCH_FILE


@pytest.mark.parametrize(
    "part",
    [
        {"first_line": 4.0},
        {"first_line": "4"},
        {"first_line": True},
        {"last_line": 4.0},
        {"last_line": "4"},
        {"last_line": True},
        {"first_line": None},
        {"first_line": 0},
        {"first_line": -2, "last_line": 5},
        {"first_line": 5, "last_line": 4},
        {"last_line": 0},
        {"last_line": -1},
        {"first_line": 2, "lines": 3},
    ],
)
def test_line_numbers_read_file_cannot_use_are_answered_and_not_raised(
    ledger: FixtureRepository, part: dict[str, object]
) -> None:
    assert read_file(ledger, path="ledger.txt", **part) == BAD_ARGUMENTS


def test_read_only_tools_keep_the_start_of_what_they_return(ledger: FixtureRepository) -> None:
    tools = reading_tools(ledger.path, ledger.after)

    assert [tool.keeps for tool in tools] == [Keep.START] * 3


# What the Reproduction writer is shown of an earlier attempt's runs

RUN_LIMITS = Limits(seconds=120.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)
NOISE = "".join(f"collected item {number}\n" for number in range(1, 200))
PASSED, FAILED = NOISE + "ok test_tax", NOISE + "failed test_tax"


class LastLineEcosystem:
    """A stand-in in which the last line a run prints says how its one test ended."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        return Unsupported("A stand-in installs nothing.")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        word, name = run.output.splitlines()[-1].split()
        return Results(
            passed=(name,) if word == "ok" else (),
            failed=(name,) if word == "failed" else (),
            could_not_run=(),
        )

    def reproduction_form(self) -> str:
        return "A Reproduction is one line."

    def place(self, reproduction: str) -> Placement:
        return Placement("reproduction.txt", ("write", reproduction), ("run-reproduction",))


class PrintingSandbox:
    """A stand-in in which every Reproduction prints the same on a commit, whatever it is."""

    def __init__(self, repository: FixtureRepository, before: str, after: str) -> None:
        self._printed = {repository.before: before, repository.after: after}

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        output = "" if command[0] == "write" else self._printed[commit]
        return RunResult(output=output, exit_status=0, limit_hit=None)


def write_twice(
    repository: FixtureRepository, before: str, after: str
) -> tuple[ReproductionRun, list[ModelRequest]]:
    """The writer's record when the model gives a Reproduction on each of two attempts, and
    what the model was sent."""
    model = ScriptedModel([finishes(reproduction="first"), finishes(reproduction="second")])
    run = write_reproduction(
        "Adds tax to the total",
        model=model,
        sandbox=PrintingSandbox(repository, before, after),
        ecosystem=LastLineEcosystem(),
        repository=repository.path,
        before=repository.before,
        after=repository.after,
        limits=RUN_LIMITS,
        attempt_cap=2,
    )
    return run, model.requests


def outputs_shown(request: ModelRequest) -> list[tuple[str, str]]:
    attempts = json.loads(request.data)["task"]["attempts"]
    return [(attempt["before"], attempt["after"]) for attempt in attempts]


def test_writer_is_shown_the_end_of_a_long_run_and_the_record_keeps_it_whole(
    fixture_repository: FixtureRepository,
) -> None:
    before, after = PASSED, "preparing\n" + PASSED

    with shown_within(ContextLimits(sandbox_output=300)):
        run, requests = write_twice(fixture_repository, before, after)

    assert outputs_shown(requests[1]) == [
        (bounded(before, 300, Keep.END), bounded(after, 300, Keep.END))
    ]
    assert outputs_shown(requests[1]) != [(before, after)]
    first = run.attempts[0]
    assert first.result is AttemptResult.SHOWS_NOTHING
    assert first.before is not None and first.after is not None
    assert (first.before.output, first.after.output) == (before, after)


def test_run_within_the_limit_is_shown_to_the_writer_whole(
    fixture_repository: FixtureRepository,
) -> None:
    with shown_within(ContextLimits(sandbox_output=len(PASSED))):
        _, requests = write_twice(fixture_repository, PASSED, PASSED)

    assert outputs_shown(requests[1]) == [(PASSED, PASSED)]


def test_run_is_bounded_by_the_default_limit_when_no_limit_was_set(
    fixture_repository: FixtureRepository,
) -> None:
    printed = "x" * SANDBOX_OUTPUT_LIMIT + "\nok test_tax"

    _, requests = write_twice(fixture_repository, printed, printed)

    shown = bounded(printed, SANDBOX_OUTPUT_LIMIT, Keep.END)
    assert outputs_shown(requests[1]) == [(shown, shown)]


def test_evidence_keeps_the_whole_output_of_both_runs(
    fixture_repository: FixtureRepository,
) -> None:
    with shown_within(ContextLimits(sandbox_output=300)):
        run, _ = write_twice(fixture_repository, FAILED, PASSED)

    assert run.evidence is not None
    assert (run.evidence.before.output, run.evidence.after.output) == (FAILED, PASSED)


def test_limit_on_a_tools_result_does_not_bound_what_the_writer_is_shown_of_a_run(
    fixture_repository: FixtureRepository,
) -> None:
    with shown_within(ContextLimits(tool_result=SMALLEST_LIMIT, sandbox_output=len(PASSED))):
        _, requests = write_twice(fixture_repository, PASSED, PASSED)

    assert outputs_shown(requests[1]) == [(PASSED, PASSED)]


# The limits a run from the command line is made within


class SeesTheLimits:
    """A stand-in orchestration that gathers nothing and records the limits in use."""

    def __init__(self) -> None:
        self.seen: list[ContextLimits] = []

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        self.seen.append(limits_in_use())
        return tuple(Gathered(()) for _ in claims)


def run_from_the_command_line(
    repository: FixtureRepository, orchestration: SeesTheLimits | None = None
) -> int:
    return main(
        [
            "--repo", str(repository.path),
            "--before", repository.before,
            "--after", repository.after,
            "--title", "Fix the crash on empty input",
        ],
        model_listing("Fixes the crash on empty input"),
        ScriptedDecider(["behavioural"]),
        orchestration=orchestration or SeesTheLimits(),
    )  # fmt: skip


def test_command_line_run_is_made_within_the_limits_the_environment_configures(
    fixture_repository: FixtureRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(TOOL_RESULT_VARIABLE, "5000")
    monkeypatch.setenv(SANDBOX_OUTPUT_VARIABLE, "700")
    orchestration = SeesTheLimits()

    code = run_from_the_command_line(fixture_repository, orchestration)

    assert code == 0
    assert orchestration.seen == [ContextLimits(tool_result=5000, sandbox_output=700)]


def test_command_line_run_is_made_within_the_defaults_when_the_environment_sets_none(
    fixture_repository: FixtureRepository,
) -> None:
    orchestration = SeesTheLimits()

    run_from_the_command_line(fixture_repository, orchestration)

    assert orchestration.seen == [ContextLimits()]


@pytest.mark.parametrize("variable", [TOOL_RESULT_VARIABLE, SANDBOX_OUTPUT_VARIABLE])
def test_command_line_exits_two_without_a_report_when_a_limit_is_misconfigured(
    fixture_repository: FixtureRepository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    variable: str,
) -> None:
    monkeypatch.setenv(variable, "plenty")

    code = run_from_the_command_line(fixture_repository)

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert captured.err.startswith("pr-verification: ")
    assert len(captured.err.splitlines()) == 1
