"""The Anthropic-protocol client: what it sends, what it reads, and how it fails.

The provider is a stand-in at the HTTP exchange, so no connection is opened.
"""

import json
from collections.abc import Callable, Mapping

import httpx
import pytest
from hypothesis import given
from hypothesis import strategies as st
from test_openai_compatible import Provider

from pr_verification.anthropic_protocol import (
    ANTHROPIC_VERSION,
    CACHE_READ_MULTIPLE,
    CACHE_WRITE_MULTIPLE,
    MAX_TOKENS,
    AnthropicModel,
)
from pr_verification.model import ModelFailed, ModelRequest, PricedResponse, TokenPrice
from pr_verification.openai_compatible import OpenAICompatibleModel
from pr_verification.tracing import SpanKind, record

REQUEST = ModelRequest(instructions="List the Claims.", data='{"title": "Tidy up"}')
ENDPOINT = "https://provider.example/v1"
NAME = "some-model at https://provider.example/v1"
CUT_SHORT = " [cut short]"


def text(written: str) -> dict[str, str]:
    """A block of an answer that holds text."""
    return {"type": "text", "text": written}


THINKING = {"type": "thinking", "thinking": "The title makes no Claims.", "signature": "zq"}
"""A block of an answer that holds the model's thinking."""


def answer(*blocks: object, usage: object = None) -> httpx.Response:
    """An answer of the protocol whose `content` is these blocks."""
    answered: dict[str, object] = {"type": "message", "role": "assistant", "content": blocks}
    if usage is not None:
        answered["usage"] = usage
    return httpx.Response(200, json=answered)


def a_model(
    provider: Provider,
    key: str = "secret-key",
    price: TokenPrice | None = None,
    endpoint: str = ENDPOINT,
) -> AnthropicModel:
    return AnthropicModel(endpoint, "some-model", key, provider.transport, price)


def body_of(request: httpx.Request) -> dict[str, object]:
    body: dict[str, object] = json.loads(request.content)
    return body


# The request


@pytest.mark.parametrize("endpoint", [ENDPOINT, ENDPOINT + "/"], ids=["bare", "with a slash"])
def test_request_goes_to_the_endpoint_with_instructions_and_data_kept_apart(endpoint: str) -> None:
    provider = Provider(answer(text('{"claims": []}')))

    response = a_model(provider, endpoint=endpoint).complete(REQUEST)

    [sent] = provider.requests
    assert response == '{"claims": []}'
    assert (sent.method, str(sent.url)) == ("POST", "https://provider.example/v1/messages")
    assert sent.headers["anthropic-version"] == ANTHROPIC_VERSION
    assert body_of(sent) == {
        "model": "some-model",
        "max_tokens": MAX_TOKENS,
        "system": "List the Claims.",
        "messages": [{"role": "user", "content": '{"title": "Tidy up"}'}],
    }


@pytest.mark.parametrize(
    ("key", "sent_as"),
    [("secret-key", "secret-key"), ("secret-key\n", "secret-key"), ("", None), (" \n", None)],
    ids=["a key", "a key pasted with a line break", "no key", "a blank key"],
)
def test_key_is_sent_without_the_whitespace_around_it_and_not_at_all_when_there_is_none(
    key: str, sent_as: str | None
) -> None:
    provider = Provider(answer(text("{}")))

    a_model(provider, key=key).complete(REQUEST)

    assert provider.requests[0].headers.get("x-api-key") == sent_as


@pytest.mark.parametrize("key", ["clé-secrète", "secret\nkey"], ids=["not ASCII", "line break"])
def test_key_that_cannot_be_sent_gives_a_failure_that_does_not_show_it(key: str) -> None:
    with pytest.raises(ModelFailed) as failure:
        a_model(Provider(answer(text("{}"))), key=key)

    assert str(failure.value) == f"the key for {NAME} holds characters that cannot be sent"


def test_slow_model_is_waited_for_for_five_minutes() -> None:
    provider = Provider(answer(text("{}")))

    a_model(provider).complete(REQUEST)

    assert provider.requests[0].extensions["timeout"]["read"] == 300


# Prompt caching


