"""The names a Claim names that the pull request removes lines of: which ones code finds, where
it searches for them, and what a name still held does to the Verdict."""

import re
from collections.abc import Mapping, Sequence
from dataclasses import replace
from pathlib import Path

import pytest
from conftest import (
    FixtureRepository,
    UnreachableModel,
    a_search,
    asks,
    commit_snapshots,
    finishes,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification import verdict
from pr_verification.model import ScriptedModel
from pr_verification.removed_names import GENERAL_NAME_LINES, names_removed
from pr_verification.report import (
    ClaimKind,
    Evidence,
    EvidenceSource,
    RemovedNameEvidence,
    SearchEvidence,
    TextChange,
    Verdict,
)
from pr_verification.structural import verify_structural_claim
from pr_verification.verdict import Computed, compute_verdict

STRUCTURAL = ClaimKind.STRUCTURAL
ADDED, REMOVED = TextChange.ADDED, TextChange.REMOVED

BEFORE = {
    "CHANGELOG.md": "Wait until the lock is free.\n",
    "settings.toml": "retries = 1\n",
    "src/tags.py": "FLAG = config('Py_NOGIL')\n",
    "src/users.py": "def get_user(): ...\ndef set_user(): ...\n",
    "src/version.py": "def format_full_version(): ...\nfull = format_full_version()\n",
    "src/wait.py": "def until_not_interrupted():\n    # loop until the call returns\n    ...\n",
    "tests/test_tags.py": 'CONFIG = {\n    "Py_NOGIL": 1,\n}\n',
}
AFTER = {
    "CHANGELOG.md": "Wait until the lock is free.\n",
    "settings.toml": "retries = 3\n",
    "src/tags.py": "FLAG = config('Py_NOGIL')\n",
    "src/users.py": "",
    "src/version.py": "def _format_full_version(): ...\nfull = _format_full_version()\n",
    "src/wait.py": "",
    "tests/test_tags.py": 'CONFIG = {\n    "Py_GIL_DISABLED": 1,\n}\n',
}
"""The tests are renamed and `src/tags.py` keeps the old name. Two functions and a helper are
removed, a function gains an underscore, and a setting changes its value."""

STILL_READ = "src/tags.py:1:FLAG = config('Py_NOGIL')"


@pytest.fixture(scope="module")
def tags(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    path = tmp_path_factory.mktemp("removed-names") / "repository"
    before, after = commit_snapshots(path, BEFORE, AFTER)
    return FixtureRepository(path=path, before=before, after=after)


def a_name(name: str, *after_lines: str) -> RemovedNameEvidence:
    return RemovedNameEvidence(
        source=EvidenceSource.READ_CODE,
        observation=f"lines holding the name: {len(after_lines)} after",
        name=name,
        after_lines=after_lines,
    )


def names_of(claim: str, repository: FixtureRepository) -> tuple[RemovedNameEvidence, ...]:
    return names_removed(
        claim, repository=repository.path, before=repository.before, after=repository.after
    )


def pull_request(
    path: Path, before: Mapping[str, str], after: Mapping[str, str]
) -> FixtureRepository:
    return FixtureRepository(path, *commit_snapshots(path, before, after))


# What the Claim names


@pytest.mark.parametrize(
    "claim",
    [
        "Rename Py_NOGIL to Py_GIL_DISABLED",
        "rename py nogil to py gil disabled",
        "Renames PYNOGIL.",
        "The variable `py-nogil`, as the tests read it, has a new name",
        "pynogil is gone",
        "Rename Py NoGIL",
    ],
)
def test_name_is_found_however_the_claim_spells_it_and_searched_for_in_every_file_after(
    tags: FixtureRepository, claim: str
) -> None:
    assert names_of(claim, tags) == (a_name("Py_NOGIL", STILL_READ),)


@pytest.mark.parametrize(
    "claim",
    [
        "Tidy up some tests",
        "Remove tests/Py_NOGIL/fixtures",
        "Rename Py_NOGILS",
        "Rename Py_NO to something",
        "Rename Py and, later, NOGIL",
        "",
    ],
    ids=[
        "no name of the code",
        "the name inside a longer word",
        "a longer name",
        "a part of the name",
        "the parts of the name apart",
        "an empty Claim",
    ],
)
def test_claim_names_a_name_only_by_whole_adjacent_words(
    tags: FixtureRepository, claim: str
) -> None:
    assert names_of(claim, tags) == ()


def test_name_on_no_line_after_the_pull_request_is_recorded_with_no_lines(
    tags: FixtureRepository,
) -> None:
    assert names_of("Remove get_user()", tags) == (a_name("get_user"),)


def test_name_inside_a_longer_name_is_not_held_as_a_whole_name(tags: FixtureRepository) -> None:
    claim = "Rename format_full_version to _format_full_version"

    assert names_of(claim, tags) == (a_name("format_full_version"),)


# What the pull request removes


def test_name_on_a_line_lost_and_on_a_line_gained_is_not_removed(tags: FixtureRepository) -> None:
    assert names_of("Raise retries to 3", tags) == ()


@pytest.mark.parametrize(
    ("before", "after", "found"),
    [
        (
            {"a.py": "import os\nOLD_NAME = 1\n", "b.py": "x = 1\n"},
            {"a.py": "OLD_NAME = 1\nimport os\n", "b.py": "x = 2\n"},
            (),
        ),
        (
            {"a.py": "OLD_NAME = 1\n", "b.py": "x = 1\n"},
            {"a.py": "", "b.py": "x = 1\nOLD_NAME = 1\n"},
            (),
        ),
        (
            {"a.py": "OLD_NAME = 1\nOLD_NAME = 1\n", "b.py": "x = 1\n"},
            {"a.py": "OLD_NAME = 1\n", "b.py": "x = 1\n"},
            (a_name("OLD_NAME", "a.py:1:OLD_NAME = 1"),),
        ),
        (
            {"a.py": "OLD_NAME = 1\n", "b.py": "print(OLD_NAME)\n"},
            {"b.py": "print(OLD_NAME)\n"},
            (a_name("OLD_NAME", "b.py:1:print(OLD_NAME)"),),
        ),
        (
            {"b.py": "x = 1\n"},
            {"a.py": "OLD_NAME = 1\n", "b.py": "x = 1\n"},
            (),
        ),
        (
            {"a.py": "OLD_NAME = 1\n"},
            {"a.py": "OLD_NAME = 1\nx = 2\n"},
            (),
        ),
    ],
    ids=[
        "a line that moves within its file",
        "a line that moves to another file",
        "one of two lines that are the same",
        "a file that is deleted",
        "a file that is added",
        "a pull request that removes no line",
    ],
)
def test_file_loses_a_line_it_holds_more_times_before_than_after(
    tmp_path: Path,
    before: dict[str, str],
    after: dict[str, str],
    found: tuple[RemovedNameEvidence, ...],
) -> None:
    repository = pull_request(tmp_path / "repository", before, after)

    assert names_of("Remove OLD_NAME", repository) == found


# Which names are searched for


def test_only_the_most_specific_name_is_searched_for(tags: FixtureRepository) -> None:
    # `until` and `the` are on a line the pull request removes too, and the changelog keeps both.
    claim = "Remove the helper until_not_interrupted, used until now"

    assert names_of(claim, tags) == (a_name("until_not_interrupted"),)


def test_names_as_long_as_each_other_are_all_searched_for_in_the_order_of_their_text(
    tags: FixtureRepository, tmp_path: Path
) -> None:
    assert names_of("Remove set_user and get_user", tags) == (
        a_name("get_user"),
        a_name("set_user"),
    )

    spelled_twice = pull_request(
        tmp_path / "repository",
        {"a.py": "chat = Chat()\n", "b.py": "import chat\nfrom c import Chat\n"},
        {"a.py": "", "b.py": "import chat\nfrom c import Chat\n"},
    )
    assert names_of("Remove chat", spelled_twice) == (
        a_name("Chat", "b.py:2:from c import Chat"),
        a_name("chat", "b.py:1:import chat"),
    )


@pytest.mark.parametrize("held", [GENERAL_NAME_LINES, GENERAL_NAME_LINES + 1])
def test_name_held_by_too_many_lines_before_is_set_aside_and_no_shorter_name_takes_its_place(
    tmp_path: Path, held: int
) -> None:
    uses = "".join(f"v{number} = common_name\n" for number in range(held - 1))
    repository = pull_request(
        tmp_path / "repository",
        {"uses.py": uses, "helper.py": "common_name()  # helper\n", "notes.md": "A helper.\n"},
        {"uses.py": uses, "helper.py": "", "notes.md": "A helper.\n"},
    )

    found = names_of("Remove the common_name helper", repository)

    if held > GENERAL_NAME_LINES:
        assert found == ()
    else:
        (name,) = found
        assert (name.name, len(name.after_lines)) == ("common_name", held - 1)


def folded(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.lower())


def held_after(name: str) -> tuple[str, ...]:
    """Every line of `AFTER` that holds the name as a whole name."""
    whole = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    return tuple(
        f"{path}:{number}:{line}"
        for path in sorted(AFTER)
        for number, line in enumerate(AFTER[path].splitlines(), start=1)
        if whole.search(line)
    )


WORDS = st.sampled_from(
    [
        "Py_NOGIL", "py", "nogil", "PYNOGIL", "`Py-NoGIL`,", "rename", "the", "until",
        "until_not_interrupted", "get_user", "set", "user", "retries", "format_full_version",
        "_format_full_version", "config", "helper", "to", "src/tags.py", "3",
    ]
)  # fmt: skip


@settings(deadline=None)
@given(words=st.lists(WORDS, max_size=6))
def test_every_name_found_is_one_the_claim_names_and_holds_the_lines_code_finds_after(
    tags: FixtureRepository, words: list[str]
) -> None:
    found = names_of(" ".join(words), tags)

    said = {
        folded("".join(words[start:end]))
        for start in range(len(words))
        for end in range(start + 1, len(words) + 1)
    }
    names = [name.name for name in found]
    assert names == sorted(set(names))
    assert len({len(folded(name)) for name in names}) <= 1
    for name in found:
        assert folded(name.name) in said
        assert name.source is EvidenceSource.READ_CODE
        assert name.after_lines == held_after(name.name)
        assert name.observation == f"lines holding the name: {len(name.after_lines)} after"
    assert names_of(" ".join(words), tags) == found


# The Structural verifier

CLAIM = "Rename Py_NOGIL to Py_GIL_DISABLED"
NARROW = [
    {"text": '"Py_NOGIL": 1,', "change": "removed", "path": "tests/test_tags.py"},
    {"text": '"Py_GIL_DISABLED": 1,', "change": "added", "path": "tests/test_tags.py"},
]
"""Texts narrower than the name: whole lines of the tests, which no other file holds."""


@pytest.mark.parametrize(
    "response",
    [finishes(texts=NARROW), finishes(texts=[]), finishes(texts="none"), finishes(names=["x"])],
    ids=["narrow texts", "no text", "an answer that cannot be read", "names of its own"],
)
def test_verifier_records_the_names_code_finds_whatever_its_role_finished_with(
    tags: FixtureRepository, response: str
) -> None:
    run = verify_structural_claim(
        CLAIM,
        repository=tags.path,
        before=tags.before,
        after=tags.after,
        model=ScriptedModel([response]),
    )

    assert run.inconclusive is None
    assert run.names == names_of(CLAIM, tags) == (a_name("Py_NOGIL", STILL_READ),)
    assert all(isinstance(piece, SearchEvidence) for piece in run.evidence)


def test_narrow_texts_no_longer_confirm_a_rename_that_skips_a_file(tags: FixtureRepository) -> None:
    run = verify_structural_claim(
        CLAIM,
        repository=tags.path,
        before=tags.before,
        after=tags.after,
        model=ScriptedModel([finishes(texts=NARROW)]),
    )

    assert compute_verdict(run.evidence, STRUCTURAL) == STRUCTURAL_CONFIRMED
    assert compute_verdict((*run.evidence, *run.names), STRUCTURAL) == NAME_KEPT


@pytest.mark.parametrize(
    "model",
    [
        ScriptedModel([asks("after_read_file", path="src/tags.py")]),
        UnreachableModel(),
    ],
    ids=["stopped at the step cap", "a model that failed"],
)
def test_verifier_that_came_to_no_answer_records_no_name(
    tags: FixtureRepository, model: ScriptedModel | UnreachableModel
) -> None:
    run = verify_structural_claim(
        CLAIM,
        repository=tags.path,
        before=tags.before,
        after=tags.after,
        model=model,
        step_cap=1,
    )

    assert run.inconclusive is not None
    assert run.names == ()


# The Verdict

STRUCTURAL_CONFIRMED = Computed(Verdict.CONFIRMED, verdict.STRUCTURAL_CONFIRMED)
STRUCTURAL_REFUTED = Computed(Verdict.REFUTED, verdict.STRUCTURAL_REFUTED)
NOTHING_SEARCHED = Computed(Verdict.INCONCLUSIVE, verdict.NOTHING_SEARCHED)
NAME_KEPT = Computed(Verdict.INCONCLUSIVE, verdict.NAME_KEPT)
KEPT_ELSEWHERE = Computed(Verdict.INCONCLUSIVE, verdict.KEPT_ELSEWHERE)
SEARCH_DECIDES_NOTHING = Computed(Verdict.INCONCLUSIVE, verdict.SEARCH_DECIDES_NOTHING)
NO_REPRODUCTION_RAN = Computed(Verdict.INCONCLUSIVE, verdict.NO_REPRODUCTION_RAN)


def name_on(lines: int, line: str = "FLAG = config('Py_NOGIL')") -> RemovedNameEvidence:
    """Evidence that a name is held by this many lines after the pull request."""
    return a_name("Py_NOGIL", *(f"src/tags.py:{number}:{line}" for number in range(1, lines + 1)))


KEPT, GONE = name_on(1), name_on(0)
SUPPORTS = a_search(REMOVED, 1, 0)
STILL_THERE = a_search(REMOVED, 1, 1)
SHOWS_NOTHING = a_search(ADDED, 0, 0)
ELSEWHERE = replace(SUPPORTS, path="tests/test_tags.py", after_lines_elsewhere=(STILL_READ,))


@pytest.mark.parametrize(
    ("evidence", "computed"),
    [
        ((SUPPORTS, GONE), STRUCTURAL_CONFIRMED),
        ((SUPPORTS, KEPT), NAME_KEPT),
        ((SUPPORTS, name_on(3)), NAME_KEPT),
        ((SUPPORTS, GONE, KEPT), NAME_KEPT),
        ((KEPT, SUPPORTS), NAME_KEPT),
        ((SHOWS_NOTHING, KEPT), NAME_KEPT),
        ((ELSEWHERE, KEPT), NAME_KEPT),
        ((ELSEWHERE, GONE), KEPT_ELSEWHERE),
        ((SHOWS_NOTHING, GONE), SEARCH_DECIDES_NOTHING),
        ((STILL_THERE, KEPT), STRUCTURAL_REFUTED),
        ((STILL_THERE, GONE), STRUCTURAL_REFUTED),
        ((KEPT,), NOTHING_SEARCHED),
        ((GONE,), NOTHING_SEARCHED),
    ],
)
def test_name_still_held_stops_confirmed_and_decides_nothing_else(
    evidence: tuple[Evidence, ...], computed: Computed
) -> None:
    assert compute_verdict(evidence, STRUCTURAL) == computed


@pytest.mark.parametrize("name", [KEPT, GONE])
def test_name_decides_nothing_for_a_behavioural_claim(name: RemovedNameEvidence) -> None:
    assert compute_verdict([name]) == NO_REPRODUCTION_RAN


def found(change: TextChange, before: int, after: int, elsewhere: int) -> SearchEvidence:
    lines = tuple(f"src/tags.py:{number}:x" for number in range(1, elsewhere + 1))
    return replace(a_search(change, before, after, path="t.py"), after_lines_elsewhere=lines)


FOUND_ON = st.integers(min_value=0, max_value=2)
SEARCHES = st.builds(found, st.sampled_from(TextChange), FOUND_ON, FOUND_ON, FOUND_ON)
NAMES = st.builds(name_on, FOUND_ON)
EVIDENCE = st.lists(SEARCHES | NAMES, max_size=5)


def still_there(piece: Evidence) -> bool:
    return (
        isinstance(piece, SearchEvidence)
        and piece.change is REMOVED
        and bool(piece.before_lines)
        and bool(piece.after_lines)
    )


def kept(piece: Evidence) -> bool:
    return isinstance(piece, RemovedNameEvidence) and bool(piece.after_lines)


@given(evidence=EVIDENCE)
def test_names_can_only_take_a_confirmed_away(evidence: list[Evidence]) -> None:
    searches = [piece for piece in evidence if isinstance(piece, SearchEvidence)]

    computed = compute_verdict(evidence, STRUCTURAL)
    without_names = compute_verdict(searches, STRUCTURAL)

    if computed.verdict is not without_names.verdict:
        assert (without_names, computed) == (STRUCTURAL_CONFIRMED, NAME_KEPT)
    if computed != without_names:
        assert computed == NAME_KEPT
    assert (computed == NAME_KEPT) == (
        bool(searches) and any(map(kept, evidence)) and not any(map(still_there, evidence))
    )
    assert (computed == STRUCTURAL_REFUTED) == any(map(still_there, evidence))
    assert (computed == NOTHING_SEARCHED) == (not searches)


@given(
    shuffled=EVIDENCE.flatmap(
        lambda evidence: st.tuples(st.just(evidence), st.permutations(evidence))
    )
)
def test_same_evidence_gives_the_same_verdict_in_whatever_order_with_names_among_it(
    shuffled: tuple[list[Evidence], Sequence[Evidence]],
) -> None:
    evidence, in_another_order = shuffled

    assert compute_verdict(in_another_order, STRUCTURAL) == compute_verdict(evidence, STRUCTURAL)


@given(searches=st.lists(SEARCHES, max_size=3), lines=FOUND_ON, name=st.text(), line=st.text())
def test_only_whether_a_name_has_lines_is_read_never_the_name_or_what_the_lines_hold(
    searches: list[SearchEvidence], lines: int, name: str, line: str
) -> None:
    told = replace(name_on(lines, line), name=name, observation=line)

    assert compute_verdict([*searches, told], STRUCTURAL) == compute_verdict(
        [*searches, name_on(min(lines, 1))], STRUCTURAL
    )
