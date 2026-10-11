# Working rules

Read `CONTEXT.md` for the project's vocabulary and `docs/adr/` for decisions already made before changing anything. Use those terms in code, tests, and commit messages.

## Three tickets per group of work

Each group of work is built as three tickets, in this order, each on its own branch and merged to `main` before the next starts:

| Branch | What it holds |
| --- | --- |
| `contract/<group>` | The public surface as stubs with docstrings. No behaviour. |
| `tests/<group>` | Tests written from the contract, reviewed by the project owner. |
| `impl/<group>` | The implementation that makes those tests pass. |

An `impl/` branch starts from `main` after the tests for its group have merged. The locks below compare `tests/` with the point where the branch left `main`.

## Tests are locked during implementation

On an `impl/` branch you may read and run everything under `tests/`. You may not add, change, or delete anything there.

- Write a general solution. Do not special-case test inputs or copy expected values from tests.
- Do not skip tests, swallow exceptions, or replace real work with stand-ins.
- If a test looks wrong or contradicts the contract, stop and report it. Do not work around it.

Two locks enforce this, both running `tools/tests_lock.py`:

- A Claude Code hook (`.claude/settings.json`) refuses a file edit under `tests/`, and after every shell command reports any file under `tests/` that no longer matches where the branch left `main`.
- The `ci` workflow fails a pull request from an `impl/` branch whose diff touches `tests/`.

The locks stop shortcuts, not a determined adversary: a pull request can change the lock itself. Any change to `tools/tests_lock.py`, `.claude/`, or `.github/` on an `impl/` branch is a reason to stop and report.

Before an `impl/` pull request merges, the `coded-to-tests-reviewer` agent (`.claude/agents/`) reads its diff in a fresh context and reports signs of coding to the tests.

## Tests tickets

Assert only what the contract states. Use Hypothesis for property-based tests of pure rules.

## Before committing

```sh
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pytest
```

## Size

Keep each ticket's change small enough to read in one sitting.
