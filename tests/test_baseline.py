"""The reading-only baseline: a role that reads the code and states each Verdict itself, what
it is given, and where it can be run from.

The model is always the scripted stand-in. Nothing here opens a Sandbox: the baseline has none.
"""

import json
from pathlib import Path

import pytest
from conftest import (
    Clock,
    FixtureRepository,
    UnreachableModel,
    asks,
    assert_shows_only_whole_responses,
    commit_snapshots,
    model_listing,
)
from conftest import finishes as finishes_with
from hypothesis import given, settings
from hypothesis import strategies as st
from test_budget import LocalModel
from test_github_action import (
    RecordingGitHub,
    between_the_stops,
    environment_for,
    pull_request_event,
)
from test_structural import AFTER, BEFORE, looks
from test_supervisor import FailsAfter

from pr_verification import cli, github_action
from pr_verification.agent import NO_SUCH_TOOL, UNREADABLE_RESPONSE, StepOutcome, Stopped
from pr_verification.baseline import (
    BASELINE_COULD_NOT_FINISH,
    BASELINE_DID_NOT_FINISH,
    BASELINE_GAVE_NO_VERDICT,
    BASELINE_PREFIX,
    StatedVerdict,
    reading_only_baseline,
    state_verdict,
)
from pr_verification.claims import ClaimExtractionFailed
from pr_verification.comparison import BASELINE_LABEL, Comparison
from pr_verification.configuration import CONFIGURATION_VARIABLE, CONFIGURATIONS
from pr_verification.core import InvalidPullRequest, PullRequest
from pr_verification.decision import ScriptedDecider
from pr_verification.evaluation import Case, Part, evaluate
from pr_verification.model import (
    Model,
    ModelFailed,
    RoleModels,
    RoleName,
    ScriptedModel,
    ScriptExhausted,
)
from pr_verification.orchestration import ORCHESTRATION_VARIABLE, OrchestrationChoice
from pr_verification.reading import reading_tools
from pr_verification.report import Claim, Verdict
from pr_verification.structural import VERIFIER_STEP_CAP

CLAIM = "Renames UserManager to AccountManager"
KEEPS = "Keeps the number of retries"
WHY = "The class has its new name wherever it is used."
UNREACHABLE = "the model could not be reached"
TITLE, DESCRIPTION, LINKED_ISSUE = "Rename the manager", "Retries are untouched.", "Issue 7"


