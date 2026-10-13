"""The Structural cases of the evaluation: real commits whose Structural Claim has a known answer.

A measurement, not a test. The cases are data, in `evaluation/structural_cases.toml`, and this
runs them through the evaluation harness with the model and the decider the environment
configures, as `docs/model-endpoints.md` and `docs/decision-layer.md` describe:

    uv run --extra laya python tools/structural_cases.py --clones ~/.cache/pr-verification

It prints, as JSON, what the harness scored and the Verdicts each run gave. `--clones` names a
directory that the repositories the cases come from are cloned into, once, from GitHub.

Each entry of the data is one real commit and what a pull request for it would say, written by
hand so that every Claim in it is Structural and has the one answer. It gives a true case,
which expects Confirmed. An entry with `left_out_of_false_case` gives a false case as well,
named `<name>@left-out`: the same pull request text over the same commit, with the commit's
change to those files left out, so that what it claims is false. It expects Refuted, and is in
the part its true case is in.

Each entry also holds an answer key: texts a careful reader of the Claim would have the code
searched for. The system is never shown it. `--check-keys` asks no model: it searches each
case's two commits for the texts of its key, as the Structural verifier would, and computes the
Verdict. A true case whose key does not give Confirmed, or a false case whose key gives
Confirmed, is a case whose answer the code does not bear out, and the command exits with 1.
A false case whose key gives Inconclusive is one the rule for Refuted cannot reach.
"""

import argparse
import json
import os
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pr_verification.core import PullRequest, run
from pr_verification.decision import configured_decider, decision_configuration
from pr_verification.evaluation import Case, Part, evaluate
from pr_verification.model import Model
from pr_verification.openai_compatible import model_from_environment
from pr_verification.report import ClaimKind, Report, TextChange, Verdict
from pr_verification.repository import commit_staged, git, repository_at, resolve_commit
from pr_verification.structural import search_for
from pr_verification.verdict import compute_verdict

CASES = Path(__file__).parent.parent / "evaluation" / "structural_cases.toml"
"""The data: one `[[commit]]` table for each real commit."""

KINDS = ("rename", "removal", "documentation", "configuration")
"""The kinds of Structural Claim the cases cover. A case's name starts `structural/<kind>/`."""

FALSE_CASE = "@left-out"
"""What the name of a false case ends with, after the name of its true case."""


@dataclass(frozen=True)
class KeyText:
    """One text of an answer key, as the Structural verifier names one."""

    text: str
    change: TextChange
    path: str | None = None


@dataclass(frozen=True)
class StructuralCase:
    """One case as the data gives it, before its repository is made."""

    name: str
    part: Part
    kind: str
    """One of `KINDS`."""
    repository: str
    """The repository on GitHub the commit is from, as `<owner>/<name>`."""
    commit: str
    """The full ID of the real commit. The code before the pull request is its first parent."""
    title: str
    description: str
    key: tuple[KeyText, ...]
    left_out: tuple[str, ...] = ()
    """The files whose change is left out of the pull request. Empty for a true case."""

    @property
    def expected(self) -> Verdict:
        return Verdict.REFUTED if self.left_out else Verdict.CONFIRMED


def load(data: Path = CASES) -> tuple[StructuralCase, ...]:
    """The cases of `data`, each true case followed by its false case when it has one.

    Raises `ValueError` when a name does not start `structural/<kind>/` for a kind of `KINDS`,
    when two entries share a name, or when an entry has an empty key.
    """
    with data.open("rb") as file:
        entries = tomllib.load(file)["commit"]
    shared = [
        name for name, count in Counter(entry["name"] for entry in entries).items() if count > 1
    ]
    if shared:
        raise ValueError(f"more than one entry is named {', '.join(shared)}")
    cases: list[StructuralCase] = []
    for entry in entries:
        name = entry["name"]
        prefix, kind, _ = (name.split("/", 2) + ["", ""])[:3]
        if prefix != "structural" or kind not in KINDS:
            raise ValueError(f"{name} is not named structural/<kind>/ for a kind of {KINDS}")
        if not entry["key"]:
            raise ValueError(f"{name} has no answer key")
        true_case = StructuralCase(
            name=name,
            part=Part(entry["part"]),
            kind=kind,
            repository=entry["repository"],
            commit=entry["commit"],
            title=entry["title"],
            description=entry.get("description", ""),
            key=tuple(
                KeyText(text["text"], TextChange(text["change"]), text.get("path"))
                for text in entry["key"]
            ),
        )
        cases.append(true_case)
        left_out = tuple(entry.get("left_out_of_false_case", ()))
        if left_out:
            cases.append(replace(true_case, name=name + FALSE_CASE, left_out=left_out))
    return tuple(cases)


def clone_of(repository: str, clones: Path) -> Path:
    """The clone of `repository` under `clones`, made from GitHub when it is not there yet."""
    clone = clones / f"{repository.replace('/', '--')}.git"
    if not clone.exists():
        clones.mkdir(parents=True, exist_ok=True)
        url = f"https://github.com/{repository}.git"
        git(clones, "clone", "--quiet", "--bare", url, clone.name)
    return clone


