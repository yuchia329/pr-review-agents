"""The pull request comment: the structured report, written out for a person to read.

Rendering is code and reads nothing but the report. No model writes any of the comment, and no
model posts anything (ADR 0002): wording that a model wrote earlier in the run, such as a
Claim's text or a Reproduction, reaches the comment only as the report holds it, and only as a
quote.

This is delivery, not core: of the package, only the GitHub wrapper imports this module.
"""

from collections.abc import Mapping

from pr_verification.report import EvidenceSource, Report

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
    raise NotImplementedError


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
    raise NotImplementedError
