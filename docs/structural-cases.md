# Structural cases of the evaluation

The evaluation's cases for Structural Claims are made by hand from real commits of eight open
source Python projects. They are data, in `evaluation/structural_cases.toml`, and
`tools/structural_cases.py` makes each into a case for the evaluation harness and runs them.

## What a case is

Each entry of the data names one real commit, by its repository on GitHub and its full ID, and
gives the title and description a pull request for it would have. The text is written by hand
from the commit's own message, so that every Claim in it is Structural and has the one answer.
Where there is a false case the text says nothing of how the code was before, since a true
account of the old code would be a true Claim inside a false case, and speaks only of the files
the false case leaves out.
The code before the pull request is the commit's parent.

An entry gives a **true case**, which expects Confirmed. An entry that lists
`left_out_of_false_case` gives a **false case** as well: the same text over the same commit,
with the commit's change to the listed files left out, so that what the text says was done was
not done. It expects Refuted. A rename that skips the file that defines the name, a removal
that keeps the code it says it removed, and a setting that keeps its old value are all made
this way. Every false case therefore has a true case for the same commit, and the two are in
the same part, so nothing in the held-out part can be learned from the development part.

A case's repository holds the commit's parent, its ancestors, and the one new commit. What
came later in the project's history is not in it, and for a false case neither is the change
that was left out.

| | Development | Held-out |
| --- | --- | --- |
| Renames | 3 true, 2 false | 2 true, 1 false |
| Removals | 3 true, 2 false | 2 true, 1 false |
| Documentation changes | 3 true, 1 false | 3 true, 2 false |
| Configuration values | 3 true, 1 false | 2 true, 1 false |
| All | 12 true, 6 false | 9 true, 5 false |

That is 32 cases from 21 commits. The part of each is written in the data, so the split is the
same on every run.

## The answer key

Each entry also holds an answer key: the texts a careful reader of the Claim would have the
code searched for, each said to be added or removed. The system is never shown it. It is there
so that a case's answer rests on the code and not on the word of whoever wrote the case:

```sh
uv run python tools/structural_cases.py --clones ~/.cache/pr-verification --check-keys
```

This asks no model. It makes every case, searches its two commits for the texts of its key as
the Structural verifier's code would, and computes the Verdict. It fails when the key of a true
case does not give Confirmed, or the key of a false case does. `--clones` names the directory
the eight repositories are cloned into the first time.

Checked on 2026-10-12, the keys bear out all 32 cases:

| | Key gives Confirmed | Key gives Refuted | Key gives Inconclusive |
| --- | --- | --- | --- |
| 21 true cases | 21 | 0 | 0 |
| 11 false cases | 0 | 10 | 1 |

This is the most the rule for Structural Verdicts can give when every text is well chosen. It
is not a result of the system, which has to choose the texts itself.

Four cases were kept because of what the keys showed while they were written:

- **A false case the rule cannot Refute.** `documentation/click-path-completion-faq@left-out`
  says a section was added to a document that was not. Nothing the Claim names is in the code
  on either commit, and Refuted needs a line to show, so the best Verdict it can get is
  Inconclusive. A False Assurance is still possible there and a decided answer is not.
- **A new name that holds the old one.** `rename/packaging-private-format-full-version` renames
  `format_full_version` to `_format_full_version`. A search for the old name alone finds it
  inside the new one after the pull request, which gives Refuted for a true Claim: a False
  Block. The key searches for `def format_full_version(`.
- **A text with good reason to stay.** `configuration/flask-flit-core` changes a requirement in
  `pyproject.toml`. The examples and the documentation of the same repository keep the old
  line, so a search of every file gives Refuted for a true Claim. The key names the one file.

- **A text the pull request itself adds elsewhere.** `removal/attrs-validators-provides` removes
  a function and adds a changelog entry that names it. A search for the name the Claim gives
  finds it after the pull request, which again gives Refuted for a true Claim. The key searches
  for the definition.

The last three are the limit that `compute_verdict` states for Structural Claims, found in real
commits.

## Running the cases

```sh
uv run --extra laya python tools/structural_cases.py --clones ~/.cache/pr-verification
```

The model and the decider are the ones the environment configures, as `docs/model-endpoints.md`
and `docs/decision-layer.md` describe. Each case is run through the core's entry point with the
decider, so its Claims are classified and a Structural Claim is verified by reading. The
command prints what the harness scored for each part and the Verdicts of every run.
`--part development` runs one part, and `--repetitions` runs each case more than once.

## First results, 2026-10-12: nothing decided

The harness ran all 32 cases once. **No Claim was decided**, because neither model on the
machine could do the work. Both were run through Ollama 0.40.3, each with its context size
fixed at 32768, with `PR_VERIFICATION_DECISION=small_llm`, so the same model classified the
Claims.

`llava:7b`:

| | Development | Held-out |
| --- | --- | --- |
| Runs | 18 | 14 |
| Runs with no Verdict | 13 | 9 |
| Claims given a Verdict | 8 (7 true, 1 false) | 5 (4 true, 1 false) |
| Decided | 0 | 0 |
| False Blocks | 0 | 0 |
| False Assurances | 0 | 0 |
| Inconclusive | 8 | 5 |
| Decided Rate | 0 | 0 |
| Seconds for a pull request | 82 | 105 |

- **22 of the 32 runs gave no report.** Each ended in the Claim extractor, whose answer was not
  a list of Claims: the model wrapped its JSON in a code fence, wrote `\_` for an underscore,
  which JSON does not allow, or gave one string where a list belongs.
- **The other 10 runs gave 13 Claims, all Inconclusive.** No Claim was Confirmed or Refuted,
  rightly or wrongly, so nothing is known yet of False Blocks or False Assurance on these
  cases.

The text of four entries was narrowed after this run, when a review found sentences in them
that stayed true in their false cases. The run was not repeated, since it decided nothing.

`qwen3-vl:8b` was tried on one case, `rename/packaging-gil-disabled`, and stopped by hand after
25 minutes with no report. It is the model that timed out in the Claim extractor in
`docs/agent-loops.md`.

What is still to do:

- The measurement itself, with a model that can do the work: a hosted one, or a stronger local
  one. Nothing but the environment needs to change to run it, and the numbers to compare it
  with are those of the answer keys above.
- The default decider, Laya, was not used. It needs `uv sync --extra laya`.
