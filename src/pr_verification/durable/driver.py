"""The `pr-verification-durable` command: start a worker, or verify a pull request durably.

`pr-verification` and the Action stay as they are. This command is the way into a durable run.
"""

from collections.abc import Sequence

from temporalio.client import Client

from pr_verification.durable.journal import RunInput
from pr_verification.durable.worker import TASK_QUEUE
from pr_verification.report import Report


async def verify(client: Client, given: RunInput, *, task_queue: str = TASK_QUEUE) -> Report:
    """Start `VerifyPullRequest` with `given`, with the workflow ID `given.run`, on
    `task_queue`, and give its report once it ends.

    Raises the `WorkflowFailureError` of a run that failed, as `VerifyPullRequest.run` says one
    can.
    """
    raise NotImplementedError


def main(argv: Sequence[str] | None = None) -> int:
    """Run the subcommand the arguments name, and return the exit code.

    Both connect to the Temporal server that `--address` names, `ADDRESS` unless given, with
    `connect`, and use the task queue that `--task-queue` names, `TASK_QUEUE` unless given.

    `worker`
        Runs a worker (`make_worker`) until it is interrupted, then returns 0. Its `Resources`
        are made from the environment as `pr-verification` makes what it runs with: the model
        or models, and the decider, as its `main` makes them, and the fixed pipeline's store
        and cache as `fixed_pipeline` makes them on the directory `--state-dir` names, which
        is required. `--max-concurrent-activities N` is passed on as
        `max_concurrent_activities`. What `pr-verification` handles as a model that is not
        configured is handled the same: one line on standard error starting
        `pr-verification-durable: `, and exit code 2, with no worker run.

    `verify`
        Takes the arguments of `pr-verification` but `--state-dir` and `--review`: the
        state is the worker's, and the Review is not part of a durable run. The repository's
        directory is made absolute, since the workers read it. The run is `verify` with a
        `RunInput` that holds the pull request, the Budget and the context limits the
        environment configures, as `pr-verification` reads them, and as `run` the value of
        `--run`, or `pr-verification-` and 16 random hexadecimal digits when not given.

        The report is printed to standard output as `Report.to_json` writes it, and the exit
        code is `exit_code` of its Gate (`pr_verification.cli`). When the run fails there is
        no report: one line on standard error starting `pr-verification-durable: `, saying
        why, and exit code `EXIT_NO_REPORT`. So a failure never exits with the code of a
        block.
    """
    raise NotImplementedError
