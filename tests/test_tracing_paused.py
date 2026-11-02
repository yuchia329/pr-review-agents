"""Pausing a recording: work done inside `paused` is not recorded, on that thread alone."""

import contextvars
import threading

import pytest

from pr_verification.claims import classify_claim
from pr_verification.decision import ScriptedDecider
from pr_verification.report import ClaimKind
from pr_verification.tracing import SpanKind, paused, record, span


def classify(times: int = 1) -> None:
    """Classify a Claim `times` times: each is one span of its own when recorded."""
    decider = ScriptedDecider(["behavioural"] * times)
    for _ in range(times):
        assert classify_claim(decider, "Fixes the crash") is ClaimKind.BEHAVIOURAL


def test_work_done_while_paused_is_not_recorded_and_work_before_and_after_is() -> None:
    with record() as recording:
        classify()
        with paused():
            classify(3)
        classify()

    assert len(recording.trace().spans) == 2


def test_span_opened_while_paused_gives_attributes_that_are_dropped() -> None:
    with record() as recording, paused(), span(SpanKind.MODEL_CALL, "asked") as attributes:
        attributes["dropped"] = "yes"

    assert recording.trace().spans == ()


def test_recording_goes_on_however_the_pause_is_left() -> None:
    with record() as recording:
        with pytest.raises(KeyError), paused():
            raise KeyError("left by an error")
        classify()

    assert len(recording.trace().spans) == 1


def test_pause_inside_another_leaves_the_outer_one_paused() -> None:
    with record() as recording:
        with paused():
            with paused():
                pass
            classify()

    assert recording.trace().spans == ()


def test_work_after_a_pause_inside_an_open_span_is_a_child_of_that_span() -> None:
    with record() as recording:
        with span(SpanKind.MODEL_CALL, "outer"):
            with paused():
                classify()
            classify()

    inner, outer = recording.trace().spans
    assert outer.name == "outer"
    assert inner.parent_id == outer.span_id


def test_thread_started_with_a_copy_of_the_context_is_paused_too() -> None:
    with record() as recording:
        with paused():
            context = contextvars.copy_context()
        thread = threading.Thread(target=context.run, args=(classify,))
        thread.start()
        thread.join()

    assert recording.trace().spans == ()


def test_pausing_one_thread_pauses_no_other() -> None:
    entered, done = threading.Event(), threading.Event()

    def paused_meanwhile() -> None:
        with paused():
            entered.set()
            done.wait(timeout=10)

    with record() as recording:
        thread = threading.Thread(target=paused_meanwhile)
        thread.start()
        entered.wait(timeout=10)
        try:
            classify()
        finally:
            done.set()
            thread.join()

    assert len(recording.trace().spans) == 1
