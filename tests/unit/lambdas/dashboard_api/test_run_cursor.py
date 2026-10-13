# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The run-history cursor codec, in isolation (p48 C2, task 3.2).

WHY THIS IS A SEPARATE FILE
---------------------------
``handlers/run_cursor.py`` is pure — no table handles, no AWS, no environment — so
its rejection paths are cheap to enumerate here and expensive to reach through the
handler, where each one costs a moto table and a seeded partition.
``test_run_history_ordering.py::TestCursorPaging`` asserts the handler's
*behaviour* (a bad cursor is a 400, not an empty 200); this file asserts *which
inputs are bad and why*, one check per test, so a future relaxation of any single
check fails a test that names it.

THE THREAT MODEL, STATED
------------------------
base64url makes a cursor URL-safe and unhelpful to read. It does **not** make it
tamper-proof: anyone can decode, edit and re-encode. So the security property comes
entirely from :func:`decode_cursor`'s checking, and the checks that matter are:

* an **allowlist** of attribute names, so no client-chosen attribute can reach
  ``ExclusiveStartKey``; and
* an equality check binding the cursor to the ``pipeline_id`` the caller named on
  the **path**, so a cursor minted for one pipeline is useless against another.

Signing was rejected: a cursor names a position inside a partition the caller is
already authorised to read in full, so forging a position is not an escalation.
Forging a *partition* would be, and that is what the binding refuses. See the
module docstring of ``handlers/run_cursor.py`` for the full argument.

