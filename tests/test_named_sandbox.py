"""A named Sandbox, which outlives the instance that made it so that a lost worker does not lose
it. Every test here needs a container runtime, and the network the first time, to fetch
`IMAGE`."""

import secrets
import subprocess
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from conftest import FixtureRepository
from test_sandbox import IMAGE, LIMITS, run_in

from pr_verification.sandbox import ContainerSandbox, Phase, remove_sandbox

WRITE_MARK = ("python", "-c", "open('mark.txt', 'w').write('left by an earlier step')")
READ_MARK = ("python", "-c", "print(open('mark.txt').read())")


@pytest.fixture
def name() -> Iterator[str]:
    """A name no other test uses, whose Sandbox is removed once the test is over."""
    made = f"pr-verification-test-{secrets.token_hex(6)}"
    yield made
    remove_sandbox(made)


def labelled(name: str) -> list[str]:
    """The volumes and containers of the container runtime that still name `name`, as the
    runtime lists what holds `name` in a label's value."""
    found = []
    for kind in ("volume", "container"):
        listed = subprocess.run(
            ["docker", kind, "ls", *(["-a"] if kind == "container" else []), "-q"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        for each in listed:
            labels = subprocess.run(
                [
                    "docker",
                    kind,
                    "inspect",
                    "--format",
                    "{{json .Config.Labels}}" if kind == "container" else "{{json .Labels}}",
                    each,
                ],
                capture_output=True,
                text=True,
                check=False,
            ).stdout
            if name in labels:
                found.append(each)
    return found


def test_instance_made_later_with_the_same_name_is_the_same_sandbox(
    fixture_repository: FixtureRepository, name: str
) -> None:
    repository, commit = fixture_repository.path, fixture_repository.before
    with ContainerSandbox(repository, IMAGE, name=name, step="write") as first:
        assert (
            first.run(WRITE_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS).exit_status == 0
        )

    with ContainerSandbox(repository, IMAGE, name=name, step="read") as later:
        read = later.run(READ_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)

    assert read.output.strip() == "left by an earlier step"


def test_run_against_another_commit_of_the_named_sandbox_never_sees_what_one_left(
    fixture_repository: FixtureRepository, name: str
) -> None:
    repository = fixture_repository.path
    with ContainerSandbox(repository, IMAGE, name=name, step="write") as first:
        first.run(WRITE_MARK, commit=fixture_repository.before, phase=Phase.TEST, limits=LIMITS)

    with ContainerSandbox(repository, IMAGE, name=name, step="read") as later:
        read = later.run(
            READ_MARK, commit=fixture_repository.after, phase=Phase.TEST, limits=LIMITS
        )

    assert read.exit_status != 0


def test_removing_a_named_sandbox_leaves_nothing_of_it_and_a_second_removal_does_nothing(
    fixture_repository: FixtureRepository, name: str
) -> None:
    repository, commit = fixture_repository.path, fixture_repository.before
    with ContainerSandbox(repository, IMAGE, name=name, step="write") as first:
        first.run(WRITE_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)
    assert labelled(name)

    remove_sandbox(name)
    remove_sandbox(name)

    assert labelled(name) == []
    with ContainerSandbox(repository, IMAGE, name=name, step="read") as fresh:
        read = fresh.run(READ_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)
    assert read.exit_status != 0


def test_removing_a_name_nothing_was_made_with_does_nothing(name: str) -> None:
    remove_sandbox(name)


def test_sandbox_without_a_name_is_removed_by_closing_it_as_before(
    fixture_repository: FixtureRepository,
) -> None:
    repository, commit = fixture_repository.path, fixture_repository.before
    with ContainerSandbox(repository, IMAGE) as sandbox:
        sandbox.run(WRITE_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)
    with ContainerSandbox(repository, IMAGE) as other:
        read = other.run(READ_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)

    assert read.exit_status != 0


def test_step_tried_again_runs_from_the_start_while_its_earlier_attempt_is_left_running(
    fixture_repository: FixtureRepository, name: str
) -> None:
    repository, commit = fixture_repository.path, fixture_repository.before
    sleeper = ("python", "-c", "import time; print('started', flush=True); time.sleep(120)")
    with ContainerSandbox(repository, IMAGE, name=name, step="prepare") as first:
        first.run(WRITE_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)
    # The first attempt's process is lost while its run goes on: here, a thread left waiting.
    lost = ContainerSandbox(repository, IMAGE, name=name, step="tried")
    threading.Thread(
        target=lambda: run_in(lost, sleeper, commit=commit, limits=LIMITS), daemon=True
    ).start()
    time.sleep(10)

    started = time.monotonic()
    with ContainerSandbox(repository, IMAGE, name=name, step="tried") as again:
        read = again.run(READ_MARK, commit=commit, phase=Phase.TEST, limits=LIMITS)

    assert read.output.strip() == "left by an earlier step"
    assert time.monotonic() - started < 60


@pytest.mark.parametrize(
    ("name", "step"),
    [("Upper", "step"), ("a name", "step"), ("name", "under_score"), ("name", "slash/step")],
)
def test_name_or_step_with_other_than_lowercase_letters_digits_and_dashes_is_refused(
    tmp_path: Path, name: str, step: str
) -> None:
    with pytest.raises(ValueError):
        ContainerSandbox(tmp_path, IMAGE, name=name, step=step)
