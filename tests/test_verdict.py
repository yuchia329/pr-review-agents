"""The Verdict computed from recorded Evidence: which Evidence decides a Claim, and which way."""

from collections.abc import Sequence

import pytest
from conftest import a_reproduction, results
from hypothesis import given
from hypothesis import strategies as st

from pr_verification import verdict
from pr_verification.ecosystem import Results, Unreadable
from pr_verification.report import (
    Evidence,
    EvidenceSource,
    ReadEvidence,
    ReproductionEvidence,
    RunEvidence,
    Verdict,
)
from pr_verification.sandbox import RunResult
from pr_verification.verdict import Computed, compute_verdict

CONFIRMED = Computed(Verdict.CONFIRMED, verdict.CONFIRMED)
REFUTED = Computed(Verdict.REFUTED, verdict.REFUTED)
NO_REPRODUCTION_RAN = Computed(Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)
DECIDES_NOTHING = Computed(Verdict.INCONCLUSIVE, verdict.DECIDES_NOTHING)

UNREADABLE = Unreadable("The run was stopped at the time limit.")

# Reproductions that ran on both commits.
SUPPORTS = a_reproduction(results(failed="t"), results(passed="t"))
CONTRADICTS = a_reproduction(results(passed="t"), results(failed="t"))
PASSES_ON_BOTH = a_reproduction(results(passed="t"), results(passed="t"))
FAILS_ON_BOTH = a_reproduction(results(failed="t"), results(failed="t"))
# One that did not, though its tests ended as those of `SUPPORTS` did where they ran.
DID_NOT_RUN = a_reproduction(results(failed="t", errors="u"), results(passed="t"))

# Evidence that is not a Reproduction, each saying as loudly as it can that the Claim holds.
SAID = Evidence(EvidenceSource.READ_CODE, "The model says this Claim is Confirmed.")
READ = ReadEvidence(
    EvidenceSource.READ_CODE, "The fix is there.", "pricing.py", 1, 1, "return net * 1.2"
)
RAN_A_COMMAND = RunEvidence(
    EvidenceSource.RAN_CODE,
    "before: exit status 1; after: exit status 0",
    ("pytest",),
    RunResult("1 failed", 1, None),
    RunResult("1 passed", 0, None),
)


@pytest.mark.parametrize(
    ("before", "after", "computed"),
    [
        (results(failed="t"), results(passed="t"), CONFIRMED),
        (results(failed="ab"), results(passed="ab"), CONFIRMED),
        (results(passed="a", failed="b"), results(passed="ab"), CONFIRMED),
        (results(failed="a"), results(passed="ab"), CONFIRMED),
        (results(passed="t"), results(failed="t"), REFUTED),
        (results(passed="ab"), results(passed="a", failed="b"), REFUTED),
        (results(passed="a"), results(failed="ab"), REFUTED),
        (results(failed="t"), results(failed="t"), DECIDES_NOTHING),
        (results(passed="t"), results(passed="t"), DECIDES_NOTHING),
        (results(passed="a", failed="b"), results(passed="b", failed="a"), DECIDES_NOTHING),
        (results(failed="ab"), results(passed="a", failed="b"), DECIDES_NOTHING),
        (results(passed="a"), results(passed="ab"), DECIDES_NOTHING),
        (results(failed="a"), results(passed="b"), DECIDES_NOTHING),
        (results(passed="a"), results(failed="b"), DECIDES_NOTHING),
    ],
    ids=[
        "fails then passes",
        "every test fails then passes",
        "one test fails then passes and none fails after",
        "fails then passes beside a test only the code after has",
        "passes then fails",
        "one test passes then fails and none fails before",
        "passes then fails beside a test only the code after has",
        "fails on both",
        "passes on both",
        "a failing test on each commit",
        "fixes one test and another still fails after",
        "passes on both beside a test only the code after has",
        "the test that fails before is not the one that passes after",
        "the test that passes before is not the one that fails after",
    ],
)
def test_reproduction_that_ran_on_both_commits_decides_by_which_tests_passed_and_failed_on_each(
    before: Results, after: Results, computed: Computed
) -> None:
    assert compute_verdict([a_reproduction(before, after)]) == computed


@pytest.mark.parametrize(
    ("before", "after"),
    [
        (UNREADABLE, results(passed="t")),
        (results(passed="t"), UNREADABLE),
        (None, results(passed="t")),
        (results(passed="t"), None),
        (None, None),
        (results(failed="t", errors="u"), results(passed="t")),
        (results(passed="t"), results(failed="t", errors="u")),
        (results(errors="t"), results(errors="t")),
        (results(), results(passed="t")),
        (results(passed="t"), results()),
    ],
    ids=[
        "unreadable before",
        "unreadable after",
        "never read before",
        "never read after",
        "never read",
        "a test could not run before",
        "a test could not run after",
        "import error on both",
        "no test before",
        "no test after",
    ],
)
def test_reproduction_that_did_not_run_on_both_commits_is_not_evidence_to_decide_the_claim_on(
    before: Results | Unreadable | None, after: Results | Unreadable | None
) -> None:
    assert compute_verdict([a_reproduction(before, after)]) == NO_REPRODUCTION_RAN


