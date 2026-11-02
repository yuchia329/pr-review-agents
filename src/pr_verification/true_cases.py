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

from pr_verification.adversarial_cases import FalsePullRequest
from pr_verification.evaluation import Case


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
        raise NotImplementedError


TRUE_PULL_REQUESTS: tuple[TruePullRequest, ...] = ()
"""The true pull requests, one for each of `FALSE_PULL_REQUESTS`, in its order."""


def true_cases(directory: Path) -> tuple[Case, ...]:
    """The true cases: each of `TRUE_PULL_REQUESTS` once, in that order.

    Builds each case's repository in a directory of its own under `directory`, which must
    exist. There is one case for each of `FALSE_PULL_REQUESTS`, so each part has as many true
    pull requests as false ones, and no code of the held-out part is in the development part.

    Called again with another directory, it gives the same names, parts, pull request text,
    and expected Verdicts, in the same order.
    """
    raise NotImplementedError
