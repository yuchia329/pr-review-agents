"""Whether the supervisor's speed comes from its model's scheduling or from working on Claims at
the same time.

A measurement, not a test. Each call runs one case once, in the real Sandbox, under one arm:

- `pipeline`, the fixed pipeline, one Claim at a time.
- `fixed-dispatch`, the supervisor with `FixedDispatch` in place of its model: the same threads,
  Sandboxes, and Budget, and a rule that dispatches every untried Claim.
- `supervisor`, the supervisor as it is, with a model scheduling.

One call for each run lets a queue take the arms in turn and pick up where it stopped:

    uv run python tools/measure_fixed_dispatch.py --out DIRECTORY --workload c --case 0 \\
        --repetition 1 --arm fixed-dispatch \\
        --every-role qwen-remote http://HOST:8000/v1 Qwen/Qwen3.8-27B-FP8 QWEN_REMOTE_KEY

`--workload c` is the pull request of `tools/measure_claims_together.py`, and `--workload a` the
development part of the Structural cases, cloned under `--clones`. A run writes
`<arm>.rep<repetition>.<case>.json`, what it gave and spent; `.run`, the same for
`--summary`; and `.dispatches`, one line for each answer the supervising model or the rule gave.

`--summary DIRECTORY` prints the arms side by side from what is there, as
`tools/compare_configurations.py` prints them.

The runs recorded in `docs/model-comparisons.md` were made at `fixed-dispatch-base`, the commit
ADR 0009's comparison ran.
"""

import argparse
import json
import os
import pickle
import statistics
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

from compare_configurations import clock_summary, every_role
from measure_claims_together import FOUR_FIXES
from structural_cases import built, clone_of, load

from pr_verification.budget import budget_from_environment
from pr_verification.comparison import Comparison
from pr_verification.configuration import configuration_named, configured_models
from pr_verification.context_bound import limits_from_environment, shown_within
from pr_verification.core import PullRequest, run
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import CaseRun, Evaluation, Part, PartEvaluation, evaluate
from pr_verification.model import (
    Model,
    ModelRequest,
    PricedModel,
    PricedResponse,
    RoleName,
    model_for,
)
from pr_verification.orchestration import Orchestration, fixed_pipeline
from pr_verification.report import Report
from pr_verification.supervisor import DISPATCH_CAP, supervisor

ARMS = ("pipeline", "fixed-dispatch", "supervisor")


class FixedDispatch:
    """A rule in the supervising model's place: dispatch every untried Claim with its first open
    strategy, up to `DISPATCH_CAP` at once, and finish when none is left. It never retries a
    Claim and never moves a share of the Budget."""

    def complete(self, request: ModelRequest) -> str:
        state = json.loads(request.data)
        untried = [
            claim for claim in state["claims"] if claim["attempts"] == 0 and claim["strategies"]
        ]
        if not untried:
            return '{"finish": {}}'
        return json.dumps(
            {
                "dispatch": [
                    {"claim": claim["claim"], "strategy": claim["strategies"][0]}
                    for claim in untried[:DISPATCH_CAP]
                ]
            }
        )


class Logged:
    """`model`, with each of its answers written to `log` as one line."""

    def __init__(self, model: Model, log: Path) -> None:
        self._model = model
        self._log = log
        self._started = time.monotonic()

    def _kept(self, request: ModelRequest, response: str, asked_at: float) -> None:
        state = json.loads(request.data)
        line = {
            "at": round(asked_at - self._started, 3),
            "answered_in": round(time.monotonic() - asked_at, 3),
            "unattempted": state["unattempted"],
            "claims": [
                {key: claim[key] for key in ("claim", "attempts", "verdict")}
                for claim in state["claims"]
            ],
            "response": response,
        }
        with self._log.open("a") as log:
            log.write(json.dumps(line) + "\n")

    def complete(self, request: ModelRequest) -> str:
        asked_at = time.monotonic()
        response: str = self._model.complete(request)
        self._kept(request, response, asked_at)
        return response


class LoggedPriced(Logged):
    """`Logged`, for a `PricedModel`, so that the Ledger still reads what each request cost."""

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        asked_at = time.monotonic()
        assert isinstance(self._model, PricedModel)
        response = self._model.complete_priced(request)
        self._kept(request, response.response, asked_at)
        return response


def logged(model: Model, log: Path) -> Model:
    """`model`, writing its answers to `log`, and still a `PricedModel` if it was one."""
    return LoggedPriced(model, log) if isinstance(model, PricedModel) else Logged(model, log)


