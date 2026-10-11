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

from pr_verification.agent import RoleRun
from pr_verification.decision import Decider
from pr_verification.model import Model
from pr_verification.report import SearchEvidence

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
    `VERIFIER_STEP_CAP_REACHED` or `VERIFIER_COULD_NOT_FINISH`, filled in. There is no
    Evidence then. None when the role finished, whatever it finished with."""


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
    `SearchEvidence`:

    - `source` is `EvidenceSource.READ_CODE`;
    - `text` and `change` are the entry's, and `path` is the entry's, or None when it was
      left out;
    - `before_lines` is every line of `before` that holds the text, and `after_lines` every
      line of `after` that does. A line holds a text, and is written and ordered, as the
      `search` tool of `reading_tools` has it: `<path>:<line number>:<line>`, by path and
      then by line number. With a `path`, only the lines of the file at that path are
      searched, and a commit with no file there has no line that holds the text;
    - `observation` is `lines holding the text: <number> before, <number> after`, written by
      code.

    So what is recorded is what code found in the two commits, whatever the role said is
    there. The model's word alone is never Evidence, and the role gives no Verdict: one is
    computed from this Evidence by `compute_verdict`.

    An entry is left out when it is not an object, when it has no `text`, no `change`, or a
    member other than those and `path`, when its `text` is not a string, is blank, or holds a
    line feed, when its `change` is not one of the two values, and when its `path` is not a
    string or is empty. There is no Evidence at all when the answer has no list under
    `texts`, or when that list has more than `TEXT_CAP` entries, counting those that would be
    left out: the cap is enforced here, by code, and a longer list is searched for nothing.

    When the run stopped at the step cap, there is no Evidence and `inconclusive` is
    `VERIFIER_STEP_CAP_REACHED` filled in. When `ModelFailed` is raised, by the model or as
    a `DecisionFailed` by the progress guard, the work ends there: the record is returned
    with no `run`, no Evidence, and `inconclusive` as `VERIFIER_COULD_NOT_FINISH` filled in.
    A failure inside the system leaves the Claim Inconclusive.

    Raises `ValueError` before any request when `step_cap` is less than 1. Any other
    exception is a fault in the system and is raised.
    """
    raise NotImplementedError
