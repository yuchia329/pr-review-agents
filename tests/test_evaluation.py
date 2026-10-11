"""The evaluation harness: how runs are scored, and how cases are run through the entry point.

Nothing here reaches a real model or a dataset. The entry point is either the core with a
scripted stand-in for the model, or a stand-in system that gives prepared Verdicts.
"""

import inspect
import json
from collections.abc import Iterable, Sequence
from dataclasses import fields
from itertools import repeat
from pathlib import Path

import pytest
from conftest import FixtureRepository, git, model_listing
from hypothesis import given
from hypothesis import strategies as st

from pr_verification import cli, core
from pr_verification.claims import ClaimExtractionFailed
from pr_verification.core import InvalidPullRequest, PullRequest
from pr_verification.evaluation import (
    Case,
    CaseRun,
    Part,
    PartEvaluation,
    Scores,
    evaluate,
    hand_made_cases,
    score,
)
from pr_verification.model import Model, ModelFailed, ScriptedModel
from pr_verification.report import BudgetSpend, Claim, Report, Verdict

CONFIRMED, REFUTED, INCONCLUSIVE = Verdict.CONFIRMED, Verdict.REFUTED, Verdict.INCONCLUSIVE
SPEND = BudgetSpend(seconds=12.0, dollars=0.25, steps=4)
NO_REPORT = "the model could not be reached"


def a_run(
    expected: Verdict,
    *verdicts: Verdict,
    name: str = "case",
    repetition: int = 1,
    spend: BudgetSpend | None = SPEND,
) -> CaseRun:
    """A run that gave these Verdicts, or one that gave no report when `spend` is None."""
    no_report = NO_REPORT if spend is None else None
    return CaseRun(name, repetition, expected, verdicts, spend, no_report)


# Scoring


@pytest.mark.parametrize(
    ("expected", "verdict", "counted_as"),
    [
        (CONFIRMED, CONFIRMED, "decided"),
        (REFUTED, REFUTED, "decided"),
        (CONFIRMED, REFUTED, "false_blocks"),
        (REFUTED, CONFIRMED, "false_assurances"),
        (CONFIRMED, INCONCLUSIVE, "inconclusive"),
        (REFUTED, INCONCLUSIVE, "inconclusive"),
    ],
)
def test_each_verdict_is_scored_once_against_the_expected_verdict(
    expected: Verdict, verdict: Verdict, counted_as: str
) -> None:
    scores = score([a_run(expected, verdict)])

    counts = ("decided", "false_blocks", "false_assurances", "inconclusive")
    assert {name: getattr(scores, name) for name in counts} == {
        **dict.fromkeys(counts, 0),
        counted_as: 1,
    }
    true_and_false = (1, 0) if expected is CONFIRMED else (0, 1)
    assert (scores.true_claims, scores.false_claims) == true_and_false


def test_rates_are_shares_of_the_claims_they_are_about() -> None:
    scores = score(
        [
            a_run(CONFIRMED, CONFIRMED, CONFIRMED, REFUTED, INCONCLUSIVE),
            a_run(REFUTED, CONFIRMED, REFUTED),
        ]
    )

    assert scores.claims == 6
    assert scores.decided_rate == pytest.approx(3 / 6)
    assert scores.false_block_rate == pytest.approx(1 / 4)
    assert scores.false_assurance_rate == pytest.approx(1 / 2)


def test_run_that_gave_no_verdicts_adds_no_claim_and_is_counted_apart() -> None:
    report_with_no_claims = a_run(CONFIRMED)
    no_report = a_run(REFUTED, spend=None)

    scores = score(iter([report_with_no_claims, no_report, a_run(CONFIRMED, CONFIRMED)]))

    assert (scores.runs, scores.runs_without_verdicts) == (3, 2)
    assert (scores.claims, scores.decided, scores.inconclusive) == (1, 1, 0)


