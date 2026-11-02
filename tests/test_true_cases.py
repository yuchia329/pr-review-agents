"""The true cases: the adversarial cases' pull requests with code that does what they claim.

Nothing here reaches a model or a Sandbox. Each case's code is read back out of its repository
and, to hold the case to its answer key, run in this process: it is this project's own.
"""

from pathlib import Path
from typing import Any

import pytest
from conftest import git

from pr_verification.adversarial_cases import FALSE_PULL_REQUESTS
from pr_verification.evaluation import Case, Part
from pr_verification.report import Verdict
from pr_verification.true_cases import TRUE_PULL_REQUESTS, TruePullRequest, true_cases

RAISED = (KeyError, ValueError, ZeroDivisionError)
"""What the code before a case may raise where its Claim says a value is returned. Anything
else, such as a name the answer key spelt wrongly, is a fault in the case and fails the test."""


@pytest.fixture(scope="module")
def cases(tmp_path_factory: pytest.TempPathFactory) -> tuple[Case, ...]:
    """The cases, built once: building them is most of the time these tests take."""
    return true_cases(tmp_path_factory.mktemp("cases"))


def paired(cases: tuple[Case, ...]) -> list[tuple[Case, TruePullRequest]]:
    return list(zip(cases, TRUE_PULL_REQUESTS, strict=True))


def test_there_is_one_true_pull_request_for_each_false_one_in_its_order() -> None:
    assert [made.of for made in TRUE_PULL_REQUESTS] == list(FALSE_PULL_REQUESTS)


def test_true_cases_are_named_for_their_false_pull_request_and_expect_confirmed(
    cases: tuple[Case, ...],
) -> None:
    assert [case.name for case in cases] == [f"true/{made.name}" for made in FALSE_PULL_REQUESTS]
    assert {case.expected for case in cases} == {Verdict.CONFIRMED}


def test_each_true_case_is_in_the_part_of_its_false_pull_request(cases: tuple[Case, ...]) -> None:
    assert [case.part for case in cases] == [made.part for made in FALSE_PULL_REQUESTS]
    assert {case.part for case in cases} == set(Part)


def test_pull_request_text_is_the_false_pull_requests_title_and_claim_alone(
    cases: tuple[Case, ...],
) -> None:
    for case, made in paired(cases):
        shown = case.pull_request
        assert shown.title == made.of.title, case.name
        assert shown.description == made.of.claim, case.name
        assert shown.linked_issue == "", case.name


def test_repository_holds_the_module_before_and_after_and_nothing_else(
    cases: tuple[Case, ...],
) -> None:
    for case, made in paired(cases):
        shown = case.pull_request
        assert git(shown.repository, "rev-parse", f"{shown.after}^") == shown.before, case.name
        assert git(shown.repository, "rev-list", "--count", shown.after) == "2", case.name
        for commit, text in ((shown.before, made.of.before), (shown.after, made.after)):
            files = git(shown.repository, "ls-tree", "-r", "--name-only", commit).split()
            assert files == [made.of.module], case.name
            held = git(shown.repository, "show", f"{commit}:{made.of.module}")
            assert held == text.strip(), case.name


def test_no_manipulation_is_in_a_true_case(cases: tuple[Case, ...]) -> None:
    for case, made in paired(cases):
        shown = case.pull_request
        text = shown.title + shown.description + shown.linked_issue
        assert made.of.manipulation not in text, case.name
        assert made.of.manipulation not in made.after, case.name


def called(module: str, call: str) -> Any:
    names: dict[str, Any] = {}
    exec(module, names)
    return eval(call, names)


def test_answer_key_bears_out_that_every_claim_is_true_after_and_not_before(
    cases: tuple[Case, ...],
) -> None:
    for case, made in paired(cases):
        claimed = eval(made.of.claimed)
        after = git(
            case.pull_request.repository, "show", f"{case.pull_request.after}:{made.of.module}"
        )
        assert called(after, made.of.call) == claimed, case.name
        try:
            given = called(made.of.before, made.of.call)
        except RAISED:
            continue  # The Claim says a value is returned, and before none is.
        assert given != claimed, case.name


def test_true_cases_are_the_same_wherever_they_are_built(tmp_path: Path) -> None:
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
            for case in true_cases(directory)
        ]

    assert built_in(tmp_path / "one") == built_in(tmp_path / "another")
