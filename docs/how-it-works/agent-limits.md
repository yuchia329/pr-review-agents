# Limits on an agent

Two limits keep a role's agent loop bounded: [what stops a loop](#loops-inside-one-agent) that repeats itself or makes no progress, and [how much of a tool result a role is shown](#the-bound-on-what-a-role-is-shown).

## Loops inside one agent

A role can go round in circles: take the same step again and again, go back and forth between
two steps, or take steps that all differ and bring it no closer to its goal. The agent loop
watches every run for this, and each time it finds it is a trigger. On the first rung of the
ladder the role is told what was found and goes on. Stopping a role that triggers again is the
next rung and is not built yet. The step cap holds whatever is found.

Two things find a loop, in this order, after every step that leaves the run going:

- **Repeat detection** is code. It compares the result of each step as well as its action, so
  a retry that comes out differently, as after a network failure during an install, is not
  flagged. Three steps the same are an exact repeat, and two rounds of the same two steps are
  an alternation.
- **The progress guard** is a decision, asked through the decision layer every four steps when
  no repeat was found: given its task and its latest steps, is the agent getting closer to its
  goal? An answer of no is a trigger. It is there for the role whose steps all differ.

The decider that classifies the Claims also answers the progress guard, so it is configured as
`docs/how-it-works/decision-layer.md` describes. Its answer steers a role's effort and is never a Verdict.

### False triggers of the progress guard

A false trigger is the progress guard telling a role that is getting there that it is not.
`tools/measure_progress_guard.py` counts them on the development part of the hand-made cases:

```sh
uv run --extra laya python tools/measure_progress_guard.py --repetitions 5
```

It runs each case through the core's entry point with the configured model and decider, and
prints every asking of the guard with the steps it was about. An asking counts as one of a role
that was getting there when it came in the attempt of the Reproduction writer that went on to
show a difference in behaviour. That is a coarse rule, which is why the steps are printed.

#### Why responses could not be read, 2026-10-13: the instructions showed bare answers

After #66 the responses left unreadable were looked at again, with every response of every role
recorded and sorted. Most came from the wording roles were given. Each role's own instructions
showed what it finishes with as a bare object, such as `{"accepted": true}`, while the loop's
wording asked for `{"finish": {...}}`, and the reading tools showed their arguments bare. A model
given both had to put them together itself, and often did not.

The wording was replaced in memory, on the code of #66, and the two hand-made development cases
were run through the core's entry point with each variant. The decider was the small LLM. The
variants:

- `baseline`: the wording as it was.
- `whole_form`: the writer told to finish with `{"finish": {"reproduction": "<text>"}}`.
- `rule`: `whole_form`, and a sentence that the reply starts with `{` and ends with `}`, with no
  reasoning before it, no code fence, and nothing after it.
- `one_shot`: `whole_form`, and one example of a step that asks for a tool and one that finishes.
- `few_shot_rule`: `whole_form`, four example replies, and the rule.
- `wrap`: the loop's wording saying that what a role's instructions say it finishes with goes
  inside `finish`, with an example. No role's instructions changed.
- `wrap_one_shot` and `wrap_rule`: `wrap` with the example, or with the rule.

Only the Reproduction writer and the Reproduction judge run through the agent loop on these cases,
so only they are counted. A response is unreadable as `run_role` reads it.

