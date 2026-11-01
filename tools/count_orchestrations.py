"""How long the two orchestrations took, counted from the logs of a side-by-side measurement.

Runs nothing. It reads the directory a side-by-side measurement of the adversarial cases left,
which holds `<orchestration>.rep<repetition>.<case>.start` and `.end` for each run, each with
the time on the clock in whole seconds, and prints as JSON what the runs took from start to end:

    uv run python tools/count_orchestrations.py \
        ~/.cache/pr-verification/measurements/issue-59/2026-10-18-orchestrations

These are the seconds of the whole run, as a person waiting for it would count them, and not
`seconds_per_pull_request` from the run's own results. A pair is the two orchestrations on one
case in one repetition. A run with no `.end` did not finish, and is left out.
"""

import argparse
import json
import statistics
from datetime import datetime
from pathlib import Path

ORCHESTRATIONS = ("pipeline", "supervisor")
WITHIN_SECONDS = 30 * 60


def on_the_clock(path: Path) -> datetime:
    """The time written in the file at `path`."""
    return datetime.fromisoformat(path.read_text().strip())


def seconds_of_runs(directory: Path, orchestration: str) -> dict[str, float]:
    """How long each finished run of `orchestration` took, by its repetition and case."""
    seconds: dict[str, float] = {}
    for end in directory.glob(f"{orchestration}.*.end"):
        pair = end.stem.removeprefix(f"{orchestration}.")
        took = on_the_clock(end) - on_the_clock(end.with_suffix(".start"))
        seconds[pair] = took.total_seconds()
    return seconds


def count(directory: Path) -> dict[str, object]:
    """What the runs in `directory` took, for each orchestration and in pairs."""
    runs = {name: seconds_of_runs(directory, name) for name in ORCHESTRATIONS}
    pipeline, supervisor = (runs[name] for name in ORCHESTRATIONS)
    pairs = pipeline.keys() & supervisor.keys()
    counted: dict[str, object] = {
        "pairs": len(pairs),
        "pipeline_shorter": sum(pipeline[pair] < supervisor[pair] for pair in pairs),
        "supervisor_shorter": sum(supervisor[pair] < pipeline[pair] for pair in pairs),
    }
    for name, seconds in runs.items():
        counted[name] = {
            "runs": len(seconds),
            "median_seconds": statistics.median(seconds.values()),
            "longest_seconds": max(seconds.values()),
            "within_30_minutes": sum(took <= WITHIN_SECONDS for took in seconds.values()),
        }
    return counted


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="the logs of the measurement")
    print(json.dumps(count(parser.parse_args().directory), indent=2))


if __name__ == "__main__":
    main()
