"""Computing a Verdict from recorded Evidence. No model has a say here (ADR 0002)."""

from collections.abc import Sequence
from dataclasses import dataclass

from pr_verification.ecosystem import Results
from pr_verification.report import (
    AcceptedReproduction,
    ClaimKind,
    Evidence,
    ReproductionEvidence,
    SearchEvidence,
    TextChange,
    Verdict,
)

CONFIRMED = (
    "The Reproduction fails on the code before the pull request and passes on the code after it."
)
"""The reason for a Confirmed Verdict on a Behavioural Claim."""

REFUTED = (
    "The Reproduction passes on the code before the pull request and fails on the code after it."
)
"""The reason for a Refuted Verdict on a Behavioural Claim."""

NO_REPRODUCTION_RAN = (
    "No Reproduction ran on both the code before the pull request and the code after it, so "
    "there is no Evidence to decide this Claim on."
)
"""The reason for Inconclusive when the Evidence for a Behavioural Claim holds no Reproduction
that ran on both commits."""

DECIDES_NOTHING = (
    "A Reproduction ran on the code before the pull request and the code after it, and the "
    "Evidence from running it neither supports nor contradicts the Claim."
)
"""The reason for Inconclusive when a Reproduction ran on both commits and the Verdict on the
Behavioural Claim is neither Confirmed nor Refuted."""

STRUCTURAL_CONFIRMED = (
    "Searching the code before the pull request and the code after it found every text where "
    "the Claim was taken to put it: what it adds only after, and what it removes only before."
)
"""The reason for a Confirmed Verdict on a Structural Claim."""

STRUCTURAL_REFUTED = (
    "Searching the code after the pull request found text that the Claim was taken to say is "
    "removed, on the lines the Evidence lists."
)
"""The reason for a Refuted Verdict on a Structural Claim."""

NOTHING_SEARCHED = (
    "The code was searched for no text, so there is no Evidence to decide this Claim on."
)
"""The reason for Inconclusive when the Evidence for a Structural Claim holds no
`SearchEvidence`."""

KEPT_ELSEWHERE = (
    "Searching the one file the Claim was taken to be about found the text removed from it, "
    "and the code after the pull request holds that text in other files, on the lines "
    "the Evidence lists."
)
"""The reason for Inconclusive when a text that a Structural Claim was taken to say is removed
is gone from the one file searched and is held by another file after the pull request."""

SEARCH_DECIDES_NOTHING = (
    "The code before the pull request and the code after it were searched, and the Evidence "
    "from searching neither supports nor contradicts the Claim."
)
"""The reason for Inconclusive when the code was searched and the Verdict on the Structural
Claim is neither Confirmed nor Refuted."""


@dataclass(frozen=True)
class Computed:
    """A Verdict and why it was given."""

    verdict: Verdict
    """The Verdict."""

    reason: str
    """Word for word, one of `CONFIRMED`, `REFUTED`, `NO_REPRODUCTION_RAN`, and
    `DECIDES_NOTHING` for a Behavioural Claim, and one of `STRUCTURAL_CONFIRMED`,
    `STRUCTURAL_REFUTED`, `NOTHING_SEARCHED`, `KEPT_ELSEWHERE`, and `SEARCH_DECIDES_NOTHING`
    for a Structural Claim. It is written by the project and holds no Untrusted Content."""


