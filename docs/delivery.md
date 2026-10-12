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
  pull_request_target:
  push:
    branches: [main]

permissions:
  contents: read
  issues: read
  checks: write
  pull-requests: write

jobs:
  verify:
    # A push, and a pull request from this repository.
    if: >-
      github.event_name == 'push' ||
      (github.event_name == 'pull_request' &&
      github.event.pull_request.head.repo.full_name == github.repository)
    runs-on: ubuntu-latest
    env:
      PR_VERIFICATION_API_KEY: ${{ secrets.PR_VERIFICATION_API_KEY }}
    steps:
      - uses: yuchia329/pr-review-agents@main

  verify-fork:
    # A pull request from a fork, once a maintainer has approved the run.
    if: >-
      github.event_name == 'pull_request_target' &&
      github.event.pull_request.head.repo.full_name != github.repository
    runs-on: ubuntu-latest
    environment: pr-verification-forks
    env:
      # A secret of the environment only: see "Pull requests from forks".
      PR_VERIFICATION_API_KEY: ${{ secrets.PR_VERIFICATION_FORK_API_KEY }}
    steps:
      - uses: yuchia329/pr-review-agents@main
```

Then add the key for the model's endpoint as the repository secret `PR_VERIFICATION_API_KEY`,
and set up the environment for forks as "Pull requests from forks" below says.

What each part is for:

- `pull_request` starts a run that verifies a pull request from the repository itself.
- `pull_request_target` starts a run that verifies a pull request from a fork. See "Pull
  requests from forks" below before changing anything about it.
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

### Pull requests from forks

GitHub gives a run for `pull_request` no secrets when the pull request comes from a fork, so
such a run has no key for the model. A fork is verified by a run for `pull_request_target`
instead, which GitHub starts from the workflow file on the default branch and does give the
secrets. ADR 0005 records why this is safe here, and it rests on four things:

- The workflow file and this Action come from the repository. Nothing in a fork changes them.
- The run checks out the default branch. The Action fetches the pull request's commit by its
  ID and never checks it out, so no file of the fork is written to the runner's working copy.
- The fork's code is run only in the Sandbox, which is given no secret (`docs/running-code.md`).
- A maintainer approves each run before it starts.

The last of these is yours to set up. Under Settings, Environments, make an environment named
`pr-verification-forks`. Add yourself and the other maintainers as its required reviewers, and
add a key as its secret `PR_VERIFICATION_FORK_API_KEY`. Give forks a key of their own, with a
low spending limit. GitHub hands an environment's secret to a job only after a reviewer
approves that run.

Keep that secret in the environment and nowhere else, and never give the `verify-fork` job
the repository's own secret. GitHub makes an environment that a workflow names and that does
not exist, with no reviewers, and lets the job start at once. With the key held only by the
environment you set up, such a run finds no key and verifies nothing. With the key also held
by the repository, it would verify a fork that no one had looked at.

Look at the pull request before approving its run. An approval covers the commit the run was
started for and no later one: a push to the fork starts a new run that waits again. When a
pull request has several runs waiting, approve the newest and reject the others, which are
for commits the fork has since replaced. The report gives the commit a run verified as
`after`.

Do not add a step to the `verify-fork` job that checks out the pull request, such as
`actions/checkout` with `ref` set to the pull request's head, or that runs anything a pull
request can change. Either would run the fork's code beside the key.

Every pull request starts two runs, one for each trigger, and the jobs' `if` leaves one of
them with nothing to do. For a fork that is the run for `pull_request`, and for a pull
request from the repository itself it is the run for `pull_request_target`. The Action keeps
the two kinds apart as well: in a workflow without the `if`, a run started for the other kind
prints a notice on its page and does nothing else.

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
- Required reviewers are offered on public repositories, and on private ones only with GitHub
  Enterprise. An environment without them lets a fork's run start at once, with no one
  having looked at the pull request.
- The container is all that stands between a fork's code and the key
  (`docs/running-code.md`, "Known limits"). Approval limits who gets to try it.
- GitHub takes the workflow file for a fork's run from the default branch, whichever branch
  the pull request targets.

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
- a fork's run waits for a maintainer's approval, is given the key only then, and starts from
  the workflow file and the checkout of the default branch;
- GitHub hands over a fork's commit asked for by its ID, and takes the token the wrapper
  sends with the request;
- GitHub acts on nothing printed between the two lines that stop commands, and, in a
  workflow without the jobs' `if`, shows the notice of a skipped pull request on the run's
  page;
- a real collector takes the trace.

## Manual check of the workflow

Run this by hand after changing `action.yml`, the workflow file above, `GitHubAPI`, or what
`main` prints. It needs a scratch repository on GitHub with the workflow installed and a model
configured, and a second account that can fork it. Make the repository private for steps 1
to 5 and step 8: a public one lets any token read its issues, and so cannot show that the
permissions above are enough. Make it public for steps 6 and 7, with the environment for forks
set up as above: required reviewers need a public repository.

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
6. From the second account, open a pull request from a fork whose description makes one Claim
   that is true. Expect two runs. In the one for `pull_request`, both jobs are skipped, once
   GitHub's own approval for a first contribution, if it asks for one, is given. The one for
   `pull_request_target` waits for review, and until it is approved the pull request
   has no comment and no `PR Verification Gate`. Approve it. Expect the job to end green, the
   log of its checkout to name the default branch, the report in its log to give the pull
   request's commit as `after`, and the pull request to show one comment and a
   `PR Verification Gate` that succeeds.
7. This step shows that no secret reaches the fork's code. In the fork, change the code the
   Claim is about so that it raises an error whose message is `sorted(os.environ)`, and
   change `.github/workflows/pr-verification.yml` so that the `verify-fork` job has a step
   that runs `env`. Push both to the pull request. Expect a new run that waits for review
   again. Approve it. Expect the job's log to show no step that runs `env`: the workflow file
   is the default branch's. Expect the comment to show a Reproduction whose output on the code
   after lists the names the fork's code found in its environment, with no
   `PR_VERIFICATION_API_KEY`, no `GITHUB_TOKEN`, and no name that starts with `ACTIONS_`
   among them. If no Reproduction shows the list, the step has shown nothing: word the Claim
   again until one does. Revoke the key afterwards if the list holds anything unexpected.
   Last, rename the environment in the workflow file on the default branch to one that does
   not exist, and push a commit to the fork. Expect the run to start with no approval, to
   end red with `no model is configured`, and a `PR Verification Gate` that is neutral.
8. Set `OTEL_EXPORTER_OTLP_ENDPOINT` in the workflow to a collector, and run step 2 again.
   Expect one trace in the collector, with one span named `pr-verification` and the others
   under it. Unset it and run again. Expect no trace, and the same check run and comment.

Not run yet. `action.yml` and the workflow file above were written by the implementation
ticket, #44, and the handling of forks by #50. They have been read but never run on GitHub, so
every step above is still to be seen.

What the automated tests do show of a fork, with a stand-in for GitHub and a real Sandbox
(`tests/test_github_action.py`): the wrapper fetches the commit the event names and checks
nothing out, and code in that commit, run as part of verifying it, finds neither the key nor
the token in its environment. `tests/test_sandbox.py` shows the same boundary directly.
