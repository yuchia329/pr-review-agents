# Run untrusted code in a local container that never holds secrets

Verifying a Behavioural Claim requires running a Reproduction, which means executing pull request code and system-written tests that were shaped by attacker-controllable pull request text, on the same CI runner that holds the LLM API key. We run all such code inside a local container (the Sandbox) that is given no secrets, while the process that holds the key stays outside and talks to the Sandbox only by asking it to run a command and return the output. Dependency installation runs with network access; test execution runs with none.

## Considered Options

- **Static analysis only, no execution.** Rejected: reading code cannot reliably confirm or refute a Behavioural Claim, so the Gate would block on opinion rather than Evidence.
- **Remote sandbox service (hosted microVM).** Stronger isolation, but adds a second vendor and secret, a per-run cost, and a network round trip on every Reproduction. The Sandbox sits behind a boundary so this can replace the local container later.
- **No isolation beyond the short-lived CI runner.** Rejected: the API key would be readable by any pull request test file.

## Consequences

- A container is a weaker boundary than a virtual machine. A kernel-level escape could reach the key-holding process on the same runner. This is a documented limit, mitigated by the runner being short-lived and the key being narrowly scoped, not a claim of full isolation.
- Isolating code execution does nothing about pull request text manipulating the LLM. Prompt injection is a separate threat handled elsewhere.
