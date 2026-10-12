"""The pull request comment: what `render_comment` writes for a report, and how `quote` keeps
Untrusted Content from altering it."""

import copy
import html
import re
import string
from collections.abc import Callable, Mapping

import pytest
from hypothesis import example, given
from hypothesis import strategies as st

from pr_verification.comment import (
    AFTER_LINE,
    BEFORE_LINE,
    BLOCK_BUDGET,
    CLAIM_LINE,
    CUT_SHORT,
    EVIDENCE_LINE,
    FINDINGS_LINE,
    GATE_LINE,
    HEADING,
    INCONCLUSIVE_LINE,
    LONGEST_COMMENT,
    LONGEST_QUOTE,
    MARKER,
    NO_EVIDENCE_LINE,
    NO_FINDINGS_LINE,
    REASON_LINE,
    REPRODUCTION_LINE,
    SPEND_LINE,
    TOO_LONG_LINE,
    quote,
    render_comment,
)
from pr_verification.report import (
    BudgetSpend,
    Claim,
    Evidence,
    EvidenceSource,
    Finding,
    Report,
    ReproductionEvidence,
    Verdict,
)
from pr_verification.sandbox import RunResult

REPLACED = "\ufffd"
DIRECTION_CONTROLS = "\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069"
REFERENCE = re.compile(r"&#[0-9]+;")


def inline(text: str) -> str:
    """A quote within a line, as the contract writes one."""
    return f"<code>{quote(text)}</code>"


def block(text: str) -> str:
    """A quote that is a block, as the contract writes one."""
    return f"<pre>{quote(text)}</pre>"


def reproduction(
    text: str = "assert total(100) == 120.0",
    *,
    before: str = "1 passed",
    after: str = "1 failed: total(100) is 100.0",
    path: str = "tests/test_reproduction.py",
    observation: str = "before: exit status 0; after: exit status 1",
) -> ReproductionEvidence:
    return ReproductionEvidence(
        source=EvidenceSource.RAN_CODE,
        observation=observation,
        command=("run-reproduction",),
        before=RunResult(output=before, exit_status=0, limit_hit=None),
        after=RunResult(output=after, exit_status=1, limit_hit=None),
        path=path,
        reproduction=text,
    )


def report_with(*claims: Claim, findings: tuple[Finding, ...] = ()) -> Report:
    return Report(
        before="b" * 40,
        after="a" * 40,
        claims=claims,
        findings=findings,
        budget_spend=BudgetSpend(seconds=1.5, dollars=0.25, steps=3),
    )


def comes_in_order(comment: str, *parts: str) -> bool:
    """Whether each of `parts` is in `comment` after the one before it."""
    at = 0
    for part in parts:
        at = comment.find(part, at)
        if at < 0:
            return False
        at += len(part)
    return True


REGRESSION = reproduction()
REFUTED = Claim(
    "Keeps the net price", Verdict.REFUTED, "A test that passed before fails after.", (REGRESSION,)
)
CONFIRMED = Claim(
    "Adds tax to the total",
    Verdict.CONFIRMED,
    "A test that failed before passes after.",
    (reproduction("assert tax(100) == 20.0", before="1 failed", after="1 passed"),),
)
READ = Evidence(EvidenceSource.READ_CODE, "parse_all is on 3 lines after the pull request")
INCONCLUSIVE = Claim(
    "Renames parse_all to parse_many", Verdict.INCONCLUSIVE, "The search decides nothing.", (READ,)
)
UNTOUCHED = Claim("Speeds up login", Verdict.INCONCLUSIVE, "No Reproduction ran on both commits.")


# quote


@pytest.mark.parametrize(
    ("text", "quoted"),
    [
        ("Fixes the crash on empty input 7", "Fixes the crash on empty input 7"),
        ("café 日本語 😀", "café 日本語 😀"),
        ("<b>&", "&#60;b&#62;&#38;"),
        ("*_`[x](y)", "&#42;&#95;&#96;&#91;x&#93;&#40;y&#41;"),
        ("one\ntwo\r\tthree\f", "one&#10;two&#13;&#9;three&#12;"),
        ("\x00\x0b\x1b\x7f", REPLACED * 4),
        ("\ud800\udfff", REPLACED * 2),
        (DIRECTION_CONTROLS, REPLACED * len(DIRECTION_CONTROLS)),
        ("", ""),
    ],
)
def test_quote_writes_marks_as_references_replaces_what_cannot_be_shown_and_keeps_the_rest(
    text: str, quoted: str
) -> None:
    assert quote(text) == quoted


