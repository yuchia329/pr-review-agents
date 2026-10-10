"""The evaluation harness: runs cases with known answers through the core's entry point.

It is a measurement, not a pass-or-fail test suite. It reports False Blocks, the Decided Rate,
and False Assurance, for the development part and the held-out part separately, over repeated
runs so that variation between runs is not mistaken for improvement.
"""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pr_verification.core import PullRequest, run
from pr_verification.model import Model
from pr_verification.report import Report, Verdict

EntryPoint = Callable[[PullRequest, Model], Report]
"""The shape of the core's entry point, `pr_verification.core.run`."""


class Part(StrEnum):
    """The part of the evaluation set a case belongs to. A case is in exactly one."""

    DEVELOPMENT = "development"
    """Cases the system may be tuned on."""

    HELD_OUT = "held_out"
    """Cases kept back, so that reported numbers are not the ones tuned on."""


@dataclass(frozen=True)
class Case:
    """A pull request whose Claims have a known answer.

    A case is written so that its pull request makes at least one Claim, and so that every
    Claim it makes has the same answer: all true or all false. The system splits the text
    into Claims in its own words, so the answer is held for the pull request and applies to
    each Claim found in it.
    """

    name: str
    """What the case is called in the results. No two cases in one evaluation share a name."""

    part: Part
    """The part this case belongs to."""

    pull_request: PullRequest
    """The repository, the two commits, and the pull request text, as the core takes them.
    This is all of the case the system is shown."""

    expected: Verdict
    """The Verdict each Claim of this pull request should get: Confirmed when its Claims are
    true, Refuted when they are false. Never Inconclusive."""


@dataclass(frozen=True)
class CaseRun:
    """What one run of one case through the entry point gave."""

    case: str
    """The case's name."""

    repetition: int
    """Which run of the case this is, counted from 1."""

    expected: Verdict
    """The case's expected Verdict."""

    verdicts: tuple[Verdict, ...]
    """The Verdict of each Claim in the report, in the report's order. Empty when the report
    has no Claims and when there is no report."""

    seconds: float | None
    """The time the report says was spent, or None when there is no report."""

    dollars: float | None
    """The money the report says was spent, or None when there is no report."""

    no_report: str | None
    """Why the run gave no report, as the error raised said it, or None when it gave one."""


@dataclass(frozen=True)
class Scores:
    """Counts over some runs, and the rates they give. Every rate is derived, never stored.

    Each Verdict given to a Claim is scored once against its case's expected Verdict:

    - expected Confirmed, given Confirmed: decided;
    - expected Refuted, given Refuted: decided;
    - expected Confirmed, given Refuted: a False Block;
    - expected Refuted, given Confirmed: a False Assurance;
    - given Inconclusive, whatever was expected: inconclusive.

    A run with no Verdicts, whether its report has no Claims or it gave no report, is scored as
    one Claim left Inconclusive: every case makes at least one Claim, so that run left at
    least one undecided.

    So `decided + false_blocks + false_assurances + inconclusive` equals `claims`, which
    equals `true_claims + false_claims`. A rate whose denominator is 0 is 0.0.
    """

    runs: int
    """How many runs were scored: one per pull request per repetition."""

    true_claims: int
    """Claims scored whose expected Verdict is Confirmed."""

    false_claims: int
    """Claims scored whose expected Verdict is Refuted."""

    decided: int
    """Claims given the expected Verdict."""

    false_blocks: int
    """True Claims given a Refuted Verdict."""

    false_assurances: int
    """False Claims given a Confirmed Verdict."""

    inconclusive: int
    """Claims given an Inconclusive Verdict, with one for each run that gave no Verdicts."""

    seconds_per_pull_request: float | None
    """The mean of `CaseRun.seconds` over the runs where it is known, or None when it is
    known for none."""

    dollars_per_pull_request: float | None
    """The mean of `CaseRun.dollars` over the runs where it is known, or None when it is
    known for none."""

    @property
    def claims(self) -> int:
        """`true_claims + false_claims`."""
        raise NotImplementedError

    @property
    def decided_rate(self) -> float:
        """The Decided Rate: `decided / claims`."""
        raise NotImplementedError

    @property
    def false_block_rate(self) -> float:
        """`false_blocks / true_claims`: the share of true Claims that were Refuted."""
        raise NotImplementedError

    @property
    def false_assurance_rate(self) -> float:
        """`false_assurances / false_claims`: the share of false Claims that were Confirmed."""
        raise NotImplementedError


