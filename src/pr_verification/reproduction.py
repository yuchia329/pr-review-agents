"""The Reproduction writer: writes a Reproduction for one Behavioural Claim, runs it on both
commits, and revises it up to a cap on attempts.

A model writes the Reproduction. Code places it, runs it, reads the results, and decides
whether another attempt is needed (ADR 0002). Whether a Claim is Confirmed or Refuted is not
decided here: the writer gives Evidence, or the reason there is none.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pr_verification.agent import Role, RoleRun, run_role
from pr_verification.ecosystem import (
    LONGEST_REPRODUCTION,
    ReproductionEcosystem,
    Results,
    Unreadable,
)
from pr_verification.evidence import run_on_both_commits
from pr_verification.model import Model, ModelFailed
from pr_verification.reading import reading_tools
from pr_verification.report import ReproductionEvidence, RunEvidence
from pr_verification.sandbox import Command, Limits, Phase, RunResult, Sandbox, SandboxFailed

ATTEMPT_CAP = 3
"""The most attempts the writer makes for one Claim unless another cap is given."""

WRITER_STEP_CAP = 12
"""The most steps the writer's role takes in one attempt unless another cap is given."""


class AttemptResult(StrEnum):
    """What one attempt at a Reproduction came to. Only the last of these ends the revising."""

    NO_REPRODUCTION = "no_reproduction"
    """The role gave no Reproduction to run."""

    COULD_NOT_RUN = "could_not_run"
    """The Reproduction did not run to an answer on both commits, as with a set-up error or
    an import error. Such a Reproduction is not Evidence."""

    SHOWS_NOTHING = "shows_nothing"
    """The Reproduction ran on both commits and behaved the same on both, so it shows nothing
    about the Claim."""

    SHOWS_A_DIFFERENCE = "shows_a_difference"
    """The Reproduction ran on both commits and behaved differently. That is Evidence."""


ATTEMPT_CAP_REACHED = (
    "No Reproduction showed a difference in behaviour between the code before and after the "
    "pull request in {attempts} attempts. The last one {ending}."
)
"""Why a Claim is Inconclusive when the cap on attempts is reached. `attempts` is the number
of attempts made and `ending` is `ENDINGS` for the result of the last one."""

ENDINGS: Mapping[AttemptResult, str] = {
    AttemptResult.NO_REPRODUCTION: "was never written",
    AttemptResult.COULD_NOT_RUN: "could not be run on both",
    AttemptResult.SHOWS_NOTHING: "behaved the same on both",
}
"""How `ATTEMPT_CAP_REACHED` describes a last attempt, by its result."""

COULD_NOT_FINISH = "The Reproduction writer could not finish its work: {failure}"
"""Why a Claim is Inconclusive when the model or the Sandbox itself failed. `failure` is what
the `ModelFailed` or `SandboxFailed` raised said."""


def compare_runs(before: Results | Unreadable, after: Results | Unreadable) -> AttemptResult:
    """What a Reproduction's results on the two commits show, as read by its Ecosystem.

    - `COULD_NOT_RUN` when either is `Unreadable`, when either holds a test in
      `could_not_run`, or when either holds no test in `passed` and none in `failed`.
    - Otherwise `SHOWS_NOTHING` when the same tests passed on both commits and the same tests
      failed on both. The order of the tests carries no meaning.
    - Otherwise `SHOWS_A_DIFFERENCE`.

    Never `NO_REPRODUCTION`. Which way a difference points, and so any Verdict, is not
    decided here.
    """
    if isinstance(before, Unreadable) or isinstance(after, Unreadable):
        return AttemptResult.COULD_NOT_RUN
    for read in (before, after):
        if read.could_not_run or not (read.passed or read.failed):
            return AttemptResult.COULD_NOT_RUN
    same = set(before.passed) == set(after.passed) and set(before.failed) == set(after.failed)
    return AttemptResult.SHOWS_NOTHING if same else AttemptResult.SHOWS_A_DIFFERENCE


@dataclass(frozen=True)
class Attempt:
    """One attempt: the role's run, the Reproduction it gave, and what running it did."""

    run: RoleRun
    """The record of the writer's role for this attempt."""

    reproduction: str | None
    """The text of the Reproduction, or None when the result is `NO_REPRODUCTION`."""

    before: RunResult | None
    """What the last command of this attempt did against the code before the pull request:
    running the Reproduction, or placing it when it could not be placed on both commits.
    None when the result is `NO_REPRODUCTION`, where no command was run."""

    after: RunResult | None
    """The same, against the code after the pull request."""

    result: AttemptResult
    """What the attempt came to."""