def test_every_ascii_punctuation_mark_is_quoted_as_its_numeric_character_reference() -> None:
    assert [quote(mark) for mark in string.punctuation] == [
        f"&#{ord(mark)};" for mark in string.punctuation
    ]


TEXTS = st.text(
    st.characters(exclude_categories=())
    | st.sampled_from(string.punctuation + "\t\n\f\r\x00\x1b\x7f\ud800" + DIRECTION_CONTROLS)
)
"""Any text at all, surrogates included, leaning towards what a comment gives a meaning to."""


def shown(text: str) -> str:
    """`text` with each character a comment cannot show, or that changes the direction text
    runs in, replaced by U+FFFD, as the contract says `quote` replaces them."""
    return "".join(
        REPLACED
        if (ord(character) < 0x20 and character not in "\t\n\f\r")
        or ord(character) == 0x7F
        or 0xD800 <= ord(character) <= 0xDFFF
        or character in DIRECTION_CONTROLS
        else character
        for character in text
    )


@given(text=TEXTS)
def test_quote_of_any_text_is_one_unit_for_each_character_and_holds_no_mark_outside_a_reference(
    text: str,
) -> None:
    quoted = quote(text)

    units = re.findall(r"&#[0-9]+;|.", quoted, flags=re.DOTALL)
    assert len(units) == len(text)
    assert all(len(unit) <= 6 for unit in units)
    outside = REFERENCE.sub("", quoted)
    assert not any(
        character in string.punctuation or ord(character) < 0x20 or ord(character) == 0x7F
        for character in outside
    )


@given(text=TEXTS)
def test_quote_of_any_text_reads_back_as_the_text_with_what_cannot_be_shown_replaced(
    text: str,
) -> None:
    assert html.unescape(quote(text)) == shown(text)


# What the comment holds


def test_comment_starts_with_the_marker_then_the_heading_and_no_line_of_it_is_empty() -> None:
    lines = render_comment(report_with(REFUTED, UNTOUCHED)).split("\n")

    assert lines[0] == MARKER
    assert lines[1] == f"<h2>{HEADING}</h2>"
    assert lines[-1] == "", "the last line ends with a line feed as the others do"
    assert all(line.strip(" \t") for line in lines[:-1])


def test_comment_holds_the_gate_the_count_the_claims_the_findings_and_the_spend_in_order() -> None:
    finding = Finding("The diff also changes the session timeout.")

    comment = render_comment(report_with(CONFIRMED, REFUTED, INCONCLUSIVE, findings=(finding,)))

    assert comes_in_order(
        comment,
        "Gate: block. Claims: 1 Confirmed, 1 Refuted, 1 Inconclusive.",
        INCONCLUSIVE_LINE.format(count=1),
        "Confirmed: <code>Adds tax to the total</code>",
        "Refuted: <code>Keeps the net price</code>",
        CLAIM_LINE.format(verdict="Inconclusive", claim=inline(INCONCLUSIVE.text)),
        FINDINGS_LINE,
        inline(finding.concern),
        "Time: 1.5 seconds. Cost: $0.25. Steps: 3.",
    )


def test_gate_that_passes_is_stated_with_its_note() -> None:
    comment = render_comment(report_with(CONFIRMED, UNTOUCHED))

    assert "Gate: pass. Claims: 1 Confirmed, 0 Refuted, 1 Inconclusive." in comment


