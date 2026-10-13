"""Which model does each role's work, and which orchestration runs the roles.

A configuration assigns a model to each role and selects the orchestration. It is data: it
names models and holds none, so it can be written down, compared, and reported. Three are
named here: the default, one for fully self-hosted use, and a ceiling for the evaluation.

The reading-only baseline of `pr_verification.baseline` is no configuration and no
orchestration, and nothing here can name it.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum

import httpx

from pr_verification.anthropic_protocol import ANTHROPIC_ENDPOINT, AnthropicModel
from pr_verification.model import REASONING_ROLES, Model, RoleModels, RoleName, TokenPrice
from pr_verification.openai_compatible import (
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    OpenAICompatibleModel,
    _price_from,
)
from pr_verification.orchestration import OrchestrationChoice


class ModelProtocol(StrEnum):
    """The protocol a model is reached through, and so the client that reaches it."""

    OPENAI_COMPATIBLE = "openai-compatible"
    """`OpenAICompatibleModel`, of `pr_verification.openai_compatible`."""

    ANTHROPIC = "anthropic"
    """`AnthropicModel`, of `pr_verification.anthropic_protocol`."""


@dataclass(frozen=True)
class ModelChoice:
    """One model, as a configuration names it: enough to make its client, and no secret."""

    protocol: ModelProtocol
    """The protocol the model is reached through."""

    endpoint: str
    """The base URL of the endpoint, up to and including `/v1`."""

    model: str
    """The model's name as the endpoint knows it."""

    key_variable: str | None = None
    """The environment variable the key is read from, or None for an endpoint that needs no
    key, to which none is sent. The key itself is never part of a configuration."""

    price: TokenPrice | None = None
    """What the provider charges for the model, when the project knows it."""

    parallel_tool_calls: bool = False
    """Whether the model is relied on to ask for several tools in one response."""


MINIMAX_M3 = ModelChoice(
    ModelProtocol.OPENAI_COMPATIBLE,
    DEFAULT_ENDPOINT,
    DEFAULT_MODEL,
    key_variable="PR_VERIFICATION_API_KEY",
)
"""MiniMax M3 at its own hosted endpoint: the development default. Its price is not yet
known to the project, so none is given here."""

QWEN_LOCAL = ModelChoice(
    ModelProtocol.OPENAI_COMPATIBLE, "http://localhost:11434/v1", "pr-verification-qwen"
)
"""Qwen 3.8 served by Ollama on this machine, under the name `ollama/Modelfile` gives it so
that its context size is set. It needs no key, and no price is given for it here."""

CLAUDE_SONNET = ModelChoice(
    ModelProtocol.ANTHROPIC,
    ANTHROPIC_ENDPOINT,
    "claude-sonnet-5-5",
    key_variable="ANTHROPIC_API_KEY",
    price=TokenPrice(input=2.0, output=10.0),
    parallel_tool_calls=True,
)
"""Claude Sonnet 5.5 at Anthropic's endpoint."""


@dataclass(frozen=True)
class Configuration:
    """A model for each role, and the orchestration that runs them."""

    name: str
    """What the configuration is called, where it is chosen and where what it measured is
    shown."""

    default: ModelChoice
    """The model of every role that `roles` does not name, and of whatever is no role's, as
    `RoleModels.default` is."""

    roles: Mapping[RoleName, ModelChoice] = field(default_factory=dict)
    """The model of each role that has one of its own."""

    orchestration: OrchestrationChoice = OrchestrationChoice.PIPELINE
    """The orchestration, unless whatever runs the configuration is told another."""

    evaluation_only: bool = False
    """Whether only the evaluation may run the configuration. One that is cannot be chosen
    for the command-line tool, as `configuration_from_environment` says."""


DEFAULT = Configuration("default", MINIMAX_M3)
"""MiniMax M3 in every role, with the fixed pipeline."""

SELF_HOSTED = Configuration("self-hosted", QWEN_LOCAL)
"""Fully self-hosted use: Qwen 3.8 in every role, with the fixed pipeline. Every
`ModelChoice` it holds has an `endpoint` whose host is `localhost` and no `key_variable`, so
every request made to a model it configures, for a role or as the small LLM behind the
decider, goes to this machine and carries no key.

The decision layer is configured apart, by `decision_configuration`
(`pr_verification.decision`). Laya, its default, runs in the process. Jev is reached over
the network, so a run that is to stay on the machine does not choose it."""

CEILING = Configuration(
    "ceiling",
    MINIMAX_M3,
    roles=dict.fromkeys(REASONING_ROLES, CLAUDE_SONNET),
    evaluation_only=True,
)
"""A stronger model run through the evaluation, to tell failures caused by the model from
failures caused by the design: Claude Sonnet 5.5 in each of `REASONING_ROLES`, and what
`DEFAULT` has everywhere else, so that only the reasoning differs between the two."""

