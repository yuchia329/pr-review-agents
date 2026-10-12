"""Traces: the spans a run records, what they carry, and where they are sent."""

import json
import re
import time
from collections.abc import Mapping

import httpx
import pytest
from conftest import (
    Collector,
    FixtureRepository,
    GathersNothing,
    UnreachableModel,
    asks,
    exported_spans,
    finishes,
    model_listing,
)
from hypothesis import given
from hypothesis import strategies as st

from pr_verification import cli
from pr_verification.agent import Role, Tool, run_role
from pr_verification.claims import ClaimExtractionFailed, classify_claim, extract_claims
from pr_verification.core import InvalidPullRequest, PullRequest, run
from pr_verification.decision import (
    Decision,
    Implementation,
    PassedOn,
    Question,
    ScriptedDecider,
    SmallLLMDecider,
)
from pr_verification.evidence import run_on_both_commits
from pr_verification.model import Model, ModelFailed, ModelRequest, ScriptedModel
from pr_verification.openai_compatible import OpenAICompatibleModel
from pr_verification.report import Report
from pr_verification.sandbox import Command, LimitHit, Limits, Phase, RunResult, SandboxFailed
from pr_verification.tracing import (
    ENDPOINT_VARIABLE,
    HEADERS_VARIABLE,
    SERVICE_NAME,
    Span,
    SpanKind,
    Trace,
    record,
    to_otlp,
    traced,
)

LIMITS = Limits(seconds=60.0, memory_bytes=512 * 1024 * 1024, processors=1.0)
COMMAND: Command = ("pytest", "tests/test_zq_command.py")
CLOCKS_AGREE_WITHIN = 1_000_000_000
"""How far, in nanoseconds, a span's times may lie from the test's own reading of the clock."""


def names(trace: Trace) -> list[tuple[SpanKind, str]]:
    return [(span.kind, span.name) for span in trace.spans]


def verify(
    repository: FixtureRepository, model: Model, decider: ScriptedDecider | None = None
) -> Report:
    """The report of the core's entry point for a pull request of the fixture repository."""
    pull_request = PullRequest(
        repository.path, repository.before, repository.after, title="Fix the zq-title crash"
    )
    return run(pull_request, model, decider, orchestration=GathersNothing())


class OnlyCompletes:
    """A stand-in model with no price: it can be asked through `complete` alone."""

    def __init__(self, *responses: str) -> None:
        self._model = ScriptedModel(responses)

    def complete(self, request: ModelRequest) -> str:
        return self._model.complete(request)


def reader(*tools: Tool, step_cap: int = 5) -> Role:
    return Role("reader", "Find the code.", tools, step_cap)


def returns(text: str) -> Tool:
    return Tool("read_file", "Reads a file.", lambda arguments: text)


class ScriptedSandbox:
    """A stand-in Sandbox that answers each commit as prepared."""

    def __init__(self, answers: Mapping[str, RunResult | SandboxFailed]) -> None:
        self._answers = answers

    def run(self, command: Command, *, commit: str, phase: Phase, limits: Limits) -> RunResult:
        answer = self._answers[commit]
        if isinstance(answer, SandboxFailed):
            raise answer
        return answer


def run_twice(sandbox: ScriptedSandbox, phase: Phase = Phase.TEST) -> None:
    run_on_both_commits(
        sandbox, COMMAND, before="before-commit", after="after-commit", phase=phase, limits=LIMITS
    )


RAN_TWICE = ScriptedSandbox(
    {
        "before-commit": RunResult("zq-output: 1 failed", exit_status=1, limit_hit=None),
        "after-commit": RunResult("zq-output", exit_status=None, limit_hit=LimitHit.TIME),
    }
)


# A recording


