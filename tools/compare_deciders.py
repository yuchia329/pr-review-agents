"""The three-way comparison: the same decisions through Laya, Jev, and the small LLM.

A measurement, not a test. It prints, as JSON, how long Laya took to load and what
`compare_deciders` reports for each implementation that can be reached from this machine:

    uv run --extra laya python tools/compare_deciders.py

Laya is always compared. Jev is compared when `PR_VERIFICATION_JEV_API_KEY` is set, and the
small LLM when a model is configured as `docs/model-endpoints.md` describes. The prices are
arguments, since neither interface reports what a decision cost.
"""

import argparse
import json
import os
from time import perf_counter

from pr_verification.claims import CLAIM_KIND
from pr_verification.decision import (
    JEV_KEY_VARIABLE,
    Decider,
    JevDecider,
    LayaDecider,
    SmallLLMDecider,
)
from pr_verification.evaluation import DecisionCase, compare_deciders
from pr_verification.openai_compatible import model_from_environment
from pr_verification.report import ClaimKind

# Claims written by hand for this comparison, in the words a pull request might use, with the
# kind the glossary gives each. They are few, so the accuracies they give are coarse.
BEHAVIOURAL = (
    "Fixes the crash when the input file is empty",
    "Uploads are now retried three times before giving up",
    "The parser accepts dates without a time zone",
    "Listing a large directory is about twice as fast",
    "Returns 404 instead of 500 for an unknown user",
    "Adds a --dry-run flag that prints the plan without applying it",
    "Stops leaking a file handle when a download is cancelled",
    "Passwords are now compared in constant time",
    "The cache no longer serves entries that have expired",
    "Sorting is stable for records with equal keys",
    "Negative quantities are rejected with a validation error",
    "The exporter writes UTF-8 with no byte order mark",
    "Search matches names whatever their letter case",
    "Fixes an off-by-one error in pagination, so the last page is no longer empty",
    "A request that times out is cancelled on the server as well",
)
STRUCTURAL = (
    "Renames parse_all to parse_many",
    "Removes the unused legacy_auth module",
    "Updates the README with the new install steps",
    "Raises the default timeout in settings.toml from 30 to 60",
    "Moves the HTTP helpers into their own package",
    "Adds type hints to the public functions of the client",
    "Deletes the deprecated v1 route definitions",
    "Bumps the pinned version of requests to 2.32",
    "Adds a docstring to every function in utils.py",
    "Marks the old Config class as deprecated in a comment",
    "Splits models.py into one file for each model",
    "Adds the MIT licence header to every source file",
    "Fixes typos in the comments of the scheduler",
    "Renames the environment variable API_TOKEN to SERVICE_TOKEN in the example configuration",
    "Adds a CHANGELOG entry for version 2.1",
)
CASES = tuple(
    DecisionCase(f"{kind.value}-{number}", claim, CLAIM_KIND, expected=kind.value)
    for kind, claims in ((ClaimKind.BEHAVIOURAL, BEHAVIOURAL), (ClaimKind.STRUCTURAL, STRUCTURAL))
    for number, claim in enumerate(claims, start=1)
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jev-dollars-per-decision", type=float, default=0.0)
    parser.add_argument("--small-llm-dollars-per-request", type=float, default=0.0)
    arguments = parser.parse_args()

    laya = LayaDecider()
    started = perf_counter()
    # The first decision loads Laya, and a comparison does not time loading.
    laya.decide("Renames a helper function", CLAIM_KIND)
    loading = perf_counter() - started

    deciders: dict[str, Decider] = {"laya": laya}
    jev_key = os.environ.get(JEV_KEY_VARIABLE, "")
    if jev_key:
        deciders["jev"] = JevDecider(jev_key, arguments.jev_dollars_per_decision)
    model = model_from_environment(os.environ)
    if model is not None:
        deciders["small_llm"] = SmallLLMDecider(model, arguments.small_llm_dollars_per_request)

    comparison = compare_deciders(CASES, deciders)
    measured = {
        "laya_first_decision_seconds": loading,
        "laya_input_limit": laya.input_limit,
        "decisions": json.loads(comparison.to_json()),
    }
    print(json.dumps(measured, indent=2))


if __name__ == "__main__":
    main()
