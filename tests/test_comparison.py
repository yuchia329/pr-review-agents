"""Runs of the evaluation under a configuration, and evaluations shown side by side.

The cases are run through the core's entry point with stand-ins for the models. Their
repository has nothing of any Ecosystem in it, so no Sandbox is opened.
"""

import json
import tempfile
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_configuration import KEYS, Endpoints
from test_evaluation import a_run, case_runs
from test_pipeline import NAMES_BOTH, RENAMES, WritesFor, renaming
from test_structural import looks
from test_supervisor import FINISH, dispatch

from pr_verification.agent import PROGRESS, PROGRESS_INTERVAL
from pr_verification.budget import Budget
from pr_verification.comparison import BASELINE_LABEL, Comparison, evaluate_configured, label_of
from pr_verification.configuration import (
    CEILING,
    DEFAULT,
    SELF_HOSTED,
    Configuration,
    InvalidConfiguration,
)
from pr_verification.core import PullRequest
from pr_verification.decision import YES, ScriptedDecider
from pr_verification.evaluation import (
    Case,
    CaseRun,
    Evaluation,
    Part,
    PartEvaluation,
    Scores,
    score,
)
from pr_verification.model import (
    ModelFailed,
    ModelRequest,
    RoleModels,
    RoleName,
    ScriptedModel,
)
from pr_verification.orchestration import OrchestrationChoice
from pr_verification.report import Verdict

CONFIRMED, REFUTED, INCONCLUSIVE = Verdict.CONFIRMED, Verdict.REFUTED, Verdict.INCONCLUSIVE
PIPELINE, SUPERVISOR = OrchestrationChoice.PIPELINE, OrchestrationChoice.SUPERVISOR
SUPERVISED = replace(CEILING, orchestration=SUPERVISOR)
"""A configuration whose own orchestration is the supervisor."""


def a_rename(tmp_path: Path) -> Case:
    """A case whose pull request makes one true Claim, which reading alone can verify."""
    accounts = renaming(tmp_path / "repository")
    pull_request = PullRequest(accounts.path, accounts.before, accounts.after, "Rename")
    return Case("rename", Part.DEVELOPMENT, pull_request, CONFIRMED)


# What a run is called


def test_run_is_called_by_its_configuration_and_its_orchestration() -> None:
    assert label_of(DEFAULT, PIPELINE) == "default/pipeline"
    assert label_of(CEILING, SUPERVISOR) == "ceiling/supervisor"


@given(name=st.text(), orchestration=st.sampled_from(list(OrchestrationChoice)))
def test_for_any_configuration_its_run_is_called_by_its_name_then_the_orchestration(
    name: str, orchestration: OrchestrationChoice
) -> None:
    assert label_of(replace(DEFAULT, name=name), orchestration) == f"{name}/{orchestration.value}"


# Running cases under a configuration


@pytest.mark.parametrize(
    ("configuration", "orchestration", "supervised"),
    [
        (CEILING, None, False),
        (CEILING, SUPERVISOR, True),
        (SUPERVISED, None, True),
        (SUPERVISED, PIPELINE, False),
    ],
    ids=[
        "the configuration's pipeline",
        "the supervisor in its place",
        "the configuration's supervisor",
        "the pipeline in its place",
    ],
)
def test_cases_are_run_with_the_models_brought_under_the_orchestration_given_or_the_configurations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    configuration: Configuration,
    orchestration: OrchestrationChoice | None,
    supervised: bool,
) -> None:
    case = a_rename(tmp_path)
    (tmp_path / "temporary").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temporary"))
    supervising = ScriptedModel([dispatch(0, reading=[0]), FINISH] * 2)
    models = RoleModels(WritesFor({RENAMES: NAMES_BOTH}), {RoleName.SUPERVISOR: supervising})

    evaluation = evaluate_configured(
        [case],
        configuration,
        repetitions=2,
        environment={},
        orchestration=orchestration,
        decider=ScriptedDecider(["structural"] * 2),
        models=models,
    )

    assert evaluation.repetitions == 2
    assert [run.verdicts for run in evaluation.development.runs] == [(CONFIRMED,)] * 2
    assert len(supervising.requests) == (4 if supervised else 0)
    # Each run was on a temporary directory of its own, and none is left.
    assert list((tmp_path / "temporary").iterdir()) == []


