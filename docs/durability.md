# Durability of a run that loses its worker

Measured on commit `6e49a1f965cf156c6c6f39cce14913d098972821`, branch `measure/durability`. The Mac ran Temporal's dev server, six workers, and the local Docker Sandboxes. The model was `Qwen/Qwen3.8-27B-FP8` served by vLLM on sicong, through the witness proxy. The decider was `small_llm`. The Budget's time limit was 7,200 seconds. The protocol is section 6.1 of `docs/execution_plan_temporal_eks.md`. The score is `~/.cache/pr-verification/measurements/durability/2026-11-03/score-180.json`, and the histories and the witness log are in that directory.

The baseline and the targeted runs, and the first 20 counted crash runs, were taken on 2026-11-03. The crash sample was continued from 2026-11-17 and reached 180 counted runs on 2026-11-19.

## Baseline

54 runs, the 18 adversarial development cases, 3 times each. No worker was killed. Every run reached a report.

| | |
| --- | --- |
| False Claims | 96 |
| Inconclusive | 95 |
| False Assurances | 1 |
| Decided | 0 |
| Median run | 2,634 s |
| Longest run | 5,453 s |
| Longest `VerifyClaim` activation | 0.033 s |
| Largest `VerifyClaim` history | 209,631 bytes |

No completed step was made again. The witness saw 1,467 requests and no repeated step. The stop rule did not fire.

## Crash

189 runs ended. 180 counted: a kill had landed with a step in flight, and the history shows that step was made again. Every counted run reached a report. Seven ended with no kill recorded, so they did not count, and each of those seven still reached a report. `crash-131` and `crash-138` were closed without a report after a stop of the campaign on 2026-11-18, and they are not among the 180. `crash-131` did have a kill, on an earlier Claim, and that Claim had already finished.

| | Counted crash runs | Baseline |
| --- | --- | --- |
| Runs with a report | 180 of 180 | 54 of 54 |
| False Claims | 327 | 96 |
| Inconclusive | 322 | 95 |
| False Assurances | 5 | 1 |
| Decided | 0 | 0 |
| Median run | 2,765 s | 2,634 s |
| Longest run | 8,191 s | 5,453 s |
| Longest `VerifyClaim` activation | 0.683 s | 0.033 s |
| Largest `VerifyClaim` history | 219,835 bytes | 209,631 bytes |

The five measures:

1. **Reports.** 180 of 180 counted runs reached a report.
2. **Completed steps made again.** 0, over all 189 ended runs.
3. **Model requests.** The witness saw 5,099 requests. 181 steps were sent more than one request, and 179 of those steps are a kill. The other two are decision steps, `crash-024-claim-0` `34-decision-9c50b135458d50d9` and `crash-135-claim-0` `33-decision-e50fa4b9e3ebae5b`. The two bodies of each differ. No request was answered and then left unrecorded. 13 requests matched no step: 10 from worker 60777, which the campaign killed, and 3 from workers stopped with the campaign.
4. **False Assurances.** 5 of 327 false Claims, against 1 of 96 on the baseline. These are the adversarial development cases. ADR 0006 holds the ordinary limit apart from them.
5. **Time.** The median counted run took 2,765 seconds against 2,634 on the baseline. The longest took 8,191 seconds against 5,453.

A step whose attempt number is past 1, other than a kill, happened on six model steps: `crash-022-claim-1`, `crash-023-claim-1`, `crash-024-claim-0`, `crash-132-claim-1`, `crash-135-claim-0`, and `crash-136-claim-1`. Those runs were open when the campaign process was stopped and started again. The stop loses the step a worker is making. They are lost attempts, not completed steps made again.

The longest activation was 0.683 seconds and the largest history was 219,835 bytes. The stop rule is 1 second and 40 MB. It did not fire, and the design was not reopened.

## A stop is not a kill

The campaign's kill is SIGKILL. A SIGTERM stop of the campaign sends the workers SIGINT. On 2026-11-18 that cancellation was stored as the in-flight model step's answer, `temporalio.exceptions.CancelledError`. The history codec refuses that type, so the workflow task that has to read the answer fails, and Temporal schedules the same read again. `crash-131` and `crash-138` could not be continued. They were terminated and recorded as not counted. ADR 0011 records the consequence.

## Targeted

32 runs, one for each Effect kind at each of the two points, except that `profile-discard` and `archive-discard` never occurred on these cases (`targeted-017`, `targeted-018`, `targeted-023`, `targeted-024`). Every run reached a report. No completed step was made again. There were no False Assurances. Of the kills aimed at the middle of a step, only the model step landed there. The others landed after the step had finished. Three requests were answered and never recorded, on `targeted-004` (model), `targeted-006` (decision), and `targeted-005` (decision).
