"""Computing a Verdict from recorded Evidence. No model has a say here (ADR 0002)."""

from collections.abc import Sequence
from dataclasses import dataclass

from pr_verification.ecosystem import Results
from pr_verification.report import Evidence, ReproductionEvidence, Verdict

CONFIRMED = (
    "The Reproduction fails on the code before the pull request and passes on the code after it."
)
"""The reason for a Confirmed Verdict."""

REFUTED = (
    "The Reproduction passes on the code before the pull request and fails on the code after it."
)
"""The reason for a Refuted Verdict."""

NO_REPRODUCTION_RAN = (
    "No Reproduction ran on both the code before the pull request and the code after it, so "
    "there is no Evidence to decide this Claim on."
)
"""The reason for Inconclusive when the Evidence holds no Reproduction that ran on both commits."""

DECIDES_NOTHING = (
    "A Reproduction ran on the code before the pull request and the code after it, and the "
    "Evidence from running it neither supports nor contradicts the Claim."
)
"""The reason for Inconclusive when a Reproduction ran on both commits and the Verdict is
neither Confirmed nor Refuted."""


@dataclass(frozen=True)
class Computed:
    """A Verdict and why it was given."""

    verdict: Verdict
    """The Verdict."""

    reason: str
    """One of `CONFIRMED`, `REFUTED`, `NO_REPRODUCTION_RAN`, and `DECIDES_NOTHING`, word for
    word. It is written by the project and holds no Untrusted Content."""


def compute_verdict(evidence: Sequence[Evidence]) -> Computed:
    """The Verdict for one Behavioural Claim, from the Evidence recorded for it and nothing else.

    The same Evidence always gives the same Verdict: nothing is read but `evidence`, no model
    is asked, and the order of the Evidence carries no meaning.

    Only a Reproduction that ran on both commits can decide the Claim. That is a
    `ReproductionEvidence` whose `before_results` and `after_results` are both `Results`,
    each holding no test in `could_not_run` and at least one test in `passed` or `failed`.
    One with None or `Unreadable` for either did not run on both commits. Any other Evidence
    decides nothing, whatever it holds: Evidence obtained by reading, a `RunEvidence` that is
    not a `ReproductionEvidence`, and a Reproduction that did not run on both commits, as
    with a set-up error or an import error. Output, exit statuses, observations, and the
    text of the Reproduction are never read here: only which tests passed and which failed.

    A Reproduction asserts the behaviour the Claim says the code has after the pull request.
    One that ran on both commits:

    - supports the Claim when no test failed on the code after, and at least one test that
      passed on the code after failed on the code before;
    - contradicts the Claim when no test failed on the code before, and at least one test that
      passed on the code before failed on the code after;
    - otherwise does neither. So a Reproduction that fails on both commits does neither: the
      Claim may be false, or the Reproduction may be wrong, and nothing recorded tells the two
      apart. So does one that passes on both, and one with a failing test on each commit.

    A test is the same test on both commits when it has the same name. A test that one commit's
    results do not hold neither passed nor failed there.

    The Verdict is then:

    - Confirmed, with the reason `CONFIRMED`, when at least one Reproduction supports the Claim
      and none contradicts it;
    - Refuted, with the reason `REFUTED`, when at least one Reproduction contradicts the Claim
      and none supports it;
    - Inconclusive, with the reason `NO_REPRODUCTION_RAN`, when no Reproduction ran on both
      commits, which includes there being no Evidence at all;
    - Inconclusive, with the reason `DECIDES_NOTHING`, in every other case, which includes
      one Reproduction supporting the Claim while another contradicts it.

    Refuted blocks the Gate, so its condition is the control on the False Block rate: it is
    met only by a regression, where what the Claim says held before the pull request and does
    not hold after it.

    Whatever kind the Claim classifier gives a Claim, it is verified as a Behavioural Claim
    until the verification of Structural Claims is built.
    Until the Reproduction judge is built, no step checks that a Reproduction tests what its
    Claim says (ADR 0002): every Reproduction that ran on both commits is taken as Evidence.
    """
    read = [results for results in map(_ran_on_both_commits, evidence) if results is not None]
    if not read:
        return Computed(Verdict.INCONCLUSIVE, NO_REPRODUCTION_RAN)
    supported = any(_holds_on_one_only(held=after, other=before) for before, after in read)
    contradicted = any(_holds_on_one_only(held=before, other=after) for before, after in read)
    if supported and not contradicted:
        return Computed(Verdict.CONFIRMED, CONFIRMED)
    if contradicted and not supported:
        return Computed(Verdict.REFUTED, REFUTED)
    return Computed(Verdict.INCONCLUSIVE, DECIDES_NOTHING)


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
