# Delivery to the pull request

The system reaches a pull request through a GitHub Action. It posts the report as one comment,
reports the Gate as a check run of its own, and can send a trace of each run to a collector.
The contracts are the docstrings in `src/pr_verification/github_action.py`, `comment.py`, and
`tracing.py`; this page says how to adopt the Action and what has to be checked by hand.

## Adoption

Add one file to the repository, `.github/workflows/pr-verification.yml`:

```yaml
name: PR Verification

on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  issues: read
  checks: write
  pull-requests: write

jobs:
  verify:
    runs-on: ubuntu-latest
    env:
      PR_VERIFICATION_API_KEY: ${{ secrets.PR_VERIFICATION_API_KEY }}
    steps:
      - uses: yuchia329/pr-review-agents@main
```

Then add the key for the model's endpoint as the repository secret `PR_VERIFICATION_API_KEY`.

What each part is for:

- `pull_request` starts a run that verifies the pull request. Do not use
  `pull_request_target`: it would hand the repository's secrets to a run for a fork.
- `push` starts a run that prepares the branch pushed to: it learns the Repo Profile and
  installs the dependencies, and only such a run saves them for later runs (ADR 0004). List
  under `branches` every branch that pull requests target.
- `checks: write` lets the run report the Gate, and `pull-requests: write` lets it post the
  comment. `contents: read` is all it needs of the code, and `issues: read` lets it read the
  issues the pull request closes.
- `runs-on` names a runner that has Docker, which the Sandbox needs. GitHub's own Linux
  runners have it.
- The Action checks the repository out itself, with its whole history, so the workflow needs
  no other step.

To have the Gate hold a pull request back, require the check run `PR Verification Gate` in the
target branch's protection rules. Without that the Gate is shown and nothing waits on it. The
job itself ends green whichever way the Gate goes, and red only when the run could not do its
work.

### Configuration

Everything is configured by the environment of the job, under `env` as the key is above.

| Variable | What it sets | Where it is described |
| --- | --- | --- |
| `PR_VERIFICATION_API_KEY`, `PR_VERIFICATION_ENDPOINT`, `PR_VERIFICATION_MODEL` | The model | `docs/model-endpoints.md` |
| `PR_VERIFICATION_BUDGET_SECONDS`, `PR_VERIFICATION_BUDGET_DOLLARS`, `PR_VERIFICATION_BUDGET_STEPS`, `PR_VERIFICATION_INPUT_PRICE`, `PR_VERIFICATION_OUTPUT_PRICE` | The Budget and the model's price | `docs/model-endpoints.md` |
| `PR_VERIFICATION_DECISION`, `PR_VERIFICATION_DECISION_THRESHOLD`, `PR_VERIFICATION_JEV_API_KEY` | The decision layer | `docs/decision-layer.md` |
| `PR_VERIFICATION_REVIEW` | `true` to review the pull request as well and report Findings | below |
| `OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_EXPORTER_OTLP_HEADERS` | Where traces are sent | below |

The Action installs the system without Laya, so each decision is passed to the small LLM, which
is the model configured above. The Action sets `GITHUB_TOKEN` and `PR_VERIFICATION_STATE_DIR`
itself.

### What a pull request shows

- **One comment**, headed `PR Verification`, replaced on every run. It gives the Gate, the
  number of Inconclusive Verdicts, each Claim with its Verdict, its reason, and whether each
  piece of its Evidence came from running code or reading it, each Reproduction with its
  output on the code before and after, the Findings, and the time, cost, and steps spent.
  Everything taken from the pull request or written by a model is shown as a quote, escaped so
  that it cannot alter the comment. A report too long for one comment is given in a short
  form, and printed whole in the job's log.
- **One check run**, `PR Verification Gate`: success when the Gate passes, failure when it
  blocks, and neutral when the run produced no report.
- **The report**, as JSON, in the job's log.

