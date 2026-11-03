"""Whether a durable run survives losing its worker: the crash campaign, and its baseline.

A measurement, not a test. It runs the development part of the adversarial cases through
`VerifyPullRequest` on a Temporal server, with workers it starts and keeps running itself:

    temporal server start-dev --db-filename DIRECTORY/temporal.db
    uv run python tools/model_witness.py --upstream http://HOST:8000/v1 \\
        --log DIRECTORY/witness.jsonl
    PR_VERIFICATION_ENDPOINT=http://127.0.0.1:18000/v1 PR_VERIFICATION_MODEL=... \\
        PR_VERIFICATION_API_KEY=... PR_VERIFICATION_DECISION=small_llm \\
        PR_VERIFICATION_BUDGET_SECONDS=7200 \\
        caffeinate -dims uv run --extra durable python tools/measure_durability.py baseline \\
        --out DIRECTORY/baseline

`baseline` runs each case `--repetitions` times, 3 unless given, and kills nothing. `crash`
draws, as each run starts, a delay from 60 to 1,200 seconds (`--kill-after`); once it has
passed, it waits for a step of the run in flight, and kills the worker holding that step with
SIGKILL. A run counts only if the kill landed with the step in flight, which the run's history
shows: the step was made again. Runs are started until `--count` count, 180 unless given; one
that does not count is followed by another run of its case.

Stopped with Ctrl-C or SIGTERM, it stops its workers and leaves the runs waiting in Temporal.
Started again with the same `--out`, it goes on with them, and kills none a second time.

The workers are `pr-verification-durable worker` processes, `--workers` of them, 6 unless
given, each making one Effect at a time, so a kill stops one run's step. They read the model,
the decider, and how long a model is waited for from the environment, as the command does,
and share one state directory under `--out`. One that exits is started again within a second.
`--at-once` runs, 6 unless given, are in flight together. Every run's Budget and context
limits are read from the environment as `pr-verification` reads them.

Under `--out` are written:

- `settings.json`: the commit and the settings, but no key. A directory is given runs of one
  commit only, so a campaign interrupted and started again goes on where it stopped, and
  never mixes code;
- `runs/<run>.start.json` and `runs/<run>.end.json`: each run's case, times, Verdicts, kill,
  and for each of its workflows the history's size and its longest activation, the time
  from a workflow task's start to its completion as the server recorded them;
- `reports/<run>.json`, and `histories/<workflow>.json`, every history as Temporal exports it,
  so that `Replayer` can be run over it;
- `kills.jsonl` and `workers.jsonl`: each kill, and each worker started, ended, or stopped
  with the campaign, which loses the step it was making as a kill does;
- `repositories/<run>`, the case's repository, which the workers read.

The baseline's stop rule is held to as each run ends: when an activation passes 1 second or a
history 40 MB, no further run is started, `stopped.json` says why, and the design is reopened.

`score --baseline DIRECTORY --crash DIRECTORY --witness LOG` prints the five measures of the
campaign as JSON, from what the two wrote and what `tools/model_witness.py` saw.
"""

import argparse
import asyncio
import json
import os
import random
import re
import signal
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from temporalio.api.enums.v1 import PendingActivityState
from temporalio.client import Client, WorkflowFailureError
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError

from pr_verification.adversarial_cases import FALSE_PULL_REQUESTS, FalsePullRequest, PlacedIn
from pr_verification.budget import budget_from_environment
from pr_verification.context_bound import limits_from_environment
from pr_verification.core import PullRequest
from pr_verification.durable.journal import RunInput
from pr_verification.durable.worker import ADDRESS, connect
from pr_verification.durable.workflows import VerifyPullRequest
from pr_verification.evaluation import CaseRun, Part, score
from pr_verification.report import Report, Verdict

TASK_QUEUE = "durability"

LONGEST_ACTIVATION = 1.0
"""Seconds an activation of the baseline may take before the design is reopened."""

