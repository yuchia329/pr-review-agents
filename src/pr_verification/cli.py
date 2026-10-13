"""Command line for the core. Exit code: 0 the Gate passes, 1 the Gate blocks, 2 no report.

The report is printed whole whenever there is one, whichever way the Gate goes, and the exit
code is the Gate's: 1 when, and only when, at least one Claim is Refuted.
"""

import argparse
import os
import sys
import tempfile
import traceback
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

import httpx

from pr_verification.budget import Budget, InvalidBudget, budget_from_environment
from pr_verification.claims import ClaimExtractionFailed
from pr_verification.configuration import (
    Configuration,
    InvalidConfiguration,
    configuration_from_environment,
    configured_models,
)
from pr_verification.context_bound import (
    InvalidContextLimits,
    limits_from_environment,
    shown_within,
)
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import (
    Decider,
    InvalidDecisionConfiguration,
    configured_decider,
    decision_configuration,
)
from pr_verification.model import Model, ModelFailed
from pr_verification.openai_compatible import model_from_environment
from pr_verification.orchestration import (
    ORCHESTRATION_VARIABLE,
    InvalidOrchestration,
    Orchestration,
    OrchestrationChoice,
    fixed_pipeline,
    orchestration_choice,
)
from pr_verification.report import Gate, GateDecision
from pr_verification.supervisor import supervisor
from pr_verification.tracing import traced

EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_NO_REPORT = 2


def exit_code(gate: Gate) -> int:
    return EXIT_BLOCK if gate.decision is GateDecision.BLOCK else EXIT_PASS


def main(
    argv: Sequence[str] | None = None,
    model: Model | None = None,
    decider: Decider | None = None,
    *,
    orchestration: Orchestration | None = None,
    budget: Budget | None = None,
    transport: httpx.BaseTransport | None = None,
) -> int:
    """Verify the pull request the arguments name, print its report, and return the exit code.

    The report is `run`'s, printed to standard output as `Report.to_json` writes it. `model`,
    `decider`, `orchestration`, and `budget` are for a caller that brings its own.

    Without `model`, the environment says which models do the work.
    `configuration_from_environment(os.environ)` (`pr_verification.configuration`) reads
    `PR_VERIFICATION_CONFIGURATION`:

    - Unset, there is one model for every role, `model_from_environment(os.environ)`, and
      everything below is as it was before configurations existed.
    - Set to the name of a configuration, as `self-hosted` is, the model is
      `configured_models(configuration, os.environ)`, which has each role's work done by
      the model the configuration assigns it. `PR_VERIFICATION_ENDPOINT` and
      `PR_VERIFICATION_MODEL` are then not read. The orchestration is the configuration's,
      unless `PR_VERIFICATION_ORCHESTRATION` is set to other than the empty string, which
      then says as below. Set to the empty string it is as unset, which is how a pipeline
      passes a setting it does not have, and the configuration's orchestration is used.

    An `InvalidConfiguration` is handled as a model that is not configured is: one line on
    standard error starting `pr-verification: `, no report, and exit code 2. That is what
    comes of naming a configuration there is none for, one that only the evaluation may
    run, and a configuration whose key the environment does not hold. So the reading-only
    baseline (`pr_verification.baseline`) cannot be selected here by any setting: it is
    neither a configuration nor an orchestration.

    `transport` is for a caller that brings its own, as a test does. It is given to
    `configured_models` and to `model_from_environment`, whichever makes the models, so with
    it no connection to a model is opened. It is not read when `model` is given.

    `budget` is passed to the core's entry point. When it is None, the one the environment
    configures is used: `budget_from_environment(os.environ)`, which is the provisional
    defaults unless the environment says otherwise. So the limits are set where the pipeline
    is configured, and the printed report shows what was spent against them.

    An `InvalidBudget` is handled as a model that is not configured is: one line on standard
    error starting `pr-verification: `, no report, and exit code 2.

    `decider` is passed to the core's entry point with the pull request and the model. When
    it is None, the one the environment configures is used:
    `configured_decider(decision_configuration(os.environ), model)`, which is Laya with the
    small LLM behind it unless the environment says otherwise.

    An `InvalidDecisionConfiguration` is handled as a model that is not configured is: one
    line on standard error starting `pr-verification: `, no report, and exit code 2.

    Without `orchestration`, the environment says which is used:
    `orchestration_choice(os.environ)`, which is the fixed pipeline unless
    `PR_VERIFICATION_ORCHESTRATION` says otherwise.

    With the fixed pipeline:

    - with `--state-dir DIRECTORY`, the orchestration is
      `fixed_pipeline(DIRECTORY, progress_guard=decider)`, with the decider that `run` is
      given, so the Repo Profile and installed dependencies are kept there between runs;
    - without it, `run` is given none, and nothing is kept between runs.

    With the supervisor, the orchestration is `supervisor(DIRECTORY, progress_guard=decider)`
    (`pr_verification.supervisor`), with that decider. `DIRECTORY` is the one `--state-dir`
    names. Without `--state-dir` it is a temporary directory, removed before `main` returns,
    so nothing is kept between runs. The model that does the roles' work supervises as well.

    Either way the decider answers the progress guard of the roles that are run, and the
    report is printed and the exit code given as for the fixed pipeline: the Verdicts and the
    Gate are computed by the same code whichever orchestration gathered the Evidence.

    An `InvalidOrchestration` is handled as a model that is not configured is: one line on
    standard error starting `pr-verification: `, no report, and exit code 2.

    With `--review` the pull request is also reviewed: `run` is given `review=True`, so the
    report's `findings` are the Review's. Without it nothing is reviewed and `findings` is
    empty. Whichever it is, the exit code is the Gate's and no Finding changes it.

    Returns `exit_code` of the report's Gate. Returns `EXIT_NO_REPORT`, having printed no
    report, when there is none to give or anything at all is raised: a failure never exits
    with the code of a block.

    What a role is shown of any one thing it reads is bounded by the limits the environment
    configures: the run is made inside
    `shown_within(limits_from_environment(os.environ))` (`pr_verification.context_bound`),
    which is the defaults unless the environment says otherwise. An `InvalidContextLimits`
    is handled as a model that is not configured is: one line on standard error starting
    `pr-verification: `, no report, and exit code 2.

    All of this is done inside `traced(os.environ)` (`pr_verification.tracing`): with tracing
    configured, the run's trace is sent before `main` returns, and what is printed and
    returned is the same as with tracing off.
    """
    with traced(os.environ):
        return _main(argv, model, decider, orchestration, budget, transport)


