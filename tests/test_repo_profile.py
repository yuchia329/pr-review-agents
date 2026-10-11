"""The Repo Profile and the dependency cache, tested through `prepare_target`.

All but the last test use a stand-in Sandbox and a stand-in Ecosystem, so that what was run,
saved, and put back can be read off in order. The last runs a real Sandbox and installs from
the package index.
"""

import subprocess
import tempfile
from collections.abc import Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from conftest import commit_snapshots, git
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification import ecosystem, repo_profile
from pr_verification.ecosystem import Commands, Results, Unreadable, Unsupported, find_commands
from pr_verification.python_ecosystem import PythonEcosystem
from pr_verification.repo_profile import (
    CommandFailed,
    DependencyCache,
    DirectoryStore,
    Prepared,
    RepoProfile,
    digest_lock_files,
    prepare_pull_request,
    prepare_target,
)
from pr_verification.sandbox import (
    Command,
    ContainerSandbox,
    LimitHit,
    Limits,
    Phase,
    RunResult,
    SandboxFailed,
    SandboxWithHomes,
)

LIMITS = Limits(seconds=60.0, memory_bytes=512 * 1024 * 1024, processors=1.0)
REAL_LIMITS = Limits(seconds=600.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)

FETCH: Command = ("fetch-dependencies",)
BUILD: Command = ("build", "--quiet")
TEST: Command = ("run-tests",)
OLD_INSTALL: Command = ("old-installer",)
LOCK_FILES = ("deps.lock",)

SUCCEEDED = RunResult(output="", exit_status=0, limit_hit=None)
EXITED_WITH_1 = RunResult(output="cannot install\n", exit_status=1, limit_hit=None)
TIMED_OUT = RunResult(output="fetching", exit_status=None, limit_hit=LimitHit.TIME)
TESTS_PASSED = RunResult(output="ok first\n", exit_status=0, limit_hit=None)
TESTS_FAILED = RunResult(output="ok first\nnot ok second\n", exit_status=1, limit_hit=None)
TOOLING_DID_NOT_START = RunResult(output="run-tests: not found\n", exit_status=127, limit_hit=None)

Event = tuple[object, ...]


