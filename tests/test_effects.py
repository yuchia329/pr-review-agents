"""The Effects that have no seam of their own: git commands and names chosen at random.

Git and the operating system's randomness are what `OnThisMachine` answers with. Everything
else here answers from what a test prepared, and runs no git at all.
"""

import contextvars
import re
import subprocess
import threading
from collections.abc import Mapping
from pathlib import Path

import pytest
from conftest import commit_snapshots
from hypothesis import given
from hypothesis import strategies as st

from pr_verification.effects import Effects, OnThisMachine, effects_in_use, effects_through
from pr_verification.python_ecosystem import PythonEcosystem
from pr_verification.repository import git, resolve_commit

NOWHERE = Path("/no/repository/here")
"""A repository path where nothing exists, so a git run on this machine there would fail."""


class Answers:
    """`Effects` that answer as prepared and record what they were asked. They run no git."""

    def __init__(
        self,
        printed: bytes = b"",
        refusal: Exception | None = None,
        token: str = "0123456789abcdef" * 2,
    ) -> None:
        self._printed, self._refusal, self._token = printed, refusal, token
        self.git_asked: list[tuple[Path, tuple[str, ...], bytes | None, dict[str, str]]] = []
        self.tokens_asked: list[int] = []

    def git(
        self,
        repository: Path,
        arguments: tuple[str, ...],
        *,
        stdin: bytes | None,
        environment: Mapping[str, str],
    ) -> bytes:
        self.git_asked.append((repository, arguments, stdin, dict(environment)))
        if self._refusal is not None:
            raise self._refusal
        return self._printed

    def token_hex(self, size: int) -> str:
        self.tokens_asked.append(size)
        return self._token


def answers_effects(answers: Answers) -> Effects:
    """`answers` as the `Effects` it stands in for, so a type checker holds it to the protocol."""
    return answers


# Which Effects are in use


def test_with_none_set_the_effects_in_use_are_those_of_this_machine() -> None:
    assert isinstance(effects_in_use(), OnThisMachine)


def test_effects_set_are_in_use_inside_the_with_and_those_before_are_back_after_it() -> None:
    outer, inner = Answers(), Answers()

    with effects_through(answers_effects(outer)):
        assert effects_in_use() is outer
        with effects_through(answers_effects(inner)):
            assert effects_in_use() is inner
        assert effects_in_use() is outer
    assert isinstance(effects_in_use(), OnThisMachine)


def test_effects_set_are_put_back_however_the_with_is_left() -> None:
    with pytest.raises(KeyError), effects_through(answers_effects(Answers())):
        raise KeyError("left by an error")

    assert isinstance(effects_in_use(), OnThisMachine)


def test_thread_started_with_a_copy_of_the_context_uses_the_effects_set_there() -> None:
    answers = Answers()
    seen: list[Effects] = []

    with effects_through(answers_effects(answers)):
        context = contextvars.copy_context()
    thread = threading.Thread(target=context.run, args=(lambda: seen.append(effects_in_use()),))
    thread.start()
    thread.join()

    assert seen == [answers]


def test_effects_set_in_one_thread_are_in_use_in_no_other() -> None:
    entered, checked = threading.Event(), threading.Event()

    def sets_its_own() -> None:
        with effects_through(answers_effects(Answers())):
            entered.set()
            checked.wait(timeout=10)

    thread = threading.Thread(target=sets_its_own)
    thread.start()
    entered.wait(timeout=10)
    try:
        assert isinstance(effects_in_use(), OnThisMachine)
    finally:
        checked.set()
        thread.join()


# Git, through the Effects in use


def test_git_is_answered_by_the_effects_in_use_and_runs_no_git_of_its_own() -> None:
    answers = Answers(printed=b"what was printed\n")

    with effects_through(answers_effects(answers)):
        printed = git(NOWHERE, "status", "--short", stdin=b"read me", environment={"A": "1"})

    assert printed == b"what was printed\n"
    assert answers.git_asked == [(NOWHERE, ("status", "--short"), b"read me", {"A": "1"})]


def test_git_given_no_stdin_and_no_environment_asks_with_none_and_an_empty_environment() -> None:
    answers = Answers()

    with effects_through(answers_effects(answers)):
        git(NOWHERE, "log")

    assert answers.git_asked == [(NOWHERE, ("log",), None, {})]


def test_git_raises_what_the_effects_in_use_raise() -> None:
    refusal = subprocess.CalledProcessError(128, ["git", "log"], b"", b"fatal: refused")

    with (
        effects_through(answers_effects(Answers(refusal=refusal))),
        pytest.raises(subprocess.CalledProcessError) as raised,
    ):
        git(NOWHERE, "log")

    assert raised.value is refusal


def test_resolving_a_commit_is_answered_through_the_effects_in_use() -> None:
    answers = Answers(printed=b"0123abcd" * 5 + b"\n")

    with effects_through(answers_effects(answers)):
        resolved = resolve_commit(NOWHERE, "main")

    assert resolved == "0123abcd" * 5
    assert len(answers.git_asked) == 1


# The Effects of this machine


def test_git_on_this_machine_prints_what_git_prints_in_the_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    (after,) = commit_snapshots(repository, {"a.txt": "one\n"})

    printed = OnThisMachine().git(repository, ("rev-parse", "HEAD"), stdin=None, environment={})

    assert printed.decode().strip() == after


def test_git_on_this_machine_is_given_stdin_to_read(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    commit_snapshots(repository, {"a.txt": "one\n"})
    expected = subprocess.run(
        ["git", "hash-object", "--stdin"], input=b"x", capture_output=True, check=True
    ).stdout

    printed = OnThisMachine().git(
        repository, ("hash-object", "--stdin"), stdin=b"x", environment={}
    )

    assert printed == expected


def test_git_on_this_machine_has_the_environment_given_added_to_its_own(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    commit_snapshots(repository, {"a.txt": "one\n"})

    printed = OnThisMachine().git(
        repository,
        ("var", "GIT_COMMITTER_IDENT"),
        stdin=None,
        environment={"GIT_COMMITTER_NAME": "Someone Given"},
    )

    assert printed.decode().startswith("Someone Given ")


def test_git_on_this_machine_raises_when_git_refuses(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    commit_snapshots(repository, {"a.txt": "one\n"})

    with pytest.raises(subprocess.CalledProcessError):
        OnThisMachine().git(
            repository, ("rev-parse", "--verify", "no-such-ref"), stdin=None, environment={}
        )


@given(st.integers(min_value=1, max_value=64))
def test_token_on_this_machine_is_twice_as_many_lowercase_hexadecimal_digits_as_bytes(
    size: int,
) -> None:
    assert re.fullmatch(f"[0-9a-f]{{{2 * size}}}", OnThisMachine().token_hex(size))


def test_tokens_on_this_machine_are_chosen_anew_each_time() -> None:
    machine = OnThisMachine()

    assert machine.token_hex(16) != machine.token_hex(16)


# The name of the results file of a Reproduction


def test_results_file_of_a_reproduction_is_named_from_16_bytes_of_the_effects_in_use() -> None:
    token = "fedcba9876543210" * 2
    answers = Answers(token=token)

    with effects_through(answers_effects(answers)):
        placement = PythonEcosystem().place("def test_total():\n    assert True\n")

    assert answers.tokens_asked == [16]
    assert placement.take_results is not None
    assert any(token in part for part in placement.run)
    assert any(token in part for part in placement.take_results)
