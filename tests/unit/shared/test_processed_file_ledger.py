# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit + property tests for shared/processed_file_ledger.py.

The Processed_File_Ledger is a cross-Lambda contract: ``job_completion`` writes
rows that ``pipeline_trigger`` reads. Its most dangerous failure mode is silent
— if the two sides derive different fingerprints for the same object, nothing
raises, no test fails, and duplicates simply stop being detected. The
equivalence tests below exist to make that failure loud.

Validates: Requirements 9.1, 9.2
"""

import pathlib
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import given
from hypothesis import strategies as st

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

from processed_file_ledger import (
    FINGERPRINT_KEY,
    content_fingerprint,
    find_processed_file,
    ledger_key,
    normalize_etag,
    record_processed_file,
)

# A single-part ETag is the content MD5: 32 hex chars.
_ETAG = "d41d8cd98f00b204e9800998ecf8427e"
# A multipart ETag is a hash of part hashes plus a part count.
_MULTIPART_ETAG = "9bb58f26192e4ba00f01e2e7b136bbd8-3"


class TestNormalizeEtag:
    """The three ETag spellings the repo can encounter must collapse to one."""

    def test_quoted_and_bare_forms_are_equal(self):
        """head_object/list_objects_v2 quote the ETag; EventBridge does not."""
        assert normalize_etag(f'"{_ETAG}"') == normalize_etag(_ETAG) == _ETAG

    def test_case_is_normalized(self):
        assert normalize_etag(_ETAG.upper()) == _ETAG

    def test_surrounding_whitespace_is_stripped(self):
        assert normalize_etag(f'  "{_ETAG}"  ') == _ETAG

    def test_multipart_suffix_is_preserved(self):
        """The -N suffix is part of the identity — stripping it would make two
        different part layouts of different content look identical."""
        assert normalize_etag(f'"{_MULTIPART_ETAG}"') == _MULTIPART_ETAG

    @pytest.mark.parametrize("bad", [None, "", '""', "   ", 42, b"bytes", {"e": "tag"}])
    def test_unusable_input_yields_empty_string(self, bad):
        assert normalize_etag(bad) == ""


class TestContentFingerprint:
    """Composition: sha256 of "{size}:{normalized_etag}"."""

    def test_head_listing_and_event_forms_agree(self):
        """The property the whole mechanism rests on.

        file_validation fingerprints from ``head_object`` (quoted ETag,
        ``ContentLength``); the trigger fingerprints from ``list_objects_v2``
        (quoted ETag, ``Size``) or from the EventBridge detail (BARE ETag,
        ``object.size``, arriving as a JSON number). All three must produce one
        value or the ledger never matches and dedup silently does nothing.
        """
        from_head = content_fingerprint(1024, f'"{_ETAG}"')
        from_listing = content_fingerprint(1024, f'"{_ETAG}"')
        from_event = content_fingerprint(1024, _ETAG)
        assert from_head == from_listing == from_event
        assert len(from_head) == 64

    def test_numeric_string_and_decimal_sizes_agree_with_int(self):
        """Size arrives as int (boto3), JSON number, and Decimal (DynamoDB)."""
        from decimal import Decimal

        assert content_fingerprint(1024, _ETAG) == content_fingerprint("1024", _ETAG)
        assert content_fingerprint(1024, _ETAG) == content_fingerprint(Decimal(1024), _ETAG)

    def test_different_size_same_etag_is_a_different_fingerprint(self):
        assert content_fingerprint(1024, _ETAG) != content_fingerprint(1025, _ETAG)

    def test_different_etag_same_size_is_a_different_fingerprint(self):
        assert content_fingerprint(1024, _ETAG) != content_fingerprint(1024, _MULTIPART_ETAG)

    def test_multipart_part_size_difference_is_not_detected_as_duplicate(self):
        """The recorded multipart limitation, pinned as a test.

        The same bytes uploaded with different multipart settings carry
        different ETags, so the fingerprints differ and the duplicate is NOT
        recognised. That is a missed skip, which is the harmless direction — but
        it is a real limitation and should fail loudly if someone ever "fixes"
        the fingerprint by stripping the part suffix.
        """
        three_parts = content_fingerprint(1024, "9bb58f26192e4ba00f01e2e7b136bbd8-3")
        five_parts = content_fingerprint(1024, "9bb58f26192e4ba00f01e2e7b136bbd8-5")
        assert three_parts != five_parts

    @pytest.mark.parametrize(
        "size,etag",
        [
            (1024, None),  # EventBridge schema marks etag OPTIONAL
            (1024, ""),
            (None, _ETAG),
            ("not-a-number", _ETAG),
            (-1, _ETAG),
            (True, _ETAG),  # bool is an int subclass; a True size is a bug
        ],
    )
    def test_unfingerprintable_input_returns_none(self, size, etag):
        """None means "do not dedup, proceed" — never an exception, because the
        harmless direction must also be the easy one."""
        assert content_fingerprint(size, etag) is None


class TestLedgerKey:
    def test_key_is_pipeline_plus_fingerprint(self):
        """R9.1: keyed by pipeline AND fingerprint — the same content in two
        pipelines is two legitimate ingestions."""
        key = ledger_key("orders", "abc")
        assert key == {"pipeline_id": "orders", FINGERPRINT_KEY: "abc"}


class TestFindProcessedFile:
    def test_returns_the_row_when_present(self):
        table = MagicMock()
        table.get_item.return_value = {"Item": {"run_id": "run-1"}}
        assert find_processed_file(table, "orders", "fp") == {"run_id": "run-1"}
        table.get_item.assert_called_once_with(Key={"pipeline_id": "orders", FINGERPRINT_KEY: "fp"})

    def test_returns_none_when_absent(self):
        table = MagicMock()
        table.get_item.return_value = {}
        assert find_processed_file(table, "orders", "fp") is None

    @pytest.mark.parametrize("fingerprint", [None, ""])
    def test_no_fingerprint_short_circuits_without_touching_dynamodb(self, fingerprint):
        table = MagicMock()
        assert find_processed_file(table, "orders", fingerprint) is None
        table.get_item.assert_not_called()

    def test_fails_open_when_dynamodb_raises(self):
        """A ledger outage must cost the dedup, not the ingestion."""
        table = MagicMock()
        table.get_item.side_effect = RuntimeError("throttled")
        assert find_processed_file(table, "orders", "fp") is None


class TestRecordProcessedFile:
    def test_writes_key_run_id_and_ttl(self):
        table = MagicMock()
        item = record_processed_file(
            table,
            "orders",
            "fp",
            run_id="run-1",
            size=1024,
            etag=f'"{_ETAG}"',
            source_key="s3://b/orders/f.csv",
            ttl_seconds=60,
        )
        table.put_item.assert_called_once()
        written = table.put_item.call_args.kwargs["Item"]
        assert written["pipeline_id"] == "orders"
        assert written[FINGERPRINT_KEY] == "fp"
        assert written["run_id"] == "run-1"
        assert written["ttl"] > 0
        assert item == written

    def test_stores_the_fingerprint_preimage_normalized(self):
        """size + ETag are kept so an operator can see WHY two submissions did
        or did not match, without recomputing a digest."""
        table = MagicMock()
        record_processed_file(table, "orders", "fp", run_id="r", size=1024, etag=f'"{_ETAG}"', ttl_seconds=60)
        written = table.put_item.call_args.kwargs["Item"]
        assert written["content_size"] == 1024
        assert written["content_etag"] == _ETAG

    def test_no_condition_expression_so_a_deliberate_rerun_refreshes_the_row(self):
        """Last-writer-wins on purpose: the manual override path re-runs the same
        content legitimately, and that completion should refresh the row rather
        than log a spurious conditional-check failure."""
        table = MagicMock()
        record_processed_file(table, "orders", "fp", run_id="r", ttl_seconds=60)
        assert "ConditionExpression" not in table.put_item.call_args.kwargs


class TestFingerprintProperties:
    """Property tests — the two invariants the cross-Lambda contract needs."""

    @given(
        size=st.integers(min_value=0, max_value=5 * 1024**4),
        etag_hex=st.text(alphabet="0123456789abcdef", min_size=32, max_size=32),
        parts=st.one_of(st.none(), st.integers(min_value=1, max_value=10000)),
    )
    def test_quoted_and_bare_etags_always_agree(self, size, etag_hex, parts):
        """For ANY object, the quoted form (HEAD / listing) and the bare form
        (EventBridge) fingerprint identically."""
        etag = etag_hex if parts is None else f"{etag_hex}-{parts}"
        assert content_fingerprint(size, f'"{etag}"') == content_fingerprint(size, etag)

    @given(
        size=st.integers(min_value=0, max_value=5 * 1024**4),
        etag_hex=st.text(alphabet="0123456789abcdefABCDEF", min_size=32, max_size=32),
    )
    def test_fingerprint_is_deterministic_and_hex(self, size, etag_hex):
        """Deterministic across calls (sha256, not the process-randomised
        builtin ``hash`` — LGR-dd80c6d-1) and always a 64-char hex digest."""
        first = content_fingerprint(size, etag_hex)
        second = content_fingerprint(size, etag_hex)
        assert first == second
        assert len(first) == 64
        assert all(c in "0123456789abcdef" for c in first)
