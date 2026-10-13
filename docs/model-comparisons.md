# Configurations, and comparing them

A configuration says which model does each role's work and which orchestration runs the roles
(`pr_verification.configuration`). Three are named:

| Name | Models | For |
| --- | --- | --- |
| `default` | MiniMax M3 in every role | Everyday use |
| `self-hosted` | Qwen 3.8 on this machine, through Ollama, in every role | Use with no request leaving the machine and no key |
| `ceiling` | Claude Sonnet 5.5 in the reasoning roles, MiniMax M3 in the Claim extractor and behind the decider | The evaluation only |

Each uses the fixed pipeline unless told otherwise.

| Variable | Meaning | When unset |
| --- | --- | --- |
| `PR_VERIFICATION_CONFIGURATION` | `default` or `self-hosted` | One model for every role, as `docs/model-endpoints.md` describes |

With it set, `PR_VERIFICATION_ENDPOINT` and `PR_VERIFICATION_MODEL` are not read, and each model
takes its key from the variable its configuration names: `PR_VERIFICATION_API_KEY` for MiniMax
M3, `ANTHROPIC_API_KEY` for Claude Sonnet 5.5, none for the local model. A model the project
knows no price for is priced by `PR_VERIFICATION_INPUT_PRICE` and `PR_VERIFICATION_OUTPUT_PRICE`.
Any other value, `ceiling` among them, exits with code 2 and no report. The command line reads
the variable. The GitHub Action does not, and configures its one model as before.

`self-hosted` keeps the models on the machine. The decision layer is configured apart
(`docs/decision-layer.md`): Laya runs in the process, and Jev is reached over the network, so
a run that is to stay on the machine does not choose Jev.

## Two optional capabilities

| | Prompt caching | Parallel tool calls |
| --- | --- | --- |
| Anthropic-protocol client | Always | For a model it is told can be relied on: Claude Sonnet 5.5 |
| OpenAI-compatible client | Never | For a model it is told can be relied on: none of the named ones |

- **Prompt caching.** Every request of the supervisor says its instructions are worth keeping,
  since they are the same on every step and for every pull request. A client that cannot keep
  them sends the request it always sent.
- **Parallel tool calls.** A role run on a model that offers them may ask for up to 4 tools
  in one step. They are run one after another, so what is saved is requests to the model. The
  results of one step share the limit on what a role is shown of one result
  (`docs/context-bound.md`), so asking for more at once shows a role no more.

On a provider that offers neither, every request is what it was before either existed.

## The reading-only baseline

`pr_verification.baseline` verifies a pull request by reading alone: for each Claim, a role
with the read-only tools reads the code before and after the pull request and states a Verdict
itself. It writes no Reproduction and has no Sandbox. It is given the same Claims, the same
model, the same reading tools, and the same step cap as the system, so that the difference
running code makes is a measured number.

- It is this project's own stand-in for judging by reading. It is not a measurement of any
  other tool, and says nothing of how any other tool would do on these cases.
- Its prompt was tuned less than the system's: it was written once and never revised against
  a case, where the system's prompts were worked on throughout development. So it somewhat
  understates what reading alone could do.
- It exists for the evaluation only. It is no configuration and no orchestration, so no setting
  of the command-line tool or the GitHub Action selects it, and wherever its numbers are
  printed they are under `reading-only baseline`.

## Running a comparison

`tools/compare_configurations.py` runs the development part of the hand-made cases, which are
all Behavioural, once for each `--run`, and prints the False Block rate, the False Assurance
rate, the Decided Rate, and the seconds and the dollars for a pull request side by side:

```sh
# Supervisor against fixed pipeline, and the reading-only baseline against the system
uv run --extra laya python tools/compare_configurations.py --repetitions 3 \
  --run default/pipeline --run default/supervisor --baseline default

# Ceiling against default, and self-hosted against default
uv run --extra laya python tools/compare_configurations.py --repetitions 3 \
  --run default/pipeline --run ceiling/pipeline --run self-hosted/pipeline

# Two models in the light roles: the Claim extractor and the small LLM behind the decider
uv run --extra laya python tools/compare_configurations.py --repetitions 3 \
  --light luna ENDPOINT MODEL KEY_VARIABLE INPUT_PRICE OUTPUT_PRICE \
  --light flash-lite ENDPOINT MODEL KEY_VARIABLE INPUT_PRICE OUTPUT_PRICE \
  --run luna/pipeline --run flash-lite/pipeline

# What prompt caching and parallel tool calls save
uv run --extra laya python tools/compare_configurations.py --repetitions 3 \
  --run ceiling/supervisor --capabilities
```

`--clones DIRECTORY` adds the Structural cases, and `--held-out` the held-out part. With
`--capabilities` each run is made twice more, once with no instructions kept and once with no
model offering parallel tool calls.

## Supported and experimental providers

A provider is **supported** when a configuration that has its model in the reasoning roles
passes this, and **experimental** otherwise:

- the development part of the hand-made cases and of the Structural cases, three repetitions,
  with the fixed pipeline;
- no False Block;
- a Decided Rate of 0.6 or more.

The threshold is a proposal made with this write-up, and no run has been held to it yet.

| Provider | Status | Why |
| --- | --- | --- |
| MiniMax M3, hosted | Experimental | Not yet run three times. One repetition on 2026-10-12 gave no False Block and a Decided Rate of 0.72 (`docs/context-bound.md`) |
| Qwen 3.8, through Ollama | Experimental | Never evaluated: the model is not on the machine |
| Claude Sonnet 5.5, through the Anthropic protocol | Experimental | Never evaluated: no request has been made to it |

## Results

**None yet.** On 2026-10-13, when the clients and the configurations were implemented, the
machine held no key for any hosted model and Ollama had no `pr-verification-qwen`, so no
comparison could be run. What was checked that day:

- `tools/compare_configurations.py --run self-hosted/pipeline --baseline self-hosted
  --capabilities` ran to the end against Ollama. Every run ended with no report, on the
  endpoint's HTTP 404 for the missing model, and the comparison printed each run under its
  name with the baseline last.
- `PR_VERIFICATION_CONFIGURATION=ceiling` and `=baseline` each exit with code 2, no report,
  and one line naming the variable.

Still to run and record here, each with the command above:

- the supervisor against the fixed pipeline;
- the ceiling against the default;
- self-hosted against the default;
- GPT-5.6 Luna against Gemini 3.5 Flash-Lite in the light roles;
- the reading-only baseline against the system on the Behavioural cases;
- the effect of prompt caching and of parallel tool calls on time and cost.

## What to keep in mind when reading the results

- **Prompts were tuned on the default configuration.** Every role's instructions were written
  and revised against MiniMax M3. So a ceiling run somewhat understates the stronger model: it
  is asked in wording shaped to another model's habits.
- **Prompt caching may save nothing as it stands.** A provider keeps instructions only above a
  minimum length, which Anthropic has given as 1,024 tokens or more. The supervisor's
  instructions are 1,967 characters, about 500 tokens, and they are all that is asked to be
  kept. Unless the measurement shows otherwise, expect the `no prompt caching` run to cost the
  same.
- **Parallel tool calls depend on the model using them.** A role is told of the form and is not
  made to use it. The measurement shows what a model saved, not what it could have.
- **Dollars are at the prices written here.** Claude Sonnet 5.5 is priced at 2 and 10 dollars
  for a million tokens in `pr_verification.configuration`. MiniMax M3 and the local model have
  no price there and take the environment's.
