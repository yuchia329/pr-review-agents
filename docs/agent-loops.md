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

### Results, 2026-10-11: no rate yet

The rate is **not measured**. The guard was never asked, because neither model on hand could
do the Reproduction writer's work for four steps. Both were run through Ollama 0.33.3 on the
machine of `docs/decision-layer.md`, each with its context size fixed at 32768, and with the
default decider: Laya, with the same model behind it as the small LLM.

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

What is still to do:

- The measurement itself, with a model that can do the writer's work: a hosted one, or a
  stronger local one. Nothing but the environment needs to change to run it.
- More development cases. The hand-made part has two, each a repository of one file, where a
  capable writer may also finish in under four steps. The cases made from SWE-bench Verified
  are the ones likely to keep a role working long enough for the guard to be asked.
- Until then the interval of four steps is a starting value, as its constant says, and nothing
  is known about how often the guard is wrong.