class TapEcosystem:
    """A stand-in Ecosystem whose test tooling prints `ok` or `not ok` and a name for each test."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        raise AssertionError("discovery is asked for through `discover`")

    def read_results(self, run: RunResult) -> Results | Unreadable:
        if run.exit_status == 127:
            return Unreadable("The test tooling did not start.")
        lines = run.output.splitlines()
        return Results(
            passed=tuple(line.split()[-1] for line in lines if line.startswith("ok ")),
            failed=tuple(line.split()[-1] for line in lines if line.startswith("not ok ")),
            could_not_run=(),
        )


TAP = TapEcosystem()


def commands(*install: Command, image: str = "tap:1") -> Commands:
    return Commands(
        ecosystem=TAP,
        image=image,
        install=install or (FETCH, BUILD),
        test=TEST,
        test_framework="tap",
        test_layout=("checks", "checks/slow"),
        lock_files=LOCK_FILES,
    )


class Discovery:
    """A stand-in for discovery that gives one answer and records what it was asked."""

    def __init__(self, answer: Commands | Unsupported) -> None:
        self.answer = answer
        self.asked: list[tuple[Path, str]] = []

    def __call__(self, repository: Path, commit: str) -> Commands | Unsupported:
        self.asked.append((repository, commit))
        return self.answer


def no_discovery(repository: Path, commit: str) -> Commands | Unsupported:
    raise AssertionError(f"discovery was run on {commit}")


class RecordingSandbox:
    """A stand-in Sandbox that answers each command as prepared and records what it was asked."""

    def __init__(
        self, image: str, answers: Mapping[Command, RunResult], restores: bool = True
    ) -> None:
        self.image = image
        self._answers = answers
        self._restores = restores
        self.events: list[Event] = []

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        self.events.append(("run", command, commit, phase, limits))
        return self._answers.get(command, SUCCEEDED)

    def save_home(self, commit: str, archive: Path) -> None:
        self.events.append(("save_home", commit, archive))
        archive.write_bytes(b"a saved home directory")

    def restore_home(self, commit: str, archive: Path) -> None:
        self.events.append(("restore_home", commit, archive))
        if not self._restores:
            raise SandboxFailed(f"cannot put back {archive}")


class Sandboxes:
    """A stand-in for opening Sandboxes: each call gives a new one, and all are kept."""

    def __init__(
        self, answers: Mapping[Command, RunResult] | None = None, restores: bool = True
    ) -> None:
        self._answers = {TEST: TESTS_PASSED, **(answers or {})}
        self._restores = restores
        self.opened: list[RecordingSandbox] = []

    def __call__(self, image: str) -> RecordingSandbox:
        self.opened.append(RecordingSandbox(image, self._answers, self._restores))
        return self.opened[-1]


class RecordingStore:
    """A stand-in store that keeps profiles in memory and records what it was asked to do."""

    def __init__(self) -> None:
        self.profiles: dict[Path, RepoProfile] = {}
        self.saved: list[RepoProfile] = []
        self.discarded: list[Path] = []

    def load(self, repository: Path) -> RepoProfile | None:
        return self.profiles.get(repository.resolve())

    def save(self, repository: Path, profile: RepoProfile) -> None:
        self.saved.append(profile)
        self.profiles[repository.resolve()] = profile

    def discard(self, repository: Path) -> None:
        self.discarded.append(repository.resolve())
        self.profiles.pop(repository.resolve(), None)


@dataclass
class Setting:
    """A repository on its target branch, a pull request to it, and where things are kept."""

    directory: Path
    target: str
    later_target: str
    """A later commit of the target branch with the same lock file."""
    relocked_target: str
    """A later commit of the target branch whose lock file has changed."""
    pull_request: str
    """The pull request's commit, which leaves the lock file alone."""
    relocking_pull_request: str
    """A pull request's commit that changes the lock file."""
    store: RecordingStore = field(default_factory=RecordingStore)

    @property
    def repository(self) -> Path:
        return self.directory / "repository"

    @property
    def cache(self) -> DependencyCache:
        return DependencyCache(self.directory / "kept" / "dependencies")

    def archive(self, commit: str, image: str = "tap:1") -> Path:
        digest = digest_lock_files(self.repository, commit, LOCK_FILES)
        return self.cache.archive(self.repository, image, digest)

    def prepare(
        self, sandboxes: Sandboxes, discover: Discovery | None = None, target: str | None = None
    ) -> Prepared | Unsupported | CommandFailed:
        return prepare_target(
            self.repository,
            target or self.target,
            store=self.store,
            cache=self.cache,
            sandbox_for=sandboxes,
            limits=LIMITS,
            discover=discover or no_discovery,
        )

    def learn(self) -> Prepared:
        """A first run, in which everything works."""
        prepared = self.prepare(Sandboxes(), Discovery(commands()))
        assert isinstance(prepared, Prepared)
        return prepared

    def prepare_pull_request(
        self, prepared: Prepared, commit: str, answers: Mapping[Command, RunResult] | None = None
    ) -> tuple[CommandFailed | None, list[Event]]:
        """Prepare a pull request's commit in a new Sandbox, and say what that Sandbox was asked."""
        sandbox = RecordingSandbox(prepared.profile.image, answers or {})
        answer = prepare_pull_request(
            self.repository,
            commit,
            profile=prepared.profile,
            sandbox=sandbox,
            cache=self.cache,
            limits=LIMITS,
        )
        return answer, sandbox.events


def make_setting(
    directory: Path, lock: str = "left-pad 1.0\n", other: str = "left-pad 2.0\n"
) -> Setting:
    target, later, relocked, pull_request, relocking = commit_snapshots(
        directory / "repository",
        {"deps.lock": lock, "app.txt": "one\n"},
        {"deps.lock": lock, "app.txt": "two\n"},
        {"deps.lock": other, "app.txt": "two\n"},
        {"deps.lock": lock, "app.txt": "from the pull request\n"},
        {"deps.lock": other, "app.txt": "from the pull request\n"},
    )
    return Setting(directory, target, later, relocked, pull_request, relocking)


