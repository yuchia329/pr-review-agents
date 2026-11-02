"""Durable runs on Temporal's test server, which skips time, with the stand-ins behind a worker.

The test server is fetched by `temporalio` the first time, so the first test here needs the
network.
"""

import asyncio
import base64
import json
import logging
import random
import threading
from collections import Counter
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path

import pytest
from conftest import FixtureRepository
from durable_stand_ins import (
    ALLOWED,
    SCRIPTS,
    NamedSandboxes,
    a_decider,
    a_model,
    a_pull_request,
    a_repository,
    on_one_machine,
    resources,
)
from temporalio.api.enums.v1 import EventType
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer
from test_pipeline import REJECTS, Fault, WritesFor, writes
from test_replay import ClaimScript

from pr_verification import claims
from pr_verification.budget import Budget
from pr_verification.durable.activities import LIMIT_REACHED, EffectTask, Resources
from pr_verification.durable.codec import data_converter
from pr_verification.durable.driver import verify
from pr_verification.durable.journal import (
    LIMIT_REACHED as LIMIT_REACHED_REASON,
)
from pr_verification.durable.journal import (
    DurableLimits,
    Effect,
    EffectKind,
    RunInput,
    effect_id,
)
from pr_verification.durable.worker import make_worker
from pr_verification.durable.workflows import (
    ACTIVATION_LOGGER,
    ActivityOptions,
    VerifyClaim,
    VerifyPullRequest,
    activity_options,
)
from pr_verification.model import Model, ModelFailed, ModelRequest, RoleName
from pr_verification.report import Report, Verdict
from pr_verification.sandbox import STOP_WITHIN_SECONDS, Limits, Phase

QUEUE = "pr-verification-test"
RUN = "pr-verification-test-run"
HISTORIES = Path(__file__).parent / "histories"


def in_temporal[Result](test: Callable[[WorkflowEnvironment], Awaitable[Result]]) -> Result:
    """What `test` gives, run against a test server of its own."""

    async def body() -> Result:
        async with await WorkflowEnvironment.start_time_skipping(
            data_converter=data_converter(allowed=ALLOWED)
        ) as environment:
            return await test(environment)

    return asyncio.run(body())


@asynccontextmanager
async def working(client: Client, worker: Resources) -> AsyncIterator[None]:
    async with make_worker(client, worker, task_queue=QUEUE):
        yield


def durably(worker: Resources, given: RunInput) -> tuple[Report, list[WorkflowHistory]]:
    """The report of a durable run of `given` on a worker with `worker`, with the history of the
    run and of each Claim's workflow, in that order."""

    async def test(environment: WorkflowEnvironment) -> tuple[Report, list[WorkflowHistory]]:
        async with working(environment.client, worker):
            report = await verify(environment.client, given, task_queue=QUEUE)
            return report, await histories(environment.client, given.run)

    return in_temporal(test)


async def histories(client: Client, run: str) -> list[WorkflowHistory]:
    found = [await client.get_workflow_handle(run).fetch_history()]
    for child in children_of(found[0]):
        found.append(await client.get_workflow_handle(child).fetch_history())
    return found


def children_of(history: WorkflowHistory) -> list[str]:
    """The ID of each child workflow the run started, in the order started."""
    return [
        event.start_child_workflow_execution_initiated_event_attributes.workflow_id
        for event in history.events
        if event.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]


def failure_of(worker: Resources, given: RunInput) -> BaseException:
    """What a durable run of `given` failed with: the cause of its `WorkflowFailureError`."""

    async def test(environment: WorkflowEnvironment) -> BaseException:
        async with working(environment.client, worker):
            with pytest.raises(WorkflowFailureError) as failed:
                await verify(environment.client, given, task_queue=QUEUE)
        return failed.value.cause

    return in_temporal(test)


def a_worker(
    tmp_path: Path,
    repository: FixtureRepository,
    scripts: list[ClaimScript],
    model: Model | None = None,
) -> tuple[Resources, NamedSandboxes]:
    sandboxes = NamedSandboxes(repository)
    model = a_model(scripts) if model is None else model
    return resources(tmp_path / "worker", model, a_decider(scripts), sandboxes), sandboxes


