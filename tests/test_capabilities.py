"""The two optional capabilities of the model interface: parallel tool calls and prompt
caching, and what a model that offers neither is left with.

Every model is a scripted stand-in, or one of the two clients over a stand-in for its provider.
"""

import json
from collections.abc import Callable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

import httpx
import pytest
from conftest import FixtureRepository, UnreachableModel, asks, commit_snapshots, finishes
from hypothesis import given
from hypothesis import strategies as st
from test_pipeline import NAMES_BOTH, RENAMES, THREE, WritesFor, renaming
from test_supervisor import DECIDED, FINISH, decided, dispatch, verify

from pr_verification.agent import (
    NO_SUCH_TOOL,
    PARALLEL_CALL_CAP,
    UNREADABLE_RESPONSE,
    Role,
    RoleRun,
    Step,
    StepOutcome,
    Stopped,
    Tool,
    run_role,
)
from pr_verification.anthropic_protocol import AnthropicModel
from pr_verification.budget import Ledger
from pr_verification.model import (
    Model,
    ModelRequest,
    PricedResponse,
    ScriptedModel,
    offers_parallel_tool_calls,
)
from pr_verification.openai_compatible import OpenAICompatibleModel

ENDPOINT = "https://provider.example/v1"
TASK = {"claim": "Fixes the tax on orders sent abroad"}
REQUEST = ModelRequest(instructions="List the Claims.", data='{"title": "Tidy up"}')
WORTH_KEEPING = ModelRequest(REQUEST.instructions, REQUEST.data, cache_instructions=True)


class Says:
    """A stand-in that passes each request on to a model, and says of parallel tool calls
    only what it is made to say: with nothing given it has no such attribute."""

    def __init__(self, model: Model, **says: object) -> None:
        self._model = model
        vars(self).update(says)

    def complete(self, request: ModelRequest) -> str:
        return self._model.complete(request)


# Who offers parallel tool calls


def test_model_offers_parallel_tool_calls_by_an_attribute_of_that_name_that_is_true() -> None:
    assert offers_parallel_tool_calls(ScriptedModel([], parallel_tool_calls=True))
    assert offers_parallel_tool_calls(Says(ScriptedModel([]), parallel_tool_calls=True))
    assert not offers_parallel_tool_calls(ScriptedModel([]))
    assert not offers_parallel_tool_calls(Says(ScriptedModel([])))
    assert not offers_parallel_tool_calls(UnreachableModel())


@given(st.none() | st.just(False) | st.integers() | st.floats() | st.text() | st.lists(st.just(1)))
def test_for_anything_but_true_itself_in_the_attribute_the_model_does_not_offer_them(
    said: object,
) -> None:
    assert not offers_parallel_tool_calls(Says(ScriptedModel([]), parallel_tool_calls=said))


@pytest.mark.parametrize("client", [OpenAICompatibleModel, AnthropicModel])
def test_either_client_offers_parallel_tool_calls_only_when_it_is_made_to(
    client: Callable[..., Model],
) -> None:
    assert offers_parallel_tool_calls(client(ENDPOINT, "some-model", "", parallel_tool_calls=True))
    assert not offers_parallel_tool_calls(client(ENDPOINT, "some-model", ""))


@pytest.mark.parametrize("offered", [True, False], ids=["offered", "not offered"])
def test_model_asked_through_the_ledger_offers_what_its_model_does_and_is_sent_requests_as_given(
    offered: bool,
) -> None:
    model = ScriptedModel(["one", "two"], parallel_tool_calls=offered)
    ledger = Ledger()
    ledger.share_out(1)

    for_no_claim, for_a_claim = ledger.model(model), ledger.model(model, claim=0)

    assert offers_parallel_tool_calls(for_no_claim) is offered
    assert offers_parallel_tool_calls(for_a_claim) is offered
    assert (for_no_claim.complete(WORTH_KEEPING), for_a_claim.complete(REQUEST)) == ("one", "two")
    assert model.requests == [WORTH_KEEPING, REQUEST]


# Several tools in one step


