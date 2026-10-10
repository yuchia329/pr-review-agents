"""Command line for the core. Exit code: 0 the Gate passes, 1 the Gate blocks, 2 no report."""

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path

from pr_verification.claims import ClaimExtractionFailed
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.model import Model, ModelFailed
from pr_verification.openai_compatible import model_from_environment
from pr_verification.report import Gate, GateDecision

EXIT_PASS = 0
EXIT_BLOCK = 1
EXIT_NO_REPORT = 2


def exit_code(gate: Gate) -> int:
    return EXIT_BLOCK if gate.decision is GateDecision.BLOCK else EXIT_PASS


def main(argv: Sequence[str] | None = None, model: Model | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
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
        )
        print(report.to_json(), flush=True)
    except (
        InvalidPullRequest,
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
    return parser
