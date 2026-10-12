"""The Verdict computed from recorded Evidence: which Evidence decides a Claim, and which way."""

from collections.abc import Sequence

import pytest
from conftest import a_reproduction, a_search, results
from hypothesis import given
from hypothesis import strategies as st

from pr_verification import verdict
from pr_verification.ecosystem import Results, Unreadable
from pr_verification.report import (
    ClaimKind,
    Evidence,
    EvidenceSource,
    ReadEvidence,
    ReproductionEvidence,
    RunEvidence,
    SearchEvidence,
    TextChange,
    Verdict,
)
from pr_verification.sandbox import RunResult
from pr_verification.verdict import Computed, compute_verdict

CONFIRMED = Computed(Verdict.CONFIRMED, verdict.CONFIRMED)
REFUTED = Computed(Verdict.REFUTED, verdict.REFUTED)
NO_REPRODUCTION_RAN = Computed(Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)
DECIDES_NOTHING = Computed(Verdict.INCONCLUSIVE, verdict.DECIDES_NOTHING)

UNREADABLE = Unreadable("The run was stopped at the time limit.")
STRUCTURAL = ClaimKind.STRUCTURAL
ADDED, REMOVED = TextChange.ADDED, TextChange.REMOVED


# Reproductions that the Reproduction judge accepted and that ran on both commits.
SUPPORTS = a_reproduction(results(failed="t"), results(passed="t"))
CONTRADICTS = a_reproduction(results(passed="t"), results(failed="t"))
PASSES_ON_BOTH = a_reproduction(results(passed="t"), results(passed="t"))
FAILS_ON_BOTH = a_reproduction(results(failed="t"), results(failed="t"))
# One that did not, though its tests ended as those of `SUPPORTS` did where they ran.
DID_NOT_RUN = a_reproduction(results(failed="t", errors="u"), results(passed="t"))

# The same two, which the judge did not accept.
NOT_ACCEPTED_SUPPORTS = a_reproduction(results(failed="t"), results(passed="t"), accepted=False)
NOT_ACCEPTED_CONTRADICTS = a_reproduction(results(passed="t"), results(failed="t"), accepted=False)

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
FOUND = a_search(ADDED, 0, 2, observation="The search shows that this Claim is Confirmed.")


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
    [(), (SAID,), (READ,), (RAN_A_COMMAND,), (FOUND,), (SAID, READ, RAN_A_COMMAND, DID_NOT_RUN)],
    ids=["none", "an observation", "read", "ran a command", "searched", "all of them"],
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


@pytest.mark.parametrize(
    ("evidence", "computed"),
    [
        ((NOT_ACCEPTED_SUPPORTS,), DECIDES_NOTHING),
        ((NOT_ACCEPTED_CONTRADICTS,), DECIDES_NOTHING),
        ((SUPPORTS, NOT_ACCEPTED_CONTRADICTS), CONFIRMED),
        ((CONTRADICTS, NOT_ACCEPTED_SUPPORTS), REFUTED),
        ((PASSES_ON_BOTH, NOT_ACCEPTED_SUPPORTS), DECIDES_NOTHING),
        ((DID_NOT_RUN, NOT_ACCEPTED_CONTRADICTS), DECIDES_NOTHING),
    ],
    ids=[
        "fails then passes",
        "passes then fails",
        "beside an accepted one that supports",
        "beside an accepted one that contradicts",
        "beside an accepted one that decides nothing",
        "beside an accepted one that did not run",
    ],
)
def test_reproduction_the_judge_did_not_accept_supports_nothing_and_contradicts_nothing(
    evidence: tuple[Evidence, ...], computed: Computed
) -> None:
    assert compute_verdict(evidence) == computed


# A Structural Claim

STRUCTURAL_CONFIRMED = Computed(Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED)
STRUCTURAL_REFUTED = Computed(Verdict.REFUTED, verdict.STRUCTURAL_REFUTED)
NOTHING_SEARCHED = Computed(Verdict.INCONCLUSIVE, verdict.NOTHING_SEARCHED)
SEARCH_DECIDES_NOTHING = Computed(Verdict.INCONCLUSIVE, verdict.SEARCH_DECIDES_NOTHING)


