"""Durable runs: the fixed pipeline run on Temporal, so that a run survives the loss of a worker.

The pipeline is synchronous and stays as it is (ADR 0010). A workflow runs the core's own code
again and again against the run's Journal, with nothing behind the seams of
`pr_verification.effects` but that Journal. The first Effect with no answer in it stops the
code, is made as an activity, and joins the Journal, and the code is run once more. So each
Effect is made once, wherever the worker that made the one before it went, and only the Effect
under way when a worker is lost is made again.

- `journal`: the Journal, and the run replayed against it. No Temporal.
- `codec`: how what crosses to Temporal is written into its history.
- `activities`: the Effects made, on the machine of a worker.
- `workflows`: `VerifyPullRequest`, and `VerifyClaim` for each Claim's work.
- `worker`: a Temporal worker that runs them.
- `driver`: the `pr-verification-durable` command.

Temporal comes with the `durable` extra. Nothing outside this package imports it.
"""
