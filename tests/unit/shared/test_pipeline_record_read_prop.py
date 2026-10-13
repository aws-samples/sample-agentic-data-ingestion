# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: the shared read primitive preserves each caller's sentinel.

Design §6.5, R12: *for any (record-present, record-absent) pair, each caller's
sentinel is preserved.*

WHY THIS IS ONE TEST AND NOT FIVE
---------------------------------
E1 declined to write this and flagged it for E2, for a reason worth restating:
before the primitive existed, "each caller's sentinel is preserved" could only be
written as five separate assertions against five separate functions — i.e. a
fifth restatement of ``test_missing_item_returns_*``, which R12.7 calls out as
not-progress. Now that the sentinel is a *parameter of one mechanism*, the
property is a property of that mechanism: whatever value goes in comes back out
on both not-found paths, and never comes back when a record is present. One
test, quantified over sentinels rather than over Lambdas.

WHAT WOULD FALSIFY IT
---------------------
Any normalisation of the sentinel inside the primitive — ``return not_found or
{}``, ``return dict(not_found)``, a hardcoded ``None``, or treating a falsy
present record as absence. The first three are the shapes R12.1 /
``LGR-b2f79ef-8`` exist to forbid: collapsing ``None`` and ``{}`` makes "no
pipeline record" indistinguishable from "record with no target schema", which
reports ``verified: False`` for a run whose real problem is a missing record.

The sentinel strategy deliberately includes values no current caller uses
(``[]``, ``0``, ``""``, a marker object). Two callers use ``None`` and one uses
``{}``; constraining the strategy to just those two would let a ``not_found or
{}`` bug through, because ``{} or {}`` is ``{}``. Falsy-but-not-``None`` values
are the discriminating inputs, so they are generated on purpose.

**Validates: Requirements R12.1, R12.4**
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError
from hypothesis import given, settings
from hypothesis import strategies as st

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import pipeline_record_read as prr
from step_errors import NOT_FOUND_CLIENT_ERROR_CODES, StepFailure

PIPELINE_ID = "pipe-prop"


class _Marker:
    """A sentinel that is neither falsy-empty nor a container."""


#: Every not-found value a caller might hand in. ``None`` and ``{}`` are the two
#: in live use (``test_code``/``schema_mapping`` and ``create_code``); the rest
#: are the falsy values that catch a ``or``-style collapse.
sentinels = st.sampled_from([None, {}, [], 0, "", False, _Marker(), {"defaulted": True}])

#: Records that *exist*. Includes the empty dict: a record that exists and
#: carries nothing is presence, not absence.
present_records = st.dictionaries(
    st.sampled_from(["pipeline_id", "ai_model", "target_schema_bucket", "target_schema_s3_key", "approval_config"]),
    st.one_of(st.text(max_size=8), st.booleans(), st.integers(-4, 4)),
    max_size=5,
)

not_found_codes = st.sampled_from(sorted(NOT_FOUND_CLIENT_ERROR_CODES))


def _table(*, returns=None, raises=None) -> MagicMock:
    table = MagicMock()
    if raises is not None:
        table.get_item.side_effect = raises
    else:
        table.get_item.return_value = returns
    return table


def _read(table, sentinel, *, step_name="a_step"):
    return prr.read_pipeline_record(
        table,
        PIPELINE_ID,
        step_name=step_name,
        operation="get_pipeline_metadata",
        not_found=sentinel,
        logger=MagicMock(),
    )


class TestSentinelPreservation:
    @given(record=present_records, sentinel=sentinels)
    @settings(max_examples=300)
    def test_a_present_record_is_returned_and_the_sentinel_is_not(self, record, sentinel):
        """Presence never yields the sentinel — the pair's first half."""
        assert _read(_table(returns={"Item": record}), sentinel) is record

    @given(sentinel=sentinels)
    @settings(max_examples=200)
    def test_a_missing_item_yields_exactly_the_sentinel_object(self, sentinel):
        """Absence yields the value handed in, by identity — no normalisation."""
        assert _read(_table(returns={}), sentinel) is sentinel

    @given(sentinel=sentinels, code=not_found_codes)
    @settings(max_examples=300)
    def test_every_not_found_client_error_yields_exactly_the_sentinel_object(self, sentinel, code):
        """The second not-found path, over the whole not-found code set.

        Both paths must answer identically: that is what makes a missing table
        and a missing record indistinguishable to the caller, which
        ``LGR-b2f79ef-8`` accepts precisely because the log keeps them apart.
        """
        exc = ClientError({"Error": {"Code": code, "Message": code}}, "GetItem")
        assert _read(_table(raises=exc), sentinel) is sentinel

    @given(record=present_records, sentinel=sentinels)
    @settings(max_examples=200)
    def test_the_two_states_are_separated_for_every_caller(self, record, sentinel):
        """The pair, stated as the pair: present and absent never coincide.

        Separation is by **object identity**, not by value, and that distinction
        is the point. With a ``{}`` sentinel and a record that also happens to be
        ``{}``, the two states are equal by value — which is exactly the collapse
        ``LGR-b2f79ef-8`` rejected ``{}`` for at ``test_code``. What the
        primitive guarantees, and all it can guarantee, is that it hands back the
        record it read or the sentinel it was given, never a substitute for
        either. A caller that needs the two states distinguishable by value has
        to choose a sentinel that is not a possible record, which is what
        ``None`` is.
        """
        present = _read(_table(returns={"Item": record}), sentinel)
        absent = _read(_table(returns={}), sentinel)
        assert present is record
        assert absent is sentinel
        assert present is not absent


class TestClassificationIsNotSentinelDependent:
    @given(sentinel=sentinels)
    @settings(max_examples=150)
    def test_a_transient_code_raises_whatever_the_sentinel_is(self, sentinel):
        """A sentinel is not a licence to swallow an infrastructure error."""
        exc = ClientError({"Error": {"Code": "ThrottlingException", "Message": "x"}}, "GetItem")
        with pytest.raises(StepFailure) as exc_info:
            _read(_table(raises=exc), sentinel)
        assert type(exc_info.value).__name__ == "TransientStepFailure"

    @given(sentinel=sentinels)
    @settings(max_examples=150)
    def test_a_permanent_code_raises_whatever_the_sentinel_is(self, sentinel):
        exc = ClientError({"Error": {"Code": "AccessDeniedException", "Message": "x"}}, "GetItem")
        with pytest.raises(StepFailure) as exc_info:
            _read(_table(raises=exc), sentinel)
        assert exc_info.value.error_type == "permanent"
