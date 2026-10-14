"""The Structural verifier: gathers Evidence for one Structural Claim by reading the code.

A Structural Claim is about the text of the code, so nothing is run to verify it. A model
reads the code and names the texts the Claim says the pull request adds or removes. Code then
searches the code before and after the pull request for each, and records what it found, so
that a Verdict rests on what code found and not on what a model said (ADR 0002). Whether a
Claim is Confirmed or Refuted is not decided here: the verifier gives Evidence, or the reason
there is none.
"""

from dataclasses import dataclass
from pathlib import Path

from pr_verification.agent import Role, RoleRun, Stopped, Tool, _stopped_looping, run_role
from pr_verification.decision import Decider
from pr_verification.model import Model, ModelFailed
from pr_verification.reading import lines_holding, reading_tools
from pr_verification.removed_names import names_removed
from pr_verification.report import (
    EvidenceSource,
    RemovedNameEvidence,
    SearchEvidence,
    TextChange,
)

VERIFIER_STEP_CAP = 12
"""The most steps one run of the Structural verifier takes unless another cap is given."""

TEXT_CAP = 8
"""The most texts the code is searched for, for one Claim."""

VERIFIER_STEP_CAP_REACHED = (
    "The Structural verifier did not finish within {steps} steps, so the code was searched "
    "for no text."
)
"""Why a Claim is Inconclusive when the verifier's run stopped at the step cap. `steps` is the
step cap."""

VERIFIER_COULD_NOT_FINISH = "The Structural verifier could not finish its work: {failure}"
"""Why a Claim is Inconclusive when the model failed. `failure` is what the `ModelFailed`
raised said."""

_INSTRUCTIONS = """\
You find the texts that a Structural Claim is about. A Structural Claim is a statement a pull
request makes about the text of its code, such as a rename, a removal, a change to a document, or a
value in a configuration file. It is the "claim" of your task.

Your tools read the files of the repository as they were before the pull request and as they are
after it. Use them to find what the Claim says the pull request adds or removes, exactly as the
code writes it: a name, a setting with its value, a line of a document.

Do not judge whether the Claim is true. The code of both is searched for each text you name, and
what is found decides. A text is matched exactly as written and letter case matters. It is not a
pattern, and it is one line or part of one. Name a text long enough to tell it apart from everything
else in the code and no longer, and at most {cap} of them. Give a "path" to search only the one
file the Claim is about.

Finish with each text and whether the Claim says the pull request "added" it or "removed" it, in
this form:
{{"texts": [{{"text": "UserManager", "change": "removed"}},
{{"text": "retries = 3", "change": "added", "path": "settings.toml"}}]}}
When the Claim names no text that is added or removed, finish with {{"texts": []}}."""

_READS = {"before": "In the code before the pull request", "after": "In the code after it"}
"""Which code a tool reads, by what stands in front of its name: how its description starts."""

_MEMBERS = {"text", "change", "path"}
"""The members an entry of the role's answer may have."""


@dataclass(frozen=True)
class StructuralRun:
    """The run record of the Structural verifier for one Structural Claim."""

    run: RoleRun | None
    """The record of the verifier's role, or None when the model failed during its run."""

    evidence: tuple[SearchEvidence, ...]
    """One piece of Evidence for each text the code was searched for, in the order the role
    named them. Empty when it was searched for none."""

    inconclusive: str | None
    """Why the Claim is Inconclusive, when the role's run came to no answer:
    `VERIFIER_STEP_CAP_REACHED`, `VERIFIER_COULD_NOT_FINISH`, or `STOPPED_LOOPING` of
    `pr_verification.agent`, filled in. There is no Evidence then. None when the role
    finished, whatever it finished with."""

    names: tuple[RemovedNameEvidence, ...] = ()
    """One piece of Evidence for each name the Claim names that the pull request removes
    lines of, as `names_removed` gives them. Code finds them, whatever the role named. Empty
    when there is none, and when `inconclusive` is not None."""