def test_recording_has_one_trace_id_of_32_hexadecimal_digits_that_no_other_recording_has() -> None:
    with record() as first:
        while_open = first.trace()
    with record() as second:
        pass

    assert re.fullmatch("[0-9a-f]{32}", while_open.trace_id)
    assert while_open.trace_id != "0" * 32
    assert first.trace().trace_id == while_open.trace_id
    assert second.trace().trace_id != while_open.trace_id
    assert while_open.spans == ()


def test_trace_holds_every_span_that_had_ended_when_it_was_asked_for() -> None:
    decider = ScriptedDecider(["behavioural", "structural", "behavioural"])

    with record() as recording:
        classify_claim(decider, "Fixes the crash")
        after_one = recording.trace()
        classify_claim(decider, "Renames parse_all")
    classify_claim(decider, "Adds a retry")

    assert len(after_one.spans) == 1
    assert len(recording.trace().spans) == 2, "work done after the recording closed is not added"
    assert recording.trace().spans[0] == after_one.spans[0]


def test_opening_a_recording_while_one_is_open_is_refused() -> None:
    with record(), pytest.raises(RuntimeError), record():
        pass


def test_recording_is_closed_however_the_with_is_left() -> None:
    with pytest.raises(KeyError), record():
        raise KeyError("left by an exception")

    with record() as recording:
        pass
    assert recording.trace().spans == ()


# What a run records


def test_run_is_one_span_with_the_claim_extractor_its_request_and_each_decision_under_it(
    fixture_repository: FixtureRepository,
) -> None:
    model = model_listing("Fixes the crash on empty input", "Adds a retry to upload")

    with record() as recording:
        verify(fixture_repository, model, ScriptedDecider(["behavioural", "behavioural"]))

    trace = recording.trace()
    assert names(trace) == [
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.ROLE, "claim-extractor"),
        (SpanKind.DECISION, "decision"),
        (SpanKind.DECISION, "decision"),
        (SpanKind.RUN, "pr-verification"),
    ]
    request, extractor, first, second, whole = trace.spans
    assert whole.parent_id is None
    assert request.parent_id == extractor.span_id
    assert [extractor.parent_id, first.parent_id, second.parent_id] == [whole.span_id] * 3
    assert not any(span.failed for span in trace.spans)


def test_each_span_has_an_id_of_its_own_and_times_that_lie_within_those_of_its_parent(
    fixture_repository: FixtureRepository,
) -> None:
    started = time.time_ns() - CLOCKS_AGREE_WITHIN
    with record() as recording:
        verify(fixture_repository, model_listing("Adds tax"), ScriptedDecider(["behavioural"]))
    ended = time.time_ns() + CLOCKS_AGREE_WITHIN

    spans = {span.span_id: span for span in recording.trace().spans}
    assert len(spans) == 4
    assert all(re.fullmatch("[0-9a-f]{16}", span_id) for span_id in spans)
    assert "0" * 16 not in spans
    for span in spans.values():
        assert started <= span.start_unix_nano <= span.end_unix_nano <= ended
        if span.parent_id is not None:
            parent = spans[span.parent_id]
            assert parent.start_unix_nano <= span.start_unix_nano
            assert span.end_unix_nano <= parent.end_unix_nano


def test_recording_changes_nothing_in_the_report(fixture_repository: FixtureRepository) -> None:
    def verified() -> Report:
        return verify(
            fixture_repository, model_listing("Adds tax"), ScriptedDecider(["behavioural"])
        )

    plain = verified()
    with record():
        recorded = verified()

    assert (recorded.before, recorded.after) == (plain.before, plain.after)
    assert recorded.claims == plain.claims
    assert recorded.findings == plain.findings
    assert recorded.gate == plain.gate


def test_run_that_raises_is_recorded_as_failed_and_the_exception_goes_on_as_it_was(
    fixture_repository: FixtureRepository,
) -> None:
    with record() as recording, pytest.raises(ClaimExtractionFailed):
        verify(fixture_repository, ScriptedModel(["Sorry, zq-response is no list of Claims."]))

    trace = recording.trace()
    assert names(trace) == [
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.ROLE, "claim-extractor"),
        (SpanKind.RUN, "pr-verification"),
    ]
    assert [span.failed for span in trace.spans] == [False, True, True]
    assert "zq-response" not in repr(trace)


