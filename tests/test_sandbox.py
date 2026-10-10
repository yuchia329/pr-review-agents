"""The Sandbox boundary, tested by running commands in a real local container.

Every test here needs a container runtime, and the network the first time, to fetch `IMAGE`.
"""

import socket
import time
from pathlib import Path

import pytest
from conftest import FixtureRepository, git

from pr_verification.sandbox import (
    STOP_WITHIN_SECONDS,
    Command,
    ContainerSandbox,
    LimitHit,
    Limits,
    Phase,
    RunResult,
    SandboxFailed,
)

IMAGE = "python:3.14-slim"
NO_SUCH_IMAGE = "registry.invalid/no-such-image:1"
MEBIBYTE = 1024 * 1024
LIMITS = Limits(seconds=60.0, memory_bytes=512 * MEBIBYTE, processors=1.0)
TWO_SECONDS = Limits(seconds=2.0, memory_bytes=512 * MEBIBYTE, processors=1.0)
LITTLE_MEMORY = Limits(seconds=60.0, memory_bytes=64 * MEBIBYTE, processors=1.0)
HALF_A_PROCESSOR = Limits(seconds=60.0, memory_bytes=512 * MEBIBYTE, processors=0.5)

OUTSIDE_HOST = "pypi.org"
# Exits with status 0 only when the host named as its argument was reached.
CONNECT = "import socket, sys; socket.create_connection((sys.argv[1], 443), timeout=10)"
# Exits with status 0 only when the host name given as its argument resolved.
RESOLVE = "import socket, sys; socket.gethostbyname(sys.argv[1])"

# Holds 600 megabytes if it is allowed to, and stops there so that a missing limit harms nothing.
HOLD_MEMORY = """\
print("started", flush=True)
held = [b"x" * 10_000_000 for _ in range(60)]
"""

# Two processes each ask for a whole processor for three seconds, then the share they got between
# them is printed: processor time used per second that passed.
BURN_TWO_PROCESSORS = """\
import os, time
started = time.monotonic()
for _ in range(2):
    if os.fork() == 0:
        while time.monotonic() - started < 3:
            pass
        os._exit(0)
for _ in range(2):
    os.wait()
print(sum(os.times()[:4]) / (time.monotonic() - started))
"""


def run_in(
    sandbox: ContainerSandbox,
    command: Command,
    commit: str,
    phase: Phase = Phase.TEST,
    limits: Limits = LIMITS,
) -> RunResult:
    return sandbox.run(command, commit=commit, phase=phase, limits=limits)


def run_once(
    repository: FixtureRepository,
    command: Command,
    phase: Phase = Phase.TEST,
    limits: Limits = LIMITS,
) -> RunResult:
    """One run against the commit after the pull request, in a Sandbox of its own."""
    with ContainerSandbox(repository.path, IMAGE) as sandbox:
        return run_in(sandbox, command, repository.after, phase, limits)


# Running a command


def test_run_returns_what_the_command_wrote_and_its_exit_status(
    fixture_repository: FixtureRepository,
) -> None:
    result = run_once(fixture_repository, ("sh", "-c", "echo to-output; echo to-error >&2; exit 3"))

    assert "to-output" in result.output
    assert "to-error" in result.output
    assert result.exit_status == 3
    assert result.limit_hit is None


def test_command_is_started_directly_and_never_through_a_shell(
    fixture_repository: FixtureRepository,
) -> None:
    argument = "$HOME && echo injected; ls *"

    result = run_once(fixture_repository, ("echo", argument))

    assert result.output.strip() == argument
    assert result.exit_status == 0


def test_program_that_does_not_exist_is_an_ordinary_result_that_says_why(
    fixture_repository: FixtureRepository,
) -> None:
    result = run_once(fixture_repository, ("no-such-program",))

    assert result.exit_status not in (0, None)
    assert result.limit_hit is None
    assert result.output.strip() != ""


def test_same_command_runs_against_the_code_before_the_pull_request_and_the_code_after_it(
    fixture_repository: FixtureRepository,
) -> None:
    command = ("cat", "greeting.py")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        before = run_in(sandbox, command, fixture_repository.before)
        after = run_in(sandbox, command, fixture_repository.after)

    assert (before.exit_status, after.exit_status) == (0, 0)
    assert 'return "hello"' in before.output
    assert 'return "hello there"' in after.output


# The working copy


def test_working_copy_holds_the_files_of_the_commit_and_nothing_else_from_the_repository(
    fixture_repository: FixtureRepository,
) -> None:
    (fixture_repository.path / "untracked.txt").write_text("never committed\n")

    result = run_once(fixture_repository, ("ls", "-A"))

    listed = result.output.split()
    assert "greeting.py" in listed
    assert ".git" not in listed
    assert "untracked.txt" not in listed


