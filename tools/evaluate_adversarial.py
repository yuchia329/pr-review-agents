"""The False Assurance rate on the adversarial cases.

A measurement, not a test. It runs the adversarial cases through the core's entry point, with
the model, the decider, and the Budget the environment configures, and prints as JSON what the
evaluation harness scored, the same scores for each placement of the manipulation, and every
Claim that was given Confirmed:

    uv run --extra laya python tools/evaluate_adversarial.py --repetitions 3

Only the development part is run unless `--held-out` is given, so that the held-out part is
not looked at while the system is being tuned. The model is configured as
`docs/model-endpoints.md` describes and the decider as `docs/decision-layer.md` does.

Every case's Claim is false, so every Confirmed is printed with its Claim's text, which shows
whether it was the case's own Claim or a Claim the system made of the manipulation.

`--case` runs the named cases alone, so that several invocations can share the cases out and
run at once against an endpoint that serves requests in parallel. `--answer-wait` sets how long
one answer of the model is waited for, in place of the client's five minutes: a slow endpoint
serving a model that thinks at length can need longer for one answer, and the wait is there to
catch a request that hangs, which is no part of what is measured here.

The orchestration is the one `PR_VERIFICATION_ORCHESTRATION` chooses, as for the command line:
the fixed pipeline unless it says `supervisor`. Each run of the supervisor is given a state
directory of its own, so that nothing is kept from one run to the next, as the command line
does without `--state-dir`.

Every Claim of every run is printed too, with its reason, how many Reproductions were run for it,
how many of those the Reproduction judge accepted, and the limit of the Budget that stopped the
work on it. A rate of no False Assurances carries weight only when Reproductions were put to
the judge, and this is where that is read.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from pr_verification.adversarial_cases import PlacedIn, adversarial_cases
from pr_verification.budget import budget_from_environment
from pr_verification.core import PullRequest, run
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import CaseRun, Part, PartEvaluation, evaluate, score
from pr_verification.model import Model, ModelFailed
from pr_verification.openai_compatible import model_from_environment
from pr_verification.orchestration import OrchestrationChoice, orchestration_choice
from pr_verification.report import (
    AcceptedReproduction,
    Claim,
    PullRequestSpend,
    Report,
    ReproductionEvidence,
    Verdict,
)
from pr_verification.supervisor import supervisor


def by_placement(part: PartEvaluation) -> dict[str, dict[str, Any]]:
    """The scores of the part's runs for each placement of the manipulation."""

    def scored(runs: list[CaseRun]) -> dict[str, Any]:
        scores = score(runs)
        return {
            "runs": scores.runs,
            "runs_without_verdicts": scores.runs_without_verdicts,
            "false_claims": scores.false_claims,
            "decided": scores.decided,
            "inconclusive": scores.inconclusive,
            "false_assurances": scores.false_assurances,
            "false_assurance_rate": scores.false_assurance_rate,
        }

    return {
        placement.value: scored(
            [case_run for case_run in part.runs if case_run.name.endswith(f"/{placement.value}")]
        )
        for placement in PlacedIn
    }


REFUSED = ("HTTP 401", "HTTP 402", "HTTP 403")
"""What a provider answers when the key is wrong or the account is out of money. Every later
request would be answered the same, and the runs would count as runs with no report."""


FAILURES_IN_A_ROW = 3
"""How many runs in a row may end with the model failed before the measurement is ended. One
is a slow answer. Three is a provider that is down or a network that is gone."""


NOT_PREPARED = "could not be prepared for running"
"""What a Claim's reason says when the Sandbox could not install what the code needs, as when
the network is gone. No Reproduction is run for such a Claim, so it was not put to the test."""


def stop_if_refused(case: str, said: str) -> None:
    """End the measurement when the provider refused the account, not the request."""
    if any(status in said for status in REFUSED):
        sys.exit(f"{case}: the provider refused the account, so nothing more is run: {said}")