def test_run_that_ends_before_any_request_is_still_one_span(
    fixture_repository: FixtureRepository,
) -> None:
    pull_request = PullRequest(
        fixture_repository.path, "no-such-commit", fixture_repository.after, title="Tidy up"
    )

    with record() as recording, pytest.raises(InvalidPullRequest):
        run(pull_request, model_listing(), orchestration=GathersNothing())

    (whole,) = recording.trace().spans
    assert (whole.kind, whole.name, whole.parent_id, whole.failed) == (
        SpanKind.RUN, "pr-verification", None, True
    )  # fmt: skip


# Roles, requests to a model, and tools


def test_role_is_one_span_with_each_request_and_each_tool_it_ran_under_it() -> None:
    model = OnlyCompletes(
        asks("read_file", path="tax.py"), asks("delete_file", path="tax.py"), "what?", finishes()
    )

    with record() as recording:
        run_role(reader(returns("RATE = 3")), {"claim": "Adds tax"}, model)

    trace = recording.trace()
    assert names(trace) == [
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.TOOL_CALL, "read_file"),
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.ROLE, "reader"),
    ]
    *under, role = trace.spans
    assert role.parent_id is None
    assert all(span.parent_id == role.span_id for span in under)
    assert role.attributes == {"pr_verification.steps": 4, "pr_verification.stopped": "finished"}
    assert all(span.attributes == {} for span in under)


def test_role_stopped_at_its_step_cap_says_so_and_how_many_steps_it_took() -> None:
    model = OnlyCompletes(*[asks("read_file", path="tax.py")] * 5)

    with record() as recording:
        run_role(reader(returns("RATE = 3"), step_cap=2), {"claim": "Adds tax"}, model)

    role = recording.trace().spans[-1]
    assert role.attributes == {"pr_verification.steps": 2, "pr_verification.stopped": "step_cap"}


def test_role_whose_tool_raises_is_a_failed_span_that_does_not_say_how_it_stopped() -> None:
    def broken(arguments: Mapping[str, object]) -> str:
        raise OSError("zq-what-the-exception-said")

    role = reader(Tool("read_file", "Reads a file.", broken))

    with record() as recording, pytest.raises(OSError, match="zq-what-the-exception-said"):
        run_role(role, {"claim": "Adds tax"}, OnlyCompletes(asks("read_file", path="tax.py")))

    request, tool, role_span = recording.trace().spans
    assert [span.failed for span in (request, tool, role_span)] == [False, True, True]
    assert (tool.kind, tool.name) == (SpanKind.TOOL_CALL, "read_file")
    assert "pr_verification.stopped" not in role_span.attributes
    assert "zq-what-the-exception-said" not in repr(recording.trace())


def test_request_to_a_model_that_fails_is_a_failed_span() -> None:
    with record() as recording, pytest.raises(ModelFailed):
        run_role(reader(), {"claim": "Adds tax"}, UnreachableModel())

    request, role = recording.trace().spans
    assert (request.kind, request.failed, role.failed) == (SpanKind.MODEL_CALL, True, True)
    assert "could not be reached" not in repr(recording.trace())


def provider(usage: object) -> httpx.MockTransport:
    """A stand-in for an endpoint that lists no Claims and reports `usage`."""
    message = {"role": "assistant", "content": json.dumps({"claims": []})}
    answer = {"choices": [{"message": message}], "usage": usage}
    return httpx.MockTransport(lambda request: httpx.Response(200, json=answer))


