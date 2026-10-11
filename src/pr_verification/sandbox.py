"""The Sandbox boundary: the only place pull request code and Reproductions are ever run.

The Sandbox is used in one way: ask it to run a command against a commit and get back the output
and the exit status. The process that holds the model's API key stays outside it (ADR 0001).
`SandboxWithHomes` adds the one other thing that crosses the boundary: a commit's home
directory, carried out and back as a file that the outside never opens.
"""

import io
import json
import os
import select
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import IO, Any, Protocol, Self

from pr_verification.repository import git, git_environment, resolve_commit

Command = tuple[str, ...]
"""A program and its arguments. It is started directly, never through a shell."""

STOP_WITHIN_SECONDS = 10.0
"""How long the Sandbox may take, beyond a run's time limit, to start the command and stop it."""


class Phase(StrEnum):
    """What a run is for, which decides whether it can reach the network. Neither holds secrets."""

    INSTALL = "install"
    """Installing dependencies. The network is reachable."""

    TEST = "test"
    """Running tests. There is no network: no address outside the Sandbox can be reached and no
    host name outside it resolves."""


@dataclass(frozen=True)
class Limits:
    """The most one run may use. All three are enforced on every run."""

    seconds: float
    """The longest the command may run, counted from when it starts. Fetching the image and
    preparing the working copy are not counted."""

    memory_bytes: int
    """The most memory the command and everything it starts may hold between them."""

    processors: float
    """The most processor time the run may use per second that passes: 1.0 is one processor.
    Code that asks for more is held to this and slowed, not stopped, so this limit is never
    reported as hit. A run slowed past `seconds` is reported as hitting the time limit."""


class LimitHit(StrEnum):
    """The limit a run was stopped for. The processor limit slows a run and never stops it."""

    TIME = "time"
    MEMORY = "memory"


@dataclass(frozen=True)
class RunResult:
    """What one run of a command in the Sandbox did. Its output is Untrusted Content."""

    output: str
    """Everything the command wrote to standard output and standard error, as one text."""

    exit_status: int | None
    """The command's exit status, or None when the run was stopped for hitting a limit."""

    limit_hit: LimitHit | None
    """The limit the run was stopped for, or None when the command ended by itself."""


class SandboxFailed(Exception):
    """The Sandbox itself could not do the run, whatever the code inside would have done.

    Raised when there is no container runtime, the image cannot be had, or the commit is not in
    the repository. Never raised for what a command does once it is inside.
    """


class Sandbox(Protocol):
    """The isolated environment in which pull request code and Reproductions are run.

    `run` is the only way to run anything in it. A local container is the first implementation,
    and the boundary is this narrow so that a remote Sandbox could take its place.
    """

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        """Run `command` against the code at `commit` and return its output and exit status.

        The same command can be run against the commit before the pull request and the commit
        after it: each commit has its own working copy and its own home directory.

        The working copy
            The command starts in the root of the working copy, which it may write to. The
            working copy holds the files of `commit` and nothing else from the repository: not
            its history, not its `.git` directory, not its untracked files. A run sees what
            earlier runs against the same commit left in the working copy and the home
            directory, so what an install phase installs there is present in the test phase.
            A run never sees what runs against another commit left behind. Nothing a run does
            changes the repository outside the Sandbox.

        Isolation
            No environment variable of the process that started the Sandbox is passed in: a
            variable set outside is absent inside, or holds the value the image gives it. No
            file of the machine that started the Sandbox is visible inside except the files of
            `commit`, and a home directory that `SandboxWithHomes.restore_home` put back: not
            the starting user's home directory, not credentials, not the container runtime's
            socket.
            The command runs as a user that is not root (its user ID is not 0), holds no
            capabilities, and cannot gain privileges: a set-user-ID program does not raise them.
            In `Phase.INSTALL` the network is reachable. In `Phase.TEST` it is not.

        Limits
            Exceeding a limit is reported in the result and never raised. A command still
            running when `limits.seconds` have passed is stopped, along with everything it
            started, and `run` returns without waiting for it to finish: the result has
            `limit_hit` `LimitHit.TIME`. Once an earlier run against the same commit has
            returned, so that the image is fetched and the working copy exists, `run` returns
            no later than `limits.seconds + STOP_WITHIN_SECONDS` after it was called.

            When the command and everything it started try to hold more than
            `limits.memory_bytes` between them, the whole run is stopped, whichever of its
            processes went over: the result has `limit_hit` `LimitHit.MEMORY`.

            In both cases `exit_status` is None and `output` holds what was written before the
            stop. `limits.processors` is enforced by slowing the run, as `Limits` describes.

        A command that ends by itself, with whatever exit status, is an ordinary result with
        `limit_hit` None. So is a command that cannot be started, such as a program that does
        not exist: its exit status is not 0 and the output says why.

        Raises `SandboxFailed` when the Sandbox itself cannot do the run.
        """
        ...


