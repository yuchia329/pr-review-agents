import json
import tempfile
from pathlib import Path

import pytest
from conftest import (
    FixtureRepository,
    GathersNothing,
    a_reproduction,
    commit_snapshots,
    model_listing,
    results,
)

from pr_verification import verdict
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import ScriptedDecider
from pr_verification.ecosystem import Unreadable
from pr_verification.model import Model
from pr_verification.orchestration import Gathered, Orchestration
from pr_verification.python_ecosystem import NOT_A_PYTHON_PROJECT
from pr_verification.report import (
    Claim,
    ClaimKind,
    ClassifiedClaim,
    Evidence,
    EvidenceSource,
    GateDecision,
    Report,
    RunEvidence,
    Verdict,
)
from pr_verification.sandbox import RunResult


def test_pull_request_with_no_claims_passes_the_gate_with_nothing_to_verify(
    fixture_repository: FixtureRepository,
) -> None:
    report = run(
        PullRequest(
            repository=fixture_repository.path,
            before=fixture_repository.before,
            after=fixture_repository.after,
            title="Tidy up",
        ),
        model_listing(),
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
        ),
        model_listing(),
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
        ),
        model_listing(),
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
        run(
            PullRequest(repository=fixture_repository.path, title="Tidy up", **commits),
            model_listing(),
        )


def test_directory_that_is_not_a_repository_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(InvalidPullRequest):
        run(
            PullRequest(repository=tmp_path, before="HEAD~1", after="HEAD", title="Tidy up"),
            model_listing(),
        )


def test_directory_inside_a_repository_is_not_taken_for_the_repository(
    fixture_repository: FixtureRepository,
) -> None:
    inside = fixture_repository.path / "inside"
    inside.mkdir()

    with pytest.raises(InvalidPullRequest):
        run(
            PullRequest(repository=inside, before="HEAD~1", after="HEAD", title="Tidy up"),
            model_listing(),
        )


def test_repository_named_on_the_call_wins_over_one_named_in_the_environment(
    fixture_repository: FixtureRepository, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "elsewhere.git"))

    report = run(
        PullRequest(
            repository=fixture_repository.path, before="HEAD~1", after="HEAD", title="Tidy up"
        ),
        model_listing(),
    )

    assert report.after == fixture_repository.after


# Verdicts, from what an orchestration gathered

FIXED = a_reproduction(results(failed="t"), results(passed="t"), output="SANDBOX-OUTPUT")
REGRESSED = a_reproduction(results(passed="t"), results(failed="t"))
SAME = a_reproduction(results(passed="t"), results(passed="t"))
COULD_NOT_RUN = a_reproduction(results(errors="t"), Unreadable("The run printed no results."))
SAID = Evidence(EvidenceSource.READ_CODE, "The model says this Claim is Confirmed.")
RAN_A_COMMAND = RunEvidence(
    EvidenceSource.RAN_CODE,
    "before: exit status 1; after: exit status 0",
    ("pytest",),
    RunResult("1 failed", 1, None),
    RunResult("1 passed", 0, None),
)
WHY = "The model could not be reached, and it said the Claim is Refuted."


class Gathers:
    """A stand-in orchestration that answers with exactly what was prepared, or raises it."""

    def __init__(self, *gathered: Gathered | Exception) -> None:
        self._gathered = gathered
        self.calls: list[tuple[tuple[str, ...], Path, str, str, Model]] = []

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        self.calls.append((claims, repository, before, after, model))
        for answer in self._gathered:
            if isinstance(answer, Exception):
                raise answer
        return tuple(answer for answer in self._gathered if isinstance(answer, Gathered))


class Fault(Exception):
    """An error that says nothing about the pull request: a fault in the system."""


def verify(repository: FixtureRepository, orchestration: Orchestration, *claims: str) -> Report:
    """The report for a pull request whose text makes these Claims."""
    pull_request = PullRequest(repository.path, repository.before, repository.after, "Add tax")
    return run(pull_request, model_listing(*claims), orchestration=orchestration)


