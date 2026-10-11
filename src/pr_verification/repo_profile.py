"""The Repo Profile and the dependency cache: what the system keeps between pull requests.

Working out how to install and test a repository, and installing its dependencies, is the
slowest and most repeated work of a run. Both are kept between runs. Both are filled only from
the target branch and only by code: nothing a pull request holds or prints, and nothing a model
writes, is ever saved (ADR 0004).

`prepare_target` is the way in, and `prepare_pull_request` follows it. A stored install command
that stops working is found by `prepare_target`. The stored test command is run by whoever
gathers Evidence with it: when that caller finds it no longer works against the target commit,
it calls `ProfileStore.discard`, and the next run learns the profile again.
"""

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Protocol

from pr_verification.ecosystem import Commands, Results, Unsupported, find_commands
from pr_verification.repository import git
from pr_verification.sandbox import (
    Command,
    Limits,
    Phase,
    RunResult,
    SandboxFailed,
    SandboxWithHomes,
)

Discover = Callable[[Path, str], Commands | Unsupported]
"""The shape of `pr_verification.ecosystem.find_commands`, which is what discovery is."""

SandboxFor = Callable[[str], SandboxWithHomes]
"""Opens a Sandbox for the repository's commits that runs in the image named. Each call gives a
new Sandbox, in which no run has been made. Closing every Sandbox it opened is the caller's to
do, whichever of them an answer names."""


@dataclass(frozen=True)
class RepoProfile:
    """The facts the system remembers about a repository between pull requests.

    Every fact was worked out from the files of one commit on the target branch and then proven
    by a successful run in the Sandbox against that commit, as `prepare_target` describes.
    `image`, `install`, `test`, `test_framework`, `test_layout`, and `lock_files` are each as
    the `Commands` found for that commit had them.
    """

    image: str
    """The container image the commands run in."""

    install: tuple[Command, ...]
    """The install command: the commands that install the repository's dependencies, in order."""

    test: Command
    """The test command."""

    test_framework: str
    """The test framework the test command runs."""

    test_layout: tuple[str, ...]
    """The test layout: the directories that hold the repository's test files."""

    lock_files: tuple[str, ...]
    """The paths of the files that make up the repository's dependency lock file."""

    lock_digest: str
    """`digest_lock_files` for the commit the facts were proven against, under `lock_files`.
    The profile still holds for a later commit of the target branch only while that commit
    gets the same digest."""


def digest_lock_files(repository: Path, commit: str, lock_files: tuple[str, ...]) -> str:
    """A short text that stands for the contents of the dependency lock file at `commit`.

    Reads the files of `commit` at the paths in `lock_files`, each a path from the root of the
    repository, and nothing else: the state of the working tree does not matter.

    Two commits, of one repository or of two, get the same digest when they agree on every
    path in `lock_files`: both hold a file there with the same contents, or neither holds a
    file. They get different digests when they disagree on any: a file that is missing is not
    a file that is empty. With no paths, every commit gets the same digest.

    Raises as `pr_verification.repository.git` does when `commit` is not in the repository.
    """
    # Each entry is `<mode> <type> <object>\t<path>`. A submodule is listed and is not a file.
    listing = os.fsdecode(git(repository, "ls-tree", "-r", "-z", "--end-of-options", commit))
    files = {
        path: details.split()[2]
        for details, _, path in (entry.partition("\t") for entry in listing.split("\0"))
        if " blob " in details
    }
    # The contents are read, not taken from git's name for them: two repositories can name the
    # same contents differently.
    held = [
        (path, hashlib.sha256(git(repository, "cat-file", "blob", files[path])).hexdigest())
        if path in files
        # Nothing stands for a file that is missing, as no contents can: an empty file has some.
        else (path, None)
        for path in lock_files
    ]
    return _digest(held)


def _digest(value: object) -> str:
    """A short text that stands for `value`, which is text, nothing, or lists of these: the same
    for values that are equal, and another for any that are not."""
    return hashlib.sha256(json.dumps(value).encode()).hexdigest()


