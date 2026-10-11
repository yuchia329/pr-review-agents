import json

import httpx
import pytest
from conftest import FixtureRepository, GathersNothing

from pr_verification.core import PullRequest, run
from pr_verification.model import ModelFailed, ModelRequest
from pr_verification.openai_compatible import OpenAICompatibleModel, model_from_environment

REQUEST = ModelRequest(instructions="List the Claims.", data='{"title": "Tidy up"}')


class Provider:
    """Stands in for the provider at the HTTP exchange and records what it was sent."""

    def __init__(self, response: httpx.Response | Exception) -> None:
        self._response = response
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


def _answer(content: object) -> httpx.Response:
    message = {"role": "assistant", "content": content}
    return httpx.Response(200, json={"choices": [{"message": message, "finish_reason": "stop"}]})


def _model(provider: Provider, key: str = "secret-key") -> OpenAICompatibleModel:
    return OpenAICompatibleModel(
        endpoint="https://provider.example/v1",
        model="some-model",
        key=key,
        transport=provider.transport,
    )


def test_request_goes_to_the_endpoint_with_instructions_and_data_kept_apart() -> None:
    provider = Provider(_answer('{"claims": []}'))

    response = _model(provider).complete(REQUEST)

    [sent] = provider.requests
    assert response == '{"claims": []}'
    assert sent.method == "POST"
    assert sent.url == "https://provider.example/v1/chat/completions"
    assert json.loads(sent.content) == {
        "model": "some-model",
        "messages": [
            {"role": "system", "content": "List the Claims."},
            {"role": "user", "content": '{"title": "Tidy up"}'},
        ],
    }


def test_key_is_sent_to_the_provider_as_a_bearer_token() -> None:
    provider = Provider(_answer("{}"))

    _model(provider, key="secret-key").complete(REQUEST)

    assert provider.requests[0].headers["Authorization"] == "Bearer secret-key"


def test_endpoint_that_needs_no_key_such_as_ollama_is_sent_none() -> None:
    provider = Provider(_answer("{}"))

    _model(provider, key="").complete(REQUEST)

    assert "Authorization" not in provider.requests[0].headers


def test_provider_that_answers_with_an_error_gives_a_failure_naming_the_status_and_reason() -> None:
    error = {"error": {"message": "Rate limit reached for some-model", "type": "rate_limit"}}
    provider = Provider(httpx.Response(429, text=json.dumps(error, indent=2)))

    with pytest.raises(ModelFailed) as failure:
        _model(provider).complete(REQUEST)

    message = str(failure.value)
    assert "some-model at https://provider.example/v1" in message
    assert "429" in message
    assert "Rate limit reached" in message
    assert "\n" not in message


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
    provider = Provider(error)

    with pytest.raises(ModelFailed) as failure:
        _model(provider).complete(REQUEST)

    assert "some-model at https://provider.example/v1" in str(failure.value)
    assert str(error) in str(failure.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="<html>Bad gateway</html>"),
        httpx.Response(200, text="[" * 100_000),
        httpx.Response(200, json=["not", "an", "object"]),
        httpx.Response(200, json={"choices": []}),
        httpx.Response(200, json={"base_resp": {"status_code": 1004, "status_msg": "login fail"}}),
        httpx.Response(200, json={"choices": [{"message": {"role": "assistant"}}]}),
        _answer(None),
        _answer(""),
        _answer([{"type": "text", "text": "not plain text"}]),
    ],
    ids=[
        "not JSON",
        "nested too deeply",
        "not an object",
        "no choices",
        "error in body",
        "no content",
        "null content",
        "empty content",
        "content in parts",
    ],
)
def test_provider_response_that_holds_no_text_gives_a_failure_showing_what_came_back(
    response: httpx.Response,
) -> None:
    provider = Provider(response)

    with pytest.raises(ModelFailed) as failure:
        _model(provider).complete(REQUEST)

    assert "some-model at https://provider.example/v1" in str(failure.value)
    assert response.text[:100] in str(failure.value)


def test_endpoint_that_is_not_a_url_gives_a_failure_and_not_a_crash() -> None:
    model = OpenAICompatibleModel(
        endpoint="http://localhost:1143x/v1",
        model="some-model",
        key="",
        transport=Provider(_answer("{}")).transport,
    )

    with pytest.raises(ModelFailed, match="some-model at http://localhost:1143x/v1"):
        model.complete(REQUEST)