def compute_verdict(
    evidence: Sequence[Evidence], kind: ClaimKind = ClaimKind.BEHAVIOURAL
) -> Computed:
    """The Verdict for one Claim of `kind`, from the Evidence recorded for it and nothing else.

    The same Evidence always gives the same Verdict for a kind: nothing is read but `evidence`
    and `kind`, no model is asked, and the order of the Evidence carries no meaning.

    The kind settles which Evidence can decide the Claim. A Behavioural Claim is decided only
    by a Reproduction, and never by reading. A Structural Claim is decided only by what a
    search of the code found, and never by running anything. A Claim that was not classified
    is verified as a Behavioural Claim, which is what `kind` is unless given.

    A Behavioural Claim
        Only a Reproduction that the Reproduction judge accepted, and that ran on both
        commits, can decide the Claim. A Reproduction ran on both commits when it is a
        `ReproductionEvidence` whose `before_results` and `after_results` are both `Results`,
        each holding no test in `could_not_run` and at least one test in `passed` or `failed`.
        One with None or `Unreadable` for either did not run on both commits. The judge
        accepted it when it is an `AcceptedReproduction`.

        A Reproduction the judge did not accept supports nothing and contradicts nothing,
        whatever its tests did on the two commits. So it never leads to Confirmed or Refuted:
        that is enforced here, by code, whatever gathered the Evidence and whatever a model
        said (ADR 0002). What is not enforced here is who makes an `AcceptedReproduction`:
        that is `write_judged_reproduction` alone. Any other Evidence decides nothing either,
        whatever it holds:
        Evidence obtained by reading, a `RunEvidence` that is not a `ReproductionEvidence`,
        and a Reproduction that did not run on both commits, as with a set-up error or an
        import error. Output, exit statuses, observations, and the text of the Reproduction
        are never read here: only which tests passed and which failed.

        A Reproduction asserts the behaviour the Claim says the code has after the pull
        request. One that the judge accepted and that ran on both commits:

        - supports the Claim when no test failed on the code after, and at least one test
          that passed on the code after failed on the code before;
        - contradicts the Claim when no test failed on the code before, and at least one test
          that passed on the code before failed on the code after;
        - otherwise does neither. So a Reproduction that fails on both commits does neither:
          the Claim may be false, or the Reproduction may be wrong, and nothing recorded tells
          the two apart. So does one that passes on both, and one with a failing test on each
          commit.

        A test is the same test on both commits when it has the same name. A test that one
        commit's results do not hold neither passed nor failed there.

        The Verdict is then:

        - Confirmed, with the reason `CONFIRMED`, when at least one Reproduction supports the
          Claim and none contradicts it;
        - Refuted, with the reason `REFUTED`, when at least one Reproduction contradicts the
          Claim and none supports it;
        - Inconclusive, with the reason `NO_REPRODUCTION_RAN`, when no Reproduction ran on
          both commits, accepted or not, which includes there being no Evidence at all;
        - Inconclusive, with the reason `DECIDES_NOTHING`, in every other case, which includes
          one Reproduction supporting the Claim while another contradicts it, and a
          Reproduction that ran on both commits and that the judge did not accept.

        Refuted blocks the Gate, so its condition is the control on the False Block rate: it
        is met only by a regression, where what the Claim says held before the pull request
        and does not hold after it.

    A Structural Claim
        Only `SearchEvidence` can decide the Claim. Any other Evidence decides nothing,
        whatever it holds: a Reproduction, accepted or not, any other Evidence obtained by
        running code, and a `ReadEvidence`. Of a `SearchEvidence`, only its `change` and
        how many lines hold its text are read: the number of its `before_lines`, of its
        `after_lines`, and of its `after_lines_elsewhere`. Its text, its path, its
        observation, and what the lines hold are never read here.

        One `SearchEvidence`:

        - supports the Claim when the code shows the change the Claim says. For
          `TextChange.ADDED`, no line holds the text before the pull request and at least one
          does after. For `TextChange.REMOVED`, at least one line holds it before, none does
          after, and none does after in any other file: it has no `after_lines_elsewhere`;
        - contradicts the Claim when its `change` is `TextChange.REMOVED` and at least one
          line holds the text before the pull request and at least one after: what the Claim
          says the pull request removes is still there;
        - is kept elsewhere when its `change` is `TextChange.REMOVED`, at least one line
          holds the text before the pull request, none does after, and it has at least one
          of `after_lines_elsewhere`: the text is gone from the one file searched and
          another file holds it. It neither supports nor contradicts the Claim;
        - otherwise does neither. So a text that is on neither commit does neither: the Claim
          may be false, or the text may be the wrong one to search for, and nothing recorded
          tells the two apart. So does a text said to be added that was already there before,
          and one the pull request did the opposite to: added when the Claim says removed, or
          removed when it says added.

        The Verdict is then:

        - Refuted, with the reason `STRUCTURAL_REFUTED`, when at least one `SearchEvidence`
          contradicts the Claim, whatever the others show. A rename that leaves the old name
          in the code is not carried out, however many lines hold the new one;
        - Confirmed, with the reason `STRUCTURAL_CONFIRMED`, when there is at least one
          `SearchEvidence` and every one supports the Claim. Each text is one thing the Claim
          says, so one that shows nothing leaves the Claim undecided;
        - Inconclusive, with the reason `NOTHING_SEARCHED`, when there is no `SearchEvidence`,
          which includes there being no Evidence at all;
        - Inconclusive, with the reason `KEPT_ELSEWHERE`, when none contradicts the Claim and
          at least one is kept elsewhere, whatever the others show;
        - Inconclusive, with the reason `SEARCH_DECIDES_NOTHING`, in every other case.

        `after_lines_elsewhere` can only take a Confirmed away. It never leads to Refuted,
        and it is not read for a text said to be added. A rename or a removal is a Claim
        about every file, and the role chooses the `path`: searched in one file alone, a
        rename that skips another file was Confirmed, a False Assurance, in three of the
        Structural cases of the evaluation. So code searches the other files whatever path
        the role gave, and a line found there stops Confirmed. It does not give Refuted,
        because a text can have good reason to stay in another file, as an old requirement
        does in a project's examples, and Refuted there would be a False Block. The price is
        Decided Rate: a true Claim whose text stays elsewhere is now Inconclusive.

        Refuted blocks the Gate, so its condition is kept to the one case with lines to
        show, which the Evidence lists: it is never met by a search that found nothing. The
        limit that remains is that a model chose each text and its `change`, and no step
        judges that choice as the Reproduction judge judges a Reproduction. A text that is
        too wide, or that has good reason to stay in the code, gives a False Block. How often
        is for the Structural cases of the evaluation to measure.
    """
    if kind is ClaimKind.STRUCTURAL:
        return _from_searches([piece for piece in evidence if isinstance(piece, SearchEvidence)])
    ran = [
        (piece, results)
        for piece in evidence
        if (results := _ran_on_both_commits(piece)) is not None
    ]
    if not ran:
        return Computed(Verdict.INCONCLUSIVE, NO_REPRODUCTION_RAN)
    # That it ran is all a Reproduction the judge did not accept is read for.
    read = [results for piece, results in ran if isinstance(piece, AcceptedReproduction)]
    supported = any(_holds_on_one_only(held=after, other=before) for before, after in read)
    contradicted = any(_holds_on_one_only(held=before, other=after) for before, after in read)
    if supported and not contradicted:
        return Computed(Verdict.CONFIRMED, CONFIRMED)
    if contradicted and not supported:
        return Computed(Verdict.REFUTED, REFUTED)
    return Computed(Verdict.INCONCLUSIVE, DECIDES_NOTHING)


