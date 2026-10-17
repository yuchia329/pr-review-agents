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

A provider is **supported** when a run with its model meets both limits of ADR 0006, and
**experimental** otherwise: a False Block rate of at most 2% of true Claims and a False
Assurance rate of at most 5% of false Claims, read on the held-out part over at least three
repetitions. No run here has used the held-out part, so no provider can be supported yet.

A limit met says only as much as the number of distinct Claims it is read over. The same
Claim in three repetitions is one Claim. With no error in n distinct Claims, the upper end of
a one-sided 95% interval is `1 - 0.05^(1/n)` [ls-21]: 2% can be shown only after no False
Block in 149 true Claims, and 5% only after no False Assurance in 59 false Claims. The
harness's own check of the limits is #71. The keys in square brackets here are those of the
Sources table in `docs/prior-art.md`.

| Provider | Status | Why |
| --- | --- | --- |
| MiniMax M3, hosted | Experimental | Never run on the held-out part. On the development part, three repetitions of the two hand-made cases gave no False Block and no False Assurance, over two distinct Claims of each kind at most |
| Qwen 3.8, through Ollama or vLLM | Experimental | Never run on the held-out part. One repetition of the two hand-made cases, through vLLM, gave no error |
| Claude Sonnet 5.5, through the Anthropic protocol | Experimental | Never evaluated |

## Results

**One small run so far, below.** On 2026-10-13, when the clients and the configurations were
implemented, the machine held no key for any hosted model and Ollama had no
`pr-verification-qwen`, so no comparison could be run on the named configurations. What was
checked that day:

- `tools/compare_configurations.py --run self-hosted/pipeline --baseline self-hosted
  --capabilities` ran to the end against Ollama. Every run ended with no report, on the
  endpoint's HTTP 404 for the missing model, and the comparison printed each run under its
  name with the baseline last.
- `PR_VERIFICATION_CONFIGURATION=ceiling` and `=baseline` each exit with code 2, no report,
  and one line naming the variable.

- Ollama was given `pr-verification-qwen`, built from `ollama/Modelfile` on `qwen3.8:27b`
  with its MLX runner, and `--run self-hosted/pipeline --run self-hosted/supervisor --baseline
  self-hosted --repetitions 1` was started with `PR_VERIFICATION_DECISION=small_llm`. It was
  stopped by hand after 7 minutes, during the first of its four runs, so that the model could
  be served another way. The model answered and a Sandbox was running when it was stopped. The
  tool printed nothing until every run had ended, so no number came of it. It now prints each
  run's scores as that run ends.
- An earlier start of the same command waited two hours behind another measurement that held
  Ollama, which answers one request at a time, and was stopped before it made a request.

Qwen is next to be served by vLLM on another machine. `self-hosted` names
`http://localhost:11434/v1` and the model `pr-verification-qwen`, so it measures that server
unchanged only when the server is reached at that address, as through a forwarded port, and
serves the model under that name. A request then leaves the machine, which is not what
`self-hosted` is named for, and the write-up of that run must say so.

### The default configuration, 2026-10-13

MiniMax M3 on its hosted endpoint in every role and behind the decider
(`PR_VERIFICATION_DECISION=small_llm`), priced at 0.30 and 1.20 dollars for a million tokens.
The Budget was 900 seconds, 0.50 dollars, and 300 steps for a pull request. The Sandbox was
Docker 28.4.0 on an Apple M4 Pro.

```sh
uv run python tools/compare_configurations.py --repetitions 3 \
  --run default/pipeline --run default/supervisor --baseline default
```

The two hand-made development cases, three times each: `median-of-an-even-count`, whose
Claims are true, and `initials-of-a-hyphenated-name`, whose Claims are false. Every run gave a
report.

| | Fixed pipeline | Supervisor | Reading-only baseline |
| --- | --- | --- | --- |
| Claims | 11 | 8 | 12 |
| Decided Rate | 0.36 | 0.38 | 0.83 |
| False Block rate | 0 | 0 | 0.33 |
| False Assurance rate | 0 | 0 | 0 |
| Seconds for a pull request | 475 | 495 | 23 |
| Dollars for a pull request | 0.039 | 0.079 | 0.006 |
| Runs stopped by the time limit | 0 of 6 | 3 of 6 | 0 of 6 |

**How much this can show.** Each case is one pull request, and the Claim extractor split each
into one or two Claims, worded afresh in every run. So each rate rests on two distinct Claims
of each kind at most, whatever the repetitions. No error in two Claims leaves the upper end
of a one-sided 95% interval at 78%: these runs cannot show that any rate is under either
limit of ADR 0006, and the baseline's False Blocks show only that its rate is not zero.