CONFIGURATIONS: Mapping[str, Configuration] = {
    configuration.name: configuration for configuration in (DEFAULT, SELF_HOSTED, CEILING)
}
"""Every named configuration, under its name."""

CONFIGURATION_VARIABLE = "PR_VERIFICATION_CONFIGURATION"
"""The environment variable that names the configuration the command-line tool uses."""


class InvalidConfiguration(Exception):
    """A configuration was asked for that cannot be had, or one cannot be made into models as
    the environment stands. Its message is safe to print: it names what is at fault, and
    holds no key."""


def configuration_named(name: str) -> Configuration:
    """The configuration of `CONFIGURATIONS` called `name`, matched exactly as written.

    Raises `InvalidConfiguration` for any other name, with a message that quotes the name
    and lists the names there are. `baseline` is such a name, as is any other word for the
    reading-only baseline: it is not among the configurations.
    """
    if name not in CONFIGURATIONS:
        raise InvalidConfiguration(
            f"there is no configuration named {name!r}: the configurations are "
            + ", ".join(CONFIGURATIONS)
        )
    return CONFIGURATIONS[name]


def configuration_from_environment(environment: Mapping[str, str]) -> Configuration | None:
    """Read which configuration the command-line tool uses from
    `PR_VERIFICATION_CONFIGURATION`.

    None when it is unset or set to the empty string: the tool then configures its one model
    as it did before configurations existed, with `model_from_environment`. Otherwise
    `configuration_named` of what it is set to.

    Raises `InvalidConfiguration`, with a message that names the variable, when it is set to
    a name there is no configuration for, and when the configuration it names is
    `evaluation_only`, as `ceiling` is. So neither a ceiling run nor the reading-only
    baseline can be chosen for a pull request in a pipeline.

    The command line reads it. The GitHub Action does not: it configures its one model as
    before, whatever the variable says.
    """
    name = environment.get(CONFIGURATION_VARIABLE, "")
    if not name:
        return None
    try:
        configuration = configuration_named(name)
    except InvalidConfiguration as invalid:
        raise InvalidConfiguration(f"{CONFIGURATION_VARIABLE} cannot be used: {invalid}") from None
    if configuration.evaluation_only:
        raise InvalidConfiguration(
            f"{CONFIGURATION_VARIABLE} is set to {name!r}, a configuration that only the "
            "evaluation may run"
        )
    return configuration


def configured_models(
    configuration: Configuration,
    environment: Mapping[str, str],
    transport: httpx.BaseTransport | None = None,
) -> RoleModels:
    """Make the models `configuration` names, each role's as the configuration assigns it.

    One client is made for each different `ModelChoice` among `configuration.default` and
    `configuration.roles`, of the class its `protocol` names, with the choice's `endpoint`,
    `model`, and `parallel_tool_calls`, and with `transport`. Two choices are the same when
    they are equal, whether or not they are one object, so two roles with equal choices
    share the one client, as does a role whose choice equals the default. The `RoleModels`
    returned has the client of `configuration.default` as its `default`, and in its `roles`
    the client of each role `configuration.roles` names, and no other role. No request is
    made here.

    A choice's key is what `environment` holds under its `key_variable`, and no key when it
    has none. Its price is the choice's own `price`. A choice with none is given the price
    the environment sets, as `model_from_environment` reads
    `PR_VERIFICATION_INPUT_PRICE` and `PR_VERIFICATION_OUTPUT_PRICE`, and no price when the
    environment sets none.

    Raises `InvalidConfiguration`, with a message that names the variable and the model that
    needs it, when a choice has a `key_variable` that `environment` does not hold or holds
    as the empty string. Raises `ModelFailed` as the clients and `model_from_environment` do,
    for a key that cannot be sent and for a price that is not a number of 0 or more.
    """
    clients: dict[ModelChoice, Model] = {}
    for choice in (configuration.default, *configuration.roles.values()):
        if choice not in clients:
            clients[choice] = _client(choice, environment, transport)
    return RoleModels(
        clients[configuration.default],
        {role: clients[choice] for role, choice in configuration.roles.items()},
    )


def _client(
    choice: ModelChoice, environment: Mapping[str, str], transport: httpx.BaseTransport | None
) -> Model:
    """The client that reaches the model `choice` names, as `configured_models` says."""
    key = ""
    if choice.key_variable is not None:
        key = environment.get(choice.key_variable, "")
        if not key:
            raise InvalidConfiguration(
                f"{choice.key_variable} is not set, and {choice.model} at {choice.endpoint} "
                "needs it for its key"
            )
    price = choice.price if choice.price is not None else _price_from(environment)
    made: type[OpenAICompatibleModel] | type[AnthropicModel] = OpenAICompatibleModel
    if choice.protocol is ModelProtocol.ANTHROPIC:
        made = AnthropicModel
    return made(
        choice.endpoint,
        choice.model,
        key,
        transport,
        price,
        parallel_tool_calls=choice.parallel_tool_calls,
    )
