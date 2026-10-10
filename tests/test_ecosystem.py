import ast
from pathlib import Path

import pytest
from conftest import commit_snapshots

import pr_verification
from pr_verification.ecosystem import Commands, Results, Unreadable, Unsupported, find_commands
from pr_verification.python_ecosystem import NOT_A_PYTHON_PROJECT, PythonEcosystem
from pr_verification.sandbox import ContainerSandbox, Limits, Phase, RunResult

LIMITS = Limits(seconds=60.0, memory_bytes=512 * 1024 * 1024, processors=1.0)


def test_commands_are_found_for_a_python_repository_without_naming_its_ecosystem(
    tmp_path: Path,
) -> None:
    (commit,) = commit_snapshots(tmp_path / "repository", {"shipping.py": "RATE = 3\n"})

    found = find_commands(tmp_path / "repository", commit)

    assert isinstance(found, Commands)
    assert isinstance(found.ecosystem, PythonEcosystem)


@pytest.mark.parametrize(
    "files",
    [
        {"go.mod": "module example.invalid/shipping\n", "main.go": "package main\n"},
        {"package.json": '{"name": "shipping"}\n', "src/index.ts": "export const rate = 3;\n"},
    ],
    ids=["go", "typescript"],
)
def test_repository_no_ecosystem_can_run_is_unsupported_for_the_python_ecosystems_own_reason(
    tmp_path: Path, files: dict[str, str]
) -> None:
    (commit,) = commit_snapshots(tmp_path / "repository", files)

    found = find_commands(tmp_path / "repository", commit)

    assert found == Unsupported(reason=NOT_A_PYTHON_PROJECT)


def test_only_the_ecosystem_boundary_imports_the_python_ecosystem() -> None:
    package = Path(pr_verification.__file__).parent

    importers = {
        module.stem
        for module in package.rglob("*.py")
        if any(name.endswith("python_ecosystem") for name in _imported(module))
    }

    assert importers == {"ecosystem"}


def _imported(module: Path) -> set[str]:
    """Every module a file imports, and every name it imports from one, in dotted form."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(module.read_text())):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            names.add(node.module or "")
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


class TapEcosystem:
    """A stand-in Ecosystem with nothing of Python in it: its test tooling is a shell script."""

    def commands(self, repository: Path, commit: str) -> Commands | Unsupported:
        return Commands(
            ecosystem=self,
            image="busybox:1.36",
            install=(("sh", "-c", "echo 'echo ok first; echo not ok second' > run-tests"),),
            test=("sh", "run-tests"),
        )

    def read_results(self, run: RunResult) -> Results | Unreadable:
        lines = run.output.splitlines()
        return Results(
            passed=tuple(line.split()[-1] for line in lines if line.startswith("ok ")),
            failed=tuple(line.split()[-1] for line in lines if line.startswith("not ok ")),
            could_not_run=(),
        )


def test_ecosystem_that_is_not_python_is_run_through_the_same_boundary(tmp_path: Path) -> None:
    (commit,) = commit_snapshots(tmp_path / "repository", {"README.md": "Shipping rates.\n"})
    commands = TapEcosystem().commands(tmp_path / "repository", commit)
    assert isinstance(commands, Commands)

    with ContainerSandbox(tmp_path / "repository", commands.image) as sandbox:
        for install in commands.install:
            installed = sandbox.run(install, commit=commit, phase=Phase.INSTALL, limits=LIMITS)
            assert installed.exit_status == 0, installed.output
        tested = sandbox.run(commands.test, commit=commit, phase=Phase.TEST, limits=LIMITS)

    assert commands.ecosystem.read_results(tested) == Results(
        passed=("first",), failed=("second",), could_not_run=()
    )
