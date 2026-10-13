# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Typed accessors for DynamoDB item values (P22 Req 2).

Values read out of a DynamoDB item (via ``boto3`` resource ``Table`` operations)
are typed by ``boto3-stubs`` as ``AttributeValueTypeDef`` — a wide union
(``bytes | bytearray | str | int | Decimal | set[...] | Sequence[Any] |
Mapping[str, Any] | None | bool``). Calling ``.get()`` / subscripting / doing
arithmetic on such a value makes mypy emit one error per union member (~9×), even
though at runtime the code already knows the value is a dict / str / int / list.

These helpers narrow a single item value to a concrete type with a runtime-safe
``isinstance`` check, returning a typed default when the key is absent or the value
is the wrong type. They are **behavior-preserving** relative to the common
``item.get(key, default)`` idiom: present-and-correct-type returns the value;
absent or wrong-type returns the default; nothing raises.

This module sits at dependency layer 0 (standard library + ``typing`` only) so it is
importable by any Lambda via the Layer_Import_Fallback (``/opt/python`` in the Lambda
layer, or the local ``shared/`` directory in test/local execution).

Example:
    >>> from dynamo_typing import item_dict, item_str
    >>> meta = item_dict(pipeline_item, "glue_job_metadata")   # -> dict[str, Any]
    >>> name = item_str(meta, "glue_job_name")                 # -> str (clean .get chain)
"""

from collections.abc import Mapping
from typing import Any


def item_dict(item: Mapping[str, Any], key: str, default: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return ``item[key]`` as a ``dict[str, Any]``.

    Returns ``default`` (or ``{}`` if ``default`` is ``None``) when the key is
    absent or the value is not a mapping. Never raises.
    """
    value = item.get(key)
    if isinstance(value, dict):
        return value
    return {} if default is None else default


def item_str(item: Mapping[str, Any], key: str, default: str = "") -> str:
    """Return ``item[key]`` as a ``str``.

    Returns ``default`` when the key is absent or the value is not a string. Never
    raises. (Note: numbers are NOT coerced to strings — use ``item_int`` for those.)
    """
    value = item.get(key)
    if isinstance(value, str):
        return value
    return default


def item_int(item: Mapping[str, Any], key: str, default: int = 0) -> int:
    """Return ``item[key]`` as an ``int``.

    DynamoDB numbers commonly arrive as ``Decimal``; this coerces ``Decimal``/``int``
    (and numeric strings) to ``int``. Returns ``default`` when the key is absent or
    the value cannot be interpreted as an integer. ``bool`` is intentionally treated
    as a non-int (DynamoDB stores booleans separately). Never raises.
    """
    value = item.get(key)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    # Decimal and numeric strings: coerce via int(...) but stay total.
    try:
        if isinstance(value, str):
            return int(value.strip())
        # Decimal, float, and other numerics support int(...)
        return int(value)  # type: ignore[arg-type]  # guarded by try/except below
    except (TypeError, ValueError):
        return default


def item_list(item: Mapping[str, Any], key: str, default: list[Any] | None = None) -> list[Any]:
    """Return ``item[key]`` as a ``list[Any]``.

    Returns ``default`` (or ``[]`` if ``default`` is ``None``) when the key is absent
    or the value is not a list. Never raises.
    """
    value = item.get(key)
    if isinstance(value, list):
        return value
    return [] if default is None else default
