"""The reading-only baseline: a role reads the code and states each Verdict itself.

It exists for the evaluation and for nothing else. There it sets aside, on purpose, the rule
that code computes every Verdict from recorded Evidence, so that the difference running code
makes is a measured number: the same cases, verified by reading alone, beside the system that
writes a Reproduction and runs it. It is this project's own stand-in for judging by reading,
and measures no other tool.

Nothing outside the evaluation can reach it. It is no `Orchestration`, no
`OrchestrationChoice` names it, and it is no configuration, so neither the command-line tool
nor the GitHub Action can select it: `orchestration_choice` and `configuration_named` refuse
any name given for it as they refuse any name they do not know. Wherever what it gave is
shown, it is named as the baseline's: each reason it gives starts with `BASELINE_PREFIX`, and a
comparison prints it under `BASELINE_LABEL` (`pr_verification.comparison`).
"""

from dataclasses import dataclass
from pathlib import Path

from pr_verification.agent import RoleRun
from pr_verification.core import PullRequest
from pr_verification.model import Model
from pr_verification.report import Report, Verdict
from pr_verification.structural import VERIFIER_STEP_CAP

BASELINE_PREFIX = "Reading-only baseline: "
"""What every reason the baseline gives starts with, so that no Verdict it stated can be
taken for one computed from Evidence."""

BASELINE_GAVE_NO_VERDICT = BASELINE_PREFIX + "the role finished without stating a Verdict."
"""The reason when the role's answer holds no Verdict that can be read."""

BASELINE_DID_NOT_FINISH = BASELINE_PREFIX + "the role was stopped before it finished ({stopped})."
"""The reason when the run ended any other way than by the role finishing. `stopped` is the
value of the run's `RoleRun.stopped`, as in `step_cap`."""

BASELINE_COULD_NOT_FINISH = BASELINE_PREFIX + "the role could not finish its work: {failure}"
"""The reason when the model failed. `failure` is what the `ModelFailed` said."""


@dataclass(frozen=True)
class StatedVerdict:
    """What the baseline's role said of one Claim. It is the model's word, and no Evidence."""

    verdict: Verdict
    """The Verdict the role stated, or Inconclusive when it stated none."""

    reason: str
    """Why, starting with `BASELINE_PREFIX`. After the prefix it may hold what the model
    wrote, and so Untrusted Content."""

    run: RoleRun | None
    """The record of the role's run, or None when the model failed before it ended."""


def state_verdict(
    claim: str,
    *,
    repository: Path,
    before: str,
    after: str,
    model: Model,
    step_cap: int = VERIFIER_STEP_CAP,
) -> StatedVerdict:
    """Have a role read the code before and after the pull request and state a Verdict for
    `claim` itself.

    Runs a role named `reading-only-baseline` with `run_role`. Its tools are the six
    read-only tools the Structural verifier has, and no other: those of
    `reading_tools(repository, before)` and of `reading_tools(repository, after)`, named and
    described as `verify_structural_claim` says, as in `before_search` and
    `after_read_file`. So it reads with the reading tools of the system it is compared
    with, for both commits, as the Structural verifier does: the Reproduction writer reads
    only the code after the pull request, and learns of the code before it by running its
    Reproduction there, which the baseline cannot. It writes no Reproduction and has
    nothing that runs or changes anything: no Sandbox is among the arguments. Its step cap
    is `step_cap`, and whoever compares gives the cap of the role it is compared with.
    Unless given it is `VERIFIER_STEP_CAP`, which is also what `WRITER_STEP_CAP` is today.
    Its task is `{"claim": claim}`, and it has no progress guard, so only `detect_repeat`
    watches it for loops.

    The role finishes with `{"verdict": "<verdict>", "reason": "<why>"}`, where the verdict
    is a value of `Verdict`. That is the `verdict` returned, whatever the code holds: here,
    and nowhere else in the project, a model's word is a Verdict. The `reason` returned is
    `BASELINE_PREFIX` followed by the role's `reason`.

    The Verdict is Inconclusive, and nothing is raised:

    - with the reason `BASELINE_GAVE_NO_VERDICT`, when the answer's `verdict` is not one of
      the three values, exactly as written, or its `reason` is not a string;
    - with `BASELINE_DID_NOT_FINISH` filled in, when the run ended without the role
      finishing: at the step cap, or stopped for going round in circles;
    - with `BASELINE_COULD_NOT_FINISH` filled in and no `run`, when `ModelFailed` is raised.

    Raises `ValueError` before any request when `step_cap` is less than 1. Any other
    exception is a fault in the system and is raised.
    """
    raise NotImplementedError


def reading_only_baseline(
    pull_request: PullRequest, model: Model, *, step_cap: int = VERIFIER_STEP_CAP
) -> Report:
    """Verify one pull request by reading alone, and return what a report would hold.

    It has the shape of the core's entry point, so the harness runs it on the same cases:
    `evaluate(cases, model, repetitions=..., entry_point=reading_only_baseline)`
    (`pr_verification.evaluation`). It is given what the system is given, so that only the
    way of verifying differs:

    - the same Claims: the commits are resolved and the Claims extracted as steps 1 and 2 of
      `run` (`pr_verification.core`) do, with `extract_claims`, from the title, the
      description, and the linked issue, by the model of the Claim extractor;
    - the same model: each Claim's role is run on `model_for(model,
      RoleName.REPRODUCTION_WRITER)`, the model that does the system's work on a
      Behavioural Claim, and the Claim extractor on its own, so a `RoleModels` is read as
      `run` reads it;
    - the same reading tools and the same step cap, as `state_verdict` says.

    Then `state_verdict` is called once for each Claim, in the order extracted. Nothing is
    classified, nothing is run in a Sandbox, and nothing is reviewed.

    The report holds one plain `Claim` for each Claim extracted, in that order, with the
    `verdict` and the `reason` of its `StatedVerdict` and no Evidence. Its `before` and
    `after` are the full commit IDs and its `findings` are empty. Its `budget_spend` is a
    `BudgetSpend` of the whole call: its `seconds` are the time the call took, its `dollars`
    the sum of what each request cost at the price of the model asked, with nothing for a
    model that has no price, and its `steps` the number of requests made to a model. No
    Budget is kept to: the step cap alone bounds each Claim.

    The report is for the harness to score and for nothing else. Its Gate follows from
    Verdicts that no Evidence bears out, and is never to be reported to a pipeline.

    Raises `InvalidPullRequest`, `ClaimExtractionFailed`, and `ModelFailed` as steps 1 and 2
    of `run` do, where there is no report to give. A model that fails on a Claim costs that
    Claim its Verdict and never the report. Raises `ValueError` as `state_verdict` does,
    before any request.
    """
    raise NotImplementedError
