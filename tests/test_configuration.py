"""Configurations: which model each role's work is sent to, and how one is chosen.

What a configuration makes is observed where a request leaves: at a stand-in for every
endpoint, so no connection is opened.
"""

import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from conftest import FixtureRepository, commit_snapshots

from pr_verification.cli import main
from pr_verification.configuration import (
    CEILING,
    CONFIGURATION_VARIABLE,
    CONFIGURATIONS,
    DEFAULT,
    SELF_HOSTED,
    Configuration,
    InvalidConfiguration,
    ModelChoice,
    ModelProtocol,
    configuration_from_environment,
    configuration_named,
    configured_models,
)
from pr_verification.decision import ScriptedDecider
from pr_verification.model import (
    Model,
    ModelFailed,
    ModelRequest,
    PricedModel,
    RoleName,
    TokenPrice,
    model_for,
    offers_parallel_tool_calls,
)
from pr_verification.orchestration import ORCHESTRATION_VARIABLE, OrchestrationChoice

REQUEST = ModelRequest(instructions="List the Claims.", data='{"title": "Tidy up"}')
KEYS = {"PR_VERIFICATION_API_KEY": "minimax-key", "ANTHROPIC_API_KEY": "anthropic-key"}
ENVIRONMENT_PRICE = {"PR_VERIFICATION_INPUT_PRICE": "1.0", "PR_VERIFICATION_OUTPUT_PRICE": "4.0"}

WHOEVER_ASKS = [None, *RoleName]
"""Each role, and None for whatever asks the models directly, for no role."""


