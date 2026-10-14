"""Runs of the evaluation under named configurations, and the reading-only baseline, side by
side.

A measurement, not a test. It runs the hand-made cases, which are all Behavioural, through the
core's entry point once for each `--run`, and prints the comparison as JSON
(`pr_verification.comparison`): the False Block rate, the False Assurance rate, the Decided
Rate, and the time and the cost of a pull request for each run.

    uv run --extra laya python tools/compare_configurations.py \\
        --run default/pipeline --run default/supervisor --baseline default --repetitions 3

A `--run` is `<configuration>/<orchestration>`. The configuration is one of the named ones
(`pr_verification.configuration`) or one made here with `--light`. Each needs the keys its
models name, as `docs/model-endpoints.md` describes, and the decider is configured as
`docs/decision-layer.md` does, with the configuration's default model as its small LLM.

`--light NAME ENDPOINT MODEL KEY_VARIABLE INPUT_PRICE OUTPUT_PRICE` makes a configuration
called NAME that has that model in the light roles, which are the Claim extractor and the small
LLM behind the decider, and what the default has in the reasoning roles.

`--every-role NAME ENDPOINT MODEL KEY_VARIABLE` makes a configuration called NAME that has
that model in every role and behind the decider, as `self-hosted` has its own, for a model
served somewhere `self-hosted` does not name. It has no price but the environment's.

`--baseline CONFIGURATION` adds the reading-only baseline (`pr_verification.baseline`), run on
that configuration's models with the step cap of the Reproduction writer.

`--capabilities` runs each `--run` twice more, once with no request asking for its
instructions to be kept and once with no model offering parallel tool calls, so that what each
capability saves is the difference from the run beside it.

Only the development part is run unless `--held-out` is given, so that the held-out part is
not looked at while the system is being tuned. `--clones`, the directory
`tools/structural_cases.py` clones into, adds the Structural cases.
"""

import argparse
import os
import sys
import tempfile
from dataclasses import replace
from functools import partial
from pathlib import Path

from structural_cases import built, clone_of, load

from pr_verification.baseline import reading_only_baseline
from pr_verification.budget import budget_from_environment
from pr_verification.comparison import BASELINE_LABEL, Comparison, evaluate_configured, label_of
from pr_verification.configuration import (
    CONFIGURATIONS,
    DEFAULT,
    Configuration,
    ModelChoice,
    ModelProtocol,
    configured_models,
)
from pr_verification.context_bound import limits_from_environment, shown_within
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import Case, Evaluation, Part, evaluate, hand_made_cases
from pr_verification.model import (
    REASONING_ROLES,
    Model,
    ModelRequest,
    PricedModel,
    PricedResponse,
    RoleModels,
    TokenPrice,
    offers_parallel_tool_calls,
)
from pr_verification.orchestration import OrchestrationChoice
from pr_verification.reproduction import WRITER_STEP_CAP


class NeverKept:
    """A model that passes on what it is asked, but for one thing: no request asks for its
    instructions to be kept. It has the price and the capabilities of the model it asks."""

    def __init__(self, model: PricedModel) -> None:
        self._model = model
        self.parallel_tool_calls = offers_parallel_tool_calls(model)

    def complete(self, request: ModelRequest) -> str:
        return self._model.complete(replace(request, cache_instructions=False))

    def complete_priced(self, request: ModelRequest) -> PricedResponse:
        return self._model.complete_priced(replace(request, cache_instructions=False))


def light(name: str, endpoint: str, model: str, key_variable: str, *price: str) -> Configuration:
    """The configuration `--light` makes."""
    sent, returned = map(float, price)
    choice = ModelChoice(
        ModelProtocol.OPENAI_COMPATIBLE,
        endpoint,
        model,
        key_variable=key_variable,
        price=TokenPrice(input=sent, output=returned),
    )
    return Configuration(name, choice, roles=dict.fromkeys(REASONING_ROLES, DEFAULT.default))


def every_role(name: str, endpoint: str, model: str, key_variable: str) -> Configuration:
    """The configuration `--every-role` makes."""
    choice = ModelChoice(ModelProtocol.OPENAI_COMPATIBLE, endpoint, model, key_variable)
    return Configuration(name, choice)


