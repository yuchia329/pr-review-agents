"""The structured report the core produces for one pull request."""

import json
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from enum import StrEnum

from pr_verification.ecosystem import Results, Unreadable
from pr_verification.sandbox import Command, RunResult


class Verdict(StrEnum):
    CONFIRMED = "confirmed"
    REFUTED = "refuted"
    INCONCLUSIVE = "inconclusive"


class ClaimKind(StrEnum):
    """The kind of a Claim, as the Claim classifier labelled it."""

    BEHAVIOURAL = "behavioural"
    """A Behavioural Claim."""

    STRUCTURAL = "structural"
    """A Structural Claim."""


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
class ReproductionEvidence(RunEvidence):
    """Evidence obtained by running one Reproduction against both commits.

    A `RunEvidence` whose `command` ran the Reproduction. In the printed report it appears
    among a Claim's `evidence` as a `RunEvidence` does, and with `path`, `reproduction`,
    `before_results`, and `after_results`, so that the Reproduction and its output on both
    commits can be read there. Results that were read appear as `passed`, `failed`, and
    `could_not_run`, results that could not be read as `reason`, and results never read as
    null.
    """

    path: str
    """Where the Reproduction was placed in the working copy, from its root."""
    reproduction: str
    """The text of the Reproduction, exactly as it was run. A model wrote it from what it
    read, so it is Untrusted Content."""
    before_results: Results | Unreadable | None = None
    """Which tests of the Reproduction passed and failed against the code before the pull
    request, as its Ecosystem read them: from the Reproduction's results file where the
    Ecosystem keeps one, and otherwise from `before`. None when they were never read. A
    Verdict is computed from these and from `after_results`, and from nothing else here."""
    after_results: Results | Unreadable | None = None
    """The same, against the code after the pull request."""


@dataclass(frozen=True)
class AcceptedReproduction(ReproductionEvidence):
    """Evidence obtained by running a Reproduction that the Reproduction judge accepted as a
    test of what its Claim says.

    A `ReproductionEvidence` with nothing added to it: what it says is in its type. A
    Reproduction the judge rejected, or was never asked about, is a `ReproductionEvidence`
    that is not one of these, and decides no Claim (see `compute_verdict`). No part of the
    system but `write_judged_reproduction` makes one. In the printed report it appears as a
    `ReproductionEvidence` does.
    """


@dataclass(frozen=True)
class ReadEvidence(Evidence):
    """Evidence obtained by reading some lines of one file of a commit.

    `source` is always `EvidenceSource.READ_CODE`. In the printed report it appears among a
    Claim's `evidence` with `source`, `observation`, `path`, `first_line`, `last_line`, and
    `excerpt`.
    """

    path: str
    """The file, by its path from the root of the repository."""
    first_line: int
    """The first line read, counted from 1."""
    last_line: int
    """The last line read."""
    excerpt: str
    """Those lines, as the commit holds them. Untrusted Content."""


class TextChange(StrEnum):
    """What a Structural Claim says a pull request does to one text of the code."""

    ADDED = "added"
    """The pull request adds the text: the code holds it after the pull request and did not
    before."""

    REMOVED = "removed"
    """The pull request removes the text: the code held it before the pull request and does
    not after."""


@dataclass(frozen=True)
class SearchEvidence(Evidence):
    """Evidence obtained by searching the code of both commits for one text.

    `source` is always `EvidenceSource.READ_CODE`. In the printed report it appears among a
    Claim's `evidence` with `source`, `observation`, `text`, `change`, `path`, `before_lines`,
    `after_lines`, and `after_lines_elsewhere`, so that every line a Verdict rests on can be
    read there.
    """

    text: str
    """The text searched for, exactly as written. The Structural verifier chose it from what
    it read, so it is Untrusted Content."""
    change: TextChange
    """What the Structural verifier took the Claim to say the pull request does to the text.
    It is a model's word about the Claim. What the code holds is not: that is in
    `before_lines`, `after_lines`, and `after_lines_elsewhere`, which code found."""
    path: str | None
    """The one file `before_lines` and `after_lines` were searched for in, by its path from
    the root of the repository, or None when every file was searched."""
    before_lines: tuple[str, ...]
    """Every line that holds the text in the code before the pull request, `Report.before`,
    each as `<path>:<line number>:<line>`, by path and then by line number. Untrusted
    Content."""
    after_lines: tuple[str, ...]
    """The same, in the code after the pull request, `Report.after`."""
    after_lines_elsewhere: tuple[str, ...] = ()
    """Every line that holds the text in the code after the pull request in a file other than
    the one at `path`, written and ordered as `after_lines` is. Code searches there only for a
    text said to be removed from one file: it is empty when `change` is `TextChange.ADDED`,
    and when `path` is None, every file being in `after_lines` already. The role chose the
    `path`, and these are the lines its choice left out. Untrusted Content."""