LARGEST_HISTORY = 40_000_000
"""Bytes a history of the baseline may take before the design is reopened: 40 of Temporal's 50
MB."""

SLACK = 0.05
"""Seconds a witnessed request may fall outside the attempt it is put to: the server's times
are to the millisecond, and the two clocks are the one clock of this machine."""

KILL_AFTER = (60.0, 1200.0)
"""The range a crash run's delay before its kill is drawn from, in seconds."""

RECORDED = (
    "PR_VERIFICATION_ENDPOINT",
    "PR_VERIFICATION_MODEL",
    "PR_VERIFICATION_CONFIGURATION",
    "PR_VERIFICATION_DECISION",
    "PR_VERIFICATION_BUDGET_SECONDS",
    "PR_VERIFICATION_BUDGET_DOLLARS",
    "PR_VERIFICATION_BUDGET_STEPS",
)
"""The settings written to `settings.json`. No key is among them."""


def development_cases() -> dict[str, tuple[FalsePullRequest, PlacedIn]]:
    """The development part of the adversarial cases by name, each as what builds it."""
    return {
        f"adversarial/{made.name}/{placement.value}": (made, placement)
        for made in FALSE_PULL_REQUESTS
        if made.part is Part.DEVELOPMENT
        for placement in PlacedIn
    }


def read_json(path: Path) -> Any:
    return json.loads(path.read_text())


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def append_line(path: Path, value: dict[str, Any]) -> None:
    with path.open("a") as lines:
        lines.write(json.dumps(value) + "\n")


