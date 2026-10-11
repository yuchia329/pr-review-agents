"""Command line for the core. Exit code: 0 the Gate passes, 1 the Gate blocks, 2 no report.

The report is printed whole whenever there is one, whichever way the Gate goes, and the exit
code is the Gate's: 1 when, and only when, at least one Claim is Refuted.
"""

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from pr_verification.budget import Budget
from pr_verification.claims import ClaimExtractionFailed
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import (
    Decider,
    InvalidDecisionConfiguration,
    configured_decider,
    decision_configuration,
)
from pr_verification.model import Model, ModelFailed
from pr_verification.openai_compatible import model_from_environment
from pr_verification.orchestration import Orchestration, fixed_pipeline
from pr_verification.report import Gate, GateDecision

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
) -> int:
    """Verify the pull request the arguments name, print its report, and return the exit code.

    The report is `run`'s, printed to standard output as `Report.to_json` writes it. `model`,
    `decider`, `orchestration`, and `budget` are for a caller that brings its own.

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

    Without `orchestration`:

    - with `--state-dir DIRECTORY`, the orchestration is
      `fixed_pipeline(DIRECTORY, progress_guard=decider)`, with the decider that `run` is
      given, so the Repo Profile and installed dependencies are kept there between runs;
    - without it, `run` is given none, and nothing is kept between runs.

    Either way the decider answers the progress guard of the roles the fixed pipeline runs.

    Returns `exit_code` of the report's Gate. Returns `EXIT_NO_REPORT`, having printed no
    report, when there is none to give or anything at all is raised: a failure never exits
    with the code of a block.
    """
    arguments = _parser().parse_args(argv)
    if budget is not None:
        raise NotImplementedError
    try:
        if orchestration is None and arguments.state_dir is not None:
            orchestration = fixed_pipeline(arguments.state_dir)
        if model is None:
            model = model_from_environment(os.environ)
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
        )
        print(report.to_json(), flush=True)
    except (
        InvalidPullRequest,
        InvalidDecisionConfiguration,
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
    return parser
