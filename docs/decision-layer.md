# The decision layer

A bounded decision is a typed question asked of a state, answered with a probability for each
option. The Claim classifier is the first to use one: it asks of each Claim whether it is
Behavioural or Structural, and the kind is printed with the Claim in the report. No decision is a
Verdict.

Three implementations sit behind the one interface, chosen by configuration.

| Variable | Meaning | When unset |
| --- | --- | --- |
| `PR_VERIFICATION_DECISION` | What is asked first: `laya`, `jev`, or `small_llm` | `laya` |
| `PR_VERIFICATION_DECISION_THRESHOLD` | The top probability under which a decision is passed to the small LLM, from 0 to 1 | 0.75 |
| `PR_VERIFICATION_JEV_API_KEY` | Key for Jev, needed only when Jev is asked | No key |

A variable set to an empty string counts as unset. Anything else the decision layer cannot use
stops the command with exit code 2 and one line naming the variable. The small LLM is the model
of `docs/model-endpoints.md`.

Under `laya` and `jev`, a decision is passed to the small LLM in three cases, and each decision
records which path it took (`made_by`, `passed_on`, `first_asked`):

- `low_confidence`: the top probability was under the threshold;
- `input_too_long`: the input was beyond what the implementation takes;
- `unavailable`: the implementation could not be used at all.

Any other failure of a decision is a `DecisionFailed`, which is handled as a failed model is:
no report, and exit code 2.

The threshold of 0.75 is a starting value. It has not been fitted yet, and the measurements
below say it must be before Laya's own answers are used.

## Laya

Laya is an open-weights decision model from Convai Innovations. It scores each option in one
forward pass and generates no text. It runs in this process through the `laya` package, which
brings PyTorch with it, so it is an extra:

```sh
uv sync --extra laya
```

Without the extra, Laya is `unavailable` and every decision goes to the small LLM. So does
every decision when Laya is installed and cannot be loaded, and the report does not say why:
the only trace is `passed_on` on the decision. The first decision downloads the English checkpoint, `convaiinnovations/laya` (about 810 MB), from
Hugging Face into its cache; later runs load it from there. The state of a decision never leaves
the machine.

Three things about how Laya is used were decided from what its own documentation warns of, and
checked by running it:

- **Always on the processor.** Laya is loaded with `device="cpu"` even where a GPU is present,
  so its probabilities, and a threshold fitted to them, do not move with the hardware.
- **A yes-or-no question is asked as a choice between `yes` and `no`.** Laya's own yes-or-no
  primitive can follow its labels instead of the state on this checkpoint: asked whether an
  agent was getting closer to its goal, it answered 0.0 with a confidence of 1.0, which would
  never be passed on.
- **Nothing is cut short.** Left alone, Laya cuts a long state, a long question, and long
  options to fit, and reports only the first. Each case is counted before Laya is asked.

### Input size limit

Confirmed on 2026-10-10 with `laya` 0.4.2: the English checkpoint takes **512 tokens** for the
state and the question together (`max_len` in the checkpoint's own configuration, which
`LayaDecider.input_limit` is read from when Laya is loaded), and reads at most **48 tokens of any
one option**.

The input is laid out as `[CLS] choice question: <question> [SEP] [MASK] <option> ... [SEP]
<state> [SEP]`. `LayaDecider` counts that layout with Laya's own tokenizer before asking, and
raises `InputTooLong` when it is over 512, when an option is over 48, or when the options
together leave fewer than 16 tokens of the 512 for the question, which is where Laya starts
to shorten every option; the decision is then passed to the small LLM. For five inputs of 27
to 409 tokens the count was equal to the `input_tokens` Laya reported. A state that brought the input to exactly 512 tokens was answered
with nothing dropped, and one token more was refused. With the Claim classifier's question, 480
tokens are left for the Claim.

Laya's separate budget of 192 tokens for the question and its options is lifted to the whole
limit on each call, so a long question is not cut either. After each answer the count made
here is compared with the `input_tokens` Laya reports. Should they ever differ, as after a
change to the `laya` package, the decision fails rather than use an answer to an input that
may have been cut.

### Load time and time per decision

Measured on 2026-10-10 on an Apple M4 Pro (14 cores, macOS 26.6) with Laya held to the
processor, `laya` 0.4.2 and PyTorch 2.14.1. The machine has a GPU, which was not used. A hosted
pipeline runner has far fewer cores, so expect these to be slower there; that run is still to
do.

| What | Seconds |
| --- | --- |
| First decision, checkpoint not yet downloaded (download, load, one decision) | 26 |
| First decision, checkpoint in the cache, over five fresh processes | 3.8 to 14.6 |
| Each later decision, mean over 30 Claims | 0.056 |

Laya is loaded by the first decision and kept, so a pull request with no Claims never waits for
it. Loading costs as much as about 70 to 260 later decisions, and it is paid on every run of the
command: for a pull request with a handful of Claims, the time Laya takes is nearly all loading.

## Jev

Jev is TypeSafe's hosted decision model. `JevDecider` sends one request for each decision to
`POST https://api.typesafe.ai/v1/systemone`, with the model pinned to `jev-1.13.0` rather than
an alias, so that the answers behind a fitted threshold do not change unseen. A yes-or-no
question is Jev's `noul` and a choice its `choice`. The key is sent to Jev and nowhere else, and
is removed from any error Jev sends back.

