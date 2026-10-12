# An LLM supervisor controls effort; code controls outcome

The steps needed to verify a pull request could be written as a fixed pipeline, and that would be simpler, faster, and harder to manipulate. We chose an LLM supervisor instead, because the right strategy differs per Claim (a Reproduction for a bug fix, existing tests on both commits for a refactor, a benchmark for a performance change), plans must change when a step fails, and effort has to be divided between Claims. The project also exists to demonstrate orchestration problems that a fixed pipeline designs away.

To keep ADR 0002 intact, the supervisor's power is limited to effort. It chooses which agent runs, in what order, with how much of the Budget, and when to re-plan. It does not see raw Untrusted Content, only structured Claims and structured agent results. Verdicts are computed by code from recorded Evidence, and the Gate is computed by code from Verdicts. Code also prevents the supervisor from finishing until every Claim has had at least one Verification attempt or the Budget is spent, and enforces the step cap and Budget from outside the supervisor.

## Considered Options

- **Fixed pipeline in code.** Rejected for the reasons above, though it remains the safer production design and a useful baseline to measure the supervisor against.
- **Supervisor with full authority, writing Verdicts and the report itself.** Rejected: it makes a manipulable LLM the final judge, contradicting ADR 0002.
- **Agents handing off to each other with no centre.** Rejected: loops can form anywhere and there is no single place to enforce the Budget.

## Consequences

- Runs are slower, cost more tokens, and are not deterministic, so evaluation must tolerate variation between runs.
- A manipulated supervisor can waste a bounded Budget or under-investigate a Claim, which yields Inconclusive. It cannot forge a Verdict.

## Strategies the supervisor may choose

Decided on 2026-10-12, before the supervisor was built. A Reproduction stays a test written by the system, and nothing else decides a Behavioural Claim. So the first reason given above does not hold for the supervisor as built: existing tests on both commits for a refactor, and a benchmark for a performance change, are not strategies, and each kind of Claim has one. Such a Claim goes on to a Reproduction like any other Behavioural Claim and is Inconclusive unless one decides it. The other reasons stand. The supervisor chooses which Claim is worked on, in what order, how many at once, with how much of the Budget, and whether to try again. `compute_verdict` is not changed by this. A strategy can be added later, with its own rule for the Verdict and its own measurements; `strategies_for` in `pr_verification.supervisor` is where the rule is stated.