# The report


@pytest.mark.parametrize("budget", [Budget(), Budget(steps=12)], ids=["default", "steps"])
def test_durable_run_gives_the_report_of_a_run_on_one_machine(
    tmp_path: Path, budget: Budget
) -> None:
    repository = a_repository(tmp_path)
    expected = on_one_machine(tmp_path / "here", repository, SCRIPTS, budget=budget)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)

    report, _ = durably(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS), budget))

    assert report == expected


def test_each_effect_is_made_by_an_activity_named_for_its_place_kind_and_what_it_asked(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)

    _, made = durably(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS)))

    converter = data_converter(allowed=ALLOWED)
    for history in made:
        places = []
        for event in history.events:
            if event.event_type != EventType.EVENT_TYPE_ACTIVITY_TASK_SCHEDULED:
                continue
            scheduled = event.activity_task_scheduled_event_attributes
            if scheduled.activity_type.name != "make_effect":
                continue
            (task,) = asyncio.run(converter.decode(scheduled.input.payloads))
            assert isinstance(task, EffectTask)
            assert scheduled.activity_id == effect_id(task.place, task.effect)
            places.append(task.place)
        assert places == sorted(places)
        assert len(set(places)) == len(places)


def test_each_claim_is_worked_on_in_a_workflow_named_for_the_run_and_its_place(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)

    _, (run, *_) = durably(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS)))

    started = [
        event.start_child_workflow_execution_initiated_event_attributes.workflow_type.name
        for event in run.events
        if event.event_type == EventType.EVENT_TYPE_START_CHILD_WORKFLOW_EXECUTION_INITIATED
    ]
    assert started == ["VerifyClaim"] * len(SCRIPTS)
    assert children_of(run) == [f"{RUN}-claim-{place}" for place in range(len(SCRIPTS))]


# A lost worker


def claim_of(request: ModelRequest) -> object:
    """The Claim of the request's task, or None for a request with no task."""
    return json.loads(request.data).get("task", {}).get("claim")


class LosesTheFirstAttempt:
    """The stand-in model, but the first request for one Claim's task is never answered by its
    first attempt: that attempt waits until it is released, as one whose worker was lost
    would never answer, and the stand-in is never asked for it. Counts every request sent."""

    def __init__(self, model: WritesFor, claim: str) -> None:
        self.model, self._claim = model, claim
        self.released = threading.Event()
        self.sent: Counter[ModelRequest] = Counter()

    def complete(self, request: ModelRequest) -> str:
        self.sent[request] += 1
        if not self.released.is_set() and claim_of(request) == self._claim:
            if sum(self.sent[r] for r in self.sent if claim_of(r) == self._claim) == 1:
                self.released.wait()
                raise ModelFailed("this attempt was lost, and its answer must never be read")
        return self.model.complete(request)


def test_step_in_flight_when_its_worker_is_lost_is_made_again_and_no_other_step_is(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path)
    expected = on_one_machine(tmp_path / "here", repository, SCRIPTS)
    model = LosesTheFirstAttempt(a_model(SCRIPTS), "Claim 1 holds")
    sandboxes = NamedSandboxes(repository)
    worker = resources(
        tmp_path / "worker", model, a_decider(SCRIPTS), sandboxes, heartbeat_seconds=3600.0
    )

    async def test(environment: WorkflowEnvironment) -> Report:
        async with working(environment.client, worker):
            try:
                started = asyncio.create_task(
                    verify(
                        environment.client,
                        RunInput(RUN, a_pull_request(repository, SCRIPTS)),
                        task_queue=QUEUE,
                    )
                )
                # Past the heartbeat timeout, so Temporal gives the lost step up for lost.
                await environment.sleep(timedelta(minutes=5))
                return await started
            finally:
                model.released.set()

    report = in_temporal(test)

    assert report == expected
    # Every request but the lost one reached the stand-in as often as it was sent.
    lost = model.sent - Counter(model.model.requests)
    assert len(lost) == 1
    ((request, times),) = lost.items()
    assert (claim_of(request), times) == ("Claim 1 holds", 1)


