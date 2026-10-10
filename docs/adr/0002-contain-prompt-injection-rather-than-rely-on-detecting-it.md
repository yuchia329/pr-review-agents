# Contain prompt injection rather than rely on detecting it

Everything the LLM reads comes from the pull request author: the title, description, diff, code comments, and even output printed by pull request code. We assume injection will sometimes succeed and limit what a manipulated LLM can cause, instead of depending on a detector to catch every attempt. Concretely: a Verdict on a Behavioural Claim is computed by code from Sandbox results on the two commits, the Gate is computed by code from Verdicts, the LLM holds no secrets and has no tool that writes outside the system, each Reproduction is judged by a separate step that sees only the Claim and the test and none of the pull request prose, and every pull request has a hard budget.

Detection still runs, but only as an extra signal: suspected injection is reported as a Finding.

## Consequences

- The worst outcome of a successful injection is a false Confirmed Verdict, which is no worse than not running the system. It cannot leak the API key, write to the repository, or exceed the budget.
- An LLM may never be given the final say on a Verdict or the Gate, and may never post output directly. Shortcuts that hand it either of these break the containment.
- The separate judgement of each Reproduction costs one extra step per Behavioural Claim.