class SandboxWithHomes(Sandbox, Protocol):
    """A Sandbox whose home directories can be saved to a file and put back from one.

    The home directory of a commit is where its install phase leaves what it installs, so this
    is what lets installed dependencies be used again in another Sandbox.
    """

    def save_home(self, commit: str, archive: Path) -> None:
        """Save the home directory of `commit`, as the runs against it so far left it.

        `archive` is where the file goes, in a directory that exists. A file already there is
        replaced. The file is there only once it is whole: a save that fails leaves the file
        that was there before, or none. What the file looks like inside is the Sandbox's own
        business.

        Raises `SandboxFailed` when the Sandbox itself cannot do it.
        """
        ...

    def restore_home(self, commit: str, archive: Path) -> None:
        """Make the home directory of `commit` hold exactly what `archive` holds.

        `archive` is a file `save_home` wrote: in this Sandbox or in another with the same
        image, for this commit or for another. Whatever the home directory held before is
        gone. Each file and directory saved is back under the same name with the same contents
        and permissions, and belongs to the user that commands run as, so a later run can
        read, run, and change what an earlier install phase left. The working copy is not
        touched.

        What is in the archive was written by code that ran in a Sandbox. It is unpacked only
        inside one, never on the machine that started the Sandbox.

        Raises `SandboxFailed` when the Sandbox itself cannot do it, as when `archive` is not
        a file `save_home` wrote.
        """
        ...


_VOLUME = "/sandbox"
"""Where a commit's volume sits inside the container."""

_WORKING_COPY = "workspace"
"""The directory in a commit's volume that holds the files of the commit."""

_HOME = "home"
"""The directory in a commit's volume that is the home directory of its runs."""

_USER_ID = 1000
"""The user ID and group ID a command runs as, and that own its working copy. Not root's."""

_COULD_NOT_START = (126, 127)
"""The exit statuses the runtime gives a command it could not start: one that cannot be run, and
one that does not exist. Any other failure to start is the Sandbox's own."""

_KILLED = 137
"""The exit status of a command that was killed, as one that goes over the memory limit is."""

_OUT_OF_MEMORY_REPORTED_WITHIN = 2.0
"""How many seconds after a command is killed the runtime may take to report that it went over the
memory limit. That report and the report of its death come by different routes."""

# A home directory is saved and put back by commands run in the Sandbox's own image, as any
# command is, so an archive is written and unpacked only inside the Sandbox. The image has to
# hold `sh`, `tar`, `chmod`, and `find`.

_SAVE_HOME: Command = ("tar", "-c", "-f", "-", "-C", f"{_VOLUME}/{_HOME}", ".")
"""Writes a commit's home directory to standard output as one file: the archive."""

_RESTORE_HOME: Command = (
    "sh",
    "-c",
    # Emptied first, so that nothing is left over. An install phase can leave directories that
    # even their owner may not change, so the owner is let into all of them before that.
    f"chmod -R u+rwX {_VOLUME}/{_HOME} && find {_VOLUME}/{_HOME} -mindepth 1 -delete"
    f" && tar -x -p -f - -C {_VOLUME}/{_HOME}",
)
"""Makes a commit's home directory hold what the archive on standard input holds, and no more."""


