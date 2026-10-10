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
    """The tests that fail before the real fix and pass after it, named as the dataset does."""

    pass_to_pass: tuple[str, ...]
    """The tests that pass both before the real fix and after it."""


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
    """`Instance.fail_to_pass`."""

    pass_to_pass: tuple[str, ...]
    """`Instance.pass_to_pass`."""


@dataclass(frozen=True)
class ImportedCase:
    """A case made from an instance, with its answer key held beside it and not inside it.

    Only `case` is given to the harness, so nothing of the answer key reaches the system
    during a run: not in the pull request text, and not in either commit.
    """

    case: Case
    """The case to evaluate."""

    answer_key: AnswerKey
    """The tests that say whether the pull request really fixes the issue."""


class ImportFailed(Exception):
    """An instance or a failed patch could not be made into a case. Its message says why."""


def import_instance(instance: Instance, repository: Path, part: Part) -> ImportedCase:
    """Make the true case for `instance`: the real fix is the pull request.

    `repository` is a local clone of `instance.repo` that holds `base_commit`. The case has:

    - `name`: `instance.instance_id`;
    - `part`: `part`;
    - `expected`: Confirmed;
    - `pull_request.repository`: `repository`;
    - `pull_request.before`: `base_commit`, as its full commit ID;
    - `pull_request.after`: a new commit whose only parent is `before` and whose files are
      those of `before` with `instance.patch` applied, and nothing of `test_patch`;
    - `pull_request.title`: `SWE_BENCH_TITLE`;
    - `pull_request.description`: `instance.problem_statement`, unchanged;
    - `pull_request.linked_issue`: empty.

    The answer key holds the instance's `test_patch`, `fail_to_pass`, and `pass_to_pass`.

    Making the commit moves no branch of `repository` and leaves its working tree and index
    as they were, so many instances can be imported into one clone.

    Raises `ImportFailed` when `base_commit` is not in `repository`, or when `patch` does not
    apply to it or changes nothing.
    """
    raise NotImplementedError


def import_failed_patch(true_case: ImportedCase, failed_patch: FailedPatch) -> ImportedCase:
    """Make a false case from `failed_patch`, for the issue `true_case` is the true case of.

    A false case can only be made from a true one, so each is for an issue that also has a
    true case. The false case has the true case's part, repository, `before`, title,
    description, linked issue, and answer key. It differs in:

    - `name`: `<instance_id>@<submission>`;
    - `expected`: Refuted;
    - `pull_request.after`: a new commit made as `import_instance` makes one, with
      `failed_patch.patch` applied in place of the real fix.

    Raises `ImportFailed` when `failed_patch.instance_id` is not the true case's name, or
    when the patch does not apply to `before` or changes nothing.
    """
    raise NotImplementedError


class AnswerKeyResult(StrEnum):
    """What running an answer key against a pull request showed."""

    PASSES = "passes"
    """Every test named in `fail_to_pass` and `pass_to_pass` passed."""

    FAILS = "fails"
    """The tests ran and gave results, and at least one of those named did not pass."""

    CANNOT_INSTALL = "cannot_install"
    """The Python Ecosystem could not install the repository, so no test was run."""

    UNREADABLE = "unreadable"
    """The repository was installed, and the run of its tests held no results to read."""


@dataclass(frozen=True)
class AnswerKeyRun:
    """One run of an answer key."""

    result: AnswerKeyResult
    """What the run showed."""

    reason: str
    """One sentence saying why. For `CANNOT_INSTALL` after `Unsupported`, it is that
    answer's reason. Otherwise its wording is not part of the contract."""


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
    - `PASSES`: every test named in `fail_to_pass` and `pass_to_pass` is in `Results.passed`.
    - `FAILS`: anything else.

    The answer key is never written into the case's repository: the work is done in a copy
    that is removed afterwards, and the repository holds no commit and no ref it did not hold
    before. Raises `SandboxFailed` when the Sandbox itself cannot do a run.
    """
    raise NotImplementedError


@dataclass(frozen=True)
class Selection:
    """Which instances were chosen and how they were split: the record that lets the choice
    be repeated."""

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
    when `held_out_share` is not between 0 and 1.
    """
    raise NotImplementedError


class LeftOutFor(StrEnum):
    """Why an instance or a failed patch did not become a case."""

    CANNOT_IMPORT = "cannot_import"
    """`ImportFailed` was raised, or no instance or no repository was given for it."""

    CANNOT_INSTALL = "cannot_install"
    """Its answer key gave `AnswerKeyResult.CANNOT_INSTALL`."""

    NOT_CONFIRMED_TRUE = "not_confirmed_true"
    """A real fix whose answer key gave `FAILS` or `UNREADABLE`."""

    NOT_CONFIRMED_FALSE = "not_confirmed_false"
    """A failed patch whose answer key gave `PASSES` or `UNREADABLE`."""

    NO_TRUE_CASE = "no_true_case"
    """A failed patch for an issue that has no true case in the set."""


@dataclass(frozen=True)
class LeftOut:
    """An instance or a failed patch that did not become a case."""

    name: str
    """The name the case would have had."""

    why: LeftOutFor
    """The kind of reason."""

    reason: str
    """One sentence saying why: the `ImportFailed` message or the `AnswerKeyRun` reason,
    where there is one."""


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
    """What did not become a case, in the same order."""


def build_case_set(
    selection: Selection,
    instances: Iterable[Instance],
    failed_patches: Iterable[FailedPatch],
    repositories: Mapping[str, Path],
    limits: Limits,
) -> CaseSet:
    """Make the cases for `selection`, keeping only those their answer keys confirm.

    `repositories` maps `Instance.repo` to a local clone. An instance that is not selected
    is ignored.

    Each selected instance is imported into its part with `import_instance` and its answer
    key is run with `run_answer_key`. It becomes a true case when the result is `PASSES`.

    Each failed patch whose issue has a true case in the set is imported with
    `import_failed_patch` and its answer key is run. It becomes a false case when the result
    is `FAILS`: it is confirmed false by the answer-key tests, not taken on the leaderboard's
    word.

    Everything else is in `left_out`, for the reason `LeftOutFor` gives. Raises
    `SandboxFailed` when the Sandbox itself cannot do a run.
    """
    raise NotImplementedError
