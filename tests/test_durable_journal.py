"""The Journal of a durable run, and the core's code replayed against it, with no Temporal."""

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from durable_stand_ins import (
    SCRIPTS,
    NamedSandboxes,
    a_decider,
    a_model,
    a_pull_request,
    a_repository,
    driven,
    made_by,
    on_one_machine,
    resources,
)
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from test_pipeline import REJECTS
from test_replay import ClaimScript, claim_scripts

from pr_verification.budget import Budget
from pr_verification.context_bound import ContextLimits, shown_within
from pr_verification.durable.activities import Activities
from pr_verification.durable.journal import (
    Answer,
    Capabilities,
    ClaimInput,
    Effect,
    EffectKind,
    JournalMismatch,
    NeedsEffect,
    Replay,
    RunInput,
    effect_id,
    replay_run,
    sandbox_name,
)
from pr_verification.model import ModelFailed, ModelRequest, RoleName
from pr_verification.report import ClassifiedClaim

NAME = re.compile(r"[a-z0-9-]+")

effects = st.builds(
    Effect,
    kind=st.sampled_from(sorted(EffectKind)),
    asked=st.tuples(st.text(max_size=20), st.integers()),
)
answers = st.builds(
    Answer, value=st.text(max_size=20), seconds=st.floats(min_value=0.0, max_value=1e6)
)
journals = st.lists(st.tuples(effects, answers), max_size=8)


# Asking


@given(journals, effects)
def test_replay_gives_each_answer_in_order_and_then_needs_the_next_effect(
    journal: list[tuple[Effect, Answer]], next_effect: Effect
) -> None:
    replay = Replay(journal)

    given_back = [replay.made(effect) for effect, _ in journal]
    with pytest.raises(NeedsEffect) as needed:
        replay.made(next_effect)

    assert given_back == [answer.value for _, answer in journal]
    assert replay.used == len(journal)
    assert (needed.value.place, needed.value.effect) == (len(journal), next_effect)
    assert replay.needed is needed.value


@given(journals, effects, st.lists(effects, min_size=1, max_size=4))
def test_once_an_effect_is_needed_every_later_effect_raises_the_same_need(
    journal: list[tuple[Effect, Answer]], first: Effect, later: list[Effect]
) -> None:
    replay = Replay(journal)
    for effect, _ in journal:
        replay.made(effect)
    with pytest.raises(NeedsEffect) as needed:
        replay.made(first)

    for effect in later:
        with pytest.raises(NeedsEffect) as again:
            replay.made(effect)
        assert again.value is needed.value


def test_effect_asked_while_a_need_unwinds_the_code_does_not_take_its_place() -> None:
    asked = Effect(EffectKind.MODEL, (None, ModelRequest("instructions", "data")))
    closing = Effect(EffectKind.HOME_SAVE, ("a-sandbox", Path("r"), "image", "commit", Path("a")))
    replay = Replay([])

    with pytest.raises(NeedsEffect) as needed:
        try:
            replay.made(asked)
        finally:
            replay.made(closing)

    assert needed.value.effect == asked


def test_answer_that_raised_is_raised_again_where_the_effect_is_asked() -> None:
    effect = Effect(EffectKind.MODEL, (RoleName.REPRODUCTION_WRITER, ModelRequest("i", "d")))
    failed = ModelFailed("the model could not be reached")

    with pytest.raises(ModelFailed) as raised:
        Replay([(effect, Answer(raised=failed))]).made(effect)

    assert raised.value is failed


def test_other_effect_than_the_journal_holds_at_a_place_is_a_mismatch() -> None:
    held = Effect(EffectKind.TOKEN, (16,))

    with pytest.raises(JournalMismatch):
        Replay([(held, Answer("a" * 32))]).made(Effect(EffectKind.TOKEN, (8,)))


def test_need_for_an_effect_is_no_exception_so_no_handler_of_exceptions_takes_it() -> None:
    assert issubclass(NeedsEffect, BaseException)
    assert not issubclass(NeedsEffect, Exception)


@given(journals, st.integers(min_value=0, max_value=8))
def test_clock_is_the_seconds_of_the_answers_given_so_far(
    journal: list[tuple[Effect, Answer]], given_so_far: int
) -> None:
    given_so_far = min(given_so_far, len(journal))
    replay = Replay(journal)
    assert replay.clock() == 0.0

    readings = []
    for effect, _ in journal[:given_so_far]:
        replay.made(effect)
        readings.append(replay.clock())

    assert replay.clock() == pytest.approx(sum(a.seconds for _, a in journal[:given_so_far]))
    assert readings == sorted(readings)


# Names


@given(st.integers(min_value=0, max_value=10_000), effects, effects)
def test_activity_id_holds_the_place_the_kind_and_a_digest_of_what_was_asked(
    place: int, effect: Effect, other: Effect
) -> None:
    made = effect_id(place, effect)

    assert NAME.fullmatch(made)
    assert made.startswith(f"{place}-{effect.kind.value}-")
    assert effect_id(place, Effect(effect.kind, tuple(effect.asked))) == made
    if other.kind is effect.kind and other.asked != effect.asked:
        assert effect_id(place, other) != made