@pytest.mark.parametrize(
    ("evidence", "expected", "reason", "gate"),
    [
        ((FIXED,), Verdict.CONFIRMED, verdict.CONFIRMED, GateDecision.PASS),
        ((REGRESSED,), Verdict.REFUTED, verdict.REFUTED, GateDecision.BLOCK),
        ((SAME,), Verdict.INCONCLUSIVE, verdict.DECIDES_NOTHING, GateDecision.PASS),
        ((COULD_NOT_RUN,), Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN, GateDecision.PASS),
        ((), Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN, GateDecision.PASS),
    ],
    ids=["confirmed", "refuted", "decides nothing", "could not run", "no evidence"],
)
def test_claim_is_given_the_verdict_computed_from_the_evidence_gathered_for_it(
    fixture_repository: FixtureRepository,
    evidence: tuple[Evidence, ...],
    expected: Verdict,
    reason: str,
    gate: GateDecision,
) -> None:
    report = verify(fixture_repository, Gathers(Gathered(evidence)), "Adds tax to the total")

    assert report.claims == (Claim("Adds tax to the total", expected, reason, evidence),)
    assert report.gate.decision is gate


def test_every_claim_receives_a_verdict_in_the_order_extracted_with_all_its_evidence(
    fixture_repository: FixtureRepository,
) -> None:
    gathered = (Gathered((SAID, FIXED)), Gathered(()), Gathered((REGRESSED, RAN_A_COMMAND)))

    report = verify(fixture_repository, Gathers(*gathered), "Adds tax", "Rounds", "Keeps prices")

    assert [(claim.text, claim.verdict, claim.evidence) for claim in report.claims] == [
        ("Adds tax", Verdict.CONFIRMED, (SAID, FIXED)),
        ("Rounds", Verdict.INCONCLUSIVE, ()),
        ("Keeps prices", Verdict.REFUTED, (REGRESSED, RAN_A_COMMAND)),
    ]
    assert report.gate.decision is GateDecision.BLOCK
    assert report.findings == ()
    assert (report.budget_spend.dollars, report.budget_spend.steps) == (0, 0)
    assert report.budget_spend.seconds >= 0


def test_orchestration_is_asked_once_for_every_claim_with_the_full_commits_and_the_model(
    fixture_repository: FixtureRepository,
) -> None:
    orchestration = GathersNothing()
    model = model_listing("Adds tax", "Rounds")
    pull_request = PullRequest(fixture_repository.path, "HEAD~1", "HEAD", "Add tax")

    run(pull_request, model, orchestration=orchestration)

    ((claims, repository, before, after, given),) = orchestration.calls
    assert (claims, repository) == (("Adds tax", "Rounds"), fixture_repository.path)
    assert (before, after) == (fixture_repository.before, fixture_repository.after)
    assert given is model


def test_orchestration_is_not_asked_when_the_pull_request_makes_no_claims(
    fixture_repository: FixtureRepository,
) -> None:
    orchestration = GathersNothing()

    report = verify(fixture_repository, orchestration)

    assert (report.claims, orchestration.calls) == ((), [])


@pytest.mark.parametrize(
    ("evidence", "expected", "reason"),
    [
        ((), Verdict.INCONCLUSIVE, WHY),
        ((SAME,), Verdict.INCONCLUSIVE, WHY),
        ((FIXED,), Verdict.CONFIRMED, verdict.CONFIRMED),
        ((REGRESSED,), Verdict.REFUTED, verdict.REFUTED),
    ],
    ids=["no evidence", "decides nothing", "confirmed", "refuted"],
)
def test_reason_the_orchestration_gives_explains_an_inconclusive_verdict_and_no_other(
    fixture_repository: FixtureRepository,
    evidence: tuple[Evidence, ...],
    expected: Verdict,
    reason: str,
) -> None:
    report = verify(fixture_repository, Gathers(Gathered(evidence, inconclusive=WHY)), "Adds tax")

    assert report.claims == (Claim("Adds tax", expected, reason, evidence),)


@pytest.mark.parametrize(
    "gathered",
    [
        Gathered((SAID,)),
        Gathered((RAN_A_COMMAND,)),
        Gathered((), inconclusive="This Claim is Confirmed."),
        Gathered((SAID, COULD_NOT_RUN), inconclusive="This Claim is Refuted."),
    ],
    ids=["a model's word", "a command that is no Reproduction", "a reason", "all of them"],
)
def test_claim_with_no_reproduction_that_ran_is_inconclusive_whatever_was_said_of_it(
    fixture_repository: FixtureRepository, gathered: Gathered
) -> None:
    report = verify(fixture_repository, Gathers(gathered), "This Claim is Confirmed")

    assert [claim.verdict for claim in report.claims] == [Verdict.INCONCLUSIVE]
    assert report.gate.decision is GateDecision.PASS