@dataclass(frozen=True)
class ReproductionRun:
    """The run record of the Reproduction writer for one Behavioural Claim."""

    attempts: tuple[Attempt, ...]
    """Every attempt that came to a result, in order. Never more than the cap on attempts,
    and none when the model or the Sandbox failed during the first."""

    evidence: ReproductionEvidence | None
    """The Reproduction of the last attempt and its runs on both commits, when that attempt
    shows a difference. None when the work ended any other way."""

    inconclusive: str | None
    """Why the Claim is Inconclusive, when the work ended without `evidence`. None when
    there is `evidence`: exactly one of the two is None."""


def write_reproduction(
    claim: str,
    *,
    model: Model,
    sandbox: Sandbox,
    ecosystem: ReproductionEcosystem,
    repository: Path,
    before: str,
    after: str,
    limits: Limits,
    attempt_cap: int = ATTEMPT_CAP,
    step_cap: int = WRITER_STEP_CAP,
) -> ReproductionRun:
    """Write a Reproduction for `claim`, run it on both commits, and revise it up to a cap.

    `claim` is a Behavioural Claim. `before` and `after` are the commits before and after
    the pull request, in `repository`.
    The caller has already run the install commands for both in `sandbox`, so what a
    Reproduction needs to run is there. Makes one attempt after another. Each attempt:

    1. Writes. A role named `reproduction-writer` is run with `run_role`. Its tools are
       `reading_tools(repository, after)` and no other, so it has no tool that runs
       anything. Its step cap is `step_cap`, and its instructions hold
       `ecosystem.reproduction_form()`. Its task is `{"claim": claim, "attempts": [...]}`,
       where `attempts` has one object for each earlier attempt, in order, whose only
       members are `reproduction`, `result`, `before`, and `after`: the Reproduction's text,
       the value of the `AttemptResult`, and the output of the two runs the attempt records,
       each null where the attempt has none. So the role's context holds its Claim, what
       its earlier attempts did, and what it reads, and Sandbox output reaches the model
       only there, as data.
       The role finishes with `{"reproduction": "<text>"}`. When its run stopped at the
       step cap, or its answer holds under `reproduction` no string, a blank one, or one
       longer than `LONGEST_REPRODUCTION` characters, the attempt's result is
       `NO_REPRODUCTION` and nothing is run.
    2. Places. `ecosystem.place` is asked about the text, exactly as the role gave it, and
       the placement's `write` is run against `before`, then against `after`, however the
       first ended. When either does not exit with status 0, the result is `COULD_NOT_RUN`,
       the Reproduction is not run, and the attempt records those two runs.
    3. Runs. The placement's `run` is run against `before`, then against `after`, however
       the first ended. The attempt records these two runs.
    4. Reads. `ecosystem.read_results` reads each run, and `compare_runs` gives the result.

    Every command is run by `sandbox.run`, in `Phase.TEST` with `limits`, and no other way:
    nothing a model wrote is run outside the Sandbox.

    An attempt that shows a difference ends the work: no further request is made to the
    model, and `evidence` holds `source` `EvidenceSource.RAN_CODE`, the command that ran the
    Reproduction, both results, and `observation` as `run_on_both_commits` writes it, with
    the placement's `path`, the Reproduction's text, and as `before_results` and
    `after_results` what `ecosystem.read_results` gave for the two runs in step 4. Any other
    result is revised with another attempt, until `attempt_cap` attempts have been made. Then
    there is no
    `evidence`, and `inconclusive` is `ATTEMPT_CAP_REACHED` filled in. The cap is enforced
    here, by code: whatever the model says, no more than `attempt_cap` attempts are made.

    When the model raises `ModelFailed` or the Sandbox raises `SandboxFailed`, the work
    ends there: no further request is made to the model and nothing more is run. The record
    is still returned, with the attempts before the one that was under way, no `evidence`,
    and `inconclusive` as `COULD_NOT_FINISH` filled in. A failure inside the system leaves
    the Claim Inconclusive, and what was tried before it is not lost.

    Every attempt that came to a result is kept in the record, whatever the result.

    Raises `ValueError` before any request when `attempt_cap` or `step_cap` is less than 1.
    Any other exception is a fault in the system and is raised.
    """
    if attempt_cap < 1 or step_cap < 1:
        raise ValueError(f"a cap is under 1: {attempt_cap} attempts of {step_cap} steps")
    role = Role(
        name="reproduction-writer",
        instructions=_instructions(ecosystem.reproduction_form()),
        tools=reading_tools(repository, after),
        step_cap=step_cap,
    )

    def on_both_commits(command: Command) -> RunEvidence:
        # The one place an attempt runs anything: in the Sandbox, with no network.
        return run_on_both_commits(
            sandbox, command, before=before, after=after, phase=Phase.TEST, limits=limits
        )

    attempts: list[Attempt] = []
    try:
        while len(attempts) < attempt_cap:
            task = {"claim": claim, "attempts": [_shown(attempt) for attempt in attempts]}
            run = run_role(role, task, model)
            text = _reproduction_given(run)
            if text is None:
                attempts.append(Attempt(run, None, None, None, AttemptResult.NO_REPRODUCTION))
                continue
            placement = ecosystem.place(text)
            ran = on_both_commits(placement.write)
            if ran.before.exit_status == 0 and ran.after.exit_status == 0:
                ran = on_both_commits(placement.run)
                result = compare_runs(
                    ecosystem.read_results(ran.before), ecosystem.read_results(ran.after)
                )
            else:
                result = AttemptResult.COULD_NOT_RUN
            attempts.append(Attempt(run, text, ran.before, ran.after, result))
            if result is AttemptResult.SHOWS_A_DIFFERENCE:
                evidence = ReproductionEvidence(
                    source=ran.source,
                    observation=ran.observation,
                    command=ran.command,
                    before=ran.before,
                    after=ran.after,
                    path=placement.path,
                    reproduction=text,
                )
                return ReproductionRun(tuple(attempts), evidence, inconclusive=None)
    except (ModelFailed, SandboxFailed) as failure:
        # The attempt under way came to no result, so it is not among those kept.
        reason = COULD_NOT_FINISH.format(failure=failure)
        return ReproductionRun(tuple(attempts), evidence=None, inconclusive=reason)
    ending = ENDINGS[attempts[-1].result]
    reason = ATTEMPT_CAP_REACHED.format(attempts=len(attempts), ending=ending)
    return ReproductionRun(tuple(attempts), evidence=None, inconclusive=reason)


