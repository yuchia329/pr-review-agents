"""Each Effect of a durable run, made on a worker by the activity `make_effect`."""

import dataclasses
import time
from pathlib import Path
from typing import Any

import pytest
from conftest import Clock, FixtureRepository
from durable_stand_ins import NamedSandboxes, resources
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from test_pipeline import IMAGE, WITHIN, shop

from pr_verification.claims import CLAIM_KIND
from pr_verification.decision import Decision, Question, ScriptedDecider
from pr_verification.durable.activities import LIMIT_REACHED, Activities, EffectTask
from pr_verification.durable.journal import (
    PAYLOAD_BYTES,
    Answer,
    Capabilities,
    Effect,
    EffectKind,
)
from pr_verification.model import (
    ModelFailed,
    ModelRequest,
    PricedResponse,
    RoleModels,
    RoleName,
    ScriptedModel,
)
from pr_verification.sandbox import Phase

__all__ = ["shop"]

REQUEST = ModelRequest("Write a Reproduction.", "the Claim")


def made(
    activities: Activities,
    effect: Effect,
    *,
    payload_bytes: int = PAYLOAD_BYTES,
    environment: ActivityEnvironment | None = None,
) -> Answer:
    environment = environment or ActivityEnvironment()
    return environment.run(activities.make_effect, EffectTask(7, effect, payload_bytes))


def worker_with(
    tmp_path: Path, shop: FixtureRepository, sandboxes: NamedSandboxes | None = None, **made: Any
) -> Activities:
    """The activities of a worker with the stand-ins, but for what `made` gives."""
    sandboxes = sandboxes or NamedSandboxes(shop)
    base = resources(tmp_path / "worker", ScriptedModel(["{}"]), None, sandboxes)
    return Activities(dataclasses.replace(base, **made))