@dataclass(frozen=True)
class RemovedNameEvidence(Evidence):
    """Evidence obtained by searching the code after the pull request for one name that the
    Claim names and the pull request removes lines of.

    `names_removed` of `pr_verification.removed_names` says which names those are. Code
    finds the name and code searches for it: no model chooses it. `source` is always
    `EvidenceSource.READ_CODE`. In the printed report it appears among a Claim's `evidence`
    with `source`, `observation`, `name`, and `after_lines`.
    """

    name: str
    """The name, exactly as the code before the pull request writes it. It is taken from
    the pull request's code, so it is Untrusted Content."""
    after_lines: tuple[str, ...]
    """Every line of the code after the pull request, `Report.after`, that holds the name
    as a whole name, in any file, each as `<path>:<line number>:<line>`, by path and then by
    line number. Empty when no file holds the name any longer. Untrusted Content."""


@dataclass(frozen=True)
class Claim:
    text: str
    verdict: Verdict
    reason: str
    evidence: tuple[Evidence, ...] = ()


@dataclass(frozen=True)
class ClassifiedClaim(Claim):
    """A Claim the Claim classifier has labelled.

    In the printed report it appears as a `Claim` does, and with `kind`: `behavioural` or
    `structural`.
    """

    kind: ClaimKind = field(kw_only=True)
    """The Claim's kind."""


@dataclass(frozen=True)
class Finding:
    """Advisory only: a Finding never affects the Gate."""

    concern: str


class FindingKind(StrEnum):
    """What a Finding reports."""

    QUALITY = "quality"
    """A concern about the quality of a change, raised by the Reviewer."""

    UNDECLARED_CHANGE = "undeclared_change"
    """An Undeclared Change: a change that no Claim covers."""

    SUSPECTED_INJECTION = "suspected_injection"
    """Untrusted Content that reads as instructions aimed at an automated tool."""

    UNFINISHED_REVIEW = "unfinished_review"
    """The Review could not be finished, so it may have missed something."""


class PullRequestPart(StrEnum):
    """The part of a pull request that a Finding concerns."""

    TITLE = "title"
    """The pull request's title."""

    DESCRIPTION = "description"
    """The pull request's description."""

    LINKED_ISSUE = "linked_issue"
    """The text of the issue the pull request links to."""

    FILE = "file"
    """One file the pull request changes: the Finding's `path`."""

    SANDBOX_OUTPUT = "sandbox_output"
    """What was printed when code was run in the Sandbox for the pull request."""

    WHOLE = "whole"
    """The pull request as a whole, and no one part of it."""


@dataclass(frozen=True)
class ReviewFinding(Finding):
    """A Finding as a Review makes one: it says what it reports, and points to the part of the
    pull request it concerns.

    In the printed report it appears among `findings` with `concern`, `kind`, `part`, and
    `path`, where `path` is null unless `part` is `file`.
    """

    kind: FindingKind = field(kw_only=True)
    """What the Finding reports."""

    part: PullRequestPart = field(kw_only=True)
    """The part of the pull request the Finding concerns."""

    path: str | None = field(default=None, kw_only=True)
    """The file, by its path from the root of the repository, when `part` is `FILE`. None for
    every other part."""


class GateDecision(StrEnum):
    PASS = "pass"
    BLOCK = "block"


@dataclass(frozen=True)
class Gate:
    decision: GateDecision
    note: str


def compute_gate(verdicts: Iterable[Verdict]) -> Gate:
    """Block when, and only when, at least one Verdict is Refuted. No model has a say here.

    With no Verdicts the Gate passes, and its note says there was nothing to verify.
    """
    counts = Counter(verdicts)
    if not counts:
        return Gate(GateDecision.PASS, "No Claims found: nothing to verify.")
    decision = GateDecision.BLOCK if counts[Verdict.REFUTED] else GateDecision.PASS
    tally = ", ".join(f"{counts[verdict]} {verdict.capitalize()}" for verdict in Verdict)
    return Gate(decision, f"Claims: {tally}.")


