"""Traces: what one run did and how long each part took, in the OpenTelemetry format.

A trace is a tree of spans. A span is one piece of work, with when it started and when it
ended. Spans are recorded only while a recording is open, and recording changes nothing else:
the report, its Verdicts, and its Gate are the same with tracing on or off.

What is recorded
    While a recording is open, the system records one span for each of these, and no other:

    - `RUN`, named `pr-verification`: each call of the core's entry point, `run`.
    - `ROLE`, named as the role is: each run of a role, which is each call of `run_role`. The
      Claim extractor's work, each call of `extract_claims`, is one too, named
      `claim-extractor`.
    - `MODEL_CALL`, named `model`: each request the system makes to a model, on whatever
      model, through `complete` or through `complete_priced`. A request asked through a
      model that passes it on to another, as the model a `Ledger` gives does, is one request
      and one span.
    - `TOOL_CALL`, named as the tool is: each tool run for a role, which is each call that
      `run_role` makes of a tool's `run`, whether it returns or raises.
    - `SANDBOX_RUN`, named `sandbox`: each run of a command the system asks of a Sandbox, on
      whatever Sandbox, through `Sandbox.run`. A run asked through a Sandbox that passes it
      on to another, as the Sandbox a `Ledger` gives does, is one run and one span.
    - `DECISION`, named `decision`: each decision that verifying a pull request asks of the
      decision layer, which is each call of `classify_claim` and each time `run_role` asks
      its progress guard. A decision passed on to the small LLM is still one decision.

    A span's parent is the innermost span that was open when it started, and a span started
    with none open has no parent. So during a call of `run` its `RUN` span has no parent, and
    every other span recorded during the call is under it. A request to a model or a tool
    run that a role's step makes is a child of that role's span, and a request to the small
    LLM for a decision is a child of that decision's span. A decision of a role's progress
    guard is a child of that role's span.

    One recording is one trace. `traced` opens one recording around one run of the command
    line or of the Action, and such a run calls `run` at most once, so a run produces one
    trace. The Action's run for a push calls `run` not at all: its trace holds the spans of
    its Sandbox runs, each with no parent.

Attributes
    A span carries these attributes and no others, each only when it is known:

    - `ROLE`, for a call of `run_role`: `pr_verification.steps`, the number of steps taken,
      and `pr_verification.stopped`, the value of the run's `Stopped`.
    - `MODEL_CALL`: `gen_ai.request.model`, the name the endpoint knows the model by,
      `gen_ai.usage.input_tokens`, the tokens of the request, and
      `gen_ai.usage.output_tokens`, the tokens of the response. For an
      `OpenAICompatibleModel` the name is the `model` it was made with, with nothing of the
      endpoint, and the tokens are known when the endpoint's answer holds
      `usage.prompt_tokens` and `usage.completion_tokens` as integers. The scripted stand-in
      knows none of the three. Also `pr_verification.dollars`, what the request cost: the
      `dollars` of the `PricedResponse`, when the model was asked through `complete_priced`
      and answered. A request asked through `complete` has no known cost and carries none.
    - `SANDBOX_RUN`: `pr_verification.phase`, the value of the run's `Phase`;
      `pr_verification.commit`, the commit the run was asked for;
      `pr_verification.exit_status`, when the result has one; and `pr_verification.limit_hit`,
      the value of the result's `LimitHit`, when it has one.
    - `DECISION`: `pr_verification.made_by`, `pr_verification.confidence`, and
      `pr_verification.dollars`, as the `Decision` has them, and `pr_verification.passed_on`
      when it was passed on. The two enumerations are given by their values.

    An attribute known only once the work has ended is absent from a span that `failed`.

Secrets
    A span holds no secret and no Untrusted Content. Its name and its attributes are the ones
    above: none is a key, a token, the text of a request or a response, a command, output, a
    Claim, or anything else that a pull request or a model wrote. What an exception said is
    never recorded, only that the span `failed`.

Where traces are sent is configuration, and with none tracing is off: see `traced`.
"""

from collections.abc import Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum

import httpx

ENDPOINT_VARIABLE = "OTEL_EXPORTER_OTLP_ENDPOINT"
"""The environment variable that names where traces are sent: the base URL of a collector
that takes OTLP over HTTP. It is OpenTelemetry's own name for it."""

HEADERS_VARIABLE = "OTEL_EXPORTER_OTLP_HEADERS"
"""The environment variable that holds the headers sent with a trace, such as the collector's
own key, as `name=value` pairs with a comma between them. Also OpenTelemetry's own name."""

SERVICE_NAME = "pr-verification"
"""The name traces are sent under: their `service.name`."""

Attribute = str | int | float
"""The value of one attribute of a span."""


