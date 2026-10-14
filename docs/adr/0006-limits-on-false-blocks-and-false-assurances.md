# Hold False Blocks and False Assurances under fixed limits, and maximise the Decided Rate beneath them

The system is worth running only if a reviewer can take its answers on trust, so that a Claim
it Confirms does not have to be checked again by hand. Two errors break that trust, in
different ways. A False Block stops sound work, and a team switches off a tool that does it. A
False Assurance tells the reviewer a false Claim holds, and once that has happened a reviewer
re-checks every Confirmed Claim, which is the work the system was meant to save. Inconclusive
breaks neither: it sends the Claim back to the reviewer with its reason, which costs time and
no trust.

So both errors are held under fixed limits, measured by the evaluation harness, and every
change to the system is judged first by whether it keeps them there. Beneath the limits the
system makes as many Claims decided as it can. The limits are:

| Measure | Limit | Of |
| --- | --- | --- |
| False Block rate | at most 2% | true Claims |
| False Assurance rate | at most 5% | false Claims, of the cases that are not adversarial |

- **They are read on the held-out part**, over at least three repetitions of a run, and stated
  as counts beside the rates. The development part is where rules are tuned, and a number met
  only there says nothing. One run is never enough: the Verdicts of one case vary from run to
  run.
- **A limit below one Claim in the set can only be met with none.** With 34 true Claims, a limit
  of 2% allows no False Block at all. The harness reports both the count and the rate so that
  this is plain.
- **The adversarial cases will have a limit of their own**, reported apart (#49, #59), set once
  a model that can do the work has been measured on them. A manipulated pull request is a
  different question, of security, and its rate would hide the ordinary one if the two were
  pooled.
- **The Decided Rate has no target yet.** It is reported beside the limits for every run, and a
  change reports it before and after. A target is set once a full run of every case with the
  current rules gives a baseline.
- **While a rate is above its limit, no change may raise it.** A limit the system does not yet
  meet is to be worked down to, and must not stop all other work in the meantime.
- **The harness marks each limit met or not met** for a run, and a model provider is described
  as supported (#47) when a run with it meets both.

Decided on 2026-10-14. When it was decided, the Structural cases had given no False Block in
any run, 34 and 31 true Claims in the two full runs. They had given 3 False Assurances of 20
false Claims and 2 of 19, before the two rules of #67; since then 0 of 9 on the three renames
that skip a file, and no full run. 2% of the true Claims of the held-out part, about 13 to 16
in a run, allows no False Block over three repetitions, which is the intent while the set is
this small; it is not 0% so that one run on a much larger set does not stop every change. 5%
is about one false Claim in twenty taken on trust, and allows one over three repetitions of
the held-out part.

## Considered Options

- **No limits; report the rates and judge each change by eye.** This is how the project has
  run so far. Rejected: #67 showed a change can buy one error down by raising another, and with
  no line to hold, each trade is argued afresh.
- **A target for the Decided Rate first.** Rejected: it rewards answering, and the cheapest way
  to answer more is to Confirm or Refute on weaker Evidence, which is the trade the limits
  exist to stop.
- **One combined score.** Rejected: it lets a gain in Decided Rate pay for a False Block, and
  the two are not worth the same to a reviewer.
- **A limit on Inconclusive.** Rejected: an Inconclusive with its reason is the safe answer, and
  a limit on it pushes towards guessing. Its cost shows up in the Decided Rate.

## Consequences

- A change that breaks a limit, or raises a rate that is already above its limit, is not
  merged, however much it raises the Decided Rate, until it is shown on the held-out part not
  to.
- The rules that give Refuted stay narrow, and the rules that take a Confirmed away (as in #67)
  are preferred to rules that add a Refuted.
- The held-out part is read only to report. Tuning on it, as was done for the name search of
  #67, has to be said wherever its numbers are given.
- The harness gains a check of each limit, which is a change to its contract and is built as
  the working rules say.
