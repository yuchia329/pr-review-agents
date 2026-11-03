"""How long a model client waits for one answer, and how long a durable worker's models wait."""

import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pr_verification.anthropic_protocol import AnthropicModel
from pr_verification.configuration import CEILING, CONFIGURATION_VARIABLE, configured_models
from pr_verification.durable.driver import WORKER_ANSWER_WAIT, worker_resources
from pr_verification.durable.journal import Effect, EffectKind
from pr_verification.durable.workflows import activity_options
from pr_verification.model import Model, ModelFailed, ModelRequest, RoleModels, RoleName
from pr_verification.openai_compatible import (
    ANSWER_WAIT,
    OpenAICompatibleModel,
    model_from_environment,
)

REQUEST = ModelRequest(instructions="List the Claims.", data='{"title": "Tidy up"}')
KEYS = {"PR_VERIFICATION_API_KEY": "minimax-key", "ANTHROPIC_API_KEY": "anthropic-key"}

Client = type[OpenAICompatibleModel] | type[AnthropicModel]
CLIENTS: list[Client] = [OpenAICompatibleModel, AnthropicModel]


class SlowEndpoint:
    """An endpoint on this machine that answers each request after `delay` seconds, in the
    protocol of the path it was posted to."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        endpoint = self

        class Answers(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                self.rfile.read(int(self.headers["Content-Length"]))
                time.sleep(endpoint.delay)
                if self.path.endswith("/messages"):
                    body = b'{"content": [{"type": "text", "text": "the answer"}]}'
                else:
                    body = b'{"choices": [{"message": {"content": "the answer"}}]}'
                try:
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass  # The client gave up waiting and closed the connection.

            def log_message(self, format: str, *args: object) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Answers)
        self._server.daemon_threads = True
        self.url = f"http://127.0.0.1:{self._server.server_address[1]}/v1"

    def __enter__(self) -> "SlowEndpoint":
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *_: object) -> None:
        self._server.shutdown()
        self._server.server_close()


@pytest.fixture
def slow() -> Iterator[SlowEndpoint]:
    with SlowEndpoint(delay=2.0) as endpoint:
        yield endpoint


def clients_of(model: Model) -> list[Model]:
    """Every client a model is made of: each of a `RoleModels`, or the model itself."""
    if isinstance(model, RoleModels):
        return [model.default, *model.roles.values()]
    return [model]


@pytest.mark.parametrize("client", CLIENTS)
def test_client_waits_the_answer_wait_it_is_made_with_or_five_minutes(client: Client) -> None:
    assert ANSWER_WAIT == 300.0
    assert client("http://127.0.0.1:1/v1", "a-model", "").answer_wait == ANSWER_WAIT
    assert client("http://127.0.0.1:1/v1", "a-model", "", answer_wait=12.5).answer_wait == 12.5
    assert client("http://127.0.0.1:1/v1", "a-model", "", answer_wait=None).answer_wait is None


@pytest.mark.parametrize("client", CLIENTS)
@given(wait=st.floats(max_value=0.0, allow_nan=False))
def test_answer_wait_that_is_not_more_than_nothing_is_refused(client: Client, wait: float) -> None:
    with pytest.raises(ValueError):
        client("http://127.0.0.1:1/v1", "a-model", "", answer_wait=wait)


@pytest.mark.parametrize("client", CLIENTS)
def test_request_not_answered_within_the_wait_fails_as_an_endpoint_not_reached(
    client: Client, slow: SlowEndpoint
) -> None:
    model = client(slow.url, "a-model", "", answer_wait=0.5)

    started = time.monotonic()
    with pytest.raises(ModelFailed) as failed:
        model.complete(REQUEST)

    assert time.monotonic() - started < slow.delay
    assert "could not be reached" in str(failed.value)


@pytest.mark.parametrize("client", CLIENTS)
@pytest.mark.parametrize("wait", [5.0, None])
def test_request_answered_within_the_wait_is_answered(
    client: Client, slow: SlowEndpoint, wait: float | None
) -> None:
    model = client(slow.url, "a-model", "", answer_wait=wait)

    assert model.complete(REQUEST) == "the answer"


def test_model_from_the_environment_waits_the_answer_wait_it_is_given() -> None:
    environment = {"PR_VERIFICATION_ENDPOINT": "http://127.0.0.1:1/v1"}

    default = model_from_environment(environment)
    given_wait = model_from_environment(environment, answer_wait=60.0)

    assert default is not None and default.answer_wait == ANSWER_WAIT
    assert given_wait is not None and given_wait.answer_wait == 60.0


def test_every_client_of_a_configuration_waits_the_answer_wait_it_is_given() -> None:
    assert CEILING.roles, "a configuration with clients of its own for some roles"

    made = configured_models(CEILING, KEYS, answer_wait=60.0)
    unless_given = configured_models(CEILING, KEYS)

    assert [client.answer_wait for client in clients_of(made)] == [60.0] * len(  # type: ignore[attr-defined]
        clients_of(made)
    )
    assert all(client.answer_wait == ANSWER_WAIT for client in clients_of(unless_given))  # type: ignore[attr-defined]


def test_worker_waits_a_minute_less_than_temporal_gives_a_model_step() -> None:
    step = activity_options(Effect(EffectKind.MODEL, (RoleName.REVIEWER, None)))

    assert step.start_to_close.total_seconds() - WORKER_ANSWER_WAIT == 60.0


@pytest.mark.parametrize(
    "environment",
    [
        {"PR_VERIFICATION_ENDPOINT": "http://127.0.0.1:1/v1"},
        {CONFIGURATION_VARIABLE: "self-hosted"},
    ],
    ids=["one model", "a configuration"],
)
def test_every_model_of_a_worker_waits_the_workers_answer_wait(
    tmp_path: Path, environment: dict[str, str]
) -> None:
    resources = worker_resources(environment, tmp_path)

    assert resources is not None
    waits = [client.answer_wait for client in clients_of(resources.model)]  # type: ignore[attr-defined]
    assert waits and all(wait == WORKER_ANSWER_WAIT for wait in waits)


def test_worker_with_no_model_configured_has_no_resources(tmp_path: Path) -> None:
    assert worker_resources({}, tmp_path) is None