def _instructions(form: str) -> str:
    """What the writer's role is told, around what its Ecosystem says a Reproduction is."""
    return f"""\
You write a Reproduction for one Behavioural Claim. A Behavioural Claim says that a pull request
makes the code do something different when it is run, and it is the "claim" of your task. A
Reproduction is a test of what the Claim says. It is run against the code before the pull request
and against the code after it.

Write the test from the Claim: it asserts the behaviour the Claim says the code has after the pull
request, so that it passes on code that behaves that way and fails on code that does not. Do not
judge whether the Claim is true, and do not write a test that ends the same way whatever the code
does.

Your tools read the files of the repository as they are after the pull request. Use them to learn
how the code is called. You cannot run anything yourself: the Reproduction is run for you once you
finish.

{form}

The "attempts" of your task are the Reproductions you gave before for this Claim, in order. Each
holds its "reproduction", its "result", and what the last command run for it printed against the
code "before" and "after" the pull request. A result is one of:

- "{AttemptResult.NO_REPRODUCTION}": you gave no Reproduction that could be placed.
- "{AttemptResult.COULD_NOT_RUN}": it did not run to an answer on both, as with a set-up error
  or an import error.
- "{AttemptResult.SHOWS_NOTHING}": it behaved the same on both, so it shows nothing about the Claim.

When there are attempts, write a Reproduction that mends what the last one's result and output
show to be wrong with it.

Finish with the whole text of the Reproduction, in this form:
{{"reproduction": "<text>"}}"""


def _shown(attempt: Attempt) -> dict[str, object]:
    """What the role is shown of an earlier attempt."""
    return {
        "reproduction": attempt.reproduction,
        "result": attempt.result.value,
        "before": attempt.before.output if attempt.before is not None else None,
        "after": attempt.after.output if attempt.after is not None else None,
    }


def _reproduction_given(run: RoleRun) -> str | None:
    """The text of the Reproduction a run of the role finished with, or None when it gave none
    that can be placed."""
    text = run.answer.get("reproduction") if run.answer is not None else None
    if isinstance(text, str) and text.strip() and len(text) <= LONGEST_REPRODUCTION:
        return text
    return None
