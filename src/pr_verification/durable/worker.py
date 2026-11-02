"""A Temporal worker for durable runs, and the client it and the driver connect with."""

from concurrent.futures import Executor

from temporalio.client import Client
from temporalio.worker import Worker

from pr_verification.durable.activities import Resources

TASK_QUEUE = "pr-verification"
"""The task queue workflows and activities of a durable run are put on unless told another."""

ADDRESS = "localhost:7233"
"""Where the Temporal server is unless told otherwise: that of `temporal server start-dev`."""


async def connect(address: str = ADDRESS) -> Client:
    """A client of the Temporal server at `address`, with `data_converter()` of
    `pr_verification.durable.codec`, which every client of a durable run must have: one made
    with `allowed` will do."""
    raise NotImplementedError


def make_worker(
    client: Client,
    resources: Resources,
    *,
    task_queue: str = TASK_QUEUE,
    max_concurrent_activities: int | None = None,
    activity_executor: Executor | None = None,
) -> Worker:
    """A worker on `task_queue` that runs `VerifyPullRequest`, `VerifyClaim`, and the
    activities of `Activities(resources)`.

    `max_concurrent_activities` is the most Effects it makes at once, as many as Temporal's
    default when None. A measurement that kills a worker gives 1, so that a kill stops one
    run's step. Activities run on `activity_executor`, and on a thread pool as large as
    `max_concurrent_activities` when None, since they are not async.

    Raises `ValueError` when the payloads of `client` are not written as
    `pr_verification.durable.codec` writes them.
    """
    raise NotImplementedError