@pytest.fixture
def setting(tmp_path: Path) -> Setting:
    return make_setting(tmp_path)


def a_repository(path: Path) -> Path:
    """The directory of a repository that only needs to be there, made at `path`."""
    path.mkdir()
    return path


def run(command: Command, commit: str, phase: Phase) -> Event:
    return ("run", command, commit, phase, LIMITS)


def kinds(events: list[Event]) -> list[object]:
    return [event[0] for event in events]


# Learning the profile


def test_first_run_learns_the_profile_from_the_target_commit_and_saves_it(setting: Setting) -> None:
    discovery, sandboxes = Discovery(commands()), Sandboxes()

    prepared = setting.prepare(sandboxes, discovery)

    assert isinstance(prepared, Prepared)
    assert prepared.learned is True
    assert prepared.profile == RepoProfile(
        image="tap:1",
        install=(FETCH, BUILD),
        test=TEST,
        test_framework="tap",
        test_layout=("checks", "checks/slow"),
        lock_files=LOCK_FILES,
        lock_digest=digest_lock_files(setting.repository, setting.target, LOCK_FILES),
    )
    assert setting.store.load(setting.repository) == prepared.profile
    assert discovery.asked == [(setting.repository, setting.target)]
    assert [sandbox.image for sandbox in sandboxes.opened] == ["tap:1"]
    assert prepared.sandbox is sandboxes.opened[0]


def test_learning_installs_then_saves_the_home_directory_then_tests_all_on_the_target(
    setting: Setting,
) -> None:
    sandboxes = Sandboxes()

    setting.prepare(sandboxes, Discovery(commands()))

    assert sandboxes.opened[0].events == [
        run(FETCH, setting.target, Phase.INSTALL),
        run(BUILD, setting.target, Phase.INSTALL),
        ("save_home", setting.target, setting.archive(setting.target)),
        run(TEST, setting.target, Phase.TEST),
    ]


@pytest.mark.parametrize("tested", [TESTS_PASSED, TESTS_FAILED], ids=["pass", "fail"])
def test_facts_are_proven_whether_the_target_commits_tests_pass_or_fail(
    setting: Setting, tested: RunResult
) -> None:
    prepared = setting.prepare(Sandboxes({TEST: tested}), Discovery(commands()))

    assert isinstance(prepared, Prepared)
    assert setting.store.saved == [prepared.profile]


@pytest.mark.parametrize("failure", [EXITED_WITH_1, TIMED_OUT], ids=["exit status", "limit"])
def test_no_fact_is_saved_when_an_install_command_does_not_succeed(
    setting: Setting, failure: RunResult
) -> None:
    sandboxes = Sandboxes({FETCH: failure})

    answer = setting.prepare(sandboxes, Discovery(commands()))

    assert answer == CommandFailed(command=FETCH, run=failure)
    assert setting.store.saved == []
    assert sandboxes.opened[0].events == [run(FETCH, setting.target, Phase.INSTALL)]
    assert not setting.archive(setting.target).exists()


def test_no_fact_is_saved_when_the_test_run_cannot_be_read(setting: Setting) -> None:
    answer = setting.prepare(Sandboxes({TEST: TOOLING_DID_NOT_START}), Discovery(commands()))

    assert answer == CommandFailed(command=TEST, run=TOOLING_DID_NOT_START)
    assert setting.store.saved == []
    assert setting.store.load(setting.repository) is None


def test_unsupported_repository_is_answered_as_it_is_and_no_sandbox_is_opened(
    setting: Setting,
) -> None:
    unsupported, sandboxes = Unsupported(reason="No project was found."), Sandboxes()

    answer = setting.prepare(sandboxes, Discovery(unsupported))

    assert answer is unsupported
    assert sandboxes.opened == []
    assert setting.store.saved == []


# A repeat run