@pytest.mark.parametrize(
    ("usage", "tokens"),
    [
        (
            {"prompt_tokens": 412, "completion_tokens": 9},
            {"gen_ai.usage.input_tokens": 412, "gen_ai.usage.output_tokens": 9},
        ),
        (None, {}),
        ({"prompt_tokens": "many", "completion_tokens": 9}, {}),
    ],
)
def test_request_to_an_endpoint_carries_the_models_name_and_its_tokens_when_the_answer_has_them(
    usage: object, tokens: Mapping[str, int]
) -> None:
    model = OpenAICompatibleModel(
        "https://zq-provider.example/v1", "some-model", "zq-secret-key", provider(usage)
    )

    with record() as recording:
        extract_claims(model, "Tidy up", "", "")

    request = recording.trace().spans[0]
    assert request.kind is SpanKind.MODEL_CALL
    assert request.attributes == {"gen_ai.request.model": "some-model", **tokens}
    assert "zq-" not in repr(recording.trace())


# Sandbox runs


def test_each_run_in_the_sandbox_is_a_span_with_its_phase_its_commit_and_how_it_ended() -> None:
    with record() as recording:
        run_twice(RAN_TWICE, Phase.INSTALL)

    before, after = recording.trace().spans
    assert names(recording.trace()) == [(SpanKind.SANDBOX_RUN, "sandbox")] * 2
    assert (before.parent_id, after.parent_id) == (None, None)
    assert before.attributes == {
        "pr_verification.phase": "install",
        "pr_verification.commit": "before-commit",
        "pr_verification.exit_status": 1,
    }
    assert after.attributes == {
        "pr_verification.phase": "install",
        "pr_verification.commit": "after-commit",
        "pr_verification.limit_hit": "time",
    }


def test_run_the_sandbox_could_not_do_is_a_failed_span_that_does_not_say_how_it_ended() -> None:
    sandbox = ScriptedSandbox({"before-commit": SandboxFailed("zq-no container runtime")})

    with record() as recording, pytest.raises(SandboxFailed):
        run_twice(sandbox)

    (failed,) = recording.trace().spans
    assert (failed.kind, failed.failed) == (SpanKind.SANDBOX_RUN, True)
    assert not {"pr_verification.exit_status", "pr_verification.limit_hit"} & set(failed.attributes)
    assert "zq-" not in repr(recording.trace())


# Decisions


class PassesOn:
    """A stand-in decider whose decision was passed on to the small LLM, at a cost."""

    def decide(self, state: str, question: Question) -> Decision:
        return Decision(
            question,
            {"behavioural": 0.25, "structural": 0.75},
            Implementation.SMALL_LLM,
            passed_on=PassedOn.LOW_CONFIDENCE,
            first_asked=Implementation.LAYA,
            dollars=0.002,
        )


def test_decision_carries_what_made_it_its_confidence_and_its_cost() -> None:
    decider = ScriptedDecider([{"behavioural": 0.25, "structural": 0.75}])

    with record() as recording:
        classify_claim(decider, "Renames zq-claim")

    (decision,) = recording.trace().spans
    assert (decision.kind, decision.name) == (SpanKind.DECISION, "decision")
    assert decision.attributes == {
        "pr_verification.made_by": "stand_in",
        "pr_verification.confidence": 0.75,
        "pr_verification.dollars": 0.0,
    }


def test_decision_that_was_passed_on_is_still_one_span_and_says_why() -> None:
    with record() as recording:
        classify_claim(PassesOn(), "Renames zq-claim")

    (decision,) = recording.trace().spans
    assert decision.attributes == {
        "pr_verification.made_by": "small_llm",
        "pr_verification.confidence": 0.75,
        "pr_verification.dollars": 0.002,
        "pr_verification.passed_on": "low_confidence",
    }


