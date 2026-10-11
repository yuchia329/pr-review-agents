"""The evaluation harness: runs cases with known answers through the core's entry point.

It is a measurement, not a pass-or-fail test suite. It reports False Blocks, the Decided Rate,
and False Assurance, for the development part and the held-out part separately, over repeated
runs so that variation between runs is not mistaken for improvement.
"""

import json
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from statistics import fmean
from time import perf_counter
from typing import Any

from pr_verification.claims import ClaimExtractionFailed
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import Decider, Question
from pr_verification.model import Model, ModelFailed
from pr_verification.report import BudgetSpend, Report, Verdict
from pr_verification.repository import commit_staged, git, new_repository

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

    name: str
    """The case's name."""

    repetition: int
    """Which run of the case this is, counted from 1."""

    expected: Verdict
    """The case's expected Verdict."""

    verdicts: tuple[Verdict, ...]
    """The Verdict of each Claim in the report, in the report's order. Empty when the report
    has no Claims and when there is no report."""

    spend: BudgetSpend | None
    """The time and money the report says were spent, or None when there is no report."""

    no_report: str | None
    """Why the run gave no report, as the error raised said it, or None when it gave one.
    It may quote a model's response, so it is Untrusted Content."""


@dataclass(frozen=True)
class Scores:
    """Counts over some runs, and the rates they give. Every rate is derived, never stored.

    Each Verdict given to a Claim is scored once against its case's expected Verdict:

    - expected Confirmed, given Confirmed: decided;
    - expected Refuted, given Refuted: decided;
    - expected Confirmed, given Refuted: a False Block;
    - expected Refuted, given Confirmed: a False Assurance;
    - given Inconclusive, whatever was expected: inconclusive.

    So `decided + false_blocks + false_assurances + inconclusive` equals `claims`, which
    equals `true_claims + false_claims`.

    A run that gave no Verdicts, whether its report has no Claims or it gave no report, adds
    no Claim to these counts. It is counted in `runs_without_verdicts`, so that a system
    that finds no Claims is seen, and is not scored as if nothing were wrong.

    A rate whose denominator is 0 is None: there is nothing to take a share of.
    """

    runs: int
    """How many runs were scored: one per pull request per repetition."""

    runs_without_verdicts: int
    """How many of those runs gave no Verdicts."""

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
    """Claims given an Inconclusive Verdict."""

    seconds_per_pull_request: float | None
    """The mean of `CaseRun.spend.seconds` over the runs where it is known, or None when it
    is known for none."""

    dollars_per_pull_request: float | None
    """The mean of `CaseRun.spend.dollars` over the runs where it is known, or None when it
    is known for none."""

    @property
    def claims(self) -> int:
        """`true_claims + false_claims`."""
        return self.true_claims + self.false_claims

    @property
    def decided_rate(self) -> float | None:
        """The Decided Rate: `decided / claims`."""
        return _share(self.decided, self.claims)

    @property
    def false_block_rate(self) -> float | None:
        """`false_blocks / true_claims`: the share of true Claims that were Refuted."""
        return _share(self.false_blocks, self.true_claims)

    @property
    def false_assurance_rate(self) -> float | None:
        """`false_assurances / false_claims`: the share of false Claims that were Confirmed."""
        return _share(self.false_assurances, self.false_claims)


def score(runs: Iterable[CaseRun]) -> Scores:
    """Score `runs` by the rule `Scores` states. No runs give all counts 0 and both means None.

    Raises `ValueError` when a run's `expected` is Inconclusive.
    """
    scored = tuple(runs)
    for case_run in scored:
        _must_expect_an_answer(case_run.name, case_run.expected)
    given = [(case_run.expected, verdict) for case_run in scored for verdict in case_run.verdicts]
    spends = [case_run.spend for case_run in scored if case_run.spend is not None]
    return Scores(
        runs=len(scored),
        runs_without_verdicts=sum(not case_run.verdicts for case_run in scored),
        true_claims=sum(expected is Verdict.CONFIRMED for expected, _ in given),
        false_claims=sum(expected is Verdict.REFUTED for expected, _ in given),
        decided=sum(verdict is expected for expected, verdict in given),
        false_blocks=given.count((Verdict.CONFIRMED, Verdict.REFUTED)),
        false_assurances=given.count((Verdict.REFUTED, Verdict.CONFIRMED)),
        inconclusive=sum(verdict is Verdict.INCONCLUSIVE for _, verdict in given),
        seconds_per_pull_request=_mean([spend.seconds for spend in spends]),
        dollars_per_pull_request=_mean([spend.dollars for spend in spends]),
    )