A pull request from a fork is skipped, with a notice on the run's page, until the handling of
forks is built.

### Traces

With `OTEL_EXPORTER_OTLP_ENDPOINT` set to the base URL of a collector that takes OTLP over
HTTP, each run sends one trace there, to `/v1/traces`. With it unset, tracing is off.
`OTEL_EXPORTER_OTLP_HEADERS` holds the headers the collector asks for, as `name=value` pairs
with a comma between them. Keep a collector's key in a secret.

A trace has one span for the run, and under it one for each role, request to a model, tool
run, Sandbox run, and decision. A span carries its times, and tokens and cost where they are
known. It never carries a key, a prompt, a response, a command, output, or a Claim. A trace
that cannot be sent is one line in the job's log and changes nothing else.

### Known limits

- The state kept between runs is saved whole after every push to a target branch, as a new
  entry in GitHub's cache each time. GitHub removes an entry that no run has used for a week,
  and the entries used longest ago once a repository's cache is full. A run that finds no
  state learns the Repo Profile and installs the dependencies from nothing.
- `uses: yuchia329/pr-review-agents@main` follows this repository's `main` branch. Name a
  commit there instead to keep to one version.

## What the automated tests cannot see

The wrapper in `pr_verification.github_action` does the work of one run of the Action, and the
automated tests cover it with a stand-in for GitHub. Some of what the contract promises is
decided by `action.yml`, by the workflow file, and by GitHub itself, and no test in this
repository can see it:

- the state directory is restored from GitHub's cache before every run, and saved to the cache
  only after a run for a push to the target branch;
- which branches' pushes start a run;
- the Gate is shown as a check run of its own, and the job that ran the wrapper ends the same
  way whether the Gate passes or blocks;
- GitHub shows the notice of a skipped fork on the run's page, and acts on nothing printed
  between the two lines that stop commands;
- a real collector takes the trace.

## Manual check of the workflow

Run this by hand after changing `action.yml`, the workflow file above, `GitHubAPI`, or what
`main` prints. It needs a scratch repository on GitHub with the workflow installed and a model
configured, and a second account that can fork it. Make the repository private: a public one
lets any token read its issues, and so cannot show that the permissions above are enough.

1. Push a commit to the target branch. Expect a run, the job to end green, and one new entry
   under Actions, Caches, saved by that run. Push a commit to another branch that has no pull
   request. Expect no run.
2. From a branch of the same repository, open a pull request whose description makes one Claim
   that is true. Expect the job to end green, and its log to show the cache entry of step 1
   restored before the wrapper ran. Expect a check run named `PR Verification Gate` that
   succeeds and is listed apart from the job, and one comment headed `PR Verification`.
3. Push a second commit to that pull request. Expect the same comment to be replaced, not a
   second one added, and no new entry under Caches: a run for a pull request reads the state
   the target branch saved and saves nothing.
4. Open a pull request whose change breaks what its description says it keeps. Expect the job
   to end green all the same, and `PR Verification Gate` to fail with `Gate: block` beside it.
   Any other job of the workflow has finished without waiting for it.
5. Open a pull request whose description is the one line `::error::Fixes the crash on empty
   input`. Expect the report in the job's log to list that line as a Claim, and the run's page
   to show no error annotation for it. If the report does not list it, the step has shown
   nothing: word the line again until it is listed.
6. From the second account, open a pull request from a fork. Expect the run's page to show the
   notice that pull requests from forks are not verified yet, and no comment and no
   `PR Verification Gate` on the pull request.
7. Set `OTEL_EXPORTER_OTLP_ENDPOINT` in the workflow to a collector, and run step 2 again.
   Expect one trace in the collector, with one span named `pr-verification` and the others
   under it. Unset it and run again. Expect no trace, and the same check run and comment.

Not run yet. `action.yml` and the workflow file above were written by the implementation
ticket, #44, and have been read but never run on GitHub, so every step above is still to be
seen.