def test_request_to_the_small_llm_for_a_decision_is_a_child_of_that_decision() -> None:
    answer = json.dumps({"probabilities": {"behavioural": 3, "structural": 1}})

    with record() as recording:
        classify_claim(SmallLLMDecider(OnlyCompletes(answer)), "Fixes the crash")

    request, decision = recording.trace().spans
    assert names(recording.trace()) == [
        (SpanKind.MODEL_CALL, "model"),
        (SpanKind.DECISION, "decision"),
    ]
    assert (request.parent_id, decision.parent_id) == (decision.span_id, None)
    assert decision.attributes["pr_verification.made_by"] == "small_llm"


# Secrets


def test_no_span_holds_anything_that_a_pull_request_or_a_model_wrote(
    fixture_repository: FixtureRepository,
) -> None:
    asked = asks("read_file", path="zq-path.py")

    with record() as recording:
        verify(
            fixture_repository, model_listing("Fixes zq-claim"), ScriptedDecider(["behavioural"])
        )
        run_role(
            reader(returns("zq-what-the-tool-returned")),
            {"claim": "Fixes zq-claim"},
            OnlyCompletes(asked, "zq-reply", finishes(places=["zq-answer"])),
        )
        run_twice(RAN_TWICE)

    trace = recording.trace()
    assert len(trace.spans) == 11, "4 for the run, 5 for the role, and 2 Sandbox runs"
    assert "zq-" not in repr(trace)
    assert "zq-" not in json.dumps(to_otlp(trace))


# The OpenTelemetry protocol

TRACE = Trace(
    trace_id="0af7651916cd43dd8448eb211c80319c",
    spans=(
        Span(
            SpanKind.MODEL_CALL,
            "model",
            span_id="00f067aa0ba902b7",
            parent_id="b7ad6b7169203331",
            start_unix_nano=1_760_000_000_000_000_100,
            end_unix_nano=1_760_000_000_900_000_000,
            attributes={
                "gen_ai.request.model": "some-model",
                "gen_ai.usage.input_tokens": 412,
                "pr_verification.dollars": 0.005,
            },
            failed=False,
        ),
        Span(
            SpanKind.SANDBOX_RUN,
            "sandbox",
            span_id="b7ad6b7169203331",
            parent_id=None,
            start_unix_nano=1_760_000_000_000_000_000,
            end_unix_nano=1_760_000_001_000_000_000,
            attributes={"pr_verification.exit_status": -9},
            failed=True,
        ),
    ),
)


def test_trace_is_written_as_one_request_to_export_it_in_the_opentelemetry_protocol() -> None:
    exported = to_otlp(TRACE)

    written = json.loads(json.dumps(exported))
    assert written == exported
    assert set(written) == {"resourceSpans"}
    (resource_spans,) = written["resourceSpans"]
    assert resource_spans["resource"]["attributes"] == [
        {"key": "service.name", "value": {"stringValue": SERVICE_NAME}}
    ]
    (scope_spans,) = resource_spans["scopeSpans"]
    assert scope_spans["scope"] == {"name": "pr_verification"}
    request, sandbox_run = scope_spans["spans"]
    for span in (request, sandbox_run):
        span["attributes"].sort(key=lambda attribute: attribute["key"])
    assert "parentSpanId" not in sandbox_run
    stated = [
        {
            "traceId": "0af7651916cd43dd8448eb211c80319c",
            "spanId": "00f067aa0ba902b7",
            "parentSpanId": "b7ad6b7169203331",
            "name": "model",
            "kind": 1,
            "startTimeUnixNano": "1760000000000000100",
            "endTimeUnixNano": "1760000000900000000",
            "attributes": [
                {"key": "gen_ai.request.model", "value": {"stringValue": "some-model"}},
                {"key": "gen_ai.usage.input_tokens", "value": {"intValue": "412"}},
                {"key": "pr_verification.dollars", "value": {"doubleValue": 0.005}},
                {"key": "pr_verification.kind", "value": {"stringValue": "model_call"}},
            ],
            "status": {},
        },
        {
            "traceId": "0af7651916cd43dd8448eb211c80319c",
            "spanId": "b7ad6b7169203331",
            "name": "sandbox",
            "kind": 1,
            "startTimeUnixNano": "1760000000000000000",
            "endTimeUnixNano": "1760000001000000000",
            "attributes": [
                {"key": "pr_verification.exit_status", "value": {"intValue": "-9"}},
                {"key": "pr_verification.kind", "value": {"stringValue": "sandbox_run"}},
            ],
            "status": {"code": 2},
        },
    ]
    assert [
        {member: span[member] for member in members}
        for span, members in zip((request, sandbox_run), stated, strict=True)
    ] == stated


