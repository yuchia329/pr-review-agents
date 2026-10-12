# The LLM supervisor

The supervisor is the second orchestration (ADR 0003). A model decides how effort is spent on
the Claims of a pull request: which Claim is worked on, in what order, how many at once, with
how much of the Budget, and whether to try again. It decides nothing else. Each Verdict and the
Gate are computed by code from the Evidence the roles record, whatever the supervising model
says.

It is chosen by configuration, and the fixed pipeline stays the default:

| Variable | Meaning | When unset |
| --- | --- | --- |
| `PR_VERIFICATION_ORCHESTRATION` | `pipeline` or `supervisor` | `pipeline` |

Any other value exits with code 2 and no report. The command line reads the variable. The
GitHub Action does not yet, and uses the fixed pipeline.

What code keeps from outside the model:

- The supervisor is shown structured Claims and summaries of results: counts, and Verdicts
  and reasons that code computed. It is never shown the pull request's title, description, or
  diff, or anything a role read or a Sandbox printed. The full results are kept for the report.
- One request holds what is so now and the summaries of the step before, and no transcript. It
  shows at most 12 Claims of 300 characters each and 4 results, however many Claims there are.
- A step may dispatch up to 4 roles. They run at the same time, each on its own thread, and
  each Reproduction in a Sandbox of its own.
- A share of the Budget can be moved only from a Claim that has been tried or can never be,
  and the ledger enforces the totals.
- A finish is refused while a Claim has had no Verification attempt and the Budget is not
  spent. When the supervisor reaches its step cap of 24, or its model fails, code gives each
  Claim left untried its attempt, one at a time.

`pr_verification.supervisor` states all of this in full.

## Time for one pull request: dispatching together against one at a time

`tools/measure_supervisor.py` verifies one pull request with several Claims twice, with the
supervisor dispatching every Claim in one step and then one Claim in each step:

```sh
uv run python tools/measure_supervisor.py --claims 4 --model-seconds 5 --repetitions 3
```

The pull request is the hand-made case `hand-made/median-of-an-even-count`, and the Sandbox is
the real one. Only the dispatching differs, so every model is a stand-in that always answers
the same: the roles' model writes for each Claim the same Reproduction, which decides it, and
accepts it as the judge. Each Claim takes two requests for a role's work, and `--model-seconds`
is how long each takes, in place of a real model's own time. Each run starts with nothing kept,
so it installs the dependencies in every Sandbox it makes ready.

### Results, 2026-10-12

Four Claims, three runs of each, on an Apple M4 Pro (14 cores) with Docker 28.4.0. Every run
gave four Confirmed Verdicts and reached no limit of the Budget. The times are medians, and the
three runs of each were within 1 second of one another.

| Seconds for each request to a role | Together | One at a time | Together takes |
| --- | --- | --- | --- |
| 0 | 28.5 s | 15.8 s | 1.8 times as long |
| 5 | 38.5 s | 55.6 s | 0.69 times as long |
| 20 | 68.0 s | 175.5 s | 0.39 times as long |

- **Dispatching together is slower when the model answers at once.** Each Reproduction of a
  step needs a Sandbox to itself, and the three further Sandboxes are made ready one after
  another before any dispatch starts. That took about 13 seconds here, for a repository with
  no dependencies of its own. One at a time, a single Sandbox does for all four Claims.
- **It is faster once the model's time is what a run is made of.** At 5 seconds a request the
  four Claims take 10 seconds of waiting on the model and not 40, and at 20 seconds 40 and not
  160. A hosted model's requests take seconds, so that is the case to expect.
- **Together spends more of the Budget's time.** The Budget's seconds are seconds of work, and
  work done at the same time is each counted. With requests of 5 seconds, a run together
  recorded 79 seconds of work and a run one at a time 55. Making the further Sandboxes
  ready is about 13 of those 24 seconds. Where the rest went was not measured.

Not measured:

- **A real model.** The stand-in answers every request in the same time and is right the first
  time. With a real model the Claims differ in the attempts they need, and an endpoint that
  serves one request at a time gives back none of the waiting.
- **A repository with dependencies to install.** Making a further Sandbox ready then costs
  more, though what is installed is put back from the dependency cache when there is one. Each
  run here started with none.
- **What a supervising model chooses.** Both ways of dispatching were scripted. Whether a
  model dispatches together when that is faster is a question for the evaluation.