def test_rate_with_nothing_to_take_a_share_of_is_none() -> None:
    only_true_claims = score([a_run(CONFIRMED, CONFIRMED)])
    only_false_claims = score([a_run(REFUTED, REFUTED)])
    no_claims = score([a_run(CONFIRMED)])

    assert (only_true_claims.false_block_rate, only_true_claims.false_assurance_rate) == (0.0, None)
    assert (only_false_claims.false_block_rate, only_false_claims.false_assurance_rate) == (
        None,
        0.0,
    )
    assert no_claims.decided_rate is None


def test_no_runs_give_all_counts_zero_and_both_means_none() -> None:
    assert score([]) == Scores(
        runs=0,
        runs_without_verdicts=0,
        true_claims=0,
        false_claims=0,
        decided=0,
        false_blocks=0,
        false_assurances=0,
        inconclusive=0,
        seconds_per_pull_request=None,
        dollars_per_pull_request=None,
    )


def test_time_and_cost_per_pull_request_are_means_over_the_runs_where_they_are_known() -> None:
    known = score(
        [
            a_run(CONFIRMED, CONFIRMED, spend=BudgetSpend(seconds=10.0, dollars=0.5, steps=3)),
            a_run(CONFIRMED, spend=BudgetSpend(seconds=20.0, dollars=0.1, steps=1)),
            a_run(CONFIRMED, spend=None),
        ]
    )
    known_for_none = score([a_run(CONFIRMED, spend=None)])

    assert known.seconds_per_pull_request == pytest.approx(15.0)
    assert known.dollars_per_pull_request == pytest.approx(0.3)
    assert known_for_none.seconds_per_pull_request is None
    assert known_for_none.dollars_per_pull_request is None


@pytest.mark.parametrize("verdicts", [(CONFIRMED,), ()], ids=["a Verdict", "no Verdicts"])
def test_run_that_expects_inconclusive_cannot_be_scored(verdicts: tuple[Verdict, ...]) -> None:
    with pytest.raises(ValueError):
        score([a_run(INCONCLUSIVE, *verdicts)])


def case_runs(verdicts: st.SearchStrategy[Verdict]) -> st.SearchStrategy[list[CaseRun]]:
    """Runs that gave a report, with any number of Claims in it, and runs that gave none."""
    names, repetitions = st.sampled_from(["a", "b", "c"]), st.integers(1, 3)
    expected = st.sampled_from([CONFIRMED, REFUTED])
    spends = st.builds(
        BudgetSpend, seconds=st.floats(0, 900), dollars=st.floats(0, 5), steps=st.integers(0, 50)
    )
    given_verdicts = st.lists(verdicts, max_size=4).map(tuple)
    with_a_report = st.builds(
        CaseRun, names, repetitions, expected, given_verdicts, spends, st.none()
    )
    without_one = st.builds(
        CaseRun, names, repetitions, expected, st.just(()), st.none(), st.just(NO_REPORT)
    )
    return st.lists(with_a_report | without_one, max_size=12)


@given(case_runs(st.sampled_from(list(Verdict))))
def test_counts_and_rates_agree_and_every_rate_is_between_zero_and_one(
    runs: list[CaseRun],
) -> None:
    scores = score(runs)

    assert scores.runs == len(runs)
    assert scores.runs_without_verdicts == sum(not run.verdicts for run in runs)
    assert scores.claims == sum(len(run.verdicts) for run in runs)
    assert scores.claims == scores.true_claims + scores.false_claims
    assert scores.claims == (
        scores.decided + scores.false_blocks + scores.false_assurances + scores.inconclusive
    )
    for rate, count, out_of in (
        (scores.decided_rate, scores.decided, scores.claims),
        (scores.false_block_rate, scores.false_blocks, scores.true_claims),
        (scores.false_assurance_rate, scores.false_assurances, scores.false_claims),
    ):
        if out_of == 0:
            assert rate is None
        else:
            assert rate is not None and 0.0 <= rate <= 1.0
            assert rate == pytest.approx(count / out_of)


@given(case_runs(st.just(INCONCLUSIVE)))
def test_system_that_answers_inconclusive_to_everything_has_no_false_blocks_and_decides_nothing(
    runs: list[CaseRun],
) -> None:
    scores = score(runs)

    assert scores.false_blocks == 0
    assert scores.false_block_rate in (None, 0.0)
    assert scores.decided_rate == (0.0 if scores.claims else None)


