# Prior art: AI code review and generated tests

What other review tools and published systems report for time, cost, tokens, and wrong
answers, how they deal with the problems this project has now, and where this project stands
among them. Read on 2026-10-14.

## How to read this page

- **Every number belongs to the date of its source.** These products change month to month.
  A number from a vendor's own page says what the vendor measured, on its own cases, by its
  own rule.
- **Each source has a grade**, given in the list at the end. A: a paper, or a public benchmark
  that states its data and method. B: a vendor's own documentation or pricing, a vendor
  evaluation that says which cases and how many, or a security researcher's own report.
  C: a vendor claim with no data or method. D: a third-party post, a forum, or a news copy.
- **Each number was found by two readers working apart**: one who wrote notes with a quote for
  every fact, and one who copied every number with the sentence around it. The numbers only one
  of them found were looked up on the page again, by a third reader or by hand, and two that
  could not be found there were left out. The fetch tool returns excerpts, so a quote may be
  the part of a sentence that holds the number.
- **Keys in brackets name the source**, as `[cr-01]`. Numbers measured in this project name the
  document that records them. Sums and ratios worked out here are marked as this page's
  arithmetic.

## What the others count, and what it means here

No tool found counts what this project counts. The nearest measures:

| Their measure | What it counts | Nearest here | Why it is not the same |
| --- | --- | --- | --- |
| Incorrect or unhelpful comments, precision | Comments that are wrong, or that nobody acts on | A False Block, had the comment blocked; otherwise noise among Findings | A comment does not block a merge. A Refuted Verdict does |
| Catch rate, recall, bugs found | Known bugs a review comments on | The reverse of False Assurance, loosely | A review that is silent about a bug has not said the change is sound. A Confirmed Verdict has |
| Resolution or address rate | Comments the author acts on before merging, often judged by an LLM | None | It counts what developers did, not whether the comment was right |
| Fail-to-pass rate of a generated test | Cases where a written test fails before a fix and passes after it | The Decided Rate on true Behavioural Claims, loosely | It scores a test, not a Verdict on a stated Claim |
| Precision of patches that generated tests accept | The share of accepted patches that are right | The share of Confirmed Verdicts that are right | False Assurance is counted over false Claims, not over Confirmed ones |
| False positive rate of a model judging code by reading | The share of buggy code it accepts | False Assurance, closely | The code is a function from a benchmark, not a pull request |

## The numbers

### Hosted review tools

| Tool | Time for a review | Price, or cost of a review | Published wrong-answer and quality numbers | Runs code | Blocks a merge |
| --- | --- | --- | --- | --- | --- |
| CodeRabbit | Mean 5:27 and 6:33 on sets of 13 and 44 pull requests with Sonnet 5.5, against 9:55 and 13:31 with Sonnet 5 [cr-01]; "10-20 minutes" in 2025 [cr-08] | $24, $48 or $72 a seat a month, billed yearly [cr-22]. Its model calls cost $0.46 to $0.47 a review in its own test, $1.16 with Sonnet 5 [cr-01] | Its production system caught 61.3% of 80 known issues, at 39.3% precision [cr-02]. An outside study: 56.3% of 31,073 comments that got a reply were rejected [cr-05] | Shell commands, `ast-grep` and linters in a sandbox; its checks cannot run the tests [cr-08] [cr-15] | Not by default [cr-16] |
| GitHub Copilot code review | "Usually less than 30 seconds", undated [bv-20]; a stronger reasoning model added 16% to latency [bv-13] | $0.05 to $1 of credits a review at "Lite" effort, $0.25 to $5 at "Balanced", Actions minutes extra [bv-14] | 29% of reviews say nothing, by design [bv-13]; precision and recall given only as changes, not levels [bv-18] | CodeQL and linters [bv-21]; tests not documented | Not said |
| OpenAI Codex code review | Not published | Within plan usage, no price a review [bv-11] | Incorrect comments 4.4%, against 13.7% for GPT-5, graded by engineers, in a copy of OpenAI's chart [bv-10]; authors change code for 52.7% of comments [bv-09] | Repository access and code execution, which OpenAI found made a stronger reviewer [bv-09] | Its rules "don't replace tests, branch protections, or required approvals" [bv-12] |
| Cursor Bugbot | About 90 seconds on average, down from about 5 minutes (June 2026) [bv-05] | $1.00 to $1.50 a run (May 2026) [bv-04], then about 22% less [bv-05]; one user's run on a monorepo: 38.3 million tokens, $29.74 [bv-08] | Resolution rate from 52% to over 70% over 40 experiments [bv-01], 80% at default effort [bv-04]; no false positive rate | Its fixing agents test in virtual machines; reviewing is not said to run tests [bv-03] | Not said |
| Greptile | Median 5:04 down to 2:25 from v4 to v5, in an A/B test over a million pull requests; the post writes the unit as seconds and minutes are meant [gq-04] | $30 a seat a month with 50 credits; a review takes 1, 3 or 10 credits; $1 a further credit [gq-01] | A 2024 audit of its own comments: about 19% good, 2% wrong, 79% nitpicks [gq-03]; addressed comments 52% to 66% from v4 to v5 [gq-04] | Installs dependencies and starts a dev server in a sandbox [gq-06] | Not said |
| Claude Code Review | 20 minutes on average [gq-23] | $15 to $25 a review [gq-23] | Under 1% of findings marked incorrect by Anthropic's engineers [gq-24] | Not said | Never: its check is always neutral [gq-23] |
| Graphite Agent | "Seconds" [bv-22]; 1 hour to 90 seconds in Anthropic's case study of it [bv-23] | $20 or $40 a seat a month [bv-24] | Under 5% negative comments, a marketing figure [bv-22] | Not said | Not said |
| Qodo Merge, PR-Agent | One model call of about 30 seconds a command [gq-09] | $0.012 a credit, 2,500 credits for about 18 reviews [gq-08] | On SWR-Bench, precision 15.39% and recall 24.06% [gq-19]; at Beko 73.8% of comments resolved, while closing a pull request went from 5 h 52 min to 8 h 20 min on average [gq-18] | No | Its docs warn against gating on its labels [gq-11] |

### Published systems that write a test and run it before and after

