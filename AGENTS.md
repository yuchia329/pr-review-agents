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

## Merging a finished ticket

The project has one developer, so no ticket goes through a pull request. This holds for every ticket: contract, tests, and implementation. When a ticket's work is complete, the agent that did it merges it:

1. Run the checks under "Before committing". When `main` already fails some tests, as it does while another group's tests wait for their implementation, the branch must fail the same tests and no others.
2. On an `impl/` branch, also run the tests lock by hand and get the `coded-to-tests-reviewer` report, as described below. Stop and report if either objects.
3. Rebase the branch onto `main` if `main` has moved, so that history stays linear.
4. Fast-forward `main` to the branch and push `main`.
5. Close the ticket's issue with a comment naming the commits.

Do not open a pull request, and do not leave a finished branch waiting for the project owner to merge. A tests ticket is the one exception to merging at once: the project owner reviews its tests first.

## Tests are locked during implementation

On an `impl/` branch you may read and run everything under `tests/`. You may not add, change, or delete anything there.

- Write a general solution. Do not special-case test inputs or copy expected values from tests.
- Do not skip tests, swallow exceptions, or replace real work with stand-ins.
- If a test looks wrong or contradicts the contract, stop and report it. Do not work around it.

Two locks enforce this, both running `tools/tests_lock.py`:

- A Claude Code hook (`.claude/settings.json`) refuses a file edit under `tests/`, and after every shell command reports any file under `tests/` that no longer matches where the branch left `main`.
- Before an `impl/` branch merges, the same check is run by hand from the branch:

  ```sh
  python3 tools/tests_lock.py pull-request --branch impl/<group> --base main
  ```

  The `ci` workflow runs this check only for a pull request, and none is opened, so on `main` it runs lint, the type check, and the tests after the push.

The locks stop shortcuts, not a determined adversary: a branch can change the lock itself. Any change to `tools/tests_lock.py`, `.claude/`, or `.github/` on an `impl/` branch is a reason to stop and report.

Before an `impl/` branch merges, the `coded-to-tests-reviewer` agent (`.claude/agents/`) reads its diff in a fresh context and reports signs of coding to the tests.

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