- **The baseline blocked a sound pull request; the system never did.** In two of its three
  runs of the true case, the baseline gave a true Claim Refuted, so the Gate would have
  blocked. That is the error the system exists to keep down, and neither orchestration made
  it once.
- **Published measurements show the same kind of error.** Models asked whether code meets its
  requirement without running it accepted up to 40.0% of buggy code and rejected up to 91.9%
  of correct code, depending on the model and the prompt, and longer prompts made it worse
  [ls-22]. Against test results, GPT-4.1-mini as a judge gave 23% false positives and 16%
  false negatives [ls-23]. The baseline is this project's own stand-in for judging by reading
  and measures none of those tools, but its Refuted true Claim is the error they report.
- **The baseline decided more, and faster.** It decided 10 of 12 Claims in 23 seconds a pull
  request, where the system decided 4 of 11 and 3 of 8 in about 8 minutes. On cases this small
  reading is enough, and a wrong Refuted is the price of it here.
- **The system never decided the false case, and by its own rule it cannot.** Every one of its
  Claims there was Inconclusive, in all six runs. The pull request claims
  `initials("Mary-Jane Watson")` now returns `"MJW"`, and changes nothing of what the code
  does, so a Reproduction of the Claim fails on both commits. `compute_verdict` gives Refuted
  only for a regression, a test that passed before and fails after, and says that one failing
  on both commits "does neither: the Claim may be false, or the Reproduction may be wrong".
  That rule is the control on False Blocks. Its price is that a false Claim about a change
  that did nothing is never Refuted, whatever the model, and the writer and the judge go round
  until the round cap or the Budget ends them: 591 to 874 seconds with the fixed pipeline,
  and the time limit in all three runs with the supervisor.
- **The supervisor cost twice what the fixed pipeline did** and decided about the same: 50 to
  64 steps on the false case against 15 to 24. No published source compares an LLM supervisor
  over roles with a fixed pipeline. Cursor and GitHub compared an agent inside one reviewer
  with a fixed flow [bv-01] [bv-13], which is another question.
- **The baseline's prompt changed after this run.** It showed its answer as the bare
  `{"verdict": ..., "reason": ...}`, not inside `{"finish": ...}` as the loop reads it, the
  mistake #66 removed from the other roles. It is now shown whole. A rerun may differ.
- **The dollars count only requests made through the ledger.** The decider's requests are not
  among them, so the bill is somewhat more. All 18 runs counted 0.75 dollars together.

### The ceiling, 2026-10-14

Claude Sonnet 5.5 in the reasoning roles and MiniMax M3 in the Claim extractor and behind the
decider, with the fixed pipeline, once on each of the two hand-made cases. The Budget was 1.50
dollars a pull request, raised from 0.50 so that the money limit would not stand in for the
model; the rest as above.

```sh
PR_VERIFICATION_BUDGET_DOLLARS=1.50 uv run python tools/compare_configurations.py \
  --run ceiling/pipeline --repetitions 1
```

| Case | Expected | Verdicts | Seconds | Dollars | Steps | Stopped by |
| --- | --- | --- | --- | --- | --- | --- |
| `median-of-an-even-count` | Confirmed | Confirmed, Confirmed | 49 | 0.05 | 14 | nothing |
| `initials-of-a-hyphenated-name` | Refuted | Inconclusive, Inconclusive | 367 | 1.08 | 33 | nothing |

- **Claude did on the true case what MiniMax did, faster.** Both Claims Confirmed in 49
  seconds, where MiniMax took 54 to 423.
- **The false case stayed Inconclusive, as the rule above says it must.** A stronger model
  does not change it: it spent 1.08 dollars, 95% of the run's cost, on a Claim the system
  cannot Refute. At the 0.50 cap the run would have been stopped by money.
- **One run on two cases**, so nothing here compares the ceiling with the default beyond that.

### Qwen 3.8 on another machine, 2026-10-13

Qwen 3.8 (`Qwen/Qwen3.8-27B-FP8`) served by vLLM on two RTX A4500 cards, with a window of
32768 tokens, reached through a forwarded port. It was in every role and behind the decider
(`PR_VERIFICATION_DECISION=small_llm`), under a configuration made with `--every-role`. This is
not `self-hosted`: every request left the machine. The Sandbox was Docker 28.4.0 on an Apple
M4 Pro.

```sh
PR_VERIFICATION_DECISION=small_llm uv run python tools/compare_configurations.py \
  --every-role qwen-remote http://localhost:18000/v1 Qwen/Qwen3.8-27B-FP8 QWEN_REMOTE_KEY \
  --run qwen-remote/pipeline --run qwen-remote/supervisor --baseline qwen-remote
```

