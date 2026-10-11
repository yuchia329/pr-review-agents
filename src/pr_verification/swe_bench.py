"""Evaluation cases from SWE-bench Verified: real fixes as true Claims, failed patches as false.

Nothing here downloads anything. Instances and failed patches are handed in, along with a
local clone of each repository. The issue text and every patch are Untrusted Content.
"""

import json
import os
import random
import shlex
import shutil
import subprocess
import tempfile
from collections import Counter
from collections.abc import Hashable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Self

from pr_verification.core import PullRequest
from pr_verification.ecosystem import Unreadable, Unsupported, find_commands
from pr_verification.evaluation import Case, Part
from pr_verification.report import Verdict
from pr_verification.repository import commit_staged, git, repository_at, resolve_commit
from pr_verification.sandbox import ContainerSandbox, Limits, Phase

SWE_BENCH_TITLE = "Fix the issue described below"
"""The title of every pull request made from an instance. The issue text only reports a
problem; with this title the pull request claims to fix it."""


@dataclass(frozen=True)
class Instance:
    """One SWE-bench Verified instance. Each field holds the dataset's field of that name."""

    instance_id: str
    """The dataset's name for the instance, such as `astropy__astropy-12907`."""

    repo: str
    """The repository the issue was raised in, as `owner/name`."""

    base_commit: str
    """The commit the issue was fixed from."""

    problem_statement: str
    """The issue text."""

    patch: str
    """The real fix, as a diff against `base_commit`. It holds none of the dataset's tests."""

    test_patch: str
    """The dataset's own tests for the fix, as a diff against `base_commit`."""

    fail_to_pass: tuple[str, ...]
    """The tests that fail before the real fix and pass after it, named as the dataset names
    them. Where those are not the names the Ecosystem gives the tests, the answer key cannot
    pass, and the instance is left out and listed."""


@dataclass(frozen=True)
class FailedPatch:
    """A patch from a public SWE-bench leaderboard submission that did not resolve its issue."""

    instance_id: str
    """The instance the patch was written for."""

    submission: str
    """The name of the leaderboard submission the patch comes from."""

    patch: str
    """The submission's patch, as a diff against the instance's `base_commit`."""

    source: str
    """Where the patch was obtained, so that it can be found again."""


@dataclass(frozen=True)
class AnswerKey:
    """The dataset's own tests for one instance. They are never part of a pull request."""

    test_patch: str
    """`Instance.test_patch`."""

    fail_to_pass: tuple[str, ...]
    """`Instance.fail_to_pass`: the tests that tell whether the issue is fixed."""


@dataclass(frozen=True)
class ImportedCase:
    """A case made from an instance, with its answer key held beside it and not inside it.

    Only `case` is given to the harness, so nothing of the answer key reaches the system
    during a run: not in the pull request text, and not anywhere in the case's repository.
    """

    case: Case
    """The case to evaluate."""

    answer_key: AnswerKey
    """The tests that say whether the pull request really fixes the issue."""


class ImportFailed(Exception):
    """An instance or a failed patch could not be made into a case. Its message says why."""


def import_instance(instance: Instance, clone: Path, directory: Path, part: Part) -> ImportedCase:
    """Make the true case for `instance`: the real fix is the pull request.

    `clone` is a local clone of `instance.repo` that holds `base_commit`. It is only read.
    The case's repository is a new one, made in a directory of its own under `directory`,
    which must exist. The case has:

    - `name`: `instance.instance_id`;
    - `part`: `part`;
    - `expected`: Confirmed;
    - `pull_request.repository`: the new repository;
    - `pull_request.before`: `base_commit`, as its full commit ID;
    - `pull_request.after`: a new commit whose only parent is `before` and whose files are
      those of `before` with `instance.patch` applied, and nothing of `test_patch`;
    - `pull_request.title`: `SWE_BENCH_TITLE`;
    - `pull_request.description`: `instance.problem_statement`, unchanged;
    - `pull_request.linked_issue`: empty.

    Every commit in the new repository is `before`, an ancestor of `before`, or `after`. So
    what came after `base_commit` in `clone`, where the project's own history holds the fix
    and its tests, is out of the system's reach.

    The answer key holds the instance's `test_patch` and `fail_to_pass`.

    Raises `ImportFailed` when `base_commit` is not in `clone`, when `patch` does not apply
    to it or changes nothing, when `test_patch` does not apply to it, or when `fail_to_pass`
    is empty.
    """
    if not instance.fail_to_pass:
        raise ImportFailed(f"{instance.instance_id} names no test that tells whether it is fixed")
    try:
        before = resolve_commit(clone, instance.base_commit)
    except subprocess.CalledProcessError as error:
        raise ImportFailed(
            f"the base commit {instance.base_commit!r} is not in the clone of {instance.repo}"
        ) from error
    repository = repository_at(clone, before, directory)
    with _removed_unless_made(repository):
        _must_apply(repository, instance.test_patch, "the dataset's test patch", "--check")
        after = _commit_patch(repository, before, instance.patch, "the real fix")
    pull_request = PullRequest(
        repository, before, after, SWE_BENCH_TITLE, instance.problem_statement
    )
    return ImportedCase(
        Case(instance.instance_id, part, pull_request, Verdict.CONFIRMED),
        AnswerKey(instance.test_patch, instance.fail_to_pass),
    )


