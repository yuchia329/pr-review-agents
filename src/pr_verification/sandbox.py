"""The Sandbox boundary: the only place pull request code and Reproductions are ever run.

The Sandbox is used in one way: ask it to run a command against a commit and get back the output
and the exit status. The process that holds the model's API key stays outside it (ADR 0001).
"""

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import TracebackType
from typing import Protocol, Self

Command = tuple[str, ...]
"""A program and its arguments. It is started directly, never through a shell."""


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

    `run` is the only way to use it. A local container is the first implementation, and the
    boundary is this narrow so that a remote Sandbox could take its place.
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
            `commit`: not the starting user's home directory, not credentials, not the
            container runtime's socket.
            The command runs as a user that is not root (its user ID is not 0), holds no
            capabilities, and cannot gain privileges: a set-user-ID program does not raise them.
            In `Phase.INSTALL` the network is reachable. In `Phase.TEST` it is not.

        Limits
            Exceeding a limit is reported in the result and never raised. A command still
            running when `limits.seconds` have passed is stopped, along with everything it
            started, and `run` returns without waiting for it to finish: the result has
            `limit_hit` `LimitHit.TIME`. When the command and everything it started
            try to hold more than `limits.memory_bytes` between them, the whole run is stopped,
            whichever of its processes went over: the result has `limit_hit` `LimitHit.MEMORY`.
            In both cases `exit_status` is None and `output` holds what was written before the
            stop. `limits.processors` is enforced by slowing the run, as `Limits` describes.

        A command that ends by itself, with whatever exit status, is an ordinary result with
        `limit_hit` None. So is a command that cannot be started, such as a program that does
        not exist: its exit status is not 0 and the output says why.

        Raises `SandboxFailed` when the Sandbox itself cannot do the run.
        """
        ...


class ContainerSandbox:
    """The `Sandbox` as a local container (ADR 0001). It keeps every promise `Sandbox.run` makes.

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
        raise NotImplementedError

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        """See `Sandbox.run`."""
        raise NotImplementedError

    def close(self) -> None:
        """Remove everything this Sandbox made. Calling it again does nothing."""
        raise NotImplementedError

    def __enter__(self) -> Self:
        """Returns this Sandbox."""
        raise NotImplementedError

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Calls `close`, whether or not the block raised."""
        raise NotImplementedError
