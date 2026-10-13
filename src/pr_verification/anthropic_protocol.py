"""The model interface over the Anthropic protocol: a second real client beside the
OpenAI-compatible one.

From the point of view of the model interface the two behave the same: a request goes in, the
response's text comes out, and whatever goes wrong is a `ModelFailed` whose message is safe to
print. What differs is what is sent and what comes back, and that this client offers both
optional capabilities of the interface: prompt caching always, and parallel tool calls for
a model it is told can be relied on for them.
"""

import httpx

from pr_verification.model import ModelRequest, PricedResponse, TokenPrice

ANTHROPIC_ENDPOINT = "https://api.anthropic.com/v1"
"""The endpoint of the protocol's own provider, up to and including `/v1`."""

ANTHROPIC_VERSION = "2023-06-01"
"""The version of the protocol, sent with every request as the header `anthropic-version`."""

MAX_TOKENS = 16_000
"""The most tokens one response may hold, sent with every request as `max_tokens`. The
protocol does not take a request without it."""

CACHE_WRITE_MULTIPLE = 1.25
"""What a token the provider was asked to keep costs when it is first sent, as a multiple of
the price of a token sent."""

CACHE_READ_MULTIPLE = 0.1
"""What a token the provider had kept costs when it is sent again, as a multiple of the price
of a token sent."""


class AnthropicModel:
    """A model reached through the Anthropic protocol. It is a `PricedModel`."""

    parallel_tool_calls: bool
    """Whether the model offers parallel tool calls, as `offers_parallel_tool_calls`
    (`pr_verification.model`) reads it. False unless the model was made with it true."""

    def __init__(
        self,
        endpoint: str,
        model: str,
        key: str,
        transport: httpx.BaseTransport | None = None,
        price: TokenPrice | None = None,
        *,
        parallel_tool_calls: bool = False,
    ) -> None:
        """`endpoint` is the base URL up to and including `/v1`, with or without a `/` after
        it, and `model` is the model's name as the endpoint knows it. `transport` is for a
        caller that brings its own, as a test does: with it no connection is opened.

        `key` is sent without the whitespace around it. A key that is empty then is not
        sent at all, as suits an endpoint that needs none. Raises `ModelFailed` with the
        message `the key for <model> at <endpoint> holds characters that cannot be sent`
        when the key holds anything but printable ASCII: the message does not show the key.

        `price` is what the provider charges for this model, or None for a model that has
        no price. It changes nothing about `complete`. `parallel_tool_calls` is the
        model's attribute of that name, false unless given as for the OpenAI-compatible
        client, and changes nothing about a request.
        """
        raise NotImplementedError

    def complete(self, request: ModelRequest) -> str:
        """Make one request to the endpoint and return the text of its answer.

        The request
            One `POST` to `<endpoint>/messages`, with the headers `anthropic-version`, which
            is `ANTHROPIC_VERSION`, and `x-api-key`, which is the key, and is left out when
            there is no key. Nothing is retried, and an answer is waited for for up to five
            minutes, as by the OpenAI-compatible client.

            Its body is a JSON object with exactly these members: `model`, the model's name;
            `max_tokens`, which is `MAX_TOKENS`; `system`, which carries
            `request.instructions`; and `messages`, a list of the one message
            `{"role": "user", "content": request.data}`. So instructions and data travel
            apart, and Untrusted Content stays data.

        Prompt caching
            With `request.cache_instructions` false, `system` is the instructions as a
            string. With it true, `system` is a list of the one block
            `{"type": "text", "text": request.instructions,
            "cache_control": {"type": "ephemeral"}}`, which asks the provider to keep the
            instructions. Nothing else of the request differs, and the answer is read the
            same.

        The response
            The answer's `content` is a list of blocks. The response is the `text` of each
            block whose `type` is `text`, in order, joined with nothing between them, then
            without any thinking the model left in front of it as `<think>...</think>`, then
            without the whitespace around it. A block of any other type, such as the
            model's thinking, is no part of the response.

        Failures
            Each of these raises `ModelFailed`, where `<name>` is `<model> at <endpoint>`:

            - the endpoint could not be reached, timed out, or is not a URL:
              `<name> could not be reached: <what went wrong>`;
            - the answer's status is not a success:
              `<name> answered with HTTP <status>: <the answer's body>`;
            - the answer is not JSON, has no such `content`, or its text blocks hold no text
              once read as above: `<name> gave a response that holds no text: <the answer's
              body>`.

            The message is one line: every run of whitespace in it is one space. The key,
            when there is one, is replaced wherever it appears by `[key removed]`, since a
            provider may repeat it back. A message longer than 500 characters is cut to 500
            and ends ` [cut short]`. The error that caused it is not chained.

        The request is recorded as one request to a model (`pr_verification.tracing`), with
        the model's name as `gen_ai.request.model`, and with `gen_ai.usage.input_tokens` and
        `gen_ai.usage.output_tokens` when the answer's `usage` holds `input_tokens` and
        `output_tokens` as integers.
        """
        raise NotImplementedError

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        """Make the one request `complete` makes, and say what it cost at the model's price.

        The response is the one `complete` gives, and whatever `complete` raises is raised.

        The protocol reports the tokens of a request in the `usage` of its answer.
        `dollars` is the sum of, each price being for a million tokens:

        - `usage.input_tokens` at `price.input`;
        - `usage.output_tokens` at `price.output`;
        - `usage.cache_creation_input_tokens` at `price.input` times `CACHE_WRITE_MULTIPLE`;
        - `usage.cache_read_input_tokens` at `price.input` times `CACHE_READ_MULTIPLE`.

        So 1,000 tokens sent and 500 returned, at 2.0 and 10.0, cost 0.007, and 1,000,000
        tokens read from what the provider had kept cost 0.2 at that price, where sending
        them afresh costs 2.0. Either of the last two counts is taken as 0 when it is
        missing or is not a whole number that is 0 or more.

        `dollars` is 0.0 when the model was given no price, and when the answer has no
        `usage` holding both of the first two counts as whole numbers that are 0 or more:
        what is not known is not charged for.
        """
        raise NotImplementedError