def test_model_that_fails_is_answered_for_and_not_asked_again(tmp_path: Path) -> None:
    repository = a_repository(tmp_path)
    failing = [
        ClaimScript(writes("fixed"), None, "behavioural"),
        ClaimScript(writes("same"), None, "behavioural"),
    ]
    responses: dict[str, str | Exception] = {
        "Claim 0 holds": writes("fixed"),
        "Claim 1 holds": ModelFailed("the model could not be reached"),
    }
    model = WritesFor(responses)
    worker, _ = a_worker(tmp_path, repository, failing, model)

    report, _ = durably(worker, RunInput(RUN, a_pull_request(repository, failing)))

    assert report.claims[1].verdict is Verdict.INCONCLUSIVE
    assert report.claims[0].verdict is not Verdict.INCONCLUSIVE
    sent = Counter(request.data for request in model.requests)
    assert set(sent.values()) == {1}


# Limits


def random_text(characters: int) -> str:
    """Text that does not compress."""
    return base64.b64encode(random.Random(characters).randbytes(characters * 3 // 4)).decode()


def with_one_large_answer(characters: int) -> list[ClaimScript]:
    return [
        ClaimScript(writes("fixed"), None, "behavioural"),
        ClaimScript(writes(random_text(characters)), None, "behavioural"),
        ClaimScript(writes("same"), REJECTS, "behavioural"),
    ]


def assert_only_claim_1_was_stopped(report: Report, expected: Report) -> None:
    assert report.claims[1].verdict is Verdict.INCONCLUSIVE
    assert report.claims[1].reason.startswith(LIMIT_REACHED_REASON.split("{limit}")[0])
    assert report.claims[1].evidence == ()
    assert [report.claims[0], report.claims[2]] == [expected.claims[0], expected.claims[2]]


def test_answer_past_the_payload_limit_leaves_its_claim_inconclusive_and_the_rest_are_worked_on(
    tmp_path: Path,
) -> None:
    scripts = with_one_large_answer(2_200_000)
    repository = a_repository(tmp_path)
    expected = on_one_machine(tmp_path / "here", repository, scripts)
    worker, _ = a_worker(tmp_path, repository, scripts)

    report, _ = durably(worker, RunInput(RUN, a_pull_request(repository, scripts)))

    assert_only_claim_1_was_stopped(report, expected)


def test_journal_past_its_limit_leaves_its_claim_inconclusive_and_the_rest_are_worked_on(
    tmp_path: Path,
) -> None:
    scripts = with_one_large_answer(1_400_000)
    repository = a_repository(tmp_path)
    expected = on_one_machine(tmp_path / "here", repository, scripts)
    worker, _ = a_worker(tmp_path, repository, scripts)
    limits = DurableLimits(journal_bytes=1_000_000)

    report, _ = durably(worker, RunInput(RUN, a_pull_request(repository, scripts), limits=limits))

    assert_only_claim_1_was_stopped(report, expected)


def test_run_past_a_limit_of_its_own_fails_and_is_not_tried_again(tmp_path: Path) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)
    limits = DurableLimits(journal_bytes=1)

    failed = failure_of(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS), limits=limits))

    assert isinstance(failed, ApplicationError)
    assert failed.type == LIMIT_REACHED
    assert failed.non_retryable


# Failing


def test_run_with_no_report_to_give_fails_with_an_error_named_for_what_was_raised(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)
    pull_request = a_pull_request(repository, SCRIPTS)
    unknown = pull_request.__class__(
        pull_request.repository, "no-such-commit", pull_request.after, pull_request.title
    )

    failed = failure_of(worker, RunInput(RUN, unknown))

    assert isinstance(failed, ApplicationError)
    assert failed.type == "InvalidPullRequest"
    assert failed.non_retryable


# Sandboxes


def first_named(sandboxes: NamedSandboxes) -> list[str]:
    return list(dict.fromkeys(name for name, _ in sandboxes.steps))


def test_every_sandbox_the_run_named_is_removed_once_it_is_over(tmp_path: Path) -> None:
    repository = a_repository(tmp_path)
    worker, sandboxes = a_worker(tmp_path, repository, SCRIPTS)

    durably(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS)))

    assert sandboxes.steps
    assert sandboxes.removed == first_named(sandboxes)