**Qwen3.8 27B (FP8, served by vLLM, which keeps the model's reasoning out of the response), four
pull requests a variant:**

| Variant | Writer unreadable | Judge unreadable | Claims decided |
| --- | --- | --- | --- |
| `baseline` | 3 of 38 | 8 of 13 | 5 of 10 |
| `whole_form` | 0 of 41 | 9 of 12 | 3 of 9 |
| `rule` | 0 of 37 | 3 of 8 | 5 of 9 |
| `one_shot` | 0 of 33 | 1 of 6 | 5 of 9 |
| `few_shot_rule` | 1 of 34 | 5 of 8 | 3 of 9 |
| `wrap` | 1 of 35 | 0 of 4 | 4 of 8 |
| `wrap_one_shot` | 2 of 34 | 1 of 5 | 4 of 8 |

Every unreadable response under `baseline` was the answer with no `finish` around it.

**MiniMax M3, two pull requests a variant:**

| Variant | Writer unreadable | Judge unreadable | Claims decided | Requests | Tokens sent / received | Seconds a pull request |
| --- | --- | --- | --- | --- | --- | --- |
| `baseline` | 8 of 33 | 2 of 4 | 2 of 5 | 48 | 66,942 / 61,252 | 713 |
| `wrap` | 0 of 18 | 1 of 2 | 1 of 3 | 25 | 32,989 / 18,392 | 368 |
| `wrap_rule` | 1 of 14 | 1 of 2 | 1 of 2 | 20 | 27,945 / 11,878 | 277 |

Under `baseline` all 8 of the writer's unreadable responses began with the model's reasoning, and 3
more were read only because of #66. Under `wrap` none began with reasoning. Every unreadable
response of the judge was the bare `{"accepted": true}` its own instructions showed.

What this showed:

- **The conflict between the two forms caused most unreadable responses.** Showing the writer its
  finish whole took its bare answers to none on Qwen3.8. Saying it once in the loop's wording, with
  an example, did the same for the judge on Qwen3.8 and stopped MiniMax M3 beginning with its
  reasoning, which had looked like a habit of the model.
- **Fewer wasted steps made the runs shorter.** Under `wrap` MiniMax M3 made half the requests and
  received under a third of the tokens.
- **More examples and a stricter rule added nothing** that these runs could show.
- **The judge's own example outweighed the loop's sentence**: `{"accepted": true}` stayed bare on
  both models whatever the loop said.
- **No variant changed a Verdict for the worse**: there was no False Block and no False Assurance.
  The number of Claims found and decided moved between runs as much as between variants.

Two to four pull requests a variant are too few for rates. Both models point the same way.

So every example a role is shown is now a whole response: each role's finish inside `finish`, each
tool's arguments inside a request for it, and the loop's wording says so with an example
(`Role.instructions`, `Tool.description`, and `run_role`, under #66).

The wording as shipped (c4fbd02) was run the same way on Qwen3.8, four pull requests: none of the
writer's 41 responses and none of the judge's 5 were unreadable, against 3 of 38 and 8 of 13 under
`baseline`. All four runs gave a report, 5 of 9 Claims were decided, and there was no False Block
and no False Assurance. It was not run again on MiniMax M3.

#### After #66, 2026-10-13: no false trigger in four askings, none of a role getting there

Since #66 the loop reads the object a response begins with and leaves unread what follows it.
The measurement was run again on the commit that made the change (170c45f), with the same model
and decider as on 2026-10-12, for one repetition of the two hand-made development cases. Each
pull request was held to 60 steps of the Budget, which neither reached.

| | Runs | Guard asked | Triggers | Asked of a role getting there | False triggers |
| --- | --- | --- | --- | --- | --- |
| 2026-10-12, one repetition | 2 | 3 | 2 | 1 | 0 |
| 2026-10-13, one repetition | 2 | 4 | 1 | 0 | 0 |

**The rate of false triggers is not known from this run**: the guard was never asked about a
role that was getting there, so there is nothing to divide by. Both runs gave a report. Of the 4
Claims, 2 were true and 2 false; 2 were decided and 2 Inconclusive, with no False Block and no
False Assurance. A pull request took 222 seconds. The two runs made 49 requests, which sent
69,701 tokens and were answered with 62,609.

What reading the steps of the four askings showed:

- **The guard was not asked about the true Claim.** On 2026-10-12 it was, because the writer's
  finish for the median case was followed by a second object and could not be read. Here that
  case ended before a fourth step. This is what #66 was to bring, seen once.
- **All four askings were on the false Claim**, where no Reproduction can show a difference.
  The guard said no once, at 0.85, and yes three times at 0.60 to 0.70 of a writer that was
  listing and reading the same file again.
- **No step shown to the guard was unreadable for what followed its object.** One response was
  a request for a tool named `finish` followed by `{"finish": false}`. It was read as the
  request for a tool it begins with and refused, as #66 decided.
- **Responses that begin with the model's reasoning are still unreadable**, and were 4 of the
  16 steps shown: the model wrote a paragraph that starts "Looking at this" and no object
  first. #66 did not change that reading.

Two runs are too few to compare the rows as rates. `PROGRESS_INTERVAL` is still kept at four.

A local model, `qwen3.8:27b-mlx` through Ollama with its context size fixed at 32768, was also
run on these cases the same day: one repetition on this commit and one on an earlier one. All
four runs gave a report, so it can do the Reproduction writer's work, and a pull request took
645 seconds on this commit. It never wrote anything
after a request that could be read. What it wrote that could not be read was the answer with no
`finish` around it, a finish that was never closed, and reasoning before the object.

#### Results, 2026-10-12: one false trigger in three askings

The model was MiniMax M3 on its hosted endpoint, in every role. The decider was the small LLM
(`PR_VERIFICATION_DECISION=small_llm`), so the same model answered the guard. The tool ran the
two hand-made development cases twice: once with one repetition, to see whether the guard would
be asked at all, and once with five.

| | Runs | Guard asked | Triggers | Asked of a role getting there | False triggers |
| --- | --- | --- | --- | --- | --- |
| One repetition | 2 | 3 | 2 | 1 | 0 |
| Five repetitions | 10 | 10 | 3 | 2 | 1 |
| Both | 12 | 13 | 5 | 3 | 1 |

**The rate of false triggers is 1 of 3 by the tool's rule**, and 1 of 2 in the five repetitions
alone. Three askings are too few to call that a rate with any confidence: it says the guard can
be wrong about a role that is getting there, and not how often.

All 12 runs gave a report. Of the 13 Claims, 7 were true and 6 false; 7 were decided and 6
Inconclusive, with no False Block and no False Assurance. One case makes a true Claim and the
other a false one. A pull request took 185 seconds in the five repetitions. No price was set,
so its cost is not known.

What reading the steps of all 13 askings showed:

- **The one false trigger described its steps fairly.** The writer had listed the files, read
  the one file twice, and then asked to list the files again in a response that could not be
  read. The guard said no at 0.85. The attempt went on to show the difference, which is why the
  rule counts the trigger as false, but in those four steps the role had not moved since the
  second. Read that way there is no false trigger among the three.
- **The other four triggers were on the false Claim**, where no Reproduction can show a
  difference and the writer was not getting there. They were right.
- **The guard missed more than it found there.** It was asked 10 times about the writer of the
  false Claim and said no 4 times. In the other 6 the writer was listing, reading, and searching
  the same file of three lines again, and the guard said yes at 0.65 to 0.85.
- **The guard came due because responses could not be read, not because the work was long.**
  In 11 of the 13 askings at least one of the four steps was a response the loop could not
  read: a finish or a request for a tool with a code fence or a second object after it, or
  with the model's reasoning before it. Each repository is one file, and a writer whose
  responses were all read could finish in three steps. #66 holds the steps and the decision
  this needs.

`PROGRESS_INTERVAL` is **kept at four**. Nothing measured here argues for another value: the
three askings of a role getting there are too few to choose from, and most of the steps the
guard was shown were responses that could not be read, which a change to the interval does not
touch.

#### Earlier, 2026-10-11: two local models, the guard never asked

Neither model on hand could do the Reproduction writer's work for four steps. Both were run
through Ollama 0.33.3 on the machine of `docs/how-it-works/decision-layer.md`, each with its context size
fixed at 32768, and with the default decider: Laya, with the same model behind it as the small
LLM.

| Model | Runs | Runs with a Verdict | Claims | Decided | Guard asked | Triggers |
| --- | --- | --- | --- | --- | --- | --- |
| `qwen3-vl:8b` | 2 | 0 | 0 | 0 | 0 | 0 |
| `llava:7b` | 6 | 3 | 7 | 0 | 0 | 0 |

- **`qwen3-vl:8b` never got past the Claim extractor.** Both runs ended in the client's
  five-minute timeout. Asked directly for the Claims of a pull request of two sentences, it
  wrote 3,200 to 8,200 tokens of thinking before its answer, at 38 tokens a second on an idle
  machine and 13 on a busy one. Neither `reasoning_effort` on the OpenAI-compatible endpoint
  nor `think` on Ollama's own turned the thinking off.
- **`llava:7b` answered in seconds and no run of the writer reached four steps.** All 7 Claims
  were Inconclusive, at about 8 seconds for a pull request. The guard comes due after the
  fourth step of a run that is still going, so it was never asked.

## The bound on what a role is shown

A role's context holds its task and what it reads. The step cap and the cap on attempts limit
how many things a role reads. Two more limits, enforced by code, are on how big each one may be
when it is shown to a model.

| Variable | Meaning | When unset |
| --- | --- | --- |
| `PR_VERIFICATION_TOOL_RESULT_LIMIT` | The most characters a role is shown of one result of a tool | 20000 |
| `PR_VERIFICATION_SANDBOX_OUTPUT_LIMIT` | The most characters the Reproduction writer is shown of the output of one run in the Sandbox from an earlier attempt | 8000 |

Each is a whole number, 200 or more. A limit that is not exits with code 2 and no report. A
variable set to an empty string counts as unset.

A text within its limit is shown whole. A longer one is cut by counting characters, and the cut
says so and says how much is missing:

- a tool's result keeps its start and ends with
  `[Cut here: the last 61261 of 81200 characters are left out.]`;
- the output of a run keeps its end, where a test run says what went wrong, and starts with
  `[Cut here: the first 4161 of 12100 characters are left out.]`.

Only what a model is shown is cut. The record of a run, the Evidence, and the report keep every
text whole, and repeat detection compares the whole.

A role reads a file too long to show whole in parts: `read_file` takes a `first_line` and a
`last_line`, and each line it returns keeps its own number in the file.

Limits are counted in characters, not in tokens, so how much of a model's window a request
takes depends on the model and on the text.

### The largest request a role can make

A role's request holds its instructions, its task, and every step so far. With the step cap at
12, as it is for the locator, the Structural verifier, the reviewer, and the Reproduction
writer, the last request of a run holds 11 results. So with the limits above, the results of
one request are bounded by:

| Role | What its request can hold of what it read | Characters |
| --- | --- | --- |
| Locator, Structural verifier, reviewer | 11 results of a tool | 220,000 |
| Reproduction writer, third attempt | 11 results of a tool, and two runs of each of two earlier attempts | 252,000 |

The instructions, the task, and the model's own responses come on top, and are not bounded
here. For the writer the task also holds its earlier Reproductions, of up to 20,000 characters
each.

Before there was a bound, none of this had a limit: one `read_file` of a long file, or one
`search` for a common text, could be larger than any window by itself.

### Measuring

`tools/measure_context_bound.py` runs the development part of the hand-made cases and of the
Structural cases within the limits the environment configures, and prints the largest request
sent to the model with what the harness scored:

```sh
uv run --extra laya python tools/measure_context_bound.py --clones ~/.cache/pr-verification
```

Run it once with the limits to be measured, and once with both set to 1000000000, which is how
a run went before there was a bound.

#### Results, 2026-10-12

Both runs were of the 20 development cases, once each, with MiniMax M3 on its hosted endpoint
and `PR_VERIFICATION_DECISION=small_llm`. Every run gave a report. The price was set to 0.30
and 1.20 dollars for a million tokens, as price trackers list it, not as MiniMax bills it.

| | Before: nothing cut | After: 20000 and 8000 |
| --- | --- | --- |
| Largest request, in characters | 105,469 | 48,767 |
| Requests to the model | 257 | 256 |
| Seconds for a pull request | 145 | 144 |
| Dollars for a pull request | 0.017 | 0.015 |
| Claims | 30 (19 true, 11 false) | 29 (17 true, 12 false) |
| Decided | 23 | 21 |
| False Blocks | 0 | 0 |
| False Assurances | 2 | 2 |
| Inconclusive | 5 | 6 |
| Decided Rate | 0.77 | 0.72 |

- **The largest request is under half what it was.** Before, one case,
  `rename/packaging-gil-disabled`, made a request twice the size of any other. After, the
  largest three requests are between 43,000 and 49,000 characters. That is under a quarter of
  what the limits allow, and it fits the 32768 tokens the local model is given
  (`ollama/Modelfile`), which the largest request before does not at three characters to a
  token.
- **Time and money did not change.** A pull request took as many requests either way, and
  most of them are small.
- **The Decided Rate differs by two Claims, which one repetition cannot tell from chance.**
  The system splits each pull request into Claims in its own words, so the two runs did not
  even score the same Claims, and `docs/results/structural-cases.md` records that a Verdict here
  varies from run to run. No Claim was Refuted that should not have been, in either run.
- **Not measured:** more than one repetition, any other value of the limits, the held-out
  part, and the cases made from SWE-bench Verified. Nothing shows that a run in the Sandbox
  printed more than 8,000 characters, so the limit on a run's output may never have cut.

#### The defaults

`PR_VERIFICATION_TOOL_RESULT_LIMIT` stays at 20000. At that value the largest request seen
fits the smaller of the two models the project is run with, and nothing measured shows it
costing a Verdict. A lower limit was not measured, so there is no ground to choose one.

`PR_VERIFICATION_SANDBOX_OUTPUT_LIMIT` stays at 8000 as a starting value: these cases give no
number to choose it from.

The largest request the limits allow, 220,000 characters and more, is still several times the
local model's window. What keeps a request inside it today is that roles finish in fewer steps
than their cap and read less than the limit at each, which the limits do not enforce.