class Endpoints:
    """Stands in for every endpoint a configuration names. It answers each request in the
    protocol of the path it was posted to, with 1,000 tokens sent and 500 returned, and
    records it."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path.endswith("/messages"):
            usage = {"input_tokens": 1_000, "output_tokens": 500}
            answer: dict[str, object] = {"content": [{"type": "text", "text": '{"claims": []}'}]}
        else:
            usage = {"prompt_tokens": 1_000, "completion_tokens": 500}
            answer = {"choices": [{"message": {"content": '{"claims": []}'}}]}
        return httpx.Response(200, json=answer | {"usage": usage})


def sent_for(
    configuration: Configuration, environment: dict[str, str], role: RoleName | None
) -> httpx.Request:
    """The one request that leaves when `role` asks the models made from a configuration."""
    endpoints = Endpoints()
    models = configured_models(configuration, environment, endpoints.transport)
    assert endpoints.requests == []
    asked = models if role is None else model_for(models, role)
    asked.complete(REQUEST)
    (sent,) = endpoints.requests
    return sent


def cost(model: Model) -> float:
    """What one request to a model made over `Endpoints` costs."""
    assert isinstance(model, PricedModel)
    return model.complete_priced(REQUEST).dollars


# The three named configurations


@pytest.mark.parametrize("role", WHOEVER_ASKS)
def test_default_configuration_sends_every_roles_work_to_minimax_m3(role: RoleName | None) -> None:
    sent = sent_for(DEFAULT, KEYS, role)

    assert str(sent.url) == "https://api.minimax.io/v1/chat/completions"
    assert json.loads(sent.content)["model"] == "MiniMax-M3"
    assert sent.headers["Authorization"] == "Bearer minimax-key"


@pytest.mark.parametrize("role", WHOEVER_ASKS)
def test_self_hosted_configuration_sends_every_roles_work_to_qwen_on_this_machine_with_no_key(
    role: RoleName | None,
) -> None:
    sent = sent_for(SELF_HOSTED, KEYS, role)

    assert sent.url.host == "localhost"
    assert str(sent.url) == "http://localhost:11434/v1/chat/completions"
    assert json.loads(sent.content)["model"] == "pr-verification-qwen"
    assert "Authorization" not in sent.headers and "x-api-key" not in sent.headers


@pytest.mark.parametrize("role", WHOEVER_ASKS)
def test_ceiling_configuration_sends_the_reasoning_to_claude_sonnet_and_the_rest_as_the_default(
    role: RoleName | None,
) -> None:
    sent = sent_for(CEILING, KEYS, role)

    if role in (None, RoleName.CLAIM_EXTRACTOR):
        as_default = sent_for(DEFAULT, KEYS, role)
        assert (sent.url, sent.content) == (as_default.url, as_default.content)
        assert sent.headers["Authorization"] == "Bearer minimax-key"
    else:
        assert str(sent.url) == "https://api.anthropic.com/v1/messages"
        assert json.loads(sent.content)["model"] == "claude-sonnet-5-5"
        assert sent.headers["x-api-key"] == "anthropic-key"
        assert "Authorization" not in sent.headers


def test_ceiling_has_one_client_for_claude_sonnet_with_its_own_price_and_parallel_tool_calls() -> (
    None
):
    models = configured_models(CEILING, KEYS | ENVIRONMENT_PRICE, Endpoints().transport)

    assert set(models.roles) == set(RoleName) - {RoleName.CLAIM_EXTRACTOR}
    (claude_sonnet,) = {id(model): model for model in models.roles.values()}.values()
    assert offers_parallel_tool_calls(claude_sonnet)
    assert not offers_parallel_tool_calls(models.default)
    # 1,000 tokens sent and 500 returned: at 2.0 and 10.0 for Claude Sonnet, whatever the
    # environment says, and at the environment's 1.0 and 4.0 for a model with no price.
    assert cost(claude_sonnet) == pytest.approx(0.007)
    assert cost(models.default) == pytest.approx(0.003)


def test_model_with_no_price_of_its_own_or_from_the_environment_costs_nothing() -> None:
    models = configured_models(DEFAULT, KEYS, Endpoints().transport)

    assert cost(models.default) == 0.0


# Any configuration

LARGE = ModelChoice(
    ModelProtocol.ANTHROPIC,
    "https://large.example/v1",
    "large-model",
    key_variable="LARGE_KEY",
    price=TokenPrice(input=3.0, output=6.0),
    parallel_tool_calls=True,
)
SMALL = ModelChoice(ModelProtocol.OPENAI_COMPATIBLE, "http://small.example/v1", "small-model")
MIXED = Configuration(
    "mixed",
    SMALL,
    roles={
        RoleName.REVIEWER: LARGE,
        RoleName.SUPERVISOR: replace(LARGE),
        RoleName.CLAIM_EXTRACTOR: replace(SMALL),
    },
)


@pytest.mark.parametrize("role", WHOEVER_ASKS)
def test_each_roles_work_is_sent_to_the_model_its_configuration_assigns_it_with_that_models_key(
    role: RoleName | None,
) -> None:
    sent = sent_for(MIXED, KEYS | {"LARGE_KEY": "large-key"}, role)

    if role in (RoleName.REVIEWER, RoleName.SUPERVISOR):
        assert str(sent.url) == "https://large.example/v1/messages"
        assert json.loads(sent.content)["model"] == "large-model"
        assert sent.headers["x-api-key"] == "large-key"
    else:
        assert str(sent.url) == "http://small.example/v1/chat/completions"
        assert json.loads(sent.content)["model"] == "small-model"
        assert "Authorization" not in sent.headers and "x-api-key" not in sent.headers


def test_roles_assigned_the_same_model_share_one_client_and_only_the_roles_named_have_one() -> None:
    models = configured_models(MIXED, {"LARGE_KEY": "large-key"}, Endpoints().transport)

    assert set(models.roles) == set(MIXED.roles)
    assert models.roles[RoleName.REVIEWER] is models.roles[RoleName.SUPERVISOR]
    assert models.roles[RoleName.CLAIM_EXTRACTOR] is models.default
    assert offers_parallel_tool_calls(models.roles[RoleName.REVIEWER])
    assert not offers_parallel_tool_calls(models.default)
    assert cost(models.roles[RoleName.REVIEWER]) == pytest.approx(0.006)


@pytest.mark.parametrize(
    ("configuration", "environment", "variable", "model"),
    [
        (DEFAULT, {}, "PR_VERIFICATION_API_KEY", "MiniMax-M3"),
        (DEFAULT, {"PR_VERIFICATION_API_KEY": ""}, "PR_VERIFICATION_API_KEY", "MiniMax-M3"),
        (CEILING, {"PR_VERIFICATION_API_KEY": "k"}, "ANTHROPIC_API_KEY", "claude-sonnet-5-5"),
        (MIXED, KEYS, "LARGE_KEY", "large-model"),
    ],
    ids=["unset", "empty", "one key of two", "a key of another name"],
)
def test_configuration_whose_key_the_environment_does_not_hold_is_refused_naming_it_and_its_model(
    configuration: Configuration, environment: dict[str, str], variable: str, model: str
) -> None:
    with pytest.raises(InvalidConfiguration) as refused:
        configured_models(configuration, environment, Endpoints().transport)

    assert variable in str(refused.value) and model in str(refused.value)


@pytest.mark.parametrize(
    ("environment", "names"),
    [
        (KEYS | {"PR_VERIFICATION_API_KEY": "clé-secrète"}, "MiniMax-M3"),
        (KEYS | {"PR_VERIFICATION_INPUT_PRICE": "cheap"}, "PR_VERIFICATION_INPUT_PRICE"),
        (KEYS | {"PR_VERIFICATION_OUTPUT_PRICE": "-1"}, "PR_VERIFICATION_OUTPUT_PRICE"),
    ],
    ids=["a key that cannot be sent", "a price that is no number", "a price under zero"],
)
def test_model_that_cannot_be_made_as_the_environment_stands_is_a_failure_that_shows_no_key(
    environment: dict[str, str], names: str
) -> None:
    with pytest.raises(ModelFailed, match=names) as failure:
        configured_models(DEFAULT, environment, Endpoints().transport)

    assert "secr" not in str(failure.value)


# Choosing a configuration


@pytest.mark.parametrize(
    ("name", "configuration"),
    [("default", DEFAULT), ("self-hosted", SELF_HOSTED), ("ceiling", CEILING)],
)
def test_each_named_configuration_is_had_by_its_name(
    name: str, configuration: Configuration
) -> None:
    assert configuration_named(name) is configuration


@pytest.mark.parametrize(
    "name", ["Default", " default", "self_hosted", "baseline", "reading-only-baseline"]
)
def test_any_other_name_is_refused_quoting_it_and_listing_the_names_there_are(name: str) -> None:
    with pytest.raises(InvalidConfiguration) as refused:
        configuration_named(name)

    message = str(refused.value)
    assert all(named in message for named in (name, "default", "self-hosted", "ceiling"))


@pytest.mark.parametrize(
    ("environment", "configuration"),
    [
        ({}, None),
        ({CONFIGURATION_VARIABLE: ""}, None),
        ({CONFIGURATION_VARIABLE: "default"}, DEFAULT),
        ({CONFIGURATION_VARIABLE: "self-hosted"}, SELF_HOSTED),
    ],
    ids=["unset", "empty", "default", "self-hosted"],
)
def test_environment_names_the_configuration_of_the_command_line_tool_or_none(
    environment: dict[str, str], configuration: Configuration | None
) -> None:
    assert configuration_from_environment(environment) is configuration


@pytest.mark.parametrize("named", ["ceiling", "Self-Hosted", "baseline", "reading-only-baseline"])
def test_configuration_that_only_the_evaluation_may_run_or_that_there_is_none_for_is_refused(
    named: str,
) -> None:
    with pytest.raises(InvalidConfiguration, match=CONFIGURATION_VARIABLE):
        configuration_from_environment({CONFIGURATION_VARIABLE: named})


@pytest.mark.parametrize(
    ("environment", "names"),
    [
        ({CONFIGURATION_VARIABLE: "ceiling"}, CONFIGURATION_VARIABLE),
        ({CONFIGURATION_VARIABLE: "no-such-configuration"}, CONFIGURATION_VARIABLE),
        ({CONFIGURATION_VARIABLE: "default"}, "PR_VERIFICATION_API_KEY"),
        (
            {CONFIGURATION_VARIABLE: "default", "PR_VERIFICATION_ENDPOINT": "http://localhost:1"},
            "PR_VERIFICATION_API_KEY",
        ),
    ],
    ids=["evaluation only", "no such configuration", "no key", "an endpoint and no key"],
)
def test_command_line_exits_two_without_a_report_when_its_configuration_cannot_be_had(
    fixture_repository: FixtureRepository,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environment: dict[str, str],
    names: str,
) -> None:
    for variable, value in environment.items():
        monkeypatch.setenv(variable, value)
    repository = fixture_repository
    arguments = ["--repo", str(repository.path), "--before", repository.before, "--after"]

    exited = main([*arguments, repository.after, "--title", "Tidy up"])

    printed = capsys.readouterr()
    assert (exited, printed.out) == (2, "")
    assert printed.err.startswith("pr-verification: ") and names in printed.err
    assert len(printed.err.splitlines()) == 1


class LocalQwen:
    """Stands in for every endpoint, as a model that lists one Claim for the Claim extractor
    and finishes with nothing for any role. It records what it was sent."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        listing = "title" in self.shown(request)
        text = json.dumps({"claims": ["Renames a file"]} if listing else {"finish": {}})
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})

    @staticmethod
    def shown(request: httpx.Request) -> dict[str, object]:
        """The data of a request, which is what a role is shown."""
        shown: dict[str, object] = json.loads(json.loads(request.content)["messages"][1]["content"])
        return shown


