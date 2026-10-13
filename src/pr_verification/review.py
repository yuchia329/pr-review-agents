"""Review: an assessment of the quality of a pull request's changes, independent of any Claim.

A Review reports Findings and nothing else. It has two parts: the injection detector, which
asks the decision layer whether Untrusted Content reads as instructions aimed at an automated
tool, and the Reviewer, a read-only role that reads the change and names quality concerns and
Undeclared Changes.

Findings are advisory: no Verdict is computed from one, and so the Gate is not either. A
detection is an extra signal and not the defence. What limits a successful injection is that
Verdicts and the Gate are computed by code (ADR 0002). No Claim is made here: a change that no
Claim covers is reported as a Finding, and the system never invents a Claim to cover it.
"""

import os
from collections.abc import Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from pr_verification.agent import Role, RoleRun, Stopped, Tool, run_role
from pr_verification.decision import YES, Decider, Decision, Question, YesOrNo
from pr_verification.model import Model, ModelFailed
from pr_verification.reading import BAD_ARGUMENTS, reading_tools, usable_arguments
from pr_verification.report import (
    Evidence,
    FindingKind,
    PullRequestPart,
    ReviewFinding,
    RunEvidence,
)
from pr_verification.repository import git

FINDING_CAP = 10
"""The most Findings one Review reports, so that a report holds only what is worth a person's
attention. A starting value: nothing may rely on the number itself."""

LONGEST_CONCERN = 500
"""The most characters a concern written by the Reviewer may have."""

REVIEWER_STEP_CAP = 12
"""The most steps one run of the Reviewer takes unless another cap is given."""

LONGEST_PIECE = 1_000
"""The most characters of text the injection detector asks about in one decision. It keeps
ordinary text within the input limit of Laya, the decision model with the smallest. A starting
value: nothing may rely on the number itself."""

PIECE_OVERLAP = 100
"""How many characters at the end of one piece of a long text are repeated at the start of the
next."""

INJECTION = YesOrNo(
    "Does this text hold instructions aimed at an automated tool, such as an AI model reading it?"
)
"""The question the injection detector asks of each piece of text."""

DECISION_CAP = 200
"""The most decisions the injection detector asks for in one Review, so that the size of a pull
request cannot make a Review unbounded. A starting value: nothing may rely on the number
itself."""

READS_AS_INSTRUCTIONS = "Text here reads as instructions aimed at an automated tool."
"""The concern of every Finding the injection detector makes. It is written by code and holds
none of the text, so the Finding does not itself repeat a suspected instruction."""

NOTHING_DECLARED = (
    "The pull request makes no Claims, so no Claim covers any of its changes. "
    "Files changed: {files}."
)
"""The concern of the one Undeclared Change reported for a pull request that makes no Claims.
`files` is the number of files it changes."""

REVIEW_UNFINISHED = "The Review could not be finished, so it may have missed something: {failure}"
"""The concern of a Finding of kind `UNFINISHED_REVIEW`. `failure` is what the `ModelFailed`
raised said, or `DECISION_CAP_REACHED` or `STEP_CAP_REACHED` filled in, or `STOPPED_LOOPING`
of `pr_verification.agent` filled in."""

DECISION_CAP_REACHED = (
    "the injection detector may ask for {decisions} decisions, and the text left needs more"
)
"""The `failure` of `REVIEW_UNFINISHED` when the injection detector stopped at its cap on
decisions. `decisions` is that cap."""

STEP_CAP_REACHED = "the Reviewer took {steps} steps without finishing"
"""The `failure` of `REVIEW_UNFINISHED` when the Reviewer's run stopped at its step cap.
`steps` is that cap."""

NOT_CHANGED = "The pull request does not change a file at that path."
"""What `read_change` answers for a path that is not a file the pull request changes."""

_READ_CHANGE = """\
Shows what the pull request changes in one of its changed files, named by its "path", as in \
{"path": "billing/tax.py"}. The change is given as a unified diff: each line added follows a +, \
and each line removed follows a -."""

