from pathlib import Path

import pytest
from conftest import FixtureRepository

from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.report import GateDecision


def test_pull_request_with_no_claims_passes_the_gate_with_nothing_to_verify(
    fixture_repository: FixtureRepository,
) -> None:
    report = run(
        PullRequest(
            repository=fixture_repository.path,
            before=fixture_repository.before,
            after=fixture_repository.after,
            title="Tidy up",
        )
    )

    assert report.claims == ()
    assert report.gate.decision is GateDecision.PASS
    assert "nothing to verify" in report.gate.note


def test_report_names_the_two_commits_it_compared_in_full(
    fixture_repository: FixtureRepository,
) -> None:
    report = run(
        PullRequest(
            repository=fixture_repository.path, before="HEAD~1", after="HEAD", title="Tidy up"
        )
    )

    assert report.before == fixture_repository.before
    assert report.after == fixture_repository.after


def test_report_for_no_claims_has_no_findings_and_no_model_spend(
    fixture_repository: FixtureRepository,
) -> None:
    report = run(
        PullRequest(
            repository=fixture_repository.path,
            before=fixture_repository.before,
            after=fixture_repository.after,
            title="Tidy up",
        )
    )

    assert report.findings == ()
    assert report.budget_spend.dollars == 0
    assert report.budget_spend.steps == 0
    assert report.budget_spend.seconds >= 0


@pytest.mark.parametrize("missing", ["before", "after"])
def test_commit_that_is_not_in_the_repository_is_rejected(
    fixture_repository: FixtureRepository, missing: str
) -> None:
    commits = {"before": fixture_repository.before, "after": fixture_repository.after}
    commits[missing] = "0" * 40

    with pytest.raises(InvalidPullRequest, match=missing):
        run(PullRequest(repository=fixture_repository.path, title="Tidy up", **commits))


def test_directory_that_is_not_a_repository_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidPullRequest):
        run(PullRequest(repository=tmp_path, before="HEAD~1", after="HEAD", title="Tidy up"))


def test_directory_inside_a_repository_is_not_taken_for_the_repository(
    fixture_repository: FixtureRepository,
) -> None:
    inside = fixture_repository.path / "inside"
    inside.mkdir()

    with pytest.raises(InvalidPullRequest):
        run(PullRequest(repository=inside, before="HEAD~1", after="HEAD", title="Tidy up"))


def test_repository_named_on_the_call_wins_over_one_named_in_the_environment(
    fixture_repository: FixtureRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))

    report = run(
        PullRequest(
            repository=fixture_repository.path, before="HEAD~1", after="HEAD", title="Tidy up"
        )
    )

    assert report.after == fixture_repository.after