def _main(
    argv: Sequence[str] | None,
    model: Model | None,
    decider: Decider | None,
    orchestration: Orchestration | None,
    budget: Budget | None,
    transport: httpx.BaseTransport | None,
) -> int:
    """The run that `main` traces."""
    arguments = _parser().parse_args(argv)
    configuration: Configuration | None = None
    try:
        if model is None:
            configuration = configuration_from_environment(os.environ)
            if configuration is None:
                model = model_from_environment(os.environ, transport)
            else:
                model = configured_models(configuration, os.environ, transport)
        if model is None:
            print(
                "pr-verification: no model is configured, so nothing can be verified: "
                "set PR_VERIFICATION_API_KEY, or PR_VERIFICATION_ENDPOINT for a model that "
                "needs no key",
                file=sys.stderr,
            )
            return EXIT_NO_REPORT
        if decider is None:
            decider = configured_decider(decision_configuration(os.environ), model)
        if budget is None:
            budget = budget_from_environment(os.environ)
        limits = limits_from_environment(os.environ)
        with shown_within(limits), ExitStack() as kept:
            if orchestration is None:
                orchestration = _configured_orchestration(
                    arguments.state_dir, decider, kept, configuration
                )
            report = run(
                PullRequest(
                    repository=arguments.repo,
                    before=arguments.before,
                    after=arguments.after,
                    title=arguments.title,
                    description=_read(arguments.description_file),
                    linked_issue=_read(arguments.linked_issue_file),
                ),
                model,
                decider,
                orchestration=orchestration,
                budget=budget,
                review=arguments.review,
            )
        print(report.to_json(), flush=True)
    except (
        InvalidPullRequest,
        InvalidBudget,
        InvalidConfiguration,
        InvalidContextLimits,
        InvalidDecisionConfiguration,
        InvalidOrchestration,
        ModelFailed,
        ClaimExtractionFailed,
        OSError,
        UnicodeDecodeError,
    ) as error:
        print(f"pr-verification: {error}", file=sys.stderr)
        return EXIT_NO_REPORT
    except Exception:
        # Left uncaught, Python would exit 1, and the pipeline would read a crash as a block.
        traceback.print_exc()
        return EXIT_NO_REPORT
    return exit_code(report.gate)


def _configured_orchestration(
    state_dir: Path | None, decider: Decider, kept: ExitStack, configuration: Configuration | None
) -> Orchestration | None:
    """The orchestration the environment names, or else the one `configuration` has, as
    `main` says, or None for `run` to bring its own. A temporary directory made for it is
    left for `kept` to remove."""
    if configuration is not None and not os.environ.get(ORCHESTRATION_VARIABLE):
        choice = configuration.orchestration
    else:
        choice = orchestration_choice(os.environ)
    if choice is OrchestrationChoice.SUPERVISOR:
        if state_dir is None:
            state_dir = Path(kept.enter_context(tempfile.TemporaryDirectory()))
        return supervisor(state_dir, progress_guard=decider)
    return None if state_dir is None else fixed_pipeline(state_dir, progress_guard=decider)


def _read(file: Path | None) -> str:
    return "" if file is None else file.read_text(encoding="utf-8")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pr-verification",
        description="Verify the Claims a pull request makes and print a structured report.",
    )
    parser.add_argument("--repo", type=Path, required=True, help="path to the repository")
    parser.add_argument("--before", required=True, help="commit before the pull request")
    parser.add_argument("--after", required=True, help="commit after the pull request")
    parser.add_argument("--title", required=True, help="pull request title")
    parser.add_argument("--description-file", type=Path, help="file with the description")
    parser.add_argument("--linked-issue-file", type=Path, help="file with the linked issue text")
    parser.add_argument(
        "--state-dir",
        type=Path,
        help="directory the Repo Profile and installed dependencies are kept in between runs",
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="also review the pull request and report Findings, which never change the Gate",
    )
    return parser