def score(runs: Iterable[CaseRun]) -> Scores:
    """Score `runs` by the rule `Scores` states. No runs give all counts 0 and both means None.

    Raises `ValueError` when a run's `expected` is Inconclusive.
    """
    raise NotImplementedError


@dataclass(frozen=True)
class PartEvaluation:
    """The runs of the cases of one part, and what they show."""

    runs: tuple[CaseRun, ...]
    """Every run of the part's cases, in the order they were run."""

    @property
    def scores(self) -> Scores:
        """`score` over all the runs together."""
        raise NotImplementedError

    @property
    def by_repetition(self) -> tuple[Scores, ...]:
        """`score` over the runs of each repetition alone, repetition 1 first: one for each
        repetition the runs hold, and none when there are no runs. The spread between them is
        the variation between runs."""
        raise NotImplementedError

    @property
    def varying(self) -> tuple[str, ...]:
        """The names of the cases whose runs did not all give the same Verdicts the same
        number of times, in the order the cases were first run. The order of the Verdicts
        within a run does not matter."""
        raise NotImplementedError


@dataclass(frozen=True)
class Evaluation:
    """What one evaluation measured. The two parts are never added together."""

    repetitions: int
    """How many times each case was run."""

    development: PartEvaluation
    """The cases of `Part.DEVELOPMENT`."""

    held_out: PartEvaluation
    """The cases of `Part.HELD_OUT`."""

    def to_json(self) -> str:
        """The evaluation as a JSON object with `repetitions`, `development`, and `held_out`.

        Each part holds `scores`, `by_repetition` (a list), and `varying` (a list of names).
        Each scores object holds every field of `Scores` under its own name, and `claims`,
        `decided_rate`, `false_block_rate`, and `false_assurance_rate`. A mean that is not
        known is null.
        """
        raise NotImplementedError


def evaluate(
    cases: Iterable[Case],
    model: Model,
    *,
    repetitions: int = 1,
    entry_point: EntryPoint = run,
) -> Evaluation:
    """Run each case `repetitions` times through the entry point and score what comes back.

    `entry_point` is `pr_verification.core.run` unless another is given: the same function
    the command-line tool calls. Every case is run once in the order given, then every case
    again, until each has run `repetitions` times. A run is `entry_point(case.pull_request,
    model)`: nothing else of the case is passed on.

    Each run becomes one `CaseRun`, in the `PartEvaluation` of its case's part, with
    `verdicts` taken from the report's Claims and `seconds` and `dollars` from its
    `budget_spend`. A run that raises
    `InvalidPullRequest`, `ModelFailed`, or `ClaimExtractionFailed`, the errors for which the
    command-line tool prints no report, is recorded with `no_report` and the evaluation
    carries on. Any other exception is raised.

    Raises `ValueError` before running anything when `repetitions` is less than 1, when two
    cases share a name, or when a case expects Inconclusive.
    """
    raise NotImplementedError


def hand_made_cases(directory: Path) -> tuple[Case, ...]:
    """A handful of small hand-made cases, so the harness can run against the core as it is.

    Builds each case's repository in a directory of its own under `directory`, which must
    exist, and returns the cases:

    - there are between 3 and 10, and every name is different and starts with `hand-made/`;
    - at least one is in each `Part`;
    - at least one expects Confirmed and at least one expects Refuted;
    - each pull request's two commits are in its repository and differ, and its text states
      at least one Behavioural Claim.

    Called again with another directory, it gives the same names, parts, pull request text,
    and expected Verdicts, in the same order.
    """
    raise NotImplementedError