# One part's runs


def test_part_is_scored_over_all_its_runs_together_and_over_each_repetition_alone() -> None:
    first = [a_run(CONFIRMED, CONFIRMED, name="a"), a_run(REFUTED, CONFIRMED, name="b")]
    second = [
        a_run(CONFIRMED, INCONCLUSIVE, name="a", repetition=2),
        a_run(REFUTED, REFUTED, name="b", repetition=2),
    ]

    part = PartEvaluation(runs=(*first, *second))

    assert part.scores == score([*first, *second])
    assert part.by_repetition == (score(first), score(second))


def test_part_with_no_runs_has_no_repetitions_to_score() -> None:
    assert PartEvaluation(runs=()).by_repetition == ()


def test_cases_whose_runs_did_not_all_give_the_same_verdicts_are_named_as_varying() -> None:
    part = PartEvaluation(
        runs=(
            a_run(CONFIRMED, CONFIRMED, INCONCLUSIVE, name="same-in-another-order"),
            a_run(CONFIRMED, CONFIRMED, name="finds-another-claim"),
            a_run(REFUTED, REFUTED, name="changes-its-verdict"),
            a_run(CONFIRMED, INCONCLUSIVE, CONFIRMED, name="same-in-another-order", repetition=2),
            a_run(CONFIRMED, CONFIRMED, CONFIRMED, name="finds-another-claim", repetition=2),
            a_run(REFUTED, CONFIRMED, name="changes-its-verdict", repetition=2),
        )
    )

    assert part.varying == ("finds-another-claim", "changes-its-verdict")


# Running cases through the entry point


class PreparedSystem:
    """A stand-in for the core's entry point: answers each call as prepared and records it."""

    def __init__(self, answers: Iterable[Sequence[Verdict] | Exception]) -> None:
        self._answers = iter(answers)
        self.calls: list[tuple[PullRequest, Model]] = []

    def __call__(self, pull_request: PullRequest, model: Model) -> Report:
        self.calls.append((pull_request, model))
        answer = next(self._answers)
        if isinstance(answer, Exception):
            raise answer
        claims = tuple(
            Claim(f"Claim {number}", verdict, "") for number, verdict in enumerate(answer)
        )
        return Report(pull_request.before, pull_request.after, claims, (), SPEND)


class Fault(Exception):
    """An error the core does not raise for a pull request it cannot verify."""


MODEL = ScriptedModel([])
DEVELOPMENT, HELD_OUT = Part.DEVELOPMENT, Part.HELD_OUT


def a_case(name: str, part: Part = DEVELOPMENT, expected: Verdict = CONFIRMED) -> Case:
    pull_request = PullRequest(Path(name), before="before", after="after", title=f"Fix {name}")
    return Case(name, part, pull_request, expected)


def test_harness_runs_a_pull_request_through_the_entry_point_the_command_line_tool_calls(
    fixture_repository: FixtureRepository, capsys: pytest.CaptureFixture[str]
) -> None:
    repository, before, after = (
        fixture_repository.path,
        fixture_repository.before,
        fixture_repository.after,
    )
    title = "Make the greeting friendlier"
    claims = ("The greeting is friendlier", "The greeting is longer")
    case = Case("greeting", DEVELOPMENT, PullRequest(repository, before, after, title), CONFIRMED)
    model = model_listing(*claims)

    evaluation = evaluate([case], model, repetitions=1)

    arguments = ["--repo", str(repository), "--before", before, "--after", after, "--title", title]
    cli.main(arguments, model=model_listing(*claims))
    printed = json.loads(capsys.readouterr().out)
    (run,) = evaluation.development.runs
    assert list(run.verdicts) == [claim["verdict"] for claim in printed["claims"]]
    assert any(title in request.data for request in model.requests)
    assert inspect.signature(evaluate).parameters["entry_point"].default is core.run


