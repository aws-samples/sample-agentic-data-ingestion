#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Opaque, pipeline-bound pagination cursors for the run-history reads (p48 C2).

WHY A CURSOR AND NOT A PAGE NUMBER
----------------------------------
DynamoDB has no offset addressing: "give me page 7" is not expressible, only
"give me the page after this key". ``get_pipeline_executions`` took a ``page``
argument and referenced it nowhere (p48 R2.1), so every request returned page 1.
Task 3.1 put the two arms to the user and **Arm A was chosen**: keyed paging,
exact and O(1) per page, at the cost of the pager becoming next/previous rather
than numbered. The rejected Arm B walked and discarded ``page - 1`` pages
server-side, which makes page N cost N reads — reintroducing the deep-read cost
``RunsByTimeIndex`` was added to remove.

WHAT A CURSOR IS
----------------
The ``LastEvaluatedKey`` of a ``RunsByTimeIndex`` query, plus the page number the
next page will be. Verified against the live service rather than assumed
(the validation account / us-west-2, ``latesttesting-19ca826a``, read-only query): a GSI
query returns

    {"pipeline_id": {"S": ...}, "started_at": {"S": ...}, "run_id": {"S": ...}}

— the index's two key attributes plus the base table's sort key, all strings —
and feeding exactly that back as ``ExclusiveStartKey`` returns the next two rows
in ``started_at`` order with no overlap. That round trip is what R1.3 rests on.

**The page number travels INSIDE the cursor, not beside it.** It is a label, not
a seek input, and the cursor is the only thing that can say which page it
addresses. Sourcing the label from the cursor makes ``page`` and the rows
returned impossible to disagree; sourcing it from a separate query-string
parameter would let a client ask for row-page 2 labelled page 9.

WHY THE VALIDATION IS THE POINT, NOT THE ENCODING
-------------------------------------------------
base64url makes the cursor URL-safe and unhelpful to read. It makes it **not**
tamper-proof: anyone can decode, edit and re-encode. So opacity is cosmetic and
the security property has to come from checking, which is what
:func:`decode_cursor` does — an **allowlist** of attribute names, a type check on
every value, and an equality check binding the cursor to the ``pipeline_id`` the
caller asked for on the path.

*Rejected: signing the cursor with an HMAC.* It would detect tampering, at the
cost of a secret to provision, rotate and share across every reader. It buys no
privilege, because a cursor names a **position inside a partition the caller is
already authorised to read in full** — the route sits behind the Cognito
authorizer and the partition is chosen by the path, not by the cursor. Forging a
position is not an escalation; forging a *partition* is, and that is what the
equality check refuses. Revisit if a cursor ever carries something the caller
could not otherwise obtain (a filter the server applied, a count, an identity).

*Rejected: trusting DynamoDB to reject a cross-partition key.* It does — a cursor
from another pipeline fails with ``ValidationException: The provided starting key
is outside query boundaries based on provided conditions``, confirmed live. But
that exception lands in the handler's outer ``except``, which returns **200 with
empty lists**: run history would read as "this pipeline has no runs". Refusing at
the boundary turns the same input into a 400. The service check stays as a second
line of defence; it is not the first.

*Rejected: passing the decoded dict straight through as ``ExclusiveStartKey``.*
The key is rebuilt from the allowlist, so an attribute the codec does not name
cannot reach the query even if it survives JSON decoding.

WHY THIS LIVES HERE AND NOT IN ``shared/``
------------------------------------------
``shared/`` is bundled into the Lambda **layer**, so a module there reads as a
cross-Lambda contract. Nothing outside the dashboard API pages this table by time.
The readers that will want this codec — task 5.2's run-detail neighbour query, task
5.6's previewable-run resolver — are handlers in *this* Lambda, in
``handlers/pipelines_runs.py`` and ``handlers/data_preview.py``, so a module in
``handlers/`` reaches all of them without promising a contract to anything else.

*Rejected: keeping it inside ``handlers/pipelines_runs.py``.* ``data_preview.py``
would then import from another handler module for a pure utility, which is a
dependency between two request handlers rather than between a handler and a helper.

Deliberately importing **nothing** from ``.common``: that module calls
``require_env`` at import time, so touching it would drag the whole table-name
environment into every importer of this codec. Asserted in
``tests/unit/lambdas/dashboard_api/test_run_cursor.py``. Revisit the placement if a
second Lambda ever needs a cursor, at which point ``shared/`` is the right home.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

#: The ONLY attribute names that may travel in a cursor, and the only ones that
#: are rebuilt out of one. This is the allowlist, so it is a security control and
#: not a convenience: an attribute absent from this tuple cannot reach
#: ``ExclusiveStartKey`` however the cursor was constructed.
#:
#: Deliberately tolerant about which of the three a given key carries. The set is
#: whatever DynamoDB reports in ``LastEvaluatedKey`` for the index being read, and
#: hardcoding "exactly these three, always" would be a second copy of the key
#: schema that a projection change could silently falsify. ``pipeline_id`` is the
#: one that is genuinely required, because it is the binding.
_CURSOR_KEY_NAMES: tuple[str, ...] = ("pipeline_id", "started_at", "run_id")

#: The page label the cursor addresses. Never a seek input — see the module
#: docstring.
_PAGE_FIELD = "page"

