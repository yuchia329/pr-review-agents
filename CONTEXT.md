# PR Verification

A multi-agent system that checks pull requests in a CI/CD pipeline, both by verifying what the pull request claims and by reviewing the quality of its changes.

## Language

### Claims

**Claim**:
A checkable statement a pull request makes about its own changes, taken from its title, description, or linked issue.
_Avoid_: Assertion, promise, intent

**Behavioural Claim**:
A **Claim** that the code does something different when run, such as a bug fix, a new feature, or a performance change.
_Avoid_: Functional claim, runtime claim

**Structural Claim**:
A **Claim** about the text of the code itself, such as a rename, a removal, a documentation change, or a configuration value.
_Avoid_: Static claim, textual claim

### Verification

**Verification**:
Establishing whether a **Claim** is true by gathering **Evidence**.
_Avoid_: Validation, check, test

**Evidence**:
An observation that supports or contradicts a **Claim**, obtained by reading the code or by running it.
_Avoid_: Proof, reasoning, justification

**Reproduction**:
A test written by the system for one **Behavioural Claim** and run against both the code before the pull request and the code after it. A difference in behaviour between the two runs is **Evidence**.
_Avoid_: Repro script, generated test, probe

**Sandbox**:
The isolated environment in which pull request code and **Reproductions** are run. It never holds secrets.
_Avoid_: Container, runner, executor

**Ecosystem**:
A programming language together with its dependency and test tooling, within which the system is able to run a **Reproduction**.
_Avoid_: Language, stack, runtime

**Verdict**:
The outcome of **Verification** for one **Claim**: Confirmed, Refuted, or Inconclusive. A **Behavioural Claim** can be Confirmed or Refuted only on the strength of a **Reproduction**; a **Structural Claim** can be Confirmed or Refuted by reading the code.
_Avoid_: Result, outcome, status

**Inconclusive**:
The **Verdict** given when the **Evidence** neither supports nor contradicts a **Claim**, including when the system could not finish gathering it.
_Avoid_: Unknown, skipped, failed

**Repo Profile**:
The facts the system remembers about a repository between pull requests, such as how to install its dependencies and run its tests. Each fact has been proven by a successful run in the **Sandbox** and comes only from code already on the target branch.
_Avoid_: Memory, cache, repo config

**Budget**:
The fixed limit on effort the system may spend on one pull request, measured in time, money, and steps, with a share set aside for each **Claim**. A **Claim** still unresolved when the Budget is spent receives an Inconclusive **Verdict**.
_Avoid_: Quota, limit, allowance

### Trust

**Untrusted Content**:
Any text that originates from the pull request, including its title, description, diff, code comments, and anything its code prints. It is always treated as data and never as instruction.
_Avoid_: User input, PR text, external content

### Review

**Review**:
An assessment of the quality of a pull request's changes, independent of any **Claim**.
_Avoid_: Verification, audit, critique

**Finding**:
A single quality concern raised by a **Review**. A Finding is advisory and never affects the **Gate**.
_Avoid_: Issue, comment, violation

**Undeclared Change**:
A change in a pull request that no **Claim** covers. It is reported as a **Finding**, and the system never invents a **Claim** to cover it.
_Avoid_: Hidden change, unrelated change, scope creep

### Gate

**Gate**:
The pass-or-block decision the system reports to the pipeline for a pull request. It blocks only when at least one **Claim** has a Refuted **Verdict**.
_Avoid_: Check, status, approval

### Evaluation

**False Block**:
A true **Claim** given a Refuted **Verdict**, so that a sound pull request is blocked. It is the error the system must keep under a fixed limit.
_Avoid_: False positive, false alarm

**False Assurance**:
A false **Claim** given a Confirmed **Verdict**.
_Avoid_: False negative, miss

**Decision Accuracy**:
The share of bounded decisions, such as which kind a **Claim** is, whose most probable option is the expected one. It measures an implementation of the decision layer and says nothing about **Verdicts**.
_Avoid_: Hit rate, precision

**Decided Rate**:
The share of **Claims** that receive a correct Confirmed or Refuted **Verdict** rather than Inconclusive or a wrong one. The system maximises it subject to the limit on **False Blocks**.
_Avoid_: Accuracy, coverage
