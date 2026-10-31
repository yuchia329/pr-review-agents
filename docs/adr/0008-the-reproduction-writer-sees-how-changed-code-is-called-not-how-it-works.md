# The Reproduction writer sees how the changed code is called, not how it works

A Reproduction is meant to come from the Claim. The writer could read every file of the code after the pull request, so it could read what the change does and pick an input that passes on it. In the development part of the adversarial cases on 2026-10-18, 13 of 20 False Assurances came that way: none used the test the pull request planted, the writer found an input of its own that behaved differently on the two commits, and the judge, which sees only the Claim and the Reproduction, accepted a test that looked sound (`docs/adversarial-evaluation.md`). `unique([3, 9, 3, 9])` tests "keeps the order of first appearance" only on an input where sorted order is the same thing. Twice the judge accepted a test that asserted something other than the Claim, as `shorten("abcdefgh", 5) == "ab"` for "returns ab...".

ADR 0002 keeps the judge from the pull request's prose and code, and that stays. This decision applies the same idea to the writer, for the code after the change:

- **The writer reads the code before the pull request in full, and of the code after it only signatures.** One tool, `changed_signatures`, gives the signature of each function and class in the files the pull request changed: how to call the code, and nothing of what it does. Its other tools read the code before. Files the pull request did not change are the same on both commits, so nothing is lost there.
- **The writer does not see what the code after the change printed, when that would show what it does.** After an attempt that behaved the same on both commits, it is shown the run on the code before and not the run on the code after, which holds the values the code returned. A run that could not run is still shown on both, since the writer has to mend it.
- **The judge holds a Reproduction to what the Claim names.** Where a Claim names an input, the Reproduction uses it. Where it names a result, the Reproduction asserts it. An input that holds nothing the Claim is about tests nothing. The judge still sees only the Claim and the Reproduction.

The attempts of a Reproduction written this way either hold on the new code or do not, and neither can be arranged by reading it.

## Considered Options

- **Show the judge the diff.** Rejected: it would then read the prose and code ADR 0002 keeps from it, and it would have to tell a fitted input from a sound one, which it is shown no way to do.
- **A stronger model for the judge alone.** Not rejected, and needs no decision: models are set per role. It does not close this route on its own, because the fitted test reads as a sound one.
- **Show the writer nothing of the code after.** Rejected: a Claim about a function the pull request adds could then never be tested, since the writer would not know how to call it.
- **Keep the outputs of the code after and tell the writer not to fit them.** Rejected: that is an order to a model that has already read the answer. What the writer has not seen cannot shape its test.

## Consequences

- Some true Claims will go Inconclusive that were decided before: a Claim whose test needs more than a signature shows, such as a new command-line option. Under ADR 0006 that costs Decided Rate and no trust. The Decided Rate on true Claims is measured before and after this change, and reported beside the False Assurance rate, so that the system is not made safe by deciding nothing.
- Signatures come from the Ecosystem, since reading them takes a parser of the language. An Ecosystem that cannot give them leaves the writer with the code before alone.
- This is not airtight. A run that could not run is shown on both commits and its traceback can quote the code after, and a default value in a signature is part of what it shows. What is closed is reading the change and fitting the test to it, the route the measurement found.
- The writer reads less, and so should take fewer steps. That is to be shown, not assumed.
