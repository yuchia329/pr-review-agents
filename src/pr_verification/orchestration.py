"""The orchestration boundary: deciding what work is done to gather Evidence for each Claim.

An orchestration chooses which roles run and in what order. It gathers Evidence and decides
nothing: each Verdict is computed from what it gathered by `compute_verdict`, and the Gate from
the Verdicts, by the core's entry point (ADR 0003). The fixed pipeline is the first
implementation and is kept as a baseline. Another can take its place without any change to the
roles, which know nothing of what runs them.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pr_verification.ecosystem import find_commands
from pr_verification.model import Model
from pr_verification.repo_profile import DependencyCache, Discover, ProfileStore
from pr_verification.report import Evidence
from pr_verification.sandbox import ContainerSandbox, Limits, SandboxWithHomes

LIMITS = Limits(seconds=600.0, memory_bytes=2 * 1024**3, processors=2.0)
"""The most one run in the Sandbox may use unless other limits are given."""

NO_REPRODUCTION_ECOSYSTEM = (
    "The repository's Ecosystem cannot run a Reproduction, so no Evidence was gathered."
)
"""Why every Claim is Inconclusive when the Ecosystem found is not a `ReproductionEcosystem`."""

COULD_NOT_PREPARE = (
    "The code {commit} the pull request could not be prepared for running, so no Reproduction "
    "was run: {failure}"
)
"""Why every Claim is Inconclusive when preparing a commit did not work. `commit` is `before`
or `after`. `failure` is `the command <command> ended with <ending>` for a `CommandFailed`,
where the command is its parts with a space between them and the ending is as
`run_on_both_commits` writes one, and is what the exception said for a `SandboxFailed`. It holds
none of the output."""

CLAIM_FAILED = "The work on this Claim failed inside the system: {failure}"
"""Why one Claim is Inconclusive when the work on it raised. `failure` is the name of the
exception's class, then `: `, then what it said."""

OpenSandbox = Callable[[Path, str], AbstractContextManager[SandboxWithHomes]]
"""Opens a Sandbox for the commits of the repository named, running in the image named. The
Sandbox is the one in use inside the `with`, and leaving the `with` closes it.
`ContainerSandbox` is one."""


@dataclass(frozen=True)
class Gathered:
    """What an orchestration gathered for one Claim."""

    evidence: tuple[Evidence, ...]
    """The Evidence, which may be none."""

    inconclusive: str | None = None
    """Why the work ended without Evidence that could decide the Claim, when the orchestration
    knows. It can only ever explain an Inconclusive Verdict: it is never read to compute one.
    It may hold what a failure said, and so Untrusted Content."""


class Orchestration(Protocol):
    """Gathers Evidence for the Claims of one pull request."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        """Gather Evidence for each of `claims`.

        `before` and `after` are the full IDs of the commits before and after the pull request,
        in `repository`. Returns one `Gathered` for each Claim, in the order of `claims`, so
        every Claim is answered for. No Verdict is given here.

        An implementation says what it raises. `ModelFailed` is never raised for the work on a
        Claim: a Claim the model failed on is answered for with no Evidence.
        """
        ...


@dataclass(frozen=True)
class FixedPipeline:
    """The `Orchestration` that does the same work for every Claim, in the order given."""

    store: ProfileStore
    """Where the Repo Profile is kept between runs."""

    cache: DependencyCache
    """Where installed dependencies are kept between runs."""

    limits: Limits = LIMITS
    """The most each run in the Sandbox may use."""

    open_sandbox: OpenSandbox = ContainerSandbox
    """Opens each Sandbox the pipeline runs in."""

    discover: Discover = find_commands
    """Works out the repository's Ecosystem and how to install and test it."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        """Write and run a Reproduction for each Claim, one Claim after another.

        With no Claims, nothing is discovered, no Sandbox is opened, and the answer is empty.
        Otherwise the steps are these, in this order.

        1. Finds the Ecosystem. `discover(repository, before)` is asked, so the Ecosystem is
           decided by code already on the target branch. When it answers `Unsupported`, every
           Claim is given no Evidence and `inconclusive` is the `Unsupported`'s reason. When
           the `Commands`' `ecosystem` is not a `ReproductionEcosystem`, every Claim is given
           no Evidence and `NO_REPRODUCTION_ECOSYSTEM`. Either way no Sandbox is opened.
        2. Prepares the code before the pull request, with `prepare_target(repository, before,
           ...)` and the pipeline's `store`, `cache`, `limits`, and `discover`. Its
           `sandbox_for` opens each Sandbox with `open_sandbox(repository, image)`.
        3. Prepares the code after it, with `prepare_pull_request(repository, after, ...)` in
           the Sandbox and with the Repo Profile that step 2 gave.
        4. For each Claim in turn, calls `write_reproduction` with the Claim, `model`, that
           Sandbox, the Ecosystem, `repository`, `before`, `after`, and `limits`, and with
           the caps `write_reproduction` has unless given others. The Claim is given the
           `ReproductionRun`'s `evidence` as its one piece of Evidence when there is one, and
           otherwise no Evidence and the `ReproductionRun`'s `inconclusive`.

        When step 2 or step 3 answers `CommandFailed` or raises `SandboxFailed`, no
        Reproduction is written, no request is made to the model, and every Claim is given no
        Evidence and `COULD_NOT_PREPARE` filled in. An `Unsupported` from step 2 is treated as
        one from step 1.

        A failure while working on one Claim does not stop work on the others. When
        `write_reproduction` raises any `Exception` for a Claim, that Claim is given no
        Evidence and `CLAIM_FAILED` filled in, and the next Claim is worked on as if nothing
        had happened. Each Claim's work is its own call, so nothing of one Claim reaches the
        role working on another.

        A model's word is never Evidence here. The only Evidence a Claim is given is what
        `write_reproduction` recorded from runs in the Sandbox.

        Every Sandbox opened is closed before the answer is given or an exception goes on.

        Raises `ValueError` as `prepare_target` does. Nothing else is raised for what the
        repository, the pull request, or the model does.
        """
        raise NotImplementedError


def fixed_pipeline(state: Path) -> FixedPipeline:
    """The fixed pipeline that keeps what it remembers between runs under `state`.

    `state` need not exist yet. The Repo Profile is kept by a `DirectoryStore` on
    `state / "profiles"` and installed dependencies by a `DependencyCache` on
    `state / "dependencies"`. Everything else is as `FixedPipeline` has it unless given.
    """
    raise NotImplementedError