def _kept_for(repository: Path, directory: Path) -> Path:
    """The directory under `directory` that holds what is kept for `repository`.

    It is named after the resolved path of the repository and need not exist yet. Raises
    `ValueError` when `directory` is the directory of `repository` or lies inside it.
    """
    directory, repository = directory.resolve(), repository.resolve()
    if directory == repository or repository in directory.parents:
        raise ValueError(
            f"{directory} is in the repository {repository}, where a pull request could put a file"
        )
    return directory / _digest(str(repository))


def _is_texts(kept: object) -> bool:
    """Whether `kept` is a list of texts, as JSON gives back a tuple of them."""
    return isinstance(kept, list) and all(isinstance(text, str) for text in kept)


class ProfileStore(Protocol):
    """Where Repo Profiles are kept between runs: somewhere a pull request cannot modify.

    One repository has at most one profile. A profile is only ever read from a store, never
    from the repository or from anything a pull request can change.
    """

    def load(self, repository: Path) -> RepoProfile | None:
        """The profile last saved for `repository` and not discarded since, or None."""
        ...

    def save(self, repository: Path, profile: RepoProfile) -> None:
        """Keep `profile` as the profile of `repository`, in place of any kept before."""
        ...

    def discard(self, repository: Path) -> None:
        """Forget the profile of `repository`. Does nothing when there is none."""
        ...


