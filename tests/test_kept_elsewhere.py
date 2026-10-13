"""A text said to be removed from one file: where else code searches for it, and what a line
found there does to the Verdict."""

from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import FixtureRepository, a_search, commit_snapshots, finishes
from hypothesis import given
from hypothesis import strategies as st
from structural_cases import KeyText, StructuralCase, built, key_verdict

from pr_verification import verdict
from pr_verification.evaluation import Part
from pr_verification.model import ScriptedModel
from pr_verification.reading import lines_holding
from pr_verification.report import (
    ClaimKind,
    Evidence,
    EvidenceSource,
    SearchEvidence,
    TextChange,
    Verdict,
)
from pr_verification.structural import search_for, verify_structural_claim
from pr_verification.verdict import Computed, compute_verdict

STRUCTURAL = ClaimKind.STRUCTURAL
ADDED, REMOVED = TextChange.ADDED, TextChange.REMOVED

CLAIM = "Rename Py_NOGIL to Py_GIL_DISABLED"
NOTES = "Set Py_NOGIL to build without the lock.\n"
BEFORE = {
    "docs/notes.md": NOTES,
    "src/tags.py": "FLAG = 'Py_NOGIL'\n",
    "tests/test_tags.py": "def test_flag():\n    assert FLAG == 'Py_NOGIL'\n",
}
AFTER = {
    "docs/notes.md": NOTES + "Py_GIL_DISABLED is its new name.\n",
    "src/tags.py": "FLAG = 'Py_NOGIL'\n",
    "tests/test_tags.py": "def test_flag():\n    assert FLAG == 'Py_GIL_DISABLED'\n",
}
"""The rename is made in the tests alone: `src/tags.py` and the notes keep the old name."""

IN_NOTES = "docs/notes.md:1:Set Py_NOGIL to build without the lock."
IN_SOURCE = "src/tags.py:1:FLAG = 'Py_NOGIL'"
IN_TESTS = "tests/test_tags.py:2:    assert FLAG == 'Py_NOGIL'"
NEW_IN_NOTES = "docs/notes.md:2:Py_GIL_DISABLED is its new name."
NEW_IN_TESTS = "tests/test_tags.py:2:    assert FLAG == 'Py_GIL_DISABLED'"

TESTS = "tests/test_tags.py"