def import_failed_patch(
    true_case: ImportedCase, failed_patch: FailedPatch, directory: Path
) -> ImportedCase:
    """Make a false case from `failed_patch`, for the issue `true_case` is the true case of.

    A false case can only be made from a true one, so each is for an issue that also has a
    true case. The false case has the true case's part, `before`, title, description, linked
    issue, and answer key. It differs in:

    - `name`: `<instance_id>@<submission>`;
    - `expected`: Refuted;
    - `pull_request.repository`: a new repository of its own under `directory`, made as
      `import_instance` makes one, so it does not hold the real fix;
    - `pull_request.after`: a new commit in that repository whose only parent is `before`
      and whose files are those of `before` with `failed_patch.patch` applied.

    The true case's repository is only read.

    Raises `ImportFailed` when `failed_patch.instance_id` is not the true case's name, or
    when the patch does not apply to `before` or changes nothing.
    """
    case, shown = true_case.case, true_case.case.pull_request
    if failed_patch.instance_id != case.name:
        raise ImportFailed(
            f"the patch is for {failed_patch.instance_id}, and the true case is {case.name}"
        )
    repository = repository_at(shown.repository, shown.before, directory)
    with _removed_unless_made(repository):
        after = _commit_patch(repository, shown.before, failed_patch.patch, "the failed patch")
    false_case = replace(
        case,
        name=_false_case_name(failed_patch),
        expected=Verdict.REFUTED,
        pull_request=replace(shown, repository=repository, after=after),
    )
    return ImportedCase(false_case, true_case.answer_key)


def _false_case_name(failed_patch: FailedPatch) -> str:
    return f"{failed_patch.instance_id}@{failed_patch.submission}"


@contextmanager
def _removed_unless_made(repository: Path) -> Iterator[None]:
    """Remove a case's repository when making its pull request fails: it is then no case's."""
    try:
        yield
    except BaseException:
        shutil.rmtree(repository)
        raise


def _apply(repository: Path, patch: str, *options: str) -> None:
    """Apply a diff to what is staged in `repository`. Raises as `git` does when it does not apply.

    The diff is Untrusted Content. Only what is staged changes, and git refuses a diff that
    names a path outside the repository's files or inside `.git`.
    """
    git(repository, "apply", "--cached", "--whitespace=nowarn", *options, stdin=patch.encode())


def _must_apply(repository: Path, patch: str, what: str, *options: str) -> None:
    try:
        _apply(repository, patch, *options)
    except subprocess.CalledProcessError as error:
        refusal = error.stderr.decode(errors="replace").strip()
        raise ImportFailed(f"{what} does not apply to the base commit: {refusal}") from error


def _commit_patch(repository: Path, before: str, patch: str, what: str) -> str:
    """Commit the files of `before`, which are staged, with `patch` applied, as its child."""
    _must_apply(repository, patch, what)
    if not _staged_changes(repository, before):
        raise ImportFailed(f"{what} changes nothing")
    # The same message for a real fix and a failed patch: it must not tell the two apart.
    return commit_staged(repository, SWE_BENCH_TITLE, before)


def _staged_changes(repository: Path, commit: str) -> list[bytes]:
    """The files that are staged in `repository` otherwise than `commit` has them."""
    differing = ("diff-index", "--cached", "--name-only", "--no-renames", "-z")
    return git(repository, *differing, commit).split(b"\0")[:-1]


class AnswerKeyResult(StrEnum):
    """What running an answer key against a pull request showed."""

    PASSES = "passes"
    """Every test named in `fail_to_pass` passed: the pull request fixes the issue."""

    FAILS = "fails"
    """The tests ran and gave results, and at least one test named in `fail_to_pass` did
    not pass: the pull request does not fix the issue."""

    CANNOT_INSTALL = "cannot_install"
    """The repository could not be installed in the Sandbox, so no test was run."""

    UNREADABLE = "unreadable"
    """The repository was installed, and the run of its tests held no results to read."""


