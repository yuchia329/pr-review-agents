# The fixed pipeline is the design, and the supervisor the alternative it is measured against

ADR 0003 chose an LLM supervisor over a fixed pipeline, and kept the pipeline as a baseline to measure it against. That measurement has been made, and this decision supersedes ADR 0003.

The hardest comparison is the development part of the adversarial cases, 2026-10-18 to 19 (`docs/adversarial-evaluation.md`): 54 runs under each orchestration, on one model, decider, and Budget.

- **The supervisor was not safer.** It gave 11 False Assurances of 99 Claims against the pipeline's 9, no difference that three repetitions can show. Of the Claims each took to some end, more of the supervisor's were Confirmed: 11 of 37 against 9 of 97. It is not shown the description, but the Claims it works on come from the same extractor, so that kept nothing out.
- **It took far longer.** The median run took 6,438.5 seconds against 1,878. Of 54 pairs on one case in one repetition, the pipeline's run was the shorter in 50. Within 30 minutes, 2 of 54 runs finished against 26 of 54.
- **It finished less.** The time limit stopped 62 of its Claims against the pipeline's 2, and 16 were worked to an end against 67.

The reasons ADR 0003 gave have gone with this. Each kind of Claim has one way to be verified, as its own amendment of 2026-10-12 records, so there is no strategy to choose. What is left to a model is the order of the work and how much each Claim gets, and on these cases that cost more time than it saved. Showing orchestration problems is a reason for a project, not for a design, and the comparison now does that better than the choice did.

So:

- **The fixed pipeline is the design.** Verifying a pull request is a well-defined task: the steps are known once the Claims are listed. New work is designed and measured on it.
- **Finding where the supervisor beats the fixed pipeline is no longer a goal.** A measurement may still run it, and none has to.
- **The supervisor stays in the code, chosen by configuration,** so that the comparison can be run again. ADR 0003's limits on it stand: it controls effort only, sees no raw Untrusted Content, and Verdicts and the Gate are computed by code.

## The evidence against this decision

- **On easier work the supervisor was quicker at the median, at the same cost.** It was 16% quicker on a pull request with four Claims and 18% on the Structural cases on MiniMax M3, and 21% on 15 of 18 pull requests on Qwen 3.8 once the Budget's time limit was read on the clock. On those Structural cases on Qwen 3.8 both had a Decided Rate of 0.76, and neither made a False Block or a False Assurance (`docs/model-comparisons.md`). In one comparison on Qwen 3.8 it was 17% slower, and on three pull requests it ran past the limit in every run. Those runs are a lead, not a settled result.
- **The adversarial comparison is narrow.** It is six pull requests in three placements, made for the measurement, on one model that also did the supervising, with the time limit at 7,200 seconds and not at the default Budget. A model that answers faster has not been tried on it.
- **The pipeline lost 16 Claims to the model, and the supervisor 1.** Why is not known. The time limit may have stopped those Claims first.
- **Where the supervisor's speed came from: not from its model, on Qwen 3.8.** It works on several Claims at once and a model chooses which, so the gain could have come from either. `measure/fixed-dispatch` (#76), 2026-11-01 and 02, put a fixed rule in the supervising model's place, with the same threads, Sandboxes, and Budget (`docs/model-comparisons.md`). On the pull request with four Claims the model chose what the rule chooses in every run. On the Structural cases its retries and moves of the Budget decided no more Claims than the rule, paired run against run, and it took about 28 seconds a run of its own. Whether working on several Claims at once saves time could not be told: the same arm on the same pull request varied by more than the arms differed. The prediction that the rule would match the supervisor's time failed as written on the four Claims, because the arms were given different numbers of Claims to work on; the project owner accepted that reading. The supervisor's lead on MiniMax M3 was not measured again.

## Considered Options

- **Keep the supervisor as the design and tune it.** Rejected: of the 20 False Assurances, the routes found run through the classifier, the extractor, the writer, and the judge, which both orchestrations share. Tuning the supervisor closes none of them.
- **Remove the supervisor.** Rejected: it is what this decision was measured against, and removing it would end the comparison.
- **Choose the orchestration per pull request, and use the supervisor on easy ones.** Rejected: it needs a way to tell easy from hard before the work is done, and two paths to keep, for about 21 seconds at the median on those 15 pull requests (80.2 against 101.1).

## Consequences

- The fixed pipeline stays the default, and `PR_VERIFICATION_ORCHESTRATION=supervisor` still chooses the supervisor on the command line. The GitHub Action uses the fixed pipeline.
- Claims are worked on one at a time, so any time saved by working on several at once is given up. A pull request with many easy Claims takes longer than it could. On Qwen 3.8 the saving was too small to tell from the noise (#76); a fixed rule would get it, if there is one, without a supervising model.
- Measurements no longer need to run both orchestrations.
- Code and documents that cite ADR 0003 for what limits the supervisor stay correct.
