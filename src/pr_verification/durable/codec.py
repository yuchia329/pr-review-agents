"""How what a durable run sends to Temporal is written into its history.

Every value that crosses, a workflow's input and output and an activity's, is pickled, so that
the project's own types cross as they are, and then compressed, since a history keeps all of
it and a request to a model can hold about 252,000 characters.

A history is read back by workers only. Even so, unpickling takes nothing but the project's own
classes and the standard types they are made of: a payload that names any other class is
refused, so a history that someone changed cannot make a worker run code of their choosing.
"""

from temporalio.converter import DataConverter

ENCODING = "binary/x-pr-verification-pickle"
"""The encoding a payload written here is marked with."""


class RefusedPayload(Exception):  # noqa: N818 - named for what it is
    """A payload that is not one written here, or that names a class unpickling does not
    take."""


def encode(value: object) -> bytes:
    """`value` as it is kept in a history: pickled, then compressed.

    Raises `TypeError` when `value` cannot be pickled.
    """
    raise NotImplementedError


def decode(data: bytes) -> object:
    """The value that `encode` wrote as `data`, equal to the one given it.

    Unpickling takes only the classes of the modules under `pr_verification`, and those of the
    standard library that the project's values are made of: the built-in types, `pathlib`
    paths, `enum`, `collections`, and the exceptions the seams raise. Raises `RefusedPayload`
    when `data` names any other, or is not something `encode` wrote.
    """
    raise NotImplementedError


def encoded_size(value: object) -> int:
    """How many bytes `value` takes in a history: `len(encode(value))`. What the limits of
    `DurableLimits` are held against."""
    raise NotImplementedError


def data_converter() -> DataConverter:
    """The data converter of every client and worker of a durable run: each value is written
    as `encode` writes it, marked with `ENCODING`, and read back as `decode` reads it.

    A payload marked otherwise is refused, with `RefusedPayload`.
    """
    raise NotImplementedError