@pytest.fixture(scope="module")
def accounts(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A pull request that renames a class, adds a file, and changes a setting. It is made
    once, since nothing is written to it."""
    path = tmp_path_factory.mktemp("baseline") / "repository"
    before, after = commit_snapshots(path, BEFORE, AFTER)
    return FixtureRepository(path=path, before=before, after=after)


def states(verdict: object, reason: object = WHY) -> str:
    """A response in which the baseline's role finishes by stating a Verdict."""
    return finishes_with(verdict=verdict, reason=reason)


def stated(repository: FixtureRepository, model: Model, **caps: int) -> StatedVerdict:
    return state_verdict(
        CLAIM,
        repository=repository.path,
        before=repository.before,
        after=repository.after,
        model=model,
        **caps,
    )


def the_pull_request(repository: FixtureRepository) -> PullRequest:
    return PullRequest(
        repository.path, repository.before, repository.after, TITLE, DESCRIPTION, LINKED_ISSUE
    )


# One Claim


@pytest.mark.parametrize("verdict", list(Verdict))
def test_verdict_the_role_states_is_the_claims_verdict_whatever_the_code_holds(
    accounts: FixtureRepository, verdict: Verdict
) -> None:
    model = ScriptedModel([states(verdict.value)])

    said = stated(accounts, model)

    assert (said.verdict, said.reason) == (verdict, BASELINE_PREFIX + WHY)
    assert said.run is not None
    assert (said.run.role, said.run.stopped) == ("reading-only-baseline", Stopped.FINISHED)
    assert json.loads(model.requests[0].data) == {"task": {"claim": CLAIM}, "steps": []}
    assert CLAIM not in model.requests[0].instructions


def test_role_reads_both_commits_with_the_six_tools_of_the_structural_verifier_and_has_no_other(
    accounts: FixtureRepository,
) -> None:
    arguments: dict[str, dict[str, object]] = {
        "list_files": {"directory": "accounts"},
        "search": {"text": "Manager"},
        "read_file": {"path": "accounts/manager.py"},
    }
    commits = {"before": accounts.before, "after": accounts.after}
    expected = {
        f"{commit}_{tool.name}": tool.run(arguments[tool.name])
        for commit, full in commits.items()
        for tool in reading_tools(accounts.path, full)
    }
    reads = [asks(name, **arguments[name.partition("_")[2]]) for name in expected]
    others = [
        asks("search", text="Manager"),
        asks("run_command", command="pytest"),
        asks("run_reproduction", reproduction="assert True"),
        asks("write_file", path="accounts/manager.py", text=""),
    ]
    responses = [*reads, *others, states("confirmed")]

    said = stated(accounts, ScriptedModel(responses), step_cap=len(responses))

    assert said.run is not None and len(expected) == 6
    assert [(step.outcome, step.tool, step.result) for step in said.run.steps[:6]] == [
        (StepOutcome.RAN_TOOL, name, result) for name, result in expected.items()
    ]
    assert expected["before_search"] != expected["after_search"]
    assert [(step.outcome, step.result) for step in said.run.steps[6:10]] == [
        (StepOutcome.REFUSED, NO_SUCH_TOOL)
    ] * len(others)
    assert said.verdict is Verdict.CONFIRMED


def test_role_is_told_of_each_of_its_six_tools_by_its_name_and_what_the_reading_tool_does(
    accounts: FixtureRepository,
) -> None:
    model = ScriptedModel([states("refuted")])

    stated(accounts, model)

    instructions = model.requests[0].instructions
    for which in ("before", "after"):
        for tool in reading_tools(accounts.path, accounts.after):
            # Each request the description shows is one the role can make: under its own name.
            asked_as = f'"tool": "{which}_{tool.name}"'
            described = tool.description.replace(f'"tool": "{tool.name}"', asked_as)
            assert described in instructions
    assert_shows_only_whole_responses(instructions)


@pytest.mark.parametrize(
    "answer",
    [
        finishes_with(),
        finishes_with(reason=WHY),
        finishes_with(verdict="confirmed"),
        states("Confirmed"),
        states("confirmed "),
        states("true"),
        states(True),
        states(None),
        states("confirmed", reason=7),
        states("confirmed", reason=None),
        finishes_with(texts=[{"text": "UserManager", "change": "removed"}]),
    ],
    ids=[
        "nothing",
        "no verdict",
        "no reason",
        "another letter case",
        "a space after it",
        "a word that is no verdict",
        "a truth value",
        "null for a verdict",
        "a number for a reason",
        "null for a reason",
        "texts to search for",
    ],
)
def test_answer_that_states_no_verdict_that_can_be_read_leaves_the_claim_inconclusive(
    accounts: FixtureRepository, answer: str
) -> None:
    said = stated(accounts, ScriptedModel([answer]))

    assert (said.verdict, said.reason) == (Verdict.INCONCLUSIVE, BASELINE_GAVE_NO_VERDICT)
    assert said.run is not None and said.run.stopped is Stopped.FINISHED


READS_AGAIN = [asks("after_read_file", path="docs/guide.md")] * VERIFIER_STEP_CAP


@pytest.mark.parametrize(
    ("responses", "caps", "stopped", "requests"),
    [
        (looks(VERIFIER_STEP_CAP + 2), {}, Stopped.STEP_CAP, VERIFIER_STEP_CAP),
        (looks(5), {"step_cap": 3}, Stopped.STEP_CAP, 3),
        (READS_AGAIN, {}, Stopped.LOOP, None),
    ],
    ids=["at the verifier's step cap", "at the step cap given", "for going round in circles"],
)
def test_role_that_is_stopped_before_it_finishes_leaves_the_claim_inconclusive_saying_why(
    accounts: FixtureRepository,
    responses: list[str],
    caps: dict[str, int],
    stopped: Stopped,
    requests: int | None,
) -> None:
    model = ScriptedModel(responses)

    said = stated(accounts, model, **caps)

    assert said.verdict is Verdict.INCONCLUSIVE
    assert said.reason == BASELINE_DID_NOT_FINISH.format(stopped=stopped.value)
    assert said.run is not None and said.run.stopped is stopped
    assert requests is None or len(model.requests) == requests


def test_model_that_fails_leaves_the_claim_inconclusive_with_no_run_and_is_not_raised(
    accounts: FixtureRepository,
) -> None:
    said = stated(accounts, UnreachableModel())

    reason = BASELINE_COULD_NOT_FINISH.format(failure=UNREACHABLE)
    assert said == StatedVerdict(Verdict.INCONCLUSIVE, reason, run=None)


def test_fault_in_the_model_is_raised(accounts: FixtureRepository) -> None:
    with pytest.raises(ScriptExhausted):
        stated(accounts, ScriptedModel([]))


VALUES = (
    st.sampled_from([verdict.value for verdict in Verdict])
    | st.text(max_size=8)
    | st.none()
    | st.booleans()
    | st.integers()
)


@settings(deadline=None)
@given(
    answer=st.dictionaries(
        st.sampled_from(["verdict", "reason"]) | st.text(max_size=8), VALUES, max_size=3
    )
)
def test_for_any_answer_the_reason_is_named_as_the_baselines_and_only_a_stated_verdict_is_taken(
    accounts: FixtureRepository, answer: dict[str, object]
) -> None:
    said = stated(accounts, ScriptedModel([finishes_with(**answer)]))

    verdict, reason = answer.get("verdict"), answer.get("reason")
    assert said.reason.startswith(BASELINE_PREFIX)
    if isinstance(verdict, str) and verdict in set(Verdict) and isinstance(reason, str):
        assert (said.verdict, said.reason) == (Verdict(verdict), BASELINE_PREFIX + reason)
    else:
        assert (said.verdict, said.reason) == (Verdict.INCONCLUSIVE, BASELINE_GAVE_NO_VERDICT)


# One pull request


def test_baseline_is_given_the_systems_claims_and_models_and_reports_each_verdict_plainly(
    accounts: FixtureRepository,
) -> None:
    extractor = ScriptedModel([json.dumps({"claims": [CLAIM, KEEPS]})], dollars_per_request=0.5)
    reader = ScriptedModel(
        [
            states("confirmed"),
            asks("after_search", text="retries"),
            states("refuted", "The number of retries is another."),
        ],
        dollars_per_request=0.25,
    )
    unasked = ScriptedModel([])
    roles: dict[RoleName, Model] = {
        RoleName.CLAIM_EXTRACTOR: extractor,
        RoleName.REPRODUCTION_WRITER: reader,
    }
    shortened = PullRequest(
        accounts.path, accounts.before[:12], accounts.after[:12], TITLE, DESCRIPTION, LINKED_ISSUE
    )

    report = reading_only_baseline(shortened, RoleModels(unasked, roles))

    assert report.claims == (
        Claim(CLAIM, Verdict.CONFIRMED, BASELINE_PREFIX + WHY),
        Claim(KEEPS, Verdict.REFUTED, BASELINE_PREFIX + "The number of retries is another."),
    )
    assert all(type(claim) is Claim for claim in report.claims)
    assert (report.before, report.after, report.findings) == (accounts.before, accounts.after, ())
    spend = report.budget_spend
    assert (spend.dollars, spend.steps) == (1.25, 4) and spend.seconds >= 0
    assert [json.loads(request.data) for request in extractor.requests] == [
        {"title": TITLE, "description": DESCRIPTION, "linked_issue": LINKED_ISSUE}
    ]
    assert [json.loads(request.data)["task"] for request in reader.requests] == [
        {"claim": CLAIM},
        {"claim": KEEPS},
        {"claim": KEEPS},
    ]
    assert unasked.requests == []


def test_baseline_may_ask_for_several_tools_in_one_step_of_a_model_that_offers_them(
    accounts: FixtureRepository,
) -> None:
    searches = [
        {"tool": f"{commit}_search", "arguments": {"text": "Manager"}}
        for commit in ("before", "after")
    ]
    model = ScriptedModel(
        [json.dumps({"claims": [CLAIM]}), json.dumps({"tools": searches}), states("confirmed")],
        parallel_tool_calls=True,
    )

    report = reading_only_baseline(the_pull_request(accounts), model)

    found = [
        tool.run({"text": "Manager"})
        for commit in (accounts.before, accounts.after)
        for tool in reading_tools(accounts.path, commit)
        if tool.name == "search"
    ]
    (step,) = json.loads(model.requests[2].data)["steps"]
    assert json.loads(step["result"]) == found
    assert [claim.verdict for claim in report.claims] == [Verdict.CONFIRMED]

    # And only of such a model: from one that does not offer them the same response
    # cannot be read.
    plain = ScriptedModel(
        [json.dumps({"claims": [CLAIM]}), json.dumps({"tools": searches}), states("confirmed")]
    )
    reading_only_baseline(the_pull_request(accounts), plain)
    (unread,) = json.loads(plain.requests[2].data)["steps"]
    assert unread["result"] == UNREADABLE_RESPONSE


def test_step_cap_given_bounds_the_work_on_each_claim(accounts: FixtureRepository) -> None:
    model = ScriptedModel([json.dumps({"claims": [CLAIM, KEEPS]}), *looks(9)])

    report = reading_only_baseline(the_pull_request(accounts), model, step_cap=2)

    did_not_finish = BASELINE_DID_NOT_FINISH.format(stopped="step_cap")
    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.INCONCLUSIVE, did_not_finish)
    ] * 2
    assert len(model.requests) == 1 + 2 * 2