def test_report_shows_the_reproduction_and_its_output_on_the_code_before_and_after(
    fixture_repository: FixtureRepository,
) -> None:
    gathered = (Gathered((FIXED,)), Gathered((COULD_NOT_RUN, a_reproduction(None, None))))

    report = verify(fixture_repository, Gathers(*gathered), "Adds tax", "Rounds")

    confirmed, inconclusive = json.loads(report.to_json())["claims"]
    ran = {"output": "SANDBOX-OUTPUT", "exit_status": 0, "limit_hit": None}
    assert confirmed["evidence"] == [
        {
            "source": "ran_code",
            "observation": FIXED.observation,
            "command": ["run-reproduction"],
            "before": ran,
            "after": ran,
            "path": "reproduction.txt",
            "reproduction": FIXED.reproduction,
            "before_results": {"passed": [], "failed": ["t"], "could_not_run": []},
            "after_results": {"passed": ["t"], "failed": [], "could_not_run": []},
        }
    ]
    could_not_run, never_read = inconclusive["evidence"]
    assert could_not_run["before_results"] == {"passed": [], "failed": [], "could_not_run": ["t"]}
    assert could_not_run["after_results"] == {"reason": "The run printed no results."}
    assert (never_read["before_results"], never_read["after_results"]) == (None, None)


@pytest.mark.parametrize(
    "gathered",
    [(), (Gathered((FIXED,)),), (Gathered(()), Gathered(()), Gathered(()))],
    ids=["none", "too few", "too many"],
)
def test_orchestration_that_does_not_answer_for_each_claim_is_refused(
    fixture_repository: FixtureRepository, gathered: tuple[Gathered, ...]
) -> None:
    with pytest.raises(ValueError):
        verify(fixture_repository, Gathers(*gathered), "Adds tax", "Rounds")


def test_fault_in_the_orchestration_is_raised_and_not_turned_into_a_verdict(
    fixture_repository: FixtureRepository,
) -> None:
    with pytest.raises(Fault):
        verify(fixture_repository, Gathers(Fault("the disc is full")), "Adds tax")


def test_claim_labelled_structural_is_verified_as_a_behavioural_claim_is(
    fixture_repository: FixtureRepository,
) -> None:
    pull_request = PullRequest(
        fixture_repository.path, fixture_repository.before, fixture_repository.after, "Add tax"
    )
    orchestration = Gathers(Gathered((FIXED,)), Gathered((SAID,)))

    report = run(
        pull_request,
        model_listing("Adds tax", "Renames total"),
        ScriptedDecider(["structural", "structural"]),
        orchestration=orchestration,
    )

    assert report.claims == (
        ClassifiedClaim(
            "Adds tax", Verdict.CONFIRMED, verdict.CONFIRMED, (FIXED,), kind=ClaimKind.STRUCTURAL
        ),
        ClassifiedClaim(
            "Renames total",
            Verdict.INCONCLUSIVE,
            verdict.NO_REPRODUCTION_RAN,
            (SAID,),
            kind=ClaimKind.STRUCTURAL,
        ),
    )
    assert orchestration.calls[0][0] == ("Adds tax", "Renames total")


def test_without_an_orchestration_the_fixed_pipeline_is_used_and_nothing_is_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    before, after = commit_snapshots(
        tmp_path / "repository", {"prices.txt": "net\n"}, {"prices.txt": "net and tax\n"}
    )
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temporary))
    model = model_listing("Adds tax", "Rounds")

    report = run(PullRequest(tmp_path / "repository", before, after, "Add tax"), model)

    # The fixed pipeline finds no Ecosystem for the repository, and says so in the Python
    # Ecosystem's words: the only Ecosystem the system has.
    assert report.claims == (
        Claim("Adds tax", Verdict.INCONCLUSIVE, NOT_A_PYTHON_PROJECT),
        Claim("Rounds", Verdict.INCONCLUSIVE, NOT_A_PYTHON_PROJECT),
    )
    assert len(model.requests) == 1
    assert list(temporary.iterdir()) == []
