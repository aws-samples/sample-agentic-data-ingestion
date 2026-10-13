# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared helpers for API-response contract tests: the frontend-type oracle.

The contract tests in this directory pin a handler's wire format against **the
frontend TypeScript interface that consumes it**, parsed at test time. That is
the ratified pattern from ``LGR-28b4d75-5``: shape drift between a producer and
its consumer fails *silently*, and a contract test written from the producer's
own output cannot detect the producer drifting, because that output is its own
oracle.

This module holds the small TypeScript reader the pattern needs. It was lifted
out of ``test_cost_recommendations_contract.py`` (where it was module-private)
when a second endpoint needed it, rather than copied: the copies would have
diverged immediately, because ``/pipelines/{id}/costs`` declares
``most_expensive_run: PipelineCostRun | null`` and the ``/cost-recommendations``
copy had no nullable branch — it raised ``Unmapped TypeScript type``. One parser
means the next contract test starts from the version that handles unions.

Deliberately small, and strict on purpose. It understands the one construct the
interfaces it reads are written in: a flat top-level ``interface`` (``export``
optional) of ``field[?]: <type>;`` lines, one per line. Unparsable lines and unmapped types
**raise** rather than being skipped, because a silently-empty or half-parsed
field set makes every assertion built on it trivially true — a contract test that
passes because it checked nothing is worse than no contract test.

Two guards worth knowing about when using this:

* ``json_types`` will not guess at a bare identifier. A named interface such as
  ``PipelineCostRun`` must be passed in ``object_types``, so a typo or a newly
  introduced named type fails loudly instead of being accepted as "some object".
* ``matches_json_type`` rejects a Python ``bool`` for a field declared
  ``number``. ``bool`` is an ``int`` subclass, so a plain ``isinstance`` check
  would let ``True`` satisfy a numeric field.

Usage::

    from .helpers import load_interfaces, matches_json_type, required_fields

    types = load_interfaces(FRONTEND_TYPE_FILE, "PipelineCosts", "PipelineCostRun")
    missing = required_fields(types["PipelineCosts"]) - set(body)
