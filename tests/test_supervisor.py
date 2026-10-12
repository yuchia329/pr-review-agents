"""The LLM supervisor: what it is shown, what it may ask for, and what code decides whatever it
says.

The supervising model is a scripted stand-in, kept apart from the stand-in that does the roles'
work. The Sandbox, the Ecosystem, and the roles' model are the stand-ins of the fixed
pipeline's tests.
"""

import json
import tempfile
import threading
from collections.abc import Iterable, Mapping
from itertools import groupby
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, FixtureRepository, a_reproduction, a_search, commit_snapshots, results
from hypothesis import given, settings
from hypothesis import strategies as st
from test_pipeline import (
    DESCRIPTION,
    FETCH,
    LINKED_ISSUE,
    NAMES_BOTH,
    NOT_FOUND,
    REJECTS,
    RENAMES,
    THREE,
    TITLE,
    WHY,
    Discovery,
    Fault,
    LineSandbox,
    Sandboxes,
    WritesFor,
    a_pipeline,
    cap_reached,
    commands,
    renaming,
    writes,
)

from pr_verification import verdict
from pr_verification.budget import (
    BUDGET_SPENT,
    DEFAULT_DOLLARS,
    DEFAULT_SECONDS,
    DEFAULT_STEPS,
    Budget,
    Ledger,
)
from pr_verification.cli import main
from pr_verification.core import PullRequest, run
from pr_verification.decision import ScriptedDecider
from pr_verification.ecosystem import Unsupported
from pr_verification.model import Model, ModelFailed, ModelRequest, ScriptedModel
from pr_verification.orchestration import (
    ORCHESTRATION_VARIABLE,
    Gathered,
    InvalidOrchestration,
    OrchestrationChoice,
    orchestration_choice,
)
from pr_verification.repo_profile import DirectoryStore, RepoProfile
from pr_verification.report import (
    AcceptedReproduction,
    BudgetLimit,
    BudgetSpend,
    ClaimKind,
    GateDecision,
    PullRequestSpend,
    Report,
    TextChange,
    Verdict,
    compute_gate,
)
from pr_verification.reproduction import AttemptResult
from pr_verification.supervisor import (
    CLAIM_TEXT_CAP,
    CLAIMS_SHOWN_CAP,
    DISPATCH_CAP,
    DISPATCH_REFUSED,
    FINISH_REFUSED,
    MOVE_REFUSED,
    SUPERVISOR_STEP_CAP,
    SUPERVISOR_UNREADABLE,
    ResultSummary,
    Strategy,
    Supervisor,
    strategies_for,
    summarise,
    supervisor,
)
from pr_verification.tracing import SpanKind, record
from pr_verification.verdict import compute_verdict

FINISH = json.dumps({"finish": {}})
"""A response in which the supervisor finishes."""

CONFIRMS = (Verdict.CONFIRMED, verdict.CONFIRMED)
REFUTES = (Verdict.REFUTED, verdict.REFUTED)
SHOWS_NOTHING = (Verdict.INCONCLUSIVE, cap_reached(AttemptResult.SHOWS_NOTHING))
DECIDED = [CONFIRMS, SHOWS_NOTHING, REFUTES]
"""The Verdict and the reason of each Claim of `THREE`, once each has had its attempt."""


def dispatch(*places: int, reading: Iterable[int] = ()) -> str:
    """A response in which the supervisor dispatches a Reproduction for the Claim at each of
    these places, in this order, or a reading for those `reading` names."""
    by_reading = set(reading)
    return json.dumps(
        {
            "dispatch": [
                {"claim": place, "strategy": "reading" if place in by_reading else "reproduction"}
                for place in places
            ]
        }
    )


def move(source: int, to: int, **amounts: float) -> str:
    """A response in which the supervisor moves these amounts from one Claim's share to
    another's."""
    return json.dumps({"move": {"from": source, "to": to, **amounts}})


def requests_of(supervising: ScriptedModel) -> list[dict[str, Any]]:
    """What the supervisor was shown in each request, in order."""
    return [json.loads(request.data) for request in supervising.requests]


def left_of_each(request: Mapping[str, Any]) -> dict[int, dict[str, float]]:
    """What a request says is left of the share of each Claim it shows."""
    return {shown["claim"]: shown["left"] for shown in request["claims"]}


def result(
    place: int, evidence_count: int, computed: Verdict, reason: str, strategy: str = "reproduction"
) -> dict[str, object]:
    """The summary of one dispatch, as a request shows it."""
    return {
        "claim": place,
        "strategy": strategy,
        "evidence_count": evidence_count,
        "verdict": computed.value,
        "reason": reason,
    }


class FailsAfter:
    """A stand-in for a supervising model that gives the responses prepared and then cannot
    be reached."""

    def __init__(self, *responses: str) -> None:
        self._responses = responses
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        if len(self.requests) > len(self._responses):
            raise ModelFailed("the model could not be reached")
        return self._responses[len(self.requests) - 1]


