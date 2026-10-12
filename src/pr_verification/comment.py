"""The pull request comment: the structured report, written out for a person to read.

Rendering is code and reads nothing but the report. No model writes any of the comment, and no
model posts anything (ADR 0002): wording that a model wrote earlier in the run, such as a
Claim's text or a Reproduction, reaches the comment only as the report holds it, and only as a
quote.

This is delivery, not core: of the package, only the GitHub wrapper imports this module.
"""

import string
from collections.abc import Iterator, Mapping

from pr_verification.report import (
    Claim,
    EvidenceSource,
    Report,
    ReproductionEvidence,
    Verdict,
)

MARKER = "<!-- pr-verification -->"
"""The first line of every comment. GitHub does not show it. The comment is found again by it,
so that a pull request has one comment from this system and not one for each run."""

LONGEST_COMMENT = 65_536
"""The most characters GitHub takes in one comment."""

LONGEST_QUOTE = 500
"""The most characters quoted of a text that sits within a line."""

BLOCK_BUDGET = 9_000
"""The most characters quoted of all the Reproductions and outputs of one comment together.
Each of them is quoted up to an equal share of it."""

CUT_SHORT = " [cut short]"
"""What follows a quote that holds only part of its text."""

HEADING = "PR Verification"
"""The comment's heading."""

GATE_LINE = "Gate: {decision}. {note}"
"""The Gate. `decision` is `pass` or `block`, and `note` is the Gate's note."""

INCONCLUSIVE_LINE = (
    "Inconclusive Verdicts: {count}. An Inconclusive Claim was neither confirmed nor refuted."
)
"""How many Claims are Inconclusive, so that a Gate that passes is not read as every Claim
being Confirmed. It is there whatever the count, 0 included."""

CLAIM_LINE = "{verdict}: {claim}"
"""One Claim. `verdict` is `Confirmed`, `Refuted`, or `Inconclusive`, and `claim` quotes the
Claim's text."""

REASON_LINE = "Reason: {reason}"
"""Why a Claim has its Verdict. `reason` quotes the Claim's reason."""

EVIDENCE_LINE = "Evidence from {source}: {observation}"
"""One piece of Evidence. `source` is the `SOURCE_WORDING` of the Evidence's source, and
`observation` quotes its observation."""

SOURCE_WORDING: Mapping[EvidenceSource, str] = {
    EvidenceSource.RAN_CODE: "running code",
    EvidenceSource.READ_CODE: "reading code",
}
"""How `EVIDENCE_LINE` says whether a piece of Evidence came from running code or reading it."""

NO_EVIDENCE_LINE = "No Evidence was gathered for this Claim."
"""What stands in place of the Evidence of a Claim that has none."""

REPRODUCTION_LINE = "Reproduction, placed at {path}:"
"""What the text of a Reproduction follows. `path` quotes where it was placed."""

BEFORE_LINE = "Its output on the code before the pull request:"
"""What the output of a Reproduction's run against the code before the pull request follows."""

AFTER_LINE = "Its output on the code after the pull request:"
"""What the output of a Reproduction's run against the code after the pull request follows."""

FINDINGS_LINE = "Findings are advisory: they never affect the Gate."
"""What the Findings follow. It is there whether or not there are any."""

NO_FINDINGS_LINE = "No Findings."
"""What stands in place of the Findings when there are none."""

SPEND_LINE = "Time: {seconds:.1f} seconds. Cost: ${dollars:.2f}. Steps: {steps}."
"""The time, the money, and the steps the run spent, filled in from the report's
`budget_spend`."""

TOO_LONG_LINE = (
    "The report is too long to show here. It is printed whole in the log of the job that "
    "verified this pull request."
)
"""What stands in place of the Claims and the Findings in the short form of the comment."""


def quote(text: str) -> str:
    """`text`, written so that nothing in it can alter the formatting of a comment.

    Each character of `text` is written as one unit, in order:

    - as the numeric character reference `&#<its code point in decimal>;`, when it is an ASCII
      punctuation mark, which is any character of `string.punctuation`, or a tab, a line
      feed, a form feed, or a carriage return;
    - as U+FFFD, the replacement character, when it is any other ASCII control character, a
      surrogate, or a control of the direction text runs in: U+061C, U+200E, U+200F, U+202A to
      U+202E, or U+2066 to U+2069. A comment cannot show the first two, and the last can
      change how the text after it is drawn;
    - as itself otherwise: an ASCII letter, an ASCII digit, a space, or any other character
      beyond U+007F.

    So a text of n characters gives n units, none longer than 6 characters. Outside its
    references the result holds no ASCII punctuation mark and no ASCII control character,
    and so none of the characters that HTML or Markdown gives a meaning to, and neither of
    the two that end a line there. `html.unescape` of the result is `text` with each
    character of the second kind replaced by U+FFFD.
    """
    return text.translate(_QUOTED)


