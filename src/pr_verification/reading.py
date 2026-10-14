"""Reading the code of a commit: the read-only tools, the search that code makes of a commit,
and the role that locates what a Claim concerns.

Nothing here runs code or changes anything. The files are read from the repository as the
commit holds them, outside the Sandbox, whatever the state of the working tree.
"""

import os
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pr_verification.agent import Role, RoleRun, Tool, run_role
from pr_verification.model import Model
from pr_verification.report import EvidenceSource, ReadEvidence
from pr_verification.repository import git

NO_SUCH_FILE = "No file of this commit has that path."
"""What a read-only tool answers for a path that is not a file of the commit."""

NOTHING_FOUND = "Nothing in this commit matches."
"""What a read-only tool answers when it has nothing to list."""

NO_SUCH_LINES = "The file has no lines in the part asked for."
"""What `read_file` answers for a part of a file that holds none of its lines."""

BAD_ARGUMENTS = "The arguments are not the ones this tool takes."
"""What a read-only tool answers for arguments it cannot use."""

LOCATOR_STEP_CAP = 12
"""The most steps one run of the locator takes unless another cap is given."""

_LIST_FILES = """\
Lists the path of every file, one to a line. Takes an optional "directory", to list only the \
files under it, as in {"directory": "billing"}."""

_SEARCH = """\
Finds every line of every file that holds "text" exactly as written, as in {"text": "tax_rate"}. \
The text is not a pattern, and letter case matters. Each line found is given as \
<path>:<line number>:<line>."""

_READ_FILE = """\
Reads one file, named by its "path" from the root of the repository, as in \
{"path": "billing/tax.py"}. Each line is given as <line number>:<line>. Takes an optional \
"first_line" and "last_line", to read only the lines numbered from the one to the other, as in \
{"path": "billing/tax.py", "first_line": 200, "last_line": 400}. Read a long file in parts this \
way: a result that is too long is cut, and says how much was left out."""

_LOCATOR_INSTRUCTIONS = """\
You find the code that a Claim concerns. A Claim is a checkable statement a pull request makes
about its own changes, and it is the "claim" of your task.

Your tools read the files of the repository. Use them to find the places a reader would have to
look at to check the Claim: the functions, classes, settings, or documents it is about. Name only
lines you have read, and as few of them as show what the Claim concerns.

Do not judge whether the Claim is true. Finish with the places you found, each as a file's path
and the first and last of its lines, counted from 1, in this form:
{"places": [{"path": "billing/tax.py", "first_line": 3, "last_line": 9}]}
When you find none, finish with {"places": []}."""


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

    `read_file` takes `path`, and an optional `first_line` and `last_line`
        Returns every line of the file, each as `<line number>:<line>`, and so the empty
        string for an empty file. `NO_SUCH_FILE` when the commit has no file at that path,
        as for a directory.

        With `first_line`, `last_line`, or both, only the lines numbered from `first_line`
        to `last_line` are returned, both included, so a long file can be read in parts.
        Each line keeps its own number in the file. Without `first_line` the part starts at
        line 1, and without `last_line` it runs to the last line of the file. A `last_line`
        beyond the end of the file is no fault: the lines there are returned.
        `NO_SUCH_LINES` when the file has no line in the part asked for, as when
        `first_line` is beyond its end, and for any part of an empty file. `NO_SUCH_FILE`
        comes before both: it is the answer whenever the path names no file and the
        arguments are ones the tool takes.

        The two are whole numbers as JSON writes them: neither `4.0`, `"4"`, nor `true` is
        one. `BAD_ARGUMENTS` when either is anything else, when `first_line` is less than
        1, and when `last_line` is less than `first_line`, or less than 1 with no
        `first_line`.

    Each answers `BAD_ARGUMENTS` when an argument it needs is missing, when an argument that
    is not a line number is not a string or is empty, or when it is given an argument it
    does not take.

    A tool returns what it finds whole, however long. What a role is shown of one result is
    bounded by `run_role`, keeping the start: each of the three has `Tool.keeps` as
    `Keep.START`.
    """
    return _tools(_Files(repository, commit))


def lines_holding(
    repository: Path, commit: str, text: str, path: str | None = None
) -> tuple[str, ...]:
    """Every line of the files of `commit` in `repository` that holds `text`, found by code.

    It is the search that code makes of a commit, where the `search` tool of `reading_tools`
    is the one a role asks for, and the two agree: a line holds a text here when, and only
    when, that tool finds it. No model is asked and nothing is run.

    A line holds `text` when `text` is in it exactly as written: `text` is not a pattern,
    letter case matters, and a text that holds a line feed is held by no line. Files, their
    text, and their lines are as `reading_tools` has them. Each line found is given as
    `<path>:<line number>:<line>`, by path and then by line number, and the answer is empty
    when no line holds the text. What the lines hold is Untrusted Content.

    With `path`, only the lines of the file at that path are searched, a file being named as
    `reading_tools` names one. A commit with no file there has no line that holds the text,
    so the answer is empty: for a path the commit does not have, for a directory, and for a
    path that leads anywhere else, such as `../secrets` or an absolute path.

    `text` is expected not to be empty. The `search` tool refuses an empty text, and here
    every line holds one, so a caller that takes its text from a model checks it first.
    """
    files = _Files(repository, commit)
    if path is None:
        return tuple(files.holding(text, sorted(files.paths)))
    return tuple(files.holding(text, [path])) if path in files.paths else ()


def lines_of_files(repository: Path, commit: str) -> dict[str, list[str]]:
    """Every file of `commit` in `repository` with its lines, by path. Files, their text, and
    their lines are as `reading_tools` has them. What the lines hold is Untrusted Content."""
    files = _Files(repository, commit)
    return dict(files.lines_of_each(sorted(files.paths)))


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
    word alone is never recorded as Evidence. There is no Evidence at all when the run did
    not finish, or when its answer has no list under `places`.

    Raises as `run_role` does.
    """
    files = _Files(repository, commit)
    role = Role("locator", _LOCATOR_INSTRUCTIONS, _tools(files), step_cap)
    run = run_role(role, {"claim": claim}, model)
    places = run.answer.get("places") if run.answer is not None else None
    read = (_read_place(files, place) for place in places) if isinstance(places, list) else ()
    return Located(evidence=tuple(place for place in read if place is not None), run=run)