The two hand-made development cases, once each. Every run gave a report.

| | Fixed pipeline | Supervisor | Reading-only baseline |
| --- | --- | --- | --- |
| Claims | 4 | 4 | 5 |
| Decided Rate | 0.50 | 0.50 | 1.00 |
| False Block rate | 0 | 0 | 0 |
| False Assurance rate | 0 | 0 | 0 |
| Seconds for a pull request | 587 | 824 | 165 |
| Dollars for a pull request | 0 | 0 | 0 |

- **Two pull requests are too few to rank anything.** Each rate is a share of four or five
  Claims from one repetition, and `docs/structural-cases.md` records that a Verdict varies
  from run to run.
- **The baseline decided every Claim and the system half of them, on these two cases.** The
  baseline states a Verdict from the model's word, so it is Inconclusive only when the model
  says so. The system is Inconclusive whenever no Reproduction was written, run, and accepted.
  Nobody was wrong here: no run gave a False Block or a False Assurance. These cases are small
  enough to judge by reading, which is not where a Reproduction is expected to earn its time.
- **Why the system's two Claims were Inconclusive is not known.** The tool prints scores and
  not reasons. A pull request took 587 and 824 seconds against a Budget of 900, so the time
  limit may have ended the work: that was not checked.
- **The supervisor took 1.4 times as long as the fixed pipeline and decided the same.** The
  server was shared with at least one other user's requests, so the times are not clean.
- **The runs did not score the same Claims.** The Claim extractor split the two pull requests
  into four Claims in two runs and five in the third.
- **Not measured:** more than one repetition, the held-out part, the Structural cases, and the
  model through Ollama on this machine.

Qwen 3.8 stays experimental: it has not been run on the held-out part that ADR 0006 reads.

Still to run and record here, each with the command above:

- the ceiling against the default, more than once and on more cases;
- self-hosted against the default;
- GPT-6 Luna (`gpt-6-luna`, 0.10 and 0.50 dollars for a million tokens) against Gemini 3.5
  Flash-Lite (`gemini-3.5-flash-lite`, 0.30 and 2.50) in the light roles. The project owner
  chose GPT-6 Luna over the GPT-5.6 Luna the spec names, as newer and cheaper;
- the reading-only baseline against the system on more Behavioural cases than the two
  hand-made ones;
- the effect of prompt caching and of parallel tool calls on time and cost.

## The supervisor against the fixed pipeline, 2026-10-16 and 17

Where the multi-agent design does better than the fixed pipeline, and what it costs (#47,
amended 2026-10-16). Two experiments, each under both orchestrations with the same models, and
every run timed on the clock as well as in the Budget's seconds of work.

**The Budget's seconds are not time on the clock.** They are seconds of work, and work done at
the same time is each counted. The supervisor works on up to four Claims at once, so its
seconds of work run ahead of the clock: in one run it had spent 907 seconds of work in 584
seconds. The comparison's `seconds_per_pull_request` is seconds of work. How long a pull
request took is `clock_seconds`, which the two tools below print for every run.

### A: the development part of the Structural cases

18 pull requests from open-source projects, 12 whose Claims are true and 6 with part of the
change left out, whose Claims are false. Budget 900 seconds, 0.50 dollars, and 300 steps, with
`PR_VERIFICATION_DECISION=small_llm`. Qwen 3.8 has no price, so only time and steps bound it.

```sh
PR_VERIFICATION_BUDGET_DOLLARS=0.50 uv run python tools/compare_configurations.py \
  --clones ~/.cache/pr-verification --no-hand-made \
  --run default/pipeline --run default/supervisor --repetitions 3

uv run python tools/compare_configurations.py --clones ~/.cache/pr-verification --no-hand-made \
  --every-role qwen-remote http://localhost:18000/v1 Qwen/Qwen3.8-27B-FP8 QWEN_REMOTE_KEY \
  --run qwen-remote/pipeline --run qwen-remote/supervisor --repetitions 2
```

| | MiniMax M3, pipeline | MiniMax M3, supervisor | Qwen 3.8, pipeline | Qwen 3.8, supervisor |
| --- | --- | --- | --- | --- |
| Repetitions | 3 | 3 | 2 | 2 |
| Claims | 89 | 83 | 63 | 63 |
| Decided Rate | 0.72 (64) | 0.73 (61) | 0.73 (46) | 0.73 (46) |
| False Block rate | 0 | 0.04 | 0 | 0 |
| False Assurance rate | 0 | 0 | 0 | 0 |
| Seconds on the clock, median | 41.2 | **33.9** | 125.0 | 121.2 |
| Seconds on the clock, mean | 93 | 90 | 269 | 266 |
| Dollars for a pull request | 0.027 | 0.028 | 0 | 0 |
| Runs stopped by the time limit | 0 of 54 | 2 of 54 | 0 of 36 | **6 of 36** |

