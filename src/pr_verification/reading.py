"""Reading the code of a commit: the read-only tools, and the role that locates what a Claim
concerns.

Nothing here runs code or changes anything. The files are read from the repository as the
commit holds them, outside the Sandbox, whatever the state of the working tree.
"""

from dataclasses import dataclass
from pathlib import Path

from pr_verification.agent import RoleRun, Tool
from pr_verification.model import Model
from pr_verification.report import ReadEvidence

NO_SUCH_FILE = "No file of this commit has that path."
"""What a read-only tool answers for a path that is not a file of the commit."""

NOTHING_FOUND = "Nothing in this commit matches."
"""What a read-only tool answers when it has nothing to list."""

BAD_ARGUMENTS = "The arguments are not the ones this tool takes."
"""What a read-only tool answers for arguments it cannot use."""

LOCATOR_STEP_CAP = 12
"""The most steps one run of the locator takes unless another cap is given."""


def reading_tools(repository: Path, commit: str) -> tuple[Tool, ...]:
    """The three read-only tools for the files of `commit` in `repository`.

    A file is named by its path from the root of the repository, with `/` between its parts,
    as in `billing/tax.py`. Only the files of `commit` can be reached: a path that leads
    anywhere else, such as `../secrets` or an absolute path, names no file of the commit.
    What the tools return is Untrusted Content.

    A file's text is its bytes read as UTF-8, with U+FFFD in place of any that are not. Its
    lines are what the text holds between line feeds, numbered from 1: the line feed is not
    part of the line, a last line with no line feed after it counts, and an empty file has
    no lines. Where a tool returns several entries, a line feed sits between one and the
    next and none follows the last.

    `list_files` takes an optional `directory`
        Returns the path of every file of the commit, in sorted order. With `directory`,
        named as a file is and with or without a `/` at its end, only the files under that
        directory. `NOTHING_FOUND` when there are none.

    `search` takes `text`
        Returns every line of the commit's files that holds `text` exactly as written, each
        as `<path>:<line number>:<line>`, by path and then by line number. `text` is not a
        pattern and letter case matters. `NOTHING_FOUND` when no line holds it.

    `read_file` takes `path`
        Returns every line of the file, each as `<line number>:<line>`, and so the empty
        string for an empty file. `NO_SUCH_FILE` when the commit has no file at that path,
        as for a directory.

    Each answers `BAD_ARGUMENTS` when an argument it needs is missing, when an argument is
    not a string or is empty, or when it is given an argument it does not take.
    """
    raise NotImplementedError


@dataclass(frozen=True)
class Located:
    """What the locator found for one Claim, and the record of its run."""

    evidence: tuple[ReadEvidence, ...]
    """One piece of Evidence for each place found, in the order the role named them. Empty
    when it found none."""

    run: RoleRun
    """The record of the role's run."""


def locate(
    claim: str,
    *,
    repository: Path,
    commit: str,
    model: Model,
    step_cap: int = LOCATOR_STEP_CAP,
) -> Located:
    """Find the code that `claim` concerns among the files of `commit`, by reading.

    Runs a read-only role named `locator` with `run_role`. Its tools are
    `reading_tools(repository, commit)` and no other, its step cap is `step_cap`, and its
    task is `{"claim": claim}`: its context holds its Claim and what it reads, and nothing
    else of the pull request.

    The role finishes with the places it found:
    `{"places": [{"path": "billing/tax.py", "first_line": 3, "last_line": 9}]}`.
    Each place is then read from the commit by code, and becomes one `ReadEvidence`:

    - `source` is `EvidenceSource.READ_CODE`;
    - `path`, `first_line`, and `last_line` are the place's;
    - `excerpt` is those lines of the file as the commit holds them, with a line feed
      between one and the next, whatever the role said they hold;
    - `observation` is `<path> lines <first_line> to <last_line>`, written by code.

    A place is left out when it is not an object with exactly those three members, when its
    path is not a file of the commit, or when its lines are not integers with
    `1 <= first_line <= last_line <= the number of lines in the file`, lines being what
    `reading_tools` says they are. Neither `4.0` nor `true` is an integer. So the model's
    word alone is never recorded as Evidence. There is no Evidence at all when the run
    stopped at the step cap, or when its answer has no list under `places`.

    Raises as `run_role` does.
    """
    raise NotImplementedError
