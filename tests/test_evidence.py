import json
from collections.abc import Mapping

import pytest
from conftest import FixtureRepository
from hypothesis import given
from hypothesis import strategies as st

from pr_verification.evidence import run_on_both_commits
from pr_verification.report import (
    BudgetSpend,
    Claim,
    EvidenceSource,
    Report,
    RunEvidence,
    Verdict,
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

COMMAND = ("pytest", "tests/test_login.py")
LIMITS = Limits(seconds=60.0, memory_bytes=512 * 1024 * 1024, processors=1.0)

PASSED = RunResult(output="1 passed\n", exit_status=0, limit_hit=None)
FAILED = RunResult(output="1 failed\n", exit_status=1, limit_hit=None)
TIMED_OUT = RunResult(output="collecting", exit_status=None, limit_hit=LimitHit.TIME)
OUT_OF_MEMORY = RunResult(output="", exit_status=None, limit_hit=LimitHit.MEMORY)


class ScriptedSandbox:
    """A stand-in Sandbox that answers each commit as prepared and records what it was asked."""

    def __init__(self, answers: Mapping[str, RunResult | SandboxFailed]) -> None:
        self._answers = answers
        self.runs: list[tuple[Command, str, Phase, Limits]] = []

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self.runs.append((command, commit, phase, limits))
        answer = self._answers[commit]
        if isinstance(answer, SandboxFailed):
            raise answer
        return answer


def run_on_both(sandbox: Sandbox, phase: Phase = Phase.TEST) -> RunEvidence:
    return run_on_both_commits(
        sandbox, COMMAND, before="before-commit", after="after-commit", phase=phase, limits=LIMITS
    )


@pytest.mark.parametrize("phase", list(Phase))
def test_same_command_is_run_against_before_then_after_with_the_phase_and_limits_given(
    phase: Phase,
) -> None:
    sandbox = ScriptedSandbox({"before-commit": FAILED, "after-commit": PASSED})

    run_on_both(sandbox, phase)

    assert sandbox.runs == [
        (COMMAND, "before-commit", phase, LIMITS),
        (COMMAND, "after-commit", phase, LIMITS),
    ]


@pytest.mark.parametrize(
    ("before", "after", "observation"),
    [
        (FAILED, PASSED, "before: exit status 1; after: exit status 0"),
        (TIMED_OUT, PASSED, "before: stopped at the time limit; after: exit status 0"),
        (PASSED, OUT_OF_MEMORY, "before: exit status 0; after: stopped at the memory limit"),
        (
            OUT_OF_MEMORY,
            TIMED_OUT,
            "before: stopped at the memory limit; after: stopped at the time limit",
        ),
    ],
)
def test_both_runs_are_recorded_as_evidence_obtained_by_running_code_however_each_ended(
    before: RunResult, after: RunResult, observation: str
) -> None:
    sandbox = ScriptedSandbox({"before-commit": before, "after-commit": after})

    evidence = run_on_both(sandbox)

    assert [commit for _, commit, _, _ in sandbox.runs] == ["before-commit", "after-commit"]
    assert evidence.source is EvidenceSource.RAN_CODE
    assert evidence.command == COMMAND
    assert (evidence.before, evidence.after) == (before, after)
    assert evidence.observation == observation


@given(
    before_output=st.text(),
    after_output=st.text(),
    before_status=st.integers(min_value=0, max_value=255),
    after_status=st.integers(min_value=0, max_value=255),
)
def test_observation_is_written_from_the_exit_statuses_and_holds_none_of_the_output(
    before_output: str, after_output: str, before_status: int, after_status: int
) -> None:
    sandbox = ScriptedSandbox(
        {
            "before-commit": RunResult(before_output, before_status, None),
            "after-commit": RunResult(after_output, after_status, None),
        }
    )

    evidence = run_on_both(sandbox)

    assert evidence.observation == (
        f"before: exit status {before_status}; after: exit status {after_status}"
    )


@pytest.mark.parametrize("failing", ["before-commit", "after-commit"])
def test_sandbox_that_cannot_do_a_run_is_raised_and_not_recorded(failing: str) -> None:
    answers: dict[str, RunResult | SandboxFailed] = {
        "before-commit": PASSED,
        "after-commit": PASSED,
    }
    answers[failing] = SandboxFailed("no container runtime")

    with pytest.raises(SandboxFailed):
        run_on_both(ScriptedSandbox(answers))


def test_printed_report_holds_both_runs_among_the_evidence_for_a_claim() -> None:
    sandbox = ScriptedSandbox({"before-commit": TIMED_OUT, "after-commit": PASSED})
    evidence = run_on_both(sandbox)
    report = Report(
        before="before-commit",
        after="after-commit",
        claims=(Claim("Fixes login", Verdict.INCONCLUSIVE, "Not judged yet.", (evidence,)),),
        findings=(),
        budget_spend=BudgetSpend(seconds=1.0, dollars=0.0, steps=0),
    )

    printed = json.loads(report.to_json())

    assert printed["claims"][0]["evidence"] == [
        {
            "source": "ran_code",
            "observation": "before: stopped at the time limit; after: exit status 0",
            "command": ["pytest", "tests/test_login.py"],
            "before": {"output": "collecting", "exit_status": None, "limit_hit": "time"},
            "after": {"output": "1 passed\n", "exit_status": 0, "limit_hit": None},
        }
    ]


def test_command_run_in_a_real_sandbox_is_recorded_for_the_code_before_and_the_code_after(
    fixture_repository: FixtureRepository,
) -> None:
    command = ("grep", "there", "greeting.py")

    with ContainerSandbox(fixture_repository.path, "python:3.14-slim") as sandbox:
        evidence = run_on_both_commits(
            sandbox,
            command,
            before=fixture_repository.before,
            after=fixture_repository.after,
            phase=Phase.TEST,
            limits=LIMITS,
        )

    assert evidence.source is EvidenceSource.RAN_CODE
    assert evidence.command == command
    assert evidence.observation == "before: exit status 1; after: exit status 0"
    assert evidence.before.output == ""
    assert "hello there" in evidence.after.output
