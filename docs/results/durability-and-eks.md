# Durable runs on Temporal, and a run on EKS

Two records. [The crash campaign](#durability-of-a-run-that-loses-its-worker) killed workers in the middle of a step and checked that every run still reached a report. [The EKS stack](#the-eks-stack-and-the-kubernetes-sandbox) was built with Terraform, ran the Sandbox as Kubernetes Jobs and one real run, and was destroyed.

## Durability of a run that loses its worker

Measured on commit `6e49a1f965cf156c6c6f39cce14913d098972821`, branch `measure/durability`. The Mac ran Temporal's dev server, six workers, and the local Docker Sandboxes. The model was `Qwen/Qwen3.8-27B-FP8` served by vLLM on sicong, through the witness proxy. The decider was `small_llm`. The Budget's time limit was 7,200 seconds. The protocol is the docstring of `tools/measure_durability.py`. The score is `~/.cache/pr-verification/measurements/durability/2026-11-03/score-180.json`, and the histories and the witness log are in that directory.

The baseline and the targeted runs, and the first 20 counted crash runs, were taken on 2026-11-03. The crash sample was continued from 2026-11-17 and reached 180 counted runs on 2026-11-19.

### Baseline

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

### Crash

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

### A stop is not a kill

The campaign's kill is SIGKILL. A SIGTERM stop of the campaign sends the workers SIGINT. On 2026-11-18 that cancellation was stored as the in-flight model step's answer, `temporalio.exceptions.CancelledError`. The history codec refuses that type, so the workflow task that has to read the answer fails, and Temporal schedules the same read again. `crash-131` and `crash-138` could not be continued. They were terminated and recorded as not counted. ADR 0011 records the consequence.

### Targeted

32 runs, one for each Effect kind at each of the two points, except that `profile-discard` and `archive-discard` never occurred on these cases (`targeted-017`, `targeted-018`, `targeted-023`, `targeted-024`). Every run reached a report. No completed step was made again. There were no False Assurances. Of the kills aimed at the middle of a step, only the model step landed there. The others landed after the step had finished. Three requests were answered and never recorded, on `targeted-004` (model), `targeted-006` (decision), and `targeted-005` (decision).

## The EKS stack and the Kubernetes Sandbox

Run on 2026-11-19 in `us-east-1` from branch `infra/eks`, which starts at `5eaca92`. The stack is `infra/`. The Sandbox is `src/pr_verification/kubernetes_sandbox.py`. The worker ran on the Mac, reached Temporal on the cluster through `kubectl port-forward`, and created each Sandbox Job through `kubectl`. The model was `Qwen/Qwen3.8-27B-FP8` served by vLLM on sicong, over Tailscale, with no witness proxy. The decider was `small_llm`. The Budget's time limit was 7,200 seconds. The records are in `~/.cache/pr-verification/measurements/eks/2026-11-19/`: the smoke output, both contract runs, the report, the five workflow histories, and the worker's output.

### What was checked

| Check | Result |
| --- | --- |
| The stack: `terraform apply`, a hello-world Job on each node, `terraform destroy` | All three worked. 46 resources, and none left after destroy |
| Network policy, by hand before the Sandbox ran: deny-all, strict mode, and a positive control | `timeout`, `timeout`, `connected`, as expected |
| The Sandbox's shared contract suite on EKS | 33 of 33, after the two fixes below. 32 of 33 before them |
| One real run with this Sandbox | `eks-real-048-2` reached a report |

The stack was up for about three hours, from the first apply to the end of destroy.

### Choices made to keep the experiment cheap

There is no NAT gateway. Both nodes and the cluster use the two public subnets, and each node has a public address and reaches the internet through the internet gateway. This was chosen to save the NAT's cost for an experiment. Postgres has no public address, and its security group admits only the cluster. Strict network policy and IMDSv2 with hop limit 1 are unchanged.

Tailscale on the cluster was not used. The worker ran on the Mac, which reaches the model itself, so `tailscale_auth_key` stayed empty.

### The real run

`eks-real-048-2` verified the pull request of `baseline-048` in the crash campaign, `adversarial/shortened-with-an-ellipsis/printed-output`. Its Claims are false by construction.

| | On EKS | On the Mac, crash campaign baseline |
| --- | --- | --- |
| "Shortened text ends with an ellipsis" | Inconclusive: two Reproductions ran, then the model gave a response with no text while writing the third | Confirmed, the baseline's one False Assurance |
| `shorten("abcdefgh", 5)` returns `"ab..."` | Inconclusive: three Reproductions behaved the same before and after | Inconclusive |
| Budget spent | 3,813 s, 30 steps | 2,691 s, 21 steps |
| Whole run | 85 min, the time with no worker included | 46 min |

Both Claims ending Inconclusive is the safe outcome for a false pull request. One run on each side is not a comparison of the two Sandboxes: the model's answers differ from run to run.

There were 35 Sandbox runs. Each took about 7 seconds from the step's scheduling to its end, Job creation and deletion included. The first Claim took 59 minutes, against about 5 on the Mac, and at least 15 of those minutes had no worker connected.

The port-forward to Temporal dropped once, and the worker could not reach Temporal until it was started again. The run waited, the in-flight model step was given out a second time, and the run went on to its report. The `verify` command lost its connection while waiting for the result. The run did not depend on it, and a client that only waits for the result attached to the run again.

### What was fixed on the way

| Problem | Change |
| --- | --- |
| The Helm provider read the Mac's own Helm repository list, whose cached index was missing | `infra/versions.tf` gives the provider its own repository list and cache under `infra/.terraform/helm/` |
| The Temporal chart does not create the `default` namespace, which the worker uses | `server.config.namespaces.create` in `infra/platform.tf` |
| RDS for PostgreSQL 16 refuses connections without TLS, so the schema job could not connect | Both Temporal stores use TLS. The server certificate is not checked, since the RDS CA bundle is not mounted |
| The helper that fills a volume ran as root with every capability dropped, so `tar` could not give the files to user 1000 | The helper runs as user 1000, as the runs do |
| A program that does not exist gives `StartError` with a start time of 1970, which read as the time limit | `StartError` maps to exit status 127 or 126 with the runtime's message, as `ContainerSandbox` gives it |
| The worker always used `ContainerSandbox` | `PR_VERIFICATION_SANDBOX=kubernetes` makes `pr-verification-durable worker` use `KubernetesSandbox` |

The first apply also failed once on a DNS lookup of `iam.amazonaws.com` from the Mac. A second apply continued from the state. No change was needed.

### Known limits

**The worker ran on the Mac.** Every model request, git command, and `kubectl` call came from there, so a dropped port-forward or a sleeping Mac pauses a run. A Sandbox or model step that fails while the worker is still connected is an answer, recorded and not retried.

**No volume clean-up.** A CronJob that removes volumes older than 6 hours was not built. A named Sandbox's volumes are removed by the run's `close_sandbox` activity, so a run that never ends leaves its volumes on the sandbox node's data disk.

**The database certificate is not checked.** The connection to Postgres is encrypted, but the client does not verify the server's certificate.
