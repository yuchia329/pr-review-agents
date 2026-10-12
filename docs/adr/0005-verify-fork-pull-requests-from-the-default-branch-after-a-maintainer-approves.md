# Verify fork pull requests from the default branch's workflow, after a maintainer approves each run

Verifying a pull request needs the model's API key, and GitHub gives a run started by `pull_request` no secrets when the pull request comes from a fork. The trigger that does give them, `pull_request_target`, is the one behind most leaked secrets, because a workflow that uses it and then checks out and runs the pull request's code runs that code beside the secrets. We use `pull_request_target` for pull requests from forks and keep the fork's code away from the key in four ways:

- **The workflow and the system come from the repository, not the fork.** GitHub starts a run for `pull_request_target` from the workflow file on the default branch, and the Action it names is this system at the version the repository chose. A fork cannot change either.
- **The fork's commit is never checked out.** The run starts from a checkout of the default branch. The wrapper fetches the pull request's commit into it by its ID and reads it only through git, so no file of the fork is written to the runner's working copy and nothing of it is run there.
- **The fork's code runs only in the Sandbox**, which is given no secret and none of the runner's environment (ADR 0001). This is the same boundary that already stands between the key and a pull request from the repository itself.
- **A maintainer approves each run before it starts.** The job that verifies forks belongs to a GitHub environment with required reviewers, and the key is a secret of that environment and of nothing else, so GitHub hands the key over only once a maintainer has approved that run. The run verifies the commit its event names, whatever the fork pushes afterwards, and a later push starts a new run that waits for its own approval.

A pull request from the repository itself is still verified by a run for `pull_request`, with no approval. Its author can already write to the repository.

## Considered Options

- **A maintainer starts verification by a comment or by hand.** The key is exposed to the same degree once the run starts, and nothing runs until a person asks. Rejected: the event of a comment does not name the commit, so the wrapper would have to pin it itself, and the environment's approval gives the same control with the commit pinned by GitHub.
- **No secret for forks.** Stay on `pull_request` and verify forks only with a model that needs no key. Rejected: the token such a run is given cannot post the comment or set the check run, so the Gate would be visible only in a job's log.
- **Two runs.** A run without secrets does the Sandbox's work and a second run with secrets asks the model. The key and the fork's code would never share a machine. Rejected for now: the agent loop asks the model and runs the Sandbox in turn, so this needs the loop taken apart or a remote Sandbox, which ADR 0001 leaves for later.
- **`pull_request_target` with no approval.** Rejected: anyone with a GitHub account could spend the Budget and try the Sandbox's boundary as often as they liked, with no one looking first.

## Consequences

- The Sandbox's boundary is now all that stands between the key and code written by a stranger. A flaw that lets code out of the container (ADR 0001) would reach the key and a token that can write check runs and comments. The approval is what limits who gets to try, so a maintainer who approves is expected to have looked at the pull request first. Give the environment a key of its own with a low spending limit.
- The key for forks has a name of its own and is never also a secret of the repository. GitHub makes an environment that a workflow names and that does not exist, with no reviewers. A job that fell back on the repository's key would then verify a fork that no one had approved; a job that finds no key verifies nothing.
- Approval is only as good as the plan allows. GitHub offers required reviewers on public repositories, and on private ones only with GitHub Enterprise. Where the environment has no required reviewers, a fork's run starts at once and the last of the four protections is absent.
- The wrapper must never check out the pull request's commit, write its files to the runner, or run anything from it outside the Sandbox. A change that does any of these breaks the arrangement even when every test still passes.
- GitHub takes the workflow file from the default branch for every `pull_request_target` run, so a change to it takes effect for forks only once it is on the default branch.
- Every pull request starts two runs, one for each trigger, and one of them does nothing. For a fork the other waits for approval.