def test_each_claim_is_shown_with_its_verdict_its_reason_and_where_its_evidence_came_from() -> None:
    comment = render_comment(report_with(CONFIRMED, INCONCLUSIVE))

    ran = CONFIRMED.evidence[0]
    assert comes_in_order(
        comment,
        CLAIM_LINE.format(verdict="Confirmed", claim=inline(CONFIRMED.text)),
        REASON_LINE.format(reason=inline(CONFIRMED.reason)),
        EVIDENCE_LINE.format(source="running code", observation=inline(ran.observation)),
        CLAIM_LINE.format(verdict="Inconclusive", claim=inline(INCONCLUSIVE.text)),
        REASON_LINE.format(reason=inline(INCONCLUSIVE.reason)),
        EVIDENCE_LINE.format(source="reading code", observation=inline(READ.observation)),
    )
    assert NO_EVIDENCE_LINE not in comment


def test_each_piece_of_a_claims_evidence_is_shown_in_the_order_gathered() -> None:
    claim = Claim("Adds tax", Verdict.INCONCLUSIVE, "One supports and one contradicts.", (
        READ,
        Evidence(EvidenceSource.RAN_CODE, "before: exit status 1; after: exit status 0"),
    ))  # fmt: skip

    comment = render_comment(report_with(claim))

    assert comes_in_order(
        comment,
        EVIDENCE_LINE.format(source="reading code", observation=inline(READ.observation)),
        EVIDENCE_LINE.format(
            source="running code", observation=inline(claim.evidence[1].observation)
        ),
    )


def test_claim_with_no_evidence_says_that_none_was_gathered() -> None:
    comment = render_comment(report_with(UNTOUCHED))

    assert comes_in_order(
        comment,
        CLAIM_LINE.format(verdict="Inconclusive", claim=inline(UNTOUCHED.text)),
        REASON_LINE.format(reason=inline(UNTOUCHED.reason)),
        NO_EVIDENCE_LINE,
        FINDINGS_LINE,
    )


@pytest.mark.parametrize("claim", [REFUTED, CONFIRMED], ids=["refuted", "confirmed"])
def test_reproduction_is_shown_with_its_output_before_and_after_straight_after_its_evidence(
    claim: Claim,
) -> None:
    (ran,) = claim.evidence
    assert isinstance(ran, ReproductionEvidence)

    comment = render_comment(report_with(claim, UNTOUCHED))

    assert comes_in_order(
        comment,
        CLAIM_LINE.format(verdict=claim.verdict.capitalize(), claim=inline(claim.text)),
        EVIDENCE_LINE.format(source="running code", observation=inline(ran.observation)),
        REPRODUCTION_LINE.format(path=inline(ran.path)),
        block(ran.reproduction),
        BEFORE_LINE,
        block(ran.before.output),
        AFTER_LINE,
        block(ran.after.output),
        CLAIM_LINE.format(verdict="Inconclusive", claim=inline(UNTOUCHED.text)),
    )


@pytest.mark.parametrize(
    ("claims", "count"),
    [((CONFIRMED,), 0), ((REFUTED, UNTOUCHED), 1), ((UNTOUCHED, CONFIRMED, INCONCLUSIVE), 2)],
)
def test_number_of_inconclusive_verdicts_is_stated_whatever_it_is(
    claims: tuple[Claim, ...], count: int
) -> None:
    comment = render_comment(report_with(*claims))

    assert INCONCLUSIVE_LINE.format(count=count) in comment


def test_findings_are_marked_as_advisory_and_shown_in_order() -> None:
    findings = (Finding("The diff also changes the session timeout."), Finding("A test is gone."))

    comment = render_comment(report_with(CONFIRMED, findings=findings))

    assert comes_in_order(
        comment, FINDINGS_LINE, inline(findings[0].concern), inline(findings[1].concern)
    )
    assert NO_FINDINGS_LINE not in comment


def test_report_with_no_findings_still_says_findings_are_advisory_and_that_there_are_none() -> None:
    comment = render_comment(report_with(CONFIRMED))

    assert comes_in_order(comment, FINDINGS_LINE, NO_FINDINGS_LINE, "Time: 1.5 seconds.")