@pytest.fixture(scope="module")
def tags(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A pull request that renames a variable in the tests and in no other file."""
    path = tmp_path_factory.mktemp("kept-elsewhere") / "repository"
    before, after = commit_snapshots(path, BEFORE, AFTER)
    return FixtureRepository(path=path, before=before, after=after)


def search(
    repository: FixtureRepository, text: str, change: TextChange, path: str | None
) -> SearchEvidence:
    return search_for(repository.path, repository.before, repository.after, text, change, path)


# What code searches


@pytest.mark.parametrize(
    ("text", "change", "path", "before", "after", "elsewhere"),
    [
        ("Py_NOGIL", REMOVED, TESTS, [IN_TESTS], [], [IN_NOTES, IN_SOURCE]),
        ("Py_NOGIL", REMOVED, "src/tags.py", [IN_SOURCE], [IN_SOURCE], [IN_NOTES]),
        ("Py_NOGIL", REMOVED, "tests/missing.py", [], [], [IN_NOTES, IN_SOURCE]),
        ("Py_GIL_DISABLED", REMOVED, TESTS, [], [NEW_IN_TESTS], [NEW_IN_NOTES]),
        ("assert FLAG == 'Py_NOGIL'", REMOVED, TESTS, [IN_TESTS], [], []),
        ("Py_NOGIL", REMOVED, None, [IN_NOTES, IN_SOURCE, IN_TESTS], [IN_NOTES, IN_SOURCE], []),
        ("Py_GIL_DISABLED", ADDED, TESTS, [], [NEW_IN_TESTS], []),
        ("Py_GIL_DISABLED", ADDED, None, [], [NEW_IN_NOTES, NEW_IN_TESTS], []),
    ],
    ids=[
        "removed from one file and kept by two others",
        "still in the one file and kept by another",
        "a path the commit has no file at",
        "said to be removed where the pull request added it",
        "removed from one file and held by no other",
        "removed, with no path",
        "added to one file and held by another",
        "added, with no path",
    ],
)
def test_text_said_to_be_removed_from_one_file_is_searched_for_in_the_other_files_after(
    tags: FixtureRepository,
    text: str,
    change: TextChange,
    path: str | None,
    before: list[str],
    after: list[str],
    elsewhere: list[str],
) -> None:
    assert search(tags, text, change, path) == SearchEvidence(
        source=EvidenceSource.READ_CODE,
        observation=f"lines holding the text: {len(before)} before, {len(after)} after",
        text=text,
        change=change,
        path=path,
        before_lines=tuple(before),
        after_lines=tuple(after),
        after_lines_elsewhere=tuple(elsewhere),
    )


@given(
    text=st.sampled_from(["Py_NOGIL", "Py_GIL_DISABLED", "FLAG", "lock", "name", "absent"]),
    change=st.sampled_from(TextChange),
    path=st.none() | st.sampled_from([*AFTER, "tests/missing.py", "../tags.py", "docs"]),
)
def test_lines_elsewhere_are_those_of_a_search_of_every_file_less_those_of_the_one_file(
    tags: FixtureRepository, text: str, change: TextChange, path: str | None
) -> None:
    found = search(tags, text, change, path)

    in_every_file = lines_holding(tags.path, tags.after, text)
    assert found.before_lines == lines_holding(tags.path, tags.before, text, path)
    assert found.after_lines == lines_holding(tags.path, tags.after, text, path)
    if change is REMOVED and path is not None:
        assert found.after_lines_elsewhere == tuple(
            line for line in in_every_file if line not in found.after_lines
        )
    else:
        assert found.after_lines_elsewhere == ()


def test_verifier_records_the_other_files_whatever_path_its_role_gave(
    tags: FixtureRepository,
) -> None:
    entries = [
        {"text": "Py_NOGIL", "change": "removed", "path": TESTS},
        {"text": "Py_GIL_DISABLED", "change": "added", "path": TESTS},
    ]

    run = verify_structural_claim(
        CLAIM,
        repository=tags.path,
        before=tags.before,
        after=tags.after,
        model=ScriptedModel([finishes(texts=entries)]),
    )

    old_name, new_name = run.evidence
    assert old_name == search(tags, "Py_NOGIL", REMOVED, TESTS)
    assert new_name == search(tags, "Py_GIL_DISABLED", ADDED, TESTS)
    assert old_name.after_lines_elsewhere == (IN_NOTES, IN_SOURCE)
    assert compute_verdict(run.evidence, STRUCTURAL) == KEPT_ELSEWHERE


# The Verdict

STRUCTURAL_CONFIRMED = Computed(Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED)
STRUCTURAL_REFUTED = Computed(Verdict.REFUTED, verdict.STRUCTURAL_REFUTED)
NOTHING_SEARCHED = Computed(Verdict.INCONCLUSIVE, verdict.NOTHING_SEARCHED)
KEPT_ELSEWHERE = Computed(Verdict.INCONCLUSIVE, verdict.KEPT_ELSEWHERE)
SEARCH_DECIDES_NOTHING = Computed(Verdict.INCONCLUSIVE, verdict.SEARCH_DECIDES_NOTHING)

SAID = Evidence(EvidenceSource.READ_CODE, "The model says this Claim is Confirmed.")


def found(
    change: TextChange, before: int, after: int, elsewhere: int, line: str = "FLAG = 'Py_NOGIL'"
) -> SearchEvidence:
    """Evidence from a search of one file that found the text on this many lines of each
    commit, and on this many lines of other files after the pull request."""
    return replace(
        a_search(change, before, after, path=TESTS),
        after_lines_elsewhere=tuple(
            f"src/tags.py:{number}:{line}" for number in range(1, elsewhere + 1)
        ),
    )


@pytest.mark.parametrize(
    ("change", "before", "after", "elsewhere", "computed"),
    [
        (REMOVED, 1, 0, 0, STRUCTURAL_CONFIRMED),
        (REMOVED, 1, 0, 1, KEPT_ELSEWHERE),
        (REMOVED, 3, 0, 2, KEPT_ELSEWHERE),
        (REMOVED, 1, 1, 0, STRUCTURAL_REFUTED),
        (REMOVED, 1, 1, 2, STRUCTURAL_REFUTED),
        (REMOVED, 0, 0, 1, SEARCH_DECIDES_NOTHING),
        (REMOVED, 0, 1, 1, SEARCH_DECIDES_NOTHING),
        (ADDED, 0, 1, 1, STRUCTURAL_CONFIRMED),
        (ADDED, 1, 1, 1, SEARCH_DECIDES_NOTHING),
        (ADDED, 1, 0, 1, SEARCH_DECIDES_NOTHING),
    ],
)
def test_text_gone_from_the_one_file_and_held_by_another_leaves_the_claim_inconclusive(
    change: TextChange, before: int, after: int, elsewhere: int, computed: Computed
) -> None:
    assert compute_verdict([found(change, before, after, elsewhere)], STRUCTURAL) == computed


@pytest.mark.parametrize(
    ("evidence", "computed"),
    [
        ((found(REMOVED, 2, 0, 1), found(ADDED, 0, 2, 0)), KEPT_ELSEWHERE),
        ((found(REMOVED, 2, 0, 1), found(REMOVED, 2, 0, 0)), KEPT_ELSEWHERE),
        ((found(REMOVED, 2, 0, 1), found(ADDED, 0, 0, 0)), KEPT_ELSEWHERE),
        ((found(REMOVED, 2, 0, 1), found(REMOVED, 0, 0, 0)), KEPT_ELSEWHERE),
        ((found(REMOVED, 2, 0, 1), SAID), KEPT_ELSEWHERE),
        ((found(REMOVED, 2, 0, 1), found(REMOVED, 1, 1, 0)), STRUCTURAL_REFUTED),
        ((found(REMOVED, 1, 1, 0), found(REMOVED, 2, 0, 1)), STRUCTURAL_REFUTED),
    ],
    ids=[
        "beside a text that was added",
        "beside a text that was removed from every file",
        "beside a text that was not added",
        "beside a text on neither commit",
        "beside Evidence that is no search",
        "beside a text still in its file",
        "after a text still in its file",
    ],
)
def test_one_text_kept_elsewhere_stops_confirmed_whatever_the_others_show_and_never_refutes(
    evidence: tuple[Evidence, ...], computed: Computed
) -> None:
    assert compute_verdict(evidence, STRUCTURAL) == computed


def still_there(search: SearchEvidence) -> bool:
    """Whether the search found text that is said to be removed in its file on both commits."""
    return search.change is REMOVED and bool(search.before_lines) and bool(search.after_lines)


def kept_elsewhere(search: SearchEvidence) -> bool:
    """Whether the text is said to be removed, is gone from its file, and is held by another."""
    return (
        search.change is REMOVED
        and bool(search.before_lines)
        and not search.after_lines
        and bool(search.after_lines_elsewhere)
    )


FOUND_ON = st.integers(min_value=0, max_value=3)
SEARCHES = st.builds(found, st.sampled_from(TextChange), FOUND_ON, FOUND_ON, FOUND_ON)
EVIDENCE = st.lists(SEARCHES | st.just(SAID), max_size=4)


@given(evidence=EVIDENCE)
def test_structural_verdict_is_one_of_the_five_and_each_inconclusive_reason_has_its_own_case(
    evidence: list[Evidence],
) -> None:
    computed = compute_verdict(evidence, STRUCTURAL)

    searches = [piece for piece in evidence if isinstance(piece, SearchEvidence)]
    assert computed in (
        STRUCTURAL_CONFIRMED,
        STRUCTURAL_REFUTED,
        NOTHING_SEARCHED,
        KEPT_ELSEWHERE,
        SEARCH_DECIDES_NOTHING,
    )
    assert (computed == NOTHING_SEARCHED) == (not searches)
    assert (computed == STRUCTURAL_REFUTED) == any(map(still_there, searches))
    assert (computed == KEPT_ELSEWHERE) == (
        any(map(kept_elsewhere, searches)) and not any(map(still_there, searches))
    )


@given(evidence=EVIDENCE)
def test_lines_found_elsewhere_can_only_take_a_confirmed_away(evidence: list[Evidence]) -> None:
    searched_in_one_file_alone = [
        replace(piece, after_lines_elsewhere=()) if isinstance(piece, SearchEvidence) else piece
        for piece in evidence
    ]

    computed = compute_verdict(evidence, STRUCTURAL)
    without = compute_verdict(searched_in_one_file_alone, STRUCTURAL)

    if computed.verdict is not without.verdict:
        assert (without, computed) == (STRUCTURAL_CONFIRMED, KEPT_ELSEWHERE)
    if computed != without:
        assert computed == KEPT_ELSEWHERE


@given(evidence=st.lists(st.builds(found, st.just(ADDED), FOUND_ON, FOUND_ON, FOUND_ON)))
def test_lines_elsewhere_are_not_read_for_a_text_said_to_be_added(
    evidence: list[SearchEvidence],
) -> None:
    searched_in_one_file_alone = [replace(piece, after_lines_elsewhere=()) for piece in evidence]

    assert compute_verdict(evidence, STRUCTURAL) == compute_verdict(
        searched_in_one_file_alone, STRUCTURAL
    )


@given(
    shuffled=EVIDENCE.flatmap(
        lambda evidence: st.tuples(st.just(evidence), st.permutations(evidence))
    )
)
def test_same_evidence_gives_the_same_verdict_in_whatever_order_with_lines_found_elsewhere(
    shuffled: tuple[list[Evidence], Sequence[Evidence]],
) -> None:
    evidence, in_another_order = shuffled

    assert compute_verdict(in_another_order, STRUCTURAL) == compute_verdict(evidence, STRUCTURAL)


@given(
    change=st.sampled_from(TextChange),
    before=FOUND_ON,
    after=FOUND_ON,
    elsewhere=FOUND_ON,
    line=st.text(),
)
def test_only_how_many_lines_were_found_elsewhere_is_read_never_what_they_hold(
    change: TextChange, before: int, after: int, elsewhere: int, line: str
) -> None:
    assert compute_verdict(
        [found(change, before, after, elsewhere, line)], STRUCTURAL
    ) == compute_verdict([found(change, before, after, elsewhere)], STRUCTURAL)


# The answer key of a Structural case

RENAME = (
    {"names.py": "def parse_all(): ...", "README.md": "Call parse_all."},
    {"names.py": "def parse_many(): ...", "README.md": "Call parse_many."},
)
IN_ONE_FILE = (
    KeyText("parse_all", REMOVED, "names.py"),
    KeyText("parse_many", ADDED, "names.py"),
)


@pytest.mark.parametrize(
    ("left_out", "by_key"),
    [((), Verdict.CONFIRMED), (("README.md",), Verdict.INCONCLUSIVE)],
    ids=["a rename fully carried out", "a rename that skips another file"],
)
def test_key_that_names_one_file_does_not_confirm_a_rename_that_another_file_was_left_out_of(
    tmp_path: Path, left_out: tuple[str, ...], by_key: Verdict
) -> None:
    _, renamed = commit_snapshots(tmp_path / "clone", *RENAME)
    case = StructuralCase(
        name="structural/rename/parse-many",
        part=Part.DEVELOPMENT,
        kind="rename",
        repository="example/names",
        commit=renamed,
        title="Rename parse_all to parse_many",
        description="",
        key=IN_ONE_FILE,
        left_out=left_out,
    )

    assert key_verdict(built(case, tmp_path / "clone", tmp_path), IN_ONE_FILE) is by_key
