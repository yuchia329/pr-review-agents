"""The Structural cases of the evaluation: what the data holds, and how a case is made from a
real commit. No test here reaches the network: the data is read as it is, and cases are made
from a small repository of the test's own."""

from pathlib import Path

import pytest
from conftest import commit_snapshots, git
from structural_cases import (
    FALSE_CASE,
    KINDS,
    KeyText,
    StructuralCase,
    borne_out,
    built,
    key_verdict,
    load,
)

from pr_verification.evaluation import Part
from pr_verification.report import TextChange, Verdict

RENAME = (
    {"names.py": "def parse_all(): ...", "README.md": "Call parse_all."},
    {"names.py": "def parse_many(): ...", "README.md": "Call parse_many."},
    {"names.py": "def parse_many(): ...", "README.md": "Call parse_many.", "later.py": "later"},
)
KEY = (KeyText("parse_all", TextChange.REMOVED), KeyText("parse_many", TextChange.ADDED))


def a_case(clone: Path, *left_out: str) -> StructuralCase:
    """A case over the second commit of a repository made at `clone` from `RENAME`."""
    _, renamed, _ = commit_snapshots(clone, *RENAME)
    return StructuralCase(
        name="structural/rename/parse-many",
        part=Part.DEVELOPMENT,
        kind="rename",
        repository="example/names",
        commit=renamed,
        title="Rename parse_all to parse_many",
        description="",
        key=KEY,
        left_out=left_out,
    )


def files_of(repository: Path, commit: str) -> dict[str, str]:
    paths = git(repository, "ls-tree", "-r", "--name-only", commit).splitlines()
    return {path: git(repository, "show", f"{commit}:{path}") for path in paths}


# The data


def test_there_are_about_thirty_cases_and_no_two_share_a_name() -> None:
    names = [case.name for case in load()]
    assert 25 <= len(names) <= 35
    assert len(set(names)) == len(names)


@pytest.mark.parametrize("part", Part)
def test_each_part_covers_every_kind_with_true_and_false_cases(part: Part) -> None:
    cases = [case for case in load() if case.part is part]
    assert {case.kind for case in cases} == set(KINDS)
    assert {case.expected for case in cases} == {Verdict.CONFIRMED, Verdict.REFUTED}


def test_a_false_case_differs_from_its_true_case_only_in_what_is_left_out() -> None:
    cases = {case.name: case for case in load()}
    false_cases = [case for case in cases.values() if case.expected is Verdict.REFUTED]
    assert false_cases
    for false_case in false_cases:
        true_case = cases[false_case.name.removesuffix(FALSE_CASE)]
        assert not true_case.left_out and false_case.left_out
        shown = (true_case.part, true_case.repository, true_case.commit, true_case.title)
        assert shown == (
            false_case.part,
            false_case.repository,
            false_case.commit,
            false_case.title,
        )
        assert true_case.description == false_case.description


def test_no_commit_is_in_both_parts() -> None:
    parts: dict[tuple[str, str], set[Part]] = {}
    for case in load():
        parts.setdefault((case.repository, case.commit), set()).add(case.part)
    assert all(len(of_commit) == 1 for of_commit in parts.values())


# Making a case from a commit


def test_a_true_case_is_the_commit_over_its_parent(tmp_path: Path) -> None:
    structural = a_case(tmp_path / "clone")
    case = built(structural, tmp_path / "clone", tmp_path)
    shown = case.pull_request
    assert (case.name, case.part, case.expected) == (
        structural.name,
        Part.DEVELOPMENT,
        Verdict.CONFIRMED,
    )
    assert (shown.title, shown.description) == (structural.title, "")
    assert files_of(shown.repository, shown.before) == RENAME[0]
    assert files_of(shown.repository, shown.after) == RENAME[1]
    assert git(shown.repository, "rev-list", "--parents", "-1", shown.after).split()[1:] == [
        shown.before
    ]


def test_a_false_case_keeps_the_files_left_out_as_they_were(tmp_path: Path) -> None:
    case = built(a_case(tmp_path / "clone", "names.py"), tmp_path / "clone", tmp_path)
    shown = case.pull_request
    assert case.expected is Verdict.REFUTED
    assert files_of(shown.repository, shown.after) == {
        "names.py": RENAME[0]["names.py"],
        "README.md": RENAME[1]["README.md"],
    }


def test_what_came_after_the_commit_is_not_in_the_cases_repository(tmp_path: Path) -> None:
    case = built(a_case(tmp_path / "clone", "names.py"), tmp_path / "clone", tmp_path)
    repository = case.pull_request.repository
    later = git(tmp_path / "clone", "rev-parse", "HEAD")
    objects = git(repository, "cat-file", "--batch-all-objects", "--batch-check").split()
    assert later not in objects
    # Nor the file the false case leaves out, as the real commit has it.
    assert git(tmp_path / "clone", "rev-parse", "HEAD:names.py") not in objects


def test_a_file_left_out_must_be_one_the_commit_changes(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="later.py"):
        built(a_case(tmp_path / "clone", "later.py"), tmp_path / "clone", tmp_path)


def test_a_case_that_leaves_out_every_change_is_refused(tmp_path: Path) -> None:
    structural = a_case(tmp_path / "clone", "names.py", "README.md")
    with pytest.raises(ValueError, match="every change"):
        built(structural, tmp_path / "clone", tmp_path)


# The answer key


def test_the_key_of_a_true_case_gives_confirmed_and_of_its_false_case_refuted(
    tmp_path: Path,
) -> None:
    true_case = built(a_case(tmp_path / "clone"), tmp_path / "clone", tmp_path)
    false_case = built(a_case(tmp_path / "other", "names.py"), tmp_path / "other", tmp_path)
    assert key_verdict(true_case, KEY) is Verdict.CONFIRMED
    assert key_verdict(false_case, KEY) is Verdict.REFUTED


def test_a_key_bears_a_case_out_when_only_a_true_case_is_confirmed_by_it() -> None:
    assert borne_out(Verdict.CONFIRMED, Verdict.CONFIRMED)
    assert borne_out(Verdict.REFUTED, Verdict.REFUTED)
    assert borne_out(Verdict.REFUTED, Verdict.INCONCLUSIVE)
    assert not borne_out(Verdict.CONFIRMED, Verdict.INCONCLUSIVE)
    assert not borne_out(Verdict.CONFIRMED, Verdict.REFUTED)
    assert not borne_out(Verdict.REFUTED, Verdict.CONFIRMED)


ENTRY = """\
[[commit]]
name = "{name}"
part = "development"
repository = "example/names"
commit = "0000000000000000000000000000000000000000"
title = "Rename parse_all to parse_many"
key = [{key}]
"""
A_TEXT = '{ text = "parse_all", change = "removed" }'


@pytest.mark.parametrize(
    ("data", "refusal"),
    [
        (ENTRY.format(name="rename/parse-many", key=A_TEXT), "structural/<kind>/"),
        (ENTRY.format(name="structural/refactor/parse-many", key=A_TEXT), "structural/<kind>/"),
        (ENTRY.format(name="structural/rename/parse-many", key=A_TEXT) * 2, "more than one"),
        (ENTRY.format(name="structural/rename/parse-many", key=""), "no answer key"),
    ],
)
def test_data_that_cannot_be_cases_is_refused(tmp_path: Path, data: str, refusal: str) -> None:
    (tmp_path / "cases.toml").write_text(data)
    with pytest.raises(ValueError, match=refusal):
        load(tmp_path / "cases.toml")