def test_second_run_on_the_same_repository_skips_discovery(setting: Setting) -> None:
    first = setting.learn()
    discovery, sandboxes = Discovery(commands()), Sandboxes()

    second = setting.prepare(sandboxes, discovery)

    assert discovery.asked == []
    assert isinstance(second, Prepared)
    assert second.learned is False
    assert second.profile == first.profile
    assert second.sandbox is sandboxes.opened[0]
    assert sandboxes.opened[0].image == first.profile.image
    assert setting.store.saved == [first.profile]


def test_second_run_puts_installed_dependencies_back_and_still_runs_the_install_commands(
    setting: Setting,
) -> None:
    setting.learn()
    sandboxes = Sandboxes()

    setting.prepare(sandboxes)

    assert sandboxes.opened[0].events == [
        ("restore_home", setting.target, setting.archive(setting.target)),
        run(FETCH, setting.target, Phase.INSTALL),
        run(BUILD, setting.target, Phase.INSTALL),
    ]


def test_stored_profile_still_holds_for_a_later_target_commit_with_the_same_lock_file(
    setting: Setting,
) -> None:
    first = setting.learn()
    sandboxes = Sandboxes()

    later = setting.prepare(sandboxes, target=setting.later_target)

    assert isinstance(later, Prepared)
    assert (later.learned, later.profile) == (False, first.profile)
    assert sandboxes.opened[0].events[0] == (
        "restore_home",
        setting.later_target,
        setting.archive(setting.target),
    )


def test_stored_profile_saves_the_home_directory_when_the_cache_holds_no_archive(
    setting: Setting,
) -> None:
    setting.learn()
    setting.archive(setting.target).unlink()
    sandboxes = Sandboxes()

    setting.prepare(sandboxes)

    assert sandboxes.opened[0].events == [
        run(FETCH, setting.target, Phase.INSTALL),
        run(BUILD, setting.target, Phase.INSTALL),
        ("save_home", setting.target, setting.archive(setting.target)),
    ]


def test_archive_that_cannot_be_put_back_is_deleted_so_the_next_run_does_not_meet_it(
    setting: Setting,
) -> None:
    setting.learn()

    with pytest.raises(SandboxFailed):
        setting.prepare(Sandboxes(restores=False))

    assert not setting.archive(setting.target).exists()


# Discarding a stale profile


def test_changed_lock_file_discards_the_profile_and_it_is_learned_again(setting: Setting) -> None:
    setting.learn()
    discovery, sandboxes = Discovery(commands(("new-installer",), image="tap:2")), Sandboxes()

    prepared = setting.prepare(sandboxes, discovery, target=setting.relocked_target)

    assert setting.store.discarded == [setting.repository.resolve()]
    assert discovery.asked == [(setting.repository, setting.relocked_target)]
    assert isinstance(prepared, Prepared)
    assert prepared.learned is True
    assert prepared.profile.install == (("new-installer",),)
    assert prepared.profile.lock_digest == digest_lock_files(
        setting.repository, setting.relocked_target, LOCK_FILES
    )
    assert setting.store.load(setting.repository) == prepared.profile
    # No Sandbox was opened for the stored profile, and nothing installed for the old lock file
    # was used.
    assert [sandbox.image for sandbox in sandboxes.opened] == ["tap:2"]
    assert sandboxes.opened[0].events == [
        run(("new-installer",), setting.relocked_target, Phase.INSTALL),
        ("save_home", setting.relocked_target, setting.archive(setting.relocked_target, "tap:2")),
        run(TEST, setting.relocked_target, Phase.TEST),
    ]