def _from_searches(searches: Sequence[SearchEvidence]) -> Computed:
    """The Verdict for a Structural Claim, from the searches among its Evidence."""
    if not searches:
        return Computed(Verdict.INCONCLUSIVE, NOTHING_SEARCHED)
    # Only the change and how many lines hold the text are read.
    found = [
        (search.change, len(search.before_lines), len(search.after_lines)) for search in searches
    ]
    if any(change is TextChange.REMOVED and before and after for change, before, after in found):
        return Computed(Verdict.REFUTED, STRUCTURAL_REFUTED)
    changed = [_shows_the_change(change, before, after) for change, before, after in found]
    # A text gone from the one file searched and held by another shows nothing of the Claim.
    kept_elsewhere = [
        shown and search.change is TextChange.REMOVED and bool(search.after_lines_elsewhere)
        for shown, search in zip(changed, searches, strict=True)
    ]
    if any(kept_elsewhere):
        return Computed(Verdict.INCONCLUSIVE, KEPT_ELSEWHERE)
    if all(changed):
        return Computed(Verdict.CONFIRMED, STRUCTURAL_CONFIRMED)
    return Computed(Verdict.INCONCLUSIVE, SEARCH_DECIDES_NOTHING)


def _shows_the_change(change: TextChange, before: int, after: int) -> bool:
    """Whether a text held on this many lines of each commit was changed as `change` says, where
    it was searched for: on no line of the one commit and at least one of the other."""
    if change is TextChange.ADDED:
        return not before and bool(after)
    return bool(before) and not after


def _ran_on_both_commits(evidence: Evidence) -> tuple[Results, Results] | None:
    """Which tests passed and failed before and after, when `evidence` is a Reproduction that
    ran on both commits. None for any other Evidence."""
    if not isinstance(evidence, ReproductionEvidence):
        return None
    before, after = evidence.before_results, evidence.after_results
    if not (isinstance(before, Results) and isinstance(after, Results)):
        return None
    if any(read.could_not_run or not (read.passed or read.failed) for read in (before, after)):
        return None
    return before, after


def _holds_on_one_only(held: Results, other: Results) -> bool:
    """Whether what the Reproduction asserts held on one commit and not on the other: no test
    failed on the first, and a test that passed there failed on the second."""
    return not held.failed and bool(set(held.passed) & set(other.failed))
