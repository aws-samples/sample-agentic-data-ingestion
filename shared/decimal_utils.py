# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Float↔Decimal converters — the single shared copy (P28 R19.2).

DynamoDB's resource client rejects Python ``float`` values (numeric
attributes must be ``Decimal``), while ``json.dumps`` rejects ``Decimal``.
Every Lambda and container agent that writes state items or serializes
DynamoDB reads needs the same two recursive walks, and before this module
the codebase carried them as local copies (``convert_floats_to_decimal``,
``convert_floats_to_decimals``, ``_convert_floats``, ``_to_decimal``,
``convert_decimals_to_float``, ``convert_decimals_for_json``). This module
replaces all of them.

Conversion semantics (preserved exactly from the replaced copies):

* ``float`` → ``Decimal(str(f))`` — string round-trip, so ``0.1`` becomes
  ``Decimal("0.1")`` and not the binary-float artifact
  ``Decimal(0.1) == Decimal('0.1000000000000000055511151231257827…')``.
* ``dict`` → values converted recursively; keys untouched.
* ``list`` → items converted recursively.
* Everything else passes through unchanged. Notably:

  - ``bool`` is NOT converted (``bool`` subclasses ``int``, not ``float``)
    — DynamoDB stores booleans natively.
  - ``tuple``/``set`` pass through unchanged, exactly like every replaced
    copy did (DynamoDB's serializer handles sets natively; tuples were
    never fed to these converters).

Reconciliation note: the two ``convert_decimals_for_json`` copies in
``eventbridge_publisher`` detected Decimals by class-name duck-typing
(``obj.__class__.__name__ == "Decimal"``); this module uses
``isinstance(obj, Decimal)``, which is exact for the stdlib ``Decimal``
those call sites actually receive (values read from DynamoDB).

Layer 0 in ``shared/__init__._LAYER_MAP`` — no shared-module dependencies.
"""

from decimal import Decimal
from typing import Any


def convert_floats_to_decimal(obj: Any) -> Any:
    """Recursively convert ``float`` values to ``Decimal`` for DynamoDB writes.

    Args:
        obj: Any value — scalars pass through, dicts/lists are walked.

    Returns:
        A copy of ``obj`` with every ``float`` replaced by
        ``Decimal(str(float))``; all other values (including ``bool``,
        ``int``, ``str``, ``None``) unchanged.
    """
    if isinstance(obj, float):
        return Decimal(str(obj))
    if isinstance(obj, dict):
        return {k: convert_floats_to_decimal(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_floats_to_decimal(item) for item in obj]
    return obj


def convert_decimals_to_float(obj: Any) -> Any:
    """Recursively convert ``Decimal`` values to ``float`` for JSON output.

    The reverse of :func:`convert_floats_to_decimal`, used when DynamoDB
    reads (which produce ``Decimal``) must be serialized with
    ``json.dumps`` or published to EventBridge.

    Args:
        obj: Any value — scalars pass through, dicts/lists are walked.

    Returns:
        A copy of ``obj`` with every ``Decimal`` replaced by ``float``;
        all other values unchanged.
    """
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, dict):
        return {k: convert_decimals_to_float(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_decimals_to_float(item) for item in obj]
    return obj
