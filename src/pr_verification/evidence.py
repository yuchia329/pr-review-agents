"""Gathering Evidence by running code in the Sandbox."""

from pr_verification.report import EvidenceSource, RunEvidence
from pr_verification.sandbox import Command, Limits, Phase, RunResult, Sandbox


def run_on_both_commits(
    sandbox: Sandbox, command: Command, *, before: str, after: str, phase: Phase, limits: Limits
) -> RunEvidence:
    """Run the same command against the code before the pull request and the code after it.

    Runs `command` in `sandbox` against `before`, then against `after`, each with `phase` and
    `limits`, and so both in that Sandbox's one image. Records both results as one piece of
    Evidence obtained by running code:

    - `source` is `EvidenceSource.RAN_CODE`;
    - `command` is the command as given;
    - `before` and `after` are the two results exactly as the Sandbox returned them;
    - `observation` is `before: <ending>; after: <ending>`, where an ending is
      `exit status <number>`, `stopped at the time limit`, or `stopped at the memory limit`.
      It is written by code and holds none of the output.

    A run that fails, times out, or hits the memory limit is still recorded: the run against
    `after` happens whatever the run against `before` did. Raises `SandboxFailed` when the
    Sandbox itself cannot do a run.
    """
    results = [
        sandbox.run(command, commit=commit, phase=phase, limits=limits)
        for commit in (before, after)
    ]
    return RunEvidence(
        source=EvidenceSource.RAN_CODE,
        observation=f"before: {_ending(results[0])}; after: {_ending(results[1])}",
        command=command,
        before=results[0],
        after=results[1],
    )


def _ending(result: RunResult) -> str:
    if result.limit_hit is not None:
        return f"stopped at the {result.limit_hit} limit"
    return f"exit status {result.exit_status}"