class MeetsFirst(WritesFor):
    """A `WritesFor` in which the first request made for each Claim waits until one has been
    made for every Claim, so the Claims are answered only when they are worked on at the same
    time. It records the threads the requests for each Claim were made on."""

    def __init__(self, responses: Mapping[str, str | Exception]) -> None:
        super().__init__(responses)
        self._met = threading.Barrier(len(responses))
        self.threads: dict[str, set[int]] = {}

    def complete(self, request: ModelRequest) -> str:
        task = json.loads(request.data).get("task")
        if task is not None:
            first = task["claim"] not in self.threads
            self.threads.setdefault(task["claim"], set()).add(threading.get_ident())
            if first:
                self._met.wait(timeout=10)
        return super().complete(request)


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A repository with nothing of any Ecosystem in it, and a pull request that changes it.
    It is made once, since nothing is written to it."""
    path = tmp_path_factory.mktemp("shop") / "repository"
    before, after = commit_snapshots(
        path,
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net\n"},
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net and tax\n"},
    )
    return FixtureRepository(path=path, before=before, after=after)


def verify(
    repository: FixtureRepository,
    roles: WritesFor,
    supervising: Model,
    state: Path,
    *kinds: str,
    sandboxes: Sandboxes | None = None,
    discover: Discovery | None = None,
    budget: Budget | None = None,
    step_cap: int = SUPERVISOR_STEP_CAP,
) -> Report:
    """The report from the core's entry point when the supervisor gathers the Evidence, where
    the Claim classifier labels the Claims with these kinds, and each Behavioural unless
    given."""
    pipeline = a_pipeline(state, sandboxes or Sandboxes(repository), discover)
    pull_request = PullRequest(
        repository.path, repository.before, repository.after, TITLE, DESCRIPTION, LINKED_ISSUE
    )
    return run(
        pull_request,
        roles,
        ScriptedDecider(kinds or ["behavioural"] * len(roles.claims)),
        orchestration=Supervisor(pipeline, model=supervising, step_cap=step_cap),
        budget=budget,
        clock=Clock(),
    )


def gather_within(
    orchestration: Supervisor,
    ledger: Ledger,
    repository: FixtureRepository,
    roles: WritesFor,
    kinds: tuple[ClaimKind | None, ...] | None = None,
) -> list[Gathered]:
    """What the supervisor gathers within `ledger` for the Claims the roles' model has a
    response for."""
    return list(
        orchestration.gather_within(
            ledger,
            roles.claims,
            repository=repository.path,
            before=repository.before,
            after=repository.after,
            model=roles,
            kinds=kinds,
        )
    )


def decided(report: Report) -> list[tuple[Verdict, str]]:
    return [(claim.verdict, claim.reason) for claim in report.claims]


def written_in(sandbox: LineSandbox) -> set[str]:
    """The Reproductions that were written in a Sandbox."""
    return {command[1] for command, *_ in sandbox.runs if command[0] == "write"}


# Chosen by configuration


@pytest.mark.parametrize(
    ("environment", "choice"),
    [
        ({}, OrchestrationChoice.PIPELINE),
        ({ORCHESTRATION_VARIABLE: ""}, OrchestrationChoice.PIPELINE),
        ({ORCHESTRATION_VARIABLE: "pipeline"}, OrchestrationChoice.PIPELINE),
        ({ORCHESTRATION_VARIABLE: "supervisor"}, OrchestrationChoice.SUPERVISOR),
    ],
    ids=["unset", "empty", "pipeline", "supervisor"],
)
def test_orchestration_is_the_fixed_pipeline_unless_the_environment_names_the_supervisor(
    environment: dict[str, str], choice: OrchestrationChoice
) -> None:
    assert orchestration_choice(environment) is choice


@pytest.mark.parametrize("written", ["Supervisor", " supervisor", "llm"])
def test_orchestration_that_is_neither_of_the_two_is_refused_naming_the_variable(
    written: str,
) -> None:
    with pytest.raises(InvalidOrchestration, match=ORCHESTRATION_VARIABLE):
        orchestration_choice({ORCHESTRATION_VARIABLE: written})


def test_supervisor_keeps_what_it_remembers_under_its_state_directory_and_has_what_it_is_given(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    state, guard, supervising = tmp_path / "state", ScriptedDecider([]), ScriptedModel([])
    profile = RepoProfile("lines:1", (FETCH,), ("run-tests",), "lines", (".",), (), "a digest")

    made = supervisor(state, progress_guard=guard, model=supervising)

    made.pipeline.store.save(shop.path, profile)
    assert DirectoryStore(state / "profiles").load(shop.path) == profile
    assert (made.pipeline.progress_guard, made.model) == (guard, supervising)
    assert made.step_cap == SUPERVISOR_STEP_CAP
    assert supervisor(state).model is None


def supervised(model: WritesFor) -> list[ModelRequest]:
    """The requests in which a model that does the roles' work was asked to supervise."""
    return [
        request for request in model.requests if "supervisor_steps_left" in json.loads(request.data)
    ]


@pytest.mark.parametrize("kept", [True, False], ids=["with a state directory", "with none"])
def test_command_line_verifies_with_the_supervisor_when_the_environment_names_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kept: bool,
) -> None:
    accounts = renaming(tmp_path / "repository")
    (tmp_path / "temporary").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temporary"))
    monkeypatch.setenv(ORCHESTRATION_VARIABLE, "supervisor")
    arguments = ["--repo", str(accounts.path), "--before", accounts.before, "--after"]
    state = ["--state-dir", str(tmp_path / "state")] if kept else []
    # The model that does the roles' work supervises as well. What this one answers is no
    # request the supervisor may make, so the supervisor is stopped at its step cap.
    model = WritesFor({RENAMES: NAMES_BOTH})

    exited = main(
        [*arguments, accounts.after, "--title", "Rename", *state],
        model,
        ScriptedDecider(["structural"]),
    )

    (claim,) = json.loads(capsys.readouterr().out)["claims"]
    assert (exited, claim["verdict"]) == (0, "confirmed")
    assert claim["reason"] == verdict.STRUCTURAL_CONFIRMED
    assert len(supervised(model)) == SUPERVISOR_STEP_CAP
    assert list((tmp_path / "temporary").iterdir()) == []


