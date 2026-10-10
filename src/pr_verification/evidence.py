"""Gathering Evidence by running code in the Sandbox."""

from pr_verification.report import RunEvidence
from pr_verification.sandbox import Command, Limits, Phase, Sandbox


def run_on_both_commits(
    sandbox: Sandbox, command: Command, *, before: str, after: str, phase: Phase, limits: Limits
) -> RunEvidence:
    """Run the same command against the code before the pull request and the code after it.

    Runs `command` in `sandbox` against `before`, then against `after`, each with `phase` and
    `limits`, and records both results as one piece of Evidence obtained by running code:

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
    raise NotImplementedError