class Kit:
    """Tools that each return a text of their own, and record in one list the order they
    were run in."""

    def __init__(self, *names: str) -> None:
        self.ran: list[tuple[str, Mapping[str, object]]] = []
        self.tools = tuple(
            Tool(name, f"What {name} does.", partial(self._run, name)) for name in names
        )

    def _run(self, name: str, arguments: Mapping[str, object]) -> str:
        self.ran.append((name, arguments))
        return f"{name} returned"

    def role(self, step_cap: int = 5) -> Role:
        return Role("reader", "Find the code.", self.tools, step_cap)


def call(tool: str, **arguments: object) -> dict[str, object]:
    """One entry of a response that asks for several tools."""
    return {"tool": tool, "arguments": arguments}


def several(*entries: object) -> str:
    """A response in which a role asks for several tools at once."""
    return json.dumps({"tools": entries})


def run_offering(role: Role, *responses: str) -> tuple[RoleRun, ScriptedModel]:
    """A run of the role on a model that offers parallel tool calls."""
    model = ScriptedModel(responses, parallel_tool_calls=True)
    return run_role(role, TASK, model), model


def shown(request: ModelRequest) -> Any:
    return json.loads(request.data)


def test_each_tool_asked_for_is_run_once_in_the_order_asked_and_it_is_all_one_step() -> None:
    kit = Kit("read_file", "search")
    asked = several(
        call("search", text="tax"), call("read_file", path="tax.py"), {"tool": "search"}
    )

    run, model = run_offering(kit.role(), asked, finishes(done=True))

    assert kit.ran == [
        ("search", {"text": "tax"}),
        ("read_file", {"path": "tax.py"}),
        ("search", {}),
    ]
    step = run.steps[0]
    assert (step.response, step.outcome, step.tool) == (asked, StepOutcome.RAN_TOOLS, None)
    assert step.tools == ("search", "read_file", "search")
    assert json.loads(step.result) == ["search returned", "read_file returned", "search returned"]
    assert (len(run.steps), len(model.requests), run.answer) == (2, 2, {"done": True})


def test_step_of_several_tools_counts_once_towards_the_step_cap() -> None:
    kit = Kit("read_file")
    full = [
        several(
            *[call("read_file", path=f"{step}/{place}.py") for place in range(PARALLEL_CALL_CAP)]
        )
        for step in range(9)
    ]

    run, model = run_offering(kit.role(step_cap=2), *full)

    assert (run.stopped, len(run.steps), len(model.requests)) == (Stopped.STEP_CAP, 2, 2)
    assert len(kit.ran) == 2 * PARALLEL_CALL_CAP


def test_when_any_tool_asked_for_was_not_given_none_is_run_and_the_step_is_refused() -> None:
    kit = Kit("read_file")
    asked = several(call("read_file", path="tax.py"), call("run_command", command="rm -rf /"))

    run, model = run_offering(kit.role(), asked, finishes())

    assert kit.ran == []
    assert run.steps[0] == Step(asked, StepOutcome.REFUSED, tool=None, result=NO_SUCH_TOOL)
    assert (run.stopped, len(model.requests)) == (Stopped.FINISHED, 2)


READ = call("read_file", path="tax.py")


@pytest.mark.parametrize(
    "response",
    [
        several(),
        several(*[READ] * (PARALLEL_CALL_CAP + 1)),
        json.dumps({"tools": READ}),
        json.dumps({"tools": "read_file"}),
        several("read_file"),
        several(READ | {"reason": "to look"}),
        several({"tool": 7}),
        several({"arguments": {"path": "tax.py"}}),
        several({"tool": "read_file", "arguments": ["tax.py"]}),
        several(READ, {"finish": {}}),
        several({"tool": "no_such_tool", "arguments": 5}),
        json.dumps({"tools": [READ], "tool": "read_file"}),
        json.dumps({"tools": [READ], "finish": {}}),
        json.dumps({"tools": [READ], "reason": "to look"}),
        json.dumps({"tools": [READ], "arguments": {"path": "tax.py"}}),
    ],
    ids=[
        "no entries",
        "more entries than the cap",
        "an object for the list",
        "a string for the list",
        "an entry that is a string",
        "an entry with another member",
        "a tool that is no string",
        "an entry with no tool",
        "arguments that are no object",
        "an entry that finishes",
        "malformed, for a tool that was not given",
        "beside one tool",
        "beside a finish",
        "beside another member",
        "beside arguments",
    ],
)
def test_response_that_asks_for_several_tools_in_any_other_form_cannot_be_read_and_runs_nothing(
    response: str,
) -> None:
    kit = Kit("read_file")

    run, model = run_offering(kit.role(), response, finishes(done=True))

    assert kit.ran == []
    assert run.steps[0] == Step(response, StepOutcome.UNREADABLE, None, UNREADABLE_RESPONSE)
    assert (run.answer, len(model.requests)) == ({"done": True}, 2)


