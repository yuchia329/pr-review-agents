"""Evaluation cases from SWE-bench Verified: real fixes as true Claims, failed patches as false.

Nothing here downloads anything. Instances and failed patches are handed in, along with a
local clone of each repository. The issue text and every patch are Untrusted Content.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Self

from pr_verification.evaluation import Case, Part
from pr_verification.sandbox import Limits

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
    raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError


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
        raise NotImplementedError

    @classmethod
    def from_json(cls, text: str) -> Self:
        """Read back what `to_json` wrote: `from_json(selection.to_json())` equals
        `selection`. Raises `ValueError` when `text` is not a selection."""
        raise NotImplementedError


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
    raise NotImplementedError


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
    raise NotImplementedError