class DirectoryStore:
    """The `ProfileStore` as a directory outside the repository.

    A repository is told from another by the path of its directory once resolved, so the same
    directory reached by another route is the same repository. What is saved outlasts the
    store: another `DirectoryStore` on the same directory, in a later run, loads it.

    `load`, `save`, and `discard` each raise `ValueError` when `directory` is the directory of
    `repository` or lies inside it, both resolved: a pull request could put a file there.
    """

    def __init__(self, directory: Path) -> None:
        """A store that keeps its profiles under `directory`, which need not exist yet."""
        self._directory = directory

    def load(self, repository: Path) -> RepoProfile | None:
        """See `ProfileStore.load`. The profile loaded equals the one saved.

        What is kept for a repository but cannot be read back as a profile, as when its file
        was damaged or left by another version of the system, is no profile: None.
        """
        file = self._file(repository)
        try:
            kept = json.loads(file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (ValueError, RecursionError):
            # Damaged: not text, or not JSON.
            return None
        match kept:
            case {
                "image": str(image),
                "install": [*install],
                "test": [*test],
                "test_framework": str(test_framework),
                "test_layout": [*test_layout],
                "lock_files": [*lock_files],
                "lock_digest": str(lock_digest),
                **others,
            } if not others and all(map(_is_texts, (*install, test, test_layout, lock_files))):
                return RepoProfile(
                    image,
                    tuple(map(tuple, install)),
                    tuple(test),
                    test_framework,
                    tuple(test_layout),
                    tuple(lock_files),
                    lock_digest,
                )
        # Not what `save` writes, so left by another version of the system.
        return None

    def save(self, repository: Path, profile: RepoProfile) -> None:
        """See `ProfileStore.save`."""
        file = self._file(repository)
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(json.dumps(asdict(profile), indent=2), encoding="utf-8")

    def discard(self, repository: Path) -> None:
        """See `ProfileStore.discard`."""
        self._file(repository).unlink(missing_ok=True)

    def _file(self, repository: Path) -> Path:
        """Where the profile of `repository` is kept. Raises `ValueError` as the store does."""
        return _kept_for(repository, self._directory) / "profile.json"


class DependencyCache:
    """Installed dependencies kept between runs in a directory, keyed by the lock file.

    What is kept is the home directory that a commit's install commands left, as
    `SandboxWithHomes.save_home` writes it: one archive for each repository, each image, and
    each digest of the lock file. The cache only says where an archive belongs.
    `prepare_target` is the one place that writes one, and only from the target branch.
    """

    def __init__(self, directory: Path) -> None:
        """A cache that keeps its archives under `directory`, which need not exist yet."""
        self._directory = directory

    def archive(self, repository: Path, image: str, lock_digest: str) -> Path:
        """Where the archive for `repository`, run in `image`, with one digest of its lock file
        belongs.

        The path of a file under `directory`, in a directory that exists once this returns.
        The file itself exists only when installed dependencies have been saved there.

        The same repository, image, and digest always give the same path, from this cache or
        from another on the same directory. A different repository, told apart as
        `DirectoryStore` tells them, a different image, or a different digest gives the path
        of a different file, so the dependencies installed for one lock file are never used
        for another, nor those installed in one image in another. `image` and `lock_digest`
        may each be any text.

        Raises `ValueError` when `directory` is the directory of `repository` or lies inside
        it, both resolved: a pull request could put a file there.
        """
        kept = _kept_for(repository, self._directory)
        kept.mkdir(parents=True, exist_ok=True)
        return kept / f"{_digest([image, lock_digest])}.home"


@dataclass(frozen=True)
class CommandFailed:
    """A command needed to prepare a commit did not work. This is an answer, not an error."""

    command: Command
    """The first command that did not work. No command after it was run."""

    run: RunResult
    """What that command did. Its output is Untrusted Content."""


@dataclass(frozen=True)
class Prepared:
    """The target commit with its dependencies installed, and the Repo Profile that did it."""

    profile: RepoProfile
    """The Repo Profile to verify the pull request with."""

    sandbox: SandboxWithHomes
    """The Sandbox in which the target commit's dependencies are installed, opened with
    `profile.image`."""

    learned: bool
    """True when the profile was learned in this run, False when the stored one was used."""


def prepare_target(
    repository: Path,
    target: str,
    *,
    store: ProfileStore,
    cache: DependencyCache,
    sandbox_for: SandboxFor,
    limits: Limits,
    discover: Discover = find_commands,
) -> Prepared | Unsupported | CommandFailed:
    """Install the target commit's dependencies, learning the Repo Profile only when needed.

    `target` is the commit on the target branch that the pull request starts from: the code
    before the pull request. It is the only commit read or run here, and nothing of the pull
    request is passed in, so nothing in a pull request can change the profile it is verified
    with. Every run is against `target`, with `limits`.

    Below, the digest is `digest_lock_files(repository, target, lock_files)`, with the
    `lock_files` of the profile or the `Commands` at hand, and the archive is
    `cache.archive(repository, image, digest)`, with their `image`.

    With a stored profile that still holds
        `store.load(repository)` is asked first. A profile whose `lock_digest` is the digest
        still holds, and `discover` is not called. In `sandbox_for(profile.image)`: when the
        archive exists, it is put back with `restore_home(target, archive)`; then each of
        `profile.install` is run in `Phase.INSTALL`, in order. Putting the archive back never
        stands in for running them: it leaves them less to do. When each has exited with
        status 0, the home directory is saved with `save_home(target, archive)` if the archive
        did not exist, and the answer is `Prepared` with that profile, that Sandbox, and
        `learned` False. Nothing is saved to the store.

        When `restore_home` raises `SandboxFailed`, the archive is deleted before the error
        goes on, so that the next run installs from nothing and does not meet the same
        archive again.

    Discarding
        A stored profile is discarded with `store.discard(repository)`, and the profile is
        learned again, when:

        - its `lock_digest` is not the digest, in which case no Sandbox is opened for it and
          none of its commands is run; or
        - one of its install commands does not exit with status 0, which includes being
          stopped at a limit, in which case those after it are not run.

    Learning
        When no profile is stored, or the stored one has just been discarded,
        `discover(repository, target)` is called once. `Unsupported` is returned as it is,
        and no Sandbox is opened for it.
        Given `Commands`, in a Sandbox from a new call of `sandbox_for(commands.image)` and
        with nothing put back from the cache:

        1. each of `commands.install` is run in `Phase.INSTALL`, in order;
        2. the home directory is saved with `save_home(target, archive)`, in place of an
           archive already there;
        3. `commands.test` is run in `Phase.TEST`, and the run is read with
           `commands.ecosystem.read_results`.

        The facts are proven when every install command exited with status 0 and the test run
        was read as `Results`, whether its tests passed or failed. Only then is
        `store.save(repository, profile)` called, with the profile `RepoProfile` describes,
        and the answer is `Prepared` with that profile, that Sandbox, and `learned` True.

        Otherwise the answer is `CommandFailed` with the first command that did not work and
        its run. No step after it is taken, so an install command that fails leaves the
        archive as it was, and nothing is saved to the store.

    Raises `SandboxFailed` when a Sandbox itself cannot do what is asked of it, and
    `ValueError` as `DirectoryStore` and `DependencyCache` do.
    """
    profile = store.load(repository)
    if profile is not None:
        prepared = _prepare_with(profile, repository, target, cache, sandbox_for, limits)
        if prepared is not None:
            return prepared
        store.discard(repository)

    commands = discover(repository, target)
    if isinstance(commands, Unsupported):
        return commands
    digest = digest_lock_files(repository, target, commands.lock_files)
    archive = cache.archive(repository, commands.image, digest)
    sandbox = sandbox_for(commands.image)
    failed = _install(sandbox, commands.install, target, limits)
    if failed is not None:
        return failed
    sandbox.save_home(target, archive)
    tested = sandbox.run(commands.test, commit=target, phase=Phase.TEST, limits=limits)
    if not isinstance(commands.ecosystem.read_results(tested), Results):
        return CommandFailed(commands.test, tested)
    profile = RepoProfile(
        image=commands.image,
        install=commands.install,
        test=commands.test,
        test_framework=commands.test_framework,
        test_layout=commands.test_layout,
        lock_files=commands.lock_files,
        lock_digest=digest,
    )
    store.save(repository, profile)
    return Prepared(profile, sandbox, learned=True)


def _prepare_with(
    profile: RepoProfile,
    repository: Path,
    target: str,
    cache: DependencyCache,
    sandbox_for: SandboxFor,
    limits: Limits,
) -> Prepared | None:
    """Install the target commit's dependencies with a stored profile, as `prepare_target`
    describes. None when the profile no longer holds and is to be discarded."""
    digest = digest_lock_files(repository, target, profile.lock_files)
    if profile.lock_digest != digest:
        return None
    archive = cache.archive(repository, profile.image, digest)
    sandbox = sandbox_for(profile.image)
    put_back = archive.exists()
    if put_back:
        try:
            sandbox.restore_home(target, archive)
        except SandboxFailed:
            # So that the next run installs from nothing, and does not meet this archive again.
            archive.unlink(missing_ok=True)
            raise
    if _install(sandbox, profile.install, target, limits) is not None:
        return None
    if not put_back:
        sandbox.save_home(target, archive)
    return Prepared(profile, sandbox, learned=False)


def _install(
    sandbox: SandboxWithHomes, install: tuple[Command, ...], commit: str, limits: Limits
) -> CommandFailed | None:
    """Run install commands against a commit in order, up to the first that does not work.
    None when each exited with status 0."""
    for command in install:
        run = sandbox.run(command, commit=commit, phase=Phase.INSTALL, limits=limits)
        if run.exit_status != 0:
            return CommandFailed(command, run)
    return None


def prepare_pull_request(
    repository: Path,
    commit: str,
    *,
    profile: RepoProfile,
    sandbox: SandboxWithHomes,
    cache: DependencyCache,
    limits: Limits,
) -> CommandFailed | None:
    """Install the dependencies of the pull request's commit with the target's Repo Profile.

    `commit` is the code after the pull request, and `profile` and `sandbox` are the ones
    `prepare_target` gave. The pull request's own files do not decide how it is installed:
    the commands are the profile's, and discovery is not run on `commit`. Because `commit` is
    pull request content, nothing here is kept: no profile is saved or discarded, and
    `save_home` is never called.

    The archive is `cache.archive(repository, profile.image, digest_lock_files(repository,
    commit, profile.lock_files))`. When it exists, it is put back with
    `restore_home(commit, archive)` first, so installed dependencies are used again only in
    the image they were installed in, and only for a commit that gets the digest of the commit
    they were installed for. Then each of `profile.install` is run against `commit` in
    `Phase.INSTALL` with `limits`, in order.

    Returns None when each exited with status 0. Otherwise returns `CommandFailed` with the
    first that did not, which includes being stopped at a limit, and those after it are not
    run.

    Raises `SandboxFailed` when the Sandbox itself cannot do what is asked of it, and
    `ValueError` as `DependencyCache` does.
    """
    digest = digest_lock_files(repository, commit, profile.lock_files)
    archive = cache.archive(repository, profile.image, digest)
    if archive.exists():
        sandbox.restore_home(commit, archive)
    return _install(sandbox, profile.install, commit, limits)