@dataclass(frozen=True)
class AnswerKeyRun:
    """One run of an answer key."""

    result: AnswerKeyResult
    """What the run showed."""

    reason: str
    """One sentence saying why. For `CANNOT_INSTALL` after `Unsupported`, it is that
    answer's reason. Otherwise its wording is not part of the contract. It may quote what
    the code printed, so it is Untrusted Content."""


def run_answer_key(imported: ImportedCase, limits: Limits) -> AnswerKeyRun:
    """Run the answer key's tests against the code after the pull request, in the Sandbox.

    The code tested is the case's `after` commit with the answer key's tests added: each file
    `test_patch` touches is as that patch leaves it from `before`, whatever the pull request
    did to it. `find_commands` says how to install and test that code, and each command is
    run within `limits` in a `ContainerSandbox`: install commands in `Phase.INSTALL`, then the
    test command in `Phase.TEST`. The Ecosystem that gave the commands reads the results.

    - `CANNOT_INSTALL`: `find_commands` answered `Unsupported`, or an install command did not
      exit with status 0, or was stopped at a limit.
    - `UNREADABLE`: the results are `Unreadable`.
    - `PASSES`: every test named in `fail_to_pass` is in `Results.passed`.
    - `FAILS`: anything else.

    The answer key is never written into the case's repository: the work is done in a copy
    that is removed afterwards, and the repository holds no commit and no ref it did not hold
    before. Raises `SandboxFailed` when the Sandbox itself cannot do a run.
    """
    shown, answer_key = imported.case.pull_request, imported.answer_key
    before = resolve_commit(shown.repository, shown.before)
    after = resolve_commit(shown.repository, shown.after)
    with tempfile.TemporaryDirectory() as scratch:
        copy = repository_at(shown.repository, after, Path(scratch))
        tested = _with_answer_key(copy, before, after, answer_key.test_patch)
        commands = find_commands(copy, tested)
        if isinstance(commands, Unsupported):
            return AnswerKeyRun(AnswerKeyResult.CANNOT_INSTALL, commands.reason)
        with ContainerSandbox(copy, commands.image) as sandbox:
            for install in commands.install:
                installed = sandbox.run(install, commit=tested, phase=Phase.INSTALL, limits=limits)
                if installed.exit_status != 0:
                    ending = (
                        f"exited with status {installed.exit_status}"
                        if installed.limit_hit is None
                        else f"was stopped at the {installed.limit_hit} limit"
                    )
                    return AnswerKeyRun(
                        AnswerKeyResult.CANNOT_INSTALL,
                        f"The install command `{shlex.join(install)}` {ending}.",
                    )
            tests = sandbox.run(commands.test, commit=tested, phase=Phase.TEST, limits=limits)
    results = commands.ecosystem.read_results(tests)
    if isinstance(results, Unreadable):
        return AnswerKeyRun(AnswerKeyResult.UNREADABLE, results.reason)
    not_passed = [name for name in answer_key.fail_to_pass if name not in results.passed]
    if not_passed:
        return AnswerKeyRun(
            AnswerKeyResult.FAILS,
            f"{len(not_passed)} of the {len(answer_key.fail_to_pass)} tests the answer key names "
            f"did not pass, {not_passed[0]!r} among them.",
        )
    return AnswerKeyRun(AnswerKeyResult.PASSES, "Every test the answer key names passed.")


def _with_answer_key(copy: Path, before: str, after: str, test_patch: str) -> str:
    """Commit, in `copy`, the code an answer key is run against, and return the commit.

    Its files are those of `after`, except that each file `test_patch` touches is as that patch
    leaves it from `before`. When that is how `after` already has them, `after` is returned.
    """
    git(copy, "read-tree", before)
    _apply(copy, test_patch)
    touched = _staged_changes(copy, before)
    answer_key = git(copy, "write-tree").decode().strip()
    git(copy, "read-tree", after)
    # With no file named, git would list every file of the answer key, and undo the pull request.
    if touched:
        # Each file is taken out, then put back as the answer key has it, where it has it.
        taken_out = b"".join(b"0 %s\t%s\0" % (b"0" * len(after), path) for path in touched)
        listing = ("--literal-pathspecs", "ls-tree", "-r", "-z", answer_key, "--")
        put_back = git(copy, *listing, *map(os.fsdecode, touched))
        git(copy, "update-index", "-z", "--index-info", stdin=taken_out + put_back)
    if not _staged_changes(copy, after):
        return after
    return commit_staged(copy, "Add the answer key's tests", after)


