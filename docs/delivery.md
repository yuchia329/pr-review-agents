# Delivery to the pull request

The wrapper in `pr_verification.github_action` does the work of one run of the Action, and the
automated tests cover it with a stand-in for GitHub. Some of what the contract promises is
decided by the workflow file and by GitHub itself, and no test in this repository can see it:

- the state directory is restored from GitHub's cache before every run, and saved to the cache
  only after a run for a push to the target branch;
- which branches' pushes start a run;
- the Gate is shown as a check run of its own, and the job that ran the wrapper ends the same
  way whether the Gate passes or blocks;
- GitHub shows the notice of a skipped fork on the run's page, and acts on nothing printed
  between the two lines that stop commands;
- a real collector takes the trace.

## Manual check of the workflow

Run this by hand after changing the workflow file, `GitHubAPI`, or what `main` prints. It needs a
scratch repository on GitHub with the workflow installed and a model configured, and a second
account that can fork it.

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

Not run yet: the workflow file is written by the implementation ticket, #44.
