"""A Temporal worker for durable runs, and the client it and the driver connect with."""

from concurrent.futures import Executor, ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from pr_verification.durable.activities import Activities, Resources
from pr_verification.durable.codec import ENCODING, data_converter
from pr_verification.durable.workflows import VerifyClaim, VerifyPullRequest

TASK_QUEUE = "pr-verification"
"""The task queue workflows and activities of a durable run are put on unless told another."""

ADDRESS = "localhost:7233"
"""Where the Temporal server is unless told otherwise: that of `temporal server start-dev`."""


async def connect(address: str = ADDRESS) -> Client:
    """A client of the Temporal server at `address`, with `data_converter()` of
    `pr_verification.durable.codec`, which every client of a durable run must have: one made
    with `allowed` will do."""
    return await Client.connect(address, data_converter=data_converter())


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
    # Temporal wraps the converter it is given, so it is asked how it writes a value.
    (written,) = client.data_converter.payload_converter.to_payloads([None])
    if written.metadata.get("encoding") != ENCODING.encode():
        raise ValueError("the client does not write payloads as a durable run's codec does")
    activities = Activities(resources)
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[VerifyPullRequest, VerifyClaim],
        activities=[activities.make_effect, activities.close_sandbox],
        activity_executor=activity_executor
        or ThreadPoolExecutor(max_workers=max_concurrent_activities or 100),
        max_concurrent_activities=max_concurrent_activities,
    )
