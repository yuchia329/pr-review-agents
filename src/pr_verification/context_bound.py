"""The bound on how much a role is shown of any one thing it reads.

A role's context holds its task and what it reads. The step cap and the cap on attempts limit
how many things a role reads. The limits here are on how big each one may be when it is shown
to a model: one result of a tool, and the output of one run in the Sandbox. A text over its
limit is cut by code, and the cut says so.

Only what a model is shown is cut. The record of a run and the report keep every text whole.
"""

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum

SMALLEST_LIMIT = 200
"""The smallest limit a text can be cut to. It leaves room for the words that say a text was
cut, however long the text."""

TOOL_RESULT_LIMIT = 20_000
"""The most characters a role is shown of one result of a tool unless another limit is set.
Kept after the measurement in `docs/context-bound.md`. It may be chosen again from another,
so nothing may rely on the number itself."""

SANDBOX_OUTPUT_LIMIT = 8_000
"""The most characters the Reproduction writer is shown of the output of one run in the
Sandbox unless another limit is set. A starting value: it is to be chosen from measurement,
so nothing may rely on the number itself."""

TOOL_RESULT_VARIABLE = "PR_VERIFICATION_TOOL_RESULT_LIMIT"
SANDBOX_OUTPUT_VARIABLE = "PR_VERIFICATION_SANDBOX_OUTPUT_LIMIT"

CUT_AFTER = "\n[Cut here: the last {left_out} of {whole} characters are left out.]"
"""What ends a text that was cut keeping its start. `whole` is the number of characters the
text had and `left_out` is how many of them are not shown."""

CUT_BEFORE = "[Cut here: the first {left_out} of {whole} characters are left out.]\n"
"""What starts a text that was cut keeping its end, with `left_out` and `whole` as in
`CUT_AFTER`."""


class Keep(StrEnum):
    """Which part of a text is kept when it is cut: the part most likely to matter."""

    START = "start"
    """The start, as for a file or a listing, which reads from the top."""

    END = "end"
    """The end, as for the output of a test run, which ends with what went wrong."""


def bounded(text: str, limit: int, keep: Keep) -> str:
    """`text` as a model is shown it: whole when it is no longer than `limit`, and cut when
    it is longer. What is returned is never longer than `limit`.

    Length is counted in characters, as `len` counts them. It is not counted in tokens, and
    not in what a character takes once a request is encoded.

    - A text of `limit` characters or fewer is returned as it is.
    - A longer text is cut to `kept` of its characters, and the cut says so:
      with `Keep.START`, the first `kept` characters and then `CUT_AFTER`; with `Keep.END`,
      `CUT_BEFORE` and then the last `kept` characters. Both are filled in with `whole` as
      the length of `text` and `left_out` as `whole - kept`. `kept` is the largest number
      for which what is returned is no longer than `limit`, so nothing is left out that
      there was room for. The cut is made by counting characters and may fall inside a line.

    So a reader can tell that a text was cut, at which end, and how much is missing. A text
    may itself hold the words of `CUT_AFTER` or `CUT_BEFORE`: it may be Untrusted Content,
    it is shown as data whether cut or not, and nothing reads the words back.

    Raises `ValueError` when `limit` is less than `SMALLEST_LIMIT`.
    """
    if limit < SMALLEST_LIMIT:
        raise ValueError(f"the limit is {limit}: under {SMALLEST_LIMIT}")
    whole = len(text)
    if whole <= limit:
        return text
    said = CUT_AFTER if keep is Keep.START else CUT_BEFORE
    # The words grow as more is left out, so the most that fits is found by trying. They are
    # far shorter than the smallest limit, so some number of characters always fits.
    kept = limit
    while kept + len(said.format(left_out=whole - kept, whole=whole)) > limit:
        kept -= 1
    words = said.format(left_out=whole - kept, whole=whole)
    return text[:kept] + words if keep is Keep.START else words + text[whole - kept :]


@dataclass(frozen=True)
class ContextLimits:
    """The limits on what a role is shown of any one thing it reads, in characters."""

    tool_result: int = TOOL_RESULT_LIMIT
    """The most a role is shown of one result of a tool, as `run_role` says."""

    sandbox_output: int = SANDBOX_OUTPUT_LIMIT
    """The most the Reproduction writer is shown of the output of one run in the Sandbox
    from an earlier attempt, as `write_reproduction` says."""


class InvalidContextLimits(Exception):
    """A limit is configured with a value it cannot have. Its message is safe to print: it
    names the variable at fault."""


def limits_from_environment(environment: Mapping[str, str]) -> ContextLimits:
    """Read the limits from two environment variables.

    A variable that is unset or set to the empty string takes its default.

    - `PR_VERIFICATION_TOOL_RESULT_LIMIT` is `tool_result`, a whole number of characters.
      The default is `TOOL_RESULT_LIMIT`.
    - `PR_VERIFICATION_SANDBOX_OUTPUT_LIMIT` is `sandbox_output`, a whole number of
      characters. The default is `SANDBOX_OUTPUT_LIMIT`.

    Raises `InvalidContextLimits`, with a message that names the variable at fault, for a
    limit that is not a whole number, as `2.5` is not, or is less than `SMALLEST_LIMIT`.
    """
    return ContextLimits(
        tool_result=_limit(environment, TOOL_RESULT_VARIABLE, TOOL_RESULT_LIMIT),
        sandbox_output=_limit(environment, SANDBOX_OUTPUT_VARIABLE, SANDBOX_OUTPUT_LIMIT),
    )


def _limit(environment: Mapping[str, str], variable: str, default: int) -> int:
    """The limit `variable` sets, or `default` when it sets none."""
    text = environment.get(variable, "")
    if not text:
        return default
    try:
        limit = int(text)
    except ValueError:
        limit = None
    if limit is None or limit < SMALLEST_LIMIT:
        raise InvalidContextLimits(
            f"{variable} is set to {text!r}, and must be a whole number of characters, "
            f"{SMALLEST_LIMIT} or more"
        )
    return limit


_in_use: ContextVar[ContextLimits | None] = ContextVar("context limits", default=None)
"""The limits in use on this thread, or None where none was set. It is a context variable, so
a thread run in a copy of the context it was started from begins with the limits in use
there, and what it sets is in use on that thread alone."""


@contextmanager
def shown_within(limits: ContextLimits) -> Iterator[None]:
    """Set the limits in use for as long as the `with` lasts.

    Inside the `with`, `limits_in_use` gives `limits`. Leaving it, however it is left, puts
    back what was in use before, so one `with` inside another holds only until it is left.

    The limits are held as tracing holds its open spans: in the context of the code that is
    running. A thread started with a copy of that context, as the supervisor starts each of
    its dispatches, has the same limits in use. Setting them in one thread changes them in
    no other.

    Raises `ValueError`, before anything is set, when either limit is less than
    `SMALLEST_LIMIT`.
    """
    for name, limit in (
        ("tool_result", limits.tool_result),
        ("sandbox_output", limits.sandbox_output),
    ):
        if limit < SMALLEST_LIMIT:
            raise ValueError(f"the limit {name} is {limit}: under {SMALLEST_LIMIT}")
    before = _in_use.set(limits)
    try:
        yield
    finally:
        _in_use.reset(before)


def limits_in_use() -> ContextLimits:
    """The limits in use where this is called: those of the innermost `shown_within` it is
    called inside, and `ContextLimits()`, the defaults, inside none.

    So what a role is shown is bounded whether or not anything set a limit.
    """
    return _in_use.get() or ContextLimits()