def test_activity_id_differs_for_a_request_that_differs_in_one_character() -> None:
    request = ModelRequest("Write a Reproduction.", "the Claim")
    changed = ModelRequest("Write a Reproduction!", "the Claim")

    assert effect_id(3, Effect(EffectKind.MODEL, (None, request))) != effect_id(
        3, Effect(EffectKind.MODEL, (None, changed))
    )


def test_activity_id_is_the_same_in_every_process() -> None:
    effect = Effect(
        EffectKind.CLAIM,
        (
            Capabilities(parallel_tool_calls=(RoleName.SUPERVISOR,), decider=True),
            {"environment": "set"},
            frozenset({"one", "two", "three"}),
        ),
    )
    program = (
        "from pr_verification.durable.journal import Capabilities, Effect, EffectKind, effect_id\n"
        "from pr_verification.model import RoleName\n"
        "print(effect_id(5, Effect(EffectKind.CLAIM, (Capabilities(parallel_tool_calls="
        "(RoleName.SUPERVISOR,), decider=True), {'environment': 'set'}, "
        "frozenset({'one', 'two', 'three'})))))"
    )

    made = {
        subprocess.run(
            [sys.executable, "-c", program],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        for seed in ("0", "1", "2", "3")
    }

    assert made == {effect_id(5, effect)}


@given(st.from_regex(NAME, fullmatch=True), st.integers(0, 50), st.integers(0, 50))
def test_each_sandbox_a_run_opens_has_a_name_of_its_own(run: str, place: int, other: int) -> None:
    assert NAME.fullmatch(sandbox_name(run, place))
    assert sandbox_name(run, place) == sandbox_name(run, place)
    if other != place:
        assert sandbox_name(run, other) != sandbox_name(run, place)


# The run replayed


@settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(
    st.lists(claim_scripts, min_size=1, max_size=3),
    st.sampled_from([Budget(), Budget(steps=4), Budget(steps=12)]),
)
def test_run_replayed_with_the_answers_of_a_worker_gives_the_report_of_a_run_on_one_machine(
    scripts: list[ClaimScript], budget: Budget
) -> None:
    with tempfile.TemporaryDirectory() as kept:
        directory = Path(kept)
        repository = a_repository(directory)
        expected = on_one_machine(directory / "here", repository, scripts, budget=budget)
        worker = resources(
            directory / "worker", a_model(scripts), a_decider(scripts), NamedSandboxes(repository)
        )

        report = driven(
            replay_run,
            RunInput("pr-verification-test", a_pull_request(repository, scripts), budget),
            made_by(Activities(worker)),
        )

    assert report == expected


def test_run_asks_first_what_the_worker_offers_then_works_on_each_claim_in_its_own_effect(
    tmp_path: Path,
) -> None:
    repository = a_repository(tmp_path)
    worker = resources(
        tmp_path / "worker", a_model(SCRIPTS), a_decider(SCRIPTS), NamedSandboxes(repository)
    )
    journal: list[tuple[Effect, Answer]] = []

    driven(
        replay_run,
        RunInput("pr-verification-test", a_pull_request(repository, SCRIPTS)),
        made_by(Activities(worker)),
        journal,
    )

    assert journal[0][0] == Effect(EffectKind.CAPABILITIES, ())
    asked = [effect.asked[0] for effect, _ in journal if effect.kind is EffectKind.CLAIM]
    assert all(isinstance(claim, ClaimInput) for claim in asked)
    claims = [claim for claim in asked if isinstance(claim, ClaimInput)]
    assert [claim.place for claim in claims] == list(range(len(SCRIPTS)))
    assert [claim.claim for claim in claims] == [f"Claim {p} holds" for p in range(len(SCRIPTS))]
    assert all(claim.run == "pr-verification-test" for claim in claims)


def test_worker_with_no_decider_gives_a_run_with_no_decider(tmp_path: Path) -> None:
    repository = a_repository(tmp_path)
    expected = on_one_machine(tmp_path / "here", repository, SCRIPTS, with_decider=False)
    worker = resources(tmp_path / "worker", a_model(SCRIPTS), None, NamedSandboxes(repository))

    report = driven(
        replay_run,
        RunInput("pr-verification-test", a_pull_request(repository, SCRIPTS)),
        made_by(Activities(worker)),
    )

    assert report == expected
    assert not any(isinstance(claim, ClassifiedClaim) for claim in report.claims)


def test_what_a_role_is_shown_is_bounded_by_the_limits_in_the_input(tmp_path: Path) -> None:
    scripts = [ClaimScript(SCRIPTS[0].response, REJECTS, "behavioural")]
    small = ContextLimits(tool_result=200, sandbox_output=200)
    repository = a_repository(tmp_path)
    with shown_within(small):
        expected = on_one_machine(tmp_path / "here", repository, scripts)
    model = a_model(scripts)
    worker = resources(tmp_path / "worker", model, a_decider(scripts), NamedSandboxes(repository))

    report = driven(
        replay_run,
        RunInput("pr-verification-test", a_pull_request(repository, scripts), shown=small),
        made_by(Activities(worker)),
    )

    assert report == expected