Qwen 3.8 was served by vLLM on `sicong`, as above; its two runs went side by side with
MiniMax M3's, and both used this machine's Docker.

- **The two decided as much as each other**: 0.72 against 0.73 on MiniMax M3, and the same 46
  of 63 Claims on Qwen 3.8.
- **The supervisor's two False Blocks are one pull request.** Both are
  `rename/packaging-private-format-full-version`, which the fixed pipeline also Refuted in the
  first run of A. The rates rest on 12 pull requests with true Claims and 6 with false ones, at
  one to three Claims each, so on at least 12 distinct true Claims. No error in 12 leaves the
  upper end of a one-sided 95% interval at 22%.
- **On MiniMax M3 the supervisor was quicker in the middle of the range**: 33.9 seconds on the
  clock against 41.2 at the median, 18% less, at the same cost. The means are within 3 seconds
  of each other. On Qwen 3.8 the two took the same.
- **The time limit stops the supervisor and not the pipeline.** Every run stopped by the limit
  was the supervisor's, and all were renames. Counted in seconds of work, working on Claims at
  the same time spends the same limit faster, so the design meant to save time is the one the
  time limit cuts short. Five of the eight stopped with less than 900 seconds on the clock: at
  584, 688, 723, 840, and 899. The other three had run past 900 on the clock as well.

**A first run of A was made with Docker stopped**, on 2026-10-16, without anyone noticing: a
Claim sent to a Reproduction was Inconclusive at once. It gave about the same Decided Rates and
half the cost, and is not counted here.

### C: one pull request with four Behavioural Claims

Made for the measurement (`tools/measure_claims_together.py`): four small fixes in one module,
each stated as a Claim that fails its test before the pull request and passes it after. The
Budget and the decision setting were as in A.

```sh
PR_VERIFICATION_BUDGET_DOLLARS=0.50 uv run python tools/measure_claims_together.py --repetitions 3

uv run python tools/measure_claims_together.py --repetitions 2 \
  --every-role qwen-remote http://localhost:18000/v1 Qwen/Qwen3.8-27B-FP8 QWEN_REMOTE_KEY
```

| | MiniMax M3, pipeline | MiniMax M3, supervisor | Qwen 3.8, pipeline | Qwen 3.8, supervisor |
| --- | --- | --- | --- | --- |
| Repetitions | 3 | 3 | 2 | 2 |
| Claims Confirmed | 12 of 12 | 12 of 12 | 9 of 9 | 8 of 9 |
| Seconds on the clock | 83, 111, 117 | **83, 93, 97** | 178, 341 | 147, 874 |
| Seconds of work | 82, 110, 116 | 151, 163, 182 | 177, 334 | 234, 969 |
| Dollars for a pull request | 0.012 | 0.012 | 0 | 0 |

In one Qwen 3.8 run of each orchestration the Claim extractor found five Claims in the
description, not four, which is why each has nine.

- **On MiniMax M3 the supervisor took 93 seconds against 111 at the median, at the same cost
  and with every Claim Confirmed.** It did 1.6 times the work in less time, by working on the
  Claims together, which is the upside the design was built for. The ranges overlap: the
  fastest run of each took 83 seconds.
- **On Qwen 3.8 the two runs disagree with each other.** The supervisor's fastest run was the
  fastest of all, 147 seconds, and its slowest was stopped by the time limit after 874 seconds
  on the clock, with 969 seconds of work and one Claim Inconclusive. Two runs cannot say which
  is typical.

### What the two say together

- **The supervisor was as accurate as the fixed pipeline, at the same cost, and on MiniMax M3
  it was quicker at the median**: by 16% on a pull request with four Claims that each need a
  Reproduction, and by 18% on the Structural cases. On Qwen 3.8 it was not: the same on the
  Structural cases, and in C one fast run and one stopped by the time limit.
- **Its upside is cut short by a rule written for one Claim at a time.** The Budget's time limit
  counts seconds of work, so the more the supervisor works in parallel, the sooner it is
  stopped, though less time has passed. It was stopped 9 times in these runs, 6 of them with
  less than 900 seconds on the clock, and the pipeline never.
- **Nothing here is larger than its noise by much.** Three repetitions on MiniMax M3 and two on
  Qwen 3.8, on 18 pull requests and one made for the measurement. The speed-up is a lead to
  measure again, on more pull requests with several Claims, not a settled result.

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