class SpanKind(StrEnum):
    """What kind of work a span is. The module's own text says when each is recorded."""

    RUN = "run"
    """One call of the core's entry point."""

    ROLE = "role"
    """One run of a role."""

    MODEL_CALL = "model_call"
    """One request to a model."""

    TOOL_CALL = "tool_call"
    """One tool run for a role."""

    SANDBOX_RUN = "sandbox_run"
    """One run of a command in the Sandbox."""

    DECISION = "decision"
    """One decision asked of the decision layer."""


@dataclass(frozen=True)
class Span:
    """One piece of work that has ended."""

    kind: SpanKind
    """What kind of work it was."""

    name: str
    """Its name, written by the project."""

    span_id: str
    """16 lowercase hexadecimal digits, not all zero. No two spans of a trace share one."""

    parent_id: str | None
    """The `span_id` of its parent, or None when it has none."""

    start_unix_nano: int
    """When the work started, in nanoseconds since the start of 1970, UTC."""

    end_unix_nano: int
    """When the work ended, the same way. Never before `start_unix_nano`. A span starts no
    earlier than its parent and ends no later."""

    attributes: Mapping[str, Attribute]
    """What is known of the work, under the names the module's own text gives."""

    failed: bool
    """True when an exception passed through the work, and False when it ended any other way.
    The exception goes on as it was."""


@dataclass(frozen=True)
class Trace:
    """The spans of one recording."""

    trace_id: str
    """32 lowercase hexadecimal digits, not all zero, chosen at random for each recording."""

    spans: tuple[Span, ...]
    """Every span that has ended, in the order they ended: a span comes after its children."""


class Recording:
    """A recording that `record` opened."""

    def trace(self) -> Trace:
        """The trace so far: every span that had ended when this was called. It can be asked
        for while the recording is open and after it has closed, and always has the same
        `trace_id`."""
        raise NotImplementedError


def record() -> AbstractContextManager[Recording]:
    """Open a recording for as long as the `with` lasts.

    Work done inside the `with` is recorded as the module's own text says. Work done outside
    one is not recorded, and costs nothing. Leaving the `with` closes the recording, however
    it is left, and nothing started after that is added to it.

    Raises `RuntimeError` when a recording is already open.
    """
    raise NotImplementedError


def to_otlp(trace: Trace) -> dict[str, object]:
    """`trace` as a request to export traces in OTLP, the OpenTelemetry protocol, in its JSON
    encoding.

    The answer can be written by `json.dumps` as it is. Its one member is `resourceSpans`, a
    list of one object, which has:

    - `resource`, whose `attributes` hold one attribute: `service.name`, with the string
      value `SERVICE_NAME`;
    - `scopeSpans`, a list of one object, whose `scope` is `{"name": "pr_verification"}` and
      whose `spans` hold one object for each span of the trace, in the trace's order.

    The object for a span has:

    - `traceId`, the trace's `trace_id`, and `spanId`, the span's `span_id`;
    - `parentSpanId`, the span's `parent_id`, left out when it is None;
    - `name`, the span's name, and `kind`, which is 1: an internal span, in OTLP's numbering;
    - `startTimeUnixNano` and `endTimeUnixNano`, the two times as strings of decimal digits;
    - `attributes`, a list of one `{"key": <name>, "value": <value>}` for each attribute of
      the span, and one more whose key is `pr_verification.kind` and whose value is the value
      of the span's `SpanKind`. The order of the list carries no meaning;
    - `status`, which is `{"code": 2}`, an error in OTLP's numbering, when the span `failed`,
      and `{}` when it did not.

    A value is written by its Python type: `{"stringValue": <the text>}` for a string,
    `{"intValue": <a string of decimal digits, with a minus sign before a negative
    number>}` for an integer, and `{"doubleValue": <the number>}` for a float.

    A pure function of `trace`.
    """
    raise NotImplementedError


def traced(
    environment: Mapping[str, str], transport: httpx.BaseTransport | None = None
) -> AbstractContextManager[None]:
    """Record the work done inside the `with` and send its trace, when `environment` says to.

    Tracing is on when `ENDPOINT_VARIABLE` is set in `environment` to other than the empty
    string, and off otherwise. Off, no recording is opened and nothing is sent.

    On, a recording is open for as long as the `with` lasts, as `record` opens one. When the
    `with` is left, however it is left, the trace is sent once: `to_otlp` of it as JSON, in
    one POST request to the endpoint with `/v1/traces` after it, whether or not the endpoint
    ends with a `/`. The request carries the header `Content-Type: application/json`, and one
    header for each pair in `HEADERS_VARIABLE`. A pair is split at its first `=`, the spaces
    around the name and around the value are dropped, and percent-encoding in the value is
    undone. A trace with no spans is not sent.

    Sending changes nothing that the work did. A trace that cannot be sent, because the
    collector cannot be reached or answers with an error, is one line on standard error that
    starts `pr-verification: ` and holds the value of no header. Nothing is raised for it, and
    an exception from the work inside goes on as it was.

    `transport` is for a caller that brings its own in place of the network.
    """
    raise NotImplementedError