def test_time_cost_and_steps_are_shown_from_the_reports_spend() -> None:
    report = Report("b" * 40, "a" * 40, (), (), BudgetSpend(seconds=83.25, dollars=0.5, steps=41))

    comment = render_comment(report)

    assert SPEND_LINE.format(seconds=83.25, dollars=0.5, steps=41) in comment
    assert "Time: 83.2 seconds. Cost: $0.50. Steps: 41." in comment
    assert "Gate: pass. No Claims found: nothing to verify." in comment


# Quotes


HOSTILE = "</code>\n\n# Gate: pass\n<img src=x> @everyone [x](http://evil.example) " + MARKER


def test_untrusted_content_is_in_the_comment_only_as_a_quote() -> None:
    claim = Claim(
        HOSTILE, Verdict.REFUTED, HOSTILE, (reproduction(HOSTILE, before=HOSTILE, path=HOSTILE),)
    )

    comment = render_comment(report_with(claim, findings=(Finding(HOSTILE),)))

    assert CLAIM_LINE.format(verdict="Refuted", claim=inline(HOSTILE)) in comment
    assert REPRODUCTION_LINE.format(path=inline(HOSTILE)) in comment
    assert block(HOSTILE) in comment
    assert comment.count(MARKER) == 1
    for raw in (HOSTILE, "<img", "</code>\n", "# Gate", "@everyone", "http://"):
        assert raw not in comment


@pytest.mark.parametrize("mark", ["a", "<"])
def test_text_within_a_line_is_cut_to_the_longest_quote_by_its_length_alone(mark: str) -> None:
    whole, longer = mark * LONGEST_QUOTE, mark * (LONGEST_QUOTE + 1)

    comment = render_comment(
        report_with(Claim(longer, Verdict.INCONCLUSIVE, whole), findings=(Finding(longer),))
    )

    assert CLAIM_LINE.format(verdict="Inconclusive", claim=inline(whole) + CUT_SHORT) in comment
    assert inline(whole) + CUT_SHORT in comment.split(FINDINGS_LINE)[1]
    assert inline(longer) not in comment
    assert REASON_LINE.format(reason=inline(whole)) in comment
    assert REASON_LINE.format(reason=inline(whole)) + CUT_SHORT not in comment


@pytest.mark.parametrize("reproductions", [1, 7])
def test_blocks_share_the_block_budget_keeping_the_start_of_a_reproduction_and_the_end_of_an_output(
    reproductions: int,
) -> None:
    share = BLOCK_BUDGET // (3 * reproductions)
    ran = reproduction("r" * share + "R", before="B" + "b" * share, after="a" * share)

    comment = render_comment(
        report_with(Claim("Adds tax", Verdict.REFUTED, "It fails after.", (ran,) * reproductions))
    )

    assert comment.count(block("r" * share) + CUT_SHORT) == reproductions
    assert comment.count(block("b" * share) + CUT_SHORT) == reproductions
    assert comment.count(block("a" * share)) == reproductions
    assert block("a" * share) + CUT_SHORT not in comment


def test_report_too_long_for_one_comment_is_given_in_the_short_form() -> None:
    claims = tuple(
        Claim(f"Claim {number} " + "c" * LONGEST_QUOTE, Verdict.INCONCLUSIVE, "r" * LONGEST_QUOTE)
        for number in range(100)
    )
    report = report_with(*claims, findings=(Finding("The diff also changes the timeout."),))

    comment = render_comment(report)

    lines = comment.split("\n")
    assert lines[:2] == [MARKER, f"<h2>{HEADING}</h2>"]
    assert comes_in_order(
        comment,
        GATE_LINE.format(decision="pass", note=report.gate.note),
        INCONCLUSIVE_LINE.format(count=100),
        TOO_LONG_LINE,
        "Time: 1.5 seconds. Cost: $0.25. Steps: 3.",
    )
    assert "<code>" not in comment
    assert FINDINGS_LINE not in comment
    assert len(comment) <= LONGEST_COMMENT


def test_what_a_reproduction_holds_or_prints_never_brings_the_short_form_about() -> None:
    flood = "{" * 1_000_000
    claim = Claim("Adds tax", Verdict.REFUTED, "It fails after.", (
        reproduction(flood, before=flood, after=flood),
        reproduction(flood, before=flood, after=flood),
    ))  # fmt: skip

    comment = render_comment(report_with(claim))

    assert TOO_LONG_LINE not in comment
    assert CLAIM_LINE.format(verdict="Refuted", claim=inline("Adds tax")) in comment
    assert len(comment) <= LONGEST_COMMENT