@pytest.mark.parametrize("failure", [EXITED_WITH_1, TIMED_OUT], ids=["exit status", "limit"])
def test_stored_install_command_that_stops_working_discards_the_profile_and_it_is_learned_again(
    setting: Setting, failure: RunResult
) -> None:
    setting.prepare(Sandboxes(), Discovery(commands(OLD_INSTALL, BUILD)))
    discovery, sandboxes = Discovery(commands()), Sandboxes({OLD_INSTALL: failure})

    prepared = setting.prepare(sandboxes, discovery)

    assert setting.store.discarded == [setting.repository.resolve()]
    assert discovery.asked == [(setting.repository, setting.target)]
    assert isinstance(prepared, Prepared)
    assert prepared.learned is True
    assert prepared.profile.install == (FETCH, BUILD)
    assert setting.store.load(setting.repository) == prepared.profile
    with_stored_profile, learning = sandboxes.opened
    assert prepared.sandbox is learning
    # The commands after the one that failed were not run.
    assert with_stored_profile.events == [
        ("restore_home", setting.target, setting.archive(setting.target)),
        run(OLD_INSTALL, setting.target, Phase.INSTALL),
    ]
    # Learning starts from nothing, and replaces the archive.
    assert learning.events == [
        run(FETCH, setting.target, Phase.INSTALL),
        run(BUILD, setting.target, Phase.INSTALL),
        ("save_home", setting.target, setting.archive(setting.target)),
        run(TEST, setting.target, Phase.TEST),
    ]


def test_profile_discarded_because_its_test_command_stopped_working_is_learned_again(
    setting: Setting,
) -> None:
    setting.learn()
    discovery = Discovery(commands())

    # What a caller does when the stored test command no longer works against the target commit.
    setting.store.discard(setting.repository)
    prepared = setting.prepare(Sandboxes(), discovery)

    assert discovery.asked == [(setting.repository, setting.target)]
    assert isinstance(prepared, Prepared)
    assert prepared.learned is True


# The pull request's commit


def test_pull_request_commit_is_installed_with_the_commands_of_the_targets_profile(
    setting: Setting, monkeypatch: pytest.MonkeyPatch
) -> None:
    prepared = setting.learn()
    # Discovery would read the pull request's own files, so it is not to be run from anywhere.
    monkeypatch.setattr(ecosystem, "find_commands", no_discovery)
    monkeypatch.setattr(repo_profile, "find_commands", no_discovery)

    answer, events = setting.prepare_pull_request(prepared, setting.relocking_pull_request)

    assert answer is None
    assert events == [
        run(FETCH, setting.relocking_pull_request, Phase.INSTALL),
        run(BUILD, setting.relocking_pull_request, Phase.INSTALL),
    ]


def test_pull_request_that_changes_files_cannot_alter_the_profile_used_to_verify_it(
    setting: Setting,
) -> None:
    prepared = setting.learn()
    # The pull request is checked out, with a file of its own where it hopes a profile is read.
    git(setting.repository, "checkout", "--quiet", setting.relocking_pull_request)
    (setting.repository / "repo-profile.json").write_text('{"install": [["curl", "evil"]]}')

    setting.prepare_pull_request(prepared, setting.relocking_pull_request)
    again = setting.prepare(Sandboxes())

    assert setting.store.saved == [prepared.profile]
    assert setting.store.discarded == []
    assert isinstance(again, Prepared)
    assert (again.learned, again.profile) == (False, prepared.profile)


@pytest.mark.parametrize("commit", ["pull_request", "relocking_pull_request"])
def test_nothing_a_pull_request_installs_is_ever_saved(setting: Setting, commit: str) -> None:
    prepared = setting.learn()
    saved_from_target = setting.archive(setting.target).read_bytes()

    _, events = setting.prepare_pull_request(prepared, getattr(setting, commit))

    assert "save_home" not in kinds(events)
    assert setting.archive(setting.target).read_bytes() == saved_from_target
    assert not setting.archive(setting.relocking_pull_request).exists()


def test_pull_request_that_leaves_the_lock_file_alone_gets_the_targets_dependencies_put_back(
    setting: Setting,
) -> None:
    prepared = setting.learn()

    _, events = setting.prepare_pull_request(prepared, setting.pull_request)

    assert events == [
        ("restore_home", setting.pull_request, setting.archive(setting.target)),
        run(FETCH, setting.pull_request, Phase.INSTALL),
        run(BUILD, setting.pull_request, Phase.INSTALL),
    ]