| System | What it decides | How often it is right | Cost and time | Attempts |
| --- | --- | --- | --- | --- |
| Testora [tg-16] | Whether a pull request changes behaviour its title and description do not mean | Precision 55%, recall 67% on 164 labelled differences (GPT-4o-mini) | $0.003 and 12.3 minutes a pull request | Not said |
| SWT-Bench agents, 2024 [tg-01] | A test that fails before a fix and passes after | 19.2% of instances (SWE-Agent+, Lite) | 3 to 5 minutes an instance | Rounds stop helping at 5 to 10 |
| Otter, Otter++ [tg-04] | The same | 31.4%, and 37.0% with 5 samples (TDD-Bench Verified, GPT-4o) | $0.06 and $0.09 an instance | 5 samples side by side |
| e-Otter++ [tg-05] | The same | 63.0% (TDD-Bench Verified, Claude 3.7 Sonnet) | $1.70 to $2.75 an instance with Claude; 5 to 10 minutes to set up Docker alone | 10 prompts, up to 10 repairs |
| AssertFlip [tg-07] | The same | 43.6% of all instances; 189 of the 326 it answered | $0.18 an instance, up to $1.01 with 10 regenerations | Regenerations one after another |
| Issue2Test [tg-06] | The same | 32.9% (Claude 3.5 Sonnet), 30.4% (GPT-4o-mini) | Median $0.0521 and 7.92K tokens written an issue (GPT-4o-mini) | Repair loop |
| ReProAgent [tg-10] | The same | 58.43% Lite, 70.30% Verified (GPT-5-mini) | $0.14 an instance | 1.29 repairs on average when it succeeds |
| Echo [tg-11] | The same | 66.28% (SWT-Bench Verified) | Mean $1.54, median $0.86 an instance | At most 2 retries, worth 7.16 points |
| Generated tests as a judge of patches [tg-01] [tg-04] [tg-09] | Whether a patch fixes its issue | Of accepted patches, 47.8% right at 20% recall (SWT-Bench), 65% to 92% at 30% to 41% (Otter++), 50.0% (SpecRover) | SpecRover: 309 seconds and $0.65 an issue | |

### This project, in the same units

| Run | Time for a pull request | Model cost | Wrong answers | Recorded in |
| --- | --- | --- | --- | --- |
| Structural cases, MiniMax M3, 2026-10-12 | 101 s development, 48 s held-out | Not priced | False Assurance 2 of 12 and 1 of 8; no False Block | `docs/structural-cases.md` |
| Hand-made and Structural development cases, MiniMax M3, 2026-10-12 | 144 to 145 s | $0.015 to $0.017 | False Assurance 2; no False Block | `docs/context-bound.md` |
| Hand-made Behavioural cases, MiniMax M3, three repetitions | Fixed pipeline 475 s, supervisor 495 s, reading-only baseline 23 s | $0.039, $0.079, $0.006 | False Block rate 0, 0, and 0.33 for the baseline | `docs/model-comparisons.md`, branch `impl/models` |
| Adversarial cases, Qwen3.8 27B on vLLM, the Budget's time limit at 1,800 s, 42 runs | Mean 1,571 to 2,090 s by pull request | None | 4 of 78 false Claims Confirmed | `docs/adversarial-evaluation.md`, branch `measure/adversarial-rate` |
| One adversarial case, side by side | MiniMax M3 977 s, Qwen3.8 1,145 s, both stopped at the Budget's time limit | $0.08, none | | The same |

In the side-by-side case 98% and 99% of the time was waiting for the model, and the longest
single answers were 23,065 and 30,986 tokens. The writer's step that writes the test thinks for
4,800 to 9,500 tokens for a test of three lines. Model cost is at MiniMax M3's listed price of
0.30 and 1.20 dollars for a million tokens.

### Where that leaves the project

- **On time, it is slower than most hosted reviewers.** Most report 30 seconds to 7 minutes.
  Claude Code Review's 20 minutes on average and CodeRabbit's "10-20 minutes" of 2025 are in
  the range of this project's 8 to 35 minutes for a pull request with Behavioural Claims. Of
  the published test writers, the few that report time take 3 to 12 minutes an instance,
  Testora's 12.3 minutes for a whole pull request being the closest kind of work.
- **On cost, its model calls are cheap.** They cost $0.015 to $0.08 a pull request at MiniMax
  M3's prices, against $0.46 to $0.47 for CodeRabbit's model calls in its own test [cr-01].
  What the vendors charge, $0.05 to $25 a review, pays for more than model calls. The research
  systems spend $0.003 to $2.75 an instance on models.
- **On tokens, its long answers are the size of CodeRabbit's.** The writer's test-writing step
  writes 4,800 to 9,500 tokens; CodeRabbit's core review call writes 5.8 thousand on average
  with Sonnet 5.5 and 21.6 thousand with Sonnet 5 [cr-01].
- **No hosted tool found decides a pull request's Claims by running code before and after it,
  or says how often it lets a false one through.** The vendors publish wrong-comment rates and
  resolution rates. CodeRabbit's issue check and custom checks have the same three outcomes as
  a Verdict (Addressed, Not addressed, Unclear; Passed, Failed, Inconclusive), decided by
  reading, with no accuracy given [cr-13] [cr-15]. PR-Agent's ticket check says of its own
  pass label that the code "requires additional manual testing" [gq-12]. The nearest published
  system is Testora, which runs a pull request's code before and after with its description
  as the guide and reports a precision and a recall for that [tg-16]. It decides whether a
  change was intended, not whether each stated Claim holds.
- **Of the tools whose documentation says, none blocks a merge by default.** Claude Code
  Review's check is always neutral [gq-23], CodeRabbit warns unless told otherwise [cr-16],
  and PR-Agent's docs warn against gating on its labels [gq-11]. This project blocks on
  Refuted, which is why its False Block limit is 2%.

## Problem A: the Reproduction writer thinks for too long

What is known here: 98% to 99% of a pull request's time is the model writing; the step that
writes the test thinks for thousands of tokens, mostly working out by hand what the code
returns; and attempts 2 and 3 take 80% of the writer's time while changing no Verdict on 46
false Claims. Rewording one rule halved the time in a trial (`docs/adversarial-evaluation.md`
on its branch).

### What others do

- **They choose the effort for each step, and do not find that more always pays.**
  CodeRabbit sets low, medium and high effort by the size of the pull request [cr-01], and
  found that the top effort of Sonnet 5 "roughly doubled the cost without finding meaningfully
  more bugs" [cr-04]. Cursor's high effort finds 35% more bugs at the same resolution rate
  [bv-04]. Copilot took a stronger reasoning model for 6% more positive feedback at 16% more
  latency [bv-13].