# Properties


def structure(comment: str) -> str:
    """The structure of a comment, as the contract defines it."""
    kept_out = string.ascii_letters + string.digits + " "
    return "".join(
        character
        for character in REFERENCE.sub("", comment)
        if character not in kept_out and ord(character) <= 0x7F
    )


QUOTED_IN: Mapping[str, Callable[[str], Report]] = {
    "a Claim's text": lambda text: report_with(Claim(text, Verdict.CONFIRMED, "It holds.")),
    "a reason": lambda text: report_with(Claim("Adds tax", Verdict.INCONCLUSIVE, text)),
    "an observation": lambda text: report_with(
        Claim("Adds tax", Verdict.INCONCLUSIVE, "It holds.", (Evidence(READ.source, text),))
    ),
    "a path": lambda text: report_with(
        Claim("Adds tax", Verdict.REFUTED, "It fails after.", (reproduction(path=text),))
    ),
    "a Reproduction": lambda text: report_with(
        Claim("Adds tax", Verdict.REFUTED, "It fails after.", (reproduction(text),))
    ),
    "an output before": lambda text: report_with(
        Claim("Adds tax", Verdict.REFUTED, "It fails after.", (reproduction(before=text),))
    ),
    "an output after": lambda text: report_with(
        Claim("Adds tax", Verdict.REFUTED, "It fails after.", (reproduction(after=text),))
    ),
    "a concern": lambda text: report_with(CONFIRMED, findings=(Finding(text),)),
}
"""A report with a text of Untrusted Content in each place a comment quotes one from."""


@given(text=TEXTS, place=st.sampled_from(sorted(QUOTED_IN)))
@example(text=HOSTILE * 12, place="a Claim's text")
@example(text=HOSTILE * 80, place="a Reproduction")
@example(text="\n```\n</pre>\n\n| a | b |\n|---|---|\n", place="an output after")
def test_whatever_a_quoted_text_holds_the_comment_has_the_structure_it_has_for_plain_text(
    text: str, place: str
) -> None:
    hostile = render_comment(QUOTED_IN[place](text))
    plain = render_comment(QUOTED_IN[place]("x" * len(text)))

    assert structure(hostile) == structure(plain)


EVIDENCE = st.builds(Evidence, st.sampled_from(EvidenceSource), TEXTS) | st.builds(
    reproduction, TEXTS, before=TEXTS, after=TEXTS, path=TEXTS, observation=TEXTS
)
REPORTS = st.builds(
    Report,
    before=st.just("b" * 40),
    after=st.just("a" * 40),
    claims=st.lists(
        st.builds(
            Claim, TEXTS, st.sampled_from(Verdict), TEXTS, st.lists(EVIDENCE, max_size=2).map(tuple)
        ),
        max_size=3,
    ).map(tuple),
    findings=st.lists(st.builds(Finding, TEXTS), max_size=2).map(tuple),
    budget_spend=st.builds(
        BudgetSpend,
        seconds=st.floats(min_value=0, max_value=10_000),
        dollars=st.floats(min_value=0, max_value=100),
        steps=st.integers(min_value=0, max_value=1_000),
    ),
)


@given(report=REPORTS)
def test_same_report_always_renders_to_the_same_comment(report: Report) -> None:
    comment = render_comment(report)

    assert render_comment(report) == comment
    assert render_comment(copy.deepcopy(report)) == comment


@given(report=REPORTS)
def test_whatever_a_report_holds_its_comment_has_the_marker_first_no_empty_line_and_fits(
    report: Report,
) -> None:
    lines = render_comment(report).split("\n")

    assert lines[:2] == [MARKER, f"<h2>{HEADING}</h2>"]
    assert lines[-1] == ""
    assert all(line.strip(" \t") for line in lines[:-1])
    assert len("\n".join(lines)) <= LONGEST_COMMENT
