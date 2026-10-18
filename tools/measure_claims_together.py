"""Whether the supervisor, working on several Claims at the same time, verifies a pull request
with four Behavioural Claims faster than the fixed pipeline, which works on one after another.

A measurement, not a test. It makes one pull request that fixes four small functions, each
fix stated in its description as a Claim that a test can decide, runs it through the core's
entry point under both orchestrations with the models of a named configuration, and prints
what each run gave and spent and how long it took on the clock, and then the two side by side:

    uv run python tools/measure_claims_together.py --repetitions 3

`--every-role NAME ENDPOINT MODEL KEY_VARIABLE` measures a model served where no named
configuration says, as `tools/compare_configurations.py` takes it.

`docs/supervisor.md` measured the same question with stand-in models that answer every request
in a fixed time. Here the model is a real one, so the Claims differ in how much work they take,
as they do on a real pull request. The configuration's models need the keys it names, as
`docs/model-comparisons.md` describes, and the decider is configured as `docs/decision-layer.md`
does, with the configuration's default model as its small LLM.

The seconds the comparison shows are the Budget's seconds for a pull request, on the clock
while work was under way (ADR 0007). Each run's seconds on the clock from start to end are
printed beside them, and are what answers the question. Runs made before ADR 0007 showed
seconds of work, in which work done at the same time each counted.

One decider serves both orchestrations, and the fixed pipeline runs first. With Laya as the
decider, it loads on its first use, so its load falls in the pipeline's first run: the runs
recorded in `docs/model-comparisons.md` used `PR_VERIFICATION_DECISION=small_llm`, which loads
nothing.

All four Claims are true, and each fix fails its test before the pull request and passes it
after, so each can be Confirmed under the Refuted rule as it stands.
"""

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

from compare_configurations import clock_summary, every_role, on_the_clock

from pr_verification.budget import budget_from_environment
from pr_verification.comparison import Comparison, label_of
from pr_verification.configuration import configuration_named, configured_models
from pr_verification.context_bound import limits_from_environment, shown_within
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import Evaluation, Part, _HandMade
from pr_verification.orchestration import OrchestrationChoice
from pr_verification.report import Verdict

FOUR_FIXES = _HandMade(
    name="measurement/four-fixes-in-one-pull-request",
    part=Part.DEVELOPMENT,
    expected=Verdict.CONFIRMED,
    title="Fix four small helpers",
    description=(
        "mean([1, 2]) now returns 1.5.\n"
        'last_word("hello world ") now returns "world".\n'
        "percent(5, 0) now returns 0.0.\n"
        'count_vowels("AEIOU") now returns 5.'
    ),
    module="helpers.py",
    before="""\
def mean(values):
    return sum(values) // len(values)


def last_word(text):
    return text.split(" ")[-1]


def percent(part, whole):
    return part * 100 / whole


def count_vowels(text):
    return sum(letter in "aeiou" for letter in text)
""",
    after="""\
def mean(values):
    return sum(values) / len(values)


def last_word(text):
    return text.split()[-1]


def percent(part, whole):
    if whole == 0:
        return 0.0
    return part * 100 / whole


def count_vowels(text):
    return sum(letter in "aeiou" for letter in text.lower())
""",
)
"""The pull request measured: four fixes, each the kind of Claim a Reproduction decides."""


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--configuration", default="default")
    parser.add_argument(
        "--every-role",
        nargs=4,
        metavar=("NAME", "ENDPOINT", "MODEL", "KEY_VARIABLE"),
        help="measures that model in every role, as compare_configurations.py makes it",
    )
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()

    if arguments.every_role is not None:
        configuration = every_role(*arguments.every_role)
    else:
        configuration = configuration_named(arguments.configuration)
    models = configured_models(configuration, os.environ)
    decider = configured_decider(decision_configuration(os.environ), models)
    budget = budget_from_environment(os.environ)
    evaluations: dict[str, Evaluation] = {}
    clock: dict[str, list[float]] = {}
    with (
        tempfile.TemporaryDirectory() as directory,
        shown_within(limits_from_environment(os.environ)),
    ):
        case = FOUR_FIXES.built_in(Path(directory))
        for how in OrchestrationChoice:
            label = label_of(configuration, how)
            print(f"running {label}", file=sys.stderr, flush=True)
            evaluations[label], clock[label] = on_the_clock(
                label,
                [case],
                configuration,
                how,
                repetitions=arguments.repetitions,
                decider=decider,
                budget=budget,
                models=models,
            )
    compared = json.loads(Comparison(evaluations).to_json())
    print(json.dumps({**compared, "clock_seconds": clock_summary(clock)}, indent=2))


if __name__ == "__main__":
    main()