@pytest.mark.parametrize("orchestration", list(OrchestrationChoice))
def test_decider_given_classifies_the_claims_and_watches_each_role_as_its_progress_guard(
    tmp_path: Path, orchestration: OrchestrationChoice
) -> None:
    listing = json.dumps({"claims": [RENAMES]})
    reads = ScriptedModel([listing, *looks(PROGRESS_INTERVAL), NAMES_BOTH])
    supervising = ScriptedModel([dispatch(0, reading=[0]), FINISH])
    decider = ScriptedDecider(["structural", YES])

    evaluation = evaluate_configured(
        [a_rename(tmp_path)],
        DEFAULT,
        repetitions=1,
        orchestration=orchestration,
        decider=decider,
        models=RoleModels(reads, {RoleName.SUPERVISOR: supervising}),
    )

    (run,) = evaluation.development.runs
    assert run.verdicts == (CONFIRMED,)
    assert [question for _, question in decider.asked][1:] == [PROGRESS]


class SeesTemporary(WritesFor):
    """A `WritesFor` that records, at each request, what the temporary directory holds, with
    the requests of each run apart: a run starts when the Claim extractor is asked."""

    def __init__(self, temporary: Path) -> None:
        super().__init__({RENAMES: NAMES_BOTH})
        self._temporary = temporary
        self.seen: list[set[str]] = []

    def complete(self, request: ModelRequest) -> str:
        if "task" not in json.loads(request.data):
            self.seen.append(set())
        self.seen[-1].update(entry.name for entry in self._temporary.iterdir())
        return super().complete(request)


@pytest.mark.parametrize("orchestration", list(OrchestrationChoice))
def test_each_run_has_a_temporary_directory_of_its_own_that_is_gone_when_the_next_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, orchestration: OrchestrationChoice
) -> None:
    case = a_rename(tmp_path)
    (tmp_path / "temporary").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temporary"))
    models = SeesTemporary(tmp_path / "temporary")
    supervising = ScriptedModel([dispatch(0, reading=[0]), FINISH] * 2)

    evaluate_configured(
        [case],
        DEFAULT,
        repetitions=2,
        orchestration=orchestration,
        decider=ScriptedDecider(["structural"] * 2),
        models=RoleModels(models, {RoleName.SUPERVISOR: supervising}),
    )

    first, second = models.seen
    assert first and second and not first & second
    assert list((tmp_path / "temporary").iterdir()) == []


def test_budget_given_is_the_budget_each_case_is_run_within(tmp_path: Path) -> None:
    evaluation = evaluate_configured(
        [a_rename(tmp_path)],
        DEFAULT,
        repetitions=1,
        decider=ScriptedDecider(["structural"]),
        budget=Budget(steps=1),
        models=WritesFor({RENAMES: NAMES_BOTH}),
    )

    # Listing the Claims took the one step, so the Claim was left unresolved.
    (run,) = evaluation.development.runs
    assert run.verdicts == (INCONCLUSIVE,)
    assert run.spend is not None and run.spend.steps == 1


def test_models_are_those_the_configuration_names_made_from_the_environment_given(
    tmp_path: Path,
) -> None:
    endpoints = Endpoints()

    evaluation = evaluate_configured(
        [a_rename(tmp_path)],
        SELF_HOSTED,
        repetitions=2,
        environment=KEYS,
        transport=endpoints.transport,
    )

    assert [run.no_report for run in evaluation.development.runs] == [None, None]
    assert [str(sent.url) for sent in endpoints.requests] == [
        "http://localhost:11434/v1/chat/completions"
    ] * 2
    assert not any("Authorization" in sent.headers for sent in endpoints.requests)