def _share(count: int, out_of: int) -> float | None:
    return count / out_of if out_of else None


def _mean(known: list[float]) -> float | None:
    return fmean(known) if known else None


def _must_expect_an_answer(name: str, expected: Verdict) -> None:
    if expected is Verdict.INCONCLUSIVE:
        raise ValueError(f"{name} expects Inconclusive, which is not an answer to score against")


@dataclass(frozen=True)
class PartEvaluation:
    """The runs of the cases of one part, and what they show."""

    runs: tuple[CaseRun, ...]
    """Every run of the part's cases, in the order they were run."""

    @property
    def scores(self) -> Scores:
        """`score` over all the runs together."""
        return score(self.runs)

    @property
    def by_repetition(self) -> tuple[Scores, ...]:
        """`score` over the runs of each repetition alone, repetition 1 first: one for each
        repetition the runs hold, and none when there are no runs. The spread between them is
        the variation between runs."""
        return tuple(
            score(case_run for case_run in self.runs if case_run.repetition == repetition)
            for repetition in sorted({case_run.repetition for case_run in self.runs})
        )

    @property
    def varying(self) -> tuple[str, ...]:
        """The names of the cases whose runs did not all give the same Verdicts the same
        number of times, in the order the cases were first run. The order of the Verdicts
        within a run does not matter."""
        given: dict[str, set[tuple[Verdict, ...]]] = {}
        for case_run in self.runs:
            given.setdefault(case_run.name, set()).add(tuple(sorted(case_run.verdicts)))
        return tuple(name for name, different in given.items() if len(different) > 1)


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
        `decided_rate`, `false_block_rate`, and `false_assurance_rate`. A rate or a mean that
        is None is null.
        """
        printed = {
            "repetitions": self.repetitions,
            "development": _printed_part(self.development),
            "held_out": _printed_part(self.held_out),
        }
        return json.dumps(printed, indent=2)


def _printed_part(part: PartEvaluation) -> dict[str, Any]:
    return {
        "scores": _printed_scores(part.scores),
        "by_repetition": [_printed_scores(scores) for scores in part.by_repetition],
        "varying": list(part.varying),
    }


def _printed_scores(scores: Scores) -> dict[str, Any]:
    return {
        **asdict(scores),
        "claims": scores.claims,
        "decided_rate": scores.decided_rate,
        "false_block_rate": scores.false_block_rate,
        "false_assurance_rate": scores.false_assurance_rate,
    }


def evaluate(
    cases: Iterable[Case],
    model: Model,
    *,
    repetitions: int,
    entry_point: EntryPoint = run,
) -> Evaluation:
    """Run each case `repetitions` times through the entry point and score what comes back.

    `entry_point` is `pr_verification.core.run` unless another is given: the same function
    the command-line tool calls. Every case is run once in the order given, then every case
    again, until each has run `repetitions` times. A run is `entry_point(case.pull_request,
    model)`: nothing else of the case is passed on. No decider is passed either, so to
    evaluate with Claims classified, give an `entry_point` that has one bound to it.

    Each run becomes one `CaseRun`, in the `PartEvaluation` of its case's part, with
    `verdicts` taken from the report's Claims and `spend` from its `budget_spend`.

    A run that raises `InvalidPullRequest`, `ModelFailed`, or `ClaimExtractionFailed` is
    recorded with `no_report` and the evaluation carries on: these say that one pull request
    could not be verified. Any other exception is a fault in the system and is raised.

    Raises `ValueError` before running anything when `repetitions` is less than 1, when two
    cases share a name, or when a case expects Inconclusive.
    """
    to_run = tuple(cases)
    if repetitions < 1:
        raise ValueError(f"each case must be run at least once, not {repetitions} times")
    shared = [name for name, count in Counter(case.name for case in to_run).items() if count > 1]
    if shared:
        raise ValueError(f"more than one case is named {', '.join(shared)}")
    for case in to_run:
        _must_expect_an_answer(case.name, case.expected)
    runs: dict[Part, list[CaseRun]] = {part: [] for part in Part}
    for repetition in range(1, repetitions + 1):
        for case in to_run:
            runs[case.part].append(_run_once(case, repetition, model, entry_point))
    return Evaluation(
        repetitions=repetitions,
        development=PartEvaluation(tuple(runs[Part.DEVELOPMENT])),
        held_out=PartEvaluation(tuple(runs[Part.HELD_OUT])),
    )


def _run_once(case: Case, repetition: int, model: Model, entry_point: EntryPoint) -> CaseRun:
    try:
        report = entry_point(case.pull_request, model)
    except (InvalidPullRequest, ModelFailed, ClaimExtractionFailed) as error:
        return CaseRun(case.name, repetition, case.expected, (), None, str(error))
    verdicts = tuple(claim.verdict for claim in report.claims)
    return CaseRun(case.name, repetition, case.expected, verdicts, report.budget_spend, None)


@dataclass(frozen=True)
class _HandMade:
    """One hand-made case: a module as it was, as the pull request leaves it, and what it says.

    The text says only what the code now does, so that every Claim found in it has the one
    answer: a true account of what the code did before would be a true Claim in a false case.
    """

    name: str
    part: Part
    expected: Verdict
    title: str
    description: str
    module: str
    before: str
    after: str

    def built_in(self, directory: Path) -> Case:
        """The case, with its repository made in a directory of its own under `directory`."""
        repository = new_repository(directory)

        def commit(text: str, message: str, *parents: str) -> str:
            blob = git(repository, "hash-object", "-w", "--stdin", stdin=text.encode())
            entry = f"100644,{blob.decode().strip()},{self.module}"
            git(repository, "update-index", "--add", "--cacheinfo", entry)
            return commit_staged(repository, message, *parents)

        before = commit(self.before, "Start the project")
        after = commit(self.after, self.title, before)
        pull_request = PullRequest(repository, before, after, self.title, self.description)
        return Case(self.name, self.part, pull_request, self.expected)


_HAND_MADE = (
    _HandMade(
        name="hand-made/median-of-an-even-count",
        part=Part.DEVELOPMENT,
        expected=Verdict.CONFIRMED,
        title="Fix the median of an even number of values",
        description="median([1, 2, 3, 4]) now returns 2.5, the mean of the two middle values.",
        module="statistics_tools.py",
        before="""\