class BudgetLimit(StrEnum):
    """One of the three limits of a Budget, and of a Claim's share of it."""

    TIME = "time"
    """The limit on seconds."""

    MONEY = "money"
    """The limit on dollars."""

    STEPS = "steps"
    """The limit on steps."""


@dataclass(frozen=True)
class BudgetSpend:
    """An amount of effort, in the three units a Budget is measured in."""

    seconds: float
    dollars: float
    steps: int


@dataclass(frozen=True)
class ClaimSpend(BudgetSpend):
    """What was spent on one Claim, out of its share of the Budget.

    In the printed report it appears with `seconds`, `dollars`, `steps`, and `stopped_by`, which
    is `time`, `money`, `steps`, or null.
    """

    stopped_by: BudgetLimit | None = None
    """The limit of the Claim's share that was reached, after which no further work was done
    for the Claim, or None when its share was not spent. It says nothing of the Verdict: a
    Claim whose Evidence came from the very work that reached the limit is still decided."""


@dataclass(frozen=True)
class PullRequestSpend(BudgetSpend):
    """What was spent on one pull request in all, and on each of its Claims.

    `seconds`, `dollars`, and `steps` are for the whole pull request: the work on every Claim,
    and the work that was for no one Claim, such as listing the Claims. Its `seconds` are
    seconds on the clock during which work was under way, in which work done at the same time
    counts once, and those of each `ClaimSpend` are seconds of work, as `pr_verification.budget`
    says. In the printed report it is the report's `budget_spend`, with `seconds`, `dollars`,
    `steps`, `stopped_by`, which is `time`, `money`, `steps`, or null, and `claims`, a list
    holding what a `ClaimSpend` prints for each Claim.
    """

    stopped_by: BudgetLimit | None = None
    """The limit of the Budget that stopped the run: the first one reached, after which no
    further work was started. None when the run ended with no limit reached."""

    claims: tuple[ClaimSpend, ...] = ()
    """The spend on each Claim: one for each of the report's Claims, in their order."""


@dataclass(frozen=True)
class Report:
    before: str
    after: str
    claims: tuple[Claim, ...]
    findings: tuple[Finding, ...]
    budget_spend: BudgetSpend
    """What the pull request cost. The core's entry point gives a `PullRequestSpend`."""

    @property
    def gate(self) -> Gate:
        """Derived, never stored, so a report cannot carry a Gate its Verdicts do not give."""
        return compute_gate(claim.verdict for claim in self.claims)

    def to_json(self) -> str:
        return json.dumps({"gate": asdict(self.gate), **asdict(self)}, indent=2)


@dataclass(frozen=True)
class LoopCounts:
    """How the loop ladder was used in some work: how many loops were detected, and what came
    of them.

    A loop is one run of a role in which loop detection triggered: a `Loop`, of
    `pr_verification.agent`. `count_loops`, of `pr_verification.core`, counts every loop
    detected under exactly one of the three outcomes and states which. So in the counts it
    gives, and in any sum of them,
    `recovered_after_told + recovered_after_replanning + ended_inconclusive` is `detected`.

    In the printed report it appears with `detected`, `recovered_after_told`,
    `recovered_after_replanning`, and `ended_inconclusive`.
    """

    detected: int = 0
    """How many loops were detected."""

    recovered_after_told: int = 0
    """Loops whose role was told and was not found going round again, and whose Claim was
    decided, whether by that work or by work dispatched for it afterwards."""

    recovered_after_replanning: int = 0
    """Loops whose role was stopped and whose Claim, handed back to the supervisor, was
    decided by work dispatched for it afterwards."""

    ended_inconclusive: int = 0
    """Loops whose Claim was given an Inconclusive Verdict."""


@dataclass(frozen=True)
class ReportWithLoops(Report):
    """A report that also records how the loop ladder was used in the run.

    In the printed report it appears as a `Report` does, and with `loops`, as `LoopCounts`
    is printed.
    """

    loops: LoopCounts
    """The loops detected in the work done for the Claims of the pull request, and what came
    of them. The core's entry point says what is counted."""