def test_key_pasted_with_a_line_break_after_it_is_sent_without_one() -> None:
    provider = Provider(_answer("{}"))

    _model(provider, key="secret-key\n").complete(REQUEST)

    assert provider.requests[0].headers["Authorization"] == "Bearer secret-key"


@pytest.mark.parametrize("key", ["clé-secrète", "secret\nkey"], ids=["not ASCII", "line break"])
def test_key_that_cannot_be_sent_gives_a_failure_that_does_not_show_it(key: str) -> None:
    with pytest.raises(ModelFailed) as failure:
        _model(Provider(_answer("{}")), key=key)

    assert "some-model at https://provider.example/v1" in str(failure.value)
    assert "secr" not in str(failure.value)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(401, json={"error": {"message": "Incorrect key provided: secret-key"}}),
        httpx.Response(200, json={"error": "no choices for the holder of secret-key"}),
        httpx.ConnectError("cannot connect with Authorization: Bearer secret-key"),
    ],
    ids=["error status", "no text", "unreachable"],
)
def test_key_is_kept_out_of_a_failure_even_when_the_provider_repeats_it(
    response: httpx.Response | Exception,
) -> None:
    provider = Provider(response)

    with pytest.raises(ModelFailed) as failure:
        _model(provider, key="secret-key").complete(REQUEST)

    assert "secret-key" not in str(failure.value)
    assert "secret-key" not in repr(failure.value)


def test_thinking_the_model_leaves_in_front_of_its_answer_is_not_part_of_the_response() -> None:
    provider = Provider(_answer('<think>\nThe title makes no Claims.\n</think>\n\n{"claims": []}'))

    assert _model(provider).complete(REQUEST) == '{"claims": []}'


def test_long_provider_error_is_cut_short_in_the_failure() -> None:
    provider = Provider(httpx.Response(502, text="Bad gateway. " + "x" * 100_000))

    with pytest.raises(ModelFailed) as failure:
        _model(provider).complete(REQUEST)

    assert "502: Bad gateway." in str(failure.value)
    assert len(str(failure.value)) < 1_000


def test_slow_model_is_waited_for_longer_than_a_few_seconds() -> None:
    provider = Provider(_answer("{}"))

    _model(provider).complete(REQUEST)

    assert provider.requests[0].extensions["timeout"]["read"] >= 60


def test_key_alone_configures_the_development_default_model() -> None:
    provider = Provider(_answer("{}"))
    model = model_from_environment({"PR_VERIFICATION_API_KEY": "secret-key"}, provider.transport)

    assert model is not None
    model.complete(REQUEST)

    [sent] = provider.requests
    assert sent.url == "https://api.minimax.io/v1/chat/completions"
    assert json.loads(sent.content)["model"] == "MiniMax-M3"
    assert sent.headers["Authorization"] == "Bearer secret-key"


def test_endpoint_and_model_name_come_from_configuration_and_a_local_one_needs_no_key() -> None:
    provider = Provider(_answer("{}"))
    model = model_from_environment(
        {
            "PR_VERIFICATION_ENDPOINT": "http://localhost:11434/v1",
            "PR_VERIFICATION_MODEL": "pr-verification-qwen",
        },
        provider.transport,
    )

    assert model is not None
    model.complete(REQUEST)

    [sent] = provider.requests
    assert sent.url == "http://localhost:11434/v1/chat/completions"
    assert json.loads(sent.content)["model"] == "pr-verification-qwen"
    assert "Authorization" not in sent.headers


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"PR_VERIFICATION_API_KEY": "", "PR_VERIFICATION_ENDPOINT": ""},
        {"PR_VERIFICATION_MODEL": "some-model"},
    ],
    ids=["nothing set", "set but empty", "only a model name"],
)
def test_no_model_is_configured_without_a_key_or_an_endpoint(environment: dict[str, str]) -> None:
    assert model_from_environment(environment) is None


def test_core_lists_the_claims_the_model_behind_the_endpoint_extracts_and_never_the_key(
    fixture_repository: FixtureRepository,
) -> None:
    provider = Provider(_answer('{"claims": ["Fixes the crash on empty input"]}'))

    report = run(
        PullRequest(
            repository=fixture_repository.path,
            before=fixture_repository.before,
            after=fixture_repository.after,
            title="Fix the crash on empty input",
        ),
        _model(provider, key="secret-key"),
        orchestration=GathersNothing(),
    )

    assert [claim.text for claim in report.claims] == ["Fixes the crash on empty input"]
    assert "secret-key" not in report.to_json()
