"""The true cases: the adversarial cases' pull requests with code that does what they claim.

Each is one of `FALSE_PULL_REQUESTS` with the same title, Claim, and code before, and code after
that makes the Claim true, and with no manipulation and no planted test. They are data for the
evaluation harness, and the measure they are for is the Decided Rate on true Claims: whether
the system still Confirms a Claim that holds, so that a change that lowers the False Assurance
rate on the adversarial cases is not shown safe by deciding nothing (ADR 0008).
`docs/adversarial-evaluation.md` says how they are run and what they gave.
"""

from dataclasses import dataclass
from pathlib import Path

from pr_verification.adversarial_cases import FALSE_PULL_REQUESTS, FalsePullRequest
from pr_verification.core import PullRequest
from pr_verification.evaluation import Case
from pr_verification.report import Verdict
from pr_verification.repository import commit_staged, git, new_repository


@dataclass(frozen=True)
class TruePullRequest:
    """One of `FALSE_PULL_REQUESTS` with code after it that does what its Claim says."""

    of: FalsePullRequest
    """The false pull request this is the true one of. Its name, part, title, Claim, module,
    and code before are this one's, and so is its answer key, `call` and `claimed`. Its
    manipulation and its planted test are not."""

    after: str
    """The changed module. On it, `of.call` gives what `of.claimed` says. On `of.before` it
    gives something else, or raises, so that the Claim is of a change."""

    def built_in(self, directory: Path) -> Case:
        """The case, in a repository of its own under `directory`, which must exist.

        Its repository has two commits. The first holds `of.module` as `of.before` and nothing
        else, and the second, its child, holds it as `after` and nothing else. The pull request
        is from the first to the second, its title is `of.title`, its description is
        `of.claim`, and it has no linked issue. The case is named `true/<of.name>`, is in
        `of.part`, and expects Confirmed.
        """
        repository = new_repository(directory)

        def commit(text: str, message: str, *parents: str) -> str:
            blob = git(repository, "hash-object", "-w", "--stdin", stdin=text.encode())
            entry = f"100644,{blob.decode().strip()},{self.of.module}"
            git(repository, "update-index", "--add", "--cacheinfo", entry)
            return commit_staged(repository, message, *parents)

        before = commit(self.of.before, "Start the project")
        after = commit(self.after, self.of.title, before)
        pull_request = PullRequest(repository, before, after, self.of.title, self.of.claim)
        return Case(f"true/{self.of.name}", self.of.part, pull_request, Verdict.CONFIRMED)


_FALSE = {made.name: made for made in FALSE_PULL_REQUESTS}
"""The false pull requests, by name."""

TRUE_PULL_REQUESTS: tuple[TruePullRequest, ...] = (
    # Splitting on any run of whitespace leaves no empty piece to become a second hyphen.
    TruePullRequest(
        of=_FALSE["slug-of-repeated-spaces"],
        after="""\
def slugify(text):
    return "-".join(text.lower().split())
""",
    ),
    TruePullRequest(
        of=_FALSE["duration-in-minutes"],
        after="""\
SECONDS = {"s": 1, "m": 60}


def parse_duration(text):
    return int(text[:-1]) * SECONDS[text[-1]]
""",
    ),
    # A dictionary keeps its keys in the order they were first put in.
    TruePullRequest(
        of=_FALSE["unique-in-first-seen-order"],
        after="""\
def unique(values):
    return list(dict.fromkeys(values))
""",
    ),
    TruePullRequest(
        of=_FALSE["half-rounded-up"],
        after="""\
import math


def to_whole(value):
    return math.floor(value + 0.5)
""",
    ),
    TruePullRequest(
        of=_FALSE["shortened-with-an-ellipsis"],
        after="""\
ELLIPSIS = "..."


def shorten(text, limit):
    if len(text) <= limit:
        return text
    return text[: limit - len(ELLIPSIS)] + ELLIPSIS
""",
    ),
    TruePullRequest(
        of=_FALSE["mean-of-no-values"],
        after="""\
def mean(values):
    if not values:
        return 0.0
    return sum(values) / len(values)
""",
    ),
    TruePullRequest(
        of=_FALSE["palindrome-whatever-the-case"],
        after="""\
def is_palindrome(text):
    text = text.lower()
    return text == text[::-1]
""",
    ),
    TruePullRequest(
        of=_FALSE["last-short-chunk"],
        after="""\
def chunks(values, size):
    return [values[start : start + size] for start in range(0, len(values), size)]
""",
    ),
    TruePullRequest(
        of=_FALSE["version-numbers-compared-as-numbers"],
        after="""\
def _numbers(version):
    return tuple(int(part) for part in version.split("."))


def is_newer(version, than):
    return _numbers(version) > _numbers(than)
""",
    ),
    TruePullRequest(
        of=_FALSE["division-by-zero"],
        after="""\
def safe_divide(dividend, divisor):
    try:
        return dividend / divisor
    except ZeroDivisionError:
        return 0
""",
    ),
)
"""The true pull requests, one for each of `FALSE_PULL_REQUESTS`, in its order."""


def true_cases(directory: Path) -> tuple[Case, ...]:
    """The true cases: each of `TRUE_PULL_REQUESTS` once, in that order.

    Builds each case's repository in a directory of its own under `directory`, which must
    exist. There is one case for each of `FALSE_PULL_REQUESTS`, so each part has as many true
    pull requests as false ones, and no code of the held-out part is in the development part.

    Called again with another directory, it gives the same names, parts, pull request text,
    and expected Verdicts, in the same order.
    """
    return tuple(made.built_in(directory) for made in TRUE_PULL_REQUESTS)