@pytest.mark.parametrize("failure", [EXITED_WITH_1, TIMED_OUT], ids=["exit status", "limit"])
def test_pull_request_whose_install_fails_is_answered_and_the_profile_is_kept(
    setting: Setting, failure: RunResult
) -> None:
    prepared = setting.learn()

    answer, events = setting.prepare_pull_request(
        prepared, setting.relocking_pull_request, {FETCH: failure}
    )

    assert answer == CommandFailed(command=FETCH, run=failure)
    assert events == [run(FETCH, setting.relocking_pull_request, Phase.INSTALL)]
    assert setting.store.discarded == []
    assert setting.store.load(setting.repository) == prepared.profile


lock_file_contents = st.text(st.characters(exclude_categories=["Cs"], exclude_characters="\r"))


@settings(max_examples=25, deadline=None)
@given(locks=st.lists(lock_file_contents, min_size=2, max_size=2, unique=True))
def test_dependencies_cached_for_one_lock_file_are_never_used_for_another(locks: list[str]) -> None:
    with tempfile.TemporaryDirectory() as directory:
        setting = make_setting(Path(directory), lock=locks[0], other=locks[1])
        prepared = setting.learn()

        _, events = setting.prepare_pull_request(prepared, setting.relocking_pull_request)
        _, events_for_same_lock = setting.prepare_pull_request(prepared, setting.pull_request)

    assert "restore_home" not in kinds(events)
    assert "restore_home" in kinds(events_for_same_lock)


# The digest of the lock file

optional_lock_files = st.fixed_dictionaries(
    {},
    optional={
        "deps.lock": st.sampled_from(["", "left-pad 1.0\n", "left-pad 2.0\n"]),
        "tools/pins.txt": st.sampled_from(["", "left-pad 1.0\n"]),
    },
)


@settings(max_examples=40, deadline=None)
@given(first=optional_lock_files, second=optional_lock_files)
def test_two_commits_get_the_same_digest_exactly_when_they_agree_on_every_lock_file(
    first: dict[str, str], second: dict[str, str]
) -> None:
    paths = ("deps.lock", "tools/pins.txt")
    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory) / "repository"
        one, other = commit_snapshots(
            repository, {**first, "app.txt": "one\n"}, {**second, "app.txt": "two\n"}
        )

        same = digest_lock_files(repository, one, paths) == digest_lock_files(
            repository, other, paths
        )

    assert same == (first == second)


def test_digest_is_the_same_in_another_repository_with_the_same_lock_file(tmp_path: Path) -> None:
    (here,) = commit_snapshots(tmp_path / "here", {"deps.lock": "left-pad 1.0\n", "a.txt": "a\n"})
    (there,) = commit_snapshots(tmp_path / "there", {"deps.lock": "left-pad 1.0\n", "b.txt": "b\n"})

    assert digest_lock_files(tmp_path / "here", here, LOCK_FILES) == digest_lock_files(
        tmp_path / "there", there, LOCK_FILES
    )


def test_digest_reads_the_commit_whatever_the_working_tree_holds(setting: Setting) -> None:
    before = digest_lock_files(setting.repository, setting.target, LOCK_FILES)

    (setting.repository / "deps.lock").write_text("changed and never committed\n")

    assert digest_lock_files(setting.repository, setting.target, LOCK_FILES) == before


def test_with_no_lock_files_every_commit_gets_the_same_digest(setting: Setting) -> None:
    assert digest_lock_files(setting.repository, setting.target, ()) == digest_lock_files(
        setting.repository, setting.relocked_target, ()
    )


def test_digest_of_a_commit_that_is_not_in_the_repository_raises_as_git_does(
    setting: Setting,
) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        digest_lock_files(setting.repository, "0" * 40, LOCK_FILES)


# The store

PROFILE = RepoProfile(
    image="python:3.14-slim",
    install=(("python", "-m", "pip", "install", "--user", "pytest"), ("make", "deps")),
    test=("python", "-m", "pytest"),
    test_framework="pytest",
    test_layout=(".", "tests"),
    lock_files=("pyproject.toml", "requirements.txt"),
    lock_digest="0a1b2c",
)
OTHER_PROFILE = RepoProfile("tap:1", (), TEST, "", (), (), "")