def test_every_case_is_run_once_in_the_order_given_then_every_case_again() -> None:
    cases = [a_case("first"), a_case("second", HELD_OUT), a_case("third")]
    system = PreparedSystem(repeat((CONFIRMED,)))

    evaluate(iter(cases), MODEL, repetitions=2, entry_point=system)

    assert [shown for shown, _ in system.calls] == [case.pull_request for case in cases] * 2
    assert all(model is MODEL for _, model in system.calls)


def test_each_run_is_recorded_in_the_part_of_its_case_with_what_its_report_gave() -> None:
    cases = [a_case("kept-back", HELD_OUT, REFUTED), a_case("tuned-on", DEVELOPMENT, CONFIRMED)]
    system = PreparedSystem([(REFUTED, INCONCLUSIVE), (CONFIRMED,), (CONFIRMED,), ()])

    evaluation = evaluate(cases, MODEL, repetitions=2, entry_point=system)

    assert evaluation.repetitions == 2
    assert evaluation.held_out.runs == (
        CaseRun("kept-back", 1, REFUTED, (REFUTED, INCONCLUSIVE), SPEND, None),
        CaseRun("kept-back", 2, REFUTED, (CONFIRMED,), SPEND, None),
    )
    assert evaluation.development.runs == (
        CaseRun("tuned-on", 1, CONFIRMED, (CONFIRMED,), SPEND, None),
        CaseRun("tuned-on", 2, CONFIRMED, (), SPEND, None),
    )


def test_development_cases_and_held_out_cases_are_scored_apart() -> None:
    cases = [a_case("tuned-on", DEVELOPMENT), a_case("kept-back", HELD_OUT)]
    system = PreparedSystem([(CONFIRMED,), (REFUTED,)])

    evaluation = evaluate(cases, MODEL, repetitions=1, entry_point=system)

    development, held_out = evaluation.development.scores, evaluation.held_out.scores
    assert (development.claims, development.decided, development.false_blocks) == (1, 1, 0)
    assert (held_out.claims, held_out.decided, held_out.false_blocks) == (1, 0, 1)


def test_variation_between_the_runs_of_a_case_is_shown() -> None:
    cases = [a_case("steady"), a_case("unsteady")]
    system = PreparedSystem([(CONFIRMED,), (CONFIRMED,), (CONFIRMED,), (INCONCLUSIVE,)])

    development = evaluate(cases, MODEL, repetitions=2, entry_point=system).development

    assert development.varying == ("unsteady",)
    assert [scores.decided for scores in development.by_repetition] == [2, 1]


@pytest.mark.parametrize(
    "error",
    [
        InvalidPullRequest("cannot resolve the before commit"),
        ModelFailed("the model could not be reached"),
        ClaimExtractionFailed("the model's response has no list of Claims"),
    ],
    ids=lambda error: type(error).__name__,
)
def test_run_that_gives_no_report_is_recorded_and_the_evaluation_carries_on(
    error: Exception,
) -> None:
    system = PreparedSystem([error, (CONFIRMED,)])

    evaluation = evaluate(
        [a_case("cannot-be-verified"), a_case("fine")], MODEL, repetitions=1, entry_point=system
    )

    assert evaluation.development.runs == (
        CaseRun("cannot-be-verified", 1, CONFIRMED, (), None, str(error)),
        CaseRun("fine", 1, CONFIRMED, (CONFIRMED,), SPEND, None),
    )


def test_fault_in_the_system_is_raised_and_not_recorded() -> None:
    system = PreparedSystem([Fault("a fault in the system")])

    with pytest.raises(Fault):
        evaluate([a_case("meets-a-fault")], MODEL, repetitions=1, entry_point=system)


@pytest.mark.parametrize(
    ("cases", "repetitions"),
    [
        ([a_case("only")], 0),
        ([a_case("twin"), a_case("twin", HELD_OUT)], 1),
        ([a_case("fine"), a_case("expects-no-answer", expected=INCONCLUSIVE)], 1),
    ],
    ids=["no repetitions", "two cases share a name", "a case expects Inconclusive"],
)
def test_evaluation_that_cannot_be_scored_is_refused_before_anything_is_run(
    cases: list[Case], repetitions: int
) -> None:
    system = PreparedSystem(repeat((CONFIRMED,)))

    with pytest.raises(ValueError):
        evaluate(cases, MODEL, repetitions=repetitions, entry_point=system)

    assert system.calls == []


