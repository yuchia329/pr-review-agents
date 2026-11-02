"""The Effects of a run that have no seam of their own: git commands, and names chosen at random.

An Effect is one call that leaves a run: one whose answer comes from outside what the run was
given. Every Effect of a run through `pr_verification.core.run`, given an orchestration, is
made through one of these seams, so that something other than this machine can answer it, as
a durable run answers each Effect it has already made from its Journal:

- a request to a model, through the `Model` the run is given (`pr_verification.model`);
- a bounded decision, through its `Decider` (`pr_verification.decision`);
- a Sandbox opened and closed, a command run in one, and its home directory saved or put
  back, through the orchestration's `open_sandbox` and the `SandboxWithHomes` that opens
  (`pr_verification.sandbox`);
- a Repo Profile loaded, saved, or discarded, through its `ProfileStore`, and installed
  dependencies looked for or discarded, through its `DependencyStore`
  (`pr_verification.repo_profile`);
- a reading of the clock, through the `Clock` of its `Ledger` (`pr_verification.budget`);
- a git command, and a name chosen at random, through the `Effects` in use, here.

So two runs with the fixed pipeline that are given the same, and whose Effects are answered
the same, make the same Effects in the same order and give equal `Report`s. Tracing is no
Effect: it records what a run does and changes nothing the run gives.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol


class Effects(Protocol):
    """Where git commands are run and names are chosen at random: on this machine, or by
    something that answers for it."""

    def git(
        self,
        repository: Path,
        arguments: tuple[str, ...],
        *,
        stdin: bytes | None,
        environment: Mapping[str, str],
    ) -> bytes:
        """What one git command printed, as `pr_verification.repository.git` says, with
        `arguments` after `git`, run in `repository`, given `stdin` to read if any, and with
        `environment` added to the environment `git_environment(repository)` gives.

        `environment` may hold a secret, such as a token to fetch with, so what keeps a record
        of Effects keeps nothing of it.

        Raises `subprocess.CalledProcessError` when git refuses, and `OSError` when it cannot
        be run.
        """
        ...

    def token_hex(self, size: int) -> str:
        """`size` bytes chosen at random, as `2 * size` lowercase hexadecimal digits. `size`
        is at least 1."""
        ...


class OnThisMachine:
    """The `Effects` of this machine: git is run here, as a process of its own, and names are
    chosen from the operating system's source of randomness, as `secrets.token_hex` chooses
    them."""

    def git(
        self,
        repository: Path,
        arguments: tuple[str, ...],
        *,
        stdin: bytes | None,
        environment: Mapping[str, str],
    ) -> bytes:
        """See `Effects.git`."""
        raise NotImplementedError

    def token_hex(self, size: int) -> str:
        """See `Effects.token_hex`."""
        raise NotImplementedError


@contextmanager
def effects_through(effects: Effects) -> Iterator[None]:
    """Make `effects` the `Effects` in use for as long as the `with` lasts.

    Inside the `with`, `effects_in_use` gives `effects`. Leaving it, however it is left, puts
    back what was in use before, so one `with` inside another holds only until it is left.

    The `Effects` in use are held as the context limits are (`shown_within`, in
    `pr_verification.context_bound`): in the context of the code that is running. A thread
    started with a copy of that context uses the same, and setting them in one thread changes
    them in no other.
    """
    raise NotImplementedError
    yield


def effects_in_use() -> Effects:
    """The `Effects` in use where this is called: those of the innermost `effects_through` it is
    called inside, and `OnThisMachine()` inside none.

    So a run that sets nothing runs git on this machine and chooses its names from this
    machine's randomness, as it did before this seam existed."""
    raise NotImplementedError
