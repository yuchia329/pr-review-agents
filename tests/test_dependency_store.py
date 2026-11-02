"""Where installed dependencies are kept: the dependency store, asked through its protocol.

`prepare_target` and `prepare_pull_request` are given a store kept in memory, whose answers
are not what the files on disk say, so the tests can tell which of the two was asked.
"""

from pathlib import Path

import pytest
from test_repo_profile import LIMITS, LOCK_FILES, Sandboxes, Setting, kinds, make_setting

from pr_verification.repo_profile import (
    DependencyCache,
    DependencyStore,
    Prepared,
    digest_lock_files,
    prepare_pull_request,
    prepare_target,
)
from pr_verification.sandbox import SandboxFailed

IMAGE = "tap:1"


class KeptInMemory:
    """A `DependencyStore` that keeps in memory which archives it holds. It says where an
    archive belongs under `directory`, and never looks at what is there."""

    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self.held: set[Path] = set()
        self.discarded: list[Path] = []

    def archive(self, repository: Path, image: str, lock_digest: str) -> Path:
        return self._directory / f"{repository.name}-{image}-{lock_digest}.home"

    def holds(self, archive: Path) -> bool:
        return archive in self.held

    def discard(self, archive: Path) -> None:
        self.discarded.append(archive)
        self.held.discard(archive)


def a_store(store: KeptInMemory) -> DependencyStore:
    """`store` as the protocol it stands in for, so a type checker holds it to the protocol."""
    return store


@pytest.fixture
def setting(tmp_path: Path) -> Setting:
    """A repository whose target commit's Repo Profile has been learned."""
    made = make_setting(tmp_path)
    made.learn()
    return made


@pytest.fixture
def memory(tmp_path: Path) -> KeptInMemory:
    directory = tmp_path / "in-memory"
    directory.mkdir()
    return KeptInMemory(directory)


def archive_of(store: KeptInMemory, setting: Setting, commit: str) -> Path:
    digest = digest_lock_files(setting.repository, commit, LOCK_FILES)
    return store.archive(setting.repository, IMAGE, digest)


def prepare(setting: Setting, store: KeptInMemory, sandboxes: Sandboxes) -> object:
    return prepare_target(
        setting.repository,
        setting.target,
        store=setting.store,
        cache=a_store(store),
        sandbox_for=sandboxes,
        limits=LIMITS,
    )


# The cache on disk


def test_cache_holds_no_archive_until_one_is_written_where_it_belongs(tmp_path: Path) -> None:
    cache = DependencyCache(tmp_path / "kept")
    repository = tmp_path / "repository"
    repository.mkdir()
    archive = cache.archive(repository, IMAGE, "digest")

    held_before = cache.holds(archive)
    archive.write_bytes(b"a saved home directory")

    assert not held_before
    assert cache.holds(archive)


def test_cache_forgets_an_archive_it_discards_and_discarding_none_does_nothing(
    tmp_path: Path,
) -> None:
    cache = DependencyCache(tmp_path / "kept")
    repository = tmp_path / "repository"
    repository.mkdir()
    archive = cache.archive(repository, IMAGE, "digest")
    archive.write_bytes(b"a saved home directory")

    cache.discard(archive)
    cache.discard(archive)

    assert not cache.holds(archive)
    assert not archive.exists()


# Preparing a commit asks the store, not the disk


def test_archive_the_store_holds_is_put_back_though_no_file_is_there(
    setting: Setting, memory: KeptInMemory
) -> None:
    archive = archive_of(memory, setting, setting.target)
    memory.held.add(archive)
    sandboxes = Sandboxes()

    prepare(setting, memory, sandboxes)

    assert not archive.exists()
    assert sandboxes.opened[0].events[0] == ("restore_home", setting.target, archive)
    assert "save_home" not in kinds(sandboxes.opened[0].events)


def test_home_directory_is_saved_when_the_store_holds_no_archive(
    setting: Setting, memory: KeptInMemory
) -> None:
    sandboxes = Sandboxes()

    prepare(setting, memory, sandboxes)

    events = sandboxes.opened[0].events
    assert "restore_home" not in kinds(events)
    assert events[-1] == ("save_home", setting.target, archive_of(memory, setting, setting.target))


def test_archive_that_cannot_be_put_back_is_discarded_through_the_store(
    setting: Setting, memory: KeptInMemory
) -> None:
    archive = archive_of(memory, setting, setting.target)
    memory.held.add(archive)

    with pytest.raises(SandboxFailed):
        prepare(setting, memory, Sandboxes(restores=False))

    assert memory.discarded == [archive]
    assert not memory.holds(archive)


def test_pull_request_commit_gets_back_the_archive_the_store_holds(
    setting: Setting, memory: KeptInMemory
) -> None:
    prepared = setting.learn()
    assert isinstance(prepared, Prepared)
    archive = archive_of(memory, setting, setting.pull_request)
    memory.held.add(archive)
    sandbox = Sandboxes()(prepared.profile.image)

    prepare_pull_request(
        setting.repository,
        setting.pull_request,
        profile=prepared.profile,
        sandbox=sandbox,
        cache=a_store(memory),
        limits=LIMITS,
    )

    assert sandbox.events[0] == ("restore_home", setting.pull_request, archive)


def test_pull_request_commit_whose_archive_the_store_does_not_hold_gets_none_back(
    setting: Setting, memory: KeptInMemory
) -> None:
    prepared = setting.learn()
    assert isinstance(prepared, Prepared)
    sandbox = Sandboxes()(prepared.profile.image)

    prepare_pull_request(
        setting.repository,
        setting.pull_request,
        profile=prepared.profile,
        sandbox=sandbox,
        cache=a_store(memory),
        limits=LIMITS,
    )

    assert "restore_home" not in kinds(sandbox.events)
