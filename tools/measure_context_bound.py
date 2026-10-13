"""What the bound on what a role is shown costs and saves: the largest request sent to the
model, and the time, the money, and the Decided Rate of a pull request.

A measurement, not a test. It runs the development part of the hand-made cases and of the
Structural cases through the core's entry point, with the model and the decider the
environment configures, within the limits the environment configures, and prints as JSON what
the harness scored and how large the requests to the model were:

    uv run --extra laya python tools/measure_context_bound.py --clones ~/.cache/pr-verification

The model is configured as `docs/model-endpoints.md` describes, the decider as
`docs/decision-layer.md` does, and the limits as `docs/context-bound.md` does. `--clones` is
the directory `tools/structural_cases.py` clones its repositories into.

Run once with the limits to be measured, and once with both set far above anything a role
reads, as 1000000000 is, which is how a run went before there was a bound: nothing is cut.

A request's size is the characters of its instructions and its data together, counted as the
limits count them. It is not a count of tokens.
"""

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from structural_cases import built, clone_of, load

from pr_verification.context_bound import limits_from_environment, shown_within
from pr_verification.core import PullRequest, run
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import Case, Part, evaluate, hand_made_cases
from pr_verification.model import Model, ModelRequest, PricedModel, PricedResponse
from pr_verification.openai_compatible import model_from_environment
from pr_verification.report import Report


class Sized:
    """A model that passes on what it is asked, and keeps the size of each request."""

    def __init__(self, model: Model) -> None:
        self._model = model
        self.sizes: list[int] = []
        """The characters of each request made, in the order made."""

    def complete(self, request: ModelRequest) -> str:
        self.sizes.append(len(request.instructions) + len(request.data))
        return self._model.complete(request)


class SizedAndPriced(Sized):
    """The same, over a model that says what each request cost."""

    def __init__(self, model: PricedModel) -> None:
        super().__init__(model)
        self._priced = model

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        self.sizes.append(len(request.instructions) + len(request.data))
        return self._priced.complete_priced(request)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--clones", type=Path, required=True)
    parser.add_argument("--repetitions", type=int, default=1)
    arguments = parser.parse_args()

    configured = model_from_environment(os.environ)
    if configured is None:
        parser.error("no model is configured: see docs/model-endpoints.md")
    priced = isinstance(configured, PricedModel)
    sized = SizedAndPriced(configured) if priced else Sized(configured)
    # The decider asks through the same model, so what it sends is sized as well.
    decider = configured_decider(decision_configuration(os.environ), sized)
    limits = limits_from_environment(os.environ)
    names: dict[Path, str] = {}
    requests: list[dict[str, Any]] = []

    def sized_run(pull_request: PullRequest, model: Model) -> Report:
        made_before = len(sized.sizes)
        try:
            return run(pull_request, model, decider)
        finally:
            # Kept also when the run gave no report: its requests were made all the same.
            made = sized.sizes[made_before:]
            requests.append(
                {
                    "case": names[pull_request.repository],
                    "requests": len(made),
                    "largest": max(made, default=0),
                }
            )

    with tempfile.TemporaryDirectory() as directory:
        hand_made = Path(directory) / "hand-made"
        hand_made.mkdir()
        cases: list[Case] = [
            case for case in hand_made_cases(hand_made) if case.part is Part.DEVELOPMENT
        ]
        cases += [
            built(case, clone_of(case.repository, arguments.clones), Path(directory))
            for case in load()
            if case.part is Part.DEVELOPMENT
        ]
        names.update({case.pull_request.repository: case.name for case in cases})
        with shown_within(limits):
            evaluation = evaluate(
                cases, sized, repetitions=arguments.repetitions, entry_point=sized_run
            )

    print(
        json.dumps(
            {
                "limits": {
                    "tool_result": limits.tool_result,
                    "sandbox_output": limits.sandbox_output,
                },
                "largest_request": max(sized.sizes, default=0),
                "development": json.loads(evaluation.to_json())["development"],
                "runs": [
                    {
                        **made,
                        "expected": case_run.expected,
                        "verdicts": list(case_run.verdicts),
                        "no_report": case_run.no_report,
                    }
                    for made, case_run in zip(requests, evaluation.development.runs, strict=True)
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
