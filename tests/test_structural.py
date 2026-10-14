"""The Structural verifier: what it reads with, and what code records of the texts it names."""

import json
from functools import cache
from pathlib import Path

import pytest
from conftest import (
    FixtureRepository,
    UnreachableModel,
    asks,
    assert_shows_only_whole_responses,
    commit_snapshots,
    finishes,
)
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification.agent import NO_SUCH_TOOL, PROGRESS, PROGRESS_INTERVAL, StepOutcome, Stopped
from pr_verification.decision import YES, Decider, DecisionFailed, ScriptedDecider
from pr_verification.model import Model, ModelRequest, ScriptedModel
from pr_verification.reading import NOTHING_FOUND, reading_tools
from pr_verification.report import EvidenceSource, SearchEvidence, TextChange
from pr_verification.structural import (
    TEXT_CAP,
    VERIFIER_COULD_NOT_FINISH,
    VERIFIER_STEP_CAP,
    VERIFIER_STEP_CAP_REACHED,
    StructuralRun,
    verify_structural_claim,
)

CLAIM = "Renames UserManager to AccountManager and raises retries to 3"
GUIDE = "Set retries = 3 to try three times.\n"
BEFORE = {
    "accounts/manager.py": "class UserManager:\n    pass\n\n\nmanager = UserManager()\n",
    "docs/guide.md": GUIDE,
    "settings.toml": "retries = 1\n",
}
AFTER = {
    "accounts/manager.py": "class AccountManager:\n    pass\n\n\nmanager = AccountManager()\n",
    "accounts/roles.py": "ADMIN = 1\n",
    "docs/guide.md": GUIDE,
    "settings.toml": "retries = 3\n",
}

OLD_NAME = {"text": "UserManager", "change": "removed"}
NEW_NAME = {"text": "AccountManager", "change": "added"}


