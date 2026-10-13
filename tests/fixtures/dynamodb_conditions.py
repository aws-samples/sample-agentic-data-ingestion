# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Structural comparison of boto3 DynamoDB condition expressions.

Why this module exists — the trap it removes
--------------------------------------------
``ConditionBase.get_expression()`` does **not** render a condition to a string. It
returns ``{"format": ..., "operator": ..., "values": (...)}``, and ``values`` holds
further ``ConditionBase`` / ``Attr`` / ``Key`` **objects** whose ``repr`` is the
default ``<boto3.dynamodb.conditions.Equals object at 0x10a…>``. So::

    repr(condition.get_expression())

carries the top-level operator and nothing else: no attribute names, no compared
literals, no nested operators. An assertion of the form ``assert "QUEUED" in
repr(cond.get_expression())`` therefore **cannot pass however correct the condition
is** — and a substring assertion that does happen to pass proves only that boto3's
own class names contain the substring, which is not a statement about the query.

Two tests were written that way and both failed against correct code (p54 gate:
``test_run_history_ordering.py`` on the windowless count filter and
``test_lost_file_durable_visibility.py`` on the keyed lost-file read). Compare
**structure** instead. :func:`flatten_condition` turns a whole condition tree into
one hashable value carrying every operator, attribute name and literal, so an
expected condition written in the ordinary ``Attr``/``Key`` vocabulary can be
compared with ``==``:

    >>> from boto3.dynamodb.conditions import Attr
    >>> actual = Attr("status").not_exists() | Attr("status").ne("QUEUED")
    >>> flatten_condition(actual) == flatten_condition(
    ...     Attr("status").ne("QUEUED") | Attr("status").not_exists()
    ... )
    True

``AND`` and ``OR`` operands are flattened into a ``frozenset``, as above, because
those operators are commutative in DynamoDB and an order-sensitive comparison would
fail a rewrite that changed nothing. Every other operator keeps operand order,
because ``BETWEEN`` and ``begins_with`` are not commutative.

boto3 conditions also implement ``__eq__`` directly, so ``actual == expected`` works
for an exact-shape comparison; the flattened form is preferred in assertions because
it is order-insensitive where that is correct and, unlike a bare condition object, it
prints legibly in a failure message.

Limitation, stated rather than worked around: ``Attr("x").size()`` collapses to the
bare attribute path ``x``, so a size condition is not distinguishable from a
comparison on the attribute itself. No caller needs that today; extend
:func:`_flatten_operand` if one does.
"""

from __future__ import annotations

from typing import Any, NamedTuple

from boto3.dynamodb.conditions import AttributeBase, ConditionBase

#: Operators whose operands are order-insensitive, so a rewrite that swaps the two
#: sides of a conjunction is the same query and must not fail an assertion.
_COMMUTATIVE_OPERATORS = frozenset({"AND", "OR"})


class AttributePath(NamedTuple):
    """One ``Attr``/``Key`` operand, reduced to the attribute name it addresses."""

    name: str

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return f"attr({self.name!r})"


def flatten_condition(condition: ConditionBase) -> tuple[str, Any]:
    """Reduce a boto3 condition tree to a hashable, comparable, printable value.

    Returns ``(operator, operands)``. ``operands`` is a ``frozenset`` for the
    commutative operators (``AND`` / ``OR``) and a ``tuple`` otherwise. Each operand
    is either a nested ``(operator, operands)`` node, an :class:`AttributePath`, or
    the compared literal itself.

    Use this instead of ``repr(condition.get_expression())`` — see the module
    docstring for why that cannot work.
    """
    expression = condition.get_expression()
    operator = expression["operator"]
    operands = tuple(_flatten_operand(value) for value in expression["values"])
    if operator in _COMMUTATIVE_OPERATORS:
        return operator, frozenset(operands)
    return operator, operands


def condition_literals(condition: ConditionBase) -> tuple[Any, ...]:
    """Every compared literal in the tree, breadth-first, attribute paths excluded.

    For asserting that a particular value reached the query — the assertion
    ``"queued-" in repr(...)`` was reaching for and could never make.
    """
    literals: list[Any] = []
    pending: list[ConditionBase] = [condition]
    while pending:
        expression = pending.pop(0).get_expression()
        for value in expression["values"]:
            if isinstance(value, ConditionBase):
                pending.append(value)
            elif isinstance(value, AttributeBase):
                continue
            else:
                literals.append(value)
    return tuple(literals)


def _flatten_operand(value: Any) -> Any:
    """One operand of one condition: nested node, attribute path, or literal."""
    if isinstance(value, ConditionBase):
        return flatten_condition(value)
    if isinstance(value, AttributeBase):
        return AttributePath(value.name)
    if isinstance(value, (list, tuple)):
        # ``is_in`` / ``between`` carry a sequence of literals; make it hashable so
        # the flattened tree can live in the frozenset a commutative parent builds.
        return tuple(_flatten_operand(item) for item in value)
    return value