def test_model_that_fails_on_a_claim_costs_that_claim_its_verdict_and_never_the_report(
    accounts: FixtureRepository,
) -> None:
    model = FailsAfter(json.dumps({"claims": [CLAIM, KEEPS]}), states("confirmed"))

    report = reading_only_baseline(the_pull_request(accounts), model)

    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.CONFIRMED, BASELINE_PREFIX + WHY),
        (Verdict.INCONCLUSIVE, BASELINE_COULD_NOT_FINISH.format(failure=UNREACHABLE)),
    ]
    # The request that failed counts as a step, as with the ledger.
    assert report.budget_spend.steps == 3


def test_time_the_call_took_is_read_from_the_clock_and_a_model_with_no_price_costs_nothing(
    accounts: FixtureRepository,
) -> None:
    clock = Clock()
    model = LocalModel(clock, seconds=2.5, answer=json.dumps({"claims": []}))

    report = reading_only_baseline(the_pull_request(accounts), model, clock=clock)

    assert (report.claims, len(model.requests)) == ((), 1)
    spend = report.budget_spend
    assert (spend.seconds, spend.dollars, spend.steps) == (2.5, 0.0, 1)


@pytest.mark.parametrize(
    ("before", "model", "raised"),
    [
        ("no-such-commit", ScriptedModel([]), InvalidPullRequest),
        (None, UnreachableModel(), ModelFailed),
        (None, ScriptedModel(["There are no Claims to list."]), ClaimExtractionFailed),
    ],
    ids=["a commit that is missing", "a model that fails", "no list of Claims"],
)
def test_pull_request_whose_claims_cannot_be_listed_gives_no_report_as_with_the_system(
    accounts: FixtureRepository, before: str | None, model: Model, raised: type[Exception]
) -> None:
    pull_request = PullRequest(accounts.path, before or accounts.before, accounts.after, TITLE)

    with pytest.raises(raised):
        reading_only_baseline(pull_request, model)