#: The first page a cursor can possibly address. Page 1 is reached by sending no
#: cursor at all, so a cursor claiming to be page 1 did not come from here.
_FIRST_CURSOR_PAGE = 2

#: Longest encoded cursor accepted, checked BEFORE decoding so a large
#: query-string value is never base64-expanded into memory. A real cursor over this
#: index measures **199** characters (the three key attributes as JSON, base64url
#: encoded — measured, not estimated), so this is roughly 5x headroom rather than a
#: tight fit.
MAX_CURSOR_CHARS = 1024


class InvalidCursorError(ValueError):
    """A client-supplied cursor is malformed, or was issued for another pipeline.

    A **bad request**, not a read failure, and the distinction decides the status
    code the handler returns. Its messages are fixed strings and never echo the
    submitted cursor, so a rejected cursor cannot inject content into the log or
    into the response body.
    """


def encode_cursor(
    last_evaluated_key: Mapping[str, Any] | None,
    *,
    pipeline_id: str,
    page: int,
) -> str | None:
    """The cursor addressing the page after ``last_evaluated_key``, or ``None``.

    ``None`` means "there is no next page", which is exactly what DynamoDB means
    by omitting ``LastEvaluatedKey``. The handler derives ``has_next`` from this
    return value rather than from the raw response, so the two cannot drift:
    "there is a next page" and "here is how to ask for it" become one fact.

    Returns ``None`` rather than raising when the key does not name the partition
    that was queried. A cursor is a promise about where the next read starts, and
    minting one for a different partition would be a promise this module's own
    :func:`decode_cursor` would then refuse — better to admit there is no usable
    next page than to hand out a cursor that cannot be redeemed.
    """
    if not last_evaluated_key:
        return None
    key = {
        name: value for name in _CURSOR_KEY_NAMES if isinstance(value := last_evaluated_key.get(name), str) and value
    }
    if key.get("pipeline_id") != pipeline_id:
        return None
    payload: dict[str, Any] = dict(key)
    payload[_PAGE_FIELD] = page
    # ``sort_keys`` so the same key always encodes to the same string: a cursor is
    # compared for equality in tests and appears in URLs, and a dict-order-dependent
    # encoding would make both quietly unstable.
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    # Unpadded base64url: ``=`` is legal in a query string but is percent-encoded by
    # some clients and not others, so dropping it removes a whole class of
    # round-trip difference. :func:`decode_cursor` re-pads.
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: str, *, pipeline_id: str) -> tuple[dict[str, str], int]:
    """Validate a client-supplied cursor into ``(ExclusiveStartKey, page)``.

    Raises :class:`InvalidCursorError` for anything that is not a cursor this
    module minted for **this** pipeline. Every check below is one an attacker
    would otherwise be choosing for us:

    * a length bound, applied before decoding;
    * strict base64 (``validate=True``), because the permissive default silently
      discards non-alphabet characters and would decode ``"!!!!"`` to nothing;
    * a JSON object at the top level, not a list, string or number;
    * **no** attribute name outside the allowlist, so nothing unexpected can be
      carried along even inertly;
    * every value a ``str``, because ``ExclusiveStartKey`` values are typed and a
      number or nested object would reach the SDK's serializer;
    * ``pipeline_id`` equal to the path's — the binding that makes a cursor from
      one pipeline useless against another;
    * a ``page`` that is an ``int`` (not a ``bool``, which ``isinstance`` would
      otherwise accept) of at least 2.

    A **stale** cursor is deliberately not an error here and needs no handling:
    DynamoDB accepts an ``ExclusiveStartKey`` naming a row that has since been
    deleted and simply starts after that position in the key space. So a run
    ageing out between two page requests degrades to "the next page starts where
    it would have" rather than to a failure.
    """
    if not isinstance(cursor, str) or not cursor:
        raise InvalidCursorError("cursor is empty")
    if len(cursor) > MAX_CURSOR_CHARS:
        raise InvalidCursorError("cursor is too long")
    padded = cursor + "=" * (-len(cursor) % 4)
    try:
        raw = base64.b64decode(padded.encode("ascii"), altchars=b"-_", validate=True)
        payload = json.loads(raw.decode("utf-8"))
    except (ValueError, TypeError) as decode_error:
        raise InvalidCursorError("cursor is not a valid pagination cursor") from decode_error
    if not isinstance(payload, dict):
        raise InvalidCursorError("cursor does not describe a page position")
    if set(payload) - set(_CURSOR_KEY_NAMES) - {_PAGE_FIELD}:
        raise InvalidCursorError("cursor carries unrecognized fields")
    key: dict[str, str] = {}
    for name in _CURSOR_KEY_NAMES:
        if name not in payload:
            continue
        value = payload[name]
        if not isinstance(value, str) or not value:
            raise InvalidCursorError("cursor carries a non-string key attribute")
        key[name] = value
    if key.get("pipeline_id") != pipeline_id:
        raise InvalidCursorError("cursor was issued for a different pipeline")
    page = payload.get(_PAGE_FIELD)
    if not isinstance(page, int) or isinstance(page, bool) or page < _FIRST_CURSOR_PAGE:
        raise InvalidCursorError("cursor does not name a page after the first")
    return key, page