def holds(printed: dict[str, object], scores: Scores) -> bool:
    """Whether a printed scores object holds every field of `scores`, `claims`, and the rates."""
    expected = {field.name: getattr(scores, field.name) for field in fields(scores)} | {
        "claims": scores.claims,
        "decided_rate": scores.decided_rate,
        "false_block_rate": scores.false_block_rate,
        "false_assurance_rate": scores.false_assurance_rate,
    }
    return {name: printed.get(name, "absent") for name in expected} == expected


def test_evaluation_is_reported_as_json_with_the_two_parts_apart() -> None:
    cases = [a_case("tuned-on", DEVELOPMENT, CONFIRMED), a_case("kept-back", HELD_OUT, REFUTED)]
    unreachable = ModelFailed(NO_REPORT)
    system = PreparedSystem([(CONFIRMED,), unreachable, (REFUTED,), unreachable])
    evaluation = evaluate(cases, MODEL, repetitions=2, entry_point=system)

    printed = json.loads(evaluation.to_json())

    assert printed["repetitions"] == 2
    for name, part in (("development", evaluation.development), ("held_out", evaluation.held_out)):
        assert holds(printed[name]["scores"], part.scores)
        assert len(printed[name]["by_repetition"]) == 2
        assert all(map(holds, printed[name]["by_repetition"], part.by_repetition))
        assert printed[name]["varying"] == list(part.varying)
    # No run of the held-out part gave a report, so it has no Claims and no known time or cost.
    held_out = printed["held_out"]["scores"]
    assert (held_out["decided_rate"], held_out["seconds_per_pull_request"]) == (None, None)
    assert held_out["dollars_per_pull_request"] is None


# The hand-made cases


def test_hand_made_cases_are_a_handful_spread_over_both_parts_and_both_answers(
    tmp_path: Path,
) -> None:
    cases = hand_made_cases(tmp_path)

    names = [case.name for case in cases]
    assert 3 <= len(cases) <= 10
    assert len(set(names)) == len(names)
    assert all(name.startswith("hand-made/") for name in names)
    assert {case.part for case in cases} == set(Part)
    assert {case.expected for case in cases} == {CONFIRMED, REFUTED}


def test_each_hand_made_case_has_a_repository_of_its_own_that_holds_its_two_commits(
    tmp_path: Path,
) -> None:
    cases = hand_made_cases(tmp_path)

    assert len({case.pull_request.repository for case in cases}) == len(cases)
    for case in cases:
        shown = case.pull_request
        assert tmp_path in shown.repository.parents
        before = git(shown.repository, "rev-parse", "--verify", f"{shown.before}^{{commit}}")
        after = git(shown.repository, "rev-parse", "--verify", f"{shown.after}^{{commit}}")
        assert before != after
        assert shown.title


def test_hand_made_cases_are_the_same_wherever_they_are_built(tmp_path: Path) -> None:
    def built_in(directory: Path) -> list[tuple[str, Part, str, str, str, Verdict]]:
        directory.mkdir()
        return [
            (
                case.name,
                case.part,
                case.pull_request.title,
                case.pull_request.description,
                case.pull_request.linked_issue,
                case.expected,
            )
            for case in hand_made_cases(directory)
        ]

    assert built_in(tmp_path / "one") == built_in(tmp_path / "another")


def test_harness_runs_the_hand_made_cases_against_the_core(tmp_path: Path) -> None:
    cases = hand_made_cases(tmp_path)
    model = ScriptedModel(json.dumps({"claims": [case.pull_request.title]}) for case in cases)

    evaluation = evaluate(cases, model, repetitions=1)

    for part, evaluated in ((DEVELOPMENT, evaluation.development), (HELD_OUT, evaluation.held_out)):
        runs = evaluated.runs
        assert [run.name for run in runs] == [case.name for case in cases if case.part is part]
        assert all(run.no_report is None and len(run.verdicts) == 1 for run in runs)