@pytest.mark.parametrize(
    ("change", "before", "after", "computed"),
    [
        (ADDED, 0, 1, STRUCTURAL_CONFIRMED),
        (ADDED, 0, 3, STRUCTURAL_CONFIRMED),
        (REMOVED, 1, 0, STRUCTURAL_CONFIRMED),
        (REMOVED, 3, 0, STRUCTURAL_CONFIRMED),
        (REMOVED, 1, 1, STRUCTURAL_REFUTED),
        (REMOVED, 3, 1, STRUCTURAL_REFUTED),
        (REMOVED, 1, 3, STRUCTURAL_REFUTED),
        (ADDED, 0, 0, SEARCH_DECIDES_NOTHING),
        (REMOVED, 0, 0, SEARCH_DECIDES_NOTHING),
        (ADDED, 1, 1, SEARCH_DECIDES_NOTHING),
        (ADDED, 1, 3, SEARCH_DECIDES_NOTHING),
        (ADDED, 2, 0, SEARCH_DECIDES_NOTHING),
        (REMOVED, 0, 2, SEARCH_DECIDES_NOTHING),
    ],
    ids=[
        "added",
        "added on several lines",
        "removed",
        "removed from several lines",
        "said removed and still there",
        "said removed and still on one line",
        "said removed and on more lines than before",
        "said added and on neither commit",
        "said removed and on neither commit",
        "said added and already there",
        "said added and already there on fewer lines",
        "said added and removed",
        "said removed and added",
    ],
)
def test_search_for_one_text_decides_by_its_change_and_how_many_lines_hold_it_on_each_commit(
    change: TextChange, before: int, after: int, computed: Computed
) -> None:
    assert compute_verdict([a_search(change, before, after)], STRUCTURAL) == computed


@pytest.mark.parametrize(
    ("evidence", "computed"),
    [
        ((a_search(REMOVED, 2, 0), a_search(ADDED, 0, 2)), STRUCTURAL_CONFIRMED),
        ((a_search(REMOVED, 2, 1), a_search(ADDED, 0, 5)), STRUCTURAL_REFUTED),
        (
            (a_search(ADDED, 0, 1), a_search(ADDED, 0, 0), a_search(REMOVED, 1, 1)),
            STRUCTURAL_REFUTED,
        ),
        ((a_search(ADDED, 0, 1), a_search(REMOVED, 0, 0)), SEARCH_DECIDES_NOTHING),
        ((a_search(REMOVED, 1, 0), a_search(ADDED, 1, 1)), SEARCH_DECIDES_NOTHING),
    ],
    ids=[
        "a rename fully carried out",
        "a rename that leaves the old name",
        "one text still there whatever the others show",
        "one text on neither commit",
        "one text already there",
    ],
)
def test_structural_claim_is_refuted_by_one_text_still_there_and_confirmed_only_by_every_text(
    evidence: tuple[Evidence, ...], computed: Computed
) -> None:
    assert compute_verdict(evidence, STRUCTURAL) == computed


@pytest.mark.parametrize(
    ("evidence", "computed"),
    [
        ((), NOTHING_SEARCHED),
        ((SUPPORTS,), NOTHING_SEARCHED),
        ((CONTRADICTS, NOT_ACCEPTED_CONTRADICTS, SAID, READ, RAN_A_COMMAND), NOTHING_SEARCHED),
        ((a_search(ADDED, 0, 1), CONTRADICTS, SAID), STRUCTURAL_CONFIRMED),
        ((a_search(REMOVED, 1, 1), SUPPORTS, READ), STRUCTURAL_REFUTED),
        ((a_search(ADDED, 0, 0), SUPPORTS), SEARCH_DECIDES_NOTHING),
    ],
    ids=[
        "none",
        "a Reproduction that supports",
        "everything but a search",
        "a search that supports beside a Reproduction that contradicts",
        "a search that contradicts beside a Reproduction that supports",
        "a search that shows nothing beside a Reproduction that supports",
    ],
)
def test_structural_claim_is_decided_only_by_what_a_search_found_and_never_by_running_anything(
    evidence: tuple[Evidence, ...], computed: Computed
) -> None:
    assert compute_verdict(evidence, STRUCTURAL) == computed


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
REPRODUCTIONS = st.builds(a_reproduction, READS, READS, accepted=st.booleans())
# Among them the two that would decide a Claim had the judge accepted them.
REPRODUCTIONS_NOT_ACCEPTED = st.builds(
    a_reproduction, READS, READS, accepted=st.just(False)
) | st.sampled_from([NOT_ACCEPTED_SUPPORTS, NOT_ACCEPTED_CONTRADICTS])
FOUND_ON = st.integers(min_value=0, max_value=3)
SEARCHES = st.builds(a_search, st.sampled_from(TextChange), FOUND_ON, FOUND_ON)
OTHERS = st.sampled_from([SAID, READ, RAN_A_COMMAND]) | SEARCHES
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