def claims_of(case: str, report: Report) -> list[dict[str, Any]]:
    """Each Claim of one run, with how far the work on it got."""
    spend = report.budget_spend
    spent = spend.claims if isinstance(spend, PullRequestSpend) else ()

    def told(index: int, claim: Claim) -> dict[str, Any]:
        reproductions = [e for e in claim.evidence if isinstance(e, ReproductionEvidence)]
        stopped_by = spent[index].stopped_by if index < len(spent) else None
        return {
            "case": case,
            "claim": claim.text,
            "verdict": claim.verdict.value,
            "reason": claim.reason,
            "reproductions": len(reproductions),
            "accepted": sum(isinstance(e, AcceptedReproduction) for e in reproductions),
            "stopped_by": stopped_by.value if stopped_by is not None else None,
        }

    return [told(index, claim) for index, claim in enumerate(report.claims)]


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--held-out", action="store_true", help="run the held-out part as well")
    parser.add_argument("--case", action="append", help="run this case alone; may be repeated")
    parser.add_argument("--answer-wait", type=float, help="seconds to wait for one answer")
    arguments = parser.parse_args()

    model = model_from_environment(os.environ)
    if model is None:
        parser.error("no model is configured: see docs/model-endpoints.md")
    if arguments.answer_wait is not None:
        # The client has no setting for it, since nothing but a measurement needs one.
        model._client.timeout = httpx.Timeout(arguments.answer_wait, connect=10.0)
    decider = configured_decider(decision_configuration(os.environ), model)
    budget = budget_from_environment(os.environ)
    choice = orchestration_choice(os.environ)
    confirmed: list[dict[str, str]] = []
    claims: list[dict[str, Any]] = []
    failed_in_a_row: list[str] = []
    not_prepared_in_a_row: list[str] = []
    names: dict[Path, str] = {}

    def recorded_run(pull_request: PullRequest, model: Model) -> Report:
        try:
            if choice is OrchestrationChoice.SUPERVISOR:
                with tempfile.TemporaryDirectory() as state:
                    orchestration = supervisor(Path(state), progress_guard=decider)
                    report = run(
                        pull_request, model, decider, orchestration=orchestration, budget=budget
                    )
            else:
                report = run(pull_request, model, decider, budget=budget)
        except ModelFailed as error:
            stop_if_refused(names[pull_request.repository], str(error))
            failed_in_a_row.append(str(error))
            if len(failed_in_a_row) == FAILURES_IN_A_ROW:
                sys.exit(f"the model failed in {FAILURES_IN_A_ROW} runs in a row: {error}")
            raise
        failed_in_a_row.clear()
        given = ", ".join(claim.verdict.value for claim in report.claims) or "no Claims"
        # A run can take minutes, so each is told as it ends, apart from what is measured.
        print(f"{names[pull_request.repository]}: {given}", file=sys.stderr, flush=True)
        told = claims_of(names[pull_request.repository], report)
        claims.extend(told)
        for claim in told:
            print(json.dumps(claim), file=sys.stderr, flush=True)
            stop_if_refused(claim["case"], claim["reason"])
        if told and all(NOT_PREPARED in claim["reason"] for claim in told):
            not_prepared_in_a_row.append(names[pull_request.repository])
            if len(not_prepared_in_a_row) == FAILURES_IN_A_ROW:
                sys.exit(f"the code {NOT_PREPARED} in {FAILURES_IN_A_ROW} runs in a row")
        else:
            not_prepared_in_a_row.clear()
        decided = [claim for claim in report.claims if claim.verdict is not Verdict.INCONCLUSIVE]
        confirmed.extend(
            {"case": names[pull_request.repository], "claim": claim.text, "reason": claim.reason}
            for claim in decided
            if claim.verdict is Verdict.CONFIRMED
        )
        # What decided a Claim is what to read: the accepted Reproduction, and how it ran.
        for one in decided:
            for evidence in one.evidence:
                if isinstance(evidence, AcceptedReproduction):
                    print(
                        json.dumps(
                            {
                                "case": names[pull_request.repository],
                                "claim": one.text,
                                "verdict": one.verdict.value,
                                "reproduction": evidence.reproduction,
                                "before": evidence.before.output[-2000:],
                                "after": evidence.after.output[-2000:],
                            }
                        ),
                        file=sys.stderr,
                        flush=True,
                    )
        return report

    with tempfile.TemporaryDirectory() as directory:
        cases = [
            case
            for case in adversarial_cases(Path(directory))
            if (arguments.held_out or case.part is Part.DEVELOPMENT)
            and (arguments.case is None or case.name in arguments.case)
        ]
        if arguments.case is not None and len(cases) != len(set(arguments.case)):
            parser.error("a --case is not the name of a case of the parts being run")
        names.update({case.pull_request.repository: case.name for case in cases})
        evaluation = evaluate(
            cases, model, repetitions=arguments.repetitions, entry_point=recorded_run
        )

    parts = {"development": evaluation.development, "held_out": evaluation.held_out}
    measured = {
        "orchestration": choice.value,
        "evaluation": json.loads(evaluation.to_json()),
        "by_placement": {name: by_placement(part) for name, part in parts.items()},
        "confirmed": confirmed,
        "claims": claims,
        "no_report": [
            {"case": case_run.name, "repetition": case_run.repetition, "why": case_run.no_report}
            for part in parts.values()
            for case_run in part.runs
            if case_run.no_report is not None
        ],
    }
    print(json.dumps(measured, indent=2))


if __name__ == "__main__":
    main()
