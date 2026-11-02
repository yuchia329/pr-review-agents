"""What a durable run sends to Temporal: pickled, compressed, and read back only as the project's
own types."""

import asyncio
import random
from pathlib import Path

import pytest
from temporalio.api.common.v1 import Payload

from pr_verification.budget import Budget, BudgetSpent, LedgerState
from pr_verification.durable.codec import (
    ENCODING,
    RefusedPayload,
    data_converter,
    decode,
    encode,
    encoded_size,
)
from pr_verification.durable.journal import PAYLOAD_BYTES, Answer, Effect, EffectKind
from pr_verification.model import ModelFailed, ModelRequest, RoleName
from pr_verification.report import BudgetLimit, BudgetSpend
from pr_verification.sandbox import LimitHit, Limits, Phase, RunResult


class Foreign:
    """A class of no module of the project."""

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Foreign)

    def __hash__(self) -> int:
        return 0


VALUES: list[object] = [
    Effect(EffectKind.MODEL, (RoleName.REPRODUCTION_WRITER, ModelRequest("Write it.", "data"))),
    Effect(
        EffectKind.SANDBOX_RUN,
        (
            "pr-verification-test-0",
            Path("/a/repository"),
            "python:3.12",
            ("python", "-m", "pytest"),
            "0" * 40,
            Phase.TEST,
            Limits(seconds=600.0, memory_bytes=2**31, processors=2.0),
        ),
    ),
    Answer(RunResult("1 passed", 0, None), seconds=2.5),
    Answer(RunResult("killed", None, LimitHit.MEMORY)),
    Answer(b"\x00git output\xff"),
    LedgerState(
        Budget(),
        BudgetSpend(1.0, 0.5, 3),
        BudgetLimit.STEPS,
        (Budget(),),
        (BudgetSpend(0.0, 0.0, 0),),
    ),
    (1, "two", None, 3.5, frozenset({"a"}), {"key": [1, 2]}),
]


@pytest.mark.parametrize("value", VALUES, ids=lambda value: type(value).__name__)
def test_value_of_the_project_reads_back_equal(value: object) -> None:
    assert decode(encode(value)) == value


def test_exception_a_seam_raised_reads_back_as_the_same_exception() -> None:
    answers = [
        Answer(raised=ModelFailed("unreachable")),
        Answer(raised=BudgetSpent(BudgetLimit.TIME, 2)),
    ]

    for answer in answers:
        read = decode(encode(answer))
        assert isinstance(read, Answer)
        assert type(read.raised) is type(answer.raised)
        assert str(read.raised) == str(answer.raised)


def test_class_of_no_module_of_the_project_is_refused_unless_allowed() -> None:
    written = encode([Foreign()])

    with pytest.raises(RefusedPayload):
        decode(written)
    assert decode(written, allowed=("test_durable_codec",)) == [Foreign()]


def test_payload_that_encode_did_not_write_is_refused() -> None:
    with pytest.raises(RefusedPayload):
        decode(b"not anything encode wrote")


def test_size_of_a_value_is_the_length_it_is_written_in() -> None:
    for value in VALUES:
        assert encoded_size(value) == len(encode(value))


def test_request_as_long_as_the_longest_writer_request_is_kept_well_under_the_payload_limit() -> (
    None
):
    lines = [
        f"    assert total(items[{n}]) == {n * 3}  # line {n} of the module under test\n"
        for n in range(4000)
    ]
    random.Random(0).shuffle(lines)
    data = "".join(lines)[:252_000]
    request = Effect(EffectKind.MODEL, (None, ModelRequest("Write a Reproduction.", data)))

    assert encoded_size(request) < min(len(data), PAYLOAD_BYTES) / 2


def test_converter_writes_each_value_as_encode_does_and_marks_it() -> None:
    value = VALUES[0]

    async def round_trip() -> tuple[list[Payload], list[object]]:
        converter = data_converter()
        written = await converter.encode([value])
        return written, await converter.decode(written)

    written, read = asyncio.run(round_trip())

    assert written[0].metadata["encoding"] == ENCODING.encode()
    assert decode(written[0].data) == value
    assert read == [value]


def test_converter_refuses_a_payload_it_did_not_write() -> None:
    other = Payload(metadata={"encoding": b"json/plain"}, data=b'"text"')

    with pytest.raises(RefusedPayload):
        asyncio.run(data_converter().decode([other]))


def test_converter_takes_the_classes_of_the_modules_it_is_allowed() -> None:
    async def round_trip(allowed: tuple[str, ...]) -> list[object]:
        converter = data_converter(allowed=allowed)
        return await converter.decode(await converter.encode([Foreign()]))

    assert asyncio.run(round_trip(("test_durable_codec",))) == [Foreign()]
    with pytest.raises(RefusedPayload):
        asyncio.run(round_trip(()))
