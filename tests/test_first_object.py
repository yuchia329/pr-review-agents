"""The first object of a response: what a run of a role reads, and what it leaves unread.

A model may go on after the one object it was asked for: with a code fence, with the same
object again, with a sentence. The finishes below that say so are the ones MiniMax M3 wrote
as the Reproduction writer (#66).
"""

import json

import pytest
from conftest import asks, finishes
from hypothesis import example, given
from hypothesis import strategies as st
from test_agent import (
    JSON,
    TASK,
    RecordingTool,
    back_and_forth,
    role_with,
    run_scripted,
    sent,
    took,
)

from pr_verification.agent import (
    NO_SUCH_TOOL,
    REPEAT_LENGTH,
    UNREADABLE_RESPONSE,
    LoopKind,
    Step,
    StepOutcome,
    Stopped,
    detect_repeat,
    run_role,
)
from pr_verification.model import ScriptedModel

REPRODUCTION = "def test_median():\n    assert median([1, 2, 3, 4]) == 2.5\n"
FINISH = finishes(reproduction=REPRODUCTION)
READS = asks("read_file", path="tax.py")
FENCE = "`" * 3


@pytest.mark.parametrize(
    "after",
    [
        "\n" + json.dumps({"reproduction": REPRODUCTION}),
        "\n" + FENCE,
        "\n\n" + FENCE + "\n" + FINISH,
        " That is the test I would write.",
        "\n" + finishes(reproduction="another"),
        "\n" + READS,
        "{",
    ],
    ids=[
        "a second object, as seen",
        "a closing code fence, as seen",
        "a code fence and the same finish again, as seen",
        "a sentence",
        "another finish",
        "a request for a tool",
        "an opening brace",
    ],
)
def test_finish_followed_by_anything_ends_the_run_with_the_answer_of_the_first_object(
    after: str,
) -> None:
    read = RecordingTool("read_file")

    run, model = run_scripted(role_with(read), FINISH + after, finishes(reproduction="too late"))

    assert run.steps == (Step(FINISH + after, StepOutcome.FINISHED, tool=None, result=""),)
    assert run.stopped is Stopped.FINISHED
    assert run.answer == {"reproduction": REPRODUCTION}
    assert read.calls == []
    assert len(model.requests) == 1


@pytest.mark.parametrize(
    "after",
    ["\n" + FENCE, "\n" + finishes(done=True), "\n" + asks("search", text="RATE"), " and then?"],
    ids=["a closing code fence", "a finish", "a request for another tool", "a sentence"],
)
def test_request_for_a_tool_followed_by_anything_runs_that_tool_once_and_nothing_else(
    after: str,
) -> None:
    read, search = RecordingTool("read_file", returns="RATE = 3"), RecordingTool("search")
    response = READS + after

    run, model = run_scripted(role_with(read, search), response, finishes(places=["tax.py"]))

    assert read.calls == [{"path": "tax.py"}]
    assert search.calls == []
    assert run.steps[0] == Step(response, StepOutcome.RAN_TOOL, tool="read_file", result="RATE = 3")
    assert run.answer == {"places": ["tax.py"]}
    assert len(model.requests) == 2


def test_role_is_shown_its_response_whole_and_is_not_told_that_part_of_it_was_left_unread() -> None:
    read = RecordingTool("read_file", returns="RATE = 3")
    response = READS + "\n" + FENCE

    _, model = run_scripted(role_with(read), response, finishes())

    assert sent(model.requests[1]) == {
        "task": TASK,
        "steps": [{"response": response, "result": "RATE = 3"}],
    }
    assert model.requests[1].instructions == model.requests[0].instructions


def test_tool_the_role_was_not_given_is_refused_whatever_follows_the_request() -> None:
    read = RecordingTool("read_file")
    response = asks("run_command", command="rm -rf /") + "\n" + READS

    run, _ = run_scripted(role_with(read), response, finishes())

    assert read.calls == []
    assert run.steps[0] == Step(response, StepOutcome.REFUSED, tool=None, result=NO_SUCH_TOOL)


@pytest.mark.parametrize("before", [" ", "\n", "\t", "\r\n", " \n\t "])
def test_whitespace_before_the_first_object_is_no_part_of_what_is_read(before: str) -> None:
    run, _ = run_scripted(role_with(), before + FINISH + "\n" + FENCE)

    assert run.stopped is Stopped.FINISHED
    assert run.answer == {"reproduction": REPRODUCTION}


@pytest.mark.parametrize(
    "response",
    [
        FENCE + "json\n" + FINISH + "\n" + FENCE,
        "Here is the test: " + FINISH,
        "\N{NO-BREAK SPACE}" + FINISH,
        FINISH[:-1],
        FINISH[:-1] + "\n" + FINISH,
        "[1]\n" + FINISH,
        "7 " + FINISH,
        '"finish" ' + FINISH,
        json.dumps({"reason": "to look"}) + "\n" + FINISH,
        json.dumps({"tool": "read_file", "finish": {}}) + "\n" + FINISH,
        json.dumps({"finish": "done"}) + "\n" + FINISH,
        json.dumps({"tool": "read_file", "arguments": ["tax.py"]}) + "\n" + READS,
    ],
    ids=[
        "a code fence before the object",
        "a sentence before the object",
        "a space that JSON does not have as whitespace before the object",
        "an object that is never closed",
        "an object that is never closed, then a finish",
        "a list, then a finish",
        "a number, then a finish",
        "a string, then a finish",
        "an object with another member, then a finish",
        "an object with both a tool and a finish, then a finish",
        "a finish that is not an object, then a finish",
        "arguments that are not an object, then a request that can be read",
    ],
)
def test_response_whose_beginning_cannot_be_read_is_unreadable_whatever_follows(
    response: str,
) -> None:
    read = RecordingTool("read_file")

    run, model = run_scripted(role_with(read), response, finishes(done=True))

    assert read.calls == []
    assert run.steps[0] == Step(response, StepOutcome.UNREADABLE, None, UNREADABLE_RESPONSE)
    assert run.answer == {"done": True}
    assert len(model.requests) == 2