Jev takes 32k of its own tokens for the state and the question, and publishes no tokenizer. A
token is seldom shorter than a byte, so `JevDecider` refuses an input over 30,000 bytes of UTF-8
with `InputTooLong` and sends nothing. That refuses some inputs Jev would take.

Tried against Jev itself on 2026-10-11, with states of exactly 30,000 bytes sent straight to
it: plain words came to 5,276 of Jev's tokens, CJK text to 10,333, and random letters and
digits to 21,117, each well within 32k. A state of about 40,000 words was refused with HTTP 400
and `max_tokens_exceeded`, so Jev does not cut an input short either. Had such a state come
through `JevDecider`, that refusal would be a `DecisionFailed` and not an `InputTooLong`; no
state under the byte limit has been found that reaches it.

The client was written from TypeSafe's API reference and first checked on 2026-10-10 against
the response shapes that reference documents, replayed locally, and against `laya-serve`, which
serves the same request and response shape. It first reached Jev itself on 2026-10-11, in the
comparison below: 30 choices asked, 30 answered. The same day a yes-or-no question was answered
0.15 for a state that was stuck and 0.95 for one that was moving, and a wrong key was refused
with HTTP 401, reported in one line that did not hold the key.

## The three-way comparison

`tools/compare_deciders.py` runs the same decisions through each implementation with
`compare_deciders` and prints Decision Accuracy, seconds, and dollars for each:

```sh
uv run --extra laya python tools/compare_deciders.py
```

Laya is always compared; Jev when `PR_VERIFICATION_JEV_API_KEY` is set; the small LLM when a
model is configured.

### Results, 2026-10-10 and 2026-10-11

Thirty Claims written by hand for this comparison, fifteen of each kind, each asked the Claim
classifier's question. Two runs of the same cases on the same machine: Laya with the small LLM
on 2026-10-10, and Laya with Jev on 2026-10-11. Laya gave the same 17 of 30 both times. The
small LLM was `qwen3-vl:8b` through Ollama on the same machine, the model on hand, and not the
model the project means to use for light work. Jev's dollars are its published price for a
decision of this size, given to the tool as an argument, and not a bill.

| Implementation | Decision Accuracy | Seconds per decision | Dollars per decision |
| --- | --- | --- | --- |
| Laya (English checkpoint, processor only) | 17 of 30 (0.57) | 0.056 | 0 |
| Small LLM (`qwen3-vl:8b`, local) | 27 of 30 (0.90) | 22.9 | 0 |
| Jev (`jev-1.13.0`, over the network) | 29 of 30 (0.97) | 0.101 | 0.00001 |

No decision failed. What the numbers say:

- **Laya as shipped does not classify Claims.** 17 of 30 is close to the 15 that guessing would
  get, and it called 24 of the 30 Structural. Its own documentation says the same of the base
  checkpoints: a fast base to fine-tune, not a decision model to use untrained.
- **Laya is not confident either, which is what makes the default safe.** Its top probability
  ran from 0.52 to 0.78 with a median of 0.63, and reached 0.75 for 1 Claim of 30. At the
  default threshold, then, 29 of 30 decisions are passed to the small LLM: all but one of the
  answers are the small LLM's, and Laya adds its loading time for almost nothing.
- **Jev classifies Claims as it is.** 29 of 30, against 27 for the local small LLM, at a tenth
  of a second and a thousandth of a cent for each decision, and with nothing to load. On these
  cases it is the only implementation that is both right and fast.
- **Laya's speed does not make up for it.** Laya is about twice as fast as Jev for each
  decision, 0.056 seconds against 0.101, but it pays 4 to 15 seconds of loading on every run,
  which Jev does not: over these 30 decisions Laya took about 7 seconds with loading and Jev 3.
  Laya would be worth having only once made right, by fine-tuning it on Claims and fitting its
  temperature as its documentation describes. Neither is done here; fine-tuning the decision
  model is out of scope for this spec.
- **What Laya keeps is that nothing leaves the machine.** A Claim is Untrusted Content from the
  pull request, and under Jev it is sent to TypeSafe.

What is still to do:

- Jev's confidence on these cases, to fit a threshold for it: the tool prints accuracy and not
  the probabilities. Its price is $0.042 for a million input tokens, about $0.00001 for a
  decision of this size, which is what was passed as `--jev-dollars-per-decision`.
- Whether the default should stay Laya. The spec names Laya the default, and these numbers
  argue for Jev or the small LLM until Laya is fine-tuned. That is the project owner's choice.
- A hosted small LLM, with its price, in place of the local one, whose 22.9 seconds is the
  speed of this machine and not of the model.
- More cases, and cases taken from real pull requests, before a threshold is fitted. With
  thirty, one Claim moves the accuracy by 0.03.
- Until then, `PR_VERIFICATION_DECISION=small_llm` answers every decision with the small LLM
  and loads nothing. On these cases that differs from the default in 1 decision of 30, the one
  Laya kept for itself.
- The times on a machine with no GPU at all, such as a hosted pipeline runner.