@pytest.fixture(scope="module")
def accounts(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A pull request that renames a class, adds a file, and changes a setting."""
    path = tmp_path_factory.mktemp("structural") / "repository"
    before, after = commit_snapshots(path, BEFORE, AFTER)
    return FixtureRepository(path=path, before=before, after=after)


def added(texts: int) -> list[dict[str, str]]:
    """This many entries, each naming a different text as added."""
    return [{"text": f"Manager{number}", "change": "added"} for number in range(texts)]


def looks(steps: int) -> list[str]:
    """Responses in which the role reads a different file at each step, and never finishes."""
    return [asks("after_read_file", path=f"notes/{number}.txt") for number in range(steps)]


def names(*texts: object) -> str:
    """A response in which the Structural verifier finishes with the texts it names."""
    return finishes(texts=list(texts))


def verify_with(
    model: Model,
    repository: FixtureRepository,
    progress_guard: Decider | None = None,
    **caps: int,
) -> StructuralRun:
    return verify_structural_claim(
        CLAIM,
        repository=repository.path,
        before=repository.before,
        after=repository.after,
        model=model,
        progress_guard=progress_guard,
        **caps,
    )


def verify(repository: FixtureRepository, *responses: str) -> tuple[StructuralRun, ScriptedModel]:
    """What the verifier records when the model gives these responses, and what it was sent."""
    model = ScriptedModel(responses)
    return verify_with(model, repository), model


@cache
def held(repository: Path, commit: str, text: str) -> tuple[str, ...]:
    """Every line of the commit that holds the text, as the `search` tool gives them."""
    (search,) = (tool for tool in reading_tools(repository, commit) if tool.name == "search")
    found = search.run({"text": text})
    return () if found == NOTHING_FOUND else tuple(found.split("\n"))


# What is recorded


def test_each_text_the_verifier_names_is_searched_for_by_code_in_both_commits_and_recorded(
    accounts: FixtureRepository,
) -> None:
    run, model = verify(accounts, names(OLD_NAME, NEW_NAME))

    assert run.evidence == (
        SearchEvidence(
            source=EvidenceSource.READ_CODE,
            observation="lines holding the text: 2 before, 0 after",
            text="UserManager",
            change=TextChange.REMOVED,
            path=None,
            before_lines=(
                "accounts/manager.py:1:class UserManager:",
                "accounts/manager.py:5:manager = UserManager()",
            ),
            after_lines=(),
        ),
        SearchEvidence(
            source=EvidenceSource.READ_CODE,
            observation="lines holding the text: 0 before, 2 after",
            text="AccountManager",
            change=TextChange.ADDED,
            path=None,
            before_lines=(),
            after_lines=(
                "accounts/manager.py:1:class AccountManager:",
                "accounts/manager.py:5:manager = AccountManager()",
            ),
        ),
    )
    assert run.inconclusive is None
    assert run.run is not None and run.run.role == "structural-verifier"
    assert json.loads(model.requests[0].data) == {"task": {"claim": CLAIM}, "steps": []}
    assert CLAIM not in model.requests[0].instructions


@pytest.mark.parametrize(
    ("entry", "before", "after"),
    [
        (
            {"text": "retries = 3", "change": "added"},
            ["docs/guide.md:1:Set retries = 3 to try three times."],
            ["docs/guide.md:1:Set retries = 3 to try three times.", "settings.toml:1:retries = 3"],
        ),
        (
            {"text": "retries = 3", "change": "added", "path": "settings.toml"},
            [],
            ["settings.toml:1:retries = 3"],
        ),
        (
            {"text": "ADMIN", "change": "added", "path": "accounts/roles.py"},
            [],
            ["accounts/roles.py:1:ADMIN = 1"],
        ),
        ({"text": "retries", "change": "removed", "path": "../settings.toml"}, [], []),
        ({"text": "usermanager", "change": "removed"}, [], []),
        ({"text": "User.*", "change": "removed"}, [], []),
    ],
    ids=[
        "every file",
        "one file",
        "a file only one commit has",
        "a path that leads out of the commit",
        "another letter case",
        "a pattern",
    ],
)
def test_text_is_searched_for_as_written_in_every_file_or_in_the_one_file_its_path_names(
    accounts: FixtureRepository, entry: dict[str, str], before: list[str], after: list[str]
) -> None:
    run, _ = verify(accounts, names(entry))

    (evidence,) = run.evidence
    assert (evidence.text, evidence.change, evidence.path) == (
        entry["text"],
        TextChange(entry["change"]),
        entry.get("path"),
    )
    assert (evidence.before_lines, evidence.after_lines) == (tuple(before), tuple(after))
    assert evidence.observation == (
        f"lines holding the text: {len(before)} before, {len(after)} after"
    )


def test_what_code_found_is_recorded_whatever_the_verifier_said_the_pull_request_does(
    accounts: FixtureRepository,
) -> None:
    run, _ = verify(accounts, names(NEW_NAME | {"change": "removed"}))

    (evidence,) = run.evidence
    assert evidence.change is TextChange.REMOVED
    assert (evidence.before_lines, evidence.after_lines) == (
        (),
        held(accounts.path, accounts.after, "AccountManager"),
    )


# What the verifier reads with


def test_verifier_reads_both_commits_with_six_read_only_tools_and_has_none_that_runs_anything(
    accounts: FixtureRepository,
) -> None:
    arguments: dict[str, dict[str, object]] = {
        "list_files": {"directory": "accounts"},
        "search": {"text": "Manager"},
        "read_file": {"path": "accounts/manager.py"},
    }
    commits = {"before": accounts.before, "after": accounts.after}
    expected = {
        f"{commit}_{tool.name}": tool.run(arguments[tool.name])
        for commit, full in commits.items()
        for tool in reading_tools(accounts.path, full)
    }
    reads = [asks(name, **arguments[name.partition("_")[2]]) for name in expected]
    others = [
        asks("list_files"),
        asks("search", text="Manager"),
        asks("read_file", path="accounts/manager.py"),
        asks("run_command", command="pytest"),
    ]
    responses = [*reads, *others, names(OLD_NAME)]

    run = verify_with(ScriptedModel(responses), accounts, step_cap=len(responses))

    assert run.run is not None and len(expected) == 6
    assert [(step.outcome, step.tool, step.result) for step in run.run.steps[:6]] == [
        (StepOutcome.RAN_TOOL, name, result) for name, result in expected.items()
    ]
    assert expected["before_search"] != expected["after_search"]
    assert [(step.outcome, step.result) for step in run.run.steps[6:10]] == [
        (StepOutcome.REFUSED, NO_SUCH_TOOL)
    ] * len(others)
    assert len(run.evidence) == 1


# Answers that name no text to search for


@pytest.mark.parametrize(
    "entry",
    [
        "UserManager",
        ["UserManager", "removed"],
        {"change": "removed"},
        {"text": "UserManager"},
        {"text": "UserManager", "change": "removed", "before_lines": ["a.py:1:UserManager"]},
        {"text": 7, "change": "removed"},
        {"text": "", "change": "removed"},
        {"text": " \t", "change": "removed"},
        {"text": "class UserManager:\n    pass", "change": "removed"},
        {"text": "UserManager", "change": "renamed"},
        {"text": "UserManager", "change": None},
        {"text": "UserManager", "change": "removed", "path": 7},
        {"text": "UserManager", "change": "removed", "path": ""},
    ],
    ids=[
        "a string",
        "a list",
        "no text",
        "no change",
        "another member",
        "a number for a text",
        "an empty text",
        "a blank text",
        "a text of two lines",
        "a change that is neither value",
        "null for a change",
        "a number for a path",
        "an empty path",
    ],
)
def test_entry_that_is_malformed_is_left_out_and_the_rest_keep_their_order(
    accounts: FixtureRepository, entry: object
) -> None:
    run, _ = verify(accounts, names(NEW_NAME, entry, OLD_NAME))

    assert [evidence.text for evidence in run.evidence] == ["AccountManager", "UserManager"]
    assert run.inconclusive is None


@pytest.mark.parametrize(
    "answer",
    [
        finishes(),
        finishes(texts="UserManager"),
        finishes(texts=OLD_NAME),
        finishes(verdict="confirmed"),
        names(*added(TEXT_CAP + 1)),
        names(*added(TEXT_CAP), "left out"),
    ],
    ids=[
        "nothing",
        "a string",
        "an object",
        "a verdict",
        "more texts than the cap",
        "more entries than the cap, one of them malformed",
    ],
)
def test_answer_with_no_list_of_texts_or_with_more_than_the_cap_is_searched_for_nothing(
    accounts: FixtureRepository, answer: str
) -> None:
    run, _ = verify(accounts, answer)

    assert (run.evidence, run.inconclusive) == ((), None)
    assert run.run is not None and run.run.stopped is Stopped.FINISHED


def test_as_many_texts_as_the_cap_are_each_searched_for(accounts: FixtureRepository) -> None:
    run, _ = verify(accounts, names(*added(TEXT_CAP)))

    assert [evidence.text for evidence in run.evidence] == [
        entry["text"] for entry in added(TEXT_CAP)
    ]


# A run that comes to no answer


def test_verifier_that_never_finishes_stops_at_its_step_cap_and_the_claim_is_inconclusive(
    accounts: FixtureRepository,
) -> None:
    by_default = verify_with(ScriptedModel(looks(VERIFIER_STEP_CAP)), accounts)
    capped = verify_with(ScriptedModel(looks(2)), accounts, step_cap=2)

    for run, steps in ((by_default, VERIFIER_STEP_CAP), (capped, 2)):
        assert run.run is not None and run.run.stopped is Stopped.STEP_CAP
        assert len(run.run.steps) == steps
        assert run.evidence == ()
        assert run.inconclusive == VERIFIER_STEP_CAP_REACHED.format(steps=steps)


def test_model_that_fails_ends_the_work_with_no_run_no_evidence_and_the_reason(
    accounts: FixtureRepository,
) -> None:
    run = verify_with(UnreachableModel(), accounts)

    assert (run.run, run.evidence) == (None, ())
    assert run.inconclusive == VERIFIER_COULD_NOT_FINISH.format(
        failure="the model could not be reached"
    )


def test_verifiers_run_is_watched_by_the_progress_guard_and_a_guard_that_fails_ends_the_work(
    accounts: FixtureRepository,
) -> None:
    watching = ScriptedDecider([YES])
    failing = ScriptedDecider([DecisionFailed("the decision model gave no answer")])
    step_cap = PROGRESS_INTERVAL + 1

    watched = verify_with(ScriptedModel(looks(step_cap)), accounts, watching, step_cap=step_cap)
    failed = verify_with(ScriptedModel(looks(step_cap)), accounts, failing, step_cap=step_cap)

    ((state, question),) = watching.asked
    assert question == PROGRESS
    assert json.loads(state)["task"] == {"claim": CLAIM}
    assert watched.run is not None and len(watched.run.steps) == step_cap
    assert (failed.run, failed.evidence) == (None, ())
    assert failed.inconclusive == VERIFIER_COULD_NOT_FINISH.format(
        failure="the decision model gave no answer"
    )


class Fault(Exception):
    """An error that says nothing about the pull request: a fault in the system."""


class Faulty:
    """A stand-in for a model that fails in a way no model should."""

    def complete(self, request: ModelRequest) -> str:
        raise Fault("the disc is full")


def test_fault_is_raised_and_not_turned_into_a_reason(accounts: FixtureRepository) -> None:
    with pytest.raises(Fault):
        verify_with(Faulty(), accounts)


@pytest.mark.parametrize("step_cap", [0, -1])
def test_step_cap_under_one_is_rejected_before_the_model_is_asked(
    accounts: FixtureRepository, step_cap: int
) -> None:
    model = ScriptedModel([names(OLD_NAME)])

    with pytest.raises(ValueError):
        verify_with(model, accounts, step_cap=step_cap)

    assert model.requests == []


# Whatever the verifier names

# Among them a text of two lines, an empty one, and a blank one, each of which is left out.
TEXTS = st.text(alphabet="UserManagerAccount= 3\n", max_size=8) | st.sampled_from(
    ["Manager", "retries = ", "class ", "pass"]
)
ENTRIES = st.lists(
    st.fixed_dictionaries({"text": TEXTS, "change": st.sampled_from(["added", "removed"])}),
    max_size=TEXT_CAP,
    unique_by=lambda entry: entry["text"],
)


@settings(deadline=None, max_examples=25)
@given(entries=ENTRIES)
def test_whatever_texts_the_verifier_names_only_lines_the_commits_hold_become_evidence(
    accounts: FixtureRepository, entries: list[dict[str, str]]
) -> None:
    searched = [entry for entry in entries if entry["text"].strip() and "\n" not in entry["text"]]

    run, _ = verify(accounts, names(*entries))

    assert [(evidence.text, evidence.change) for evidence in run.evidence] == [
        (entry["text"], TextChange(entry["change"])) for entry in searched
    ]
    for evidence in run.evidence:
        assert evidence.source is EvidenceSource.READ_CODE
        assert evidence.before_lines == held(accounts.path, accounts.before, evidence.text)
        assert evidence.after_lines == held(accounts.path, accounts.after, evidence.text)


def test_structural_verifier_is_shown_only_whole_responses(accounts: FixtureRepository) -> None:
    _, model = verify(accounts, finishes(texts=[]))

    assert_shows_only_whole_responses(model.requests[0].instructions)