def test_request_for_a_tool_named_finish_is_a_request_for_a_tool_and_no_finish() -> None:
    read = RecordingTool("read_file")
    response = asks("finish", reproduction=REPRODUCTION) + "\n" + FENCE

    run, model = run_scripted(role_with(read), response, finishes(done=True))

    assert run.steps[0] == Step(response, StepOutcome.REFUSED, tool=None, result=NO_SUCH_TOOL)
    assert run.answer == {"done": True}
    assert len(model.requests) == 2


def test_role_given_a_tool_named_finish_has_it_run_and_the_run_goes_on() -> None:
    finish = RecordingTool("finish", returns="noted")
    response = asks("finish", reproduction=REPRODUCTION)

    run, _ = run_scripted(role_with(finish), response, finishes(done=True))

    assert finish.calls == [{"reproduction": REPRODUCTION}]
    assert run.steps[0] == Step(response, StepOutcome.RAN_TOOL, tool="finish", result="noted")
    assert run.answer == {"done": True}


REQUESTS = st.one_of(
    st.builds(
        lambda name, arguments: {"tool": name, "arguments": arguments},
        st.sampled_from(["given", "withheld"]),
        st.dictionaries(st.text(), JSON, max_size=2),
    ),
    st.sampled_from(["given", "withheld"]).map(lambda name: {"tool": name}),
    st.dictionaries(st.text(), JSON, max_size=2).map(lambda answer: {"finish": answer}),
)
"""A response that can be read, as the object it is."""


@given(
    request=REQUESTS,
    indent=st.sampled_from([None, 0, 2]),
    before=st.text(" \t\n\r", max_size=3),
    after=st.text(),
)
@example(request={"finish": {}}, indent=None, before="", after="}")
@example(request={"tool": "given"}, indent=None, before="", after=',"finish":{}}')
@example(request={"finish": {}}, indent=None, before="", after="\x00")
def test_whatever_follows_the_first_object_the_step_is_the_one_the_object_alone_makes(
    request: dict[str, object], indent: int | None, before: str, after: str
) -> None:
    alone = json.dumps(request, indent=indent)
    followed = before + alone + after

    def taken(response: str) -> tuple[object, ...]:
        given_tool = RecordingTool("given")
        run = run_role(role_with(given_tool, step_cap=1), TASK, ScriptedModel([response]))
        (step,) = run.steps
        assert step.response == response
        return (step.outcome, step.tool, step.result, run.stopped, run.answer, given_tool.calls)

    assert taken(followed) == taken(alone)


def test_several_tools_followed_by_anything_are_each_run_when_the_model_offers_them() -> None:
    read = RecordingTool("read_file", returns="RATE = 3")
    asked = [{"tool": "read_file", "arguments": {"path": path}} for path in ("tax.py", "rate.py")]
    response = json.dumps({"tools": asked}) + "\n" + FENCE
    model = ScriptedModel([response, finishes()], parallel_tool_calls=True)

    run = run_role(role_with(read), TASK, model)

    assert read.calls == [{"path": "tax.py"}, {"path": "rate.py"}]
    assert (run.steps[0].response, run.steps[0].outcome) == (response, StepOutcome.RAN_TOOLS)


def test_several_tools_followed_by_anything_are_unreadable_when_the_model_does_not_offer_them() -> (
    None
):
    read = RecordingTool("read_file")
    response = json.dumps({"tools": [{"tool": "read_file"}]}) + "\n" + FENCE

    run, _ = run_scripted(role_with(read), response, finishes())

    assert read.calls == []
    assert run.steps[0] == Step(response, StepOutcome.UNREADABLE, None, UNREADABLE_RESPONSE)


@pytest.mark.parametrize(
    ("one", "other"),
    [
        (READS + "\n" + FENCE, READS),
        (READS + " first", READS + " second"),
        (READS + "\n" + FENCE, json.dumps(json.loads(READS), indent=2) + "\n" + FENCE),
    ],
    ids=[
        "one goes on after the object and the other does not",
        "they go on differently",
        "they go on alike and differ in the whitespace within the object",
    ],
)
def test_responses_that_begin_with_the_same_object_are_the_same_action_only_with_the_same_text(
    one: str, other: str
) -> None:
    assert detect_repeat([took(one)] * REPEAT_LENGTH) is LoopKind.EXACT_REPEAT
    assert detect_repeat([*[took(one)] * (REPEAT_LENGTH - 1), took(other)]) is None
    assert detect_repeat(back_and_forth(took(one), took(other))) is LoopKind.ALTERNATION