_ASCII_CONTROLS = (*range(0x20), 0x7F)
_SURROGATES = range(0xD800, 0xE000)
_DIRECTION_CONTROLS = (0x061C, 0x200E, 0x200F, *range(0x202A, 0x202F), *range(0x2066, 0x206A))

_QUOTED: Mapping[int, str] = {
    **dict.fromkeys((*_ASCII_CONTROLS, *_SURROGATES, *_DIRECTION_CONTROLS), chr(0xFFFD)),
    **{ord(mark): f"&#{ord(mark)};" for mark in string.punctuation + "\t\n\f\r"},
}
"""What `quote` writes for each character that it does not write as itself, by code point."""


def render_comment(report: Report) -> str:
    """The text of the pull request comment for `report`.

    A pure function of `report`: nothing else is read, no model is asked, and the same report
    always gives the same comment.

    The comment is HTML, and its lines end with a line feed. Its first line is `MARKER`, its
    second is `<h2>`, `HEADING`, `</h2>`, and no line of it is empty or holds only spaces and
    tabs, so that GitHub reads all of it as HTML and none of it as Markdown. It holds these
    lines, in this order:

    1. `GATE_LINE`, with the `decision` and the `note` of `report.gate`.
    2. `INCONCLUSIVE_LINE`, with the number of Claims whose Verdict is Inconclusive.
    3. For each Claim, in the report's order:

       - `CLAIM_LINE`, with the Verdict's value with a capital first letter, as `compute_gate`
         writes it, and the Claim's text;
       - `REASON_LINE`, with the Claim's reason;
       - for each piece of its Evidence, in order, `EVIDENCE_LINE` with its source and its
         observation, or `NO_EVIDENCE_LINE` when it has none;
       - straight after the `EVIDENCE_LINE` of each `ReproductionEvidence`:
         `REPRODUCTION_LINE` with its `path`, then its `reproduction`; `BEFORE_LINE`, then
         its `before.output`; and `AFTER_LINE`, then its `after.output`.

    4. `FINDINGS_LINE`, then each Finding's `concern`, in order, or `NO_FINDINGS_LINE`.
    5. `SPEND_LINE`, with `report.budget_spend`.

    So every Claim is shown with its Verdict, its reason, and whether each piece of its
    Evidence came from running code or reading it, and every Reproduction is shown with its
    output on both commits, whatever the Verdict: for a Refuted Verdict, that is what refuted
    the Claim.

    Each line named is in the comment exactly as its constant is written, once filled in.
    Markup may stand around a line. None stands inside one but the markup of a quote.

    Quotes
        The text of a Claim, a reason, an observation, a path, a concern, a Reproduction, and
        an output are each treated as Untrusted Content, whoever wrote them. Each is in the
        comment only as a quote:

        - a Claim's text, a reason, an observation, a path, or a concern is a quote within a
          line: `<code>`, `quote` of the text, `</code>`. It is what fills the field of its
          line;
        - a Reproduction or an output is a block: `<pre>`, `quote` of the text, `</pre>`.

        A text longer than what is quoted of it is cut by its length alone. Of a text within a
        line, the first `LONGEST_QUOTE` characters are quoted. Of a block, as many characters
        as `BLOCK_BUDGET` divided by the number of blocks in the comment, rounded down: the
        first of a Reproduction, and the last of an output, which is where a failing test
        says why it failed. `CUT_SHORT` comes straight after the closing tag of a quote that
        was cut, and with a quote within a line it fills the field together with the quote.

        Everything else in the comment is written by the project, or is a number or a note
        that code computed.

    Structure
        The structure of a comment is what is left of it once every numeric character
        reference is taken out, and then every ASCII letter, ASCII digit, space, and character
        beyond U+007F. Whatever a quoted text holds, the comment has the same structure as it
        has with any other text of the same length in that text's place, unless either of the
        two is the short form.

    The short form
        When the comment described above would be longer than `LONGEST_COMMENT` characters,
        `TOO_LONG_LINE` takes the place of parts 3 and 4, and the rest is as described. A
        comment is never longer than `LONGEST_COMMENT` characters. The quotes of a comment's
        blocks never come to more than six times `BLOCK_BUDGET` characters between them, so
        it is the number of Claims, Evidence, and Findings that brings the short form about,
        and not what a Reproduction holds or prints.
    """
    whole = _comment(report, [*_claims(report), *_findings(report)])
    if len(whole) <= LONGEST_COMMENT:
        return whole
    return _comment(report, [_paragraph(TOO_LONG_LINE)])


