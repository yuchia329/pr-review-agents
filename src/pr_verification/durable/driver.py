"""The `pr-verification-durable` command: start a worker, or verify a pull request durably.

`pr-verification` and the Action stay as they are. This command is the way into a durable run.
"""

import argparse
import asyncio
import os
import secrets
import sys
import traceback
from collections.abc import Mapping, Sequence
from pathlib import Path

from temporalio.client import Client, WorkflowFailureError

from pr_verification.budget import InvalidBudget, budget_from_environment
from pr_verification.cli import EXIT_NO_REPORT, exit_code
from pr_verification.configuration import (
    InvalidConfiguration,
    configuration_from_environment,
    configured_models,
)
from pr_verification.context_bound import InvalidContextLimits, limits_from_environment
from pr_verification.core import InvalidPullRequest, PullRequest
from pr_verification.decision import (
    InvalidDecisionConfiguration,
    configured_decider,
    decision_configuration,
)
from pr_verification.durable.activities import Resources
from pr_verification.durable.journal import RunInput
from pr_verification.durable.worker import ADDRESS, TASK_QUEUE, connect, make_worker
from pr_verification.durable.workflows import VerifyPullRequest
from pr_verification.model import Model
from pr_verification.openai_compatible import model_from_environment
from pr_verification.orchestration import fixed_pipeline
from pr_verification.report import Report

WORKER_ANSWER_WAIT = 3540.0
"""How many seconds a worker's models wait for the answer to one request: 60 seconds less than
the start-to-close timeout of a model step (`activity_options`). So a model that does not answer
in time fails in its client first, and its `ModelFailed` is the step's answer, recorded and not
tried again, where Temporal would time the step out and send the request once more."""


def worker_resources(environment: Mapping[str, str], state_dir: Path) -> Resources | None:
    """The `Resources` of a worker that `pr-verification-durable worker` runs, or None when no
    model is configured.

    They are made from `environment` as `pr-verification` makes what it runs with: the model
    or models, and the decider, as its `main` makes them, and the fixed pipeline's store and
    cache as `fixed_pipeline` makes them on `state_dir`. Every model client among them is made
    with `answer_wait` `WORKER_ANSWER_WAIT`.

    Raises what making them raises as `pr-verification` reads the environment:
    `InvalidConfiguration`, `InvalidDecisionConfiguration`, and `ModelFailed`.
    """
    configuration = configuration_from_environment(environment)
    model: Model | None
    if configuration is None:
        model = model_from_environment(environment, answer_wait=WORKER_ANSWER_WAIT)
    else:
        model = configured_models(configuration, environment, answer_wait=WORKER_ANSWER_WAIT)
    if model is None:
        return None
    decider = configured_decider(decision_configuration(environment), model)
    pipeline = fixed_pipeline(state_dir)
    return Resources(model=model, decider=decider, store=pipeline.store, cache=pipeline.cache)


async def verify(client: Client, given: RunInput, *, task_queue: str = TASK_QUEUE) -> Report:
    """Start `VerifyPullRequest` with `given`, with the workflow ID `given.run`, on
    `task_queue`, and give its report once it ends.

    Raises the `WorkflowFailureError` of a run that failed, as `VerifyPullRequest.run` says one
    can.
    """
    report = await client.execute_workflow(
        VerifyPullRequest.run, given, id=given.run, task_queue=task_queue
    )
    assert isinstance(report, Report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Run the subcommand the arguments name, and return the exit code.

    Both connect to the Temporal server that `--address` names, `ADDRESS` unless given, with
    `connect`, and use the task queue that `--task-queue` names, `TASK_QUEUE` unless given.

    `worker`
        Runs a worker (`make_worker`) until it is interrupted, then returns 0. Its `Resources`
        are `worker_resources` of the environment and the directory `--state-dir` names, which
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
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "worker":
            return _work(arguments)
        return _verify(arguments)
    except (
        InvalidPullRequest,
        InvalidBudget,
        InvalidConfiguration,
        InvalidContextLimits,
        InvalidDecisionConfiguration,
        OSError,
        UnicodeDecodeError,
        WorkflowFailureError,
    ) as error:
        cause = error.cause if isinstance(error, WorkflowFailureError) else error
        print(f"pr-verification-durable: {cause}", file=sys.stderr)
        return EXIT_NO_REPORT
    except Exception:
        traceback.print_exc()
        return EXIT_NO_REPORT


def _work(arguments: argparse.Namespace) -> int:
    resources = worker_resources(os.environ, arguments.state_dir)
    if resources is None:
        print(
            "pr-verification-durable: no model is configured, so nothing can be verified: "
            "set PR_VERIFICATION_API_KEY, or PR_VERIFICATION_ENDPOINT for a model that needs "
            "no key",
            file=sys.stderr,
        )
        return EXIT_NO_REPORT

    async def work() -> None:
        client = await connect(arguments.address)
        worker = make_worker(
            client,
            resources,
            task_queue=arguments.task_queue,
            max_concurrent_activities=arguments.max_concurrent_activities,
        )
        await worker.run()

    try:
        asyncio.run(work())
    except KeyboardInterrupt:
        pass
    return 0


def _verify(arguments: argparse.Namespace) -> int:
    given = RunInput(
        run=arguments.run or f"pr-verification-{secrets.token_hex(8)}",
        pull_request=PullRequest(
            repository=arguments.repo.absolute(),
            before=arguments.before,
            after=arguments.after,
            title=arguments.title,
            description=_read(arguments.description_file),
            linked_issue=_read(arguments.linked_issue_file),
        ),
        budget=budget_from_environment(os.environ),
        shown=limits_from_environment(os.environ),
    )

    async def verified() -> Report:
        return await verify(
            await connect(arguments.address), given, task_queue=arguments.task_queue
        )

    report = asyncio.run(verified())
    print(report.to_json(), flush=True)
    return exit_code(report.gate)


def _read(file: Path | None) -> str:
    return "" if file is None else file.read_text()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-verification-durable",
        description="Verify the Claims of a pull request in a run that survives a lost worker.",
    )
    parser.add_argument("--address", default=ADDRESS, help="the Temporal server")
    parser.add_argument("--task-queue", default=TASK_QUEUE, help="the task queue to use")
    commands = parser.add_subparsers(dest="command", required=True)
    worker = commands.add_parser("worker", help="run a worker until interrupted")
    worker.add_argument(
        "--state-dir",
        type=Path,
        required=True,
        help="directory the Repo Profile and installed dependencies are kept in between runs",
    )
    worker.add_argument("--max-concurrent-activities", type=int, default=None)
    verifying = commands.add_parser("verify", help="verify one pull request and print its report")
    verifying.add_argument("--repo", type=Path, required=True, help="path to the repository")
    verifying.add_argument("--before", required=True, help="commit before the pull request")
    verifying.add_argument("--after", required=True, help="commit after the pull request")
    verifying.add_argument("--title", required=True, help="pull request title")
    verifying.add_argument("--description-file", type=Path, help="file with the description")
    verifying.add_argument("--linked-issue-file", type=Path, help="file with the linked issue")
    verifying.add_argument("--run", help="the run's name, which is its workflow ID")
    return parser