def _read_place(files: "_Files", place: object) -> ReadEvidence | None:
    """The lines a place names, read from the commit, or None when it names no such lines."""
    if not (isinstance(place, dict) and place.keys() == {"path", "first_line", "last_line"}):
        return None
    path, first, last = place["path"], place["first_line"], place["last_line"]
    if not (isinstance(path, str) and path in files.paths):
        return None
    # Asked whether it is an instance of `int`, `true` would pass for 1.
    if not (type(first) is int and type(last) is int):
        return None
    lines = files.lines(path)
    if not 1 <= first <= last <= len(lines):
        return None
    return ReadEvidence(
        source=EvidenceSource.READ_CODE,
        observation=f"{path} lines {first} to {last}",
        path=path,
        first_line=first,
        last_line=last,
        excerpt="\n".join(lines[first - 1 : last]),
    )


class _Files:
    """The files of one commit, read from git's object store and never from the working tree.

    A file is reached only by finding its path among those the commit lists. Nothing a model
    wrote is handed to git as a path, so no path can lead out of the commit.
    """

    def __init__(self, repository: Path, commit: str) -> None:
        self._repository = repository
        # Each entry is `<mode> <type> <object>\t<path>`. A submodule is listed and is not a file.
        listing = os.fsdecode(git(repository, "ls-tree", "-r", "-z", "--end-of-options", commit))
        self._objects = {
            path: details.split()[2]
            for details, _, path in (entry.partition("\t") for entry in listing.split("\0"))
            if " blob " in details
        }

    @property
    def paths(self) -> Collection[str]:
        """The path of every file of the commit."""
        return self._objects.keys()

    def lines(self, path: str) -> list[str]:
        """The lines of the file at `path`, which is one of `paths`."""
        ((_, lines),) = self.lines_of_each([path])
        return lines

    def lines_of_each(self, paths: Sequence[str]) -> Iterator[tuple[str, list[str]]]:
        """Each of these files with its lines, in the order given. Each is one of `paths`."""
        asked = "".join(f"{self._objects[path]}\n" for path in paths)
        # One git process reads them all. It answers for each with `<object> <type> <size>`, a
        # line feed, the content, and a line feed.
        answers = git(self._repository, "cat-file", "--batch", stdin=asked.encode())
        start = 0
        for path in paths:
            content_starts = answers.index(b"\n", start) + 1
            content_ends = content_starts + int(answers[start:content_starts].split()[2])
            yield path, _lines(answers[content_starts:content_ends])
            start = content_ends + 1

    def holding(self, text: str, paths: Sequence[str]) -> list[str]:
        """Every line of these files that holds `text` exactly as written, each as
        `<path>:<line number>:<line>`, in the order of `paths` and then by line number. Each
        is one of `paths`."""
        return [
            f"{path}:{number}:{line}"
            for path, lines in self.lines_of_each(paths)
            for number, line in enumerate(lines, start=1)
            if text in line
        ]