Validates: p48 Requirements 2.1
"""

from __future__ import annotations

import base64
import json
import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Path setup. ``handlers/run_cursor.py`` imports nothing from ``handlers.common``,
# so unlike its siblings this file needs no table-name environment at import time —
# which is itself a property worth preserving and is asserted at the bottom.
# ---------------------------------------------------------------------------
_LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
if os.path.abspath(_LAMBDA_DIR) not in sys.path:
    sys.path.insert(0, os.path.abspath(_LAMBDA_DIR))

from handlers.run_cursor import (
    MAX_CURSOR_CHARS,
    InvalidCursorError,
    decode_cursor,
    encode_cursor,
)

PIPELINE_ID = "latesttesting-19ca826a"

#: The ``LastEvaluatedKey`` a ``RunsByTimeIndex`` query actually returns, taken from
#: a read-only query against the validation account / us-west-2 on 2026-08-24 rather than
#: invented: the index's two key attributes plus the base table's sort key, all
#: strings. The codec's contract is with this shape.
LIVE_LAST_EVALUATED_KEY = {
    "run_id": "skipped-7412636fccb938719673179658c05773",
    "started_at": "2026-08-24T13:04:04.864784+00:00",
    "pipeline_id": PIPELINE_ID,
}


def _token(payload: object) -> str:
    """A hand-built token, bypassing :func:`encode_cursor`.

    The point of the file: a test that could only produce well-formed cursors
    cannot exercise a rejection path.
    """
    raw = json.dumps(payload).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


# ===========================================================================
# Encoding
# ===========================================================================
class TestEncode:
    def test_the_live_key_round_trips_to_itself(self):
        """The whole contract, in one assertion, against the observed key shape."""
        cursor = encode_cursor(LIVE_LAST_EVALUATED_KEY, pipeline_id=PIPELINE_ID, page=2)
        assert cursor is not None

        key, page = decode_cursor(cursor, pipeline_id=PIPELINE_ID)

        assert key == LIVE_LAST_EVALUATED_KEY
        assert page == 2

    def test_no_last_evaluated_key_means_no_cursor(self):
        """``None`` is DynamoDB's "there is no next page", passed straight through.

        The handler derives ``has_next`` from this return value, so "there is a next
        page" and "here is how to ask for it" are one fact rather than two that can
        disagree.
        """
        assert encode_cursor(None, pipeline_id=PIPELINE_ID, page=2) is None
        assert encode_cursor({}, pipeline_id=PIPELINE_ID, page=2) is None

    def test_the_encoding_is_url_safe_and_unpadded(self):
        """A cursor goes into a query string, so it must survive one unescaped.

        Unpadded because ``=`` is legal in a query string but percent-encoded by
        some clients and not others — dropping it removes a class of round-trip
        difference rather than relying on every client agreeing.
        """
        cursor = encode_cursor(LIVE_LAST_EVALUATED_KEY, pipeline_id=PIPELINE_ID, page=2)

        assert cursor is not None
        assert "=" not in cursor
        assert "+" not in cursor
        assert "/" not in cursor
        assert set(cursor) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")

    def test_encoding_is_stable_for_the_same_key(self):
        """Same key, same string — ``sort_keys`` rather than dict order.

        A cursor appears in URLs and is compared for equality in tests; an encoding
        that depended on insertion order would make both quietly unstable.
        """
        reordered = {
            "pipeline_id": LIVE_LAST_EVALUATED_KEY["pipeline_id"],
            "run_id": LIVE_LAST_EVALUATED_KEY["run_id"],
            "started_at": LIVE_LAST_EVALUATED_KEY["started_at"],
        }

        assert encode_cursor(LIVE_LAST_EVALUATED_KEY, pipeline_id=PIPELINE_ID, page=2) == encode_cursor(
            reordered, pipeline_id=PIPELINE_ID, page=2
        )

    def test_a_key_for_another_partition_mints_nothing(self):
        """Bound at mint time as well as at redeem time.

        Returning ``None`` rather than raising: a cursor is a promise about where
        the next read starts, and minting one for a different partition would be a
        promise :func:`decode_cursor` would then refuse. Better to report no next
        page than to hand out a token that cannot be redeemed.
        """
        assert encode_cursor(LIVE_LAST_EVALUATED_KEY, pipeline_id="some-other-pipeline", page=2) is None

    def test_only_allowlisted_attributes_survive_encoding(self):
        """An attribute the codec does not name cannot be carried through it.

        Belt and braces with :func:`decode_cursor`'s own allowlist: even a
        ``LastEvaluatedKey`` that somehow grew an extra attribute cannot put it in a
        client's hands, let alone back into a query.
        """
        polluted = dict(LIVE_LAST_EVALUATED_KEY, ttl="9999999999", Limit="9999")

        cursor = encode_cursor(polluted, pipeline_id=PIPELINE_ID, page=2)

        assert cursor is not None
        key, _ = decode_cursor(cursor, pipeline_id=PIPELINE_ID)
        assert key == LIVE_LAST_EVALUATED_KEY

    def test_a_partial_key_is_accepted_because_the_key_schema_is_dynamodb_s_to_state(self):
        """Tolerance about *which* allowlisted names appear, deliberately.

        The set is whatever DynamoDB reports for the index being read. Requiring
        exactly three would be a second copy of the key schema that a projection or
        index change could silently falsify, and the security property does not
        depend on the other two: ``pipeline_id`` is the binding.
        """
        cursor = encode_cursor({"pipeline_id": PIPELINE_ID, "run_id": "run-001"}, pipeline_id=PIPELINE_ID, page=3)

        assert cursor is not None
        assert decode_cursor(cursor, pipeline_id=PIPELINE_ID) == ({"pipeline_id": PIPELINE_ID, "run_id": "run-001"}, 3)


# ===========================================================================
# Decoding — one rejection per test, each naming the check it trips
# ===========================================================================
class TestDecodeRefusals:
    def test_an_empty_cursor(self):
        with pytest.raises(InvalidCursorError, match="empty"):
            decode_cursor("", pipeline_id=PIPELINE_ID)

    def test_a_cursor_longer_than_the_bound(self):
        """Length is checked BEFORE decoding, so a large value is never expanded.

        A real cursor is ~180 characters, so the bound is headroom rather than a
        tight fit; the property being pinned is the *ordering* of the two checks.
        """
        with pytest.raises(InvalidCursorError, match="too long"):
            decode_cursor("A" * (MAX_CURSOR_CHARS + 1), pipeline_id=PIPELINE_ID)

    def test_non_base64_characters(self):
        """Strict base64. The permissive default silently discards non-alphabet
        characters, so ``"!!!!"`` would decode to nothing and read as a valid empty
        payload rather than as garbage.
        """
        with pytest.raises(InvalidCursorError, match="not a valid pagination cursor"):
            decode_cursor("not-base64-at-all!!", pipeline_id=PIPELINE_ID)

    def test_valid_base64_that_is_not_json(self):
        token = base64.urlsafe_b64encode(b"\xff\xfe not json").decode("ascii").rstrip("=")
        with pytest.raises(InvalidCursorError, match="not a valid pagination cursor"):
            decode_cursor(token, pipeline_id=PIPELINE_ID)

    @pytest.mark.parametrize("payload", [[1, 2, 3], "a string", 7, None, True])
    def test_json_that_is_not_an_object(self, payload):
        with pytest.raises(InvalidCursorError, match="does not describe a page position"):
            decode_cursor(_token(payload), pipeline_id=PIPELINE_ID)

    def test_an_attribute_outside_the_allowlist(self):
        """The allowlist is the security control, so it refuses rather than ignores.

        Silently dropping an unrecognized field would accept a cursor a future
        version of this codec might read differently — and would hide a client
        sending something it should not.
        """
        payload = dict(LIVE_LAST_EVALUATED_KEY, page=2, Limit=9999)
        with pytest.raises(InvalidCursorError, match="unrecognized fields"):
            decode_cursor(_token(payload), pipeline_id=PIPELINE_ID)

    @pytest.mark.parametrize("bad_value", [7, None, {"S": "x"}, ["x"], "", True])
    def test_a_key_attribute_that_is_not_a_non_empty_string(self, bad_value):
        """``ExclusiveStartKey`` values are typed; a number or nested object would
        reach the SDK's serializer with a client-chosen shape.
        """
        payload = dict(LIVE_LAST_EVALUATED_KEY, page=2)
        payload["started_at"] = bad_value
        with pytest.raises(InvalidCursorError):
            decode_cursor(_token(payload), pipeline_id=PIPELINE_ID)

    def test_a_cursor_bound_to_a_different_pipeline(self):
        """**The security property.** A cursor cannot reach a partition the caller
        did not name on the path.

        DynamoDB refuses this too — confirmed live: ``ValidationException: The
        provided starting key is outside query boundaries based on provided
        conditions``. But that exception reaches the handler's outer ``except`` and
        becomes 200-with-empty-lists, i.e. "this pipeline has no runs". The service
        check is a second line of defence, not the first.
        """
        cursor = encode_cursor(LIVE_LAST_EVALUATED_KEY, pipeline_id=PIPELINE_ID, page=2)
        assert cursor is not None

        with pytest.raises(InvalidCursorError, match="issued for a different pipeline"):
            decode_cursor(cursor, pipeline_id="some-other-pipeline")

    def test_a_cursor_with_no_pipeline_binding_at_all(self):
        with pytest.raises(InvalidCursorError, match="issued for a different pipeline"):
            decode_cursor(_token({"run_id": "run-001", "page": 2}), pipeline_id=PIPELINE_ID)

    def test_a_cursor_naming_no_page(self):
        with pytest.raises(InvalidCursorError, match="does not name a page after the first"):
            decode_cursor(_token({"pipeline_id": PIPELINE_ID}), pipeline_id=PIPELINE_ID)

    @pytest.mark.parametrize("page", [1, 0, -5, "2", 2.0, True, None])
    def test_a_page_that_is_not_an_int_of_at_least_two(self, page):
        """Page 1 is reached by sending no cursor, so a cursor claiming page 1 did
        not come from here. ``True`` is called out because ``isinstance(True, int)``
        is true and would otherwise pass as page 1.
        """
        payload = dict(LIVE_LAST_EVALUATED_KEY, page=page)
        with pytest.raises(InvalidCursorError, match="does not name a page after the first"):
            decode_cursor(_token(payload), pipeline_id=PIPELINE_ID)


# ===========================================================================
# Properties of the refusals themselves
# ===========================================================================
class TestRefusalsAreSafeToSurface:
    def test_no_message_echoes_the_submitted_cursor(self):
        """The handler puts ``str(exc)`` in a 400 body and in a log line, so a
        message that echoed the input would let a client choose part of both.
        """
        marker = "MARKER-a1b2c3-injected"
        submitted = _token({"pipeline_id": marker, "page": 2})

        with pytest.raises(InvalidCursorError) as raised:
            decode_cursor(submitted, pipeline_id=PIPELINE_ID)

        assert marker not in str(raised.value)
        assert submitted not in str(raised.value)

    def test_invalid_cursor_error_is_a_value_error(self):
        """So a caller that has not been taught about the subclass still handles it
        as a bad value rather than letting it escape as an unexpected type.
        """
        assert issubclass(InvalidCursorError, ValueError)

    def test_the_codec_needs_no_table_environment(self):
        """A pure module, asserted rather than assumed.

        ``handlers/common.py`` calls ``require_env`` at import time, so any handler
        module that reaches it drags the whole table-name environment into every
        importer. Keeping the codec free of that is what lets 5.2 and 5.6 use it
        without inheriting a fixture, and what keeps this file's import block short.
        """
        import handlers.run_cursor as codec

        assert not hasattr(codec, "executions_table")
        assert "handlers.common" not in {getattr(v, "__module__", "") for v in vars(codec).values()}


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__])
