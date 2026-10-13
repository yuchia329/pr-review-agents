"""Runs of the evaluation under a chosen configuration, shown side by side.

A measurement, like the harness it is built on (`pr_verification.evaluation`). It answers
what a configuration is worth: one model against another in the same roles, the supervisor
against the fixed pipeline, and the system against the reading-only baseline.
"""

import json
import os
import tempfile
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from pr_verification.budget import Budget
from pr_verification.configuration import Configuration, configured_models
from pr_verification.core import PullRequest, run
from pr_verification.decision import Decider
from pr_verification.evaluation import Case, Evaluation, PartEvaluation, evaluate
from pr_verification.model import Model
from pr_verification.orchestration import Orchestration, OrchestrationChoice, fixed_pipeline
from pr_verification.report import Report
from pr_verification.supervisor import supervisor

BASELINE_LABEL = "reading-only baseline"
"""What the reading-only baseline is called in a comparison. No other evaluation may be."""


def label_of(configuration: Configuration, orchestration: OrchestrationChoice) -> str:
    """What a run of `configuration` under `orchestration` is called in a comparison:
    `<name>/<orchestration>`, as in `default/pipeline` and `ceiling/supervisor`."""
    return f"{configuration.name}/{orchestration.value}"


def evaluate_configured(
    cases: Iterable[Case],
    configuration: Configuration,
    *,
    repetitions: int,
    environment: Mapping[str, str] | None = None,
    orchestration: OrchestrationChoice | None = None,
    decider: Decider | None = None,
    budget: Budget | None = None,
    models: Model | None = None,
    transport: httpx.BaseTransport | None = None,
) -> Evaluation:
    """Run `cases` through the core's entry point under `configuration`, and score them.

    The cases are run and scored as `evaluate` (`pr_verification.evaluation`) runs and
    scores them, `repetitions` times each, and what it raises is raised. What a
    configuration settles is the model each role is run on and the orchestration:

    - The models are `configured_models(configuration, environment, transport)`
      (`pr_verification.configuration`), made once, before any case is run, where
      `environment` is the process's own when None. `models` is for
      a caller that brings its own, as a test does: when given, it is used in their place,
      and neither `environment` nor `transport` is read.
    - The orchestration is `orchestration`, and `configuration.orchestration` when that is
      None. So any configuration can be run under either orchestration, and the supervisor
      measured against the fixed pipeline with the same models.

    Each run of a case is `run(case.pull_request, models, decider, orchestration=made,
    budget=budget)` (`pr_verification.core`), where `made` is made anew for that run, on a
    temporary directory of its own that is removed when the run ends:
    `fixed_pipeline(directory, progress_guard=decider)` for the fixed pipeline, and
    `supervisor(directory, progress_guard=decider)` for the supervisor. So nothing is kept
    from one run to the next, and no run is faster for what another installed.

    Raises `InvalidConfiguration` and `ModelFailed` as `configured_models` does, before any
    case is run. A configuration that is `evaluation_only` is run like any other: this is
    the evaluation.
    """
    if models is None:
        models = configured_models(
            configuration, os.environ if environment is None else environment, transport
        )
    if orchestration is None:
        orchestration = configuration.orchestration
    supervised = orchestration is OrchestrationChoice.SUPERVISOR

    def run_once(pull_request: PullRequest, model: Model) -> Report:
        with tempfile.TemporaryDirectory() as directory:
            made: Orchestration
            if supervised:
                made = supervisor(Path(directory), progress_guard=decider)
            else:
                made = fixed_pipeline(Path(directory), progress_guard=decider)
            return run(pull_request, model, decider, orchestration=made, budget=budget)

    return evaluate(cases, models, repetitions=repetitions, entry_point=run_once)


@dataclass(frozen=True)
class Comparison:
    """Several evaluations of the same cases, side by side."""

    evaluations: Mapping[str, Evaluation]
    """Each evaluation, under what it is called, in the order to show them. For a run of a
    configuration that is its `label_of`."""

    baseline: Evaluation | None = None
    """The evaluation of the reading-only baseline, when there is one: what `evaluate` gives
    with `reading_only_baseline` (`pr_verification.baseline`) as its entry point."""

    def to_json(self) -> str:
        """The comparison as a JSON object with `development` and `held_out`: the two parts
        are never added together.

        Each part is an object with one member for each of `evaluations`, under its name
        and in order, and then, when there is a `baseline`, one more under `BASELINE_LABEL`.
        So the baseline's numbers are named as the baseline's wherever they are printed,
        and come last.

        Each member holds, from the `scores` of that evaluation's part: `false_block_rate`,
        `false_assurance_rate`, `decided_rate`, `seconds_per_pull_request`, and
        `dollars_per_pull_request`, and with them `claims`, `runs`, and
        `runs_without_verdicts`, so that a rate is read beside what it is a share of. A rate
        or a mean that is None is null.

        Raises `ValueError` when one of `evaluations` is called `BASELINE_LABEL`.
        """
        if BASELINE_LABEL in self.evaluations:
            raise ValueError(f"only the reading-only baseline may be called {BASELINE_LABEL!r}")
        shown = dict(self.evaluations)
        if self.baseline is not None:
            shown[BASELINE_LABEL] = self.baseline
        printed = {
            "development": {
                name: _printed(evaluation.development) for name, evaluation in shown.items()
            },
            "held_out": {name: _printed(evaluation.held_out) for name, evaluation in shown.items()},
        }
        return json.dumps(printed, indent=2)


def _printed(part: PartEvaluation) -> dict[str, Any]:
    """What a comparison shows of one evaluation's part, as `Comparison.to_json` says."""
    scores = part.scores
    return {
        "false_block_rate": scores.false_block_rate,
        "false_assurance_rate": scores.false_assurance_rate,
        "decided_rate": scores.decided_rate,
        "seconds_per_pull_request": scores.seconds_per_pull_request,
        "dollars_per_pull_request": scores.dollars_per_pull_request,
        "claims": scores.claims,
        "runs": scores.runs,
        "runs_without_verdicts": scores.runs_without_verdicts,
    }