def verify_structural_claim(
    claim: str,
    *,
    repository: Path,
    before: str,
    after: str,
    model: Model,
    step_cap: int = VERIFIER_STEP_CAP,
    progress_guard: Decider | None = None,
) -> StructuralRun:
    """Gather Evidence for `claim` by reading the code before and after the pull request.

    `claim` is a Structural Claim. `before` and `after` are the commits before and after the
    pull request, in `repository`. No Sandbox is among the arguments and nothing is run: a
    Structural Claim is never sent to one.

    Runs a role named `structural-verifier` with `run_role`. Its tools are those of
    `reading_tools(repository, before)` and those of `reading_tools(repository, after)`, six
    in all and no other. Each has `before_` or `after_` in front of its name, as in
    `before_search` and `after_read_file`, and does for its commit what the tool of that
    name does. So the role can read what the pull request removed as well as what it added,
    and every tool it has is read-only: it has none that runs or changes anything. Its step
    cap is `step_cap`, and its task is `{"claim": claim}`: its context holds its Claim and
    what it reads, and nothing else of the pull request. The run is given `progress_guard`,
    so it is watched for loops as `run_role` says.

    The role finishes with the texts the Claim says the pull request adds or removes:
    `{"texts": [{"text": "UserManager", "change": "removed"},
    {"text": "retries = 3", "change": "added", "path": "settings.toml"}]}`.
    A `change` is a value of `TextChange`. A `path` may be left out, and limits the search to
    the one file it names, as `reading_tools` names a file.

    Each text is then searched for by code, in the code of both commits, and becomes one
    `SearchEvidence`: the one `search_for` gives for the entry's `text` and `change`, and its
    `path`, or None when it was left out. So a text said to be removed from one file is
    searched for in the other files as well, whatever the role said of them.

    So what is recorded is what code found in the two commits, whatever the role said is
    there. The model's word alone is never Evidence, and the role gives no Verdict: one is
    computed from this Evidence by `compute_verdict`.

    When the role finished, whatever it finished with, `names` is what
    `names_removed(claim, repository=repository, before=before, after=after)` gives. The
    role has no say in it and is told nothing of it: its answer may name no text at all, or
    not be one that can be read, and the names are the same.

    An entry is left out when it is not an object, when it has no `text`, no `change`, or a
    member other than those and `path`, when its `text` is not a string, is blank, or holds a
    line feed, when its `change` is not one of the two values, and when its `path` is not a
    string or is empty. `evidence` is empty when the answer has no list under
    `texts`, or when that list has more than `TEXT_CAP` entries, counting those that would be
    left out: the cap is enforced here, by code, and a longer list is searched for nothing.

    When the run stopped at the step cap, there is no Evidence and `inconclusive` is
    `VERIFIER_STEP_CAP_REACHED` filled in. When it stopped with `Stopped.LOOP`, the role
    having been stopped for going round in circles, there is no Evidence and `inconclusive`
    is `STOPPED_LOOPING` filled in for the run. When `ModelFailed` is raised, by the model or as
    a `DecisionFailed` by the progress guard, the work ends there: the record is returned
    with no `run`, no Evidence, and `inconclusive` as `VERIFIER_COULD_NOT_FINISH` filled in.
    A failure inside the system leaves the Claim Inconclusive.

    Raises `ValueError` before any request when `step_cap` is less than 1. Any other
    exception is a fault in the system and is raised.
    """
    commits = {"before": before, "after": after}
    role = Role(
        name="structural-verifier",
        instructions=_INSTRUCTIONS.format(cap=TEXT_CAP),
        tools=tuple(
            Tool(
                f"{which}_{tool.name}",
                f"{_READS[which]}: {tool.description}",
                tool.run,
                keeps=tool.keeps,
            )
            for which, commit in commits.items()
            for tool in reading_tools(repository, commit)
        ),
        step_cap=step_cap,
    )
    try:
        run = run_role(role, {"claim": claim}, model, progress_guard=progress_guard)
    except ModelFailed as failure:
        reason = VERIFIER_COULD_NOT_FINISH.format(failure=failure)
        return StructuralRun(run=None, evidence=(), inconclusive=reason)
    if run.stopped is Stopped.STEP_CAP:
        reason = VERIFIER_STEP_CAP_REACHED.format(steps=step_cap)
        return StructuralRun(run, evidence=(), inconclusive=reason)
    if run.stopped is Stopped.LOOP:
        return StructuralRun(run, evidence=(), inconclusive=_stopped_looping(run))
    # The role finished. Code finds the names itself, whatever the role finished with.
    names = names_removed(claim, repository=repository, before=before, after=after)
    texts = run.answer.get("texts") if run.answer is not None else None
    if not isinstance(texts, list) or len(texts) > TEXT_CAP:
        return StructuralRun(run, evidence=(), inconclusive=None, names=names)
    named = [entry for entry in map(_named, texts) if entry is not None]
    evidence = tuple(
        search_for(repository, before, after, text, change, path) for text, change, path in named
    )
    return StructuralRun(run, evidence, inconclusive=None, names=names)


