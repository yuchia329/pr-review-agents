"""The names a Claim names that the pull request removes lines of, found by code.

A Structural Claim is verified by searching the code for texts a model chooses, and a text
narrower than the name the Claim is about is borne out by the lines the pull request did
change. So code also looks for the name itself, as an engineer would: it takes the words of the
Claim, finds what the pull request's own changes call them, sets aside a word too general to
mean anything, and searches every file of the code after the pull request for what is left. No
model is asked (ADR 0002).
"""

import re
from collections import Counter
from collections.abc import Mapping
from pathlib import Path

from pr_verification.reading import lines_of_files
from pr_verification.report import EvidenceSource, RemovedNameEvidence

GENERAL_NAME_LINES = 100
"""The most lines of the code before the pull request that may hold a name as a whole name
before it is too general to be searched for."""

_RUN = re.compile(r"[A-Za-z0-9_]+")
"""A run of the characters a name is made of, as long as it can be."""

_LETTER = re.compile(r"[A-Za-z]")
_NOT_FOLDED = re.compile(r"[^A-Za-z0-9]")


def names_removed(
    claim: str, *, repository: Path, before: str, after: str
) -> tuple[RemovedNameEvidence, ...]:
    """Search the code after the pull request for each name that `claim` names and the pull
    request removes lines of.

    `before` and `after` are the commits before and after the pull request, in `repository`.
    Files, their text, and their lines are as `reading_tools` of `pr_verification.reading` has
    them. No model is asked and nothing is run.

    A name
        A name is a run of ASCII letters, digits, and underscores that is as long as it can
        be and holds at least one letter: `Py_NOGIL`, `__post_init__`, `URLMatcher`. A line
        holds a name as a whole name when the name is in it with no letter, digit, or
        underscore directly before it or after it. So `format_full_version` is not held as a
        whole name by a line whose only use of it is `_format_full_version`.

    What the pull request removes
        A file loses a line when the file holds that line more times in `before` than in
        `after`, and gains one when it holds it more times in `after`. A file that a commit
        does not have holds no line there. So a line that only moves within its file is
        neither lost nor gained.

        The pull request removes lines of a name when the name is on a line some file loses
        and on no line any file gains. A name on both, such as the name of a setting whose
        value changes, is not one.

    What the Claim names
        Two texts are the same when folded: put in lower case, with every character that is
        not an ASCII letter or digit taken out. The words of the Claim are what its text is
        split into at white space. The Claim names a name when the name, folded, is the same
        as one word or several adjacent words of the Claim put together, folded. So a Claim
        that says `Py_NOGIL`, `py nogil`, `PYNOGIL`, or `py-nogil,` names `Py_NOGIL`. A word
        is taken whole: `js/chat.js` does not name `chat`.

    Which names are searched for
        Of the names the Claim names that the pull request removes lines of, only the most
        specific are kept: those that are longest once folded, all of them when several are
        as long. A shorter one is most often an ordinary word of the Claim's sentence.

        A name left is then set aside as too general when more than `GENERAL_NAME_LINES`
        lines of `before` hold it as a whole name. A shorter name does not take its place.

    Each name left becomes one `RemovedNameEvidence`, in the order of the names as text:

    - `source` is `EvidenceSource.READ_CODE`;
    - `name` is the name, exactly as the code writes it. Names that differ only in letter
      case or underscores are different names, each with its own Evidence;
    - `after_lines` is every line of every file of `after` that holds the name as a whole
      name, each as `<path>:<line number>:<line>`, by path and then by line number. It is
      empty when no line does;
    - `observation` is `lines holding the name: <number> after`, written by code.

    The answer is empty when the Claim names no such name, which includes a pull request
    that removes no line.
    """
    before_lines = lines_of_files(repository, before)
    after_lines = lines_of_files(repository, after)
    named = _named(claim, _removed(before_lines, after_lines))
    longest = max((len(_folded(name)) for name in named), default=0)
    most_specific = sorted(name for name in named if len(_folded(name)) == longest)
    evidence = []
    for name in most_specific:
        if len(_holding(name, before_lines)) > GENERAL_NAME_LINES:
            continue
        found = _holding(name, after_lines)
        evidence.append(
            RemovedNameEvidence(
                source=EvidenceSource.READ_CODE,
                observation=f"lines holding the name: {len(found)} after",
                name=name,
                after_lines=found,
            )
        )
    return tuple(evidence)


def _names(line: str) -> set[str]:
    """The names on a line."""
    return {run for run in _RUN.findall(line) if _LETTER.search(run)}


def _folded(text: str) -> str:
    """A text in lower case with every character that is not an ASCII letter or digit taken
    out."""
    return _NOT_FOLDED.sub("", text).lower()


def _removed(before: Mapping[str, list[str]], after: Mapping[str, list[str]]) -> set[str]:
    """The names the pull request removes lines of: on a line some file loses and on no line
    any file gains."""
    lost: set[str] = set()
    gained: set[str] = set()
    for path in before.keys() | after.keys():
        was, now = Counter(before.get(path, ())), Counter(after.get(path, ()))
        lost.update(*(_names(line) for line in was - now))
        gained.update(*(_names(line) for line in now - was))
    return lost - gained


def _named(claim: str, names: set[str]) -> set[str]:
    """Those of `names` that the Claim names: the same, folded, as one word of the Claim or
    several adjacent ones put together."""
    folds = {_folded(name) for name in names}
    longest = max(map(len, folds), default=0)
    words = [_folded(word) for word in claim.split()]
    said = set()
    for start in range(len(words)):
        together = ""
        # Words put together only grow, so one already longer than every name ends the run.
        for word in words[start:]:
            together += word
            if len(together) > longest:
                break
            said.add(together)
    return {name for name in names if _folded(name) in said}


def _holding(name: str, files: Mapping[str, list[str]]) -> tuple[str, ...]:
    """Every line of these files that holds `name` as a whole name, each as
    `<path>:<line number>:<line>`, by path and then by line number."""
    whole = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(name)}(?![A-Za-z0-9_])")
    return tuple(
        f"{path}:{number}:{line}"
        for path in sorted(files)
        for number, line in enumerate(files[path], start=1)
        if whole.search(line)
    )
