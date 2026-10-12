"""The False Assurance rate on the adversarial cases.

A measurement, not a test. It runs the adversarial cases through the core's entry point, with
the model, the decider, and the Budget the environment configures, and prints as JSON what the
evaluation harness scored, the same scores for each placement of the manipulation, and every
Claim that was given Confirmed:

    uv run --extra laya python tools/evaluate_adversarial.py --repetitions 3

Only the development part is run unless `--held-out` is given, so that the held-out part is
not looked at while the system is being tuned. The model is configured as
`docs/model-endpoints.md` describes and the decider as `docs/decision-layer.md` does.

Every case's Claim is false, so every Confirmed is printed with its Claim's text, which shows
whether it was the case's own Claim or a Claim the system made of the manipulation.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

from pr_verification.adversarial_cases import PlacedIn, adversarial_cases
from pr_verification.budget import budget_from_environment
from pr_verification.core import PullRequest, run
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import CaseRun, Part, PartEvaluation, evaluate, score
from pr_verification.model import Model
from pr_verification.openai_compatible import model_from_environment
from pr_verification.report import Report, Verdict


def by_placement(part: PartEvaluation) -> dict[str, dict[str, Any]]:
    """The scores of the part's runs for each placement of the manipulation."""

    def scored(runs: list[CaseRun]) -> dict[str, Any]:
        scores = score(runs)
        return {
            "runs": scores.runs,
            "runs_without_verdicts": scores.runs_without_verdicts,
            "false_claims": scores.false_claims,
            "decided": scores.decided,
            "inconclusive": scores.inconclusive,
            "false_assurances": scores.false_assurances,
            "false_assurance_rate": scores.false_assurance_rate,
        }

    return {
        placement.value: scored(
            [case_run for case_run in part.runs if case_run.name.endswith(f"/{placement.value}")]
        )
        for placement in PlacedIn
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--held-out", action="store_true", help="run the held-out part as well")
    arguments = parser.parse_args()

    model = model_from_environment(os.environ)
    if model is None:
        parser.error("no model is configured: see docs/model-endpoints.md")
    decider = configured_decider(decision_configuration(os.environ), model)
    budget = budget_from_environment(os.environ)
    confirmed: list[dict[str, str]] = []
    names: dict[Path, str] = {}

    def recorded_run(pull_request: PullRequest, model: Model) -> Report:
        report = run(pull_request, model, decider, budget=budget)
        given = ", ".join(claim.verdict.value for claim in report.claims) or "no Claims"
        # A run can take minutes, so each is told as it ends, apart from what is measured.
        print(f"{names[pull_request.repository]}: {given}", file=sys.stderr, flush=True)
        confirmed.extend(
            {"case": names[pull_request.repository], "claim": claim.text, "reason": claim.reason}
            for claim in report.claims
            if claim.verdict is Verdict.CONFIRMED
        )
        return report

    with tempfile.TemporaryDirectory() as directory:
        cases = [
            case
            for case in adversarial_cases(Path(directory))
            if arguments.held_out or case.part is Part.DEVELOPMENT
        ]
        names.update({case.pull_request.repository: case.name for case in cases})
        evaluation = evaluate(
            cases, model, repetitions=arguments.repetitions, entry_point=recorded_run
        )

    parts = {"development": evaluation.development, "held_out": evaluation.held_out}
    measured = {
        "evaluation": json.loads(evaluation.to_json()),
        "by_placement": {name: by_placement(part) for name, part in parts.items()},
        "confirmed": confirmed,
        "no_report": [
            {"case": case_run.name, "repetition": case_run.repetition, "why": case_run.no_report}
            for part in parts.values()
            for case_run in part.runs
            if case_run.no_report is not None
        ],
    }
    print(json.dumps(measured, indent=2))


if __name__ == "__main__":
    main()
