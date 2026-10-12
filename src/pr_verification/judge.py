"""The Reproduction judge: decides whether a Reproduction tests what its Claim says, and the
rounds between it and the Reproduction writer.

The judge is a step of its own that sees only the Claim and the Reproduction (ADR 0002). The
writer reads the pull request's code, so what it writes may have been shaped by that code, as
a planted trivial test is. The judge read none of it. Its word is still a model's, and all it
can do is keep a Reproduction from deciding a Claim, by not accepting it: whether an accepted
Reproduction supports or contradicts its Claim is computed by code, from its runs.
"""

from dataclasses import dataclass, fields
from pathlib import Path

from pr_verification.agent import Role, RoleRun, run_role
from pr_verification.decision import Decider
from pr_verification.ecosystem import ReproductionEcosystem
from pr_verification.model import Model, ModelFailed
from pr_verification.report import AcceptedReproduction, ReproductionEvidence
from pr_verification.reproduction import (
    Rejected,
    ReproductionRun,
    rewrite_reproduction,
    write_reproduction,
)
from pr_verification.sandbox import Limits, Sandbox

JUDGE_STEP_CAP = 3
"""The most steps the judge's role takes for one Reproduction unless another cap is given."""

ROUND_CAP = 3
"""The most rounds between the writer and the judge for one Claim unless another cap is
given."""

NO_REASON_GIVEN = "The Reproduction judge gave no reason."
"""The reason of a rejection when the judge's role gave none that can be used."""

ROUND_CAP_REACHED = (
    "The Reproduction judge accepted no Reproduction as a test of what the Claim says in "
    "{rounds} rounds. Its last reason: {reason}"
)
"""Why a Claim is Inconclusive when the cap on rounds is reached. `rounds` is the number of
rounds made and `reason` is the reason of the last rejection."""

JUDGE_COULD_NOT_FINISH = "The Reproduction judge could not finish its work: {failure}"
"""Why a Claim is Inconclusive when the model failed while the judge was at work. `failure` is
what the `ModelFailed` raised said."""

_INSTRUCTIONS = """\
You judge a Reproduction. A Claim is a statement a pull request makes about what its code does when
it is run, and it is the "claim" of your task. A Reproduction is a test of what that Claim says, and
it is the "reproduction" of your task. It is run against the code before the pull request and
against the code after it.

Decide one thing: whether the Reproduction tests what the Claim says. It does when it would pass on
code that behaves as the Claim says and fail on code that does not. It does not when it could end
the same way whatever that behaviour is: when it asserts nothing, asserts something the Claim does
not say, or checks only that the code exists or can be called.

Do not judge whether the Claim is true. You are shown the Claim and the Reproduction and nothing
else, and you have no tools: judge from the two alone.

Finish with {"accepted": true} when the Reproduction tests what the Claim says. Otherwise finish
with why it does not, in one or two sentences, in this form:
{"accepted": false, "reason": "<why>"}"""


@dataclass(frozen=True)
class Judgement:
    """What the judge decided about one Reproduction, and the record of its run."""

    accepted: bool
    """Whether the judge accepted the Reproduction as a test of what the Claim says."""

    reason: str | None
    """Why the Reproduction was rejected, or None when it was accepted. It is a model's word,
    shaped by what the judge read, and so Untrusted Content."""

    run: RoleRun
    """The record of the judge's role."""


def judge_reproduction(
    claim: str,
    reproduction: str,
    *,
    model: Model,
    step_cap: int = JUDGE_STEP_CAP,
    progress_guard: Decider | None = None,
) -> Judgement:
    """Decide whether `reproduction` tests what `claim` says, seeing only the two.

    Runs a role named `reproduction-judge` with `run_role`. It has no tools, its step cap is
    `step_cap`, and its task is `{"claim": claim, "reproduction": reproduction}`, each exactly
    as given and with no other member. So the judge's context holds the Claim and the
    Reproduction and nothing else: nothing of the pull request's title, description, or
    linked issue beyond the Claim itself, none of its diff or of the repository's files, and
    nothing a run in the Sandbox printed. Both are Untrusted Content, and reach the model only
    as data. The run is given `progress_guard`, so it is watched for loops as `run_role`
    says, and what the loop tells the role is the loop's own wording.

    The role is asked whether the Reproduction would pass on code that behaves as the Claim
    says and fail on code that does not. It finishes with `{"accepted": true}`, or with
    `{"accepted": false, "reason": "<why>"}`.

    The Reproduction is accepted when, and only when, the run finished and its answer holds
    the JSON value `true` under `accepted`. Anything else is a rejection: `false`, any other
    value, such as `"true"` or `1`, an answer with no `accepted`, and a run that stopped at
    the step cap. So a judge that cannot be read accepts nothing.

    The `reason` of a rejection is the answer's `reason`, exactly as the role gave it, when
    that is a string that is not blank, and `NO_REASON_GIVEN` otherwise. An acceptance has no
    reason, whatever the answer holds.

    Raises as `run_role` does. `ModelFailed` is not caught here.
    """
    role = Role("reproduction-judge", _INSTRUCTIONS, tools=(), step_cap=step_cap)
    task = {"claim": claim, "reproduction": reproduction}
    run = run_role(role, task, model, progress_guard=progress_guard)
    answer = run.answer if run.answer is not None else {}
    # Asked whether it is equal to `True`, 1 would pass for it.
    if answer.get("accepted") is True:
        return Judgement(accepted=True, reason=None, run=run)
    reason = answer.get("reason")
    if not (isinstance(reason, str) and reason.strip()):
        reason = NO_REASON_GIVEN
    return Judgement(accepted=False, reason=reason, run=run)