"""

from __future__ import annotations

import re
from pathlib import Path

#: TypeScript spellings for "no value". ``null`` is what a JSON body can carry;
#: ``undefined`` cannot survive ``JSON.stringify``, but a hand-written interface
#: may still declare it, so both map to ``NoneType`` rather than raising.
_NULLISH = frozenset({"null", "undefined"})

#: TypeScript spellings for "any JSON value". A field declared either way
#: constrains nothing, so ``json_types`` maps both to every Python type a parsed
#: JSON body can hold rather than raising.
_ANY_JSON = frozenset({"unknown", "any"})

#: One interface's fields: ``{field_name: (is_required, ts_type)}``.
Interface = dict[str, tuple[bool, str]]


def read_interface(source: str, name: str, source_path: Path | str = "<source>") -> Interface:
    """Return ``{field: (required, ts_type)}`` for one top-level ``interface``.

    ``export`` is optional. A module-private interface is still a consumer
    contract: ``use-chat-agent.ts`` declares ``interface ChatResponse`` without
    exporting it, because only the hook in that file instantiates
    ``apiClient.post<ChatResponse>`` with it — and that is precisely the type
    ``POST /api/chat`` has to satisfy. Requiring ``export`` would have made the
    reader refuse the type and force a second parser, which is what
    ``LGR-99050e6-5`` extracted this module to prevent. The keyword is still
    anchored to the start of a line, so a nested or commented-out declaration
    does not match.

    Args:
        source: Full text of the ``.ts`` file.
        name: Interface name, e.g. ``"PipelineCosts"``.
        source_path: Only used in assertion messages, so a failure names the
            file the expectations were supposed to come from.

    Raises:
        AssertionError: if the interface is absent, or if any line inside it is
            not a ``field[?]: type;`` declaration. Both are failures of the
            oracle itself and must be loud: a missing interface would otherwise
            yield an empty required-field set and pass everything.
    """
    match = re.search(
        r"^(?:export\s+)?interface\s+" + re.escape(name) + r"\s*\{(.*?)^\}",
        source,
        re.DOTALL | re.MULTILINE,
    )
    assert match, f"{name} not found in {source_path} — the type this contract is pinned against"

    fields: Interface = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        field = re.match(r"^(\w+)(\?)?:\s*(.+);$", stripped)
        assert field, f"Unparsed line in {name}: {stripped!r} — teach read_interface about it"
        fields[field.group(1)] = (field.group(2) is None, field.group(3).strip())
    return fields


def load_interfaces(path: Path, *names: str) -> dict[str, Interface]:
    """Read a ``.ts`` file and parse several interfaces out of it.

    Asserts the file exists and that every requested interface parsed to a
    non-empty field set, so a moved hook or a renamed type fails this test
    rather than quietly reducing it to a no-op.
    """
    assert path.is_file(), (
        f"{path} is missing. This contract test derives its expectations from the frontend types in "
        f"that file; if the hook moved, repoint the constant rather than deleting the assertion."
    )
    source = path.read_text(encoding="utf-8")
    parsed = {name: read_interface(source, name, path) for name in names}
    for name, fields in parsed.items():
        assert fields, f"{name} parsed as empty from {path} — every assertion built on it would pass vacuously"
    return parsed


def required_fields(interface: Interface) -> set[str]:
    """The field names an interface declares without ``?``."""
    return {field for field, (required, _) in interface.items() if required}


def union_members(ts_type: str) -> list[str]:
    """Top-level union members of a type, or ``[ts_type]`` if it is not a union.

    A string-literal union (``'a' | 'b'``) is returned as a single member: those
    are handled as a plain ``string`` and splitting them adds nothing.
    """
    ts_type = ts_type.strip()
    if "'" in ts_type or '"' in ts_type:
        return [ts_type]
    return [member.strip() for member in ts_type.split("|")]


def declares_null(ts_type: str) -> bool:
    """Whether the type explicitly admits ``null`` / ``undefined``.

    Used to guard the oracle: a test that pins "the key is present but may be
    ``null``" is only meaningful while the interface still says so, so it can
    assert this rather than trusting that nobody dropped the union to make a
    parser complaint go away.
    """
    return any(member in _NULLISH for member in union_members(ts_type))


def json_types(ts_type: str, object_types: frozenset[str] | set[str] = frozenset()) -> tuple[type, ...]:
    """Map a TypeScript type to the Python types its JSON form may have.

    Args:
        ts_type: The declared type, e.g. ``"number"``, ``"PipelineCostRun[]"``,
            ``"PipelineCostRun | null"``.
        object_types: Names of interfaces in scope that serialize to a JSON
            object. Anything else that looks like a bare identifier raises —
            guessing "identifier means object" would accept a typo.

    Raises:
        AssertionError: on a type this reader does not model.
    """
    ts_type = ts_type.strip()

    # Order matters and mirrors the original single-file reader: an array or an
    # inline object is decided before the union split, so `Foo[]` and
    # `{ a: string; b: number }` never reach it.
    if ts_type.endswith("[]"):
        return (list,)
    # ``Array<T>`` is the same TypeScript type as ``T[]`` and the same JSON
    # shape; the reader already knew one spelling. ``use-chat-agent.ts`` writes
    # ``PipelineProposal.mappings`` in this form. Teaching the parser a real wire
    # shape is the move ``LGR-99050e6-5`` invites — the rejection in that record
    # was of NARROWING the frontend type to silence the parser, which is the
    # opposite direction.
    if ts_type.startswith("Array<") and ts_type.endswith(">"):
        return (list,)
    if ts_type.startswith("{"):
        return (dict,)
    if "'" in ts_type or '"' in ts_type:  # plain or string-literal union
        return (str,)

    members = union_members(ts_type)
    if len(members) > 1:
        mapped: list[type] = []
        for member in members:
            mapped.extend(json_types(member, object_types))
        return tuple(dict.fromkeys(mapped))  # de-duplicate, preserve order

    if ts_type == "string":
        return (str,)
    if ts_type == "number":
        return (int, float)
    if ts_type == "boolean":
        return (bool,)
    if ts_type in _NULLISH:
        return (type(None),)
    if ts_type in object_types:
        return (dict,)
    # ``unknown`` / ``any`` admit every JSON shape, so the mapping is the full
    # set rather than a narrowing. Checked AFTER ``object_types`` so a caller
    # that declared either name as an interface in scope keeps its ``(dict,)``.
    #
    # This is the move ``LGR-99050e6-5`` invites, not the one it rejected. That
    # record (scope ``tests/contracts/api_responses/**``) refused "narrowing the
    # frontend type to make the parser complaint go away", because the declared
    # union is the true wire shape. Teaching the parser a real wire shape is the
    # opposite: the type stays as wide as the producer, and the reader catches
    # up. Rejected here for the same reason: declaring
    # ``UnsourcedTargetField.default_value`` as ``string | number | boolean |
    # null`` to dodge this branch — its producer reads a JSON Schema ``default``,
    # which may be an object or an array, so that would be the narrowing.
    if ts_type in _ANY_JSON:
        return (str, int, float, bool, list, dict, type(None))
    raise AssertionError(
        f"Unmapped TypeScript type {ts_type!r} — extend json_types, or pass it in object_types if it "
        f"is an interface in scope that serializes to a JSON object"
    )


def matches_json_type(
    value: object,
    ts_type: str,
    object_types: frozenset[str] | set[str] = frozenset(),
) -> bool:
    """Whether ``value`` (parsed out of a JSON body) satisfies ``ts_type``.

    A Python ``bool`` is rejected unless ``boolean`` is actually declared:
    ``bool`` subclasses ``int``, so a bare ``isinstance`` check would accept
    ``True`` for a field the frontend renders with ``.toFixed(2)``.
    """
    expected = json_types(ts_type, object_types)
    if isinstance(value, bool) and bool not in expected:
        return False
    return isinstance(value, expected)