- **Thinking off made no measurable difference to CodeRabbit's review time.** On the same 13
  pull requests, thinking off halved the tokens a core call writes, 2,900 against 5,800, and a
  review took 5:58 against 5:27, a gap CodeRabbit calls within noise [cr-01]. The Qwen3.8 model
  card warns of the same: in agentic tasks "lower reasoning effort does not always reduce
  overall task completion time" [ls-09].
- **The model matters most in what they report.** With Sonnet 5.5 in place of Sonnet 5,
  CodeRabbit's reviews took about half as long and a core call wrote 5.8 thousand tokens in
  place of 21.6 thousand [cr-01]. Bugbot went from about 5 minutes to about 90 seconds, which
  Cursor credits to a model it trained [bv-05].
- **The controls this project does not use yet.** Qwen3.8 thinks by default, at the effort
  `xhigh` unless told otherwise, and thinking can be turned off for each request [ls-09]. vLLM
  caps thinking for each request with `thinking_token_budget` and ends the thinking when it is
  reached; without it there is no limit beyond `max_tokens` [ls-10]. MiniMax M3 thinks by
  default on its OpenAI-compatible endpoint, and `thinking` set to `disabled` skips it; on its
  Anthropic-compatible endpoint thinking is off unless asked for; `reasoning_effort` is ignored
  [ls-11] [ls-12].
- **A cap on the answer alone can leave no answer.** OpenAI counts thinking inside
  `max_output_tokens`, so a capped request can end before any visible text, and it advises
  leaving 25,000 tokens [ls-14]. Google says to lower the thinking level instead of the cap
  [ls-15]. A hard cut of thinking hurts a model not trained for it: one code model scored under
  10% with a budget under 4K tokens [ls-07].
- **Several short tries side by side.** Agentless writes 40 candidate tests and keeps the most
  common one that reproduces the issue [tg-08]; Otter++ takes 5 [tg-04]. In Agentless, and in
  SWT-Bench's runs of LIBRO, more samples stop helping after about five [tg-08] [tg-01].
  Answers without thinking, several at once and picked by a checker, matched thinking at up to
  9 times lower latency, though code was the exception where thinking still won [ls-01].
  Starting several and voting among the first three to finish cut wall time by up to 33%
  against waiting for all [ls-04]. Greptile's v5, which runs agents side by side each on one
  hypothesis, had a median review of 2:25 against 5:04 for v4 [gq-04].
- **Repairs from the run's output pay early.** ReProAgent's successful cases need 1.29 repairs
  on average [tg-10]; Echo's at most two retries add 7.16 points [tg-11]; in SWT-Bench more
  rounds stop helping after 5 to 10 [tg-01]. In all of these papers the issue is real, so a
  test that shows no difference is always a wrong test, and trying again is always right.
  Here a Claim can be false, and then a test that shows nothing is the right answer. The
  published case for retries does not carry over to false Claims.
- **The writer is told where the test goes.** In SWT-Bench, giving the writer the test file to
  change nearly doubled its success, from 8.1% to 15.1%; the fix itself helped less, to 10.5%,
  and a wrong fix helped as much [tg-01].
- **Review is background work for them.** CodeRabbit calls its workflow latency-insensitive
  [cr-09], queues each review, and runs it in a service that allows 3,600 seconds a request
  [cr-08]. This project's client waits 300 seconds for one answer.

### What fits here

1. **Measure the controls first, in the measurement only**, as the reworded rule was: Qwen3.8
   at lower effort and with thinking off for the writer, a `thinking_token_budget` on vLLM, and
   MiniMax M3 with thinking disabled. Each against the reworded rule, on the adversarial
   development cases and, once they can be run, on true Claims. What to read: time, Claims
   worked to an end, and on true Claims whether a Reproduction still shows the difference.
   CodeRabbit's result says time may not fall with effort; only a run here can say.
2. **Retry after a failed run, and decide on evidence whether to retry after
   `shows_nothing`.** After `could_not_run` or `no_reproduction` a retry is what the papers
   do. After `shows_nothing` it cost 80% of the writer's time on 46 false Claims and changed
   no Verdict. Whether it rescues true Claims is the number that decides it, and it is not
   measured: it needs the true Claims of the SWE-bench cases run with the attempt recorded.
3. **Give the writer the files the pull request changes and where its tests live**, with its
   task. It saves the search and read steps every attempt repeats, and SWT-Bench's result says
   it may also make tests better.
4. **Try a few short attempts side by side in place of three in a row**, each run in the
   Sandbox, the first to show a difference going to the judge. A further Sandbox costs about
   13 seconds to make ready (`docs/supervisor.md`), against minutes of thinking.
5. **Treat a capped or empty answer as one failed step**, never as a lost Claim, if a cap is
   set at all.

Each of 2 to 5 changes what the writer is asked or allowed to do, which tests pin, so each is
work of its own under the working rules. Step 1 changes nothing in `src/`.

## Problem B: False Assurance above its limit

What is known here: before the rules of #67, 3 of 20 and 2 of 19 false Structural Claims were
Confirmed in the two full runs (ADR 0006), all of them renames checked by too narrow a text. On
the adversarial cases 4 of 78 false Claims were Confirmed, each one a Behavioural Claim that
the classifier sent to the Structural verifier, where any new text is enough.

### What the numbers say about the limit itself