@pytest.mark.parametrize("step_cap", [0, -1])
def test_step_cap_under_one_is_rejected_before_the_model_is_asked(
    accounts: FixtureRepository, step_cap: int
) -> None:
    alone, listing = ScriptedModel([states("confirmed")]), model_listing(CLAIM)

    with pytest.raises(ValueError):
        stated(accounts, alone, step_cap=step_cap)
    with pytest.raises(ValueError):
        reading_only_baseline(the_pull_request(accounts), listing, step_cap=step_cap)

    assert (alone.requests, listing.requests) == ([], [])


# Where it can be run from, and what it is called


def test_harness_runs_the_baseline_as_an_entry_point_and_its_numbers_are_shown_as_the_baselines(
    accounts: FixtureRepository,
) -> None:
    case = Case("rename", Part.DEVELOPMENT, the_pull_request(accounts), Verdict.CONFIRMED)
    model = ScriptedModel([json.dumps({"claims": [CLAIM]}), states("refuted")])

    evaluation = evaluate([case], model, repetitions=1, entry_point=reading_only_baseline)

    (run,) = evaluation.development.runs
    assert (run.verdicts, run.no_report) == ((Verdict.REFUTED,), None)
    assert run.spend is not None and run.spend.steps == 2
    printed = json.loads(Comparison({}, baseline=evaluation).to_json())
    assert list(printed["development"]) == [BASELINE_LABEL]
    # A true Claim that reading alone refuted: a False Block, counted as the baseline's.
    assert printed["development"][BASELINE_LABEL]["false_block_rate"] == 1.0


