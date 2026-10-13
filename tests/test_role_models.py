"""A model for each role: which model each role's requests are made to, and at whose price.

Each role is given a stand-in of its own, so a request that reaches any other model shows.
"""

import json
from pathlib import Path

import pytest
from conftest import Clock, GathersNothing, UnreachableModel, finishes, model_listing
from hypothesis import given
from hypothesis import strategies as st
from test_pipeline import NAMES_BOTH, RENAMES, Sandboxes, WritesFor, a_pipeline, renaming, writes
from test_supervisor import FINISH, dispatch

from pr_verification import verdict
from pr_verification.core import PullRequest, run
from pr_verification.decision import NO, ScriptedDecider
from pr_verification.model import (
    Model,
    ModelFailed,
    ModelRequest,
    PricedResponse,
    RoleModels,
    RoleName,
    ScriptedModel,
    model_for,
)
from pr_verification.orchestration import FixedPipeline, Orchestration
from pr_verification.report import Verdict
from pr_verification.supervisor import Supervisor

REQUEST = ModelRequest(instructions="List the Claims.", data='{"title": "Tidy up"}')
ADDS_TAX = "Adds tax"
WORK = {ADDS_TAX: writes("fixed"), RENAMES: NAMES_BOTH}
"""A Claim for the Reproduction writer and its judge, and one for the Structural verifier."""


def test_asked_for_no_role_the_models_of_the_roles_answer_with_their_default() -> None:
    default, reviewer = ScriptedModel(["the default's response"]), ScriptedModel([])
    models = RoleModels(default, {RoleName.REVIEWER: reviewer})

    assert models.complete(REQUEST) == "the default's response"
    assert (default.requests, reviewer.requests) == ([REQUEST], [])
    with pytest.raises(ModelFailed, match="could not be reached"):
        RoleModels(UnreachableModel()).complete(REQUEST)


ROLES = st.sampled_from(sorted(RoleName))


@given(named=st.sets(ROLES), role=ROLES)
def test_for_any_assignment_a_roles_model_is_its_own_or_the_default_and_one_model_is_every_roles(
    named: set[RoleName], role: RoleName
) -> None:
    default = ScriptedModel([])
    own: dict[RoleName, Model] = {name: ScriptedModel([]) for name in named}

    assert model_for(RoleModels(default, own), role) is own.get(role, default)
    assert model_for(default, role) is default


class Charges:
    """A stand-in that answers as the model it is given does, says each request cost
    `dollars`, and records the requests."""

    def __init__(self, model: Model, dollars: float) -> None:
        self._model, self.dollars = model, dollars
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        return self._model.complete(request)

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        return PricedResponse(self.complete(request), self.dollars)


def asked_as(request: ModelRequest) -> RoleName:
    """The role a request was made for, read from what the role is shown."""
    data = json.loads(request.data)
    if "supervisor_steps_left" in data:
        return RoleName.SUPERVISOR
    if "task" not in data:
        return RoleName.CLAIM_EXTRACTOR
    task = data["task"]
    if "claims" in task:
        return RoleName.REVIEWER
    if "reproduction" in task:
        return RoleName.REPRODUCTION_JUDGE
    if task["claim"] == RENAMES:
        return RoleName.STRUCTURAL_VERIFIER
    return RoleName.REPRODUCTION_WRITER


def a_model_for_each_role() -> dict[RoleName, Charges]:
    """A stand-in for each role that could answer for any of them, each at a price of its
    own: small enough that no Budget is spent, and a power of two so that sums are exact."""
    supervises = ScriptedModel([dispatch(0, 1, reading=[1]), FINISH])
    reviews = ScriptedModel([finishes(findings=[])])
    answers = {RoleName.SUPERVISOR: supervises, RoleName.REVIEWER: reviews}
    return {
        role: Charges(answers.get(role, WritesFor(WORK)), 2.0 ** -(place + 6))
        for place, role in enumerate(RoleName)
    }


@pytest.mark.parametrize(
    "orchestrated", ["pipeline", "supervisor", "supervisor made with a model of its own"]
)
def test_each_roles_requests_are_made_to_that_roles_model_and_charged_at_its_price(
    tmp_path: Path, orchestrated: str
) -> None:
    accounts = renaming(tmp_path / "repository")
    pipeline: FixedPipeline = a_pipeline(tmp_path / "state", Sandboxes(accounts))
    models, default = a_model_for_each_role(), Charges(ScriptedModel([]), 1.0)
    own = Charges(ScriptedModel([dispatch(0, 1, reading=[1]), FINISH]), 2.0**-12)
    orchestration: Orchestration = pipeline
    asked_for = dict(models)
    if orchestrated == "pipeline":
        del asked_for[RoleName.SUPERVISOR]
    elif orchestrated == "supervisor":
        orchestration = Supervisor(pipeline)
    else:
        orchestration = Supervisor(pipeline, model=own)
        asked_for[RoleName.SUPERVISOR] = own

    report = run(
        PullRequest(accounts.path, accounts.before, accounts.after, "Add tax and rename"),
        RoleModels(default, models),
        ScriptedDecider(["behavioural", "structural", *[NO] * 30]),
        orchestration=orchestration,
        clock=Clock(),
        review=True,
    )

    assert [(claim.verdict, claim.reason) for claim in report.claims] == [
        (Verdict.CONFIRMED, verdict.CONFIRMED),
        (Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED),
    ]
    for role, model in asked_for.items():
        assert {asked_as(request) for request in model.requests} == {role}
    for model in (default, own, models[RoleName.SUPERVISOR]):
        if model is not asked_for.get(RoleName.SUPERVISOR):
            assert model.requests == []
    # A Review's requests are recorded nowhere, so the Reviewer's model is not in the sum.
    recorded = [model for role, model in asked_for.items() if role is not RoleName.REVIEWER]
    assert report.budget_spend.dollars == sum(
        len(model.requests) * model.dollars for model in recorded
    )
    assert report.budget_spend.steps == sum(len(model.requests) for model in recorded)


def test_another_orchestration_is_given_the_models_of_the_roles_as_they_were_given(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    models = RoleModels(ScriptedModel([]), {RoleName.CLAIM_EXTRACTOR: model_listing(ADDS_TAX)})
    gathers = GathersNothing()

    run(
        PullRequest(accounts.path, accounts.before, accounts.after, "Add tax"),
        models,
        orchestration=gathers,
    )

    ((claims, *_, given),) = gathers.calls
    assert claims == (ADDS_TAX,) and given is models


def test_supervising_model_that_holds_a_model_for_each_role_is_asked_as_its_supervisors_model(
    tmp_path: Path,
) -> None:
    accounts = renaming(tmp_path / "repository")
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(accounts))
    supervising, unasked = ScriptedModel([dispatch(0, 1, reading=[1]), FINISH]), ScriptedModel([])
    own = RoleModels(unasked, {RoleName.SUPERVISOR: supervising})

    report = run(
        PullRequest(accounts.path, accounts.before, accounts.after, "Add tax and rename"),
        WritesFor(WORK),
        ScriptedDecider(["behavioural", "structural"]),
        orchestration=Supervisor(pipeline, model=own),
        clock=Clock(),
    )

    assert [claim.verdict for claim in report.claims] == [Verdict.CONFIRMED] * 2
    assert {asked_as(request) for request in supervising.requests} == {RoleName.SUPERVISOR}
    assert unasked.requests == []