def read_lines(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def moment(text: str) -> float:
    """Seconds since the epoch of a time as Temporal writes it, to the microsecond."""
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", text).replace("Z", "+00:00")
    return datetime.fromisoformat(trimmed).timestamp()


def pid_of(identity: str) -> int:
    """The process of a worker's identity, which the SDK makes `pid@host`."""
    return int(identity.split("@", 1)[0])


# What a history shows.


def events(history: dict[str, Any]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = history.get("events", [])
    return found


def kind_of(event: dict[str, Any]) -> str:
    return str(event["eventType"]).removeprefix("EVENT_TYPE_")


def attributes(event: dict[str, Any]) -> dict[str, Any]:
    (found,) = (value for key, value in event.items() if key.endswith("EventAttributes"))
    return dict(found)


def activations(history: dict[str, Any]) -> list[float]:
    """How long each completed workflow task took, from its start to its completion."""
    started: dict[str, float] = {}
    taken: list[float] = []
    for event in events(history):
        if kind_of(event) == "WORKFLOW_TASK_STARTED":
            started[str(event["eventId"])] = moment(event["eventTime"])
        elif kind_of(event) == "WORKFLOW_TASK_COMPLETED":
            began = started[str(attributes(event)["startedEventId"])]
            taken.append(moment(event["eventTime"]) - began)
    return taken


@dataclass
class Step:
    """One activity of a history, as its last attempt left it."""

    activity: str
    type: str
    scheduled: int
    attempt: int = 0
    identity: str = ""
    started: float = 0.0
    ended: float | None = None
    outcome: str = "pending"


def steps(history: dict[str, Any]) -> dict[str, Step]:
    """Each activity of a history by its activity ID. Temporal records the start of the last
    attempt only, with the attempt's number, so an activity made again shows a number past
    1."""
    found: dict[str, Step] = {}
    by_event: dict[str, Step] = {}
    for event in events(history):
        kind, given = kind_of(event), attributes(event)
        if kind == "ACTIVITY_TASK_SCHEDULED":
            activity = str(given["activityId"])
            step = found.get(activity) or Step(activity, given["activityType"]["name"], 0)
            step.scheduled += 1
            found[activity] = by_event[str(event["eventId"])] = step
        elif kind == "ACTIVITY_TASK_STARTED":
            step = by_event[str(given["scheduledEventId"])]
            step.attempt = int(given.get("attempt", 1))
            step.identity = str(given.get("identity", ""))
            step.started = moment(event["eventTime"])
        elif kind.startswith("ACTIVITY_TASK_") and "scheduledEventId" in given:
            step = by_event[str(given["scheduledEventId"])]
            step.ended = moment(event["eventTime"])
            step.outcome = kind.removeprefix("ACTIVITY_TASK_").lower()
    return found


# The workers.


class Workers:
    """`count` worker processes on the task queue, each started again when it exits."""

    def __init__(self, count: int, arguments: argparse.Namespace) -> None:
        self._count = count
        self._arguments = arguments
        self._out: Path = arguments.out
        self._processes: dict[int, subprocess.Popen[bytes]] = {}
        self._logs: dict[int, IO[bytes]] = {}

    def _command(self) -> list[str]:
        return [
            str(Path(sys.executable).parent / "pr-verification-durable"),
            "--address",
            self._arguments.address,
            "--task-queue",
            self._arguments.task_queue,
            "worker",
            "--state-dir",
            str(self._out / "state"),
            "--max-concurrent-activities",
            "1",
        ]

    def _start(self, slot: int) -> None:
        log = self._logs.setdefault(slot, (self._out / f"workers/{slot}.log").open("ab"))
        process = subprocess.Popen(
            self._command(), stdout=log, stderr=subprocess.STDOUT, start_new_session=True
        )
        self._processes[slot] = process
        append_line(
            self._out / "workers.jsonl",
            {"event": "start", "slot": slot, "pid": process.pid, "at": time.time()},
        )

    def start(self) -> None:
        (self._out / "workers").mkdir(parents=True, exist_ok=True)
        for slot in range(self._count):
            self._start(slot)

    async def keep(self) -> None:
        """Start again each worker that exits, within a second of its exit."""
        while True:
            for slot, process in list(self._processes.items()):
                if process.poll() is not None:
                    append_line(
                        self._out / "workers.jsonl",
                        {
                            "event": "end",
                            "slot": slot,
                            "pid": process.pid,
                            "exit": process.returncode,
                            "at": time.time(),
                        },
                    )
                    self._start(slot)
            await asyncio.sleep(1)

    def pids(self) -> set[int]:
        return {process.pid for process in self._processes.values() if process.poll() is None}

    def stop(self) -> None:
        """Stop every worker. A step one was making is lost with it, as with a kill, so each
        is recorded as stopped."""
        for process in self._processes.values():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
        for slot, process in self._processes.items():
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            append_line(
                self._out / "workers.jsonl",
                {"event": "stopped", "slot": slot, "pid": process.pid, "at": time.time()},
            )


def stray_workers(task_queue: str) -> list[int]:
    """Workers on `task_queue` left by a campaign that ended without stopping them."""
    found = subprocess.run(
        ["pgrep", "-f", f"--task-queue {task_queue} worker"],
        capture_output=True,
        text=True,
        check=False,
    )
    return [int(pid) for pid in found.stdout.split()]


def busy_machine() -> str | None:
    """Why the machine is not quiet enough to time runs on, or None when it is."""
    found = subprocess.run(["pgrep", "-x", "yes"], capture_output=True, text=True, check=False)
    if found.stdout.strip():
        return f"`yes` processes are running: {' '.join(found.stdout.split())}"
    return None


def pinned(arguments: argparse.Namespace) -> dict[str, Any]:
    """The settings of the runs under `--out`, written when there are none yet. Exits when the
    directory holds runs of another commit or phase, or the working tree has changes."""
    commit = subprocess.run(
        ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()
    changed = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=no"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    if changed:
        sys.exit("the working tree has changes, so the runs would not be of one commit")
    path = arguments.out / "settings.json"
    if path.exists():
        kept: dict[str, Any] = read_json(path)
        if (kept["commit"], kept["phase"]) != (commit, arguments.phase):
            sys.exit(
                f"{arguments.out} holds {kept['phase']} runs of {kept['commit']}, "
                f"not {arguments.phase} runs of {commit}"
            )
        return kept
    settings = {
        "phase": arguments.phase,
        "commit": commit,
        "started": time.time(),
        "workers": arguments.workers,
        "at_once": arguments.at_once,
        "task_queue": arguments.task_queue,
        "load_average": os.getloadavg(),
        "environment": {name: os.environ.get(name) for name in RECORDED},
    }
    write_json(path, settings)
    return settings


# One run.


@dataclass(frozen=True)
class Planned:
    name: str
    case: str
    repetition: int


class Campaign:
    def __init__(self, arguments: argparse.Namespace, client: Client, workers: Workers) -> None:
        self.arguments = arguments
        self.out: Path = arguments.out
        self.client = client
        self.workers = workers
        self.cases = development_cases()
        self.stopped: str | None = None
        if (self.out / "stopped.json").exists():
            self.stopped = read_json(self.out / "stopped.json")["why"]

    def started(self) -> dict[str, dict[str, Any]]:
        return {
            path.name.removesuffix(".start.json"): read_json(path)
            for path in sorted((self.out / "runs").glob("*.start.json"))
        }

    def ended(self) -> dict[str, dict[str, Any]]:
        return {
            path.name.removesuffix(".end.json"): read_json(path)
            for path in sorted((self.out / "runs").glob("*.end.json"))
        }

    async def run(self, planned: Planned) -> dict[str, Any]:
        """Run `planned` to its end, or go on with it when it was started before, and record
        it."""
        start_path = self.out / f"runs/{planned.name}.start.json"
        if start_path.exists():
            start = read_json(start_path)
        else:
            made, placement = self.cases[planned.case]
            directory = self.out / f"repositories/{planned.name}"
            directory.mkdir(parents=True)
            case = made.built_in(directory, placement)
            assert case.name == planned.case
            start = {
                "run": planned.name,
                "case": planned.case,
                "repetition": planned.repetition,
                "started": time.time(),
                "kill_after": (
                    random.Random(f"{self.arguments.seed}-{planned.name}").uniform(
                        *self.arguments.kill_after
                    )
                    if self.arguments.phase == "crash"
                    else None
                ),
                "pull_request": {
                    **vars(case.pull_request),
                    "repository": str(case.pull_request.repository),
                },
            }
            write_json(start_path, start)
        given = RunInput(
            run=planned.name,
            pull_request=PullRequest(
                **{**start["pull_request"], "repository": Path(start["pull_request"]["repository"])}
            ),
            budget=budget_from_environment(os.environ),
            shown=limits_from_environment(os.environ),
        )
        try:
            # Never a second time: a run that has ended is not run again.
            await self.client.start_workflow(
                VerifyPullRequest.run,
                given,
                id=planned.name,
                task_queue=self.arguments.task_queue,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
            )
        except WorkflowAlreadyStartedError:
            pass
        killing = None
        if start["kill_after"] is not None and not self.killed(planned.name):
            killing = asyncio.create_task(
                self.kill_in_flight(planned.name, start["started"] + start["kill_after"])
            )
        report: Report | None = None
        no_report: str | None = None
        try:
            report = await self.client.get_workflow_handle(planned.name).result()
        except WorkflowFailureError as failed:
            no_report = str(failed.cause)
        finally:
            if killing is not None:
                killing.cancel()
        ended = time.time()
        if report is not None:
            write_json(self.out / f"reports/{planned.name}.json", json.loads(report.to_json()))
        workflows = await self.exported(planned.name)
        kill = self.killed(planned.name)
        end = {
            **start,
            "ended": ended,
            "seconds": ended - start["started"],
            "verdicts": [] if report is None else [c.verdict.value for c in report.claims],
            "no_report": no_report,
            "kill": kill,
            "counted": kill is not None and landed(kill, self.out),
            "workflows": workflows,
        }
        write_json(self.out / f"runs/{planned.name}.end.json", end)
        return end

    def killed(self, run: str) -> dict[str, Any] | None:
        kills = [kill for kill in read_lines(self.out / "kills.jsonl") if kill["run"] == run]
        return kills[0] if kills else None

    async def in_flight(self, run: str) -> dict[str, Any] | None:
        """A step of `run` a worker is making, or None when none is."""
        looked = [run]
        while looked:
            workflow = looked.pop()
            described = (await self.client.get_workflow_handle(workflow).describe()).raw_description
            looked.extend(child.workflow_id for child in described.pending_children)
            for pending in described.pending_activities:
                if pending.state == PendingActivityState.PENDING_ACTIVITY_STATE_STARTED:
                    return {
                        "run": run,
                        "workflow": workflow,
                        "activity": pending.activity_id,
                        "attempt": pending.attempt,
                        "identity": pending.last_worker_identity,
                        "step_started": pending.last_started_time.seconds
                        + pending.last_started_time.nanos / 1e9,
                    }
        return None

    async def kill_in_flight(self, run: str, due: float) -> None:
        """Once `due` has passed, kill the worker making a step of `run`, as soon as one is."""
        await asyncio.sleep(max(0.0, due - time.time()))
        while True:
            try:
                step = await self.in_flight(run)
            except RPCError:
                step = None  # A workflow ended between asking for its children and for it.
            if step is not None and pid_of(step["identity"]) in self.workers.pids():
                os.kill(pid_of(step["identity"]), signal.SIGKILL)
                append_line(self.out / "kills.jsonl", {**step, "at": time.time()})
                return
            await asyncio.sleep(2)

    async def exported(self, run: str) -> list[dict[str, Any]]:
        """Export the history of `run` and of each workflow it started, and say of each how
        large it is and how long its longest activation took."""
        found: list[dict[str, Any]] = []
        workflows = [run]
        for workflow in workflows:
            handle = self.client.get_workflow_handle(workflow)
            history = (await handle.fetch_history()).to_json_dict()
            write_json(self.out / f"histories/{workflow}.json", history)
            for event in events(history):
                if kind_of(event) == "START_CHILD_WORKFLOW_EXECUTION_INITIATED":
                    child = str(attributes(event)["workflowId"])
                    if child not in workflows:
                        workflows.append(child)
            info = (await handle.describe()).raw_description.workflow_execution_info
            taken = activations(history)
            found.append(
                {
                    "workflow": workflow,
                    "history_bytes": info.history_size_bytes,
                    "events": info.history_length,
                    "activations": len(taken),
                    "longest_activation": max(taken, default=0.0),
                }
            )
        return found

    def stop_rule(self, end: dict[str, Any]) -> str | None:
        for workflow in end["workflows"]:
            if workflow["longest_activation"] > LONGEST_ACTIVATION:
                return (
                    f"an activation of {workflow['workflow']} took "
                    f"{workflow['longest_activation']:.3f} s"
                )
            if workflow["history_bytes"] > LARGEST_HISTORY:
                return f"the history of {workflow['workflow']} takes {workflow['history_bytes']} B"
        return None


def landed(kill: dict[str, Any], out: Path) -> bool:
    """Whether a kill landed with its step in flight: the history shows the step was not
    completed by the attempt the kill hit."""
    step = steps(read_json(out / f"histories/{kill['workflow']}.json")).get(kill["activity"])
    if step is None:
        return False
    return not (step.outcome == "completed" and step.attempt == kill["attempt"])


async def campaign(arguments: argparse.Namespace) -> None:
    busy = busy_machine()
    if busy is not None:
        sys.exit(f"the machine is not quiet: {busy}")
    for pid in stray_workers(arguments.task_queue):
        print(f"stopping a worker left on {arguments.task_queue}: {pid}", file=sys.stderr)
        os.kill(pid, signal.SIGTERM)
    pinned(arguments)
    client = await connect(arguments.address)
    workers = Workers(arguments.workers, arguments)
    workers.start()
    keeping = asyncio.create_task(workers.keep())
    try:
        await scheduled(Campaign(arguments, client, workers))
    finally:
        keeping.cancel()
        workers.stop()


async def scheduled(campaign: Campaign) -> None:
    """Start runs, `--at-once` at a time, until the phase has run what it runs."""
    arguments = campaign.arguments
    names = sorted(campaign.cases)
    started, ended = campaign.started(), campaign.ended()
    if arguments.phase == "baseline":
        plan = [
            Planned(f"baseline-{index:03d}", case, repetition)
            for index, (repetition, case) in enumerate(
                (
                    (repetition, case)
                    for repetition in range(1, arguments.repetitions + 1)
                    for case in names
                ),
                start=1,
            )
        ]
        waiting = [
            planned for planned in plan if planned.name not in ended and planned.name not in started
        ]
    else:
        waiting = []
    counted = sum(bool(end.get("counted")) for end in ended.values())
    runs_of = Counter(start["case"] for start in started.values())
    # Runs started before are running in Temporal whatever is decided now, so each is followed.
    running = {
        asyncio.create_task(campaign.run(planned)): planned
        for planned in (
            Planned(name, start["case"], start["repetition"])
            for name, start in started.items()
            if name not in ended
        )
    }

    def next_crash_run() -> Planned:
        case = min(names, key=lambda name: (runs_of[name], names.index(name)))
        runs_of[case] += 1
        return Planned(f"crash-{len(started) + 1:03d}", case, runs_of[case])

    while True:
        while len(running) < arguments.at_once and campaign.stopped is None:
            if waiting:
                planned = waiting.pop(0)
            elif arguments.phase == "crash" and counted + len(running) < arguments.count:
                planned = next_crash_run()
                started[planned.name] = {"case": planned.case}
            else:
                break
            running[asyncio.create_task(campaign.run(planned))] = planned
            print(f"started {planned.name}: {planned.case}", file=sys.stderr, flush=True)
        if not running:
            break
        done, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            planned = running.pop(task)
            end = task.result()
            counted += bool(end["counted"])
            given = ", ".join(end["verdicts"]) or end["no_report"] or "no Claims"
            print(
                f"ended {planned.name}: {given}, "
                f"{end['seconds']:.0f} s, kill {'counted' if end['counted'] else 'none'}, "
                f"{counted} counted",
                file=sys.stderr,
                flush=True,
            )
            why = campaign.stop_rule(end)
            if why is not None and campaign.stopped is None:
                campaign.stopped = why
                write_json(campaign.out / "stopped.json", {"run": planned.name, "why": why})
                print(f"stop rule: {why}; no further run is started", file=sys.stderr)


# The score.


def ended_runs(directory: Path) -> list[dict[str, Any]]:
    return [read_json(path) for path in sorted((directory / "runs").glob("*.end.json"))]


def summarised(runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Measures 1, 4, and 5 over `runs`, and the largest history and longest activation of
    each kind of workflow."""
    scores = score(
        CaseRun(
            name=run["case"],
            repetition=run["repetition"],
            expected=Verdict.REFUTED,
            verdicts=tuple(Verdict(verdict) for verdict in run["verdicts"]),
            spend=None,
            no_report=run["no_report"],
        )
        for run in runs
    )
    seconds = [run["seconds"] for run in runs]
    workflows = [workflow for run in runs for workflow in run["workflows"]]
    claims = [workflow for workflow in workflows if "-claim-" in workflow["workflow"]]
    pull_requests = [workflow for workflow in workflows if "-claim-" not in workflow["workflow"]]

    def largest(of: list[dict[str, Any]]) -> dict[str, Any]:
        return {
            "longest_activation": max((w["longest_activation"] for w in of), default=None),
            "largest_history_bytes": max((w["history_bytes"] for w in of), default=None),
            "most_events": max((w["events"] for w in of), default=None),
        }

    return {
        "runs": len(runs),
        "reports": sum(run["no_report"] is None for run in runs),
        "no_report": [
            {"run": run["run"], "why": run["no_report"]} for run in runs if run["no_report"]
        ],
        "false_claims": scores.false_claims,
        "decided": scores.decided,
        "inconclusive": scores.inconclusive,
        "false_assurances": scores.false_assurances,
        "runs_without_verdicts": scores.runs_without_verdicts,
        "median_seconds": statistics.median(seconds) if seconds else None,
        "longest_seconds": max(seconds, default=None),
        "verify_claim": largest(claims),
        "verify_pull_request": largest(pull_requests),
    }


def made_twice(directory: Path, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure 2: activities a history shows made more than once. A step scheduled twice is a
    completed step made again, the target being none; a step whose last attempt is past the
    first was made again after an attempt that did not finish, which each kill should
    explain."""
    scheduled_twice: list[dict[str, Any]] = []
    retried: list[dict[str, Any]] = []
    for run in runs:
        kill = run["kill"]
        for workflow in run["workflows"]:
            history = read_json(directory / f"histories/{workflow['workflow']}.json")
            for step in steps(history).values():
                where = {"run": run["run"], "workflow": workflow["workflow"], "step": step.activity}
                if step.scheduled > 1:
                    scheduled_twice.append(where)
                if step.attempt > 1:
                    explained = (
                        kill is not None
                        and kill["workflow"] == workflow["workflow"]
                        and kill["activity"] == step.activity
                    )
                    retried.append({**where, "attempt": step.attempt, "by_kill": explained})
    return {
        "completed_steps_made_again": len(scheduled_twice),
        "completed_steps_made_again_where": scheduled_twice,
        "steps_made_again_after_a_lost_attempt": len(retried),
        "of_which_by_a_kill": sum(each["by_kill"] for each in retried),
        "not_by_a_kill": [each for each in retried if not each["by_kill"]],
    }


def witnessed(directory: Path, runs: list[dict[str, Any]], log: Path) -> dict[str, Any]:
    """Measure 3: model requests sent more than once for one step, as the witness saw them.

    Each request is put to the step its sender was making when it was sent: a worker makes
    one step at a time, so its process and the time name the step. The attempt a kill
    stopped is known from the kill; every other attempt is the last, which the history
    holds. Only requests of the directory's own workers are read, so one witness can serve
    the baseline and the campaign. A request put to no step was most likely of a worker
    stopped with the campaign itself, which `workers.jsonl` records."""
    attempts: list[tuple[int, float, float, tuple[str, str], dict[str, Any] | None]] = []
    workers = read_lines(directory / "workers.jsonl")
    own = {worker["pid"] for worker in workers}
    stopped_with_campaign = {w["pid"] for w in workers if w["event"] == "stopped"}
    first = min((run["started"] for run in runs), default=0.0)
    last = max((run["ended"] for run in runs), default=0.0)
    for run in runs:
        kill = run["kill"]
        if kill is not None:
            attempts.append(
                (
                    pid_of(kill["identity"]),
                    kill["step_started"],
                    kill["at"],
                    (kill["workflow"], kill["activity"]),
                    kill,
                )
            )
        for workflow in run["workflows"]:
            history = read_json(directory / f"histories/{workflow['workflow']}.json")
            for step in steps(history).values():
                if step.identity:
                    attempts.append(
                        (
                            pid_of(step.identity),
                            step.started,
                            step.ended or last,
                            (workflow["workflow"], step.activity),
                            None,
                        )
                    )
    lines = read_lines(log)
    ends = {line["request"]: line for line in lines if line["event"] == "end"}
    by_step: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    unplaced: list[dict[str, Any]] = []
    finished_unrecorded: list[dict[str, Any]] = []
    for line in lines:
        if line["event"] != "start" or line["pid"] not in own or not first <= line["at"] <= last:
            continue
        end = ends.get(line["request"])
        # The server stamps an attempt's start before its worker sends the request, and its
        # end after the answer came back; of a worker's attempts, the last begun holds it.
        placed = [
            (began, place, kill)
            for pid, began, stopped, place, kill in attempts
            if pid == line["pid"] and began - SLACK <= line["at"] <= stopped + SLACK
        ]
        if not placed:
            unplaced.append(line)
            continue
        _, place, kill = max(placed, key=lambda each: each[0])
        by_step[place].append(line)
        if kill is not None and end is not None and end["status"] == 200:
            if end["at"] < kill["at"]:
                finished_unrecorded.append({"step": place, "request": line["request"]})
    killed = {
        (run["kill"]["workflow"], run["kill"]["activity"])
        for run in runs
        if run["kill"] is not None
    }
    repeated = [
        {
            "workflow": step[0],
            "step": step[1],
            "requests": len(sent),
            "same_request": len({line["sha256"] for line in sent}) == 1,
            "by_kill": step in killed,
        }
        for step, sent in by_step.items()
        if len(sent) > 1
    ]
    return {
        "requests": sum(len(sent) for sent in by_step.values()),
        "repeated_steps": len(repeated),
        "of_which_by_a_kill": sum(each["by_kill"] for each in repeated),
        "not_by_a_kill": [each for each in repeated if not each["by_kill"]],
        "answered_but_never_recorded": finished_unrecorded,
        "requests_of_no_step": [
            {
                "request": line["request"],
                "pid": line["pid"],
                "stopped_with_campaign": line["pid"] in stopped_with_campaign,
            }
            for line in unplaced
        ],
    }


def scored(arguments: argparse.Namespace) -> None:
    baseline = ended_runs(arguments.baseline)
    crash = ended_runs(arguments.crash)
    counted = [run for run in crash if run["counted"]]
    commits = {
        name: read_json(directory / "settings.json")["commit"]
        for name, directory in (("baseline", arguments.baseline), ("crash", arguments.crash))
    }
    print(
        json.dumps(
            {
                "commits": commits,
                "one_commit": len(set(commits.values())) == 1,
                "crash_runs": len(crash),
                "counted": len(counted),
                "baseline": summarised(baseline),
                "crash": summarised(counted),
                "made_twice": made_twice(arguments.crash, crash),
                "witness": witnessed(arguments.crash, crash, arguments.witness),
                "baseline_made_twice": made_twice(arguments.baseline, baseline),
                "baseline_witness": witnessed(arguments.baseline, baseline, arguments.witness),
            },
            indent=2,
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    phases = parser.add_subparsers(dest="phase", required=True)
    for phase in ("baseline", "crash"):
        running = phases.add_parser(phase)
        running.add_argument("--out", type=Path, required=True)
        running.add_argument("--address", default=ADDRESS)
        running.add_argument("--task-queue", default=TASK_QUEUE)
        running.add_argument("--workers", type=int, default=6)
        running.add_argument("--at-once", type=int, default=6)
        if phase == "baseline":
            running.add_argument("--repetitions", type=int, default=3)
        else:
            running.add_argument("--count", type=int, default=180)
            running.add_argument("--seed", default="durability")
            running.add_argument(
                "--kill-after", type=float, nargs=2, default=KILL_AFTER, metavar=("LOW", "HIGH")
            )
    scoring = phases.add_parser("score")
    scoring.add_argument("--baseline", type=Path, required=True)
    scoring.add_argument("--crash", type=Path, required=True)
    scoring.add_argument("--witness", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.phase == "score":
        scored(arguments)
        return
    arguments.out.mkdir(parents=True, exist_ok=True)
    # Stopped either way, the workers are stopped with it, and the runs wait in Temporal.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    try:
        asyncio.run(campaign(arguments))
    except KeyboardInterrupt:
        sys.exit("stopped: run again with the same --out to go on")


if __name__ == "__main__":
    main()