def _named(entry: object) -> tuple[str, TextChange, str | None] | None:
    """The text an entry of the role's answer names, what it says the pull request does to it,
    and the one file to search when it names one. None when the entry cannot be used."""
    if not (isinstance(entry, dict) and {"text", "change"} <= entry.keys() <= _MEMBERS):
        return None
    text, change, path = entry["text"], entry["change"], entry.get("path")
    if not (isinstance(text, str) and text.strip() and "\n" not in text):
        return None
    if not (isinstance(change, str) and change in TextChange):
        return None
    if "path" in entry and not (isinstance(path, str) and path):
        return None
    return text, TextChange(change), path


def search_for(
    repository: Path,
    before: str,
    after: str,
    text: str,
    change: TextChange,
    path: str | None = None,
) -> SearchEvidence:
    """What code finds of one text in the commits before and after a pull request.

    It is the search behind every `SearchEvidence`: the Structural verifier makes it for each
    text its role names, and the Structural cases of the evaluation make it for each text of
    an answer key. No model is asked and nothing is run. Nothing of the caller's is in the
    Evidence but `text`, `change`, and `path`:

    - `source` is `EvidenceSource.READ_CODE`;
    - `text`, `change`, and `path` are as given;
    - `before_lines` is every line of `before` that holds the text, and `after_lines` every
      line of `after` that does, each as `lines_holding` of `pr_verification.reading` gives
      them for the commit, the text, and the `path`. A line holds a text, and is written
      and ordered, as the `search` tool of `reading_tools` has it:
      `<path>:<line number>:<line>`, by path and then by line number. With a `path`, only
      the lines of the file at that path are searched, and a commit with no file there has
      no line that holds the text;
    - `after_lines_elsewhere` is, when `change` is `TextChange.REMOVED` and there is a
      `path`, every line of `after` that holds the text in any other file: the lines that
      `lines_holding` gives for `after` and the text with no path, less those of
      `after_lines`, in the same order. So a `path` that the commit has no file at leaves
      every line of `after` that holds the text here. It is empty when `change` is
      `TextChange.ADDED` and when there is no `path`;
    - `observation` is `lines holding the text: <number> before, <number> after`, written by
      code. It counts `before_lines` and `after_lines` and no other line.

    `text` is expected not to be empty, as `lines_holding` expects.
    """
    before_lines = lines_holding(repository, before, text, path)
    after_lines = lines_holding(repository, after, text, path)
    elsewhere: tuple[str, ...] = ()
    if change is TextChange.REMOVED and path is not None:
        in_the_file = set(after_lines)
        elsewhere = tuple(
            line for line in lines_holding(repository, after, text) if line not in in_the_file
        )
    return SearchEvidence(
        source=EvidenceSource.READ_CODE,
        observation=(
            f"lines holding the text: {len(before_lines)} before, {len(after_lines)} after"
        ),
        text=text,
        change=change,
        path=path,
        before_lines=before_lines,
        after_lines=after_lines,
        after_lines_elsewhere=elsewhere,
    )
