"""The work on each Claim of the fixed pipeline, done in place or handed to whoever is given."""

import dataclasses
from pathlib import Path

import pytest
from conftest import Clock, FixtureRepository
from test_pipeline import (
    THREE,
    Discovery,
    ReadsResultsOnly,
    Sandboxes,
    WritesFor,
    a_pipeline,
    commands,
    gather_within,
    shop,
)

from pr_verification.budget import BUDGET_SPENT, Budget, BudgetSpent, Ledger
from pr_verification.model import Model
from pr_verification.orchestration import Gathered, Ready
from pr_verification.report import BudgetLimit, BudgetSpend, ClaimKind

__all__ = ["shop"]


class HandsOn:
    """Records each Claim's work it is asked for, and gives what is prepared for its place, or
    raises it. Spends what is prepared for its place against the ledger first."""

    def __init__(
        self,
        answers: dict[int, Gathered | BudgetSpent],
        spends: dict[int, tuple[BudgetSpend, int | None]] | None = None,
    ) -> None:
        self._answers, self._spends = answers, spends or {}
        self.asked: list[tuple[object, ...]] = []

    def __call__(
        self,
        ledger: Ledger,
        place: int,
        claim: str,
        kind: ClaimKind | None,
        ready: Ready | None,
        *,
        repository: Path,
        before: str,
        after: str,
        model: Model,
    ) -> Gathered:
        self.asked.append((ledger, place, claim, kind, ready, repository, before, after, model))
        if place in self._spends:
            spend, claim_spent = self._spends[place]
            ledger.record(spend, claim=claim_spent)
        answer = self._answers[place]
        if isinstance(answer, BudgetSpent):
            raise answer
        return answer


def given(evidence_of: str) -> Gathered:
    return Gathered((), evidence_of)


def test_work_on_each_claim_is_asked_of_whoever_is_given_once_and_what_it_gives_is_given(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(clock=Clock())
    hands = HandsOn({place: given(f"answer {place}") for place in range(3)})
    kinds = (ClaimKind.BEHAVIOURAL, None, ClaimKind.STRUCTURAL)
    pipeline = dataclasses.replace(
        a_pipeline(tmp_path / "state", Sandboxes(shop)), work_on_claim=hands
    )

    gathered = pipeline.gather_within(
        ledger,
        model.claims,
        repository=shop.path,
        before=shop.before,
        after=shop.after,
        model=model,
        kinds=kinds,
    )

    assert gathered == tuple(given(f"answer {place}") for place in range(3))
    assert [asked[1:4] for asked in hands.asked] == [
        (place, claim, kind)
        for place, (claim, kind) in enumerate(zip(model.claims, kinds, strict=True))
    ]
    for asked in hands.asked:
        assert asked[0] is ledger
        assert asked[5:] == (shop.path, shop.before, shop.after, model)
        ready = asked[4]
        assert isinstance(ready, Ready)
    # Only the Claim extractor's requests would be the model's: the work was handed on.
    assert model.requests == []


def test_claims_are_given_no_ready_sandbox_when_none_could_be_prepared(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(clock=Clock())
    hands = HandsOn({2: given("read")})
    pipeline = dataclasses.replace(
        a_pipeline(tmp_path / "state", Sandboxes(shop)),
        discover=Discovery(commands(ReadsResultsOnly())),
        work_on_claim=hands,
    )

    gathered = pipeline.gather_within(
        ledger,
        model.claims,
        repository=shop.path,
        before=shop.before,
        after=shop.after,
        model=model,
        kinds=(ClaimKind.BEHAVIOURAL, ClaimKind.BEHAVIOURAL, ClaimKind.STRUCTURAL),
    )

    assert [asked[1] for asked in hands.asked] == [2]
    assert hands.asked[0][4] is None
    assert gathered[2] == given("read")


def test_share_spent_in_the_work_handed_on_ends_that_claim_and_the_next_is_worked_on(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(clock=Clock())
    hands = HandsOn(
        {0: given("first"), 1: BudgetSpent(BudgetLimit.STEPS, claim=1), 2: given("third")}
    )
    pipeline = dataclasses.replace(
        a_pipeline(tmp_path / "state", Sandboxes(shop)), work_on_claim=hands
    )

    gathered = gather_within(pipeline, ledger, shop, model)

    assert gathered == [
        given("first"),
        Gathered((), str(BudgetSpent(BudgetLimit.STEPS, claim=1))),
        given("third"),
    ]


def test_budget_spent_in_the_work_handed_on_leaves_every_claim_not_yet_answered_for(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(Budget(steps=50), clock=Clock())
    hands = HandsOn(
        {0: given("first"), 1: BudgetSpent(BudgetLimit.STEPS)},
        spends={1: (BudgetSpend(seconds=0.0, dollars=0.0, steps=50), None)},
    )
    pipeline = dataclasses.replace(
        a_pipeline(tmp_path / "state", Sandboxes(shop)), work_on_claim=hands
    )

    gathered = gather_within(pipeline, ledger, shop, model)

    spent = Gathered((), BUDGET_SPENT.format(limit=BudgetLimit.STEPS.value))
    assert gathered == [given("first"), spent, spent]
    assert [asked[1] for asked in hands.asked] == [0, 1]


@pytest.mark.parametrize(
    "kinds",
    [None, (ClaimKind.BEHAVIOURAL, ClaimKind.STRUCTURAL, None)],
    ids=["unclassified", "classified"],
)
def test_work_done_in_place_by_name_is_the_work_done_when_no_one_is_given(
    shop: FixtureRepository, tmp_path: Path, kinds: tuple[ClaimKind | None, ...] | None
) -> None:
    def gathered(directory: str, by_name: bool) -> tuple[tuple[Gathered, ...], object]:
        model, ledger = WritesFor(THREE), Ledger(clock=Clock())
        pipeline = a_pipeline(tmp_path / directory, Sandboxes(shop))
        if by_name:
            pipeline = dataclasses.replace(pipeline, work_on_claim=pipeline.work_in_place)
        made = pipeline.gather_within(
            ledger,
            model.claims,
            repository=shop.path,
            before=shop.before,
            after=shop.after,
            model=model,
            kinds=kinds,
        )
        return made, (ledger.snapshot(), [request.data for request in model.requests])

    assert gathered("by-name", True) == gathered("unnamed", False)


def test_work_in_place_records_every_request_against_the_claim_at_its_place(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(clock=Clock())
    ledger.share_out(3)
    pipeline = a_pipeline(tmp_path / "state", Sandboxes(shop))

    pipeline.work_in_place(
        ledger,
        1,
        model.claims[1],
        ClaimKind.STRUCTURAL,
        None,
        repository=shop.path,
        before=shop.before,
        after=shop.after,
        model=model,
    )

    assert ledger.spent(claim=1).steps == len(model.requests) > 0
    assert ledger.spent(claim=0).steps == ledger.spent(claim=2).steps == 0


def test_work_in_place_toward_a_reproduction_with_nothing_prepared_is_refused(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    model, ledger = WritesFor(THREE), Ledger(clock=Clock())
    ledger.share_out(3)

    with pytest.raises(ValueError):
        a_pipeline(tmp_path / "state", Sandboxes(shop)).work_in_place(
            ledger,
            0,
            model.claims[0],
            ClaimKind.BEHAVIOURAL,
            None,
            repository=shop.path,
            before=shop.before,
            after=shop.after,
            model=model,
        )
