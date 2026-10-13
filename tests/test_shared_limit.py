"""Several tools in one step: the limit on what a role is shown, shared between their results.

The model is a scripted stand-in that offers parallel tool calls, and the tool is the stand-in
of the context bound's tests.
"""

import json

from conftest import finishes
from hypothesis import example, given
from hypothesis import strategies as st
from test_context_bound import KEEPS, LONG, TASK, TEXTS, Returning, results_shown

from pr_verification.agent import PARALLEL_CALL_CAP, Role, StepOutcome, run_role
from pr_verification.context_bound import (
    SMALLEST_LIMIT,
    ContextLimits,
    Keep,
    bounded,
    shown_within,
)
from pr_verification.model import ScriptedModel


@given(
    returns=st.lists(TEXTS, min_size=1, max_size=PARALLEL_CALL_CAP),
    limit=st.integers(min_value=SMALLEST_LIMIT, max_value=4 * len(LONG)),
    keep=KEEPS,
)
# What one result leaves unused of its share is given to no other.
@example(returns=["RATE = 3", LONG], limit=3 * SMALLEST_LIMIT, keep=Keep.START)
# A share is never under the smallest limit, however many tools the step asks for.
@example(returns=[LONG] * PARALLEL_CALL_CAP, limit=SMALLEST_LIMIT, keep=Keep.END)
def test_tools_of_one_step_share_the_limit_equally_and_none_is_shown_less_than_the_smallest_limit(
    returns: list[str], limit: int, keep: Keep
) -> None:
    reading = Returning(*returns, keeps=keep)
    asked = [{"tool": "read", "arguments": {"part": part}} for part in range(len(returns))]
    model = ScriptedModel([json.dumps({"tools": asked}), finishes()], parallel_tool_calls=True)

    with shown_within(ContextLimits(tool_result=limit)):
        run = run_role(Role("reader", "Find the code.", (reading.tool,), 5), TASK, model)

    step = run.steps[0]
    share = max(limit // len(returns), SMALLEST_LIMIT)
    (written,) = results_shown(model.requests[1])
    shown: list[str] = json.loads(written)
    assert (step.outcome, json.loads(step.result)) == (StepOutcome.RAN_TOOLS, returns)
    assert shown == [bounded(returned, share, keep) for returned in returns]
    # A step that asks for several tools is shown no more than a step that asks for one.
    assert sum(map(len, shown)) <= max(limit, len(returns) * SMALLEST_LIMIT)
