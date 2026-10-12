"""How often the progress guard tells a role that is getting there that it is not.

A measurement, not a test. It runs the development part of the hand-made cases through the
core's entry point, with the model and the decider the environment configures, and prints as
JSON every asking of the progress guard and how the evaluation scored:

    uv run --extra laya python tools/measure_progress_guard.py --repetitions 5

The model is configured as `docs/model-endpoints.md` describes and the decider as
`docs/decision-layer.md` does. The decider answers the progress guard, as it does for the
command-line tool.

Whether a role was getting closer to its goal is not known for certain, so it is taken from
how its work ended. An asking is counted as one of a role that was getting there when it came
in the attempt of the Reproduction writer that showed a difference in behaviour: the last
attempt for a Claim that the report gives Evidence for. A trigger there is a false trigger.
The steps each asking was about are printed with it, to be read where that rule is too coarse.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pr_verification.agent import PROGRESS
from pr_verification.core import PullRequest, run
from pr_verification.decision import (
    NO,
    Decider,
    Decision,
    Question,
    configured_decider,
    decision_configuration,
)
from pr_verification.evaluation import Part, evaluate, hand_made_cases
from pr_verification.model import Model, ModelRequest
from pr_verification.openai_compatible import model_from_environment
from pr_verification.report import Report


class Watched:
    """A model and a decider that pass on what they are asked, and keep what tells one
    asking of the progress guard from another."""

    def __init__(self, model: Model, decider: Decider) -> None:
        self._model, self._decider = model, decider
        self.attempts: dict[str, int] = {}
        """For each Claim the Reproduction writer worked on, how many attempts it had made
        before the last one it started."""
        self.asked: list[tuple[dict[str, Any], Decision]] = []
        """The state and the answer of each asking of the progress guard."""

    def complete(self, request: ModelRequest) -> str:
        task = json.loads(request.data).get("task")
        if task is not None and "attempts" in task:
            self.attempts[task["claim"]] = len(task["attempts"])
        return self._model.complete(request)

    def decide(self, state: str, question: Question) -> Decision:
        decision = self._decider.decide(state, question)
        if question == PROGRESS:
            self.asked.append((json.loads(state), decision))
        return decision


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repetitions", type=int, default=1)
    arguments = parser.parse_args()

    model = model_from_environment(os.environ)
    if model is None:
        parser.error("no model is configured: see docs/model-endpoints.md")
    decider = configured_decider(decision_configuration(os.environ), model)
    askings: list[dict[str, Any]] = []
    names: dict[Path, str] = {}

    def watched_run(pull_request: PullRequest, model: Model) -> Report:
        watched = Watched(model, decider)
        report = None
        try:
            report = run(pull_request, watched, watched)
            return report
        finally:
            # Kept also when the run gave no report, where no asking can be counted.
            with_evidence = (
                {claim.text for claim in report.claims if claim.evidence} if report else ()
            )
            for state, decision in watched.asked:
                claim, earlier = state["task"]["claim"], len(state["task"]["attempts"])
                askings.append(
                    {
                        "case": names[pull_request.repository],
                        "claim": claim,
                        "attempt": earlier + 1,
                        "getting_there": None
                        if report is None
                        else claim in with_evidence and earlier == watched.attempts[claim],
                        "trigger": decision.top == NO,
                        "probabilities": dict(decision.probabilities),
                        "made_by": decision.made_by,
                        "passed_on": decision.passed_on,
                        "steps": state["steps"],
                    }
                )

    with tempfile.TemporaryDirectory() as directory:
        cases = [case for case in hand_made_cases(Path(directory)) if case.part is Part.DEVELOPMENT]
        names.update({case.pull_request.repository: case.name for case in cases})
        evaluation = evaluate(
            cases, model, repetitions=arguments.repetitions, entry_point=watched_run
        )

    getting_there = [asking for asking in askings if asking["getting_there"]]
    measured = {
        "asked": len(askings),
        "triggers": sum(asking["trigger"] for asking in askings),
        "asked_of_a_role_getting_there": len(getting_there),
        "false_triggers": sum(asking["trigger"] for asking in getting_there),
        "askings": askings,
        "evaluation": json.loads(evaluation.to_json()),
    }
    print(json.dumps(measured, indent=2))


if __name__ == "__main__":
    main()
