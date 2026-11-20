# The EKS stack and the Kubernetes Sandbox

Run on 2026-11-19 in `us-east-1` from branch `infra/eks`, which starts at `5eaca92`. The stack is `infra/`. The Sandbox is `src/pr_verification/kubernetes_sandbox.py`. The worker ran on the Mac, reached Temporal on the cluster through `kubectl port-forward`, and created each Sandbox Job through `kubectl`. The model was `Qwen/Qwen3.8-27B-FP8` served by vLLM on sicong, over Tailscale, with no witness proxy. The decider was `small_llm`. The Budget's time limit was 7,200 seconds. The records are in `~/.cache/pr-verification/measurements/eks/2026-11-19/`: the smoke output, both contract runs, the report, the five workflow histories, and the worker's output.

## What was checked

| Check | Result |
| --- | --- |
| The stack: `terraform apply`, a hello-world Job on each node, `terraform destroy` | All three worked. 46 resources, and none left after destroy |
| Network policy, by hand before the Sandbox ran: deny-all, strict mode, and a positive control | `timeout`, `timeout`, `connected`, as expected |
| The Sandbox's shared contract suite on EKS | 33 of 33, after the two fixes below. 32 of 33 before them |
| One real run with this Sandbox | `eks-real-048-2` reached a report |

The stack was up for about three hours, from the first apply to the end of destroy.

## Choices made to keep the experiment cheap

There is no NAT gateway. Both nodes and the cluster use the two public subnets, and each node has a public address and reaches the internet through the internet gateway. This was chosen to save the NAT's cost for an experiment. Postgres has no public address, and its security group admits only the cluster. Strict network policy and IMDSv2 with hop limit 1 are unchanged.

Tailscale on the cluster was not used. The worker ran on the Mac, which reaches the model itself, so `tailscale_auth_key` stayed empty.

## The real run

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

## What was fixed on the way

| Problem | Change |
| --- | --- |
| The Helm provider read the Mac's own Helm repository list, whose cached index was missing | `infra/versions.tf` gives the provider its own repository list and cache under `infra/.terraform/helm/` |
| The Temporal chart does not create the `default` namespace, which the worker uses | `server.config.namespaces.create` in `infra/platform.tf` |
| RDS for PostgreSQL 16 refuses connections without TLS, so the schema job could not connect | Both Temporal stores use TLS. The server certificate is not checked, since the RDS CA bundle is not mounted |
| The helper that fills a volume ran as root with every capability dropped, so `tar` could not give the files to user 1000 | The helper runs as user 1000, as the runs do |
| A program that does not exist gives `StartError` with a start time of 1970, which read as the time limit | `StartError` maps to exit status 127 or 126 with the runtime's message, as `ContainerSandbox` gives it |
| The worker always used `ContainerSandbox` | `PR_VERIFICATION_SANDBOX=kubernetes` makes `pr-verification-durable worker` use `KubernetesSandbox` |

The first apply also failed once on a DNS lookup of `iam.amazonaws.com` from the Mac. A second apply continued from the state. No change was needed.

## Known limits

**The worker ran on the Mac.** Every model request, git command, and `kubectl` call came from there, so a dropped port-forward or a sleeping Mac pauses a run. A Sandbox or model step that fails while the worker is still connected is an answer, recorded and not retried. Running the worker in the cluster is not yet done.

**No volume clean-up.** A CronJob that removes volumes older than 6 hours was not built. A named Sandbox's volumes are removed by the run's `close_sandbox` activity, so a run that never ends leaves its volumes on the sandbox node's data disk.

**The database certificate is not checked.** The connection to Postgres is encrypted, but the client does not verify the server's certificate.
