"""A durable worker that kills itself on a chosen step, for the targeted part of the crash
campaign.

A measurement tool, not part of the system. It is `pr-verification-durable` with the same
arguments, and runs a worker as the command does, but for runs named in the JSON file that
`PR_VERIFICATION_TARGETS` names: `{"<run>": {"kind": "git", "point": "after", "during":
10.0}, ...}`. A kind is an `EffectKind` value, or `close-sandbox` for the activity that removes
a run's Sandbox.

For a run named there, the first attempt of the first step of that kind, in any of the run's
workflows, is the one killed, by the worker making it, with SIGKILL:

- `during`: `during` seconds into the step, while the step's work is under way. A step that is
  over sooner is killed as soon as it is, which is then the point after;
- `after`: once the step's work is done and before its answer reaches Temporal, so the work is
  done and done again.

One step of a run is killed at most, whichever worker makes it: the worker that kills first
makes a file `fired/<run>` next to the targets. Before it dies it writes the kill to
`kills.jsonl` next to the targets, as `tools/measure_durability.py` writes its own, with the
kind and the point it landed at.
"""

import json
import os
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from temporalio import activity

from pr_verification.durable import driver
from pr_verification.durable.activities import Activities, Resources
from pr_verification.durable.journal import Effect

TARGETS = "PR_VERIFICATION_TARGETS"


def targeted[Result](kind: str, work: Callable[[], Result]) -> Result:
    """`work()`, unless this attempt is the one the targets say to kill this worker in."""
    info = activity.info()
    run = str(info.workflow_id).split("-claim-")[0]
    targets = Path(os.environ[TARGETS])
    target = json.loads(targets.read_text()).get(run)
    if target is None or target["kind"] != kind or info.attempt != 1:
        return work()
    try:
        # Made once only, by whichever worker comes first, so one step of a run is killed.
        os.close(os.open(targets.parent / f"fired/{run}", os.O_CREAT | os.O_EXCL))
    except FileExistsError:
        return work()

    def die(point: str) -> None:
        line = {
            "run": run,
            "workflow": info.workflow_id,
            "activity": info.activity_id,
            "attempt": info.attempt,
            "identity": f"{os.getpid()}@{socket.gethostname()}",
            "step_started": info.started_time.timestamp(),
            "at": time.time(),
            "kind": kind,
            "point": point,
        }
        with (targets.parent / "kills.jsonl").open("a") as kills:
            kills.write(json.dumps(line) + "\n")
            kills.flush()
            os.fsync(kills.fileno())
        os.kill(os.getpid(), signal.SIGKILL)

    timer = threading.Timer(target["during"], die, args=("during",))
    if target["point"] == "during":
        timer.start()
    try:
        work()
    except Exception:
        pass  # A step whose seam raised is answered with what it raised: done all the same.
    finally:
        timer.cancel()
    die("after")
    raise AssertionError("a killed worker goes no further")


def with_kills(made: Resources | None) -> Resources | None:
    """`made`, with removing a Sandbox killed as the targets say."""
    if made is None:
        return None
    remove = made.remove_sandbox
    return replace(
        made, remove_sandbox=lambda name: targeted("close-sandbox", lambda: remove(name))
    )


def main() -> None:
    answer = Activities._answer
    made = driver.worker_resources

    def answered(self: Activities, effect: Effect) -> object:
        return targeted(effect.kind.value, lambda: answer(self, effect))

    def resources(environment: Mapping[str, str], state_dir: Path) -> Any:
        return with_kills(made(environment, state_dir))

    Activities._answer = answered  # type: ignore[method-assign]
    driver.worker_resources = resources
    (Path(os.environ[TARGETS]).parent / "fired").mkdir(exist_ok=True)
    sys.exit(driver.main(sys.argv[1:]))


if __name__ == "__main__":
    main()