- **A small set cannot show a small rate.** With no error in N cases, the upper end of a 95%
  confidence interval is about 3 in N [ls-21]. Worked exactly, no False Assurance in 59 false
  Claims is needed before 5% can be claimed, and no False Block in 149 true Claims before 2%
  can (this page's arithmetic). The held-out Structural part gave 8 false Claims and 16 true
  ones in a run. Repeating a run does not add new Claims: the same Claim judged three times is
  not three Claims.
- **Abstaining has a floor.** When the error rate is above the target, any method that makes
  no assumption about the cases must abstain on at least (μ − α) / (M − α) of them, where μ is
  the error rate without abstaining, α the target, and M the largest loss [ls-20]. Read over
  false Claims, with a False Assurance rate of 10.5% to 15% and a limit of 5%, at least 6% to
  11% of them must end Inconclusive, even when the ones to give up are chosen perfectly (this
  page's arithmetic). Inconclusive is the price of the limit, as ADR 0006 says.
- **A threshold fitted on one set moves on another.** Under a change of dataset, a static
  conformal risk control missed its target on 14 of 16 transfers [ls-20]. One method that
  gives its guarantee for each run, not only on average, met its target in 91% of runs, fitted
  on 500 cases [ls-18].

### What others do

- **They trade recall for precision.** "Silence is better than noise", and 29% of Copilot's
  reviews say nothing [bv-13]. OpenAI accepts lower recall on purpose [bv-09], and Codex posts
  only its top two severities [bv-12]. Inconclusive makes the same trade for a Verdict, with
  one difference: a Confirmed is an assertion, so this project limits the miss that asserts,
  a False Assurance, as well as the false alarm, a False Block.
- **They check before they post.** CodeRabbit removes weak comments with generated scripts and
  a verification agent, without saying how many [cr-10] [cr-01]. Claude Code Review checks
  each finding "against actual code behavior" [gq-23]. Bugbot began with eight passes over
  differently ordered diffs, a majority vote, and a validator model [bv-01].
- **A model's opinion of its own output is no check.** Greptile found a model's rating of its
  own comments from 1 to 10 "nearly random"; what worked was a filter learned from the comments
  its users had voted down [gq-03].
- **Judging by reading is wrong both ways.** Models asked whether code meets a requirement,
  without running it, accepted as much as 40.0% of buggy code and rejected as much as 91.9% of
  correct code, depending on the model and the prompt, and longer prompts made it worse
  [ls-22]. Against test results, GPT-4.1-mini as a judge had 23% false positives and 16% false
  negatives [ls-23]. The reading-only baseline wrongly Refuted true Claims here too, on two
  cases.
- **Tests let wrong things through as well.** Of the patches that generated tests accepted,
  8% to 52% were wrong (precision 47.8% to 92%) [tg-01] [tg-04] [tg-09]; that is a share of
  accepted patches, not of wrong ones. Of 213 tests that reproduced an issue in Agentless, only
  94 passed on the real fix [tg-08]. Scored by how many wrong versions of the fix they still
  accept, only the stricter half of fail-to-pass tests helped repair, by 8.5 points [tg-12].
  When one model writes both the fix and the test, both are wrong together in 19.9% of cases,
  1.87 times as often as chance [tg-12]. This page's reading: a run test is stronger Evidence
  than reading, and a weak one is still a way for a False Assurance to come through.
- **Tests are made stricter by trying them on wrong versions.** Meta's ACH writes tests that
  must fail on mutants of the code, and engineers accepted 73% of them [tg-14]. CoHarden
  hardens reproduction tests against wrong patches in five rounds [tg-12].

### What fits here

1. **Guard the Structural path, which gave every adversarial False Assurance.** A Claim may be
   Confirmed by searching the code only when the decision that it is Structural was made with
   high confidence; otherwise it goes to a Reproduction, or ends Inconclusive. This applies
   abstaining to the routing, decided by code, so it keeps ADR 0002. The case for it is this
   project's own four Confirmed Claims; no source found routes Claims at all.
2. **State the size the limits need.** ADR 0006 reads its limits on the held-out part. At the
   sizes above, a run that meets them shows only that nothing has gone wrong yet. Either the
   held-out part grows to about 60 distinct false Claims and 150 true ones, or the harness
   reports the upper bound beside each rate. Cases with known answers can be taken from
   SWT-Bench and TDD-Bench Verified, whose true Behavioural Claims are judged by the projects'
   own tests [tg-01] [tg-03], and from Testora's 164 labelled differences [tg-16].
3. **Try each accepted Reproduction on a wrong version of the change** before it confirms a
   Claim: a test that also passes on a mutant that undoes the Claim's behaviour does not test
   the Claim. No False Assurance here has come through a Reproduction yet, so this is for
   later. It is the defence the papers found against lax tests, which is what a planted test
   is.

## Problem C: the rest

### Deciding fewer Claims than reading does

- The reading-only baseline decided 0.83 of Claims and the system 0.36, on two cases, with the
  baseline Refuting a true Claim in two of its three runs (`docs/model-comparisons.md` on
  `impl/models`).
- The published test writers decide 31% to 70% of their instances with models of 2024 and
  2025 [tg-04] [tg-10]. Abstaining helps them too: AssertFlip answered 326 of 433 instances,
  and 189 of the 326 were right, against 189 of all 433 [tg-07].
- Why this project decides so few is not settled. On the two hand-made cases the false case
  was never decided, for reasons not yet looked into; in the adversarial runs the Budget's
  time limit ended 55 of 78 Claims. How often the writer reproduces a true Claim is not
  measured.

### An LLM supervisor against a fixed pipeline

- Cursor and GitHub both moved their reviewers from a fixed flow to an agent. Bugbot's
  resolution rate rose from 52% to over 70% over 40 experiments, the largest gains coming with
  the agent [bv-01], and Copilot's agent brought an initial 8.1% more positive feedback
  [bv-13]. Both then worked on the agent's cost, 22% and 20% lower [bv-05] [bv-17].
- That is an agent inside a role, as this project's roles already are. No source found
  compares an LLM supervisor over roles with a fixed pipeline. Here it cost twice as much and
  decided the same. Where it can pay is time: dispatching four Claims at once took 0.39 of the
  time when each request took 20 seconds (`docs/supervisor.md`).

### Context larger than the window

- CodeRabbit sends 87.3 to 110.7 thousand input tokens in a core call with Sonnet 5.5, and
  191.5 to 247.5 thousand with Sonnet 5 [cr-01], giving each changed line an equal weight of
  context around it [cr-10]. PR-Agent works inside 32,000 tokens by default, keeps additions
  before deletions and only the names of deleted files, and splits a large pull request into at
  most three calls [gq-13] [gq-10] [gq-11]. Greptile reads a graph built in advance [gq-05],
  and its guide to building a review agent advises a light model to narrow what the strong
  model reads [gq-06].
- Here, 18 of 25 Claims of the SWE-bench cases were lost to the window of 32,768 tokens. A
  longer window on the server is the first thing to try for that measurement.

### Test environments of old repositories

- SWE-bench-Live installs only the package versions released before the commit, through a pip
  proxy [tg-22]. SWE-bench's own images run a 2015 instance on Python 3.5 and pip 10 [tg-23].
- Setting up a repository is hard for everyone: 86% of 420 repositories created in 2024
  (Repo2Run, 29 minutes on average) [tg-17], 6.69% of hard Python repositories (EnvBench)
  [tg-19], 31% of repositories for SWE-rebench's recipes [tg-21]. An independent evaluator
  leaves out 16 of 500 SWE-bench Verified instances, for tests that need the network and
  dependency versions that no longer fit [tg-24].
- CodeRabbit keeps a prepared copy of a repository and its dependencies for up to seven days
  [cr-24], as the Repo Profile and the dependency cache do here.
- Here, 24 of 28 SWE-bench instances did not install on the Python the Sandbox picks, 3.14.
  The Python of the commit's date, and a package index held to that date, are what SWE-bench
  and SWE-bench-Live do.

### Answers that cannot be read

- Ellipsis checks each answer against a schema and asks again: 92% are right the first time,
  99.7% after retries [gq-25]. Here, showing every role its finish whole took the writer's
  unreadable answers from 3 of 38 to none (`docs/agent-loops.md`).

### Evaluation sets

- Others' sets: Greptile 50 bugs [gq-02], Martian 50 pull requests [gq-20], Qodo 100 pull
  requests with 580 injected issues [gq-15], GitHub 219 pull requests [bv-18], Graphite 500
  [bv-23], SWR-Bench 1,000, half of them clean so that false alarms can be counted [gq-19].
  CodeRabbit's model posts use sets of 13 and 44 pull requests [cr-01] and 80 known issues
  [cr-02], and say "Thirteen cases is a small set" [cr-01].
- The same tool scores far apart from one reading of a benchmark to another: Greptile's F1 was
  60.8% on one reading of Martian and 40.6% on another [gq-07] [gq-21].

### When a review runs

| | Drafts | Each push | Description edited | Two at once |
| --- | --- | --- | --- | --- |
| CodeRabbit | Skipped [cr-18] | Reviews the new commits; pauses after 5 [cr-18] | No new review [cr-20] | Not said |
| PR-Agent | Skipped [gq-14] | Only when turned on [gq-14] | Not said | Not said |
| Cursor Bugbot | Off by default [bv-06] | Every update, the changes since its last review [bv-06] | Not said | A push during a review is reviewed [bv-07] |
| Copilot | Can be reviewed [bv-14] | Once, unless turned on [bv-14] | Not said | Not said |
| Claude Code Review | Only when asked [gq-23] | Optional, and each costs again [gq-23] | Not said | Queued [gq-23] |

This project's Claims come from the description, so an edited description matters more here
than to any of them: a new Claim is not verified until the next push. Skipping drafts, running
on `ready_for_review` and on `edited`, and one run at a time for each pull request are the
items for the delivery workflow.

### Attacks on review tools

Every incident found joins text an attacker wrote to a tool that held secrets:

- **CodeRabbit, 2025.** A `.rubocop.yml` in a pull request made a linter that ran outside the
  sandbox execute Ruby, and the server's environment held the GitHub App's private key and the
  model providers' keys. The app had over 80,000 installations [ls-25]; CodeRabbit says it
  fixed it within 12 hours [cr-07].
- **Copilot Chat, 2025.** A prompt hidden in a pull request's description made it read private
  repositories and leak them through image links (CVSS 9.6) [ls-26].
- **AI agents in GitHub Actions, 2025 and 2026.** Issue titles, descriptions and comments pasted
  into an agent's prompt affected at least five Fortune 500 companies [ls-27], reached Cline's
  release tokens through the Actions cache [ls-28], and leaked keys from Anthropic's and
  Google's own review actions [ls-29].

ADR 0001 and ADR 0002 are the fixes these reports point to: no secret in the Sandbox, and no
model with the last word. Three checks follow for this project: that nothing a pull request
brings is run outside the Sandbox, as a linter's settings were at CodeRabbit; that hidden HTML
comments in a description are at least reported, since attacks hid there; and that nothing a
pull request can write reaches the store the Repo Profile is read from.

## What to do, in order

| | Change | Grounds | Where it belongs |
| --- | --- | --- | --- |
| 1 | Measure the writer with lower effort, with thinking off, and with a thinking budget, on Qwen3.8 and MiniMax M3, beside the reworded rule | The controls exist and are unused [ls-09] [ls-10] [ls-11]; CodeRabbit's result says time may not fall [cr-01] | #59's measurement; changes nothing in `src/`; needs a go, since it uses the server |
| 2 | Retry after a failed run, and measure whether retrying after `shows_nothing` rescues true Claims; give the writer the changed files and where its tests live; the reworded rule | The repair loops published, SWT-Bench's result on context, this project's trial | A group of its own (contract, tests, implementation) |
| 3 | Guard the Structural path by the confidence of the decision that a Claim is Structural | All four adversarial False Assurances | A group of its own; the most direct cut in False Assurance |
| 4 | State the number of Claims the ADR 0006 limits need, or report the bound beside each rate | The 3-in-N rule [ls-21] | With the harness's check of the limits |
| 5 | A longer model window for the SWE-bench measurement; the commit's Python and a package index held to its date | 18 of 25 Claims lost to the window; 24 of 28 instances not installed | The measurement first; the Sandbox change as a group of its own |
| 6 | Several short attempts side by side | Otter++, Agentless, NoThinking, Greptile v5 | After 1 and 2 show what one attempt costs |
| 7 | Skip drafts; run on `ready_for_review` and `edited`; one run at a time for each pull request | The table above | The delivery workflow |
| 8 | Report hidden HTML comments; check what runs outside the Sandbox and what can reach the Repo Profile's store | The incidents above | Review Findings and the delivery workflow |

A measured comparison this page could not make: PR-Agent is open source, and its ticket check
judges a pull request against its issue by reading. Run on this project's cases with the same
model, it would give a False Assurance and a False Block rate for a reading tool that is not
this project's own. That is a run, and waits for a go.

## What was looked for and not found

- A 90th percentile review time from any vendor. Medians come only from Greptile's A/B test and
  CodeRabbit's small sets.
- Tokens a review in production, from any vendor.
- An accuracy for any hosted tool's check of a pull request against its issue or description.
- A rate of false changes passed, the nearest thing to False Assurance, from any hosted tool.
- A study of thinking, or of reasoning effort, when writing tests that reproduce a bug.
- Wall-clock time for the newer test-writing systems, which report dollars only.
- How often a generated test fails only because tests run without the network.

## Sources

| Key | Source | Publisher | Date | Grade |
| --- | --- | --- | --- | --- |
| cr-01 | [Claude Sonnet 5.5 for code review: More catches than Sonnet 5, in half the time](https://coderabbit.ai/blog/sonnet-5-5-model-review) | CodeRabbit blog | 2026-09-28 | B |
| cr-02 | [Claude Opus 5.5 for code review: More catches, different misses](https://www.coderabbit.ai/blog/opus-5-5-model-review) | CodeRabbit blog | 2026-09-22 | B |
| cr-04 | [Claude Sonnet 5 review: Coding, code review, and cost](https://www.coderabbit.ai/blog/claude-sonnet-5-review) | CodeRabbit blog | 2026-06-30 | B |
| cr-05 | [Is Agentic Code Review Helpful? Mining Developers' Feedback to CodeRabbit Reviews in the Wild](https://arxiv.org/abs/2607.03316) | arXiv (University of Melbourne and Monash University) | 2026-07-03 | A |
| cr-07 | [Our response to the January 2025 Kudelski Security vulnerability disclosure: action and continuous improvement](https://coderabbit.ai/blog/our-response-to-the-january-2025-kudelski-security-vulnerability-disclosure-action-and-continuous-improvement) | CodeRabbit blog (CEO Harjot Gill) | 2025-08-19 | B |
| cr-08 | [How CodeRabbit built its AI code review agent with Google Cloud Run](https://cloud.google.com/blog/products/ai-machine-learning/how-coderabbit-built-its-ai-code-review-agent-with-google-cloud-run) | Google Cloud Blog (bylines: Google Cloud product director and CodeRabbit CEO) | 2025-04-22 | B |
| cr-09 | [SED1844 transcript: CodeRabbit and RAG for Code Review with Harjot Gill](https://softwareengineeringdaily.com/wp-content/uploads/2025/06/SED1844-CodeRabbit.txt) | Software Engineering Daily (episode sponsored by CodeRabbit) | 2025-06-24 | C |
| cr-10 | [Context engineering for AI code reviews](https://www.coderabbit.ai/blog/context-engineering-ai-code-reviews) | CodeRabbit blog | 2025-07-17 | C |
| cr-13 | [PR validation using linked issues](https://docs.coderabbit.ai/issues/pr-validation) | CodeRabbit docs | unknown | B |
| cr-15 | [Custom checks](https://docs.coderabbit.ai/pr-reviews/custom-checks.md) | CodeRabbit docs | unknown | B |
| cr-16 | [Pre-merge checks](https://docs.coderabbit.ai/pr-reviews/pre-merge-checks.md) | CodeRabbit docs | unknown | B |
| cr-18 | [Automatic review controls](https://docs.coderabbit.ai/configuration/auto-review.md) | CodeRabbit docs | unknown | B |
| cr-20 | [Troubleshoot: Why CodeRabbit reviews might not trigger](https://kb.coderabbit.ai/articles/1442026547-troubleshoot-why-coderabbit-reviews-might-not-trigger) | CodeRabbit knowledge base | unknown | B |
| cr-22 | [CodeRabbit Pricing](https://www.coderabbit.ai/pricing) | CodeRabbit | unknown | B |
| cr-24 | [Caching](https://docs.coderabbit.ai/reference/caching.md) | CodeRabbit docs | unknown | B |
| bv-01 | [Building a better Bugbot](https://cursor.com/blog/building-bugbot) | Cursor (Anysphere) | 2026-01-15 | B |
| bv-03 | [Closing the code review loop with Bugbot Autofix](https://cursor.com/blog/bugbot-autofix) | Cursor (Anysphere) | 2026-02-26 | C |
| bv-04 | [Updates to Bugbot for Teams and Individuals](https://cursor.com/blog/may-2026-bugbot-changes) | Cursor (Anysphere) | 2026-05-11 | B |
| bv-05 | [Bugbot is now over 3x faster, 22% cheaper, and finds 10% more bugs](https://cursor.com/changelog/bugbot-updates-june-2026) | Cursor (Anysphere) | 2026-06-10 | B |
| bv-06 | [Bugbot (Cursor documentation)](https://cursor.com/docs/bugbot) | Cursor (Anysphere) | unknown | B |
| bv-07 | [Bugbot release notes](https://cursor.com/docs/release-notes/bugbot) | Cursor (Anysphere) | 2026-10 | B |
| bv-08 | [Optimizing Bugbot Costs in a Large Monorepo: ~$30 per run and $300 in 2 days. Any best practices?](https://forum.cursor.com/t/optimizing-bugbot-costs-in-a-large-monorepo-30-per-run-and-300-in-2-days-any-best-practices/164241) | Cursor community forum (user post) | 2026-06-28 | D |
| bv-09 | [A Practical Approach to Verifying Code at Scale](https://alignment.openai.com/scaling-code-verification/) | OpenAI Alignment Research Blog | 2025-12-01 | B |
| bv-10 | [GPT-5-Codex: OpenAI's Most Powerful Programming Model That Can Work Continuously for 7 Hours Without "Getting Tired"](https://eu.36kr.com/en/p/3468715162162824) | 36Kr (repost of a Zhidx article) | 2025-09-16 | D |
| bv-11 | [Pricing (Codex)](https://learn.chatgpt.com/docs/pricing) | OpenAI | unknown | B |
| bv-12 | [Review GitHub pull requests with Codex](https://learn.chatgpt.com/docs/third-party/github) | OpenAI | unknown | B |
| bv-13 | [60 million Copilot code reviews and counting](https://github.blog/ai-and-ml/github-copilot/60-million-copilot-code-reviews-and-counting/) | GitHub Blog | 2026-03-05 | B |
| bv-14 | [About GitHub Copilot code review](https://docs.github.com/en/copilot/concepts/agents/code-review) | GitHub Docs | unknown | B |
| bv-17 | [Better tools made Copilot code review worse. Here's how we actually improved it](https://github.blog/ai-and-ml/github-copilot/better-tools-made-copilot-code-review-worse-heres-how-we-actually-improved-it/) | GitHub Blog | 2026-07-10 | B |
| bv-18 | [ReviewBench: An open benchmark for AI code review](https://github.blog/ai-and-ml/reviewbench-an-open-benchmark-for-ai-code-review/) | GitHub Blog | 2026-10-05 | B |
| bv-20 | [Using GitHub Copilot code review](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/request-a-code-review/use-code-review) | GitHub Docs | unknown | B |
| bv-21 | [Linter integration with Copilot code review now in public preview](https://github.blog/changelog/2025-11-20-linter-integration-with-copilot-code-review-now-in-public-preview/) | GitHub Changelog | 2025-11-20 | B |
| bv-22 | [AI Reviews - Graphite](https://graphite.com/features/ai-reviews) | Graphite | unknown | B |
| bv-23 | [Graphite speeds up code review by 40x with Claude](https://claude.com/customers/graphite) | Anthropic | 2024-12 | D |
| bv-24 | [Graphite documentation (full text)](https://graphite.com/docs/llms-full.txt) | Graphite | unknown | B |
| gq-01 | [Greptile pricing](https://www.greptile.com/pricing) | Greptile | unknown | B |
| gq-02 | [AI Code Review Benchmarks: 5 tools, 50 bugs, 5 repos](https://www.greptile.com/benchmarks.md) | Greptile | 2025-07 | B |
| gq-03 | [How to Make LLMs Shut Up](https://www.greptile.com/blog/make-llms-shut-up) | Greptile | 2024-12-18 | C |
| gq-04 | [Greptile v5](https://www.greptile.com/blog/greptile-v5) | Greptile | 2026-08-05 | B |
| gq-05 | [Graph-based Codebase Context](https://www.greptile.com/docs/how-greptile-works/graph-based-codebase-context) | Greptile | unknown | B |
| gq-06 | [How to build a code review agent](https://www.greptile.com/blog/how-to-build-a-code-review-agent) | Greptile | 2026-10-09 | C |
| gq-07 | [Greptile on Martian's Code Review Bench](https://www.greptile.com/content-library/greptile-martian-code-review-benchmark) | Greptile | 2026-07-30 | C |
| gq-08 | [Qodo pricing](https://www.qodo.ai/pricing/) | Qodo | unknown | B |
| gq-09 | [PR-Agent (community-owned repository README)](https://github.com/The-PR-Agent/pr-agent) | The-PR-Agent community (donated by Qodo) | unknown | B |
| gq-10 | [PR-Agent: PR Compression strategy](https://docs.pr-agent.ai/core-abilities/compression_strategy/) | PR-Agent docs | unknown | B |
| gq-11 | [PR-Agent: /review tool](https://docs.pr-agent.ai/tools/review/) | PR-Agent docs | unknown | B |
| gq-12 | [PR-Agent: Ticket Context and Compliance](https://docs.pr-agent.ai/core-abilities/fetching_ticket_context/) | PR-Agent docs | unknown | B |
| gq-13 | [PR-Agent: Changing a model](https://docs.pr-agent.ai/usage-guide/changing_a_model/) | PR-Agent docs | unknown | B |
| gq-14 | [PR-Agent: Automations and usage (triggers)](https://docs.pr-agent.ai/usage-guide/automations_and_usage/) | PR-Agent docs | unknown | B |
| gq-15 | [How we built a real-world benchmark for AI code review](https://qodo.ai/blog/how-we-built-a-real-world-benchmark-for-ai-code-review) | Qodo | 2026-02-04 | B |
| gq-18 | [Automated Code Review In Practice](https://arxiv.org/abs/2412.18531) | arXiv (industrial study at Beko) | 2024-12-24 | A |
| gq-19 | [SWR-Bench: Assessing LLM Performance in Real-World Code Review Comment Generation](https://arxiv.org/html/2509.01494v1) | arXiv (Peking University and Northwestern Polytechnical University authors) | 2025-09 | A |
| gq-20 | [Code Review Bench (Martian) repository README](https://github.com/withmartian/code-review-benchmark) | Martian | unknown | A |
| gq-21 | [AI Code Review Benchmark 2026: Precision, Recall, and F1 Results](https://www.codeant.ai/blogs/ai-code-review-benchmark-results-from-200-000-real-pull-requests) | CodeAnt AI | 2026-03 | C |
| gq-23 | [Claude Code: Code Review](https://code.claude.com/docs/en/code-review) | Anthropic | unknown | B |
| gq-24 | [Code Review for Claude Code](https://claude.com/blog/code-review) | Anthropic | 2026-03-09 | C |
| gq-25 | [Lessons from 15 Months of Building LLM Agents](https://www.ellipsis.dev/blog/lessons-from-building-llm-agents) | Ellipsis | 2025-01-30 | C |
| tg-01 | [SWT-Bench: Testing and Validating Real-World Bug-Fixes with Code Agents](https://arxiv.org/abs/2406.12952) | arXiv (NeurIPS 2024); ETH Zurich | 2024-06-18 | A |
| tg-03 | [TDD-Bench Verified: Can LLMs Generate Tests for Issues Before They Get Resolved?](https://arxiv.org/abs/2412.02883) | arXiv; IBM Research | 2024-12 | A |
| tg-04 | [Otter: Generating Tests from Issues to Validate SWE Patches](https://arxiv.org/abs/2502.05368) | arXiv (ICML 2025); IBM Research | 2025-02-07 | A |
| tg-05 | [Heterogeneous Prompting and Execution Feedback for SWE Issue Test Generation and Selection (e-Otter++; v1 titled Execution-Feedback Driven Test Generation from SWE Issues)](https://arxiv.org/abs/2508.06365) | arXiv (ICSE 2026); IBM Research | 2025-08 | A |
| tg-06 | [Issue2Test: Generating Reproducing Test Cases from Issue Reports](https://arxiv.org/abs/2503.16320) | arXiv | 2025-03-20 | A |
| tg-07 | [AssertFlip: Reproducing Bugs via Inversion of LLM-Generated Passing Tests](https://arxiv.org/abs/2507.17542) | arXiv (ICSE 2026); University of Waterloo | 2025-07-23 | A |
| tg-08 | [Agentless: Demystifying LLM-based Software Engineering Agents](https://arxiv.org/abs/2407.01489) | arXiv (FSE 2025); UIUC | 2024-07 | A |
| tg-09 | [SpecRover: Code Intent Extraction via LLMs](https://arxiv.org/abs/2408.02232) | arXiv (ICSE 2025); National University of Singapore | 2024-08 | A |
| tg-10 | [ReProAgent: Tool-Augmented Multi-Stage Agentic Generation of Bug Reproduction Tests from Issue Reports](https://arxiv.org/abs/2607.09123) | arXiv; Nanjing University | 2026-07-10 | A |
| tg-11 | [Echo: Graph-Enhanced Retrieval and Execution Feedback for Issue Reproduction Test Generation](https://arxiv.org/abs/2603.07326) | arXiv | 2026-03-07 | A |
| tg-12 | [Beyond Fail-to-Pass: Iterative Hardening of Co-Generated Bug Reproduction Tests and Fixes](https://arxiv.org/abs/2607.19843) | arXiv | 2026-07-22 | A |
| tg-14 | [Mutation-Guided LLM-based Test Generation at Meta](https://arxiv.org/abs/2501.12862) | arXiv; Meta | 2025-01-22 | A |
| tg-16 | [Testora: Using Natural Language Intent to Detect Behavioral Regressions (v1 titled Regression Testing with a Natural Language Oracle)](https://arxiv.org/abs/2503.18597) | arXiv (ICSE 2026); University of Stuttgart | 2025-03 | A |
| tg-17 | [Repo2Run: Automated Building Executable Environment for Code Repository at Scale](https://arxiv.org/abs/2502.13681) | arXiv (NeurIPS 2025 spotlight) | 2025-02 | A |
| tg-19 | [EnvBench: A Benchmark for Automated Environment Setup](https://arxiv.org/abs/2503.14443) | arXiv (ICLR 2025 workshop listing) | 2025-03 | A |
| tg-21 | [SWE-rebench: An Automated Pipeline for Task Collection and Decontaminated Evaluation of Software Engineering Agents](https://arxiv.org/abs/2505.20411) | arXiv; Nebius | 2025-05 | A |
| tg-22 | [SWE-bench Goes Live!](https://arxiv.org/abs/2505.23419) | arXiv | 2025-05 | A |
| tg-23 | [How to run SWE-bench Verified in one hour on one machine](https://epoch.ai/blog/swebench-docker) | Epoch AI | 2025-07-10 | B |
| tg-24 | [SWE-bench Verified / Epoch AI](https://epoch.ai/benchmarks/swe-bench-verified) | Epoch AI | 2026-03 | B |
| ls-01 | [Reasoning Models Can Be Effective Without Thinking](https://arxiv.org/abs/2504.09858) | arXiv (Ma, He, Snell, Griggs, Min, Zaharia; UC Berkeley, Allen Institute for AI) | 2025-04-14 | A |
| ls-04 | [Don't Overthink it. Preferring Shorter Thinking Chains for Improved LLM Reasoning](https://arxiv.org/abs/2505.17813) | arXiv (Hassid, Synnaeve, Adi, Schwartz; FAIR at Meta, Hebrew University of Jerusalem) | 2025-05-23 | A |
| ls-07 | [Scalable Chain of Thoughts via Elastic Reasoning](https://arxiv.org/abs/2505.05315) | arXiv (Xu et al.; Salesforce AI Research) | 2025-05-08 | A |
| ls-09 | [Qwen3.8-27B (model card)](https://huggingface.co/Qwen/Qwen3.8-27B) | Qwen team (Hugging Face) | unknown | B |
| ls-10 | [Reasoning Outputs (vLLM documentation)](https://docs.vllm.ai/en/latest/features/reasoning_outputs/) | vLLM project | unknown | B |
| ls-11 | [Chat Completions API (MiniMax API reference, OpenAI-compatible)](https://platform.minimax.io/docs/api-reference/text-chat-openai.md) | MiniMax | unknown | B |
| ls-12 | [Messages API (MiniMax API reference, Anthropic-compatible)](https://platform.minimax.io/docs/api-reference/text-chat-anthropic.md) | MiniMax | unknown | B |
| ls-14 | [Reasoning models (OpenAI API guide)](https://developers.openai.com/api/docs/guides/reasoning) | OpenAI | unknown | B |
| ls-15 | [Gemini thinking](https://ai.google.dev/gemini-api/docs/thinking) | Google | unknown | B |
| ls-18 | [Trust or Escalate: LLM Judges with Provable Guarantees for Human Agreement](https://arxiv.org/abs/2407.18370) | arXiv / ICLR 2025 (Jung, Brahman, Choi; University of Washington, Allen Institute for AI) | 2024-07-25 | A |
| ls-20 | [When Can Conformal Risk Control Certify LLM Outputs? Bounds, Impossibility, and Adaptation for Structured Generation](https://arxiv.org/abs/2606.29054) | arXiv (Kotte, independent researcher; single-author preprint) | 2026-06-27 | A |
| ls-21 | [If Almost Nothing Goes Wrong, Is Almost Everything All Right? Interpreting Small Numerators](https://jamanetwork.com/journals/jama/article-abstract/389714) | JAMA (letter by Thomas B. Newman; the rule itself is from Hanley and Lippman-Hand, JAMA 1983) | 1995-10-04 | A |
| ls-22 | [Are LLMs Reliable Code Reviewers? Systematic Overcorrection in Requirement Conformance Judgement](https://arxiv.org/abs/2603.00539) | arXiv (Jin, Chen; University of Sydney) | 2026-02-28 | A |
| ls-23 | [Improving Code Generation via Small Language Model-as-a-judge](https://arxiv.org/abs/2602.11911) | arXiv / ICSE 2026 (Crupi, Tufano, Bavota; Universita della Svizzera italiana) | 2026-02-12 | A |
| ls-25 | [How we exploited CodeRabbit: from a simple PR to RCE and write access on 1M repositories](https://kudelskisecurity.com/research/how-we-exploited-coderabbit-from-a-simple-pr-to-rce-and-write-access-on-1m-repositories) | Kudelski Security Research (Nils Amiet) | 2025-08-19 | B |
| ls-26 | [CamoLeak: Critical GitHub Copilot Vulnerability Leaks Private Source Code](https://www.legitsecurity.com/blog/camoleak-critical-github-copilot-vulnerability-leaks-private-source-code) | Legit Security (Omer Mayraz) | 2025-10-08 | B |
| ls-27 | [PromptPwnd: Prompt Injection Vulnerabilities in GitHub Actions Using AI Agents](https://www.aikido.dev/blog/promptpwnd-github-actions-ai-agents) | Aikido Security (Rein Daelman) | 2025-12-04 | B |
| ls-28 | [Clinejection - Compromising Cline's Production Releases just by Prompting an Issue Triager](https://adnanthekhan.com/posts/clinejection/) | Adnan Khan (independent researcher) | 2026-02-09 | B |
| ls-29 | [Comment and Control: Prompt Injection to Credential Theft in Claude Code, Gemini CLI, and GitHub Copilot Agent](https://oddguan.com/blog/comment-and-control-prompt-injection-credential-theft-claude-code-gemini-cli-github-copilot/) | Aonan Guan (with Johns Hopkins University researchers Zhengyu Liu and Gavin Zhong) | 2026-04-15 | B |
