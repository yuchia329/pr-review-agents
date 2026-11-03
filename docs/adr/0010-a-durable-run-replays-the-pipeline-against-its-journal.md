# A durable run replays the pipeline against its Journal

A run of the fixed pipeline can take two hours, and a worker lost in that time loses the run. Temporal keeps a workflow's history and has a lost step made again, but its Python workflows are async, and the pipeline is synchronous: `core`, `orchestration`, `agent`, `reproduction`, and `judge` hold no `async def`.

So a durable run does not rewrite the pipeline. A workflow runs the core's own code again and again against the run's Journal, with nothing behind the seams of `pr_verification.effects` but that Journal. The first Effect the Journal holds no answer for stops the code with `NeedsEffect`, is made as an activity, and joins the Journal, and the code is run once more (`pr_verification.durable`).

- **One pipeline.** The code measured is the code made durable. A rewrite would be a second implementation, or would break most of the tests.
- **`NeedsEffect` derives from `BaseException`,** so no `except Exception` in the pipeline takes it for a failure. Once raised, every Effect asked for until the replay ends raises it again, so a `finally` or `__exit__` cannot put its own Effect in place of the one the run needs.
- **Each Claim's work is a workflow of its own.** A request to the Reproduction writer can hold about 252,000 characters, so one history holding the work on every Claim could near Temporal's 50 MB limit. The run hands its ledger to the Claim's workflow and takes it back (`Ledger.adopt`), so the Budget is held as in a run on one machine. Claims are still worked on one after another.
- **The clock is the seconds Effects took.** Time in which no worker was running, or in which the code was replayed, does not spend the Budget.
- **A Sandbox is named for the run,** and an activity finds it by name, so a lost worker does not lose it. The name is looked up in the container runtime of the worker's own machine, so only a worker on the machine that made the Sandbox finds it: a step taken over on another machine starts from a fresh copy of the commit. The run removes it once it is over, however it ended.
- **Secrets stay with the workers.** A workflow's input holds no key: each worker makes its own models and decider from its environment.
- **Limits end a run, never stall it.** Temporal retries a workflow task that breaks a payload limit without end, so a Claim would hang. A durable run holds each Effect, answer, and output under 1.5 MB, and each Journal under 30 MB, once encoded. A Claim's work past them is stopped, and the Claim is Inconclusive.
- **What crosses is pickled and compressed,** so the project's types cross as they are. Unpickling takes only the project's classes and the standard types they are made of.
- **An activity's ID holds a digest of what it was asked.** Temporal compares activity IDs, and not inputs, when it replays a history, so without it a changed request would be given an old answer.

## Consequences

- Replaying the whole Journal at each step costs time that grows with the square of its length. A spike on 2026-11-01 replayed 100 Effects with 4 kB answers and 250 kB results in at most 42 ms, and the same with the largest answer seen so far on every step in up to 770 ms. Temporal suspects a deadlock at 2 s. Each replay is logged with its length and duration, so that real runs show where it stands.
- The spike's own exit, one activation under 1 s with 100 entries of 250,000 characters, failed: past 1 s from entry 45. That shape needs a single request of about 12 MB, which the payload limit refuses first. The owner accepted the design on that evidence, which was chosen after the failure and is weaker for it, and the limit is checked again on real runs.
- `FixedPipeline` gains `work_on_claim`, the seam through which a Claim's work is done elsewhere. With none given, nothing changes.

## Considered Options

- **Rewrite the pipeline as async.** Rejected: a second implementation of what was measured.
- **Turn the call sites into generators that yield Effects,** about 20 functions in 9 modules, with a synchronous driver keeping today's API. The fallback if replay proves too slow on real runs.
- **One workflow for the whole pull request.** Rejected for the size of its history.
- **Encode payloads as JSON.** Readable in Temporal's UI, but every type the pipeline passes would need a schema of its own.
