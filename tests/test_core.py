import json
import tempfile
from pathlib import Path

import pytest
from conftest import (
    Clock,
    FixtureRepository,
    GathersNothing,
    a_reproduction,
    a_search,
    commit_snapshots,
    model_listing,
    results,
)

from pr_verification import verdict
from pr_verification.budget import DEFAULT_SECONDS, Budget, Ledger
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import Decision, Implementation, Question, ScriptedDecider, options_of
from pr_verification.ecosystem import Unreadable
from pr_verification.model import Model, ScriptedModel
from pr_verification.orchestration import Gathered, Orchestration
from pr_verification.python_ecosystem import NOT_A_PYTHON_PROJECT
from pr_verification.report import (
    BudgetLimit,
    BudgetSpend,
    Claim,
    ClaimKind,
    ClaimSpend,
    ClassifiedClaim,
    Evidence,
    EvidenceSource,
    GateDecision,
    PullRequestSpend,
    Report,
    RunEvidence,
    TextChange,
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


def test_report_for_no_claims_has_no_findings_and_spends_only_the_step_that_lists_the_claims(
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
    assert report.budget_spend.steps == 1
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
    assert (report.budget_spend.dollars, report.budget_spend.steps) == (0, 1)
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


NOT_ACCEPTED_FIXED = a_reproduction(results(failed="t"), results(passed="t"), accepted=False)
NOT_ACCEPTED_REGRESSED = a_reproduction(results(passed="t"), results(failed="t"), accepted=False)


@pytest.mark.parametrize(
    "evidence", [NOT_ACCEPTED_FIXED, NOT_ACCEPTED_REGRESSED], ids=["fails then passes", "regressed"]
)
def test_reproduction_the_judge_did_not_accept_decides_no_claim_whatever_gathered_it(
    fixture_repository: FixtureRepository, evidence: Evidence
) -> None:
    report = verify(fixture_repository, Gathers(Gathered((evidence,))), "Adds tax")

    assert report.claims == (
        Claim("Adds tax", Verdict.INCONCLUSIVE, verdict.DECIDES_NOTHING, (evidence,)),
    )
    assert report.gate.decision is GateDecision.PASS


RENAMED = a_search(TextChange.REMOVED, 2, 0)
STILL_THERE = a_search(TextChange.REMOVED, 2, 1)


@pytest.mark.parametrize(
    ("kind", "evidence", "expected", "reason"),
    [
        ("structural", (RENAMED,), Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
        ("structural", (STILL_THERE, FIXED), Verdict.REFUTED, verdict.STRUCTURAL_REFUTED),
        ("structural", (FIXED, SAID), Verdict.INCONCLUSIVE, verdict.NOTHING_SEARCHED),
        ("structural", (REGRESSED,), Verdict.INCONCLUSIVE, verdict.NOTHING_SEARCHED),
        ("behavioural", (STILL_THERE,), Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN),
        ("behavioural", (STILL_THERE, FIXED), Verdict.CONFIRMED, verdict.CONFIRMED),
    ],
    ids=[
        "structural, a search that supports",
        "structural, a search that contradicts beside a Reproduction",
        "structural, a Reproduction that supports",
        "structural, a Reproduction that contradicts",
        "behavioural, a search that contradicts",
        "behavioural, a Reproduction that supports beside a search",
    ],
)
def test_kind_of_a_claim_settles_which_evidence_decides_it_whatever_was_gathered(
    fixture_repository: FixtureRepository,
    kind: str,
    evidence: tuple[Evidence, ...],
    expected: Verdict,
    reason: str,
) -> None:
    pull_request = PullRequest(
        fixture_repository.path, fixture_repository.before, fixture_repository.after, "Add tax"
    )

    report = run(
        pull_request,
        model_listing("Renames total"),
        ScriptedDecider([kind]),
        orchestration=Gathers(Gathered(evidence)),
    )

    assert report.claims == (
        ClassifiedClaim("Renames total", expected, reason, evidence, kind=ClaimKind(kind)),
    )


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


# The Budget


class KeepsToABudget:
    """A stand-in orchestration that keeps to a Budget. For each Claim it records the spend
    prepared against that Claim, and gives what was prepared. It records each call, and the
    kinds each call told it."""

    def __init__(self, *prepared: tuple[BudgetSpend, Gathered]) -> None:
        self._prepared = prepared
        self.calls: list[tuple[Ledger, tuple[str, ...], Path, str, str, Model]] = []
        self.told: list[tuple[ClaimKind | None, ...] | None] = []

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        raise AssertionError("an orchestration that keeps to a Budget is asked within one")

    def gather_within(
        self,
        ledger: Ledger,
        claims: tuple[str, ...],
        *,
        repository: Path,
        before: str,
        after: str,
        model: Model,
        kinds: tuple[ClaimKind | None, ...] | None = None,
    ) -> tuple[Gathered, ...]:
        self.calls.append((ledger, claims, repository, before, after, model))
        self.told.append(kinds)
        ledger.share_out(len(claims))
        for place, (spend, _) in enumerate(self._prepared):
            ledger.record(spend, claim=place)
        return tuple(gathered for _, gathered in self._prepared)


class TakesAMinute(GathersNothing):
    """A stand-in orchestration that keeps to no Budget: gathering takes a minute on the clock."""

    def __init__(self, clock: Clock) -> None:
        super().__init__()
        self._clock = clock

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        self._clock.now += 60.0
        return super().gather(
            claims, repository=repository, before=before, after=after, model=model
        )


class Charges:
    """A stand-in decider that takes the first option of every question. Each decision takes
    `seconds` on the clock and costs `dollars`."""

    def __init__(self, clock: Clock, seconds: float, dollars: float) -> None:
        self._clock, self._seconds, self._dollars = clock, seconds, dollars
        self.asked: list[str] = []

    def decide(self, state: str, question: Question) -> Decision:
        self.asked.append(state)
        self._clock.now += self._seconds
        first, *others = options_of(question)
        probabilities = {first: 1.0, **dict.fromkeys(others, 0.0)}
        return Decision(question, probabilities, Implementation.STAND_IN, dollars=self._dollars)


def steps(number: int) -> BudgetSpend:
    return BudgetSpend(seconds=0.0, dollars=0.0, steps=number)


def a_pull_request(repository: FixtureRepository) -> PullRequest:
    return PullRequest(repository.path, repository.before, repository.after, "Add tax")


def spend_of(report: Report) -> PullRequestSpend:
    assert isinstance(report.budget_spend, PullRequestSpend)
    return report.budget_spend


def test_orchestration_that_keeps_to_a_budget_is_asked_once_within_the_ledger_of_the_call(
    fixture_repository: FixtureRepository,
) -> None:
    orchestration = KeepsToABudget((steps(2), Gathered(())), (steps(1), Gathered(())))
    model = model_listing("Adds tax", "Rounds")
    pull_request = PullRequest(fixture_repository.path, "HEAD~1", "HEAD", "Add tax")

    report = run(pull_request, model, orchestration=orchestration, clock=Clock())

    ((ledger, claims, repository, before, after, given),) = orchestration.calls
    assert (claims, repository) == (("Adds tax", "Rounds"), fixture_repository.path)
    assert (before, after) == (fixture_repository.before, fixture_repository.after)
    assert given is model
    # The ledger already held the step that listed the Claims, and the report is read from it.
    assert (ledger.spent().steps, report.budget_spend.steps) == (4, 4)


@pytest.mark.parametrize(
    ("labels", "kinds"),
    [
        (["structural", "behavioural"], (ClaimKind.STRUCTURAL, ClaimKind.BEHAVIOURAL)),
        (None, (None, None)),
    ],
    ids=["classified", "not classified"],
)
def test_orchestration_that_keeps_to_a_budget_is_told_the_kind_of_each_claim_in_order(
    fixture_repository: FixtureRepository,
    labels: list[str] | None,
    kinds: tuple[ClaimKind | None, ...],
) -> None:
    orchestration = KeepsToABudget((steps(0), Gathered(())), (steps(0), Gathered(())))

    run(
        a_pull_request(fixture_repository),
        model_listing("Renames total", "Adds tax"),
        None if labels is None else ScriptedDecider(labels),
        orchestration=orchestration,
        clock=Clock(),
    )

    assert orchestration.told == [kinds]


def test_report_shows_the_spend_in_each_unit_for_the_pull_request_and_for_each_claim(
    fixture_repository: FixtureRepository,
) -> None:
    clock = Clock()
    listing = json.dumps({"claims": ["Adds tax", "Rounds"]})
    orchestration = KeepsToABudget(
        (BudgetSpend(seconds=10.0, dollars=1.0, steps=3), Gathered(())),
        (BudgetSpend(seconds=5.0, dollars=0.0, steps=2), Gathered(())),
    )

    report = run(
        a_pull_request(fixture_repository),
        ScriptedModel([listing], dollars_per_request=0.25),
        Charges(clock, seconds=2.0, dollars=0.5),
        orchestration=orchestration,
        budget=Budget(seconds=100.0, dollars=10.0, steps=50),
        clock=clock,
    )

    # Listing the Claims is one step at the model's price. Each of the two decisions costs its
    # time and its dollars, and no step. Neither is for any one Claim.
    assert report.budget_spend == PullRequestSpend(
        seconds=19.0,
        dollars=2.25,
        steps=6,
        stopped_by=None,
        claims=(
            ClaimSpend(seconds=10.0, dollars=1.0, steps=3, stopped_by=None),
            ClaimSpend(seconds=5.0, dollars=0.0, steps=2, stopped_by=None),
        ),
    )


def test_report_shows_the_limit_that_stopped_the_run_and_it_says_nothing_of_a_verdict(
    fixture_repository: FixtureRepository,
) -> None:
    # 5 steps are left once the Claims are listed, so the share of each Claim is 2.
    orchestration = KeepsToABudget((steps(3), Gathered((FIXED,))), (steps(2), Gathered((), WHY)))

    report = run(
        a_pull_request(fixture_repository),
        model_listing("Adds tax", "Rounds"),
        orchestration=orchestration,
        budget=Budget(steps=6),
        clock=Clock(),
    )

    spent = (
        ClaimSpend(seconds=0.0, dollars=0.0, steps=3, stopped_by=BudgetLimit.STEPS),
        ClaimSpend(seconds=0.0, dollars=0.0, steps=2, stopped_by=BudgetLimit.STEPS),
    )
    assert report.budget_spend == PullRequestSpend(0.0, 0.0, 6, BudgetLimit.STEPS, spent)
    assert json.loads(report.to_json())["budget_spend"] == {
        "seconds": 0.0,
        "dollars": 0.0,
        "steps": 6,
        "stopped_by": "steps",
        "claims": [
            {"seconds": 0.0, "dollars": 0.0, "steps": 3, "stopped_by": "steps"},
            {"seconds": 0.0, "dollars": 0.0, "steps": 2, "stopped_by": "steps"},
        ],
    }
    # The Claim whose Evidence came of the very work that reached its share is still decided.
    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.CONFIRMED, verdict.CONFIRMED),
        (Verdict.INCONCLUSIVE, WHY),
    ]


@pytest.mark.parametrize(
    ("budget", "decided", "limit"),
    [(Budget(dollars=2.0), 2, BudgetLimit.MONEY), (Budget(steps=1), 0, BudgetLimit.STEPS)],
    ids=["spent by a decision", "spent by listing the Claims"],
)
def test_once_the_budget_is_spent_no_further_decision_is_asked_for_and_the_rest_go_unclassified(
    fixture_repository: FixtureRepository, budget: Budget, decided: int, limit: BudgetLimit
) -> None:
    claims = ["Adds tax", "Rounds", "Keeps prices"]
    clock = Clock()
    decider = Charges(clock, seconds=0.0, dollars=1.0)

    report = run(
        a_pull_request(fixture_repository),
        model_listing(*claims),
        decider,
        orchestration=GathersNothing(),
        budget=budget,
        clock=clock,
    )

    # Reaching a limit is not a failure: the report is given, with every Claim in it.
    assert decider.asked == claims[:decided]
    assert [claim.text for claim in report.claims] == claims
    assert [type(claim) for claim in report.claims] == [
        *[ClassifiedClaim] * decided,
        *[Claim] * (3 - decided),
    ]
    assert spend_of(report).stopped_by is limit


def test_what_an_orchestration_that_keeps_to_no_budget_spends_is_not_recorded_and_no_limit_stops_it(
    fixture_repository: FixtureRepository,
) -> None:
    clock = Clock()
    orchestration = TakesAMinute(clock)
    model = model_listing("Adds tax")

    report = run(
        a_pull_request(fixture_repository),
        model,
        orchestration=orchestration,
        budget=Budget(seconds=10.0),
        clock=clock,
    )

    ((_, _, _, _, given),) = orchestration.calls
    assert given is model
    nothing = ClaimSpend(seconds=0.0, dollars=0.0, steps=0, stopped_by=None)
    assert report.budget_spend == PullRequestSpend(0.0, 0.0, 1, None, (nothing,))


@pytest.mark.parametrize(
    ("seconds", "limit"),
    [(DEFAULT_SECONDS - 1, None), (DEFAULT_SECONDS, BudgetLimit.TIME)],
    ids=["under fifteen minutes", "fifteen minutes"],
)
def test_with_no_budget_given_the_provisional_defaults_are_kept_to(
    fixture_repository: FixtureRepository, seconds: float, limit: BudgetLimit | None
) -> None:
    spend = BudgetSpend(seconds=seconds, dollars=0.0, steps=0)
    orchestration = KeepsToABudget((spend, Gathered(())))

    report = run(
        a_pull_request(fixture_repository),
        model_listing("Adds tax"),
        orchestration=orchestration,
        clock=Clock(),
    )

    assert spend_of(report).stopped_by is limit


def test_budget_with_a_limit_that_is_not_more_than_zero_is_refused_before_anything_else_is_done(
    fixture_repository: FixtureRepository,
) -> None:
    model = model_listing()
    pull_request = PullRequest(fixture_repository.path, "no-such-commit", "HEAD", "Tidy up")

    with pytest.raises(ValueError):
        run(pull_request, model, budget=Budget(steps=0))

    assert model.requests == []