def otlp_value(value: str | int | float) -> dict[str, object]:
    """One attribute's value, written by its Python type as the contract says."""
    if isinstance(value, str):
        return {"stringValue": value}
    return {"intValue": str(value)} if isinstance(value, int) else {"doubleValue": value}


IDS = st.text("0123456789abcdef", min_size=16, max_size=16)
TIMES = st.integers(min_value=0, max_value=2**63 - 1)
SPANS = st.builds(
    Span,
    st.sampled_from(SpanKind),
    st.text(max_size=8),
    span_id=IDS,
    parent_id=st.none() | IDS,
    start_unix_nano=TIMES,
    end_unix_nano=TIMES,
    attributes=st.dictionaries(
        st.text("abcdefgh_.", max_size=8),
        st.text(max_size=8) | st.integers() | st.floats(allow_nan=False, allow_infinity=False),
        max_size=3,
    ),
    failed=st.booleans(),
)


@given(spans=st.lists(SPANS, max_size=4))
def test_any_trace_is_written_span_for_span_in_its_order_with_each_value_by_its_type(
    spans: list[Span],
) -> None:
    trace = Trace("0af7651916cd43dd8448eb211c80319c", tuple(spans))

    exported = to_otlp(trace)

    assert to_otlp(trace) == exported
    written = exported_spans(json.loads(json.dumps(exported)))
    assert [span["spanId"] for span in written] == [span.span_id for span in spans]
    for sent, span in zip(written, spans, strict=True):
        assert sent.get("parentSpanId") == span.parent_id
        assert ("parentSpanId" in sent) == (span.parent_id is not None)
        assert sent["status"] == ({"code": 2} if span.failed else {})
        assert {held["key"]: held["value"] for held in sent["attributes"]} == {
            **{name: otlp_value(value) for name, value in span.attributes.items()},
            "pr_verification.kind": {"stringValue": span.kind.value},
        }


# Where traces are sent


class Listening:
    """A stand-in for a collector of traces, in place of the network."""

    def __init__(self, answer: httpx.Response | Exception | None = None) -> None:
        self._answer = httpx.Response(200, json={}) if answer is None else answer
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._answer, Exception):
            raise self._answer
        return self._answer


def span_names(body: bytes) -> list[str]:
    """The names of the spans in a request to export traces."""
    return [span["name"] for span in exported_spans(json.loads(body))]


@pytest.mark.parametrize("environment", [{}, {ENDPOINT_VARIABLE: ""}])
def test_with_no_endpoint_configured_tracing_is_off_and_nothing_is_sent(
    environment: Mapping[str, str],
) -> None:
    listening = Listening()

    with traced(environment, listening.transport), record() as own:
        run_twice(RAN_TWICE)

    assert listening.requests == []
    assert len(own.trace().spans) == 2, "no recording was open, so one could be opened inside"


@pytest.mark.parametrize(
    ("endpoint", "sent_to"),
    [
        ("http://collector.example:4318", "http://collector.example:4318/v1/traces"),
        ("http://collector.example:4318/", "http://collector.example:4318/v1/traces"),
        ("https://collector.example/otlp", "https://collector.example/otlp/v1/traces"),
    ],
)
def test_with_an_endpoint_configured_the_trace_is_sent_there_once_when_the_work_is_done(
    endpoint: str, sent_to: str
) -> None:
    listening = Listening()

    with traced({ENDPOINT_VARIABLE: endpoint}, listening.transport):
        with pytest.raises(RuntimeError), record():
            pass
        run_twice(RAN_TWICE)
        assert listening.requests == []

    (request,) = listening.requests
    assert (request.method, str(request.url)) == ("POST", sent_to)
    assert request.headers["Content-Type"] == "application/json"
    assert span_names(request.content) == ["sandbox", "sandbox"]