def test_forms_that_ask_for_one_tool_and_that_finish_are_read_as_before() -> None:
    kit = Kit("read_file")
    asked = asks("read_file", path="tax.py")

    run, _ = run_offering(kit.role(), asked, finishes(places=["tax.py"]))

    assert run.steps == (
        Step(asked, StepOutcome.RAN_TOOL, tool="read_file", result="read_file returned"),
        Step(finishes(places=["tax.py"]), StepOutcome.FINISHED, tool=None, result=""),
    )


def test_only_a_model_that_offers_parallel_tool_calls_is_told_of_the_form_and_read_for_it() -> None:
    asked = several(READ)
    offered, fallback, silent = Kit("read_file"), Kit("read_file"), Kit("read_file")
    says_nothing = ScriptedModel([asked, finishes()])

    ran, offering = run_offering(offered.role(), asked, finishes())
    unread, not_offering = fallback.role(), ScriptedModel([asked, finishes()])
    not_read = run_role(unread, TASK, not_offering)
    run_role(silent.role(), TASK, Says(says_nothing))

    assert ran.steps[0].outcome is StepOutcome.RAN_TOOLS
    assert not_read.steps[0] == Step(asked, StepOutcome.UNREADABLE, None, UNREADABLE_RESPONSE)
    assert (fallback.ran, silent.ran) == ([], [])
    told = {request.instructions for request in offering.requests}
    plain = {request.instructions for request in (*not_offering.requests, *says_nothing.requests)}
    assert (len(told), len(plain)) == (1, 1)
    assert all('"tools"' in instructions for instructions in told)
    assert not any('"tools"' in instructions for instructions in plain)


class OffersAtFirst(ScriptedModel):
    """A scripted stand-in that offers parallel tool calls until it has answered once."""

    def complete(self, request: ModelRequest) -> str:
        response = super().complete(request)
        self.parallel_tool_calls = False
        return response


def test_whether_a_model_offers_parallel_tool_calls_is_read_once_before_the_first_request() -> None:
    kit = Kit("read_file")
    model = OffersAtFirst([asks("read_file", path="a.py"), several(READ)], parallel_tool_calls=True)

    run = run_role(kit.role(step_cap=2), TASK, model)

    assert [step.outcome for step in run.steps] == [StepOutcome.RAN_TOOL, StepOutcome.RAN_TOOLS]
    assert len({request.instructions for request in model.requests}) == 1


# Prompt caching, and a provider that offers neither