def built(case: StructuralCase, clone: Path, directory: Path) -> Case:
    """The case for the harness, with its repository made in a directory of its own under
    `directory`. `clone` is a local clone of the case's repository and is only read.

    The new repository holds the real commit's first parent, which is `before`, its ancestors,
    and `after`: a new commit whose only parent is `before` and whose files are the real
    commit's, but for the files of `left_out`, which are as `before` has them. So nothing that
    came later in the project's history is within the system's reach, nor, for a false case,
    the change that was left out. The message of `after` is the title, for a true case and a
    false one alike.

    Raises `ValueError` when a file of `left_out` is not one the real commit changes, or when
    leaving them out leaves no change at all.
    """
    commit = resolve_commit(clone, case.commit)
    before = resolve_commit(clone, f"{commit}^")
    changed = git(clone, "diff", "--name-only", "--no-renames", "-z", before, commit)
    unchanged = set(case.left_out) - set(os.fsdecode(changed).split("\0"))
    if unchanged:
        raise ValueError(f"{case.name} leaves out {sorted(unchanged)}, which its commit keeps")
    kept = (":(top)", *(f":(top,exclude,literal){path}" for path in case.left_out))
    patch = git(
        clone, "diff", "--binary", "--full-index", "--no-renames", before, commit, "--", *kept
    )
    if not patch:
        raise ValueError(f"{case.name} leaves out every change its commit makes")
    repository = repository_at(clone, before, directory)
    git(repository, "apply", "--cached", "--whitespace=nowarn", stdin=patch)
    after = commit_staged(repository, case.title, before)
    pull_request = PullRequest(repository, before, after, case.title, case.description)
    return Case(case.name, case.part, pull_request, case.expected)


def key_verdict(case: Case, key: tuple[KeyText, ...]) -> Verdict:
    """The Verdict the code computes when a case's two commits are searched for the texts of
    `key`: what the Structural verifier's Evidence would give had it named exactly those."""
    shown = case.pull_request
    evidence = [
        search_for(shown.repository, shown.before, shown.after, text.text, text.change, text.path)
        for text in key
    ]
    return compute_verdict(evidence, ClaimKind.STRUCTURAL).verdict


def borne_out(expected: Verdict, by_key: Verdict) -> bool:
    """Whether a case's answer key bears out its expected Verdict: Confirmed for a true case,
    and anything else for a false one."""
    return (by_key is Verdict.CONFIRMED) is (expected is Verdict.CONFIRMED)


def main() -> None:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--clones", type=Path, required=True)
    parser.add_argument("--check-keys", action="store_true")
    parser.add_argument("--part", type=Part, choices=list(Part))
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--case", action="append", default=[], metavar="TEXT")
    parser.add_argument("--reports", type=Path, metavar="DIRECTORY")
    arguments = parser.parse_args()

    wanted = [
        case
        for case in load()
        if arguments.part in (None, case.part)
        and (not arguments.case or any(text in case.name for text in arguments.case))
    ]
    with tempfile.TemporaryDirectory() as directory:
        cases = [
            built(case, clone_of(case.repository, arguments.clones), Path(directory))
            for case in wanted
        ]
        if arguments.check_keys:
            checked = {
                case.name: key_verdict(case, structural.key)
                for structural, case in zip(wanted, cases, strict=True)
            }
            print(json.dumps(checked, indent=2))
            if not all(borne_out(case.expected, checked[case.name]) for case in cases):
                sys.exit(1)
            return

        model = model_from_environment(os.environ)
        if model is None:
            parser.error("no model is configured: see docs/model-endpoints.md")
        decider = configured_decider(decision_configuration(os.environ), model)

        names = {case.pull_request.after: case.name for case in cases}
        written: Counter[str] = Counter()

        def classified_run(pull_request: PullRequest, model: Model) -> Report:
            report = run(pull_request, model, decider)
            if arguments.reports is not None:
                # One file for each run, so the Evidence behind a Verdict can be read afterwards.
                name = names[pull_request.after].removeprefix("structural/").replace("/", "--")
                written[name] += 1
                arguments.reports.mkdir(parents=True, exist_ok=True)
                (arguments.reports / f"{name}.{written[name]}.json").write_text(report.to_json())
            return report

        evaluation = evaluate(
            cases, model, repetitions=arguments.repetitions, entry_point=classified_run
        )

    runs: list[dict[str, Any]] = [
        {
            "case": case_run.name,
            "repetition": case_run.repetition,
            "expected": case_run.expected,
            "verdicts": list(case_run.verdicts),
            "no_report": case_run.no_report,
        }
        for part in (evaluation.development, evaluation.held_out)
        for case_run in part.runs
    ]
    print(json.dumps({"evaluation": json.loads(evaluation.to_json()), "runs": runs}, indent=2))


if __name__ == "__main__":
    main()