def test_with_no_environment_given_the_models_are_made_from_the_processes_own(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    endpoints = Endpoints()
    monkeypatch.setenv("PR_VERIFICATION_API_KEY", "minimax-key")

    evaluate_configured([a_rename(tmp_path)], DEFAULT, repetitions=1, transport=endpoints.transport)

    (sent,) = endpoints.requests
    assert sent.headers["Authorization"] == "Bearer minimax-key"


@pytest.mark.parametrize(
    ("environment", "raised"),
    [({}, InvalidConfiguration), (KEYS | {"PR_VERIFICATION_INPUT_PRICE": "cheap"}, ModelFailed)],
    ids=["no key", "a price that is no number"],
)
def test_configuration_that_cannot_be_made_into_models_is_raised_and_no_case_is_run(
    tmp_path: Path, environment: dict[str, str], raised: type[Exception]
) -> None:
    endpoints = Endpoints()

    with pytest.raises(raised):
        evaluate_configured(
            [a_rename(tmp_path)],
            DEFAULT,
            repetitions=1,
            environment=environment,
            transport=endpoints.transport,
        )

    assert endpoints.requests == []


def test_evaluation_that_cannot_be_scored_is_refused_as_the_harness_refuses_it(
    tmp_path: Path,
) -> None:
    models = WritesFor({RENAMES: NAMES_BOTH})

    with pytest.raises(ValueError):
        evaluate_configured([a_rename(tmp_path)], DEFAULT, repetitions=0, models=models)

    assert models.requests == []


# Side by side

SHOWN = (
    "false_block_rate",
    "false_assurance_rate",
    "decided_rate",
    "seconds_per_pull_request",
    "dollars_per_pull_request",
    "claims",
    "runs",
    "runs_without_verdicts",
)


def holds(member: dict[str, object], scores: Scores) -> bool:
    """Whether a member of a printed comparison holds each number shown, from `scores`."""
    return {name: member.get(name, "absent") for name in SHOWN} == {
        name: getattr(scores, name) for name in SHOWN
    }


def measured(development: Iterable[CaseRun], held_out: Iterable[CaseRun] = ()) -> Evaluation:
    return Evaluation(1, PartEvaluation(tuple(development)), PartEvaluation(tuple(held_out)))


SYSTEM = measured(
    [a_run(CONFIRMED, CONFIRMED, name="a"), a_run(REFUTED, REFUTED, name="b")],
    [a_run(CONFIRMED, REFUTED, name="c")],
)
STRONGER = measured([a_run(CONFIRMED, INCONCLUSIVE, name="a")])
READING = measured([a_run(REFUTED, CONFIRMED, name="b")], [a_run(CONFIRMED, name="c", spend=None)])


def test_comparison_shows_each_evaluation_under_its_name_in_order_and_the_baseline_last() -> None:
    named = {label_of(DEFAULT, PIPELINE): SYSTEM, label_of(CEILING, SUPERVISOR): STRONGER}

    printed = json.loads(Comparison(named, baseline=READING).to_json())
    without = json.loads(Comparison(named).to_json())

    assert set(printed) == set(without) == {"development", "held_out"}
    shown = ["default/pipeline", "ceiling/supervisor", BASELINE_LABEL]
    for part in ("development", "held_out"):
        assert list(printed[part]) == shown
        assert list(without[part]) == shown[:2]
        for name, evaluation in zip(shown, (SYSTEM, STRONGER, READING), strict=True):
            assert holds(printed[part][name], getattr(evaluation, part).scores)
    # No run of the stronger model's held-out part: nothing to take a share or a mean of.
    assert printed["held_out"]["ceiling/supervisor"]["decided_rate"] is None
    assert printed["held_out"][BASELINE_LABEL]["seconds_per_pull_request"] is None


@given(
    development=case_runs(st.sampled_from(list(Verdict))),
    held_out=case_runs(st.sampled_from(list(Verdict))),
)
def test_for_any_runs_each_part_shows_its_own_scores_and_the_two_are_never_added_together(
    development: list[CaseRun], held_out: list[CaseRun]
) -> None:
    printed = json.loads(
        Comparison({"default/pipeline": measured(development, held_out)}).to_json()
    )

    assert holds(printed["development"]["default/pipeline"], score(development))
    assert holds(printed["held_out"]["default/pipeline"], score(held_out))


@pytest.mark.parametrize("baseline", [None, READING], ids=["with no baseline", "with a baseline"])
def test_no_other_evaluation_may_be_called_the_baseline(baseline: Evaluation | None) -> None:
    with pytest.raises(ValueError):
        Comparison({"default/pipeline": SYSTEM, BASELINE_LABEL: STRONGER}, baseline).to_json()
