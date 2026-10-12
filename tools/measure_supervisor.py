"""How long one pull request with several Claims takes when the supervisor dispatches them
together, against one at a time.

A measurement, not a test. It verifies one hand-made pull request through the core's entry
point with the LLM supervisor, in the real Sandbox, once for each way of dispatching, and
prints as JSON how long each run took:

    uv run python tools/measure_supervisor.py --claims 4 --model-seconds 5 --repetitions 3

Only the dispatching differs between the two ways, so every model is a stand-in that always
answers the same. The supervising model asks for as many Claims as one step may hold, or for
one Claim in each step. The roles' model lists the Claims, writes for each the same
Reproduction, which decides it, and accepts it as the judge. `--model-seconds` is how long
each request for a role's work takes, in place of a real model's own time. A request to the
supervising model takes none.

Each run starts with nothing kept, so it installs the dependencies in every Sandbox it makes
ready: the one before the supervisor is asked, and one more for each further Reproduction of a
step. So dispatching together is measured with what it costs, and not only with what it saves.
"""

import argparse
import json
import statistics
import tempfile
import time
from pathlib import Path

from pr_verification.core import run
from pr_verification.evaluation import hand_made_cases
from pr_verification.model import ModelRequest, ScriptedModel
from pr_verification.report import PullRequestSpend
from pr_verification.supervisor import DISPATCH_CAP, supervisor

CASE = "hand-made/median-of-an-even-count"

REPRODUCTION = """\
def test_median_of_an_even_count_is_the_mean_of_the_two_middle_values():
    from statistics_tools import median

    assert median([1, 2, 3, 4]) == 2.5
"""


class Roles:
    """A stand-in for the model that does the roles' work, as the module's own text says."""

    def __init__(self, claims: int, seconds: float) -> None:
        self._claims = [
            f"median([1, 2, 3, 4]) now returns 2.5 (said {place})" for place in range(claims)
        ]
        self._seconds = seconds

    def complete(self, request: ModelRequest) -> str:
        task = json.loads(request.data).get("task")
        if task is None:
            return json.dumps({"claims": self._claims})
        time.sleep(self._seconds)
        if "reproduction" in task:
            return json.dumps({"finish": {"accepted": True}})
        return json.dumps({"finish": {"reproduction": REPRODUCTION}})


def dispatching(claims: int, together: int) -> ScriptedModel:
    """A stand-in for a supervising model that dispatches a Reproduction for every Claim,
    `together` of them in each step, and then finishes."""
    steps = [range(first, min(first + together, claims)) for first in range(0, claims, together)]
    responses = [
        {"dispatch": [{"claim": place, "strategy": "reproduction"} for place in step]}
        for step in steps
    ]
    return ScriptedModel(json.dumps(response) for response in [*responses, {"finish": {}}])


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--claims", type=int, default=DISPATCH_CAP)
    parser.add_argument("--model-seconds", type=float, default=5.0)
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()

    measured = []
    with tempfile.TemporaryDirectory() as directory:
        (case,) = [case for case in hand_made_cases(Path(directory)) if case.name == CASE]
        for repetition in range(arguments.repetitions):
            for name, together in (("together", DISPATCH_CAP), ("one at a time", 1)):
                with tempfile.TemporaryDirectory() as state:
                    orchestration = supervisor(
                        Path(state), model=dispatching(arguments.claims, together)
                    )
                    started = time.monotonic()
                    report = run(
                        case.pull_request,
                        Roles(arguments.claims, arguments.model_seconds),
                        orchestration=orchestration,
                    )
                    seconds = time.monotonic() - started
                spend = report.budget_spend
                assert isinstance(spend, PullRequestSpend)
                measured.append(
                    {
                        "dispatching": name,
                        "repetition": repetition,
                        "seconds": round(seconds, 1),
                        "seconds_of_work": round(spend.seconds, 1),
                        "stopped_by": spend.stopped_by,
                        "verdicts": [claim.verdict for claim in report.claims],
                    }
                )

    def median_seconds(name: str) -> float:
        return statistics.median(
            float(str(one["seconds"])) for one in measured if one["dispatching"] == name
        )

    print(
        json.dumps(
            {
                "claims": arguments.claims,
                "model_seconds": arguments.model_seconds,
                "median_seconds_together": median_seconds("together"),
                "median_seconds_one_at_a_time": median_seconds("one at a time"),
                "runs": measured,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