def one_run(arguments: argparse.Namespace) -> None:
    """Run one case once under one arm and write what came of it under `--out`."""
    if arguments.every_role is not None:
        configuration = every_role(*arguments.every_role)
    else:
        configuration = configuration_named(arguments.configuration)
    models = configured_models(configuration, os.environ)
    decider = configured_decider(decision_configuration(os.environ), models)
    budget = budget_from_environment(os.environ)
    stem = f"{arguments.arm}.rep{arguments.repetition}.{arguments.case}"

    def run_once(pull_request: PullRequest, model: Model) -> Report:
        with tempfile.TemporaryDirectory() as state:
            made: Orchestration
            if arguments.arm == "pipeline":
                made = fixed_pipeline(Path(state), progress_guard=decider)
            else:
                asked = (
                    FixedDispatch()
                    if arguments.arm == "fixed-dispatch"
                    else model_for(model, RoleName.SUPERVISOR)
                )
                made = supervisor(
                    Path(state),
                    progress_guard=decider,
                    model=logged(asked, arguments.out / f"{stem}.dispatches"),
                )
            return run(pull_request, model, decider, orchestration=made, budget=budget)

    with (
        tempfile.TemporaryDirectory() as directory,
        shown_within(limits_from_environment(os.environ)),
    ):
        if arguments.workload == "c":
            case = FOUR_FIXES.built_in(Path(directory))
        else:
            development = [each for each in load() if each.part is Part.DEVELOPMENT]
            chosen = development[arguments.case]
            case = built(chosen, clone_of(chosen.repository, arguments.clones), Path(directory))
        started = time.monotonic()
        evaluation = evaluate([case], models, repetitions=1, entry_point=run_once)
        clock_seconds = time.monotonic() - started

    (case_run,) = evaluation.development.runs + evaluation.held_out.runs
    spend = case_run.spend
    (arguments.out / f"{stem}.json").write_text(
        json.dumps(
            {
                "arm": arguments.arm,
                "case": case_run.name,
                "repetition": arguments.repetition,
                "expected": case_run.expected.value,
                "verdicts": [verdict.value for verdict in case_run.verdicts],
                "seconds": None if spend is None else round(spend.seconds, 1),
                "clock_seconds": round(clock_seconds, 1),
                "dollars": None if spend is None else round(spend.dollars, 4),
                "steps": None if spend is None else spend.steps,
                "stopped_by": getattr(spend, "stopped_by", None),
                "no_report": case_run.no_report,
            }
        )
        + "\n"
    )
    (arguments.out / f"{stem}.run").write_bytes(pickle.dumps((case_run, clock_seconds)))


def summary(directory: Path) -> None:
    """Print the arms side by side from the runs under `directory`."""
    runs: dict[str, list[CaseRun]] = defaultdict(list)
    clock: dict[str, list[float]] = defaultdict(list)
    for path in sorted(directory.glob("*.run")):
        case_run, clock_seconds = pickle.loads(path.read_bytes())
        arm = path.name.split(".")[0]
        runs[arm].append(case_run)
        clock[arm].append(clock_seconds)
    evaluations = {
        arm: Evaluation(
            repetitions=max(each.repetition for each in runs[arm]),
            development=PartEvaluation(tuple(runs[arm])),
            held_out=PartEvaluation(()),
        )
        for arm in ARMS
        if arm in runs
    }
    compared = json.loads(Comparison(evaluations).to_json())
    shown = {
        **compared,
        "runs": {arm: len(clock[arm]) for arm in evaluations},
        "clock_seconds": clock_summary(clock),
        "clock_seconds_median": {arm: statistics.median(clock[arm]) for arm in evaluations},
    }
    print(json.dumps(shown, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--summary", type=Path, metavar="DIRECTORY")
    parser.add_argument("--out", type=Path)
    parser.add_argument("--workload", choices=("a", "c"))
    parser.add_argument("--case", type=int, default=0)
    parser.add_argument("--repetition", type=int, default=1)
    parser.add_argument("--arm", choices=ARMS)
    parser.add_argument("--configuration", default="default")
    parser.add_argument("--every-role", nargs=4)
    parser.add_argument("--clones", type=Path, default=Path.home() / ".cache/pr-verification")
    arguments = parser.parse_args()
    if arguments.summary is not None:
        summary(arguments.summary)
        return
    if arguments.out is None or arguments.workload is None or arguments.arm is None:
        parser.error("a run needs --out, --workload, and --arm")
    arguments.out.mkdir(parents=True, exist_ok=True)
    one_run(arguments)
    print(f"{arguments.arm} {arguments.workload} {arguments.case} ended", file=sys.stderr)


if __name__ == "__main__":
    main()