@dataclass(frozen=True)
class Selection:
    """Which instances were chosen and how they were split.

    This is the record that lets the choice be repeated: written out with `to_json` and kept,
    it is read back with `from_json` and given to `build_case_set` again.
    """

    seed: int
    """The seed the choice was made with."""

    development: tuple[str, ...]
    """The instance IDs in `Part.DEVELOPMENT`, sorted."""

    held_out: tuple[str, ...]
    """The instance IDs in `Part.HELD_OUT`, sorted. None is also in `development`."""

    def to_json(self) -> str:
        """The selection as a JSON object with `seed`, `development`, and `held_out`."""
        return json.dumps(asdict(self), indent=2)

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Read back what `to_json` wrote: `from_json(selection.to_json())` equals
        `selection`. Raises `ValueError` when `text` is not a selection."""
        not_a_selection = "the text is not a selection as `to_json` writes one"
        try:
            written = json.loads(text)
            seed, parts = written["seed"], (written["development"], written["held_out"])
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise ValueError(not_a_selection) from error
        if type(seed) is not int or not all(
            isinstance(part, list) and all(isinstance(name, str) for name in part) for part in parts
        ):
            raise ValueError(not_a_selection)
        development, held_out = parts
        if any(part != sorted(set(part)) for part in parts) or set(development) & set(held_out):
            raise ValueError(f"{not_a_selection}: each part is sorted, and no ID is in both")
        return cls(seed, tuple(development), tuple(held_out))


def select(
    instance_ids: Iterable[str], *, count: int, held_out_share: float, seed: int
) -> Selection:
    """Choose `count` of `instance_ids` and split them into the two parts.

    `round(count * held_out_share)` of those chosen are held out and the rest are for
    development. The same IDs, count, share, and seed always give the same selection,
    whatever order the IDs come in and however often one is repeated.

    Raises `ValueError` when `count` is negative or more than the number of different IDs, or
    when `held_out_share` is less than 0 or more than 1.
    """
    # Sorted, so that the seed alone decides the choice and the order the IDs came in does not.
    pool = sorted(set(instance_ids))
    if not 0 <= count <= len(pool):
        raise ValueError(f"cannot choose {count} of {len(pool)} instances")
    if not 0 <= held_out_share <= 1:
        raise ValueError(f"the held-out share must be from 0 to 1, not {held_out_share}")
    chosen = random.Random(seed).sample(pool, count)
    held_out = round(count * held_out_share)
    return Selection(seed, tuple(sorted(chosen[held_out:])), tuple(sorted(chosen[:held_out])))


class LeftOutFor(StrEnum):
    """Why an instance or a failed patch did not become a case."""

    CANNOT_IMPORT = "cannot_import"
    """`ImportFailed` was raised for it, or it is a selected instance for which no instance
    or no clone was given."""

    CANNOT_INSTALL = "cannot_install"
    """Its answer key gave `AnswerKeyResult.CANNOT_INSTALL`."""

    ANSWER_KEY_NOT_PASSED = "answer_key_not_passed"
    """A real fix whose answer key gave `FAILS` or `UNREADABLE`."""

    ANSWER_KEY_NOT_FAILED = "answer_key_not_failed"
    """A failed patch whose answer key gave `PASSES` or `UNREADABLE`."""

    NO_TRUE_CASE = "no_true_case"
    """A failed patch whose issue has no true case in the set, whether its instance was not
    selected or was itself left out."""


@dataclass(frozen=True)
class LeftOut:
    """An instance or a failed patch that did not become a case."""

    name: str
    """The name the case would have had."""

    why: LeftOutFor
    """The kind of reason."""

    detail: str
    """One sentence saying more: the `ImportFailed` message or the `AnswerKeyRun` reason,
    where there is one. It may be Untrusted Content."""


@dataclass(frozen=True)
class CaseSet:
    """The cases made from a selection, and everything that was left out of them.

    Every selected instance and every failed patch offered is in exactly one of `cases` and
    `left_out`: nothing is dropped silently.
    """

    selection: Selection
    """The selection the set was built from."""

    cases: tuple[ImportedCase, ...]
    """The true cases in the order of the selection, development first, then the false cases
    in the order their patches were offered."""

    left_out: tuple[LeftOut, ...]
    """What did not become a case: instances in the order of the selection, development
    first, then failed patches in the order they were offered."""


def build_case_set(
    selection: Selection,
    instances: Iterable[Instance],
    failed_patches: Iterable[FailedPatch],
    clones: Mapping[str, Path],
    directory: Path,
    limits: Limits,
) -> CaseSet:
    """Make the cases for `selection`, keeping only those their answer keys bear out.

    `clones` maps `Instance.repo` to a local clone. Each case's repository is made under
    `directory`. An instance that is not selected is ignored.

    Each selected instance is imported into its part with `import_instance` and its answer
    key is run with `run_answer_key`. It becomes a true case when the result is `PASSES`.

    Each failed patch whose issue has a true case in the set is imported with
    `import_failed_patch` and its answer key is run. It becomes a false case when the result
    is `FAILS`: it is shown false by the answer-key tests, not taken on the leaderboard's
    word.

    Everything else is in `left_out`, for the reason `LeftOutFor` gives.

    Raises `ValueError` before doing anything when two instances share an ID or two failed
    patches share an instance and a submission. Raises `SandboxFailed` when the Sandbox
    itself cannot do a run.
    """
    offered, patches = tuple(instances), tuple(failed_patches)
    _must_differ("instance", (instance.instance_id for instance in offered))
    _must_differ("failed patch", ((patch.instance_id, patch.submission) for patch in patches))
    by_id = {instance.instance_id: instance for instance in offered}
    true_cases: dict[str, ImportedCase] = {}
    false_cases: list[ImportedCase] = []
    left_out: list[LeftOut] = []
    selected = [
        *((instance_id, Part.DEVELOPMENT) for instance_id in selection.development),
        *((instance_id, Part.HELD_OUT) for instance_id in selection.held_out),
    ]
    for instance_id, part in selected:
        made = _true_case(instance_id, part, by_id, clones, directory, limits)
        if isinstance(made, LeftOut):
            left_out.append(made)
        else:
            true_cases[instance_id] = made
    for patch in patches:
        made = _false_case(patch, true_cases, directory, limits)
        if isinstance(made, LeftOut):
            left_out.append(made)
        else:
            false_cases.append(made)
    return CaseSet(selection, (*true_cases.values(), *false_cases), tuple(left_out))


def _must_differ(what: str, names: Iterable[Hashable]) -> None:
    repeated = [name for name, count in Counter(names).items() if count > 1]
    if repeated:
        raise ValueError(f"more than one {what} was given as {repeated[0]!r}")


def _true_case(
    instance_id: str,
    part: Part,
    instances: Mapping[str, Instance],
    clones: Mapping[str, Path],
    directory: Path,
    limits: Limits,
) -> ImportedCase | LeftOut:
    instance = instances.get(instance_id)
    if instance is None:
        return LeftOut(instance_id, LeftOutFor.CANNOT_IMPORT, "No instance with this ID was given.")
    clone = clones.get(instance.repo)
    if clone is None:
        detail = f"No clone of {instance.repo} was given."
        return LeftOut(instance_id, LeftOutFor.CANNOT_IMPORT, detail)
    try:
        imported = import_instance(instance, clone, directory, part)
    except ImportFailed as error:
        return LeftOut(instance_id, LeftOutFor.CANNOT_IMPORT, str(error))
    return _borne_out(
        imported, limits, AnswerKeyResult.PASSES, otherwise=LeftOutFor.ANSWER_KEY_NOT_PASSED
    )


def _false_case(
    failed_patch: FailedPatch,
    true_cases: Mapping[str, ImportedCase],
    directory: Path,
    limits: Limits,
) -> ImportedCase | LeftOut:
    name = _false_case_name(failed_patch)
    true_case = true_cases.get(failed_patch.instance_id)
    if true_case is None:
        detail = f"{failed_patch.instance_id} has no true case in the set."
        return LeftOut(name, LeftOutFor.NO_TRUE_CASE, detail)
    try:
        imported = import_failed_patch(true_case, failed_patch, directory)
    except ImportFailed as error:
        return LeftOut(name, LeftOutFor.CANNOT_IMPORT, str(error))
    return _borne_out(
        imported, limits, AnswerKeyResult.FAILS, otherwise=LeftOutFor.ANSWER_KEY_NOT_FAILED
    )


def _borne_out(
    imported: ImportedCase, limits: Limits, wanted: AnswerKeyResult, *, otherwise: LeftOutFor
) -> ImportedCase | LeftOut:
    """The case when its answer key gives the result wanted of it, and why it is left out when not.

    A case that is left out has its repository removed.
    """
    run = run_answer_key(imported, limits)
    if run.result is wanted:
        return imported
    shutil.rmtree(imported.case.pull_request.repository)
    cannot_install = run.result is AnswerKeyResult.CANNOT_INSTALL
    why = LeftOutFor.CANNOT_INSTALL if cannot_install else otherwise
    return LeftOut(imported.case.name, why, run.reason)