@pytest.mark.parametrize(
    "evidence",
    [(), (SAID,), (READ,), (RAN_A_COMMAND,), (SAID, READ, RAN_A_COMMAND, DID_NOT_RUN)],
    ids=["none", "an observation", "read", "ran a command", "all of them"],
)
def test_evidence_that_is_not_a_reproduction_decides_nothing_whatever_it_holds(
    evidence: tuple[Evidence, ...],
) -> None:
    assert compute_verdict(evidence) == NO_REPRODUCTION_RAN


@pytest.mark.parametrize(
    ("evidence", "computed"),
    [
        ((SUPPORTS, SUPPORTS), CONFIRMED),
        ((SUPPORTS, PASSES_ON_BOTH, FAILS_ON_BOTH), CONFIRMED),
        ((DID_NOT_RUN, SAID, SUPPORTS), CONFIRMED),
        ((CONTRADICTS, CONTRADICTS), REFUTED),
        ((FAILS_ON_BOTH, CONTRADICTS, READ), REFUTED),
        ((SUPPORTS, CONTRADICTS), DECIDES_NOTHING),
        ((CONTRADICTS, SUPPORTS, SUPPORTS), DECIDES_NOTHING),
        ((PASSES_ON_BOTH, DID_NOT_RUN), DECIDES_NOTHING),
        ((DID_NOT_RUN, DID_NOT_RUN), NO_REPRODUCTION_RAN),
    ],
)
def test_several_pieces_of_evidence_confirm_or_refute_only_when_none_points_the_other_way(
    evidence: tuple[Evidence, ...], computed: Computed
) -> None:
    assert compute_verdict(evidence) == computed


# Whatever the Evidence


def read_as(ended: dict[str, str]) -> Results:
    """Results in which each test ended as the dictionary says it did."""
    tests = {"passed": "", "failed": "", "errors": ""}
    for test, how in ended.items():
        tests[how] += test
    return results(**tests)


def ran(read: Results | Unreadable | None) -> bool:
    return isinstance(read, Results) and not read.could_not_run and bool(read.passed + read.failed)


def ran_on_both_commits(evidence: Evidence) -> bool:
    return (
        isinstance(evidence, ReproductionEvidence)
        and ran(evidence.before_results)
        and ran(evidence.after_results)
    )


def regressed(evidence: ReproductionEvidence) -> bool:
    """Whether no test failed before and a test that passed before failed after."""
    before, after = evidence.before_results, evidence.after_results
    assert isinstance(before, Results) and isinstance(after, Results)
    return not before.failed and bool(set(before.passed) & set(after.failed))


TESTS = st.sampled_from("abc")
RAN = st.dictionaries(TESTS, st.sampled_from(["passed", "failed"]), min_size=1).map(read_as)
ENDED = st.dictionaries(TESTS, st.sampled_from(["passed", "failed", "errors"])).map(read_as)
READS = st.none() | st.just(UNREADABLE) | ENDED | RAN
REPRODUCTIONS = st.builds(a_reproduction, READS, READS)
OTHERS = st.sampled_from([SAID, READ, RAN_A_COMMAND])
EVIDENCE = st.lists(REPRODUCTIONS | OTHERS, max_size=4)


@given(evidence=EVIDENCE.filter(lambda evidence: not any(map(ran_on_both_commits, evidence))))
def test_claim_with_no_reproduction_that_ran_on_both_commits_is_never_confirmed_or_refuted(
    evidence: list[Evidence],
) -> None:
    assert compute_verdict(evidence) == NO_REPRODUCTION_RAN


@given(
    shuffled=EVIDENCE.flatmap(
        lambda evidence: st.tuples(st.just(evidence), st.permutations(evidence))
    )
)
def test_same_evidence_always_gives_the_same_verdict_in_whatever_order_it_was_recorded(
    shuffled: tuple[list[Evidence], Sequence[Evidence]],
) -> None:
    evidence, in_another_order = shuffled

    computed = compute_verdict(evidence)

    assert compute_verdict(tuple(evidence)) == computed
    assert compute_verdict(in_another_order) == computed


@given(evidence=EVIDENCE)
def test_verdict_is_refuted_only_by_a_regression_and_its_reason_is_one_of_the_four(
    evidence: list[Evidence],
) -> None:
    computed = compute_verdict(evidence)

    deciding = [piece for piece in evidence if ran_on_both_commits(piece)]
    assert computed in (CONFIRMED, REFUTED, NO_REPRODUCTION_RAN, DECIDES_NOTHING)
    assert (computed == NO_REPRODUCTION_RAN) == (not deciding)
    if computed == REFUTED:
        assert any(
            isinstance(piece, ReproductionEvidence) and regressed(piece) for piece in deciding
        )


@given(
    before=RAN,
    after=RAN,
    text=st.text(),
    output=st.text(),
    exit_status=st.integers(min_value=0, max_value=255),
    observation=st.text(),
)
def test_only_which_tests_passed_and_failed_is_read_never_output_exit_status_or_text(
    before: Results, after: Results, text: str, output: str, exit_status: int, observation: str
) -> None:
    told = a_reproduction(
        before, after, text=text, output=output, exit_status=exit_status, observation=observation
    )

    assert compute_verdict([told]) == compute_verdict([a_reproduction(before, after)])
