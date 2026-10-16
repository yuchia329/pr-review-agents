"""Whether the supervisor, working on several Claims at the same time, verifies a pull request
with four Behavioural Claims faster than the fixed pipeline, which works on one after another.

A measurement, not a test. It makes one pull request that fixes four small functions, each
fix stated in its description as a Claim that a test can decide, runs it through the core's
entry point under both orchestrations with the models of a named configuration, and prints
what each run gave and spent, and then the two side by side:

    uv run python tools/measure_claims_together.py --repetitions 3

`docs/supervisor.md` measured the same question with stand-in models that answer every request
in a fixed time. Here the model is a real one, so the Claims differ in how much work they take,
as they do on a real pull request. The configuration's models need the keys it names, as
`docs/model-comparisons.md` describes, and the decider is configured as `docs/decision-layer.md`
does, with the configuration's default model as its small LLM.

All four Claims are true, and each fix fails its test before the pull request and passes it
after, so each can be Confirmed under the Refuted rule as it stands.
"""

import argparse
import os
import sys
import tempfile
from pathlib import Path

from compare_configurations import each_run

from pr_verification.budget import budget_from_environment
from pr_verification.comparison import Comparison, evaluate_configured, label_of
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
    parser.add_argument("--repetitions", type=int, default=3)
    arguments = parser.parse_args()

    configuration = configuration_named(arguments.configuration)
    models = configured_models(configuration, os.environ)
    decider = configured_decider(decision_configuration(os.environ), models)
    budget = budget_from_environment(os.environ)
    evaluations: dict[str, Evaluation] = {}
    with (
        tempfile.TemporaryDirectory() as directory,
        shown_within(limits_from_environment(os.environ)),
    ):
        case = FOUR_FIXES.built_in(Path(directory))
        for how in OrchestrationChoice:
            label = label_of(configuration, how)
            print(f"running {label}", file=sys.stderr, flush=True)
            evaluations[label] = evaluate_configured(
                [case],
                configuration,
                repetitions=arguments.repetitions,
                orchestration=how,
                decider=decider,
                budget=budget,
                models=models,
            )
            each_run(label, evaluations[label])
    print(Comparison(evaluations).to_json())


if __name__ == "__main__":
    main()