def median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]
""",
        after="""\
def median(values):
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2
""",
    ),
    # The pull request only names a value: the code does what it did before.
    _HandMade(
        name="hand-made/initials-of-a-hyphenated-name",
        part=Part.DEVELOPMENT,
        expected=Verdict.REFUTED,
        title="Give an initial for each part of a hyphenated name",
        description='initials("Mary-Jane Watson") now returns "MJW".',
        module="names.py",
        before="""\
def initials(name):
    return "".join(word[0].upper() for word in name.split())
""",
        after="""\
def initials(name):
    words = name.split()
    return "".join(word[0].upper() for word in words)
""",
    ),
    _HandMade(
        name="hand-made/century-that-is-not-a-leap-year",
        part=Part.HELD_OUT,
        expected=Verdict.CONFIRMED,
        title="Stop counting 1900 as a leap year",
        description="is_leap_year(1900) now returns False.",
        module="calendar_tools.py",
        before="""\
def is_leap_year(year):
    return year % 4 == 0
""",
        after="""\
def is_leap_year(year):
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)
""",
    ),
    # The pull request does change what the code does, and not to what it says.
    _HandMade(
        name="hand-made/clamp-above-the-upper-bound",
        part=Part.HELD_OUT,
        expected=Verdict.REFUTED,
        title="Make clamp() return the upper bound for a value above it",
        description="clamp(12, 0, 10) now returns 10.",
        module="ranges.py",
        before="""\
def clamp(value, low, high):
    return max(low, value)
