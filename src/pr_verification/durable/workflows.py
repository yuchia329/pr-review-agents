"""The workflows of a durable run: `VerifyPullRequest`, and `VerifyClaim` for each Claim's work.

Each workflow holds its Journal and replays the core's code against it
(`pr_verification.durable.journal`): when the replay needs an Effect, the workflow has it made,
adds the answer to the Journal, and replays again. A Claim's work is a workflow of its own, so
that no one history holds the work on every Claim of a pull request.

Both are unsandboxed: the code replayed is the project's own, it is deterministic against its
Journal, and it does not touch Temporal.

Every replay is logged, so that how long one takes can be watched as a Journal grows: one
record on the logger `ACTIVATION_LOGGER`, at `INFO`, with `workflow_id`, `journal` (the number
of answers the Journal held), and `seconds` (how long the replay took, by `time.perf_counter`)
as attributes of the record.
"""

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, ChildWorkflowError

from pr_verification.durable.activities import LIMIT_REACHED, EffectTask
from pr_verification.durable.codec import encoded_size
from pr_verification.durable.journal import LIMIT_REACHED as LIMIT_REACHED_REASON
from pr_verification.durable.journal import (
    Answer,
    ClaimInput,
    ClaimOutcome,
    DurableLimits,
    Effect,
    EffectKind,
    JournalMismatch,
    NeedsEffect,
    Replay,
    RunInput,
    effect_id,
    replay_claim,
    replay_run,
)
from pr_verification.orchestration import Gathered
from pr_verification.report import Report
from pr_verification.sandbox import STOP_WITHIN_SECONDS, Limits

ACTIVATION_LOGGER = "pr_verification.durable"

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=60),
    non_retryable_error_types=["DurableLimitReached"],
)
"""How an activity is tried again: without end, but for a limit reached, since a worker can be
lost any number of times and the step in flight must still be made."""


@dataclass(frozen=True)
class ActivityOptions:
    """The timeouts of the activity that makes one Effect."""

    start_to_close: timedelta
    heartbeat: timedelta | None


def activity_options(effect: Effect) -> ActivityOptions:
    """The timeouts for making `effect`.

    - MODEL and DECISION: 3,600 seconds to finish, as long as a model was waited for in the
      measurements this design is held against, and a heartbeat every 60 seconds at most, so
      that a lost worker is found within a minute.
    - SANDBOX_RUN: the run's own `Limits.seconds`, with `STOP_WITHIN_SECONDS` and 600 seconds
      more for fetching the image and filling the working copy. No heartbeat: a run in the
      Sandbox is bounded by its limits.
    - HOME_SAVE and HOME_RESTORE: 1,800 seconds, with no heartbeat.
    - Every other kind: 300 seconds, with no heartbeat.

    Raises `ValueError` for a CLAIM Effect, which is made by a workflow and not an activity.
    """
    match effect.kind:
        case EffectKind.CLAIM:
            raise ValueError("the work on a Claim is made by a workflow, not an activity")
        case EffectKind.MODEL | EffectKind.DECISION:
            return ActivityOptions(timedelta(seconds=3600), timedelta(seconds=60))
        case EffectKind.SANDBOX_RUN:
            limits = effect.asked[6]
            assert isinstance(limits, Limits)
            seconds = limits.seconds + STOP_WITHIN_SECONDS + 600
            return ActivityOptions(timedelta(seconds=seconds), None)
        case EffectKind.HOME_SAVE | EffectKind.HOME_RESTORE:
            return ActivityOptions(timedelta(seconds=1800), None)
    return ActivityOptions(timedelta(seconds=300), None)


_SANDBOX_KINDS = (EffectKind.SANDBOX_RUN, EffectKind.HOME_SAVE, EffectKind.HOME_RESTORE)

_logged = logging.getLogger(ACTIVATION_LOGGER)


def _limit_reached(message: str) -> ApplicationError:
    return ApplicationError(message, type=LIMIT_REACHED, non_retryable=True)


async def _replayed[Given, Result](
    replay: Callable[[Given, Replay], Result],
    given: Given,
    run: str,
    limits: DurableLimits,
    journal: list[tuple[Effect, Answer]],
) -> Result:
    """What `replay` gives once the workflow has made every Effect it needs, each added to
    `journal`, within `limits`."""
    kept = 0
    while True:
        started = time.perf_counter()
        try:
            result = replay(given, Replay(journal))
        except NeedsEffect as needed:
            _log(len(journal), started)
            effect = needed.effect
            asked = encoded_size(effect)
            if asked > limits.payload_bytes:
                raise _limit_reached(
                    f"a {effect.kind.value} Effect takes {asked} bytes, past the limit of "
                    f"{limits.payload_bytes}"
                ) from None
            answer = await _made(needed.place, effect, run, limits)
            kept += asked + encoded_size(answer)
            if kept > limits.journal_bytes:
                raise _limit_reached(
                    f"the Journal takes {kept} bytes with Effect {needed.place}, past the limit "
                    f"of {limits.journal_bytes}"
                ) from None
            journal.append((effect, answer))
            continue
        except JournalMismatch:
            raise
        except Exception as raised:
            _log(len(journal), started)
            raise ApplicationError(
                str(raised), type=type(raised).__name__, non_retryable=True
            ) from None
        _log(len(journal), started)
        given_back = encoded_size(result)
        if given_back > limits.payload_bytes:
            raise _limit_reached(
                f"what the workflow gives takes {given_back} bytes, past the limit of "
                f"{limits.payload_bytes}"
            )
        return result


