# Contain prompt injection rather than rely on detecting it

Everything the LLM reads comes from the pull request author: the title, description, diff, code comments, and even output printed by pull request code. We assume injection will sometimes succeed and limit what a manipulated LLM can cause, instead of depending on a detector to catch every attempt. Concretely: a Verdict on a Behavioural Claim is computed by code from Sandbox results on the two commits, the Gate is computed by code from Verdicts, the LLM holds no secrets and has no tool that writes outside the system, each Reproduction is judged by a separate step that sees only the Claim and the test and none of the pull request prose, and every pull request has a hard budget.

Detection still runs, but only as an extra signal: suspected injection is reported as a Finding.

## Consequences

- The worst outcome of a successful injection is a false Confirmed Verdict, which is no worse than not running the system. It cannot leak the API key, write to the repository, or exceed the budget.
- An LLM may never be given the final say on a Verdict or the Gate, and may never post output directly. Shortcuts that hand it either of these break the containment.
- The separate judgement of each Reproduction costs one extra step per Behavioural Claim.

## The reading-only baseline

Recorded on 2026-10-12, with the contract for model comparisons (#45). The evaluation includes a reading-only baseline in which a role reads the code and states each Verdict itself, with no Reproduction and no Sandbox. It gives an LLM the final say on a Verdict on purpose, so that the difference running code makes is a measured number. This is the one exception to the consequence above, and it holds only inside the evaluation: the baseline is not an orchestration or a configuration, neither the command-line tool nor the GitHub Action can select it, its reasons and its numbers are named as the baseline's wherever they are shown, and its Gate is never reported to a pipeline.