def test_capabilities_are_what_the_workers_models_and_decider_offer(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    model = RoleModels(
        ScriptedModel([]),
        {
            RoleName.SUPERVISOR: ScriptedModel([], parallel_tool_calls=True),
            RoleName.REPRODUCTION_WRITER: ScriptedModel([], parallel_tool_calls=True),
        },
    )
    with_decider = worker_with(tmp_path, shop, model=model, decider=ScriptedDecider([]))
    without = worker_with(tmp_path, shop, model=model, decider=None)

    answered = made(with_decider, Effect(EffectKind.CAPABILITIES, ()))

    assert answered.value == Capabilities(
        parallel_tool_calls=tuple(
            role for role in RoleName if role in (RoleName.SUPERVISOR, RoleName.REPRODUCTION_WRITER)
        ),
        decider=True,
    )
    other = made(without, Effect(EffectKind.CAPABILITIES, ()))
    assert isinstance(other.value, Capabilities)
    assert other.value.decider is False


def test_request_to_a_model_with_a_price_is_answered_with_what_it_cost(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    priced = ScriptedModel(["the answer"], dollars_per_request=0.25)
    model = RoleModels(ScriptedModel([]), {RoleName.REPRODUCTION_JUDGE: priced})
    activities = worker_with(tmp_path, shop, model=model)

    answered = made(activities, Effect(EffectKind.MODEL, (RoleName.REPRODUCTION_JUDGE, REQUEST)))

    assert answered.value == PricedResponse("the answer", 0.25)
    assert priced.requests == [REQUEST]


class Unpriced:
    """A model with no price."""

    def __init__(self) -> None:
        self.requests: list[ModelRequest] = []

    def complete(self, request: ModelRequest) -> str:
        self.requests.append(request)
        return "free"


def test_request_to_a_model_with_no_price_and_for_no_role_costs_nothing(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    default = Unpriced()
    activities = worker_with(tmp_path, shop, model=RoleModels(default))

    answered = made(activities, Effect(EffectKind.MODEL, (None, REQUEST)))

    assert answered.value == PricedResponse("free", 0.0)
    assert default.requests == [REQUEST]


class Fails:
    def complete(self, request: ModelRequest) -> str:
        raise ModelFailed("the model could not be reached")


def test_what_a_seam_raises_is_the_answer_and_is_not_raised(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    activities = worker_with(tmp_path, shop, model=Fails())

    answered = made(activities, Effect(EffectKind.MODEL, (None, REQUEST)))

    assert isinstance(answered.raised, ModelFailed)
    assert answered.value is None


class Ticks:
    """A model that moves a controlled clock on while it answers."""

    def __init__(self, clock: Clock) -> None:
        self._clock = clock

    def complete(self, request: ModelRequest) -> str:
        self._clock.now += 4.5
        return "{}"


def test_seconds_of_an_answer_are_those_the_workers_clock_read_around_the_effect(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    clock = Clock()
    clock.now = 100.0
    activities = worker_with(tmp_path, shop, model=Ticks(clock), clock=clock)

    answered = made(activities, Effect(EffectKind.MODEL, (None, REQUEST)))

    assert answered.seconds == pytest.approx(4.5)


class Slow:
    def complete(self, request: ModelRequest) -> str:
        time.sleep(1.0)
        return "{}"


@pytest.mark.parametrize("kind", [EffectKind.MODEL, EffectKind.DECISION])
def test_activity_waiting_on_a_model_or_a_decider_heartbeats_while_it_waits(
    tmp_path: Path, shop: FixtureRepository, kind: EffectKind
) -> None:
    class SlowDecider:
        def decide(self, state: str, question: Question) -> Decision:
            time.sleep(1.0)
            return ScriptedDecider(["behavioural"]).decide(state, question)

    activities = worker_with(
        tmp_path, shop, model=Slow(), decider=SlowDecider(), heartbeat_seconds=0.1
    )
    beats: list[object] = []
    environment = ActivityEnvironment()
    environment.on_heartbeat = lambda *details: beats.append(details)
    asked = (None, REQUEST) if kind is EffectKind.MODEL else ("a Claim", CLAIM_KIND)

    made(activities, Effect(kind, asked), environment=environment)

    assert len(beats) >= 3


def test_run_in_the_sandbox_opens_the_sandbox_named_for_this_step_and_runs_once(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    sandboxes = NamedSandboxes(shop)
    activities = worker_with(tmp_path, shop, sandboxes=sandboxes)
    environment = ActivityEnvironment()
    command = ("write", "fixed")

    answered = made(
        activities,
        Effect(
            EffectKind.SANDBOX_RUN,
            ("a-sandbox", shop.path, IMAGE, command, shop.before, Phase.TEST, WITHIN),
        ),
        environment=environment,
    )

    assert answered.raised is None
    assert sandboxes.steps == [("a-sandbox", environment.info.activity_id)]
    sandbox = sandboxes.by_name["a-sandbox"]
    assert sandbox.runs == [(command, shop.before, Phase.TEST, WITHIN)]  # type: ignore[attr-defined]


def test_claim_effect_is_made_by_no_activity(tmp_path: Path, shop: FixtureRepository) -> None:
    with pytest.raises(ValueError):
        made(worker_with(tmp_path, shop), Effect(EffectKind.CLAIM, ("anything",)))


def test_answer_past_the_payload_limit_ends_the_activity_and_is_not_tried_again(
    tmp_path: Path, shop: FixtureRepository
) -> None:
    activities = worker_with(tmp_path, shop, model=ScriptedModel(["x" * 10_000]))

    with pytest.raises(ApplicationError) as raised:
        made(activities, Effect(EffectKind.MODEL, (None, REQUEST)), payload_bytes=50)

    assert raised.value.type == LIMIT_REACHED
    assert raised.value.non_retryable


def test_closing_a_sandbox_removes_it_by_its_name(tmp_path: Path, shop: FixtureRepository) -> None:
    sandboxes = NamedSandboxes(shop)
    activities = worker_with(tmp_path, shop, sandboxes=sandboxes)

    ActivityEnvironment().run(activities.close_sandbox, "a-sandbox")

    assert sandboxes.removed == ["a-sandbox"]