""",
        after="""\
def clamp(value, low, high):
    return max(low, min(value, low))
""",
    ),
)


def hand_made_cases(directory: Path) -> tuple[Case, ...]:
    """A handful of small hand-made cases, so the harness can run against the core as it is.

    Each pull request is a small change to a Python repository whose text states a
    Behavioural Claim. Builds each case's repository in a directory of its own under
    `directory`, which must exist, and returns the cases:

    - there are between 3 and 10, and every name is different and starts with `hand-made/`;
    - at least one is in each `Part`;
    - at least one expects Confirmed and at least one expects Refuted;
    - each pull request's two commits are in its repository and differ, and its title is
      not empty.

    Called again with another directory, it gives the same names, parts, pull request text,
    and expected Verdicts, in the same order.
    """
    return tuple(hand_made.built_in(directory) for hand_made in _HAND_MADE)


@dataclass(frozen=True)
class DecisionCase:
    """One decision with a known answer."""

    name: str
    """What the case is called. No two cases in one comparison share a name."""

    state: str
    """The state the question is asked of."""

    question: Question
    """The question."""

    expected: str
    """The option that is the right answer: one of the question's options."""


@dataclass(frozen=True)
class DecisionScores:
    """What one implementation of the decision interface did over the cases of a comparison.

    A decision is correct when its `top` is the case's `expected`. A decision that raised
    `ModelFailed`, which a `DecisionFailed` is, or `InputTooLong` is failed: it is not
    correct, and it adds nothing to `passed_on` or `dollars`. So `correct + failed` is never
    more than `decisions`.

    A rate or a mean whose denominator is 0 is None.
    """

    decisions: int
    """How many decisions were asked for: one for each case."""

    correct: int
    """Decisions whose `top` was the expected option."""

    failed: int
    """Decisions that raised instead of answering."""

    passed_on: int
    """Decisions whose `passed_on` is not None: those the small LLM answered in the end."""

    seconds: float
    """The time taken by all the decisions together, failed ones included."""

    dollars: float
    """The sum of the `dollars` of the decisions that answered."""

    @property
    def accuracy(self) -> float | None:
        """`correct / decisions`."""
        raise NotImplementedError

    @property
    def seconds_per_decision(self) -> float | None:
        """`seconds / decisions`."""
        raise NotImplementedError

    @property
    def dollars_per_decision(self) -> float | None:
        """`dollars / decisions`."""
        raise NotImplementedError


@dataclass(frozen=True)
class DecisionComparison:
    """The same decisions answered by each of several implementations, side by side."""

    scores: Mapping[str, DecisionScores]
    """The scores of each implementation, under the name it was given, in the order given."""

    def to_json(self) -> str:
        """The comparison as a JSON object with one member for each name in `scores`, in
        order. Each holds every field of `DecisionScores` under its own name, and
        `accuracy`, `seconds_per_decision`, and `dollars_per_decision`. A rate or a mean
        that is None is null."""
        raise NotImplementedError


def compare_deciders(
    cases: Iterable[DecisionCase],
    deciders: Mapping[str, Decider],
    *,
    clock: Callable[[], float] = perf_counter,
) -> DecisionComparison:
    """Run the same decisions through each decider and report accuracy, latency, and cost.

    `deciders` are the implementations to compare, each under the name it is reported by:
    for the three-way comparison, Laya, Jev, and the small LLM. Each decider in turn, in the
    order given, is asked every case once, in the order given, with exactly the case's
    `state` and `question`. So every decider answers the same decisions, and none sees
    `expected`.

    Each decision is timed on its own: `clock` is read once just before `decide` is called
    and once just after it returns or raises, and the difference is added to that decider's
    `seconds`. `clock` is read at no other time. Loading a model is not timed here, since a
    decider is given already made.

    A decision that raises `ModelFailed` or `InputTooLong` is counted as failed and the
    comparison carries on. Any other exception is a fault in the system and is raised.

    Raises `ValueError` before asking anything when two cases share a name, or when a
    case's `expected` is not one of `options_of(case.question)`. No cases, or no deciders,
    is not an error.
    """
    raise NotImplementedError