@pytest.fixture(scope="module")
def shop(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A repository with nothing of any Ecosystem in it, and a pull request that changes it."""
    path = tmp_path_factory.mktemp("capabilities") / "repository"
    before, after = commit_snapshots(
        path,
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net\n"},
        {"deps.lock": "left-pad 1.0\n", "total.txt": "net and tax\n"},
    )
    return FixtureRepository(path=path, before=before, after=after)


SUPERVISES = [dispatch(2), dispatch(0, 1), FINISH]
"""What a supervising model answers to have each Claim of `THREE` worked on, in three steps."""


def test_every_request_to_the_supervising_model_says_its_instructions_are_worth_keeping(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    supervising = ScriptedModel(SUPERVISES)

    report = verify(shop, WritesFor(THREE), supervising, tmp_path)

    assert decided(report) == DECIDED
    assert [request.cache_instructions for request in supervising.requests] == [True] * 3
    # Another pull request, with other Claims, is supervised with the very same instructions.
    another = ScriptedModel([dispatch(0, reading=[0]), FINISH])
    accounts = renaming(tmp_path / "repository")
    verify(accounts, WritesFor({RENAMES: NAMES_BOTH}), another, tmp_path / "other", "structural")
    assert len({request.instructions for request in supervising.requests + another.requests}) == 1


class Speaks:
    """Stands in for a provider that gives these texts in order, each as an answer of its
    protocol, and records what it was sent."""

    def __init__(self, texts: list[str], answer_of: Callable[[str], object]) -> None:
        self._texts, self._answer_of = iter(texts), answer_of
        self.bodies: list[Any] = []
        self.sent: list[tuple[str, tuple[tuple[str, str], ...], bytes]] = []
        """The address, the headers, and the body of each request, exactly as sent."""
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        self.sent.append((str(request.url), tuple(request.headers.items()), request.content))
        return httpx.Response(200, json=self._answer_of(next(self._texts)))


def test_supervisors_instructions_are_kept_where_a_provider_can_and_sent_as_before_where_not(
    shop: FixtureRepository, tmp_path: Path
) -> None:
    keeps = Speaks(SUPERVISES, lambda text: {"content": [{"type": "text", "text": text}]})
    cannot = Speaks(SUPERVISES, lambda text: {"choices": [{"message": {"content": text}}]})
    scripted = ScriptedModel(SUPERVISES)

    supervisors: tuple[Model, ...] = (
        AnthropicModel(ENDPOINT, "some-model", "", keeps.transport),
        OpenAICompatibleModel(ENDPOINT, "some-model", "", cannot.transport),
        scripted,
    )

    reports = [
        verify(shop, WritesFor(THREE), supervising, tmp_path / str(number))
        for number, supervising in enumerate(supervisors)
    ]

    assert [decided(report) for report in reports] == [DECIDED] * 3
    instructions = scripted.requests[0].instructions
    kept = {"type": "text", "text": instructions, "cache_control": {"type": "ephemeral"}}
    assert [body["system"] for body in keeps.bodies] == [[kept]] * 3
    assert cannot.bodies == [
        {
            "model": "some-model",
            "messages": [
                {"role": "system", "content": instructions},
                {"role": "user", "content": request.data},
            ],
        }
        for request in scripted.requests
    ]


def test_openai_compatible_client_sends_the_same_request_whatever_either_capability_is() -> None:
    sent = set()
    for parallel_tool_calls in (False, True):
        for request in (REQUEST, WORTH_KEEPING):
            provider = Speaks(["{}"], lambda text: {"choices": [{"message": {"content": text}}]})
            model = OpenAICompatibleModel(
                ENDPOINT,
                "some-model",
                "secret-key",
                provider.transport,
                parallel_tool_calls=parallel_tool_calls,
            )
            assert model.complete(request) == "{}"
            sent.update(provider.sent)

    assert len(sent) == 1


def test_anthropic_client_sends_the_same_request_whether_or_not_it_offers_parallel_tool_calls() -> (
    None
):
    sent = set()
    for parallel_tool_calls in (False, True):
        provider = Speaks(["{}"], lambda text: {"content": [{"type": "text", "text": text}]})
        model = AnthropicModel(
            ENDPOINT,
            "some-model",
            "secret-key",
            provider.transport,
            parallel_tool_calls=parallel_tool_calls,
        )
        assert model.complete(REQUEST) == "{}"
        sent.update(provider.sent)

    assert len(sent) == 1


def test_scripted_stand_in_records_requests_as_given_and_answers_the_same_whatever_it_offers() -> (
    None
):
    for offered in (False, True):
        model = ScriptedModel(["one", "two"], 0.25, parallel_tool_calls=offered)

        assert model.complete(WORTH_KEEPING) == "one"
        assert model.complete_priced(REQUEST) == PricedResponse("two", 0.25)
        assert model.requests == [WORTH_KEEPING, REQUEST]