def without_parallel_tool_calls(configuration: Configuration) -> Configuration:
    """The configuration with no model relied on to ask for several tools at once."""
    return replace(
        configuration,
        default=replace(configuration.default, parallel_tool_calls=False),
        roles={
            role: replace(choice, parallel_tool_calls=False)
            for role, choice in configuration.roles.items()
        },
    )


def without_prompt_caching(models: RoleModels) -> RoleModels:
    """The models, each asked through a `NeverKept` when it has a price to save on."""

    def never_kept(model: Model) -> Model:
        return NeverKept(model) if isinstance(model, PricedModel) else model

    return RoleModels(
        never_kept(models.default),
        {role: never_kept(model) for role, model in models.roles.items()},
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--run", action="append", default=[], metavar="CONFIGURATION/ORCHESTRATION")
    parser.add_argument("--light", action="append", default=[], nargs=6)
    parser.add_argument("--every-role", action="append", default=[], nargs=4)
    parser.add_argument("--baseline", metavar="CONFIGURATION")
    parser.add_argument("--capabilities", action="store_true")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--held-out", action="store_true", help="run the held-out part as well")
    parser.add_argument("--clones", type=Path, help="adds the Structural cases, cloned here")
    arguments = parser.parse_args()

    named = dict(CONFIGURATIONS)
    named.update({given[0]: light(*given) for given in arguments.light})
    named.update({given[0]: every_role(*given) for given in arguments.every_role})
    runs: list[tuple[Configuration, OrchestrationChoice]] = []
    for asked in arguments.run:
        name, _, how = asked.partition("/")
        if name not in named or how not in set(OrchestrationChoice):
            parser.error(f"--run {asked}: not a configuration and an orchestration")
        runs.append((named[name], OrchestrationChoice(how)))
    if arguments.baseline is not None and arguments.baseline not in named:
        parser.error(f"--baseline {arguments.baseline}: not a configuration")
    cases: list[Case] = []
    budget = budget_from_environment(os.environ)
    limits = limits_from_environment(os.environ)
    evaluations: dict[str, Evaluation] = {}

    def models_of(configuration: Configuration) -> RoleModels:
        return configured_models(configuration, os.environ)

    def measure(
        label: str, configuration: Configuration, models: RoleModels, how: OrchestrationChoice
    ) -> None:
        print(f"running {label}", file=sys.stderr, flush=True)
        evaluations[label] = evaluate_configured(
            cases,
            configuration,
            repetitions=arguments.repetitions,
            orchestration=how,
            decider=configured_decider(decision_configuration(os.environ), models),
            budget=budget,
            models=models,
        )
        # A run can take a long time, so each is shown as it ends, apart from the comparison.
        print(Comparison({label: evaluations[label]}).to_json(), file=sys.stderr, flush=True)

    with tempfile.TemporaryDirectory() as directory, shown_within(limits):
        hand_made = Path(directory) / "hand-made"
        hand_made.mkdir()
        cases += hand_made_cases(hand_made)
        if arguments.clones is not None:
            cases += [
                built(case, clone_of(case.repository, arguments.clones), Path(directory))
                for case in load()
            ]
        if not arguments.held_out:
            cases[:] = [case for case in cases if case.part is Part.DEVELOPMENT]

        for configuration, how in runs:
            label = label_of(configuration, how)
            measure(label, configuration, models_of(configuration), how)
            if arguments.capabilities:
                measure(
                    f"{label}, no prompt caching",
                    configuration,
                    without_prompt_caching(models_of(configuration)),
                    how,
                )
                unrelied = without_parallel_tool_calls(configuration)
                measure(f"{label}, no parallel tool calls", unrelied, models_of(unrelied), how)

        baseline = None
        if arguments.baseline is not None:
            print(f"running the {BASELINE_LABEL}", file=sys.stderr, flush=True)
            baseline = evaluate(
                cases,
                models_of(named[arguments.baseline]),
                repetitions=arguments.repetitions,
                entry_point=partial(reading_only_baseline, step_cap=WRITER_STEP_CAP),
            )

    for label, evaluation in {**evaluations, BASELINE_LABEL: baseline}.items():
        for part in (evaluation.development, evaluation.held_out) if evaluation else ():
            for case_run in part.runs:
                if case_run.no_report is not None:
                    print(f"{label}: {case_run.name}: {case_run.no_report}", file=sys.stderr)
    print(Comparison(evaluations, baseline).to_json())


if __name__ == "__main__":
    main()
