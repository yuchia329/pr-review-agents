"""The Python Ecosystem: what it recognises, and what its commands do in a real Sandbox.

The tests from `install_and_test` down install from the package index in a real Sandbox.
"""

import functools
import tempfile
from pathlib import Path

import pytest
from conftest import FixtureRepository, commit_snapshots
from hypothesis import given, settings
from hypothesis import strategies as st

from pr_verification.ecosystem import Commands, Results, Unreadable, Unsupported, find_commands
from pr_verification.python_ecosystem import (
    LOCK_FILES,
    NOT_A_PYTHON_PROJECT,
    PYTHON_VERSIONS,
    PythonEcosystem,
)
from pr_verification.repo_profile import digest_lock_files
from pr_verification.sandbox import ContainerSandbox, LimitHit, Limits, Phase, RunResult

MODULE = "def cost(kilograms):\n    return 3 * kilograms\n"
INSTALL_LIMITS = Limits(seconds=600.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)
TEST_LIMITS = Limits(seconds=120.0, memory_bytes=1024 * 1024 * 1024, processors=2.0)


def pyproject(requires_python: str | None = None) -> str:
    stated = "" if requires_python is None else f'requires-python = "{requires_python}"\n'
    return f'[project]\nname = "shipping"\nversion = "1.0"\n{stated}'


def commands_for(directory: Path, files: dict[str, str]) -> Commands | Unsupported:
    """What the Python Ecosystem answers for a repository whose one commit holds these files."""
    (commit,) = commit_snapshots(directory / "repository", files)
    return PythonEcosystem().commands(directory / "repository", commit)


# Recognising a repository


@pytest.mark.parametrize(
    "files",
    [
        {"pyproject.toml": pyproject(), "src/shipping/__init__.py": MODULE},
        {"pyproject.toml": '[build-system]\nrequires = ["setuptools"]\n', "shipping.py": MODULE},
        {"setup.py": "from setuptools import setup\n\nsetup(name='shipping')\n"},
        {"setup.cfg": "[metadata]\nname = shipping\n", "shipping/__init__.py": MODULE},
        {"requirements.txt": "six\n", "README.md": "Shipping rates.\n"},
        {"tools/shipping.py": MODULE, "README.md": "Shipping rates.\n"},
    ],
    ids=["project table", "build-system table", "setup.py", "setup.cfg", "requirements", "plain"],
)
def test_common_python_layout_is_given_commands_without_being_told(
    tmp_path: Path, files: dict[str, str]
) -> None:
    found = commands_for(tmp_path, files)

    assert isinstance(found, Commands)
    assert isinstance(found.ecosystem, PythonEcosystem)
    assert found.test != ()


@pytest.mark.parametrize(
    "files",
    [
        {"README.md": "Shipping rates.\n"},
        {"go.mod": "module example.invalid/shipping\n", "main.go": "package main\n"},
        {"pyproject.toml": "[tool.ruff]\nline-length = 100\n", "README.md": "Shipping rates.\n"},
    ],
    ids=["no code", "another ecosystem", "pyproject.toml with only tool settings"],
)
def test_repository_of_no_python_layout_is_unsupported_for_the_stated_reason(
    tmp_path: Path, files: dict[str, str]
) -> None:
    assert commands_for(tmp_path, files) == Unsupported(reason=NOT_A_PYTHON_PROJECT)