def test_instructions_worth_keeping_are_sent_as_a_block_the_provider_is_asked_to_keep() -> None:
    plain, kept = Provider(answer(text("{}"))), Provider(answer(text("{}")))
    worth_keeping = ModelRequest(REQUEST.instructions, REQUEST.data, cache_instructions=True)

    responses = (a_model(plain).complete(REQUEST), a_model(kept).complete(worth_keeping))

    block = {"type": "text", "text": "List the Claims.", "cache_control": {"type": "ephemeral"}}
    assert body_of(kept.requests[0]) == body_of(plain.requests[0]) | {"system": [block]}
    assert kept.requests[0].url == plain.requests[0].url
    for header in ("anthropic-version", "x-api-key"):
        assert kept.requests[0].headers[header] == plain.requests[0].headers[header]
    assert responses == ("{}", "{}")


# The response


@pytest.mark.parametrize(
    "blocks",
    [
        [text('{"claims": []}')],
        [text('{"claims"'), text(": []}")],
        [THINKING, text('{"claims": []}')],
        [text('{"claims"'), THINKING, {"type": "tool_use", "name": "zq"}, text(": []}")],
        [text('<think>\nThe title makes no Claims.\n</think>\n\n{"claims": []}')],
        [text("<think>None, I"), text(' think.</think>{"claims": []}')],
        [text('\n {"claims": []}\n\n')],
    ],
    ids=[
        "one block",
        "two blocks",
        "a block of thinking",
        "blocks of other types between",
        "thinking left in the text",
        "thinking left across two blocks",
        "whitespace around it",
    ],
)
def test_response_is_the_text_blocks_joined_without_thinking_or_the_whitespace_around_it(
    blocks: list[object],
) -> None:
    provider = Provider(answer(*blocks))

    assert a_model(provider).complete(REQUEST) == '{"claims": []}'


# Failures


def failure_of(provider: Provider, key: str = "secret-key") -> ModelFailed:
    """The failure of the one request made to a provider, which is not made again."""
    with pytest.raises(ModelFailed) as failure:
        a_model(provider, key=key).complete(REQUEST)
    assert len(provider.requests) == 1
    return failure.value


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectError("connection refused"),
        httpx.ReadTimeout("timed out"),
        httpx.RemoteProtocolError("server disconnected"),
    ],
    ids=["refused", "timed out", "disconnected"],
)
def test_provider_that_cannot_be_reached_gives_a_failure_saying_why(error: Exception) -> None:
    failure = failure_of(Provider(error))

    assert str(failure) == f"{NAME} could not be reached: {error}"
    assert failure.__cause__ is None
    assert failure.__context__ is None or failure.__suppress_context__


def test_endpoint_that_is_not_a_url_gives_a_failure_and_not_a_crash() -> None:
    model = AnthropicModel(
        "http://localhost:1143x/v1", "some-model", "", Provider(answer(text("{}"))).transport
    )

    with pytest.raises(ModelFailed) as failure:
        model.complete(REQUEST)

    assert str(failure.value).startswith(
        "some-model at http://localhost:1143x/v1 could not be reached: "
    )


def test_provider_that_answers_with_an_error_gives_a_failure_of_one_line_with_what_it_said() -> (
    None
):
    error = {"type": "error", "error": {"type": "rate_limit_error", "message": "Slow down"}}
    provider = Provider(httpx.Response(429, text=json.dumps(error, indent=2)))

    said = " ".join(json.dumps(error, indent=2).split())
    assert str(failure_of(provider)) == f"{NAME} answered with HTTP 429: {said}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>Bad gateway</html>"),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"type": "message", "role": "assistant"}),
        httpx.Response(200, json={"content": None}),
        httpx.Response(200, json={"content": "text that is in no block"}),
        answer(),
        answer(THINKING),
        answer(text("")),
        answer(text(" \n")),
        answer(text("<think>Nothing to say.</think>")),
    ],
    ids=[
        "not JSON",
        "not an object",
        "no content",
        "null content",
        "content that is no list",
        "no blocks",
        "only thinking",
        "empty text",
        "blank text",
        "text that is all thinking",
    ],
)
def test_provider_response_that_holds_no_text_gives_a_failure_showing_what_came_back(
    response: httpx.Response,
) -> None:
    came_back = " ".join(response.text.split())

    failure = failure_of(Provider(response))

    assert str(failure) == f"{NAME} gave a response that holds no text: {came_back}"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "Incorrect key provided: secret-key"}}),
        httpx.Response(200, json={"error": "no content for the holder of secret-key"}),
        httpx.ConnectError("cannot connect with x-api-key: secret-key"),
    ],
    ids=["error status", "no text", "unreachable"],
)
def test_key_the_provider_repeats_back_is_replaced_in_the_failure(
    response: httpx.Response | Exception,
) -> None:
    failure = failure_of(Provider(response), key="secret-key")

    assert "secret-key" not in str(failure) + repr(failure)
    assert "[key removed]" in str(failure)