def test_no_setting_of_the_command_line_tool_selects_the_baseline(
    accounts: FixtureRepository, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    arguments = ["--repo", str(accounts.path), "--before", accounts.before, "--after"]
    arguments += [accounts.after, "--title", TITLE]
    assert not {"baseline", BASELINE_LABEL} & {*CONFIGURATIONS, *OrchestrationChoice}

    for word in ("baseline", "reading-only-baseline", BASELINE_LABEL):
        model = model_listing(CLAIM)
        with monkeypatch.context() as environment:
            environment.setenv(CONFIGURATION_VARIABLE, word)
            by_configuration = cli.main(arguments), capsys.readouterr()
        with monkeypatch.context() as environment:
            environment.setenv(ORCHESTRATION_VARIABLE, word)
            by_orchestration = cli.main(arguments, model, ScriptedDecider([])), capsys.readouterr()

        for variable, (exited, printed) in (
            (CONFIGURATION_VARIABLE, by_configuration),
            (ORCHESTRATION_VARIABLE, by_orchestration),
        ):
            assert (exited, printed.out) == (2, "")
            assert printed.err.startswith("pr-verification: ") and variable in printed.err


def test_github_action_verifies_as_before_whatever_its_environment_says_of_the_baseline(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A repository of no Ecosystem, so the fixed pipeline opens no Sandbox.
    path = tmp_path / "repository"
    repository = FixtureRepository(
        path, *commit_snapshots(path, {"a.txt": "a\n"}, {"a.txt": "b\n"})
    )
    named = {CONFIGURATION_VARIABLE: "baseline", ORCHESTRATION_VARIABLE: "baseline"}
    environment = environment_for(tmp_path, repository, pull_request_event(repository), **named)
    model = model_listing(CLAIM)

    exited = github_action.main(
        environment,
        github=RecordingGitHub(),
        model=model,
        decider=ScriptedDecider(["behavioural"]),
    )

    (claim,) = json.loads(between_the_stops(capsys.readouterr().out))["claims"]
    assert (exited, claim["verdict"]) == (github_action.EXIT_DONE, "inconclusive")
    assert not claim["reason"].startswith(BASELINE_PREFIX)
    assert len(model.requests) == 1

    # With no model brought, it looks for its one model as before, and names no other: a
    # configuration that needs no key configures nothing.
    self_hosted = environment | {CONFIGURATION_VARIABLE: "self-hosted"}
    exited = github_action.main(self_hosted, github=RecordingGitHub())

    assert exited == github_action.EXIT_FAILED
    assert "no model is configured" in between_the_stops(capsys.readouterr().out)