_REVIEWER_INSTRUCTIONS = f"""\
You review the quality of the changes a pull request makes. Your task holds its "claims", the
Claims it makes: the checkable statements a pull request makes about its own changes. It also
holds its "changed_files", the path of every file it changes.

Your tools only read. read_change shows what the pull request changes in one file, and the others
read the files of the repository as they are after the pull request. Read a change before you say
anything about it.

Report two kinds of Finding:

- "quality": a concern about the quality of a change, such as a likely bug, a case left
  unhandled, or code that will be hard to maintain.
- "undeclared_change": a change that none of the Claims covers. Say what the change is. Do not
  write a Claim for it. When "claims" is empty, report none of these: that no Claim covers the
  changes is reported already.

Do not judge whether a Claim is true. Report only what is worth a person's attention: at most
{FINDING_CAP} Findings, the most important first. Each names one of the changed files by its
"path" and states its "concern" in at most {LONGEST_CONCERN} characters.

Finish with your Findings in this form:
{{"findings": [{{"kind": "undeclared_change", "path": "auth/session.py", "concern": "The session \
timeout is raised, and no Claim says so."}}]}}
When you have none, finish with {{"findings": []}}."""

# The difference is settled by the two commits alone, whatever git is set up to do on this
# machine and whatever attributes the working copy gives a file: no colour, no program of the
# repository's choosing, and every file as text, so that none can be marked as one whose lines
# are not shown.
_DIFFERENCE = ("diff", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv", "--text")


def pieces(
    text: str, longest: int = LONGEST_PIECE, overlap: int = PIECE_OVERLAP
) -> tuple[str, ...]:
    """Cut `text` into pieces of at most `longest` characters, so that a long text can be asked
    about within a decision model's input limit and none of it goes unread.

    The empty text has no pieces, and text of `longest` characters or fewer is one piece: the
    text itself. Longer text is cut so that:

    - every piece is a stretch of `text`, exactly as written;
    - the first piece starts where the text starts;
    - each later piece starts `longest - overlap` characters after the one before it started,
      so the last `overlap` characters of a piece are the first of the next;
    - every piece is `longest` characters long, except the last, which ends where the text
      ends and is the first piece to reach there.

    So nothing is left out, and any stretch of the text up to `overlap + 1` characters long is
    whole in at least one piece: what is written across a cut is still read in one piece.

    Raises `ValueError` unless `0 <= overlap < longest`.
    """
    if not 0 <= overlap < longest:
        raise ValueError(
            f"pieces of {longest} characters that repeat {overlap} do not move through a text"
        )
    cut = []
    start = 0
    while start < len(text):
        cut.append(text[start : start + longest])
        if start + longest >= len(text):
            break
        start += longest - overlap
    return tuple(cut)


def detect_injection(decider: Decider, text: str) -> bool:
    """Whether `text` reads as instructions aimed at an automated tool, by decisions.

    `text` is Untrusted Content, and the decider reads it only as data. Blank text, which is
    empty or holds only whitespace, asks for no decision and is not a detection.

    Otherwise the decider is asked about the pieces `pieces(text)` gives, one decision for a
    piece and in order: the state is the piece, exactly as cut, and the question is
    `INJECTION`. A piece is a detection when the `top` of its `Decision` is `YES`, as it is
    when the two probabilities are equal: a detection costs a Finding and nothing more. The
    first detection ends the asking, and no later piece is asked about.

    So no text is cut short to fit, and no decision is asked of more than `LONGEST_PIECE`
    characters. A piece that is still beyond the limit of the decision model first asked is
    passed to the small LLM by a configured decider, as any such decision is. `decider` is
    expected to be one that does so: an `InputTooLong` is a fault in the caller.

    Nothing the decider raises is caught.
    """
    return any(decider.decide(piece, INJECTION).top == YES for piece in _asked_about(text))


@dataclass(frozen=True)
class Reviewed:
    """What a Review found for one pull request, and the record of the Reviewer's run."""

    findings: tuple[ReviewFinding, ...]
    """The Findings, in the order `review_pull_request` gives them. Never more than
    `FINDING_CAP`."""

    run: RoleRun | None
    """The record of the Reviewer's run. None when the Reviewer was not run, or when the model
    failed during its run."""


def review_pull_request(
    claims: tuple[str, ...],
    *,
    title: str,
    description: str,
    linked_issue: str,
    evidence: tuple[Evidence, ...],
    repository: Path,
    before: str,
    after: str,
    model: Model,
    decider: Decider,
    step_cap: int = REVIEWER_STEP_CAP,
    decision_cap: int = DECISION_CAP,
) -> Reviewed:
    """Review one pull request and report Findings about it.

    `before` and `after` are the commits before and after the pull request, in `repository`.
    `title`, `description`, and `linked_issue` are its text, `claims` are the Claims that text
    makes, and `evidence` is whatever Evidence was gathered for them. Nothing is run here and
    nothing is changed. No Verdict is given to the Review, and it gives none.

    The files the pull request changes are the paths at which the two commits differ, as git
    lists them without pairing a file removed with a file added: a file added, a file removed,
    or a file changed. So a file moved is one removed and one added. Wherever they are taken
    in turn, it is in sorted order by path.

    The Review has two parts, done in this order.

    1. The injection detector
        `detect_injection(decider, text)` is asked about the texts below, in this order. Each
        of the five parts gives at most one Finding, of kind `SUSPECTED_INJECTION`, whose
        concern is `READS_AS_INSTRUCTIONS` and whose `part` is the one named. So the detector
        makes five Findings at most, however much of a pull request reads as instructions.

        - `title`: part `TITLE`.
        - `description`: part `DESCRIPTION`.
        - `linked_issue`: part `LINKED_ISSUE`.
        - For each file the pull request changes: the lines it adds to that file, which are
          the lines git's difference between the two commits marks as added, each without
          the mark and with a line feed between one and the next. They are read as text as
          `reading_tools` reads a file. Every line added is asked about, the comments in the
          diff among them: which lines are comments is not worked out. A file to which no
          line is added asks for no decision. Part `FILE`, with the `path` of the first
          file that is a detection: after it no other file is asked about.
        - For each `RunEvidence` in `evidence`, in order: its `before.output`, then its
          `after.output`. Part `SANDBOX_OUTPUT`. After the first output that is a detection
          no other output is asked about.

        Part 1 asks for `decision_cap` decisions at most, counted over all these texts. A
        text with more pieces than there are decisions left is not asked about, and part 1
        ends there.

    2. The Reviewer
        Not run when the pull request changes no file. Otherwise a role named `reviewer` is
        run with `run_role`, once. Its tools are the three of `reading_tools(repository,
        after)` and `read_change`, and no other: it reads, and has no tool that runs or
        writes anything. Its step cap is `step_cap`. Its task is
        `{"claims": [...], "changed_files": [...]}`: the Claims in the order given, and the
        paths of the files the pull request changes. So its context holds the Claims, those
        paths, and what it reads, and nothing else of the pull request. It is run with no
        progress guard, so the decider is asked for nothing in part 2.

        `read_change` takes `path`
            Returns what the pull request changes in that file, as git writes the
            difference between the two commits in unified form. Among its lines is each
            line the pull request adds, after a `+`, and each line it removes, after a
            `-`. Nothing more is settled here about what git writes around them.
            `NOT_CHANGED` when the path is not that of a file the pull request changes, and
            `BAD_ARGUMENTS` (from `pr_verification.reading`) as the read-only tools answer
            it. What it returns is Untrusted Content.

        The role finishes with its findings, the most important first:
        `{"findings": [{"kind": "undeclared_change", "path": "auth/session.py",
        "concern": "The session timeout is raised, and no Claim says so."}]}`.
        Each becomes one Finding with part `FILE` and that `kind`, `path`, and `concern`, the
        concern without the whitespace at its two ends. A finding is left out when:

        - it is not an object with exactly those three members;
        - its `kind` is not `quality` or `undeclared_change`, the two kinds that are the
          Reviewer's to report;
        - its `path` is not that of a file the pull request changes, so every Finding it
          makes points into the change;
        - its `concern` is not a string, is blank, or is longer than `LONGEST_CONCERN`
          characters once that whitespace is removed.

        An answer with no list under `findings` gives no Findings. A concern is the model's
        word, shaped by what the role read, and so Untrusted Content. Whatever else the
        answer holds is not read: nothing the Reviewer says becomes a Claim.

    A pull request with no Claims
        When `claims` is empty and the pull request changes at least one file, no Claim
        covers any of its changes, and one Finding says so in place of one for each change:
        kind `UNDECLARED_CHANGE`, part `WHOLE`, and concern `NOTHING_DECLARED` filled
        in. It is made by code, whatever comes of the Reviewer. Every `undeclared_change`
        the Reviewer names for such a pull request is left out.

    A Review that could not be finished
        It says so with a Finding of kind `UNFINISHED_REVIEW` and part `WHOLE`, whose concern
        is `REVIEW_UNFINISHED` filled in. Each part of the Review adds one at most.

        - When the decider raises `ModelFailed` in part 1, as a `DecisionFailed` is one,
          part 1 ends there: nothing more is asked of the decider, the detections made
          before it are kept, and one such Finding is added with what the exception said.
        - When part 1 ends at its cap on decisions, the same holds, and the Finding is added
          with `DECISION_CAP_REACHED` filled in.
        - When the model raises `ModelFailed` in part 2, the Reviewer gives no Findings and
          one such Finding is added with what the exception said.
        - When the Reviewer's run stops at the step cap, it gives no Findings and one such
          Finding is added with `STEP_CAP_REACHED` filled in.
        - When the Reviewer's run stops with `Stopped.LOOP`, the role having been stopped
          for going round in circles, it gives no Findings and one such Finding is added
          with `STOPPED_LOOPING` filled in for the run.

        Part 2 is done however part 1 ended.

    The order, and the cap
        `findings` holds first what code made: the detections of part 1 in the order of its
        parts, then its `UNFINISHED_REVIEW` if it has one, then the `NOTHING_DECLARED`
        Finding if there is one, then the `UNFINISHED_REVIEW` of part 2 if it has one. These
        are eight at most. After them comes what the Reviewer named, in the order it named
        them, and only as many as leave `findings` no more than `FINDING_CAP`: the rest are
        left out. The cap is enforced here, by code. So nothing code made is ever left out,
        and a Reviewer misled by what it read can neither fill a report nor push a
        detection out of it.

    Raises `ValueError` before any decision or request when `step_cap` or `decision_cap` is
    less than 1. An `InputTooLong` from the decider is not caught, as in `detect_injection`:
    it is a fault in the caller. Any other exception is a fault in the system and is raised.
    """
    if step_cap < 1 or decision_cap < 1:
        raise ValueError(
            f"the step cap is {step_cap} and the cap on decisions is {decision_cap}: "
            "neither may be under 1"
        )
    change = _Change(repository, before, after)
    outputs = (
        output
        for ran in evidence
        if isinstance(ran, RunEvidence)
        for output in (ran.before.output, ran.after.output)
    )
    made = _detections(
        decider,
        decision_cap,
        (
            (PullRequestPart.TITLE, [(None, title)]),
            (PullRequestPart.DESCRIPTION, [(None, description)]),
            (PullRequestPart.LINKED_ISSUE, [(None, linked_issue)]),
            (PullRequestPart.FILE, ((path, change.added(path)) for path in change.paths)),
            (PullRequestPart.SANDBOX_OUTPUT, ((None, output) for output in outputs)),
        ),
    )
    if not change.paths:
        return Reviewed(findings=tuple(made), run=None)
    reportable = [FindingKind.QUALITY, FindingKind.UNDECLARED_CHANGE]
    if not claims:
        # One Finding stands for every change, so the Reviewer's own are not wanted.
        reportable.remove(FindingKind.UNDECLARED_CHANGE)
        concern = NOTHING_DECLARED.format(files=len(change.paths))
        whole = PullRequestPart.WHOLE
        made.append(ReviewFinding(concern, kind=FindingKind.UNDECLARED_CHANGE, part=whole))

    tools = (*reading_tools(repository, after), Tool("read_change", _READ_CHANGE, change.read))
    role = Role("reviewer", _REVIEWER_INSTRUCTIONS, tools, step_cap)
    task = {"claims": list(claims), "changed_files": list(change.paths)}
    try:
        run = run_role(role, task, model)
    except ModelFailed as error:
        return Reviewed(findings=(*made, _unfinished(str(error))), run=None)
    if run.stopped is Stopped.STEP_CAP:
        made.append(_unfinished(STEP_CAP_REACHED.format(steps=step_cap)))
    named = _named(run.answer, change.paths, reportable)
    # Enforced here, whatever the Reviewer was told: what code made is kept whole.
    room = max(FINDING_CAP - len(made), 0)
    return Reviewed(findings=(*made, *named[:room]), run=run)


def _asked_about(text: str) -> tuple[str, ...]:
    """The pieces of `text` the injection detector asks about: none when the text is blank."""
    return pieces(text) if text.strip() else ()


def _unfinished(failure: str) -> ReviewFinding:
    """The Finding that says a Review could not be finished, and why."""
    return ReviewFinding(
        REVIEW_UNFINISHED.format(failure=failure),
        kind=FindingKind.UNFINISHED_REVIEW,
        part=PullRequestPart.WHOLE,
    )


class _Counted:
    """Asks a decider, and keeps count of the decisions asked for."""

    def __init__(self, decider: Decider) -> None:
        self._decider = decider
        self.asked = 0

    def decide(self, state: str, question: Question) -> Decision:
        self.asked += 1
        return self._decider.decide(state, question)


def _detections(
    decider: Decider,
    decision_cap: int,
    parts: Iterable[tuple[PullRequestPart, Iterable[tuple[str | None, str]]]],
) -> list[ReviewFinding]:
    """Part 1 of a Review: the Findings of the injection detector, and the one that says it
    could not finish when it could not.

    Each of `parts` has its texts in the order to ask about them, each with the path of its
    file if it is from one. A text is taken only when the ones before it have been asked about.
    """
    counted = _Counted(decider)
    findings: list[ReviewFinding] = []
    for part, texts in parts:
        for path, text in texts:
            if len(_asked_about(text)) > decision_cap - counted.asked:
                failure = DECISION_CAP_REACHED.format(decisions=decision_cap)
                return [*findings, _unfinished(failure)]
            try:
                detected = detect_injection(counted, text)
            except ModelFailed as error:
                return [*findings, _unfinished(str(error))]
            if detected:
                kind = FindingKind.SUSPECTED_INJECTION
                findings.append(
                    ReviewFinding(READS_AS_INSTRUCTIONS, kind=kind, part=part, path=path)
                )
                break
    return findings


class _Change:
    """What a pull request changes: git's difference between the commits before and after it.

    `paths` holds the path of every file it changes, in sorted order. A file is reached only
    by finding its path among them, so nothing a model wrote is handed to git as a path.
    """

    def __init__(self, repository: Path, before: str, after: str) -> None:
        self._repository = repository
        self._commits = (before, after)
        listing = os.fsdecode(self._difference("--name-only", "-z"))
        self.paths = tuple(sorted(path for path in listing.split("\0") if path))

    def _difference(self, *options: str, only: Sequence[str] = ()) -> bytes:
        """What git writes of the difference between the two commits: in every file, or in the
        files `only` names when it names any."""
        asked = (*_DIFFERENCE, *options, "--end-of-options", *self._commits, "--", *only)
        return git(self._repository, *asked)

    def difference(self, path: str) -> str:
        """The difference in the file at `path`, which is one of `paths`, in unified form and
        read as text as `reading_tools` reads a file."""
        # A path alone also names every file under a directory of that name, and there is one
        # when the pull request puts a directory where a file was.
        only = (f":(literal){path}", f":(exclude,literal){path}/")
        return self._difference(only=only).decode(errors="replace")

    def added(self, path: str) -> str:
        """The lines the pull request adds to the file at `path`, which is one of `paths`."""
        added = []
        in_hunk = False
        # Only a line feed ends a line here. Within a hunk every line starts with a mark, so a
        # line added is never taken for a header, nor a header for a line added.
        for line in self.difference(path).split("\n"):
            if line.startswith("diff --git "):
                in_hunk = False
            elif line.startswith("@@"):
                in_hunk = True
            elif in_hunk and line.startswith("+"):
                added.append(line[1:])
        return "\n".join(added)

    def read(self, arguments: Mapping[str, object]) -> str:
        """The tool `read_change`."""
        given = usable_arguments(arguments, needed=("path",))
        if given is None:
            return BAD_ARGUMENTS
        return self.difference(given["path"]) if given["path"] in self.paths else NOT_CHANGED


def _named(
    answer: Mapping[str, object] | None, paths: Collection[str], kinds: Collection[FindingKind]
) -> list[ReviewFinding]:
    """The Findings the Reviewer named in its answer, in its order: those of `kinds` that point
    at one of `paths`. None when it gave no answer."""
    findings = answer.get("findings") if answer is not None else None
    if not isinstance(findings, list):
        return []
    read = (_read_finding(finding, paths, kinds) for finding in findings)
    return [finding for finding in read if finding is not None]


def _read_finding(
    said: object, paths: Collection[str], kinds: Collection[FindingKind]
) -> ReviewFinding | None:
    """The Finding made of one the Reviewer named, or None when it is to be left out."""
    if not (isinstance(said, dict) and said.keys() == {"kind", "path", "concern"}):
        return None
    kind, path, concern = said["kind"], said["path"], said["concern"]
    if not (isinstance(kind, str) and kind in kinds):
        return None
    if not (isinstance(path, str) and path in paths):
        return None
    if not isinstance(concern, str):
        return None
    concern = concern.strip()
    if not 0 < len(concern) <= LONGEST_CONCERN:
        return None
    return ReviewFinding(concern, kind=FindingKind(kind), part=PullRequestPart.FILE, path=path)