def test_headers_configured_for_the_collector_are_sent_with_the_trace() -> None:
    listening = Listening()
    environment = {
        ENDPOINT_VARIABLE: "http://collector.example:4318",
        HEADERS_VARIABLE: "x-api-key = zq%20collector%2Ckey , x-team=verification=core",
    }

    with traced(environment, listening.transport):
        run_twice(RAN_TWICE)

    (request,) = listening.requests
    assert request.headers["x-api-key"] == "zq collector,key"
    assert request.headers["x-team"] == "verification=core"


def test_trace_with_no_spans_is_not_sent() -> None:
    listening = Listening()

    with traced({ENDPOINT_VARIABLE: "http://collector.example:4318"}, listening.transport):
        pass

    assert listening.requests == []


def test_trace_is_sent_though_the_work_raised_and_the_exception_goes_on_as_it_was() -> None:
    listening = Listening()
    sandbox = ScriptedSandbox({"before-commit": SandboxFailed("no container runtime")})

    with pytest.raises(SandboxFailed, match="no container runtime"):
        with traced({ENDPOINT_VARIABLE: "http://collector.example:4318"}, listening.transport):
            run_twice(sandbox)

    (request,) = listening.requests
    assert span_names(request.content) == ["sandbox"]


@pytest.mark.parametrize(
    "answer",
    [
        httpx.ConnectError("connection refused"),
        httpx.Response(503, text="zq-collector-key is not known here"),
        httpx.Response(401, json={"error": "unauthorised"}),
    ],
)
def test_trace_that_cannot_be_sent_is_one_line_on_standard_error_and_nothing_is_raised(
    answer: httpx.Response | Exception, capsys: pytest.CaptureFixture[str]
) -> None:
    listening = Listening(answer)
    environment = {
        ENDPOINT_VARIABLE: "http://collector.example:4318",
        HEADERS_VARIABLE: "x-api-key=zq-collector-key",
    }

    with traced(environment, listening.transport):
        run_twice(RAN_TWICE)

    captured = capsys.readouterr()
    assert len(listening.requests) == 1
    assert captured.err.startswith("pr-verification: ")
    assert len(captured.err.splitlines()) == 1
    assert "zq-collector-key" not in captured.err


def test_command_line_sends_the_one_trace_of_its_run_and_prints_and_returns_what_it_would(
    fixture_repository: FixtureRepository,
    collector: Collector,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    arguments = [
        "--repo", str(fixture_repository.path),
        "--before", fixture_repository.before,
        "--after", fixture_repository.after,
        "--title", "Fix the crash on empty input",
    ]  # fmt: skip

    def verified() -> tuple[int, object]:
        code = cli.main(
            arguments,
            model_listing("Fixes the crash on empty input"),
            ScriptedDecider(["behavioural"]),
            orchestration=GathersNothing(),
        )
        printed = json.loads(capsys.readouterr().out)
        printed["budget_spend"].pop("seconds")
        return code, printed

    plain = verified()
    monkeypatch.setenv(ENDPOINT_VARIABLE, collector.endpoint)
    traced_run = verified()

    assert traced_run == plain
    assert len(collector.received) == 1
    ((path, sent),) = collector.received
    assert path == "/v1/traces"
    spans = exported_spans(sent)
    assert [span["name"] for span in spans] == [
        "model", "claim-extractor", "decision", "pr-verification"
    ]  # fmt: skip
    assert "parentSpanId" not in spans[-1]
    assert {span["traceId"] for span in spans} == {spans[0]["traceId"]}