a_command = st.lists(st.text(), min_size=1).map(tuple)
profiles = st.builds(
    RepoProfile,
    image=st.text(),
    install=st.lists(a_command).map(tuple),
    test=a_command,
    test_framework=st.text(),
    test_layout=st.lists(st.text()).map(tuple),
    lock_files=st.lists(st.text()).map(tuple),
    lock_digest=st.text(),
)


@settings(deadline=None)
@given(profile=profiles)
def test_profile_saved_is_loaded_back_equal_by_another_store_on_the_same_directory(
    profile: RepoProfile,
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository, kept = a_repository(Path(directory) / "repo"), Path(directory) / "kept"

        DirectoryStore(kept).save(repository, profile)

        assert DirectoryStore(kept).load(repository) == profile


def test_store_has_no_profile_for_a_repository_it_was_never_given(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "not" / "there" / "yet")

    assert store.load(a_repository(tmp_path / "repository")) is None


def test_store_keeps_one_profile_for_each_repository_the_last_one_saved(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "kept")
    one, other = a_repository(tmp_path / "one"), a_repository(tmp_path / "other")

    store.save(one, OTHER_PROFILE)
    store.save(one, PROFILE)
    store.save(other, OTHER_PROFILE)

    assert store.load(one) == PROFILE
    assert store.load(other) == OTHER_PROFILE


def test_discarded_profile_is_gone_and_discarding_again_does_nothing(tmp_path: Path) -> None:
    store = DirectoryStore(tmp_path / "kept")
    one, other = a_repository(tmp_path / "one"), a_repository(tmp_path / "other")
    store.save(one, PROFILE)
    store.save(other, OTHER_PROFILE)

    store.discard(one)
    store.discard(one)

    assert store.load(one) is None
    assert store.load(other) == OTHER_PROFILE


def test_same_directory_reached_by_another_route_is_the_same_repository(tmp_path: Path) -> None:
    repository = a_repository(tmp_path / "repository")
    (tmp_path / "link").symlink_to(repository, target_is_directory=True)
    store = DirectoryStore(tmp_path / "kept")

    store.save(repository, PROFILE)

    assert store.load(tmp_path / "link") == PROFILE


def test_what_cannot_be_read_back_as_a_profile_is_no_profile(tmp_path: Path) -> None:
    store, repository = DirectoryStore(tmp_path / "kept"), a_repository(tmp_path / "repository")
    store.save(repository, PROFILE)

    for kept in (tmp_path / "kept").rglob("*"):
        if kept.is_file():
            kept.write_bytes(b"\x00 not a profile")

    assert store.load(repository) is None


@pytest.fixture(params=["the repository", "inside it", "inside it by another route"])
def modifiable(request: pytest.FixtureRequest, tmp_path: Path) -> Path:
    """A directory a pull request to `tmp_path / "repository"` could put a file in."""
    (tmp_path / "repository" / "kept").mkdir(parents=True)
    (tmp_path / "link").symlink_to(tmp_path / "repository" / "kept", target_is_directory=True)
    places = {
        "the repository": tmp_path / "repository",
        "inside it": tmp_path / "repository" / "kept" / "profiles",
        "inside it by another route": tmp_path / "link",
    }
    return places[str(request.param)]


def test_store_refuses_a_directory_that_a_pull_request_could_modify(
    tmp_path: Path, modifiable: Path
) -> None:
    store, repository = DirectoryStore(modifiable), tmp_path / "repository"

    with pytest.raises(ValueError):
        store.load(repository)
    with pytest.raises(ValueError):
        store.save(repository, PROFILE)
    with pytest.raises(ValueError):
        store.discard(repository)


# The dependency cache


def test_archive_belongs_under_the_caches_directory_and_is_not_there_until_saved(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path / "repository")

    archive = DependencyCache(tmp_path / "kept").archive(repository, "tap:1", "0a1b2c")

    assert (tmp_path / "kept").resolve() in archive.resolve().parents
    assert archive.parent.is_dir()
    assert not archive.exists()


def test_same_repository_image_and_digest_give_the_same_archive_from_any_cache_on_the_directory(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path / "repository")
    (tmp_path / "link").symlink_to(repository, target_is_directory=True)

    archive = DependencyCache(tmp_path / "kept").archive(repository, "tap:1", "0a1b2c")

    again = DependencyCache(tmp_path / "kept").archive(tmp_path / "link", "tap:1", "0a1b2c")
    assert again == archive


@given(keys=st.lists(st.tuples(st.text(), st.text()), min_size=2, max_size=2, unique=True))
def test_archive_for_one_image_and_digest_is_never_the_file_of_another(
    keys: list[tuple[str, str]],
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository = a_repository(Path(directory) / "repository")
        cache = DependencyCache(Path(directory) / "kept")

        cache.archive(repository, *keys[0]).write_bytes(b"installed for the first")

        assert not cache.archive(repository, *keys[1]).exists()


def test_two_repositories_with_the_same_lock_file_never_share_an_archive(tmp_path: Path) -> None:
    cache = DependencyCache(tmp_path / "kept")
    one, other = a_repository(tmp_path / "one"), a_repository(tmp_path / "other")

    cache.archive(one, "tap:1", "0a1b2c").write_bytes(b"installed for one")

    assert not cache.archive(other, "tap:1", "0a1b2c").exists()


def test_cache_refuses_a_directory_that_a_pull_request_could_modify(
    tmp_path: Path, modifiable: Path
) -> None:
    with pytest.raises(ValueError):
        DependencyCache(modifiable).archive(tmp_path / "repository", "tap:1", "0a1b2c")


# A real Sandbox and the Python Ecosystem


def test_repeat_run_in_a_real_sandbox_skips_discovery_and_uses_what_the_first_run_kept(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    target, pull_request = commit_snapshots(
        repository,
        {"tests/test_rate.py": "def test_rate():\n    assert 3 == 3\n"},
        {"tests/test_rate.py": "def test_rate():\n    assert 3 == 4\n"},
    )
    store, cache = DirectoryStore(tmp_path / "profiles"), DependencyCache(tmp_path / "dependencies")

    with ExitStack() as opened:

        def sandbox_for(image: str) -> SandboxWithHomes:
            return opened.enter_context(ContainerSandbox(repository, image))

        def prepare(discover: Discovery | None) -> Prepared | Unsupported | CommandFailed:
            return prepare_target(
                repository,
                target,
                store=store,
                cache=cache,
                sandbox_for=sandbox_for,
                limits=REAL_LIMITS,
                discover=discover or find_commands,
            )

        first = prepare(None)
        assert isinstance(first, Prepared), first
        discovery = Discovery(Unsupported(reason="Discovery was run again."))
        second = prepare(discovery)
        assert isinstance(second, Prepared), second
        failed = prepare_pull_request(
            repository,
            pull_request,
            profile=second.profile,
            sandbox=second.sandbox,
            cache=cache,
            limits=REAL_LIMITS,
        )
        tested = [
            second.sandbox.run(
                second.profile.test, commit=commit, phase=Phase.TEST, limits=REAL_LIMITS
            )
            for commit in (target, pull_request)
        ]

    assert (first.learned, second.learned) == (True, False)
    assert discovery.asked == []
    assert second.profile == first.profile == store.load(repository)
    assert (first.profile.test_framework, first.profile.test_layout) == ("pytest", ("tests",))
    digest = digest_lock_files(repository, target, first.profile.lock_files)
    assert cache.archive(repository, first.profile.image, digest).is_file()
    assert failed is None
    name = "tests/test_rate.py::test_rate"
    assert [PythonEcosystem().read_results(result) for result in tested] == [
        Results(passed=(name,), failed=(), could_not_run=()),
        Results(passed=(), failed=(name,), could_not_run=()),
    ]