@given(evidence=st.lists(REPRODUCTIONS_NOT_ACCEPTED | OTHERS, max_size=4))
def test_claim_with_no_reproduction_the_judge_accepted_is_never_confirmed_or_refuted(
    evidence: list[Evidence],
) -> None:
    assert compute_verdict(evidence).verdict is Verdict.INCONCLUSIVE


@given(
    evidence=EVIDENCE,
    not_accepted=st.lists(REPRODUCTIONS_NOT_ACCEPTED, min_size=1, max_size=3),
    kind=st.sampled_from(ClaimKind),
)
def test_reproduction_the_judge_did_not_accept_never_changes_a_verdict_whatever_its_runs_showed(
    evidence: list[Evidence], not_accepted: list[Evidence], kind: ClaimKind
) -> None:
    beside_them = compute_verdict([*evidence, *not_accepted], kind)

    assert beside_them.verdict is compute_verdict(evidence, kind).verdict


def still_there(evidence: Evidence) -> bool:
    """Whether a search found text that is said to be removed on both commits."""
    return (
        isinstance(evidence, SearchEvidence)
        and evidence.change is REMOVED
        and bool(evidence.before_lines)
        and bool(evidence.after_lines)
    )


@given(evidence=EVIDENCE)
def test_structural_claim_is_refuted_only_when_text_said_to_be_removed_is_still_there(
    evidence: list[Evidence],
) -> None:
    computed = compute_verdict(evidence, STRUCTURAL)

    searches = [piece for piece in evidence if isinstance(piece, SearchEvidence)]
    assert computed in (
        STRUCTURAL_CONFIRMED,
        STRUCTURAL_REFUTED,
        NOTHING_SEARCHED,
        SEARCH_DECIDES_NOTHING,
    )
    assert (computed == NOTHING_SEARCHED) == (not searches)
    assert (computed == STRUCTURAL_REFUTED) == any(map(still_there, searches))


@given(
    shuffled=EVIDENCE.flatmap(
        lambda evidence: st.tuples(st.just(evidence), st.permutations(evidence))
    )
)
def test_same_evidence_always_gives_a_structural_claim_the_same_verdict_in_whatever_order(
    shuffled: tuple[list[Evidence], Sequence[Evidence]],
) -> None:
    evidence, in_another_order = shuffled

    computed = compute_verdict(evidence, STRUCTURAL)

    assert compute_verdict(tuple(evidence), STRUCTURAL) == computed
    assert compute_verdict(in_another_order, STRUCTURAL) == computed


@given(
    change=st.sampled_from(TextChange),
    before=FOUND_ON,
    after=FOUND_ON,
    text=st.text(),
    path=st.none() | st.text(),
    line=st.text(),
    observation=st.text(),
)
def test_only_the_change_and_how_many_lines_hold_the_text_are_read_never_the_text_or_the_lines(
    change: TextChange,
    before: int,
    after: int,
    text: str,
    path: str | None,
    line: str,
    observation: str,
) -> None:
    told = a_search(change, before, after, text=text, path=path, line=line, observation=observation)

    assert compute_verdict([told], STRUCTURAL) == compute_verdict(
        [a_search(change, before, after)], STRUCTURAL
    )