def test_long_failure_is_cut_to_five_hundred_characters_and_says_so() -> None:
    provider = Provider(httpx.Response(502, text="Bad gateway. " + "x" * 100_000))

    message = str(failure_of(provider))

    assert message.startswith(f"{NAME} answered with HTTP 502: Bad gateway. xxx")
    assert message.endswith("x" + CUT_SHORT)
    assert len(message) == 500 + len(CUT_SHORT)


@pytest.mark.parametrize(
    "answered",
    [
        lambda: httpx.ConnectError("connection refused"),
        lambda: httpx.Response(429, text='{\n  "error": "Slow down, holder of secret-key"\n}'),
        lambda: httpx.Response(200, text="<html>Bad gateway</html>"),
        lambda: httpx.Response(200, json={"error": "nothing for secret-key " + "x" * 600}),
    ],
    ids=["unreachable", "error status", "no text", "a long answer that repeats the key"],
)
def test_failure_reads_the_same_whichever_of_the_two_clients_met_it(
    answered: Callable[[], httpx.Response | Exception],
) -> None:
    failures = []
    for client in (OpenAICompatibleModel, AnthropicModel):
        model = client(ENDPOINT, "some-model", "secret-key", Provider(answered()).transport)
        with pytest.raises(ModelFailed) as failure:
            model.complete(REQUEST)
        failures.append(str(failure.value))

    assert failures[0] == failures[1]


def test_key_that_cannot_be_sent_is_refused_the_same_by_either_client() -> None:
    failures = []
    for client in (OpenAICompatibleModel, AnthropicModel):
        with pytest.raises(ModelFailed) as failure:
            client(ENDPOINT, "some-model", "clé-secrète")
        failures.append(str(failure.value))

    assert failures[0] == failures[1]


# What a request is recorded as


@pytest.mark.parametrize(
    ("usage", "tokens"),
    [
        (
            {"input_tokens": 412, "output_tokens": 9},
            {"gen_ai.usage.input_tokens": 412, "gen_ai.usage.output_tokens": 9},
        ),
        (None, {}),
        ({"input_tokens": "many", "output_tokens": 9}, {}),
    ],
    ids=["both counts", "no usage", "a count that is text"],
)
def test_request_is_recorded_once_with_the_models_name_and_its_tokens_when_the_answer_has_them(
    usage: object, tokens: Mapping[str, int]
) -> None:
    provider = Provider(answer(text('{"claims": []}'), usage=usage))
    model = AnthropicModel(
        "https://zq-provider.example/v1", "some-model", "zq-secret-key", provider.transport
    )

    with record() as recording:
        model.complete(REQUEST)

    (request,) = recording.trace().spans
    assert request.kind is SpanKind.MODEL_CALL
    assert request.attributes == {"gen_ai.request.model": "some-model", **tokens}


# What a request cost

PRICE = TokenPrice(input=2.0, output=10.0)
SENT_AND_RETURNED = {"input_tokens": 1_000, "output_tokens": 500}