def test_every_sandbox_the_run_named_is_removed_when_the_run_fails(tmp_path: Path) -> None:
    repository = a_repository(tmp_path)
    model = WritesFor({"Claim 0 holds": writes("fixed"), "Claim 1 holds": Fault("a fault")})
    scripts = [ClaimScript("", None, "behavioural")] * 2
    worker, sandboxes = a_worker(tmp_path, repository, scripts, model)

    failure_of(worker, RunInput(RUN, a_pull_request(repository, scripts)))

    assert sandboxes.steps
    assert sandboxes.removed == first_named(sandboxes)


# Watching


def test_each_replay_is_logged_with_its_workflow_the_length_of_its_journal_and_its_seconds(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)
    caplog.set_level(logging.INFO, logger=ACTIVATION_LOGGER)

    durably(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS)))

    logged = [vars(r) for r in caplog.records if r.name == ACTIVATION_LOGGER]
    workflows = {record["workflow_id"] for record in logged}
    assert workflows == {RUN} | {f"{RUN}-claim-{place}" for place in range(len(SCRIPTS))}
    for workflow in workflows:
        lengths = [r["journal"] for r in logged if r["workflow_id"] == workflow]
        assert sorted(set(lengths)) == list(range(max(lengths) + 1))
    assert all(record["seconds"] >= 0 for record in logged)


# Replaying a history


def replayer() -> Replayer:
    return Replayer(
        workflows=[VerifyPullRequest, VerifyClaim], data_converter=data_converter(allowed=ALLOWED)
    )


def test_history_of_a_run_replays_and_fails_to_once_a_request_it_made_has_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)
    _, (run, *_) = durably(worker, RunInput(RUN, a_pull_request(repository, SCRIPTS)))

    asyncio.run(replayer().replay_workflow(run))

    monkeypatch.setattr(claims, "INSTRUCTIONS", claims.INSTRUCTIONS + " Changed.")
    with pytest.raises(Exception):  # noqa: B017 - whichever error Temporal gives for it
        asyncio.run(replayer().replay_workflow(run))


@pytest.mark.parametrize("saved", sorted(HISTORIES.glob("*.json")), ids=lambda path: path.stem)
def test_saved_history_replays_on_the_code_as_it_is(saved: Path) -> None:
    history = WorkflowHistory.from_json(saved.stem, saved.read_text())

    asyncio.run(replayer().replay_workflow(history))


# Activities and workers


def test_each_kind_of_effect_has_the_timeouts_its_work_needs() -> None:
    hour, minute = timedelta(seconds=3600), timedelta(seconds=60)
    limits = Limits(seconds=120.0, memory_bytes=2**30, processors=1.0)
    run = Effect(
        EffectKind.SANDBOX_RUN,
        ("a-sandbox", Path("r"), "image", ("run",), "commit", Phase.TEST, limits),
    )
    save = Effect(EffectKind.HOME_SAVE, ("a-sandbox", Path("r"), "image", "commit", Path("a")))

    assert activity_options(Effect(EffectKind.MODEL, (RoleName.REVIEWER, None))) == (
        ActivityOptions(hour, minute)
    )
    assert activity_options(Effect(EffectKind.DECISION, ("state", None))) == (
        ActivityOptions(hour, minute)
    )
    assert activity_options(run) == ActivityOptions(
        timedelta(seconds=120.0 + STOP_WITHIN_SECONDS + 600), None
    )
    assert activity_options(save) == ActivityOptions(timedelta(seconds=1800), None)
    assert activity_options(Effect(EffectKind.GIT, ())) == ActivityOptions(
        timedelta(seconds=300), None
    )
    with pytest.raises(ValueError):
        activity_options(Effect(EffectKind.CLAIM, ()))


def test_worker_refuses_a_client_whose_payloads_are_not_written_by_the_codec(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path)
    worker, _ = a_worker(tmp_path, repository, SCRIPTS)

    async def test(environment: WorkflowEnvironment) -> None:
        plain = Client(environment.client.service_client, namespace=environment.client.namespace)
        with pytest.raises(ValueError):
            make_worker(plain, worker, task_queue=QUEUE)

    in_temporal(test)