class ContainerSandbox:
    """The `Sandbox` as a local container (ADR 0001). It keeps every promise `Sandbox.run` makes,
    and those of `SandboxWithHomes`.

    A container is a weaker boundary than a virtual machine: an escape through the kernel could
    reach the process that started it. That limit is documented, not removed.

    One Sandbox runs every commit in the same image. Use it as a context manager, or call
    `close` when done. Either removes the containers and working copies it made.
    """

    def __init__(self, repository: Path, image: str) -> None:
        """A Sandbox for the commits of `repository`, running commands in `image`.

        `repository` is the directory holding the repository. `image` names the container image
        that holds the tooling the commands need, as an Ecosystem's `Commands` give it.
        Raises `SandboxFailed` when there is no container runtime to use.
        """
        self._repository = repository
        self._image = image
        # The volume that holds the working copy and the home directory of each commit run so far.
        self._volumes: dict[str, str] = {}
        # Asking also finds out whether there is a runtime to use.
        self._processors = int(_docker("info", "--format", "{{.NCPU}}"))

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        """See `Sandbox.run`."""
        try:
            return self._run(command, self._volume_for(commit), phase, limits)
        except (OSError, subprocess.CalledProcessError) as error:
            # git or the container runtime could not be run, or git could not read the commit.
            raise SandboxFailed(f"the Sandbox could not do the run: {error}") from error

    def _run(self, command: Command, volume: str, phase: Phase, limits: Limits) -> RunResult:
        container = _docker(
            "create",
            *_held_in(volume, network="bridge" if phase is Phase.INSTALL else "none"),
            "--memory", str(limits.memory_bytes),
            # The same again for memory and swap together leaves no swap to spill into.
            "--memory-swap", str(limits.memory_bytes),
            # The runtime refuses a limit above what it has, where what it has is the limit.
            "--cpus", str(min(limits.processors, self._processors)),
            # Whatever the runtime is set up to do with output, it must be able to give it back.
            "--log-driver", "json-file",
            self._image,
            *command,
        ).strip()  # fmt: skip
        try:
            return _run_to_its_end(container, limits.seconds)
        finally:
            _docker("rm", "--force", "--volumes", container)

    def save_home(self, commit: str, archive: Path) -> None:
        """See `SandboxWithHomes.save_home`."""
        try:
            volume = self._volume_for(commit)
            # Written beside the archive, whose place it takes in one step once it is whole.
            descriptor, partial = tempfile.mkstemp(dir=archive.parent, prefix=f"{archive.name}.")
            try:
                with open(descriptor, "wb") as written:
                    self._carry_home(volume, _SAVE_HOME, stdout=written)
                os.replace(partial, archive)
            except BaseException:
                os.unlink(partial)
                raise
        except (OSError, subprocess.CalledProcessError) as error:
            raise SandboxFailed(f"the Sandbox could not save a home directory: {error}") from error

    def restore_home(self, commit: str, archive: Path) -> None:
        """See `SandboxWithHomes.restore_home`."""
        try:
            volume = self._volume_for(commit)
            with open(archive, "rb") as saved:
                self._carry_home(volume, _RESTORE_HOME, stdin=saved)
        except (OSError, subprocess.CalledProcessError) as error:
            raise SandboxFailed(
                f"the Sandbox could not restore a home directory: {error}"
            ) from error

    def _carry_home(
        self,
        volume: str,
        command: Command,
        *,
        stdin: IO[bytes] | None = None,
        stdout: IO[bytes] | None = None,
    ) -> None:
        """Run the command that writes or unpacks an archive, on a commit's volume and with no
        network. The archive goes straight from or to the file given: it is never read here."""
        _docker(
            "run",
            "--rm",
            *_held_in(volume, network="none"),
            # The runtime would otherwise keep a second copy of the archive as the run's output.
            "--log-driver", "none",
            # Without this the command is given no standard input to read an archive from.
            *(["--interactive"] if stdin else []),
            self._image,
            *command,
            stdin=stdin,
            stdout=stdout,
        )  # fmt: skip

    def close(self) -> None:
        """Remove everything this Sandbox made. Calling it again does nothing."""
        volumes, self._volumes = list(self._volumes.values()), {}
        if volumes:
            # One request, so that a volume the runtime will not remove does not keep the rest.
            _docker("volume", "rm", *volumes)

    def __enter__(self) -> Self:
        """Returns this Sandbox."""
        return self

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Calls `close`, whether or not the block raised."""
        self.close()

    def _volume_for(self, commit: str) -> str:
        """The volume for a commit, made and filled the first time the commit is run."""
        try:
            commit = resolve_commit(self._repository, commit)
        except subprocess.CalledProcessError as error:
            raise SandboxFailed(f"commit {commit!r} is not in {self._repository}") from error
        if commit not in self._volumes:
            volume = _docker("volume", "create").strip()
            try:
                self._fill(volume, commit)
            except BaseException:
                _docker("volume", "rm", volume)
                raise
            self._volumes[commit] = volume
        return self._volumes[commit]

    def _fill(self, volume: str, commit: str) -> None:
        """Put the files of a commit, and an empty home directory, into a new volume."""
        # A container that is never started, there so that the runtime copies into its volume.
        # Creating it also fetches the image.
        holder = _docker("create", "--volume", f"{volume}:{_VOLUME}", self._image, "true").strip()
        try:
            with tempfile.TemporaryFile() as archive:
                with tarfile.open(fileobj=archive, mode="w") as tar:
                    _archive_commit(self._repository, commit, tar)
                archive.seek(0)
                _docker("cp", "-", f"{holder}:{_VOLUME}", stdin=archive)
        finally:
            _docker("rm", "--force", "--volumes", holder)


def _held_in(volume: str, network: str) -> tuple[str, ...]:
    """The options that hold a command in, for a container made on a commit's volume.

    Nothing of the starting process is passed in: no environment, and no file but the volume.
    """
    return (
        # The command is started directly, not handed to a program the image names.
        "--entrypoint", "",
        "--volume", f"{volume}:{_VOLUME}",
        "--workdir", f"{_VOLUME}/{_WORKING_COPY}",
        "--env", f"HOME={_VOLUME}/{_HOME}",
        "--user", f"{_USER_ID}:{_USER_ID}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--network", network,
    )  # fmt: skip


def _docker(
    *arguments: str,
    stdin: IO[bytes] | None = None,
    stdout: IO[bytes] | None = None,
    stderr: int = subprocess.PIPE,
) -> str:
    """What one command to the container runtime printed, or nothing when it printed to a file
    given as `stdout`. Raises `SandboxFailed` when it fails."""
    try:
        done = subprocess.run(
            ["docker", *arguments],
            stdin=stdin,
            stdout=stdout or subprocess.PIPE,
            stderr=stderr,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as error:
        raise SandboxFailed(f"cannot run the container runtime: {error}") from error
    if done.returncode != 0:
        refusal = (done.stderr or done.stdout or "").strip()
        raise SandboxFailed(f"the container runtime failed to {arguments[0]}: {refusal}")
    return done.stdout or ""


def _state(container: str) -> dict[str, Any]:
    state: dict[str, Any] = json.loads(_docker("inspect", "--format", "{{json .State}}", container))
    return state


def _run_to_its_end(container: str, seconds: float) -> RunResult:
    """Start a created container, wait until it ends or reaches a limit, and say how it ended."""
    watching = [
        "docker", "events", "--since", "0", "--format", "{{.Action}}",
        "--filter", f"container={container}", "--filter", "event=oom", "--filter", "event=die",
    ]  # fmt: skip
    # Unbuffered, so that a report read here is never one the wait for the next cannot see.
    with subprocess.Popen(watching, stdout=subprocess.PIPE, bufsize=0) as events:
        try:
            started = subprocess.run(["docker", "start", container], capture_output=True, text=True)
            if started.returncode != 0:
                state = _state(container)
                if state["ExitCode"] not in _COULD_NOT_START:
                    raise SandboxFailed(f"cannot start a container: {started.stderr.strip()}")
                return RunResult(state["Error"], state["ExitCode"], limit_hit=None)
            # The time limit counts from here, where the command has started.
            event = _next_event(events, within=seconds)
            if event != "die":
                # At a limit, so everything in the container is stopped: a process that went over
                # the memory limit may not be the only one. A container that has ended meanwhile
                # refuses, and that is fine: its state says how it ended.
                subprocess.run(["docker", "kill", container], capture_output=True)
            elif _state(container)["ExitCode"] == _KILLED:
                # The command itself may have gone over the memory limit, with its death reported
                # first.
                event = _next_event(events, within=_OUT_OF_MEMORY_REPORTED_WITHIN) or event
        finally:
            events.kill()
    output = _docker("logs", container, stderr=subprocess.STDOUT)
    state = _state(container)
    if event == "oom" or state["OOMKilled"]:
        return RunResult(output, exit_status=None, limit_hit=LimitHit.MEMORY)
    if event is None:
        return RunResult(output, exit_status=None, limit_hit=LimitHit.TIME)
    return RunResult(output, state["ExitCode"], limit_hit=None)


def _next_event(events: subprocess.Popen[bytes], within: float) -> str | None:
    """What the runtime next reports of a watched container: `oom` when a process in it goes over
    the memory limit, `die` when it ends, or None when it reports nothing within that many seconds.
    """
    assert events.stdout is not None
    reported, _, _ = select.select([events.stdout], [], [], within)
    if not reported:
        return None
    event = events.stdout.readline().decode().strip()
    if event not in ("oom", "die"):
        raise SandboxFailed("the container runtime stopped reporting on the run")
    return event


def _archive_commit(repository: Path, commit: str, tar: tarfile.TarFile) -> None:
    """Write the files of a commit into `tar` as a working copy, with a home directory beside it.

    The files are read from git's object store. `git archive` would leave out or rewrite the
    ones a `.gitattributes` in the commit marks `export-ignore` or `export-subst`.
    """

    # Files are dated as a fresh checkout dates them: some packaging tools refuse the year 1970.
    checked_out = int(time.time())

    def add(name: str, kind: bytes, mode: int, content: bytes = b"", target: str = "") -> None:
        member = tarfile.TarInfo(name)
        member.type, member.mode, member.linkname = kind, mode, target
        member.uid = member.gid = _USER_ID
        member.mtime = checked_out
        member.size = len(content)
        tar.addfile(member, io.BytesIO(content))

    add(_HOME, tarfile.DIRTYPE, 0o755)
    add(_WORKING_COPY, tarfile.DIRTYPE, 0o755)
    reading = ["git", "cat-file", "--batch"]
    with subprocess.Popen(
        reading,
        cwd=repository,
        env=git_environment(repository),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
    ) as blobs:
        assert blobs.stdin is not None and blobs.stdout is not None
        # Each entry is `<mode> <type> <object>\t<path>`, a directory before what it holds.
        for entry in git(repository, "ls-tree", "-r", "-t", "-z", commit).split(b"\0")[:-1]:
            details, path = entry.split(b"\t", 1)
            mode, kind, object_id = details.split()
            name = f"{_WORKING_COPY}/{os.fsdecode(path)}"
            if kind != b"blob":
                # A directory, or a submodule: its files belong to another repository.
                add(name, tarfile.DIRTYPE, 0o755)
                continue
            blobs.stdin.write(object_id + b"\n")
            blobs.stdin.flush()
            answer = blobs.stdout.readline().split()
            if answer[1:2] != [b"blob"]:
                raise SandboxFailed(f"cannot read {name} at commit {commit} from {repository}")
            content = blobs.stdout.read(int(answer[2]))
            blobs.stdout.read(1)  # The line feed that ends each object.
            if mode == b"120000":
                add(name, tarfile.SYMTYPE, 0o777, target=os.fsdecode(content))
            else:
                add(name, tarfile.REGTYPE, int(mode[-3:], 8), content)