def test_run_sees_what_an_earlier_run_against_the_same_commit_left_behind(
    fixture_repository: FixtureRepository,
) -> None:
    leave = ("sh", "-c", "echo kept > in-working-copy && echo kept > ~/in-home")
    read = ("sh", "-c", "cat in-working-copy ~/in-home")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        installing = run_in(sandbox, leave, fixture_repository.after, Phase.INSTALL)
        testing = run_in(sandbox, read, fixture_repository.after, Phase.TEST)

    assert installing.exit_status == 0, installing.output
    assert testing.exit_status == 0, testing.output
    assert testing.output.split() == ["kept", "kept"]


def test_run_never_sees_what_a_run_against_the_other_commit_left_behind(
    fixture_repository: FixtureRepository,
) -> None:
    leave = ("sh", "-c", "touch left-by-the-other-commit ~/left-by-the-other-commit")
    look = ("sh", "-c", "ls -A . ~")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        left = run_in(sandbox, leave, fixture_repository.before)
        seen = run_in(sandbox, look, fixture_repository.after)

    assert left.exit_status == 0, left.output
    assert seen.exit_status == 0, seen.output
    assert "left-by-the-other-commit" not in seen.output


def test_nothing_a_run_does_changes_the_repository_outside_the_sandbox(
    fixture_repository: FixtureRepository,
) -> None:
    vandalise = ("sh", "-c", "echo overwritten > greeting.py && echo added > added.py")

    result = run_once(fixture_repository, vandalise)

    assert result.exit_status == 0, result.output
    assert git(fixture_repository.path, "status", "--porcelain") == ""
    assert git(fixture_repository.path, "rev-parse", "HEAD") == fixture_repository.after
    assert "hello there" in (fixture_repository.path / "greeting.py").read_text()


# Isolation


@pytest.mark.parametrize("phase", list(Phase))
def test_environment_of_the_process_that_started_the_sandbox_is_not_passed_in(
    fixture_repository: FixtureRepository, monkeypatch: pytest.MonkeyPatch, phase: Phase
) -> None:
    monkeypatch.setenv("PR_VERIFICATION_API_KEY", "key-set-outside")
    # The image sets this one itself, so inside it holds the image's value.
    monkeypatch.setenv("PYTHON_VERSION", "version-set-outside")

    result = run_once(fixture_repository, ("env",), phase)

    assert result.exit_status == 0, result.output
    assert "PR_VERIFICATION_API_KEY" not in result.output
    assert "key-set-outside" not in result.output
    assert "version-set-outside" not in result.output


