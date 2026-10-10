import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from conftest import commit, git
from hypothesis import assume, given
from hypothesis import strategies as st
from tests_lock import is_test_path

SCRIPT = Path(__file__).parent.parent / "tools" / "tests_lock.py"


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A repository whose tests have merged to main, now on the impl/ branch for that group."""
    path = tmp_path / "repository"
    (path / "tests").mkdir(parents=True)
    (path / "src").mkdir()
    git(path, "init", "--quiet", "--initial-branch=main")
    (path / "tests" / "test_greeting.py").write_text("def test_greeting(): ...\n")
    (path / "src" / "greeting.py").write_text("")
    commit(path, "Add tests for greeting")
    git(path, "checkout", "--quiet", "-b", "impl/greeting")
    return path


def run_hook(
    working_directory: Path, tool_name: str, **tool_input: str
) -> subprocess.CompletedProcess[str]:
    """Send the lock one Claude Code hook event, as the harness would on stdin."""
    event = {"cwd": str(working_directory), "tool_name": tool_name, "tool_input": tool_input}
    return subprocess.run(
        [sys.executable, str(SCRIPT), "hook"],
        input=json.dumps(event),
        capture_output=True,
        text=True,
    )


def test_editing_a_test_file_on_an_impl_branch_is_refused(repository: Path) -> None:
    result = run_hook(repository, "Edit", file_path=str(repository / "tests" / "test_greeting.py"))

    assert result.returncode == 2
    assert "tests/test_greeting.py" in result.stderr


def test_creating_a_test_file_on_an_impl_branch_is_refused(repository: Path) -> None:
    result = run_hook(repository, "Write", file_path=str(repository / "tests" / "test_new.py"))

    assert result.returncode == 2
    assert "tests/test_new.py" in result.stderr


def test_editing_a_notebook_under_tests_on_an_impl_branch_is_refused(repository: Path) -> None:
    result = run_hook(
        repository, "NotebookEdit", notebook_path=str(repository / "tests" / "explore.ipynb")
    )

    assert result.returncode == 2
    assert "tests/explore.ipynb" in result.stderr


def test_editing_source_on_an_impl_branch_is_allowed(repository: Path) -> None:
    result = run_hook(repository, "Edit", file_path=str(repository / "src" / "greeting.py"))

    assert result.returncode == 0


@pytest.mark.parametrize("branch", ["main", "tests/greeting", "contract/greeting", "simplify"])
def test_editing_a_test_file_off_an_impl_branch_is_allowed(repository: Path, branch: str) -> None:
    git(repository, "checkout", "--quiet", "-B", branch)

    result = run_hook(repository, "Edit", file_path=str(repository / "tests" / "test_greeting.py"))

    assert result.returncode == 0


def change_a_test(repository: Path) -> str:
    (repository / "tests" / "test_greeting.py").write_text("def test_greeting(): assert True\n")
    return "tests/test_greeting.py"


def add_a_test(repository: Path) -> str:
    (repository / "tests" / "test_new.py").write_text("")
    return "tests/test_new.py"


def delete_a_test(repository: Path) -> str:
    (repository / "tests" / "test_greeting.py").unlink()
    return "tests/test_greeting.py"


def commit_a_changed_test(repository: Path) -> str:
    changed = change_a_test(repository)
    commit(repository, "Weaken the greeting test")
    return changed


ways_to_touch_tests = pytest.mark.parametrize(
    "touch_tests", [change_a_test, add_a_test, delete_a_test, commit_a_changed_test]
)


@ways_to_touch_tests
def test_shell_command_that_touched_tests_on_an_impl_branch_is_reported(
    repository: Path, touch_tests: Callable[[Path], str]
) -> None:
    touched = touch_tests(repository)

    result = run_hook(repository, "Bash", command="make tests-pass")

    assert result.returncode == 2
    assert touched in result.stderr


def test_shell_command_that_left_tests_alone_on_an_impl_branch_is_allowed(repository: Path) -> None:
    (repository / "src" / "greeting.py").write_text("def greet(): ...\n")
    commit(repository, "Implement greeting")
    (repository / "src" / "farewell.py").write_text("")

    result = run_hook(repository, "Bash", command="make tests-pass")

    assert result.returncode == 0


def test_tests_merged_to_main_after_the_impl_branch_started_are_not_its_changes(
    repository: Path,
) -> None:
    git(repository, "checkout", "--quiet", "main")
    (repository / "tests" / "test_farewell.py").write_text("")
    commit(repository, "Add tests for farewell")
    git(repository, "checkout", "--quiet", "impl/greeting")

    result = run_hook(repository, "Bash", command="make tests-pass")

    assert result.returncode == 0


@ways_to_touch_tests
def test_shell_command_that_touched_tests_off_an_impl_branch_is_allowed(
    repository: Path, touch_tests: Callable[[Path], str]
) -> None:
    git(repository, "checkout", "--quiet", "-b", "tests/farewell")
    touch_tests(repository)

    result = run_hook(repository, "Bash", command="make tests-pass")

    assert result.returncode == 0


def test_editing_a_test_file_from_a_working_directory_outside_the_repository_is_refused(
    repository: Path, tmp_path: Path
) -> None:
    result = run_hook(tmp_path, "Edit", file_path=str(repository / "tests" / "test_greeting.py"))

    assert result.returncode == 2
    assert "tests/test_greeting.py" in result.stderr


def test_editing_a_test_file_through_another_letter_case_of_its_path_is_refused(
    repository: Path,
) -> None:
    """On a file system that ignores case, Tests/ is tests/."""
    result = run_hook(repository, "Edit", file_path=str(repository / "Tests" / "test_greeting.py"))

    assert result.returncode == 2


def test_shell_command_outside_any_repository_is_allowed(tmp_path: Path) -> None:
    result = run_hook(tmp_path, "Bash", command="ls")

    assert result.returncode == 0


def test_tests_merged_upstream_are_not_the_changes_of_a_branch_started_from_there(
    repository: Path,
) -> None:
    git(repository, "checkout", "--quiet", "--detach")
    (repository / "tests" / "test_farewell.py").write_text("")
    upstream = commit(repository, "Add tests for farewell")
    git(repository, "update-ref", "refs/remotes/origin/main", upstream)
    git(repository, "checkout", "--quiet", "-b", "impl/farewell", "origin/main")

    result = run_hook(repository, "Bash", command="make tests-pass")

    assert result.returncode == 0


def test_event_the_lock_cannot_read_is_refused_rather_than_passed_unchecked() -> None:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "hook"], input="{}", capture_output=True, text=True
    )

    assert result.returncode == 2
    assert "could not be checked" in result.stderr


def test_lock_that_cannot_find_main_refuses_rather_than_passing_unchecked(repository: Path) -> None:
    git(repository, "branch", "--quiet", "-m", "main", "trunk")

    result = run_hook(repository, "Bash", command="make tests-pass")

    assert result.returncode == 2
    assert "could not be checked" in result.stderr


def run_on_pull_request(repository: Path, branch: str) -> subprocess.CompletedProcess[str]:
    """Run the lock as the workflow does: told the branch name, with no branch checked out."""
    git(repository, "checkout", "--quiet", "--detach")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "pull-request", "--branch", branch, "--base", "main"],
        cwd=repository,
        capture_output=True,
        text=True,
    )


def test_pull_request_from_an_impl_branch_that_touches_tests_is_refused(
    repository: Path,
) -> None:
    touched = commit_a_changed_test(repository)

    result = run_on_pull_request(repository, "impl/greeting")

    assert result.returncode != 0
    assert touched in result.stderr


def test_pull_request_from_an_impl_branch_that_leaves_tests_alone_is_allowed(
    repository: Path,
) -> None:
    (repository / "src" / "greeting.py").write_text("def greet(): ...\n")
    commit(repository, "Implement greeting")

    result = run_on_pull_request(repository, "impl/greeting")

    assert result.returncode == 0


def test_pull_request_from_a_tests_branch_that_touches_tests_is_allowed(
    repository: Path,
) -> None:
    commit_a_changed_test(repository)

    result = run_on_pull_request(repository, "tests/greeting")

    assert result.returncode == 0


ROOT = "/repository"
name = st.text(st.characters(categories=["L", "N"], include_characters="_-."), min_size=1).filter(
    lambda text: text not in {".", ".."}
)
names = st.lists(name, min_size=1, max_size=5)


@given(names)
def test_every_path_below_tests_is_a_test_path(below: list[str]) -> None:
    path = "/".join(["tests", *below])

    assert is_test_path(path, ROOT)
    assert is_test_path(f"{ROOT}/{path}", ROOT)


@given(names)
def test_no_path_that_starts_elsewhere_is_a_test_path(parts: list[str]) -> None:
    assume(parts[0].casefold() != "tests")
    path = "/".join(parts)

    assert not is_test_path(path, ROOT)
    assert not is_test_path(f"{ROOT}/{path}", ROOT)


@given(st.booleans(), names, name, st.data())
def test_detour_through_another_directory_does_not_change_the_answer(
    below_tests: bool, parts: list[str], aside: str, data: st.DataObject
) -> None:
    direct = ["tests", *parts] if below_tests else parts
    at = data.draw(st.integers(min_value=0, max_value=len(direct) - 1))
    detour = [*direct[:at], aside, "..", *direct[at:]]

    assert is_test_path("/".join(detour), ROOT) == is_test_path("/".join(direct), ROOT)
