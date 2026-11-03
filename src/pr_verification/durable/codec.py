"""How what a durable run sends to Temporal is written into its history.

Every value that crosses, a workflow's input and output and an activity's, is pickled, so that
the project's own types cross as they are, and then compressed, since a history keeps all of
it and a request to a model can hold about 252,000 characters.

A history is read back by workers only. Even so, unpickling takes nothing but the project's own
classes and the standard types they are made of: a payload that names any other class is
refused, so a history that someone changed cannot make a worker run code of their choosing.
"""

import io
import pickle
import zlib
from collections.abc import Collection, Sequence
from typing import Any

from temporalio.api.common.v1 import Payload
from temporalio.converter import (
    CompositePayloadConverter,
    DataConverter,
    EncodingPayloadConverter,
)

ENCODING = "binary/x-pr-verification-pickle"
"""The encoding a payload written here is marked with."""

_PROTOCOL = 5
"""The pickle protocol every payload is written with."""

_STANDARD = {
    "builtins": None,
    "pathlib": None,
    "enum": None,
    "collections": None,
    "copyreg": {"_reconstructor"},
    "subprocess": {"CalledProcessError", "TimeoutExpired"},
}
"""The modules of the standard library whose classes unpickling takes, each with the only names
taken from it, or None for every class it holds. Of `builtins`, only types are taken."""


class RefusedPayload(Exception):  # noqa: N818 - named for what it is
    """A payload that is not one written here, or that names a class unpickling does not
    take."""


def encode(value: object) -> bytes:
    """`value` as it is kept in a history: pickled, then compressed.

    Raises `TypeError` when `value` cannot be pickled.
    """
    try:
        pickled = pickle.dumps(value, protocol=_PROTOCOL)
    except (pickle.PicklingError, AttributeError) as error:
        raise TypeError(f"cannot be kept in a history: {error}") from error
    return zlib.compress(pickled, 6)


def decode(data: bytes, *, allowed: Collection[str] = ()) -> object:
    """The value that `encode` wrote as `data`, equal to the one given it.

    Unpickling takes only the classes of the modules under `pr_verification`, those of the
    modules `allowed` names and the modules under them, and those of the standard library that
    the project's values are made of: the built-in types, `pathlib` paths, `enum`,
    `collections`, and the exceptions the seams raise. Raises `RefusedPayload` when `data`
    names any other, or is not something `encode` wrote.

    `allowed` is for a test whose stand-ins cross to Temporal. A worker of a real run gives
    none.
    """
    try:
        pickled = zlib.decompress(data)
    except zlib.error as error:
        raise RefusedPayload(f"not a payload of a durable run: {error}") from error
    try:
        return _Unpickler(io.BytesIO(pickled), ("pr_verification", *allowed)).load()
    except RefusedPayload:
        raise
    except Exception as error:
        raise RefusedPayload(f"not a payload of a durable run: {error}") from error


def encoded_size(value: object) -> int:
    """How many bytes `value` takes in a history: `len(encode(value))`. What the limits of
    `DurableLimits` are held against."""
    return len(encode(value))


def data_converter(*, allowed: Collection[str] = ()) -> DataConverter:
    """The data converter of every client and worker of a durable run: each value is written
    as `encode` writes it, marked with `ENCODING`, and read back as `decode` reads it with
    `allowed`.

    A payload marked otherwise is refused, with `RefusedPayload`.
    """
    taken = tuple(allowed)

    class _Converter(CompositePayloadConverter):
        def __init__(self) -> None:
            super().__init__(_Pickled(taken))

        def from_payloads(
            self, payloads: Sequence[Payload], type_hints: list[type] | None = None
        ) -> list[Any]:
            for payload in payloads:
                if payload.metadata.get("encoding") != ENCODING.encode():
                    raise RefusedPayload(
                        f"a payload marked {payload.metadata.get('encoding')!r}, not {ENCODING}"
                    )
            return super().from_payloads(payloads, type_hints)

    return DataConverter(payload_converter_class=_Converter)


class _Pickled(EncodingPayloadConverter):
    """Writes every value as `encode` does, and reads it back as `decode` does."""

    def __init__(self, allowed: tuple[str, ...]) -> None:
        self._allowed = allowed

    @property
    def encoding(self) -> str:
        return ENCODING

    def to_payload(self, value: Any) -> Payload | None:
        return Payload(metadata={"encoding": ENCODING.encode()}, data=encode(value))

    def from_payload(self, payload: Payload, type_hint: type | None = None) -> Any:
        return decode(payload.data, allowed=self._allowed)


class _Unpickler(pickle.Unpickler):
    """Takes only the classes `decode` says it takes."""

    def __init__(self, file: io.BytesIO, modules: tuple[str, ...]) -> None:
        super().__init__(file)
        self._modules = modules

    def find_class(self, module: str, name: str) -> Any:
        if any(module == taken or module.startswith(f"{taken}.") for taken in self._modules):
            return super().find_class(module, name)
        if module in _STANDARD:
            names = _STANDARD[module]
            if names is None or name in names:
                found = super().find_class(module, name)
                if module != "builtins" or isinstance(found, type):
                    return found
        raise RefusedPayload(f"a payload of a durable run names {module}.{name}")