def test_file_of_the_machine_that_started_the_sandbox_is_not_visible_inside(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    credentials = tmp_path / "only-outside-the-sandbox"
    credentials.write_text("token-kept-outside\n")

    read = run_once(fixture_repository, ("cat", str(credentials)))
    search = ("find", "/", "-name", credentials.name, "-o", "-name", "greeting.py")
    searched = run_once(fixture_repository, search)

    assert read.exit_status not in (0, None)
    assert "token-kept-outside" not in read.output
    assert "/greeting.py" in searched.output
    assert f"/{credentials.name}" not in searched.output


def test_container_runtimes_socket_is_not_visible_inside(
    fixture_repository: FixtureRepository,
) -> None:
    search = ("find", "/", "-name", "*.sock", "-o", "-name", "greeting.py")

    result = run_once(fixture_repository, search)

    assert "/greeting.py" in result.output
    assert ".sock" not in result.output


@pytest.mark.parametrize("phase", list(Phase))
def test_command_runs_as_a_user_that_is_not_root(
    fixture_repository: FixtureRepository, phase: Phase
) -> None:
    result = run_once(fixture_repository, ("id", "-u"), phase)

    assert result.exit_status == 0, result.output
    assert result.output.strip().isdigit()
    assert result.output.strip() != "0"


@pytest.mark.parametrize("phase", list(Phase))
def test_command_holds_no_capabilities_and_cannot_gain_privileges(
    fixture_repository: FixtureRepository, phase: Phase
) -> None:
    result = run_once(fixture_repository, ("cat", "/proc/self/status"), phase)

    status = dict(line.split(":", 1) for line in result.output.splitlines())
    for held in ("CapInh", "CapPrm", "CapEff", "CapAmb"):
        assert int(status[held], 16) == 0, held
    # The kernel's own word that running a set-user-ID program raises nothing.
    assert int(status["NoNewPrivs"]) == 1


def test_install_phase_can_reach_the_network(fixture_repository: FixtureRepository) -> None:
    address = socket.gethostbyname(OUTSIDE_HOST)

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        by_name, by_address = (
            run_in(
                sandbox, ("python", "-c", CONNECT, host), fixture_repository.after, Phase.INSTALL
            )
            for host in (OUTSIDE_HOST, address)
        )

    assert by_name.exit_status == 0, by_name.output
    assert by_address.exit_status == 0, by_address.output


def test_test_phase_can_reach_no_address_outside_and_resolve_no_host_name(
    fixture_repository: FixtureRepository,
) -> None:
    connect = ("python", "-c", CONNECT, socket.gethostbyname(OUTSIDE_HOST))
    resolve = ("python", "-c", RESOLVE, OUTSIDE_HOST)

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        connected = run_in(sandbox, connect, fixture_repository.after, Phase.TEST)
        resolved = run_in(sandbox, resolve, fixture_repository.after, Phase.TEST)

    assert connected.exit_status not in (0, None)
    assert resolved.exit_status not in (0, None)


# Limits


def test_run_still_going_at_its_time_limit_is_stopped_and_reported_as_timed_out(
    fixture_repository: FixtureRepository,
) -> None:
    outlast = ("sh", "-c", "echo started; sleep 300")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        # An earlier run against the commit: the image is fetched and the working copy exists.
        run_in(sandbox, ("true",), fixture_repository.after)
        called = time.monotonic()
        result = run_in(sandbox, outlast, fixture_repository.after, limits=TWO_SECONDS)
        took = time.monotonic() - called

    assert result.limit_hit is LimitHit.TIME
    assert result.exit_status is None
    assert "started" in result.output
    assert took <= TWO_SECONDS.seconds + STOP_WITHIN_SECONDS


def test_everything_a_timed_out_command_started_is_stopped_with_it(
    fixture_repository: FixtureRepository,
) -> None:
    keep_ticking = ("sh", "-c", "while true; do echo tick >> ticks; sleep 0.2; done & sleep 300")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        stopped = run_in(sandbox, keep_ticking, fixture_repository.after, limits=TWO_SECONDS)
        first = run_in(sandbox, ("cat", "ticks"), fixture_repository.after)
        time.sleep(1.5)
        second = run_in(sandbox, ("cat", "ticks"), fixture_repository.after)

    assert stopped.limit_hit is LimitHit.TIME
    assert "tick" in first.output
    assert second.output == first.output


def test_run_that_goes_over_its_memory_limit_is_stopped_and_reported(
    fixture_repository: FixtureRepository,
) -> None:
    result = run_once(fixture_repository, ("python", "-c", HOLD_MEMORY), limits=LITTLE_MEMORY)

    assert result.limit_hit is LimitHit.MEMORY
    assert result.exit_status is None
    assert "started" in result.output


def test_memory_limit_stops_the_whole_run_when_a_process_the_command_started_goes_over(
    fixture_repository: FixtureRepository,
) -> None:
    in_a_child = ("sh", "-c", 'python -c "$1"; sleep 2; touch carried-on', "sh", HOLD_MEMORY)

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        result = run_in(sandbox, in_a_child, fixture_repository.after, limits=LITTLE_MEMORY)
        time.sleep(3)
        left = run_in(sandbox, ("ls",), fixture_repository.after)

    assert result.limit_hit is LimitHit.MEMORY
    assert result.exit_status is None
    assert "carried-on" not in left.output


def test_processor_limit_slows_a_run_and_is_never_reported_as_hit(
    fixture_repository: FixtureRepository,
) -> None:
    burn = ("python", "-c", BURN_TWO_PROCESSORS)

    result = run_once(fixture_repository, burn, limits=HALF_A_PROCESSOR)

    assert result.exit_status == 0, result.output
    assert result.limit_hit is None
    # Held to half a processor; left alone, the two would have taken at least a whole one.
    assert float(result.output.split()[-1]) < 0.75


def test_run_slowed_past_its_time_limit_is_reported_as_timed_out(
    fixture_repository: FixtureRepository,
) -> None:
    one_processor_second = ("python", "-c", "import time\nwhile time.process_time() < 1: pass")
    a_quarter = Limits(seconds=2.0, memory_bytes=512 * MEBIBYTE, processors=0.25)

    result = run_once(fixture_repository, one_processor_second, limits=a_quarter)

    assert result.limit_hit is LimitHit.TIME
    assert result.exit_status is None


# The Sandbox itself


def test_commit_that_is_not_in_the_repository_fails_the_sandbox_itself(
    fixture_repository: FixtureRepository,
) -> None:
    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        with pytest.raises(SandboxFailed):
            run_in(sandbox, ("true",), "0" * 40)


def test_image_that_cannot_be_had_fails_the_sandbox_itself(
    fixture_repository: FixtureRepository,
) -> None:
    with pytest.raises(SandboxFailed):
        with ContainerSandbox(fixture_repository.path, NO_SUCH_IMAGE) as sandbox:
            run_in(sandbox, ("true",), fixture_repository.after)


def test_sandbox_entered_as_a_context_manager_is_itself_and_can_be_closed_again(
    fixture_repository: FixtureRepository,
) -> None:
    sandbox = ContainerSandbox(fixture_repository.path, IMAGE)

    with sandbox as entered:
        assert entered is sandbox
        run_in(entered, ("true",), fixture_repository.after)

    sandbox.close()
    sandbox.close()
