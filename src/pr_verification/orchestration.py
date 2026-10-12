"""The orchestration boundary: deciding what work is done to gather Evidence for each Claim.

An orchestration chooses which roles run and in what order. It gathers Evidence and decides
nothing: each Verdict is computed from what it gathered by `compute_verdict`, and the Gate from
the Verdicts, by the core's entry point (ADR 0003). The fixed pipeline is the first
implementation and is kept as a baseline. Another can take its place without any change to the
roles, which know nothing of what runs them: the LLM supervisor, in
`pr_verification.supervisor`, is the second.

An orchestration that is told the kind of each Claim routes by it: a Behavioural Claim goes on
to a Reproduction, and a Structural Claim is verified by reading and is not sent to a Sandbox.
"""

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol, runtime_checkable

from pr_verification.budget import BUDGET_SPENT, BudgetSpent, Ledger
from pr_verification.decision import Decider
from pr_verification.ecosystem import ReproductionEcosystem, Unsupported, find_commands
from pr_verification.evidence import ending
from pr_verification.judge import ROUND_CAP, write_judged_reproduction
from pr_verification.model import Model
from pr_verification.repo_profile import (
    CommandFailed,
    DependencyCache,
    DirectoryStore,
    Discover,
    Prepared,
    ProfileStore,
    RepoProfile,
    SandboxFor,
    prepare_pull_request,
    prepare_target,
)
from pr_verification.report import ClaimKind, Evidence
from pr_verification.sandbox import (
    ContainerSandbox,
    Limits,
    Sandbox,
    SandboxFailed,
    SandboxWithHomes,
)
from pr_verification.structural import verify_structural_claim

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
        every Claim is answered for. No Verdict is given here. It is told no kinds, so each of
        `claims` is verified as a Behavioural Claim.

        An implementation says what it raises. `ModelFailed` is never raised for the work on a
        Claim: a Claim the model failed on is answered for with no Evidence.
        """
        ...


@runtime_checkable
class BudgetedOrchestration(Orchestration, Protocol):
    """An orchestration that keeps to a Budget, by doing its work through a ledger.

    The core's entry point asks such an orchestration through `gather_within`, and does not call
    its `gather`.
    """

    def gather_within(
        self,
        ledger: Ledger,
        claims: tuple[str, ...],
        *,
        repository: Path,
        before: str,
        after: str,
        model: Model,
        kinds: tuple[ClaimKind | None, ...] | None = None,
    ) -> tuple[Gathered, ...]:
        """Gather Evidence for each of `claims`, as `gather` does, spending only what `ledger`
        allows, and in the way the kind of each calls for.

        Everything else is as `gather` has it: one `Gathered` for each Claim, in the order of
        `claims`, and no Verdict. In `ledger`, a Claim is named by its place in `claims`,
        counted from 0. It is called with at least one Claim.

        `kinds`, when given, holds the kind of each Claim, in the order of `claims`, with None
        for a Claim that was not classified. A Claim it names Structural is verified by
        reading, and nothing is run in a Sandbox for it. Every other Claim goes on to a
        Reproduction, as every Claim does when `kinds` is None. An implementation says how.

        - Every request to a model is made through `ledger.model`, and everything done in a
          Sandbox through `ledger.sandbox`: for the Claim the work is for, and for no Claim
          when it is for no one Claim. Nothing is asked of a model or a Sandbox any other way,
          so nothing is spent that the ledger does not record, and no model can go on past a
          limit.
        - The share of each Claim is set aside with `ledger.share_out(len(claims))`, before
          any work for one Claim. When no such work is ever started, it need not be.
        - When a Claim's share is spent, the work on that Claim stops, and the work on the
          others goes on. A Claim stopped so with no Evidence that could decide it is given
          `SHARE_SPENT` filled in as its `inconclusive`.
        - When the Budget is spent, no further work is started for any Claim. Each Claim then
          left with no Evidence that could decide it is given `BUDGET_SPENT` filled in as its
          `inconclusive`.

        `BudgetSpent` is never raised from here: a limit that is reached ends work, and every
        Claim is still answered for.
        """
        ...


@dataclass(frozen=True)
class _Ready:
    """What a Reproduction is run with, once both commits are prepared."""

    sandbox: SandboxWithHomes
    """The Sandbox in which the dependencies of both commits are installed."""

    ecosystem: ReproductionEcosystem
    """The repository's Ecosystem."""


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

    progress_guard: Decider | None = None
    """Answers the progress guard of every role the pipeline runs. With None, no role has
    one."""

    round_cap: int = ROUND_CAP
    """The most rounds between the Reproduction writer and the Reproduction judge for one
    Claim."""

    def gather(
        self, claims: tuple[str, ...], *, repository: Path, before: str, after: str, model: Model
    ) -> tuple[Gathered, ...]:
        """Write and run a Reproduction for each Claim and have it judged, one Claim after
        another.

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
        4. For each Claim in turn, calls `write_judged_reproduction` with the Claim, `model`,
           that Sandbox, the Ecosystem, `repository`, `before`, `after`, and `limits`, and
           with the pipeline's `round_cap` and `progress_guard`. The Claim is given the
           `JudgedRun`'s `evidence` as its one piece of Evidence when there is one, and
           otherwise no Evidence and the `JudgedRun`'s `inconclusive`.

        When step 2 or step 3 answers `CommandFailed` or raises `SandboxFailed`, no
        Reproduction is written, no request is made to the model, and every Claim is given no
        Evidence and `COULD_NOT_PREPARE` filled in. An `Unsupported` from step 2 is treated as
        one from step 1.

        A failure while working on one Claim does not stop work on the others. When the
        model or the Sandbox fails during a Claim's work, `write_judged_reproduction` answers
        with the reason and no Evidence, and the next Claim is worked on as if nothing had
        happened: every Claim is worked on, whatever came of those before it. Each Claim's
        work is its own call, so nothing of one Claim reaches a role working on another.

        A model's word is never Evidence here. The only Evidence a Claim is given is a
        Reproduction the judge accepted, as `write_judged_reproduction` recorded it from runs
        in the Sandbox. A Reproduction the judge did not accept is not among a Claim's
        Evidence, whatever its runs showed.

        Every Sandbox opened is closed before the answer is given or an exception goes on.

        Raises `ValueError` as `prepare_target` does. Any other exception is a fault in the
        system and is raised, as `write_judged_reproduction` raises it: it is not turned into
        an Inconclusive Verdict.
        """
        if not claims:
            return ()
        with ExitStack() as opened:
            ready = self._prepare(
                repository,
                before,
                after,
                lambda image: opened.enter_context(self.open_sandbox(repository, image)),
            )
            if isinstance(ready, str):
                return tuple(Gathered((), ready) for _ in claims)
            return tuple(
                self._reproduce(
                    claim, model, ready.sandbox, ready.ecosystem, repository, before, after
                )
                for claim in claims
            )

    def gather_within(
        self,
        ledger: Ledger,
        claims: tuple[str, ...],
        *,
        repository: Path,
        before: str,
        after: str,
        model: Model,
        kinds: tuple[ClaimKind | None, ...] | None = None,
    ) -> tuple[Gathered, ...]:
        """Gather Evidence for each Claim in the way its kind calls for, within the Budget.

        The steps are those of `gather`, and each Claim is given what `gather` would give it,
        except as said here. With no Claims nothing is done and `ledger` is not used.

        Routing
            A Claim that `kinds` names Structural is verified by reading. In its turn in
            step 4, `verify_structural_claim` is called for it in place of
            `write_judged_reproduction`, with the Claim, `repository`, `before`, `after`, the
            model, the pipeline's `progress_guard`, and the step cap it has unless given
            another. The Claim is given the `StructuralRun`'s `evidence` and its
            `inconclusive`. No Sandbox is used for it, and no Reproduction is written.

            Every other Claim goes on to a Reproduction, as step 4 of `gather` says: a Claim
            `kinds` names Behavioural, one it has None for, and every Claim when `kinds` is
            None. The Claims are worked on in the order of `claims`, of whichever kind.

            Steps 1 to 3 are for the Claims that go on to a Reproduction. When every Claim is
            Structural they are not taken: nothing is discovered and no Sandbox is opened.
            When one of them ends with `Unsupported`, `CommandFailed`, or `SandboxFailed`,
            what `gather` gives every Claim for that is given to each Claim that goes on to a
            Reproduction, and each Structural Claim is still verified.

            Raises `ValueError`, before anything else is done, when `kinds` is given and
            holds other than one kind for each Claim.

        What is recorded
            - Steps 1 to 3 are for no one Claim. Each Sandbox opened is used through
              `ledger.sandbox(sandbox)`, so the time taken to install dependencies is recorded
              against the pull request alone.
            - When step 3 has been taken, the shares are set aside with
              `ledger.share_out(len(claims))`: preparing comes out of no Claim's share. They
              are for every Claim, of whichever kind, and are set aside before the first Claim
              is worked on also when steps 1 to 3 were not taken or did not work and a
              Structural Claim is still to be verified.
            - In step 4, the Claim at place `index` in `claims` is worked on with
              `ledger.model(model, claim=index)` in place of `model` and
              `ledger.sandbox(sandbox, claim=index)` in place of the Sandbox, where `sandbox`
              is the Sandbox as it was opened, so that no run is timed twice. So every request
              made for the Claim, by the writer, the judge, or the Structural verifier, is one
              step against that Claim, and every run is timed against it.

        When a Claim's share is spent
            A `BudgetSpent` for a Claim's share ends the work on that Claim: it is given no
            Evidence, and the message of the `BudgetSpent`, which is `SHARE_SPENT` filled in.
            The next Claim is worked on as if nothing had happened, with its own share whole.

        When the Budget is spent
            No further work is started: no further request is made to the model, nothing more
            is run, and no further Sandbox is opened. Every Claim not yet answered for, the
            one being worked on included, is given no Evidence and `BUDGET_SPENT` filled in
            with the limit `ledger.stopped_by()` gives. Claims answered for before then keep
            what they were given. The Budget is found spent:

            - on the call, when `ledger.stopped_by()` gives a limit, in which case nothing is
              discovered and no Sandbox is opened;
            - by a `BudgetSpent` raised during steps 1 to 3, or by `ledger.share_out`;
            - before the work on a Claim starts, when `ledger.stopped_by()` gives a limit, or
              by a `BudgetSpent` for the Budget raised during that work.

            A step of 1 to 3 that ends as `gather` says one can, with `Unsupported`,
            `CommandFailed`, or `SandboxFailed`, is answered for as `gather` says, whatever
            had been spent by then.

        Evidence already gathered is never thrown away. A Claim whose work gave Evidence is
        given it, though the last piece of that work was the one that reached a limit.

        Every Sandbox opened is closed before the answer is given or an exception goes on.
        Raises what `gather` raises. `BudgetSpent` is never raised from here.
        """
        if kinds is None:
            kinds = (None,) * len(claims)
        if len(kinds) != len(claims):
            raise ValueError(f"{len(kinds)} kinds were given for the {len(claims)} Claims")
        if not claims:
            return ()
        gathered: dict[int, Gathered] = {}
        with ExitStack() as opened:
            try:
                for index, given in self._gather_until_spent(
                    ledger, claims, kinds, repository, before, after, model, opened
                ):
                    gathered[index] = given
            except BudgetSpent:
                # It was spent while preparing or by setting the shares aside. The Claims
                # answered for by then keep what they were given.
                pass
        limit = ledger.stopped_by()
        if limit is None:
            return tuple(gathered[index] for index in range(len(claims)))
        unresolved = Gathered((), BUDGET_SPENT.format(limit=limit.value))
        return tuple(gathered.get(index, unresolved) for index in range(len(claims)))

    def _gather_until_spent(
        self,
        ledger: Ledger,
        claims: tuple[str, ...],
        kinds: tuple[ClaimKind | None, ...],
        repository: Path,
        before: str,
        after: str,
        model: Model,
        opened: ExitStack,
    ) -> Iterator[tuple[int, Gathered]]:
        """The steps of `gather_within`, for as long as the Budget is not spent: the place in
        `claims` of each Claim answered for by then, with what it is given. Raises
        `BudgetSpent` when the Budget is spent during steps 1 to 3 or by setting the shares
        aside. Every Sandbox opened is left for `opened` to close."""
        if ledger.stopped_by() is not None:
            return
        structural = [kind is ClaimKind.STRUCTURAL for kind in kinds]
        # Each Sandbox as it is used while preparing, with the same Sandbox as it was opened.
        as_opened: dict[SandboxWithHomes, SandboxWithHomes] = {}

        def sandbox_for(image: str) -> SandboxWithHomes:
            spent = ledger.stopped_by()
            if spent is not None:
                raise BudgetSpent(spent)
            sandbox = opened.enter_context(self.open_sandbox(repository, image))
            through = ledger.sandbox(sandbox)
            as_opened[through] = sandbox
            return through

        # Steps 1 to 3 are for the Claims that go on to a Reproduction, when there are any.
        ready = None if all(structural) else self._prepare(repository, before, after, sandbox_for)
        if isinstance(ready, str):
            # No Reproduction can be run, so each Claim that would go on to one is answered for.
            for index in range(len(claims)):
                if not structural[index]:
                    yield index, Gathered((), ready)
            if not any(structural):
                return
        ledger.share_out(len(claims))
        for index, claim in enumerate(claims):
            if not (structural[index] or isinstance(ready, _Ready)):
                continue
            if ledger.stopped_by() is not None:
                break
            try:
                if isinstance(ready, _Ready) and not structural[index]:
                    given = self._reproduce(
                        claim,
                        ledger.model(model, claim=index),
                        ledger.sandbox(as_opened[ready.sandbox], claim=index),
                        ready.ecosystem,
                        repository,
                        before,
                        after,
                    )
                else:
                    given = self._verify_by_reading(
                        claim, ledger.model(model, claim=index), repository, before, after
                    )
            except BudgetSpent as spent:
                if spent.claim is None:
                    break
                given = Gathered((), str(spent))
            yield index, given

    def prepare_target_commit(
        self, repository: Path, target: str
    ) -> RepoProfile | Unsupported | CommandFailed:
        """Prepare `target`, a commit of the target branch, with no pull request to verify.

        What a later run needs is then already kept: the Repo Profile is learned, and the
        installed dependencies are saved, from the target branch and from nothing of a pull
        request.

        Calls `prepare_target(repository, target, ...)` once, with the pipeline's `store`,
        `cache`, `limits`, and `discover`, as step 2 of `gather` does. Its `sandbox_for`
        opens each Sandbox with `open_sandbox(repository, image)`. The answer is the
        `profile` of a `Prepared`, and an `Unsupported` or a `CommandFailed` as it is. Every
        Sandbox opened is closed before the answer is given or an exception goes on. No
        model is asked.

        Raises as `prepare_target` does.
        """
        with ExitStack() as opened:
            prepared = prepare_target(
                repository,
                target,
                store=self.store,
                cache=self.cache,
                sandbox_for=lambda image: opened.enter_context(
                    self.open_sandbox(repository, image)
                ),
                limits=self.limits,
                discover=self.discover,
            )
        return prepared.profile if isinstance(prepared, Prepared) else prepared

    def _reproduce(
        self,
        claim: str,
        model: Model,
        sandbox: Sandbox,
        ecosystem: ReproductionEcosystem,
        repository: Path,
        before: str,
        after: str,
    ) -> Gathered:
        """Step 4 of `gather` for one Claim: what it is given, with every request made to
        `model` and every run made in `sandbox`."""
        judged = write_judged_reproduction(
            claim,
            model=model,
            sandbox=sandbox,
            ecosystem=ecosystem,
            repository=repository,
            before=before,
            after=after,
            limits=self.limits,
            round_cap=self.round_cap,
            progress_guard=self.progress_guard,
        )
        evidence = () if judged.evidence is None else (judged.evidence,)
        return Gathered(evidence, judged.inconclusive)

    def _verify_by_reading(
        self, claim: str, model: Model, repository: Path, before: str, after: str
    ) -> Gathered:
        """Step 4 of `gather_within` for one Structural Claim: what it is given, with every
        request made to `model` and nothing run."""
        verified = verify_structural_claim(
            claim,
            repository=repository,
            before=before,
            after=after,
            model=model,
            progress_guard=self.progress_guard,
        )
        return Gathered(verified.evidence, verified.inconclusive)

    def _prepare(
        self, repository: Path, before: str, after: str, sandbox_for: SandboxFor
    ) -> _Ready | str:
        """Steps 1 to 3 of `gather`: what a Reproduction is run with, or why none can be run.
        Each Sandbox is the one `sandbox_for` gives for its image, and closing it is left to
        the caller."""
        found = self.discover(repository, before)
        if isinstance(found, Unsupported):
            return found.reason
        if not isinstance(found.ecosystem, ReproductionEcosystem):
            return NO_REPRODUCTION_ECOSYSTEM
        try:
            target = prepare_target(
                repository,
                before,
                store=self.store,
                cache=self.cache,
                sandbox_for=sandbox_for,
                limits=self.limits,
                discover=self.discover,
            )
        except SandboxFailed as failure:
            return COULD_NOT_PREPARE.format(commit="before", failure=failure)
        if isinstance(target, Unsupported):
            return target.reason
        if isinstance(target, CommandFailed):
            return COULD_NOT_PREPARE.format(commit="before", failure=_failure(target))
        try:
            failed = prepare_pull_request(
                repository,
                after,
                profile=target.profile,
                sandbox=target.sandbox,
                cache=self.cache,
                limits=self.limits,
            )
        except SandboxFailed as failure:
            return COULD_NOT_PREPARE.format(commit="after", failure=failure)
        if failed is not None:
            return COULD_NOT_PREPARE.format(commit="after", failure=_failure(failed))
        return _Ready(target.sandbox, found.ecosystem)


