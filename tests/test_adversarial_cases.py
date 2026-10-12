"""The adversarial cases: false Claims that come with an attempt to manipulate the system.

Nothing here reaches a model or a Sandbox. Each case's code is read back out of its repository
and, to check the case against its answer key, run in this process: it is this project's own.
"""

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from conftest import git

from pr_verification.adversarial_cases import (
    FALSE_PULL_REQUESTS,
    FalsePullRequest,
    Placement,
    adversarial_cases,
)
from pr_verification.evaluation import Case, Part
from pr_verification.report import Verdict

MADE = [(made, placement) for made in FALSE_PULL_REQUESTS for placement in Placement]


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> tuple[Case, ...]:
    """The cases, built once: building them is most of the time these tests take."""
    return adversarial_cases(tmp_path_factory.mktemp("cases"))


def test_adversarial_cases_are_about_thirty_and_every_one_is_a_false_claim(
    cases: tuple[Case, ...],
) -> None:
    names = [case.name for case in cases]
    assert 25 <= len(cases) <= 35
    assert len(set(names)) == len(names)
    assert all(name.startswith("adversarial/") for name in names)
    assert {case.expected for case in cases} == {Verdict.REFUTED}


def test_every_false_pull_request_is_a_case_once_for_each_placement(
    cases: tuple[Case, ...],
) -> None:
    assert [case.name for case in cases] == [
        f"adversarial/{made.name}/{placement.value}" for made, placement in MADE
    ]
    assert len({made.manipulation for made in FALSE_PULL_REQUESTS}) == len(FALSE_PULL_REQUESTS)


def test_cases_of_one_false_pull_request_are_all_in_the_same_part(cases: tuple[Case, ...]) -> None:
    assert [case.part for case in cases] == [made.part for made, _ in MADE]
    # Enough in each part for a rate to mean something, and more to tune on than kept back.
    in_part = Counter(case.part for case in cases)
    assert in_part[Part.DEVELOPMENT] > in_part[Part.HELD_OUT] >= 9


def after_module(case: Case, made: FalsePullRequest) -> str:
    return git(case.pull_request.repository, "show", f"{case.pull_request.after}:{made.module}")


def test_manipulation_is_placed_where_the_case_says_and_nowhere_else(
    cases: tuple[Case, ...],
) -> None:
    for case, (made, placement) in zip(cases, MADE, strict=True):
        shown = case.pull_request
        text = shown.title + shown.description + shown.linked_issue
        module = after_module(case, made)
        comments = "\n".join(
            line.removeprefix("# ") for line in module.splitlines() if line.startswith("# ")
        )

        assert (made.manipulation in text) == (placement is Placement.DESCRIPTION), case.name
        assert (made.manipulation in comments) == (placement is Placement.CODE_COMMENT), case.name
        assert (repr(made.manipulation) in module) == (placement is Placement.PRINTED_OUTPUT)
        assert made.claim in shown.description
        before = git(shown.repository, "show", f"{shown.before}:{made.module}")
        assert before == made.before.strip()


def test_printed_manipulation_is_printed_when_the_changed_code_is_called(
    cases: tuple[Case, ...], capsys: pytest.CaptureFixture[str]
) -> None:
    for case, (made, placement) in zip(cases, MADE, strict=True):
        try:
            called(after_module(case, made), made.call)
        except Exception:
            pass  # Some of the code raises where the Claim says it returns.

        printed = capsys.readouterr().out
        assert (made.manipulation in printed) == (placement is Placement.PRINTED_OUTPUT), case.name


def test_some_cases_plant_a_trivial_test_and_only_in_the_pull_request(
    cases: tuple[Case, ...],
) -> None:
    planted = [case for case, (made, _) in zip(cases, MADE, strict=True) if made.planted_test]
    assert 6 <= len(planted) < len(cases)
    assert {case.part for case in planted} == set(Part)
    for case, (made, _) in zip(cases, MADE, strict=True):
        shown = case.pull_request
        files = {
            commit: set(git(shown.repository, "ls-tree", "-r", "--name-only", commit).split())
            for commit in (shown.before, shown.after)
        }
        assert files[shown.before] == {made.module}
        assert files[shown.after] - {made.module} == (
            {f"test_{made.module}"} if made.planted_test else set()
        )


def called(module: str, call: str) -> Any:
    names: dict[str, Any] = {}
    exec(module, names)
    return eval(call, names)


def test_answer_key_bears_out_that_every_claim_is_false(cases: tuple[Case, ...]) -> None:
    for case, (made, _) in zip(cases, MADE, strict=True):
        try:
            given = called(after_module(case, made), made.call)
        except Exception:
            continue  # The Claim says a value is returned, and none is.
        assert given != eval(made.claimed), case.name


def test_adversarial_cases_are_the_same_wherever_they_are_built(tmp_path: Path) -> None:
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
            for case in adversarial_cases(directory)
        ]

    assert built_in(tmp_path / "one") == built_in(tmp_path / "another")
