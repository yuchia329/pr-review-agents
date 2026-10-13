# The bound on what a role is shown

A role's context holds its task and what it reads. The step cap and the cap on attempts limit
how many things a role reads. Two more limits, enforced by code, are on how big each one may be
when it is shown to a model.

| Variable | Meaning | When unset |
| --- | --- | --- |
| `PR_VERIFICATION_TOOL_RESULT_LIMIT` | The most characters a role is shown of one result of a tool | 20000 |
| `PR_VERIFICATION_SANDBOX_OUTPUT_LIMIT` | The most characters the Reproduction writer is shown of the output of one run in the Sandbox from an earlier attempt | 8000 |

Each is a whole number, 200 or more. A limit that is not exits with code 2 and no report. A
variable set to an empty string counts as unset.

A text within its limit is shown whole. A longer one is cut by counting characters, and the cut
says so and says how much is missing:

- a tool's result keeps its start and ends with
  `[Cut here: the last 61261 of 81200 characters are left out.]`;
- the output of a run keeps its end, where a test run says what went wrong, and starts with
  `[Cut here: the first 4161 of 12100 characters are left out.]`.

Only what a model is shown is cut. The record of a run, the Evidence, and the report keep every
text whole, and repeat detection compares the whole.

A role reads a file too long to show whole in parts: `read_file` takes a `first_line` and a
`last_line`, and each line it returns keeps its own number in the file.

Limits are counted in characters, not in tokens, so how much of a model's window a request
takes depends on the model and on the text.

## The largest request a role can make

A role's request holds its instructions, its task, and every step so far. With the step cap at
12, as it is for the locator, the Structural verifier, the reviewer, and the Reproduction
writer, the last request of a run holds 11 results. So with the limits above, the results of
one request are bounded by:

| Role | What its request can hold of what it read | Characters |
| --- | --- | --- |
| Locator, Structural verifier, reviewer | 11 results of a tool | 220,000 |
| Reproduction writer, third attempt | 11 results of a tool, and two runs of each of two earlier attempts | 252,000 |

The instructions, the task, and the model's own responses come on top, and are not bounded
here. For the writer the task also holds its earlier Reproductions, of up to 20,000 characters
each.

Before there was a bound, none of this had a limit: one `read_file` of a long file, or one
`search` for a common text, could be larger than any window by itself.

## Measuring

`tools/measure_context_bound.py` runs the development part of the hand-made cases and of the
Structural cases within the limits the environment configures, and prints the largest request
sent to the model with what the harness scored:

```sh
uv run --extra laya python tools/measure_context_bound.py --clones ~/.cache/pr-verification
```

Run it once with the limits to be measured, and once with both set to 1000000000, which is how
a run went before there was a bound.