def _comment(report: Report, middle: list[str]) -> str:
    """A comment for `report` whose lines between the count of Inconclusive Verdicts and the
    spend are `middle`."""
    gate, spend = report.gate, report.budget_spend
    inconclusive = sum(claim.verdict is Verdict.INCONCLUSIVE for claim in report.claims)
    lines = [
        MARKER,
        f"<h2>{HEADING}</h2>",
        _paragraph(
            f"<strong>{GATE_LINE.format(decision=gate.decision.value, note=gate.note)}</strong>"
        ),
        _paragraph(INCONCLUSIVE_LINE.format(count=inconclusive)),
        *middle,
        _paragraph(
            SPEND_LINE.format(seconds=spend.seconds, dollars=spend.dollars, steps=spend.steps)
        ),
    ]
    return "".join(line + "\n" for line in lines)


def _claims(report: Report) -> Iterator[str]:
    """The lines of part 3: each Claim with its Verdict, its reason, and its Evidence."""
    blocks = 3 * sum(
        isinstance(evidence, ReproductionEvidence)
        for claim in report.claims
        for evidence in claim.evidence
    )
    share = BLOCK_BUDGET // blocks if blocks else 0
    if report.claims:
        yield "<ul>"
    for claim in report.claims:
        yield "<li>"
        yield from _claim(claim, share)
        yield "</li>"
    if report.claims:
        yield "</ul>"


def _claim(claim: Claim, share: int) -> Iterator[str]:
    """The lines of one Claim. `share` is how many characters are quoted of each block."""
    stated = CLAIM_LINE.format(verdict=claim.verdict.capitalize(), claim=_inline(claim.text))
    yield _paragraph(f"<strong>{stated}</strong>")
    yield _paragraph(REASON_LINE.format(reason=_inline(claim.reason)))
    if not claim.evidence:
        yield _paragraph(NO_EVIDENCE_LINE)
    for evidence in claim.evidence:
        source = SOURCE_WORDING[evidence.source]
        observation = _inline(evidence.observation)
        yield _paragraph(EVIDENCE_LINE.format(source=source, observation=observation))
        if isinstance(evidence, ReproductionEvidence):
            reproduction = evidence.reproduction
            yield _paragraph(REPRODUCTION_LINE.format(path=_inline(evidence.path)))
            yield _block(reproduction[:share], cut=len(reproduction) > share)
            yield _paragraph(BEFORE_LINE)
            yield _end_of(evidence.before.output, share)
            yield _paragraph(AFTER_LINE)
            yield _end_of(evidence.after.output, share)


def _findings(report: Report) -> Iterator[str]:
    """The lines of part 4: the Findings, marked as advisory."""
    yield _paragraph(FINDINGS_LINE)
    if not report.findings:
        yield _paragraph(NO_FINDINGS_LINE)
        return
    yield "<ul>"
    for finding in report.findings:
        yield f"<li>{_inline(finding.concern)}</li>"
    yield "</ul>"


def _paragraph(line: str) -> str:
    return f"<p>{line}</p>"


def _inline(text: str) -> str:
    """A quote within a line, of no more than the first `LONGEST_QUOTE` characters of `text`."""
    quoted = f"<code>{quote(text[:LONGEST_QUOTE])}</code>"
    return quoted + CUT_SHORT if len(text) > LONGEST_QUOTE else quoted


def _end_of(output: str, share: int) -> str:
    """A block that quotes the last `share` characters of `output`."""
    return _block(output[max(len(output) - share, 0) :], cut=len(output) > share)


def _block(text: str, *, cut: bool) -> str:
    """A block that quotes `text`, which is all of a text or the part of it that is quoted."""
    quoted = f"<pre>{quote(text)}</pre>"
    return quoted + CUT_SHORT if cut else quoted