SUPERVISED = replace(
    SELF_HOSTED, name="self-hosted-supervised", orchestration=OrchestrationChoice.SUPERVISOR
)
"""A configuration the command line may choose whose own orchestration is the supervisor.
None of the named ones is, so a test adds it to them."""


@pytest.mark.parametrize(
    ("configuration", "orchestration", "supervised"),
    [
        (SELF_HOSTED, {}, False),
        (SELF_HOSTED, {ORCHESTRATION_VARIABLE: "supervisor"}, True),
        (SUPERVISED, {}, True),
        (SUPERVISED, {ORCHESTRATION_VARIABLE: "pipeline"}, False),
        (SUPERVISED, {ORCHESTRATION_VARIABLE: ""}, True),
    ],
    ids=[
        "the configuration's pipeline",
        "the supervisor the environment names",
        "the configuration's supervisor",
        "the pipeline the environment names",
        "the configuration's supervisor when the setting is empty",
    ],
)
def test_command_line_verifies_with_the_models_of_the_configuration_its_environment_names(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    configuration: Configuration,
    orchestration: dict[str, str],
    supervised: bool,
) -> None:
    monkeypatch.setitem(CONFIGURATIONS, SUPERVISED.name, SUPERVISED)
    # A repository of no Ecosystem, so no Sandbox is opened.
    before, after = commit_snapshots(tmp_path / "repository", {"a.txt": "a\n"}, {"b.txt": "a\n"})
    elsewhere = {
        CONFIGURATION_VARIABLE: configuration.name,
        "PR_VERIFICATION_ENDPOINT": "https://elsewhere.example/v1",
        "PR_VERIFICATION_MODEL": "another-model",
        **KEYS,
        **orchestration,
    }
    for variable, value in elsewhere.items():
        monkeypatch.setenv(variable, value)
    qwen = LocalQwen()
    arguments = ["--repo", str(tmp_path / "repository"), "--before", before, "--after", after]

    exited = main(
        [*arguments, "--title", "Rename a file"],
        decider=ScriptedDecider(["structural"]),
        transport=qwen.transport,
    )

    (claim,) = json.loads(capsys.readouterr().out)["claims"]
    assert (exited, claim["text"]) == (0, "Renames a file")
    assert len(qwen.requests) > 1
    for sent in qwen.requests:
        assert str(sent.url) == "http://localhost:11434/v1/chat/completions"
        assert json.loads(sent.content)["model"] == "pr-verification-qwen"
        assert "Authorization" not in sent.headers
    asked_to_supervise = ["supervisor_steps_left" in qwen.shown(sent) for sent in qwen.requests]
    assert any(asked_to_supervise) is supervised