def test_command_line_keeps_to_the_fixed_pipeline_when_the_environment_names_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    accounts = renaming(tmp_path / "repository")
    monkeypatch.setenv(ORCHESTRATION_VARIABLE, "pipeline")
    arguments = ["--repo", str(accounts.path), "--before", accounts.before, "--after"]
    model = WritesFor({RENAMES: NAMES_BOTH})

    exited = main(
        [*arguments, accounts.after, "--title", "Rename", "--state-dir", str(tmp_path / "state")],
        model,
        ScriptedDecider(["structural"]),
    )

    (claim,) = json.loads(capsys.readouterr().out)["claims"]
    assert (exited, claim["verdict"], supervised(model)) == (0, "confirmed", [])


def test_command_line_exits_two_without_a_report_when_the_orchestration_cannot_be_what_is_set(
    shop: FixtureRepository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv(ORCHESTRATION_VARIABLE, "llm")
    arguments = ["--repo", str(shop.path), "--before", shop.before, "--after", shop.after]

    exited = main([*arguments, "--title", "Add tax"], WritesFor(THREE), ScriptedDecider([]))

    printed = capsys.readouterr()
    assert (exited, printed.out) == (2, "")
    assert printed.err.startswith("pr-verification: ") and ORCHESTRATION_VARIABLE in printed.err
    assert len(printed.err.splitlines()) == 1


# The rules that code keeps


@pytest.mark.parametrize(
    ("kind", "open_to_it"),
    [
        (ClaimKind.BEHAVIOURAL, (Strategy.REPRODUCTION,)),
        (None, (Strategy.REPRODUCTION,)),
        (ClaimKind.STRUCTURAL, (Strategy.READING,)),
    ],
    ids=["behavioural", "not classified", "structural"],
)
def test_each_kind_of_claim_has_one_strategy(
    kind: ClaimKind | None, open_to_it: tuple[Strategy, ...]
) -> None:
    assert strategies_for(kind) == open_to_it


FAILS_THEN_PASSES = a_reproduction(results(failed="t"), results(passed="t"))
REMOVED = a_search(TextChange.REMOVED, 2, 0)


@pytest.mark.parametrize(
    ("kind", "gathered", "evidence_count", "computed", "reason"),
    [
        (ClaimKind.BEHAVIOURAL, Gathered((FAILS_THEN_PASSES,)), 1, *CONFIRMS),
        (None, Gathered((FAILS_THEN_PASSES,)), 1, *CONFIRMS),
        (
            ClaimKind.STRUCTURAL,
            Gathered((REMOVED, REMOVED)),
            2,
            Verdict.CONFIRMED,
            verdict.STRUCTURAL_CONFIRMED,
        ),
        (
            ClaimKind.STRUCTURAL,
            Gathered((FAILS_THEN_PASSES,)),
            1,
            Verdict.INCONCLUSIVE,
            verdict.NOTHING_SEARCHED,
        ),
        (
            ClaimKind.BEHAVIOURAL,
            Gathered((), "The model could not be reached."),
            0,
            Verdict.INCONCLUSIVE,
            verdict.NO_REPRODUCTION_RAN,
        ),
    ],
    ids=[
        "a Reproduction that decides",
        "a Claim that was not classified",
        "searches that decide",
        "a Reproduction for a Structural Claim",
        "no Evidence",
    ],
)
def test_summary_of_a_result_is_its_count_of_evidence_and_the_verdict_code_computes_from_it(
    kind: ClaimKind | None,
    gathered: Gathered,
    evidence_count: int,
    computed: Verdict,
    reason: str,
) -> None:
    summary = summarise(4, Strategy.READING, kind, gathered)

    assert summary == ResultSummary(4, Strategy.READING, evidence_count, computed, reason)


@given(said=st.none() | st.text(), text=st.text(), output=st.text(), observation=st.text())
def test_for_any_result_its_summary_holds_none_of_its_text(
    said: str | None, text: str, output: str, observation: str
) -> None:
    evidence = a_reproduction(
        results(failed="t"), results(passed="t"), text=text, output=output, observation=observation
    )

    summary = summarise(
        0, Strategy.REPRODUCTION, ClaimKind.BEHAVIOURAL, Gathered((evidence,), said)
    )

    assert summary == ResultSummary(0, Strategy.REPRODUCTION, 1, *CONFIRMS)


# A run


def test_supervisor_chooses_the_order_and_each_claim_is_decided_by_what_its_dispatch_recorded(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor(THREE)
    supervising = ScriptedModel([dispatch(2), dispatch(0, 1), FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    assert decided(report) == DECIDED
    assert report.gate.decision is GateDecision.BLOCK
    assert isinstance(report.claims[0].evidence[0], AcceptedReproduction)
    assert len(supervising.requests) == 3
    # The Claim dispatched alone in the first step was worked on before the others.
    worked_on = [json.loads(request.data)["task"]["claim"] for request in roles.requests[1:]]
    assert worked_on[:2] == ["Keeps", "Keeps"]
    # Each step of the supervisor is one step of the Budget, recorded for no Claim.
    spend = report.budget_spend
    assert isinstance(spend, PullRequestSpend)
    assert spend.steps == len(roles.requests) + len(supervising.requests)
    assert [claim.steps for claim in spend.claims] == [len(roles.tasks(text)) for text in THREE]


def test_each_request_holds_what_is_so_now_and_only_the_summaries_of_the_step_before(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor(THREE)
    supervising = ScriptedModel([dispatch(0, 2), dispatch(1), FINISH])
    ledger = Ledger(Budget(seconds=90.0, dollars=3.0, steps=300), clock=Clock())
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising)
    kinds = (ClaimKind.BEHAVIOURAL, None, ClaimKind.BEHAVIOURAL)

    gathered = gather_within(orchestration, ledger, shop, roles, kinds)

    def shown(place: int, strategies: list[str], attempts: int, computed: str) -> dict[str, object]:
        text = roles.claims[place]
        return {
            "claim": place,
            "kind": None if place == 1 else "behavioural",
            "text": text,
            "strategies": strategies,
            "attempts": attempts,
            "verdict": computed,
            "left": {
                "seconds": 30.0,
                "dollars": 1.0,
                "steps": 100 - (len(roles.tasks(text)) if attempts else 0),
            },
        }

    untried = [shown(place, ["reproduction"], 0, "inconclusive") for place in range(3)]
    first, second, third = requests_of(supervising)
    assert first == {
        "claims": untried,
        "unattempted": 3,
        "budget": {"seconds": 90.0, "dollars": 3.0, "steps": 300},
        "supervisor_steps_left": SUPERVISOR_STEP_CAP - 1,
        "results": [],
        "refused": None,
    }
    # A Claim still to have its attempt is shown first, and no strategy is open to a Claim
    # that Evidence has been gathered for.
    spent_on_two = len(roles.tasks("Adds tax")) + len(roles.tasks("Keeps"))
    assert second == {
        "claims": [untried[1], shown(0, [], 1, "confirmed"), shown(2, [], 1, "refuted")],
        "unattempted": 1,
        "budget": {"seconds": 90.0, "dollars": 3.0, "steps": 299 - spent_on_two},
        "supervisor_steps_left": SUPERVISOR_STEP_CAP - 2,
        "results": [result(0, 1, *CONFIRMS), result(2, 1, *REFUTES)],
        "refused": None,
    }
    assert third == {
        "claims": [
            shown(1, ["reproduction"], 1, "inconclusive"),
            shown(0, [], 1, "confirmed"),
            shown(2, [], 1, "refuted"),
        ],
        "unattempted": 0,
        "budget": {"seconds": 90.0, "dollars": 3.0, "steps": 298 - len(roles.requests)},
        "supervisor_steps_left": SUPERVISOR_STEP_CAP - 3,
        "results": [result(1, 0, Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)],
        "refused": None,
    }
    assert len({request.instructions for request in supervising.requests}) == 1
    assert [len(given.evidence) for given in gathered] == [1, 0, 1]
    assert gathered[1].inconclusive == SHOWS_NOTHING[1]


def test_supervisor_is_shown_structured_claims_and_summaries_and_the_report_keeps_the_results(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor(
        {
            "Adds tax": writes("fixed"),
            "Rounds": ModelFailed("WHAT-THE-FAILURE-SAID"),
            "Keeps": writes("broken"),
        },
        judged={"Keeps": REJECTS},
    )
    supervising = ScriptedModel([dispatch(0, 1, 2), FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    untrusted = [
        TITLE,
        DESCRIPTION,
        LINKED_ISSUE,
        "net and tax",
        "test_total",
        "reproduction.txt",
        "WHAT-THE-FAILURE-SAID",
        WHY,
    ]
    for request in supervising.requests:
        for text in untrusted:
            assert text not in request.data and text not in request.instructions
    nothing_ran = (Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)
    assert requests_of(supervising)[1]["results"] == [
        result(0, 1, *CONFIRMS),
        result(1, 0, *nothing_ran),
        result(2, 0, *nothing_ran),
    ]
    # All of it is kept for the report, though only the summaries reached the supervisor.
    printed = report.to_json()
    assert all(text in printed for text in ("test_total", "WHAT-THE-FAILURE-SAID", WHY))


MANY = CLAIMS_SHOWN_CAP + 3


def test_supervisor_is_shown_no_more_than_the_caps_allow_however_many_claims_there_are(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    claims = [f"{RENAMES} in part {place}" + ", and elsewhere" * 30 for place in range(MANY)]
    unreachable = ModelFailed("the model could not be reached")
    roles = WritesFor({claim: NAMES_BOTH for claim in claims} | {claims[1]: unreachable})
    first_step = range(DISPATCH_CAP)
    supervising = ScriptedModel([dispatch(*first_step, reading=first_step), FINISH])
    orchestration = Supervisor(
        a_pipeline(tmp_path / "state", Sandboxes(accounts)), model=supervising, step_cap=2
    )

    gathered = gather_within(
        orchestration, Ledger(clock=Clock()), accounts, roles, (ClaimKind.STRUCTURAL,) * MANY
    )

    first, second = requests_of(supervising)
    for request in (first, second):
        assert len(request["claims"]) == CLAIMS_SHOWN_CAP
        assert len(request["results"]) <= DISPATCH_CAP
        for shown in request["claims"]:
            assert shown["text"] == claims[shown["claim"]][:CLAIM_TEXT_CAP]
    assert [shown["claim"] for shown in first["claims"]] == list(range(CLAIMS_SHOWN_CAP))
    assert (first["unattempted"], second["unattempted"]) == (MANY, MANY - DISPATCH_CAP)
    # Then the Claims still to have an attempt, the one whose attempt decided nothing, and
    # the rest.
    in_turn = [*range(DISPATCH_CAP, MANY), 1, 0, *range(2, DISPATCH_CAP)]
    assert [shown["claim"] for shown in second["claims"]] == in_turn[:CLAIMS_SHOWN_CAP]
    assert [len(given.evidence) for given in gathered] == [2, 0, *[2] * (MANY - 2)]


# What the supervisor cannot do


def test_verdict_and_gate_the_supervisor_writes_in_its_finish_are_not_read(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor({"Adds tax": writes("fixed"), "Keeps": writes("broken")})
    states = {"verdicts": ["refuted", "confirmed"], "gate": "pass"}
    supervising = ScriptedModel([dispatch(0, 1), json.dumps({"finish": states})])

    report = verify(shop, roles, supervising, tmp_path)

    assert decided(report) == [CONFIRMS, REFUTES]
    assert report.gate.decision is GateDecision.BLOCK


REPRODUCTION_FOR_THE_FIRST = {"claim": 0, "strategy": "reproduction"}


@pytest.mark.parametrize(
    "response",
    [
        "I would rather not.",
        "[]",
        "{}",
        json.dumps({"verdict": "confirmed", "gate": "pass"}),
        json.dumps({"dispatch": [REPRODUCTION_FOR_THE_FIRST], "finish": {}}),
        json.dumps({"dispatch": [REPRODUCTION_FOR_THE_FIRST], "verdict": "confirmed"}),
        json.dumps({"dispatch": [REPRODUCTION_FOR_THE_FIRST | {"verdict": "confirmed"}]}),
        json.dumps({"dispatch": REPRODUCTION_FOR_THE_FIRST}),
        '{"dispatch": [{"claim": 0.0, "strategy": "reproduction"}]}',
        json.dumps({"move": {"from": 0, "to": 1, "steps": 1, "verdict": "confirmed"}}),
        '{"move": {"from": 0, "to": 1, "steps": 1.0}}',
        '{"move": {"from": 0.0, "to": 1, "steps": 1}}',
        json.dumps({"move": {"to": 1, "steps": 1}}),
        '{"move": {"from": 0, "to": 1, "seconds": true}}',
        '{"move": {"from": 0, "to": 1, "seconds": NaN}}',
        '{"move": {"from": 0, "to": 1, "dollars": Infinity}}',
        json.dumps({"finish": "confirmed"}),
    ],
)
def test_response_that_is_no_request_the_supervisor_may_make_does_nothing_and_is_told_so(
    shop: FixtureRepository, tmp_path: Path, response: str
) -> None:
    roles = WritesFor(THREE)
    supervising = ScriptedModel([response, FINISH, FINISH])

    report = verify(shop, roles, supervising, tmp_path, step_cap=3)

    first, second, third = requests_of(supervising)
    assert (second["refused"], second["results"]) == (SUPERVISOR_UNREADABLE, [])
    assert (second["claims"], second["unattempted"]) == (first["claims"], 3)
    # Only the refusal of the step before is told.
    assert third["refused"] == FINISH_REFUSED.format(unattempted=3)
    assert decided(report) == DECIDED


FIVE = {f"Claim {place}": writes("fixed") for place in range(DISPATCH_CAP + 1)}


@pytest.mark.parametrize(
    "response",
    [
        dispatch(),
        dispatch(*range(DISPATCH_CAP + 1)),
        dispatch(0, 0),
        dispatch(0, len(FIVE)),
        dispatch(0, -1),
        dispatch(0, 1, reading=[1]),
        json.dumps({"dispatch": [{"claim": 0, "strategy": "benchmark"}]}),
    ],
    ids=[
        "none",
        "more than the cap",
        "one Claim twice",
        "a place past the last Claim",
        "a place before the first",
        "a strategy not open to the Claim",
        "a strategy there is none of",
    ],
)
def test_dispatch_that_is_refused_runs_nothing_for_any_claim_it_names(
    shop: FixtureRepository, tmp_path: Path, response: str
) -> None:
    roles = WritesFor(FIVE)
    supervising = ScriptedModel([response, FINISH])

    report = verify(shop, roles, supervising, tmp_path, step_cap=2)

    first, second = requests_of(supervising)
    assert second["refused"] == DISPATCH_REFUSED.format(cap=DISPATCH_CAP)
    assert (second["results"], second["unattempted"]) == ([], len(FIVE))
    assert second["claims"] == first["claims"]
    assert decided(report) == [CONFIRMS] * len(FIVE)


def test_claim_is_tried_again_only_while_no_evidence_has_been_gathered_for_it(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor(THREE)
    supervising = ScriptedModel([dispatch(0, 1), dispatch(1), dispatch(0), dispatch(2), FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    requests = requests_of(supervising)
    again = {shown["claim"]: shown for shown in requests[2]["claims"]}
    assert (again[1]["attempts"], again[1]["strategies"]) == (2, ["reproduction"])
    assert (again[0]["attempts"], again[0]["strategies"]) == (1, [])
    assert requests[2]["refused"] is None
    assert requests[3]["refused"] == DISPATCH_REFUSED.format(cap=DISPATCH_CAP)
    assert decided(report) == DECIDED
    assert [len(claim.evidence) for claim in report.claims] == [1, 0, 1]


def test_finish_is_refused_until_every_claim_has_had_a_verification_attempt(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor(THREE)
    supervising = ScriptedModel([FINISH, dispatch(0), FINISH, dispatch(1, 2), FINISH])

    report = verify(shop, roles, supervising, tmp_path)

    assert [request["refused"] for request in requests_of(supervising)] == [
        None,
        FINISH_REFUSED.format(unattempted=3),
        None,
        FINISH_REFUSED.format(unattempted=2),
        None,
    ]
    assert decided(report) == DECIDED


def test_claim_no_reproduction_can_be_run_for_is_stranded_and_nothing_can_be_dispatched_for_it(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    roles = WritesFor({"Adds tax": writes("fixed"), RENAMES: NAMES_BOTH})
    supervising = ScriptedModel(
        [dispatch(0), move(0, 1, steps=1), dispatch(1, reading=[1]), FINISH]
    )

    report = verify(
        accounts,
        roles,
        supervising,
        tmp_path / "state",
        "behavioural",
        "structural",
        discover=Discovery(Unsupported(NOT_FOUND)),
    )

    first, second, third, _ = requests_of(supervising)
    assert first["unattempted"] == 1
    assert [(shown["claim"], shown["kind"], shown["strategies"]) for shown in first["claims"]] == [
        (1, "structural", ["reading"]),
        (0, "behavioural", []),
    ]
    assert second["refused"] == DISPATCH_REFUSED.format(cap=DISPATCH_CAP)
    # What a stranded Claim can never use may be moved to a Claim that can.
    assert third["refused"] is None
    had, has = left_of_each(second), left_of_each(third)
    assert (has[0]["steps"], has[1]["steps"]) == (had[0]["steps"] - 1, had[1]["steps"] + 1)
    assert decided(report) == [
        (Verdict.INCONCLUSIVE, NOT_FOUND),
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
    ]
    assert roles.tasks("Adds tax") == []


def test_supervisor_is_never_asked_when_every_claim_is_stranded(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    supervising = ScriptedModel([])

    report = verify(
        shop, WritesFor(THREE), supervising, tmp_path, discover=Discovery(Unsupported(NOT_FOUND))
    )

    assert decided(report) == [(Verdict.INCONCLUSIVE, NOT_FOUND)] * 3
    assert supervising.requests == []


def test_with_no_claims_nothing_is_done_and_the_ledger_is_not_used(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    sandboxes, discovery, supervising = Sandboxes(shop), Discovery(commands()), ScriptedModel([])
    orchestration = Supervisor(a_pipeline(tmp_path, sandboxes, discovery), model=supervising)
    ledger = Ledger(clock=Clock())

    gathered = gather_within(orchestration, ledger, shop, WritesFor({}))

    assert (gathered, discovery.asked, sandboxes.opened) == ([], [], [])
    assert supervising.requests == []
    ledger.share_out(1)  # No share had been set aside.


def test_gather_keeps_to_a_budget_with_the_provisional_defaults_and_is_told_no_kinds(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor({"Adds tax": writes("fixed")})
    supervising = ScriptedModel([dispatch(0), FINISH])
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising)

    (gathered,) = orchestration.gather(
        roles.claims, repository=shop.path, before=shop.before, after=shop.after, model=roles
    )

    first = requests_of(supervising)[0]
    assert isinstance(gathered.evidence[0], AcceptedReproduction)
    assert first["claims"][0]["kind"] is None
    assert (first["budget"]["dollars"], first["budget"]["steps"]) == (
        DEFAULT_DOLLARS,
        DEFAULT_STEPS,
    )
    assert first["budget"]["seconds"] == pytest.approx(DEFAULT_SECONDS, abs=60.0)


# The step cap and the Budget, kept from outside


def test_supervisor_that_never_finishes_is_stopped_at_its_step_cap_and_no_claim_loses_its_attempt(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, sandboxes = WritesFor(THREE), Sandboxes(shop)
    more_steps = json.dumps({"finish": {"step_cap": 99, "supervisor_steps_left": 99}})
    supervising = ScriptedModel([FINISH, more_steps, FINISH])

    report = verify(shop, roles, supervising, tmp_path, sandboxes=sandboxes, step_cap=3)

    # Asked a fourth time, the stand-in would raise.
    steps_left = [request["supervisor_steps_left"] for request in requests_of(supervising)]
    assert steps_left == [2, 1, 0]
    assert decided(report) == DECIDED
    # Code gave each Claim its attempt one at a time, in the order of the Claims.
    worked_on = [json.loads(request.data)["task"]["claim"] for request in roles.requests[1:]]
    assert worked_on == sorted(worked_on, key=roles.claims.index)
    assert len(sandboxes.opened) == 1


def test_supervising_model_that_fails_is_asked_nothing_more_and_no_claim_loses_its_attempt(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, supervising = WritesFor(THREE), FailsAfter(dispatch(1))

    report = verify(shop, roles, supervising, tmp_path)

    assert decided(report) == DECIDED
    assert len(supervising.requests) == 2
    # Code gave the Claims it had left untried their attempts, one at a time and in order.
    worked_on = [json.loads(request.data)["task"]["claim"] for request in roles.requests[1:]]
    assert [claim for claim, _ in groupby(worked_on)] == ["Rounds", "Adds tax", "Keeps"]


def test_response_that_comes_once_the_budget_is_spent_is_not_acted_on_and_nothing_more_is_asked(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, sandboxes = WritesFor(THREE), Sandboxes(shop)
    # Listing the Claims is one step, and each step of the supervisor is one more.
    supervising = ScriptedModel(["I would rather not.", dispatch(0, 1, 2)])

    report = verify(shop, roles, supervising, tmp_path, sandboxes=sandboxes, budget=Budget(steps=3))

    assert decided(report) == [(Verdict.INCONCLUSIVE, BUDGET_SPENT.format(limit="steps"))] * 3
    assert report.gate.decision is GateDecision.PASS
    spend = report.budget_spend
    assert isinstance(spend, PullRequestSpend)
    assert (spend.steps, spend.stopped_by) == (3, BudgetLimit.STEPS)
    assert (len(roles.requests), len(supervising.requests)) == (1, 2)
    assert sandboxes.opened and all(sandbox.closed for sandbox in sandboxes.opened)


def test_supervisor_is_never_asked_when_the_budget_is_spent_before_its_first_step(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, supervising = WritesFor(THREE), ScriptedModel([])
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising)
    ledger = Ledger(Budget(dollars=1.0), clock=Clock())
    ledger.record(BudgetSpend(seconds=0.0, dollars=1.0, steps=0))

    gathered = gather_within(orchestration, ledger, shop, roles)

    assert gathered == [Gathered((), BUDGET_SPENT.format(limit="money"))] * 3
    assert (roles.requests, supervising.requests) == ([], [])


@pytest.mark.parametrize(
    ("step_cap", "kinds"),
    [(0, None), (-1, None), (2, ()), (2, (ClaimKind.BEHAVIOURAL,) * 4)],
    ids=["no steps", "fewer than no steps", "no kinds", "too many kinds"],
)
def test_supervisor_that_could_not_be_run_as_given_is_refused_before_anything_else_is_done(
    shop: FixtureRepository,
    tmp_path: Path,
    step_cap: int,
    kinds: tuple[ClaimKind | None, ...] | None,
) -> None:
    roles, sandboxes, discovery = WritesFor(THREE), Sandboxes(shop), Discovery(commands())
    supervising = ScriptedModel([])
    orchestration = Supervisor(
        a_pipeline(tmp_path, sandboxes, discovery), model=supervising, step_cap=step_cap
    )

    with pytest.raises(ValueError):
        gather_within(orchestration, Ledger(clock=Clock()), shop, roles, kinds)

    assert (roles.requests, supervising.requests) == ([], [])
    assert (sandboxes.opened, discovery.asked) == ([], [])


# Several roles at once


def test_dispatches_of_one_step_run_at_the_same_time_each_reproduction_in_a_sandbox_of_its_own(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    sandboxes = Sandboxes(accounts)
    roles = MeetsFirst(
        {RENAMES: NAMES_BOTH, "Adds tax": writes("fixed"), "Keeps": writes("broken")}
    )
    supervising = ScriptedModel([dispatch(2, 0, 1, reading=[0]), FINISH])

    report = verify(
        accounts,
        roles,
        supervising,
        tmp_path / "state",
        "structural",
        "behavioural",
        "behavioural",
        sandboxes=sandboxes,
    )

    # Had they been worked on one after another, the first would have waited in vain.
    assert decided(report) == [(Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED), CONFIRMS, REFUTES]
    assert all(len(threads) == 1 for threads in roles.threads.values())
    worked_on = set().union(*roles.threads.values())
    assert len(worked_on) == 3 and threading.get_ident() not in worked_on
    # The results come back as summaries, in the order they were dispatched in.
    assert requests_of(supervising)[1]["results"] == [
        result(2, 1, *REFUTES),
        result(0, 2, Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED, "reading"),
        result(1, 1, *CONFIRMS),
    ]
    # The first Reproduction listed has the Sandbox made ready before, and the next a further
    # one, with both commits prepared in it.
    assert [written_in(sandbox) for sandbox in sandboxes.opened] == [{"broken"}, {"fixed"}]
    for sandbox in sandboxes.opened:
        assert FETCH in [command for command, *_ in sandbox.runs]
        assert sandbox.closed


def test_sandbox_made_ready_is_used_again_in_later_steps(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, sandboxes = WritesFor(THREE | {"Charges": writes("fixed")}), Sandboxes(shop)
    supervising = ScriptedModel([dispatch(0), dispatch(1, 2), dispatch(3, 1), FINISH])

    verify(shop, roles, supervising, tmp_path, sandboxes=sandboxes)

    assert [written_in(sandbox) for sandbox in sandboxes.opened] == [
        {"fixed", "same"},
        {"broken", "same"},
    ]
    assert all(sandbox.closed for sandbox in sandboxes.opened)


def test_fault_in_one_dispatch_is_raised_once_the_others_have_ended_and_every_sandbox_is_closed(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles, sandboxes = WritesFor({"Adds tax": Fault(), "Keeps": writes("broken")}), Sandboxes(shop)
    supervising = ScriptedModel([dispatch(0, 1)])

    with pytest.raises(Fault):
        verify(shop, roles, supervising, tmp_path, sandboxes=sandboxes)

    assert any("reproduction" in task for task in roles.tasks("Keeps"))
    assert len(sandboxes.opened) == 2 and all(sandbox.closed for sandbox in sandboxes.opened)


def test_what_dispatches_at_work_together_record_is_under_the_run_and_not_under_one_another(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    roles = MeetsFirst(
        {RENAMES: NAMES_BOTH, "Adds tax": writes("fixed"), "Keeps": writes("broken")}
    )
    supervising = ScriptedModel([dispatch(0, 1, 2, reading=[0]), FINISH])

    with record() as recording:
        verify(accounts, roles, supervising, tmp_path / "state", "structural", *["behavioural"] * 2)

    spans = recording.trace().spans
    (the_run,) = [span for span in spans if span.kind is SpanKind.RUN]
    of_roles = [span for span in spans if span.kind is SpanKind.ROLE]
    assert len(of_roles) > 3
    assert {span.parent_id for span in of_roles} == {the_run.span_id}


# Moving shares of the Budget


def test_supervisor_moves_what_a_claim_has_left_after_its_attempt_and_the_totals_do_not_change(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    roles = WritesFor(THREE)
    supervising = ScriptedModel(
        [dispatch(0), move(0, 1, seconds=10.0, steps=5), dispatch(1, 2), FINISH]
    )
    ledger = Ledger(Budget(seconds=90.0, dollars=3.0, steps=300), clock=Clock())
    orchestration = Supervisor(a_pipeline(tmp_path, Sandboxes(shop)), model=supervising)

    gather_within(orchestration, ledger, shop, roles)

    _, before, after, _ = requests_of(supervising)
    tried = 100 - len(roles.tasks("Adds tax"))
    assert left_of_each(before) == {
        0: {"seconds": 30.0, "dollars": 1.0, "steps": tried},
        1: {"seconds": 30.0, "dollars": 1.0, "steps": 100},
        2: {"seconds": 30.0, "dollars": 1.0, "steps": 100},
    }
    assert left_of_each(after) == {
        0: {"seconds": 20.0, "dollars": 1.0, "steps": tried - 5},
        1: {"seconds": 40.0, "dollars": 1.0, "steps": 105},
        2: {"seconds": 30.0, "dollars": 1.0, "steps": 100},
    }
    assert (after["refused"], after["results"]) == (None, [])
    # Moving is no spend: only the supervisor's own step came out of the Budget.
    assert after["budget"] == before["budget"] | {"steps": before["budget"]["steps"] - 1}
    shares = [
        ledger.spent(claim=place).steps + ledger.left(claim=place).steps for place in range(3)
    ]
    assert shares == [95, 105, 100]


@pytest.mark.parametrize(
    "response",
    [
        move(0, 1, steps=500),
        move(0, 1, seconds=DEFAULT_SECONDS),
        move(1, 0, steps=1),
        move(0, 0, steps=1),
        move(0, 3, steps=1),
        move(0, 1),
        move(0, 1, seconds=0.0, dollars=0.0, steps=0),
        move(0, 1, seconds=-1.0, steps=1),
    ],
    ids=[
        "more steps than are left",
        "more seconds than are left",
        "from a Claim still to have its attempt",
        "to the same Claim",
        "to no Claim",
        "no amounts",
        "nothing",
        "an amount under zero",
    ],
)
def test_move_that_is_refused_moves_nothing(
    shop: FixtureRepository, tmp_path: Path, response: str
) -> None:
    supervising = ScriptedModel([dispatch(0), response, FINISH])

    report = verify(shop, WritesFor(THREE), supervising, tmp_path, step_cap=3)

    _, before, after = requests_of(supervising)
    assert after["refused"] == MOVE_REFUSED
    assert left_of_each(after) == left_of_each(before)
    assert decided(report) == DECIDED


# Whatever the supervisor says

REPRODUCTIONS = {
    "fixed": Verdict.CONFIRMED,
    "broken": Verdict.REFUTED,
    "same": Verdict.INCONCLUSIVE,
}
"""The Verdict that running each Reproduction the stand-in writes leads to."""

PLACES = st.integers(-1, 3)
STATED = st.sampled_from([*(stated.value for stated in Verdict), "pass", "block"])
SAID = st.one_of(
    st.text(max_size=30),
    st.lists(
        st.fixed_dictionaries(
            {"claim": PLACES, "strategy": st.sampled_from(["reproduction", "reading", "refuted"])}
        ),
        max_size=DISPATCH_CAP + 1,
    ).map(lambda dispatches: json.dumps({"dispatch": dispatches})),
    st.fixed_dictionaries(
        {"from": PLACES, "to": PLACES},
        optional={"seconds": st.floats(-1, 400), "steps": st.integers(-1, 400)},
    ).map(lambda moved: json.dumps({"move": moved})),
    st.dictionaries(
        st.sampled_from(["verdict", "verdicts", "gate"]), STATED | st.lists(STATED, max_size=3)
    ).map(lambda states: json.dumps({"finish": states})),
    st.dictionaries(
        st.sampled_from(["verdict", "gate", "finish", "dispatch"]), STATED, min_size=1
    ).map(json.dumps),
)
"""A response of the supervisor: a request it may make, well formed or not, a Verdict or a
Gate stated outright, or text that is nothing at all."""


@settings(max_examples=40, deadline=None)
@given(
    written=st.lists(st.sampled_from(sorted(REPRODUCTIONS)), min_size=1, max_size=3),
    said=st.lists(SAID, min_size=1, max_size=6),
)
def test_whatever_the_supervisor_says_every_verdict_and_the_gate_are_what_code_computes(
    shop: FixtureRepository, written: list[str], said: list[str]
) -> None:
    roles = WritesFor({f"Claim {place}": writes(text) for place, text in enumerate(written)})

    with tempfile.TemporaryDirectory() as state:
        report = verify(shop, roles, ScriptedModel(said), Path(state), step_cap=len(said))

    verdicts = [claim.verdict for claim in report.claims]
    assert verdicts == [compute_verdict(claim.evidence).verdict for claim in report.claims]
    assert report.gate == compute_gate(verdicts)
    # Every Claim had its attempt, so each is decided by what its own Reproduction did.
    assert verdicts == [REPRODUCTIONS[text] for text in written]
