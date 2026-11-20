# The measured runs keep the replay design

ADR 0010 kept the fixed pipeline and replays it against a Journal. It left one check to real runs: an activation under 1 second, and a history under 40 MB, with a worker killed mid-run. That check is `docs/durability.md`, on commit `6e49a1f`, and it passed.

- **180 of 180 counted runs reached a report.** A run counted only when a SIGKILL landed on a step that was in flight and the history shows that step was made again.
- **A completed step was not made again.** The count of those, over every ended crash run, is 0.
- **The stop rule did not fire.** The longest `VerifyClaim` activation was 0.683 seconds. The largest `VerifyClaim` history was 219,835 bytes.
- **The durable design stays.** Generators and an async rewrite are not taken up. Stage B is a separate choice, at Gate A, and this result does not start it.

## Consequences

- The time and the False Assurances are recorded beside the baseline in `docs/durability.md`. The median counted run took 2,765 seconds against 2,634 with no kill. False Assurances were 5 of 327 false Claims against 1 of 96. These are the adversarial development cases, whose limit ADR 0006 keeps apart from the ordinary one.
- Six model steps were made again because the campaign process was stopped and started again, not because a kill was recorded for that step. A stop of the workers loses the step in flight. That is the same shape as a kill, and it is not a completed step running twice.
- A graceful stop is not yet a lost step. On 2026-11-18, SIGINT during a model step was caught and stored as the answer, and that answer names `temporalio.exceptions.CancelledError`. The codec refuses the type, so the run cannot read the answer back and cannot continue. Two runs were closed without a report for that reason and are not among the 180. The counted kills were SIGKILL, which does not take this path. A later change has to let a cancellation be lost, so the step is made again, before a clean shutdown of a worker can be trusted mid-step.
- Two decision steps each have two request bodies in the witness, on runs that were open across a stop. No request was answered and then left unrecorded in the crash sample. The targeted sample has three, named in `docs/durability.md`.

## Considered options

- **Reopen the design because the 1 second spike had failed.** Rejected: the failure was a single request of about 12 MB, which the payload limit refuses, and the real runs stayed under 1 second.
- **Count the two runs that a stop made unreadable.** Rejected: one had a kill that had already landed on an earlier Claim, and neither reached a report. Counting them would put the stop into the kill sample.
