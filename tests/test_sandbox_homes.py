"""Saving a commit's home directory and putting it back, tested in a real local container.

Every test here needs a container runtime, as the tests of the Sandbox boundary do.
"""

from pathlib import Path

import pytest
from conftest import FixtureRepository

from pr_verification.sandbox import ContainerSandbox, Limits, Phase, RunResult, SandboxFailed

IMAGE = "python:3.14-slim"
LIMITS = Limits(seconds=60.0, memory_bytes=512 * 1024 * 1024, processors=1.0)

# What an install phase leaves behind: a program to run, and a file only its owner may read.
INSTALL = """\
mkdir -p "$HOME/bin" "$HOME/.config"
printf '#!/bin/sh\\necho the tool ran\\n' > "$HOME/bin/tool"
chmod 755 "$HOME/bin/tool"
echo quiet > "$HOME/.config/settings"
chmod 600 "$HOME/.config/settings"
"""


def shell(sandbox: ContainerSandbox, script: str, commit: str) -> RunResult:
    return sandbox.run(("sh", "-c", script), commit=commit, phase=Phase.TEST, limits=LIMITS)


def save_home_after(script: str, repository: FixtureRepository, archive: Path) -> None:
    """Run a script against the commit before the pull request, in a Sandbox of its own, and
    save the home directory it leaves."""
    with ContainerSandbox(repository.path, IMAGE) as sandbox:
        ran = shell(sandbox, script, repository.before)
        assert ran.exit_status == 0, ran.output
        sandbox.save_home(repository.before, archive)


def test_home_saved_in_one_sandbox_is_put_back_in_another_for_another_commit(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    archive = tmp_path / "home.archive"
    save_home_after(INSTALL, fixture_repository, archive)
    use = """\
"$HOME/bin/tool" && cat "$HOME/.config/settings" && id -u &&
stat -c '%a %u' "$HOME/bin/tool" "$HOME/.config/settings"
"""

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        sandbox.restore_home(fixture_repository.after, archive)
        used = shell(sandbox, use, fixture_repository.after)

    assert used.exit_status == 0, used.output
    tool_said, settings_hold, user, tool, settings = used.output.splitlines()
    assert (tool_said, settings_hold) == ("the tool ran", "quiet")
    assert (tool, settings) == (f"755 {user}", f"600 {user}")


def test_later_run_can_change_what_was_put_back(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    archive = tmp_path / "home.archive"
    save_home_after(INSTALL, fixture_repository, archive)
    change = 'echo more >> "$HOME/.config/settings" && rm "$HOME/bin/tool" && touch "$HOME/bin/new"'

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        sandbox.restore_home(fixture_repository.after, archive)
        changed = shell(sandbox, change, fixture_repository.after)

    assert changed.exit_status == 0, changed.output


def test_putting_a_home_back_replaces_what_the_home_held_and_leaves_the_working_copy(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    archive = tmp_path / "home.archive"
    save_home_after(INSTALL, fixture_repository, archive)

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        shell(sandbox, 'touch "$HOME/stale" built-here', fixture_repository.after)
        sandbox.restore_home(fixture_repository.after, archive)
        listed = shell(sandbox, 'ls -A "$HOME"; echo --; ls -A', fixture_repository.after)

    home, working_copy = listed.output.split("--\n")
    assert sorted(home.split()) == [".config", "bin"]
    assert sorted(working_copy.split()) == ["built-here", "greeting.py"]


def test_saving_replaces_an_archive_that_is_already_there(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    archive = tmp_path / "home.archive"
    save_home_after('echo first > "$HOME/marker"', fixture_repository, archive)
    save_home_after('echo second > "$HOME/marker"', fixture_repository, archive)

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        sandbox.restore_home(fixture_repository.after, archive)
        marker = shell(sandbox, 'cat "$HOME/marker"', fixture_repository.after)

    assert marker.output.strip() == "second"


def test_save_that_fails_leaves_the_archive_that_was_there_or_none(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    there, not_there = tmp_path / "there.archive", tmp_path / "not-there.archive"
    there.write_bytes(b"saved earlier")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        for archive in (there, not_there):
            with pytest.raises(SandboxFailed):
                sandbox.save_home("0" * 40, archive)

    assert there.read_bytes() == b"saved earlier"
    assert not not_there.exists()


def test_file_that_save_home_did_not_write_is_refused(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    (tmp_path / "home.archive").write_bytes(b"not a saved home directory")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        with pytest.raises(SandboxFailed):
            sandbox.restore_home(fixture_repository.after, tmp_path / "home.archive")


def test_putting_a_home_back_writes_nothing_on_the_machine_that_started_the_sandbox(
    fixture_repository: FixtureRepository, tmp_path: Path
) -> None:
    kept = tmp_path / "kept"
    kept.mkdir()
    # A home directory that points out of itself, at a place on the starting machine.
    point_out = f'ln -s "{kept}" "$HOME/out" && ln -s "{kept}/planted" "$HOME/planted"'
    save_home_after(point_out, fixture_repository, kept / "home.archive")

    with ContainerSandbox(fixture_repository.path, IMAGE) as sandbox:
        sandbox.restore_home(fixture_repository.after, kept / "home.archive")

    assert list(kept.iterdir()) == [kept / "home.archive"]