def _failure(failed: CommandFailed) -> str:
    """The `failure` of `COULD_NOT_PREPARE` for a command that did not work."""
    return f"the command {' '.join(failed.command)} ended with {ending(failed.run)}"


def fixed_pipeline(state: Path, *, progress_guard: Decider | None = None) -> FixedPipeline:
    """The fixed pipeline that keeps what it remembers between runs under `state`.

    `state` need not exist yet. The Repo Profile is kept by a `DirectoryStore` on
    `state / "profiles"` and installed dependencies by a `DependencyCache` on
    `state / "dependencies"`. Its `progress_guard` is the one given. Everything else is as
    `FixedPipeline` has it unless given.
    """
    return FixedPipeline(
        store=DirectoryStore(state / "profiles"),
        cache=DependencyCache(state / "dependencies"),
        progress_guard=progress_guard,
    )


class OrchestrationChoice(StrEnum):
    """Which implementation of the orchestration boundary a run uses."""

    PIPELINE = "pipeline"
    """The fixed pipeline."""

    SUPERVISOR = "supervisor"
    """The LLM supervisor, of `pr_verification.supervisor`."""


ORCHESTRATION_VARIABLE = "PR_VERIFICATION_ORCHESTRATION"


class InvalidOrchestration(Exception):
    """The orchestration is configured as something it cannot be. Its message is safe to
    print: it names the variable at fault."""


def orchestration_choice(environment: Mapping[str, str]) -> OrchestrationChoice:
    """Read which orchestration a run uses from `PR_VERIFICATION_ORCHESTRATION`.

    `pipeline` is the fixed pipeline and `supervisor` is the LLM supervisor. Unset, or set to
    the empty string, it is the fixed pipeline: the supervisor is chosen by configuration,
    and the fixed pipeline stays the default until the supervisor has been measured against
    it.

    Raises `InvalidOrchestration`, with a message that names the variable, when it is set to
    anything else. The two values are matched exactly as written.

    The command line reads it. The GitHub Action does not yet: it uses the fixed pipeline
    whatever the variable says.
    """
    raise NotImplementedError