@dataclass(frozen=True)
class Round:
    """One round: what the writer did, and what the judge decided about the Reproduction it
    gave."""

    written: ReproductionRun
    """The run record of the Reproduction writer for this round."""

    judgement: Judgement | None
    """What the judge decided about the Reproduction of `written.evidence`. None when it
    decided nothing: the writer gave no Evidence, so there was nothing to judge, or the model
    failed while the judge was at work."""


@dataclass(frozen=True)
class JudgedRun:
    """The run record of the Reproduction writer and the Reproduction judge for one Behavioural
    Claim."""

    rounds: tuple[Round, ...]
    """Every round, in order. At least one, and never more than the cap on rounds."""

    evidence: AcceptedReproduction | None
    """The Reproduction the judge accepted and its runs on both commits. None when the work
    ended any other way."""

    inconclusive: str | None
    """Why the Claim is Inconclusive, when the work ended without `evidence`. None when
    there is `evidence`: exactly one of the two is None."""


def write_judged_reproduction(
    claim: str,
    *,
    model: Model,
    sandbox: Sandbox,
    ecosystem: ReproductionEcosystem,
    repository: Path,
    before: str,
    after: str,
    limits: Limits,
    round_cap: int = ROUND_CAP,
    progress_guard: Decider | None = None,
) -> JudgedRun:
    """Write a Reproduction for `claim` and have the judge decide on it, for a capped number
    of rounds.

    `claim` is a Behavioural Claim, and the other arguments are as `write_reproduction` takes
    them. Makes one round after another. Each round:

    1. Writes. In the first round `write_reproduction` is called with `claim`,
       `progress_guard`, and the arguments given. In each later round `rewrite_reproduction`
       is called with them and, as `rejected`, one `Rejected` for each earlier round, in
       order: the Reproduction that round's judge rejected, and the reason. Either is called
       with the caps it has unless given others. Each round is a call of its own, so the
       writer's attempts start again from none, and all it is shown of the earlier rounds is
       `rejected`.
       When the `ReproductionRun` has no `evidence`, the work ends there. The judge is not
       asked, since there is nothing to judge, and `inconclusive` is the `ReproductionRun`'s.
    2. Judges. `judge_reproduction` is called with `claim`, the `reproduction` of that
       `evidence`, `model`, `progress_guard`, and the step cap it has unless given another.
       By then the Reproduction has run on both commits and shown a difference, and the
       judge is shown nothing of that.

    An acceptance ends the work, and no further request is made to the model. `evidence` is
    an `AcceptedReproduction` that holds exactly what the writer's `evidence` of that round
    holds. No other part of the system makes an `AcceptedReproduction`, so this is the only
    way a Reproduction comes to decide a Claim (see `compute_verdict`).

    A rejection goes back to the writer, with its reason, in another round, until `round_cap`
    rounds have been made. Then there is no `evidence`, and `inconclusive` is
    `ROUND_CAP_REACHED` filled in. The cap is enforced here, by code: whatever a model says,
    no more than `round_cap` rounds are made, and so the judge is asked no more than
    `round_cap` times. A Reproduction the judge rejected is never given as `evidence`,
    whatever its runs showed.

    When `ModelFailed` is raised while the judge is at work, by the model or as a
    `DecisionFailed` by the progress guard, the work ends there: no further request is made
    to the model and nothing more is run. The record is still returned, with that round in
    it and its `judgement` None, no `evidence`, and `inconclusive` as
    `JUDGE_COULD_NOT_FINISH` filled in. A failure while the writer is at work is answered
    for by the writer, and ends the work as step 1 says.

    Every round is kept in the record, however it ended.

    Raises `ValueError` before any request when `round_cap` is less than 1. Any other
    exception is a fault in the system and is raised.
    """
    if round_cap < 1:
        raise ValueError(f"the cap on rounds is {round_cap}: under 1")
    rounds: list[Round] = []
    rejected: list[Rejected] = []

    def ended(evidence: AcceptedReproduction | None, inconclusive: str | None) -> JudgedRun:
        return JudgedRun(tuple(rounds), evidence, inconclusive)

    while len(rounds) < round_cap:
        if rounds:
            written = rewrite_reproduction(
                claim,
                tuple(rejected),
                model=model,
                sandbox=sandbox,
                ecosystem=ecosystem,
                repository=repository,
                before=before,
                after=after,
                limits=limits,
                progress_guard=progress_guard,
            )
        else:
            written = write_reproduction(
                claim,
                progress_guard,
                model=model,
                sandbox=sandbox,
                ecosystem=ecosystem,
                repository=repository,
                before=before,
                after=after,
                limits=limits,
            )
        if written.evidence is None:
            rounds.append(Round(written, judgement=None))
            return ended(None, written.inconclusive)
        reproduction = written.evidence.reproduction
        try:
            judgement = judge_reproduction(
                claim, reproduction, model=model, progress_guard=progress_guard
            )
        except ModelFailed as failure:
            rounds.append(Round(written, judgement=None))
            return ended(None, JUDGE_COULD_NOT_FINISH.format(failure=failure))
        rounds.append(Round(written, judgement))
        if judgement.accepted:
            return ended(_accepted(written.evidence), None)
        rejected.append(Rejected(reproduction, judgement.reason or NO_REASON_GIVEN))
    reason = ROUND_CAP_REACHED.format(rounds=len(rounds), reason=rejected[-1].reason)
    return ended(None, reason)


def _accepted(evidence: ReproductionEvidence) -> AcceptedReproduction:
    """The Evidence of a Reproduction the judge accepted: all that `evidence` holds, and
    nothing else. The one place an `AcceptedReproduction` is made."""
    held = {field.name: getattr(evidence, field.name) for field in fields(evidence)}
    return AcceptedReproduction(**held)
