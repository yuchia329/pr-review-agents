"""The structured report the core produces for one pull request."""

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from enum import StrEnum

from pr_verification.sandbox import Command, RunResult


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class EvidenceSource(StrEnum):
    RAN_CODE = "ran_code"
    READ_CODE = "read_code"


@dataclass(frozen=True)
class Evidence:
    source: EvidenceSource
    observation: str


@dataclass(frozen=True)
class RunEvidence(Evidence):
    """Evidence obtained by running one command in the Sandbox against both commits.

    `source` is always `EvidenceSource.RAN_CODE`. In the printed report it appears among a
    Claim's `evidence` with `source`, `observation`, `command`, `before`, and `after`, where
    `before` and `after` each hold `output`, `exit_status`, and `limit_hit`. The outputs are
    Untrusted Content.
    """

    command: Command
    """The command that was run against both commits."""
    before: RunResult
    """What the command did against the code before the pull request, `Report.before`."""
    after: RunResult
    """What the command did against the code after the pull request, `Report.after`."""


@dataclass(frozen=True)
class Claim:
    text: str
    verdict: Verdict
    reason: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class Finding:
    """Advisory only: a Finding never affects the Gate."""

    concern: str


class GateDecision(StrEnum):
    PASS = "pass"
    BLOCK = "block"


@dataclass(frozen=True)
class Gate:
    decision: GateDecision
    note: str


def compute_gate(verdicts: Iterable[Verdict]) -> Gate:
    """Block only when at least one Claim is Refuted. No model has a say here."""
    counts = Counter(verdicts)
    if not counts:
        return Gate(GateDecision.PASS, "No Claims found: nothing to verify.")
    decision = GateDecision.BLOCK if counts[Verdict.REFUTED] else GateDecision.PASS
    tally = ", ".join(f"{counts[verdict]} {verdict.capitalize()}" for verdict in Verdict)
    return Gate(decision, f"Claims: {tally}.")


@dataclass(frozen=True)
class BudgetSpend:
    seconds: float
    dollars: float
    steps: int


@dataclass(frozen=True)
class Report:
    before: str
    after: str
    claims: tuple[Claim, ...]
    findings: tuple[Finding, ...]
    budget_spend: BudgetSpend

    @property
    def gate(self) -> Gate:
        """Derived, never stored, so a report cannot carry a Gate its Verdicts do not give."""
        return compute_gate(claim.verdict for claim in self.claims)

    def to_json(self) -> str:
        return json.dumps({"gate": asdict(self.gate), **asdict(self)}, indent=2)
