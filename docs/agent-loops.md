# Loops inside one agent

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
`docs/decision-layer.md` describes. Its answer steers a role's effort and is never a Verdict.

## False triggers of the progress guard

A false trigger is the progress guard telling a role that is getting there that it is not.
`tools/measure_progress_guard.py` counts them on the development part of the hand-made cases:

```sh
uv run --extra laya python tools/measure_progress_guard.py --repetitions 5
```

It runs each case through the core's entry point with the configured model and decider, and
prints every asking of the guard with the steps it was about. An asking counts as one of a role
that was getting there when it came in the attempt of the Reproduction writer that went on to
show a difference in behaviour. That is a coarse rule, which is why the steps are printed.

### After #66, 2026-10-13: no false trigger in four askings, none of a role getting there

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

What is still to do:

- Measure on the development cases made from SWE-bench Verified, which keep a role working
  longer: on the hand-made cases a writer that is getting there now ends before the guard is
  asked, so they give no rate of false triggers.
- Decide how a response that begins with the model's reasoning is to be treated, and one that
  holds the answer with no `finish` around it.
- Measure with Laya as the decider, the default. Here the small LLM answered every asking.

### Results, 2026-10-12: one false trigger in three askings

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

### Earlier, 2026-10-11: two local models, the guard never asked

Neither model on hand could do the Reproduction writer's work for four steps. Both were run
through Ollama 0.33.3 on the machine of `docs/decision-layer.md`, each with its context size
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
