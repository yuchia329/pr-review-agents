# The Budget's time limit is read on the clock, and a Claim's share in seconds of work

The Budget's time limit was the most seconds of work, in which each piece of work counted in
full, though others were done at the same time. That was fair while one piece of work was done
at a time, as the fixed pipeline does. The supervisor works on up to four Claims at once, so its
seconds of work ran ahead of the clock: in experiments A and C (#47,
`docs/model-comparisons.md`) the time limit stopped the supervisor 9 times and the fixed
pipeline never, 6 of them with less than 900 seconds on the clock, the earliest at 584. The
design meant to save time was the one the time limit cut short.

So the pull request's time limit is held against seconds on the clock during which work was
under way, in which work done at the same time counts once. Each Claim's share stays in seconds
of work, in which every piece of work counts in full.

- **The time limit is there to bound how long a pull request takes**, which is the wait a
  reviewer sees. Seconds on the clock are that wait. The money and step limits still count
  every piece of work in full, so working in parallel spends no more money or steps than the
  Budget allows.
- **A share bounds the effort one Claim gets.** Each Claim's share is in the same unit under
  both orchestrations, so a Claim gets the same seconds of work whether its work is done alone
  or beside others, though work done beside others may go slower, sharing Docker and the model
  server. As the code stands, a Claim's own work is done one piece after another, so its
  seconds of work are about its seconds on the clock.
- **Only time with work under way counts**: from just before a model is asked or a Sandbox used
  to just after, and the seconds of a spend the ledger did not time, such as the decisions that
  classify Claims, in full. Time between pieces of work is not counted, as it was not before.
  The fixed pipeline does one piece of work at a time, so what it records does not change.
- **Seconds on the clock depend on how busy the machine is.** A pull request verified while the
  same Docker or model server is shared with other work takes longer, and may be stopped where
  it would not be on a quiet machine. Seconds of work had the same weakness, since a slow
  request counts more seconds too.

The other choice was to keep seconds of work and raise the limit for an orchestration that
works in parallel. It was not taken: how much higher is a number with nothing to set it by, and
the same limit would no longer mean the same wait under the two orchestrations.

ADR 0003 holds: code still enforces every limit, and the supervisor still controls only effort.
This decides how effort is measured, not who decides it.

Decided on 2026-10-17 by the project owner (#47).