def test_answer_is_read_from_the_commit_whatever_the_working_tree_holds(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    python_commit, other_commit = commit_snapshots(
        repository, {"shipping.py": MODULE}, {"README.md": "Shipping rates.\n"}
    )
    (repository / "setup.py").write_text("from setuptools import setup\n\nsetup()\n")

    assert isinstance(PythonEcosystem().commands(repository, python_commit), Commands)
    assert PythonEcosystem().commands(repository, other_commit) == Unsupported(NOT_A_PYTHON_PROJECT)


def test_working_out_the_commands_runs_no_file_of_the_commit(tmp_path: Path) -> None:
    ran = tmp_path / "a-file-of-the-commit-ran"
    leaves_a_mark = f"from pathlib import Path\n\nPath({str(ran)!r}).touch()\n"

    found = commands_for(tmp_path, {"setup.py": leaves_a_mark, "conftest.py": leaves_a_mark})

    assert isinstance(found, Commands)
    assert not ran.exists()


def test_repository_whose_packaging_metadata_cannot_be_parsed_is_answered_not_raised(
    tmp_path: Path,
) -> None:
    found = commands_for(tmp_path, {"pyproject.toml": "[project\nname = ", "shipping.py": MODULE})

    assert isinstance(found, Commands | Unsupported)


# Choosing the image


@pytest.mark.parametrize(
    ("requires_python", "version"),
    [
        (None, PYTHON_VERSIONS[0]),
        (">=3.10", PYTHON_VERSIONS[0]),
        ("<3.13", "3.12"),
        (">=3.11,<3.14", "3.13"),
        ("==3.11.*", "3.11"),
    ],
)
def test_image_is_the_newest_python_that_requires_python_allows(
    tmp_path: Path, requires_python: str | None, version: str
) -> None:
    found = commands_for(tmp_path, {"pyproject.toml": pyproject(requires_python)})

    assert isinstance(found, Commands)
    assert found.image == f"python:{version}-slim"


def test_image_is_the_newest_python_for_a_project_with_no_pyproject_toml(tmp_path: Path) -> None:
    found = commands_for(tmp_path, {"requirements.txt": "six\n", "shipping.py": MODULE})

    assert isinstance(found, Commands)
    assert found.image == f"python:{PYTHON_VERSIONS[0]}-slim"


def test_commits_before_and_after_a_pull_request_each_get_their_own_image(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    before, after = commit_snapshots(
        repository, {"pyproject.toml": pyproject("<3.12")}, {"pyproject.toml": pyproject("<3.14")}
    )

    for_before = PythonEcosystem().commands(repository, before)
    for_after = PythonEcosystem().commands(repository, after)

    assert isinstance(for_before, Commands)
    assert isinstance(for_after, Commands)
    assert (for_before.image, for_after.image) == ("python:3.11-slim", "python:3.13-slim")


# Reading a run that holds no results


@given(output=st.text(), limit=st.sampled_from(LimitHit))
def test_run_stopped_at_a_limit_is_unreadable_whatever_it_wrote(
    output: str, limit: LimitHit
) -> None:
    stopped = RunResult(output=output, exit_status=None, limit_hit=limit)

    assert isinstance(PythonEcosystem().read_results(stopped), Unreadable)


@pytest.mark.parametrize(
    "run",
    [
        RunResult(output="", exit_status=127, limit_hit=None),
        RunResult("/usr/local/bin/python: No module named pytest\n", 1, None),
    ],
    ids=["no output", "pytest is not installed"],
)
def test_run_in_which_the_test_tooling_could_not_start_is_unreadable(run: RunResult) -> None:
    assert isinstance(PythonEcosystem().read_results(run), Unreadable)


# The facts a Repo Profile keeps


def test_test_framework_is_pytest_and_a_repository_with_no_tests_yet_has_no_test_layout(
    tmp_path: Path,
) -> None:
    found = commands_for(tmp_path, {"shipping.py": MODULE})

    assert isinstance(found, Commands)
    assert (found.test_framework, found.test_layout) == ("pytest", ())


@pytest.mark.parametrize(
    ("test_files", "layout"),
    [
        (("test_shipping.py",), (".",)),
        (
            ("tests/unit/rates_test.py", "tests/test_tax.py", "tests/test_rates.py"),
            ("tests", "tests/unit"),
        ),
        (
            ("src/shipping/tests/test_rates.py", "checks/tax_test.py"),
            ("checks", "src/shipping/tests"),
        ),
    ],
    ids=["at the root", "nested", "beside the code"],
)
def test_test_layout_names_each_directory_that_holds_a_test_file_once_and_sorted(
    tmp_path: Path, test_files: tuple[str, ...], layout: tuple[str, ...]
) -> None:
    # Neither of the last two is what pytest takes for a test file.
    not_tests = {"shipping.py": MODULE, "helpers/latest.py": MODULE, "docs/test_plan.md": "Soon.\n"}

    found = commands_for(tmp_path, {**not_tests, **dict.fromkeys(test_files, MODULE)})

    assert isinstance(found, Commands)
    assert found.test_layout == layout


@pytest.mark.parametrize(
    "files",
    [{"shipping.py": MODULE}, {"pyproject.toml": pyproject(), "requirements-dev.txt": "six\n"}],
    ids=["holds none of them", "holds some of them"],
)
def test_lock_files_are_the_same_paths_whichever_of_them_the_commit_holds(
    tmp_path: Path, files: dict[str, str]
) -> None:
    found = commands_for(tmp_path, files)

    assert isinstance(found, Commands)
    assert found.lock_files == LOCK_FILES


some_lock_files = st.fixed_dictionaries(
    {},
    optional={
        "pyproject.toml": st.sampled_from([pyproject(), pyproject(">=3.11,<3.13")]),
        "setup.py": st.just("from setuptools import setup\n\nsetup(name='shipping')\n"),
        "requirements.txt": st.just("six\n"),
        "test-requirements.txt": st.just("pytest-timeout\n"),
    },
)
some_other_files = st.fixed_dictionaries(
    {},
    optional={"README.md": st.just("Shipping rates.\n"), "tests/test_shipping.py": st.just(MODULE)},
)


@settings(max_examples=25, deadline=None)
@given(lock_files=some_lock_files, others=st.tuples(some_other_files, some_other_files))
def test_two_commits_that_agree_on_every_lock_file_get_the_same_image_and_install(
    lock_files: dict[str, str], others: tuple[dict[str, str], dict[str, str]]
) -> None:
    with tempfile.TemporaryDirectory() as directory:
        repository = Path(directory) / "repository"
        one, other = commit_snapshots(
            repository,
            {**lock_files, **others[0], "shipping.py": MODULE},
            {**lock_files, **others[1], "shipping.py": MODULE + "\n"},
        )
        first = PythonEcosystem().commands(repository, one)
        second = PythonEcosystem().commands(repository, other)
        assert isinstance(first, Commands) and isinstance(second, Commands)

        assert digest_lock_files(repository, one, first.lock_files) == digest_lock_files(
            repository, other, second.lock_files
        )
    assert (first.image, first.install) == (second.image, second.install)


# Installing and testing fixture repositories in a real Sandbox


# Cached, not a fixture, so that each test sharing a run fails on its own when it cannot be done.
@functools.cache
def install_and_test(repository: Path, commit: str) -> Results | Unreadable:
    """Find the commands for a commit, run them in a Sandbox, and read the results."""
    commands = find_commands(repository, commit)
    assert isinstance(commands, Commands), commands
    with ContainerSandbox(repository, commands.image) as sandbox:
        for install in commands.install:
            installed = sandbox.run(
                install, commit=commit, phase=Phase.INSTALL, limits=INSTALL_LIMITS
            )
            assert installed.exit_status == 0, installed.output
        tested = sandbox.run(commands.test, commit=commit, phase=Phase.TEST, limits=TEST_LIMITS)
    return commands.ecosystem.read_results(tested)


PACKAGED_PYPROJECT = """\
[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[project]
name = "pricing"
version = "0.1.0"
requires-python = ">=3.10"
dependencies = ["six"]

[project.optional-dependencies]
test = ["sortedcontainers"]
tests = ["toolz"]
testing = ["idna"]
dev = ["zipp"]

[dependency-groups]
test = ["decorator"]
tests = ["colorama"]
testing = ["pyparsing"]
dev = ["cachetools"]
"""

# What `PACKAGED_PYPROJECT` names, one package for each place, and a `requirements.txt` beside it.
PRICING_NEEDS = [
    "six",
    "sortedcontainers",
    "toolz",
    "idna",
    "zipp",
    "decorator",
    "colorama",
    "pyparsing",
    "cachetools",
    "tabulate",
]

PRICING = """\
RATES = {"home": 0.2, "abroad": 0.0}


def rate_for(region):
    return RATES[region]


def total(net, region):
    return round(net * (1 + rate_for(region)), 2)
"""

PRICING_BEFORE = PRICING.replace("round(net * (1 + rate_for(region)), 2)", "net")

TEST_PRICING = f"""\
import importlib

import pytest

from pricing import rate_for, total


def test_total_adds_tax():
    assert total(100, "home") == 120.0


@pytest.mark.parametrize("region", ["home", "abroad"])
def test_region_has_a_rate(region):
    assert rate_for(region) >= 0


def test_half_pennies_round_up():
    assert total(0.125, "abroad") == 0.13


@pytest.fixture
def exchange_rates():
    raise RuntimeError("the exchange rate service cannot be reached")


def test_total_in_another_currency(exchange_rates):
    assert total(100, "abroad") == 100.0


@pytest.mark.skip(reason="not written yet")
def test_bulk_discount():
    raise AssertionError


@pytest.mark.xfail(reason="refunds are not handled yet")
def test_refund_keeps_its_fee():
    assert total(-100, "home") == -120.5


@pytest.mark.xfail(reason="thought to be broken")
def test_nothing_costs_nothing():
    assert total(0, "home") == 0


@pytest.mark.parametrize("module", {PRICING_NEEDS!r})
def test_is_installed(module):
    importlib.import_module(module)
"""

PRICING_TESTS = "tests/test_pricing.py::"
PRICING_NEEDS_INSTALLED = [
    PRICING_TESTS + f"test_is_installed[{module}]" for module in PRICING_NEEDS
]


@pytest.fixture(scope="module")
def packaged(tmp_path_factory: pytest.TempPathFactory) -> FixtureRepository:
    """A packaged project under `src/`. The pull request makes `total` add tax."""
    path = tmp_path_factory.mktemp("packaged") / "repository"
    files = {
        "pyproject.toml": PACKAGED_PYPROJECT,
        "requirements.txt": "tabulate\n",
        "tests/test_pricing.py": TEST_PRICING,
    }
    before, after = commit_snapshots(
        path,
        {**files, "src/pricing/__init__.py": PRICING_BEFORE},
        {**files, "src/pricing/__init__.py": PRICING},
    )
    return FixtureRepository(path=path, before=before, after=after)


def test_packaged_project_is_installed_with_its_dependencies_test_dependencies_and_pytest(
    packaged: FixtureRepository,
) -> None:
    results = install_and_test(packaged.path, packaged.after)

    assert isinstance(results, Results)
    assert {PRICING_TESTS + "test_total_adds_tax", *PRICING_NEEDS_INSTALLED} <= set(results.passed)


def test_results_name_only_the_tests_that_passed_failed_or_could_not_run_by_node_id(
    packaged: FixtureRepository,
) -> None:
    results = install_and_test(packaged.path, packaged.after)

    # The file's skipped, xfailed, and xpassed tests are in none of the three.
    assert isinstance(results, Results)
    assert sorted(results.passed) == sorted(
        [
            PRICING_TESTS + "test_region_has_a_rate[abroad]",
            PRICING_TESTS + "test_region_has_a_rate[home]",
            PRICING_TESTS + "test_total_adds_tax",
            *PRICING_NEEDS_INSTALLED,
        ]
    )
    assert results.failed == (PRICING_TESTS + "test_half_pennies_round_up",)
    assert results.could_not_run == (PRICING_TESTS + "test_total_in_another_currency",)


def test_test_the_pull_request_fixes_fails_before_it_and_passes_after_it(
    packaged: FixtureRepository,
) -> None:
    before = install_and_test(packaged.path, packaged.before)
    after = install_and_test(packaged.path, packaged.after)

    assert isinstance(before, Results)
    assert isinstance(after, Results)
    assert PRICING_TESTS + "test_total_adds_tax" in before.failed
    assert PRICING_TESTS + "test_total_adds_tax" in after.passed


TEST_SHIPPING = """\
import importlib
import unittest

from shipping import cost


class ShippingCost(unittest.TestCase):
    def test_heavier_parcels_cost_more(self):
        self.assertGreater(cost(2), cost(1))

    def test_requirements_are_installed(self):
        for module in ["six", "sortedcontainers", "toolz", "idna", "zipp"]:
            importlib.import_module(module)
"""


def test_project_with_requirements_files_is_installed_and_its_unittest_style_tests_run(
    tmp_path: Path,
) -> None:
    files = {
        "requirements.txt": "six\n",
        "requirements-dev.txt": "sortedcontainers\n",
        "requirements-test.txt": "toolz\n",
        "dev-requirements.txt": "idna\n",
        "test-requirements.txt": "zipp\n",
        "shipping.py": MODULE,
        "test_shipping.py": TEST_SHIPPING,
    }
    (commit,) = commit_snapshots(tmp_path / "repository", files)

    results = install_and_test(tmp_path / "repository", commit)

    assert isinstance(results, Results)
    assert sorted(results.passed) == [
        "test_shipping.py::ShippingCost::test_heavier_parcels_cost_more",
        "test_shipping.py::ShippingCost::test_requirements_are_installed",
    ]
    assert (results.failed, results.could_not_run) == ((), ())


def test_plain_modules_with_no_tests_yet_give_results_with_all_three_empty(
    tmp_path: Path,
) -> None:
    (commit,) = commit_snapshots(tmp_path / "repository", {"tools/shipping.py": MODULE})

    results = install_and_test(tmp_path / "repository", commit)

    assert results == Results(passed=(), failed=(), could_not_run=())


def test_test_file_that_cannot_be_imported_is_named_by_its_path_as_could_not_run(
    tmp_path: Path,
) -> None:
    broken = "import a_module_nobody_wrote\n\n\ndef test_cost():\n    assert True\n"
    files = {"tools/shipping.py": MODULE, "tools/test_shipping.py": broken}
    (commit,) = commit_snapshots(tmp_path / "repository", files)

    results = install_and_test(tmp_path / "repository", commit)

    assert isinstance(results, Results)
    assert "tools/test_shipping.py" in results.could_not_run