def _log(journal: int, started: float) -> None:
    _logged.info(
        "replayed",
        extra={
            "workflow_id": workflow.info().workflow_id,
            "journal": journal,
            "seconds": time.perf_counter() - started,
        },
    )


async def _made(place: int, effect: Effect, run: str, limits: DurableLimits) -> Answer:
    """The answer to `effect`: from a `VerifyClaim` for a CLAIM, and from `make_effect` else."""
    if effect.kind is EffectKind.CLAIM:
        (given,) = effect.asked
        assert isinstance(given, ClaimInput)
        try:
            outcome = await workflow.execute_child_workflow(
                VerifyClaim.run, given, id=f"{run}-claim-{given.place}"
            )
        except ChildWorkflowError as failed:
            cause = failed.cause
            if not (isinstance(cause, ApplicationError) and cause.type == LIMIT_REACHED):
                raise
            stopped = Gathered((), LIMIT_REACHED_REASON.format(limit=cause.message))
            outcome = ClaimOutcome(stopped, None, given.ledger)
        return Answer(outcome)
    options = activity_options(effect)
    try:
        answer = await workflow.execute_activity(
            "make_effect",
            EffectTask(place, effect, limits.payload_bytes),
            activity_id=effect_id(place, effect),
            start_to_close_timeout=options.start_to_close,
            heartbeat_timeout=options.heartbeat,
            retry_policy=RETRY,
        )
    except ActivityError as failed:
        cause = failed.cause
        if isinstance(cause, ApplicationError) and cause.type == LIMIT_REACHED:
            raise _limit_reached(cause.message) from None
        raise
    assert isinstance(answer, Answer)
    return answer


def _sandboxes_named(journal: list[tuple[Effect, Answer]]) -> list[str]:
    """Each Sandbox an Effect in `journal` names, in the order first named."""
    named: dict[str, None] = {}
    for effect, _ in journal:
        if effect.kind in _SANDBOX_KINDS:
            named[str(effect.asked[0])] = None
        elif effect.kind is EffectKind.CLAIM:
            (given,) = effect.asked
            if isinstance(given, ClaimInput) and given.ready is not None:
                named[given.ready.name] = None
    return list(named)


@workflow.defn(name="VerifyPullRequest", sandboxed=False)
class VerifyPullRequest:
    """Verifies one pull request, and gives its report."""

    @workflow.run
    async def run(self, given: RunInput) -> Report:
        """The report of `given.pull_request`: `replay_run` of `given`, against the Journal.

        Making Effects
            Each Effect the replay needs, but a CLAIM, is made by the activity `make_effect`,
            with the activity ID `effect_id(place, effect)`, the timeouts of
            `activity_options(effect)`, and `RETRY`. Each CLAIM Effect is made by the child
            workflow `VerifyClaim`, with the workflow ID `{run}-claim-{place}`, where `place`
            is the Claim's place among the Claims, and its outcome is the answer.

        Limits
            Before an Effect is made, and before the report is given, the workflow holds to
            `given.limits`. When the Effect, or the report, would take more than
            `payload_bytes` once encoded, or the Journal with the Effect and its answer more
            than `journal_bytes`, the run fails with a non-retryable `ApplicationError` of type
            `LIMIT_REACHED`, saying which limit and by how much. So a run past a limit ends,
            and is not retried without end.

            When `VerifyClaim` fails with that error, the Claim's work is over and the CLAIM
            Effect is answered with an outcome that gives the Claim no Evidence and
            `LIMIT_REACHED` (`journal`) filled in, with the ledger as it was given to the
            Claim's work. So the Claim is Inconclusive, and the other Claims are worked on as
            if nothing had happened. When it fails for any other reason, the run fails with
            it.

        Failing
            What the core's entry point raises where there is no report, and any other
            exception the replay raises but `JournalMismatch`, fails the run with a
            non-retryable `ApplicationError` whose type is the exception's class name and
            whose message is the exception's. `JournalMismatch` fails only the workflow task,
            which Temporal tries again, so a run whose code no longer matches its Journal
            waits for code that does.

        Removing the Sandboxes
            Once the run is over, however it ended, each Sandbox named by an Effect in the
            Journal is removed by the activity `close_sandbox`, one at a time and in the order
            first named.
        """
        journal: list[tuple[Effect, Answer]] = []
        try:
            return await _replayed(replay_run, given, given.run, given.limits, journal)
        finally:
            for name in _sandboxes_named(journal):
                await workflow.execute_activity(
                    "close_sandbox",
                    name,
                    start_to_close_timeout=timedelta(seconds=300),
                    retry_policy=RETRY,
                )


@workflow.defn(name="VerifyClaim", sandboxed=False)
class VerifyClaim:
    """Does the work on one Claim of a pull request."""

    @workflow.run
    async def run(self, given: ClaimInput) -> ClaimOutcome:
        """`replay_claim` of `given`, against the Journal.

        Effects are made, and limits held to, as `VerifyPullRequest.run` says. Past a limit
        the workflow fails as it says there, so that the run that asked for the work can tell
        a limit from a fault. Exceptions are failed with as it says there.

        Nothing is removed at the end: the Sandbox is the run's, and the run removes it.
        """
        return await _replayed(replay_claim, given, given.run, given.limits, [])