def _lines(content: bytes) -> list[str]:
    """The lines of a file, a line being what `reading_tools` says it is."""
    lines = content.decode(errors="replace").split("\n")
    # Nothing after the last line feed is no line: an empty file has none, and a line feed at
    # the end of a file starts none.
    if lines[-1] == "":
        lines.pop()
    return lines


def _tools(files: _Files) -> tuple[Tool, ...]:
    """The three read-only tools over these files."""

    def list_files(arguments: Mapping[str, object]) -> str:
        given = usable_arguments(arguments, optional=("directory",))
        if given is None:
            return BAD_ARGUMENTS
        paths = sorted(files.paths)
        if "directory" in given:
            under = given["directory"].removesuffix("/") + "/"
            paths = [path for path in paths if path.startswith(under)]
        return "\n".join(paths) if paths else NOTHING_FOUND

    def search(arguments: Mapping[str, object]) -> str:
        given = usable_arguments(arguments, needed=("text",))
        if given is None:
            return BAD_ARGUMENTS
        found = files.holding(given["text"], sorted(files.paths))
        return "\n".join(found) if found else NOTHING_FOUND

    def read_file(arguments: Mapping[str, object]) -> str:
        line_numbers = arguments.keys() & _LINE_NUMBERS
        others = {name: value for name, value in arguments.items() if name not in line_numbers}
        given = usable_arguments(others, needed=("path",))
        part = _part(arguments)
        if given is None or part is None:
            return BAD_ARGUMENTS
        if given["path"] not in files.paths:
            return NO_SUCH_FILE
        first, last = part
        numbered = list(enumerate(files.lines(given["path"]), start=1))[first - 1 : last]
        # A file read whole may have no lines. A part that holds none is said to.
        if line_numbers and not numbered:
            return NO_SUCH_LINES
        return "\n".join(f"{number}:{line}" for number, line in numbered)

    return (
        Tool("list_files", _LIST_FILES, list_files),
        Tool("search", _SEARCH, search),
        Tool("read_file", _READ_FILE, read_file),
    )


_LINE_NUMBERS = frozenset({"first_line", "last_line"})
"""The arguments of `read_file` that are line numbers."""


def _part(arguments: Mapping[str, object]) -> tuple[int, int | None] | None:
    """The first and the last line of the part of a file that a model asked `read_file` for,
    the last being None when the part runs to the end of the file. None when the line numbers
    are not ones the tool can use."""
    first = arguments.get("first_line", 1)
    # Asked whether it is an instance of `int`, `true` would pass for 1.
    if type(first) is not int or first < 1:
        return None
    if "last_line" not in arguments:
        return first, None
    last = arguments["last_line"]
    if type(last) is not int or last < first:
        return None
    return first, last


def usable_arguments(
    arguments: Mapping[str, object], needed: Sequence[str] = (), optional: Sequence[str] = ()
) -> dict[str, str] | None:
    """The arguments a model gave, when they are ones a tool can use: all it needs, none it
    does not take, and each a string that is not empty. None when they are not."""
    if not set(needed) <= arguments.keys() <= {*needed, *optional}:
        return None
    given = {name: value for name, value in arguments.items() if isinstance(value, str) and value}
    return given if len(given) == len(arguments) else None
