import json
from pathlib import Path

import pytest
from conftest import FixtureRepository

from pr_verification.cli import exit_code, main
from pr_verification.report import (
    BudgetSpend,
    Claim,
    Evidence,
    EvidenceSource,
    Finding,
    Report,
    Verdict,
)


def test_command_line_prints_the_report_and_exits_zero_when_the_gate_passes(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    description = tmp_path / "description.md"
    description.write_text("Housekeeping only.\n")

    code = main(
        [
            "--repo", str(fixture_repository.path),
            "--before", fixture_repository.before,
            "--after", fixture_repository.after,
            "--title", "Tidy up",
            "--description-file", str(description),
        ]
    )  # fmt: skip

    printed = json.loads(capsys.readouterr().out)
    assert printed["budget_spend"].pop("seconds") >= 0
    assert printed == {
        "gate": {"decision": "pass", "note": "No Claims found: nothing to verify."},
        "before": fixture_repository.before,
        "after": fixture_repository.after,
        "claims": [],
        "findings": [],
        "budget_spend": {"dollars": 0.0, "steps": 0},
    }
    assert code == 0


def test_command_line_exits_two_without_a_report_when_a_commit_is_missing(
    fixture_repository: FixtureRepository, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(
        [
            "--repo", str(fixture_repository.path),
            "--before", "no-such-commit",
            "--after", fixture_repository.after,
            "--title", "Tidy up",
        ]
    )  # fmt: skip

    captured = capsys.readouterr()
    assert code == 2
    assert captured.out == ""
    assert "no-such-commit" in captured.err


def test_command_line_exits_two_not_one_when_the_pull_request_text_cannot_be_read(
    fixture_repository: FixtureRepository, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    description = tmp_path / "description.md"
    description.write_bytes(b"\xff\xfe not utf-8")

    code = main(
        [
            "--repo", str(fixture_repository.path),
            "--before", fixture_repository.before,
            "--after", fixture_repository.after,
            "--title", "Tidy up",
            "--description-file", str(description),
        ]
    )  # fmt: skip

    assert code == 2
    assert capsys.readouterr().out == ""


def _report_with(*claims: Claim) -> Report:
    return Report(
        before="b" * 40,
        after="a" * 40,
        claims=claims,
        findings=(Finding(concern="The diff also changes the session timeout."),),
        budget_spend=BudgetSpend(seconds=1.5, dollars=0.25, steps=3),
    )


REFUTED_CLAIM = Claim(
    text="Fixes the crash on empty input",
    verdict=Verdict.REFUTED,
    reason="The Reproduction fails on both commits.",
    evidence=(Evidence(source=EvidenceSource.RAN_CODE, observation="1 failed on both commits"),),
)


def test_exit_code_is_one_when_the_gate_blocks() -> None:
    assert exit_code(_report_with(REFUTED_CLAIM).gate) == 1


def test_printed_report_carries_each_claim_with_its_verdict_and_evidence() -> None:
    printed = json.loads(_report_with(REFUTED_CLAIM).to_json())

    assert printed["claims"] == [
        {
            "text": "Fixes the crash on empty input",
            "verdict": "refuted",
            "reason": "The Reproduction fails on both commits.",
            "evidence": [{"source": "ran_code", "observation": "1 failed on both commits"}],
        }
    ]
    assert printed["findings"] == [{"concern": "The diff also changes the session timeout."}]
    assert printed["gate"]["decision"] == "block"
    assert printed["budget_spend"] == {"seconds": 1.5, "dollars": 0.25, "steps": 3}