@pytest.mark.parametrize(
    ("usage", "dollars"),
    [
        (SENT_AND_RETURNED, 0.007),
        ({"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 1_000_000}, 0.2),
        ({"input_tokens": 1_000_000, "output_tokens": 0}, 2.0),
        ({"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 1_000_000}, 2.5),
    ],
    ids=["sent and returned", "read from what was kept", "sent afresh", "kept for later"],
)
def test_priced_request_is_the_one_request_and_costs_its_tokens_at_the_models_price(
    usage: dict[str, int], dollars: float
) -> None:
    provider = Provider(answer(text('{"claims": []}'), usage=usage))

    priced = a_model(provider, price=PRICE).complete_priced(REQUEST)

    assert priced.response == '{"claims": []}'
    assert priced.dollars == pytest.approx(dollars)
    assert len(provider.requests) == 1


TOKENS = st.integers(min_value=0, max_value=10_000_000)
PRICES = st.floats(min_value=0, max_value=100)


@given(
    sent=TOKENS,
    returned=TOKENS,
    kept=TOKENS,
    read=TOKENS,
    price=st.builds(TokenPrice, PRICES, PRICES),
)
def test_for_any_counts_a_request_costs_each_kind_of_token_at_its_multiple_of_the_price(
    sent: int, returned: int, kept: int, read: int, price: TokenPrice
) -> None:
    usage = {
        "input_tokens": sent,
        "output_tokens": returned,
        "cache_creation_input_tokens": kept,
        "cache_read_input_tokens": read,
    }
    provider = Provider(answer(text("{}"), usage=usage))

    priced = a_model(provider, price=price).complete_priced(REQUEST)

    at_input = sent + kept * CACHE_WRITE_MULTIPLE + read * CACHE_READ_MULTIPLE
    expected = (at_input * price.input + returned * price.output) / 1_000_000
    assert priced.dollars == pytest.approx(expected)


@pytest.mark.parametrize(
    "count", [None, "1000", 0.5, -1], ids=["null", "text", "not whole", "under zero"]
)
@pytest.mark.parametrize("which", ["cache_creation_input_tokens", "cache_read_input_tokens"])
def test_count_of_kept_tokens_that_is_no_whole_number_of_zero_or_more_is_taken_as_none(
    which: str, count: object
) -> None:
    provider = Provider(answer(text("{}"), usage=SENT_AND_RETURNED | {which: count}))

    assert a_model(provider, price=PRICE).complete_priced(REQUEST).dollars == pytest.approx(0.007)


@pytest.mark.parametrize(
    ("price", "usage"),
    [
        (None, SENT_AND_RETURNED),
        (PRICE, None),
        (PRICE, {"input_tokens": 1_000, "cache_read_input_tokens": 1_000}),
        (PRICE, {"input_tokens": "1000", "output_tokens": 500}),
        (PRICE, {"input_tokens": 1_000, "output_tokens": 0.5}),
        (PRICE, {"input_tokens": -1, "output_tokens": 500}),
        (PRICE, "1500 tokens"),
    ],
    ids=[
        "no price",
        "no usage",
        "one count missing",
        "a count that is text",
        "a count that is not whole",
        "a count under zero",
        "usage that is no object",
    ],
)
def test_request_whose_cost_is_not_known_is_not_charged_for(
    price: TokenPrice | None, usage: object
) -> None:
    provider = Provider(answer(text("{}"), usage=usage))

    assert a_model(provider, price=price).complete_priced(REQUEST) == PricedResponse("{}", 0.0)


def test_price_changes_nothing_about_a_request_that_is_not_priced() -> None:
    plain, priced = Provider(answer(text("{}"))), Provider(answer(text("{}")))

    responses = (a_model(plain).complete(REQUEST), a_model(priced, price=PRICE).complete(REQUEST))

    assert responses == ("{}", "{}")
    assert priced.requests[0].content == plain.requests[0].content


def test_priced_request_that_fails_raises_what_the_plain_request_raises() -> None:
    def failure_from(ask: Callable[[AnthropicModel], object]) -> str:
        provider = Provider(httpx.Response(429, text="Rate limit reached"))
        with pytest.raises(ModelFailed) as failure:
            ask(a_model(provider, price=PRICE))
        return str(failure.value)

    plain = failure_from(lambda model: model.complete(REQUEST))
    assert failure_from(lambda model: model.complete_priced(REQUEST)) == plain
