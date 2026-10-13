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
  line, so a search of every file gives Refuted for a true Claim. The key names the one file,
  and since the rule below, only the line that was added.

- **A text the pull request itself adds elsewhere.** `removal/attrs-validators-provides` removes
  a function and adds a changelog entry that names it. A search for the name the Claim gives
  finds it after the pull request, which again gives Refuted for a true Claim. The key searches
  for the definition.

The last three are the limit that `compute_verdict` states for Structural Claims, found in real
commits.

## A text removed from one file is searched for in the others

Decided on 2026-10-12, after the first results below (#67). A rename or a removal is a Claim
about every file, and the verifier chooses the path of each text. So for a text said to be
removed from one file, code also searches every other file of the code after the pull request,
whatever path the verifier gave, and records the lines in `after_lines_elsewhere`. A line found
there stops Confirmed: the Claim is Inconclusive, with the reason `KEPT_ELSEWHERE`. It is never
Refuted for it, because of the Flask case above, where the old line has good reason to stay
elsewhere and Refuted would be a False Block. The rule is stated in `compute_verdict`.

Two other courses were turned down. Telling the verifier to give a rename no path rests on a
model doing as told, and the Verdict is to rest on code (ADR 0002). Leaving the rule alone
keeps a False Assurance that changes from run to run.

The price is Decided Rate: a true Claim whose removed text another file keeps is Inconclusive
where it was Confirmed. Two answer keys showed it, and were rewritten so that each still names
what a careful reader would search for under the rule:

| Case | Key before | Key now |
| --- | --- | --- |
| `configuration/flask-flit-core` | the old line removed from `pyproject.toml`, the new one added | the new line added to `pyproject.toml` |
| `documentation/httpx-readme-prompt` | `$ pip install` removed from `README.md`, which `docs/advanced/text-encodings.md` keeps | the one install line with its comment, which only `README.md` held |

With these the keys bear out all 32 cases as before: 21 true cases Confirmed, 10 false cases
Refuted, and the one false case the rule cannot Refute Inconclusive.

What the rule does not reach: a verifier that names only the text a rename adds, with a path,
is still Confirmed by a search of one file. The Structural cases have not been run again with
a model since the rule, so its effect on the three False Assurances and on the Decided Rate is
not yet measured.

## Running the cases

```sh
uv run --extra laya python tools/structural_cases.py --clones ~/.cache/pr-verification
```

The model and the decider are the ones the environment configures, as `docs/model-endpoints.md`
and `docs/decision-layer.md` describe. Each case is run through the core's entry point with the
decider, so its Claims are classified and a Structural Claim is verified by reading. The
command prints what the harness scored for each part and the Verdicts of every run.
`--part development` runs one part, `--case` runs the cases whose name holds a text and can be
given more than once, and `--repetitions` runs each case more than once. `--reports` names a
directory that the report of every run is written into, so that the Evidence behind a Verdict
can be read afterwards.

## First results, 2026-10-12

The harness ran all 32 cases once with MiniMax M3 on its hosted endpoint, with
`PR_VERIFICATION_DECISION=small_llm`, so the same model classified the Claims. Every run gave a
report. The system splits a pull request's text into Claims in its own words, so there are more
Claims than cases.

| | Development | Held-out |
| --- | --- | --- |
| Runs | 18 | 14 |
| Runs with no Verdict | 0 | 0 |
| Claims | 30 (18 true, 12 false) | 24 (16 true, 8 false) |
| Decided | 21 | 17 |
| False Blocks | 0 | 0 |
| False Assurances | 2 | 1 |
| Inconclusive | 7 | 6 |
| Decided Rate | 0.70 | 0.71 |
| False Block rate | 0 of 18 | 0 of 16 |
| False Assurance rate | 2 of 12, 0.17 | 1 of 8, 0.13 |
| Seconds for a pull request | 101 | 48 |

No price was set for the model, so the cost of a pull request is not known. The whole run took
45 minutes. One repetition says nothing of the variation between runs.

- **No False Block.** None of the 34 true Claims was Refuted, the three cases above where a
  careless search would do so among them: the verifier named a text narrow enough each time.
- **All three False Assurances are renames that skip a file**: `rename/attrs-post-init-hook`,
  `rename/packaging-gil-disabled`, and `rename/httpx-url-pattern`, each `@left-out`. The three
  were run once more to read their Evidence. In two the Claim was Confirmed again, and the
  reason was the same: the verifier gave each text the path of a file where the rename was
  made, so the code was never searched in the file that still holds the old name. For
  `Py_NOGIL` it searched `tests/test_tags.py` alone, while `src/packaging/tags.py` kept the old
  name. A rename is a Claim about every file, and a search of one file cannot bear it out. In
  the third, the same Claim was searched for in every file and Refuted, so the Verdict of
  these cases varies from run to run.
- **7 of the 20 false Claims were Refuted**, and 10 were Inconclusive. Among the Inconclusive
  are the false case the rule cannot Refute, and Claims that a value was set or a text added
  that was not, where again there is no line to show.
- **A Claim can be classified as Behavioural** and go to a Reproduction in the Sandbox. In a
  trial of `rename/packaging-gil-disabled` before the run, the Claim that `tags.py` now reads
  the variable under its new name did. So these cases measure the classifier as well as the
  Structural verifier. The harness does not record the kind of each Claim, so how often is
  not known.
- **A removed file was Inconclusive.** `removal/httpx-chinese-readme` names a file and no text
  of it, and the verifier found nothing to search for.

Before this run the two models on the machine were tried through Ollama 0.40.3, and neither
could do the work. With `llava:7b`, 22 of the 32 runs ended in the Claim extractor, whose
answer was not a list of Claims, and the 13 Claims of the other runs were all Inconclusive.
`qwen3-vl:8b` was stopped by hand after 25 minutes on one case with no report.

What is still to do:

- Repeat the run, to see how much the Verdicts vary, and set the model's prices so that the
  cost of a pull request is recorded.
- Run the cases again now that a text removed from one file is searched for in the others,
  and record the three renames that skip a file and the Decided Rate before and after.
- Run with the default decider, Laya, which needs `uv sync --extra laya`.
