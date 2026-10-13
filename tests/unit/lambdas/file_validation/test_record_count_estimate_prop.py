# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for `estimate_record_count` (p64 task 1a.1, R1).

Companion to `test_record_count_estimate.py`, which carries the examples and the
full provenance note. These are **characterization properties** — p64's design
document names no numbered property for this symbol, only that it must be
characterized before C1 (task 1a.3) edits its file (design.md:465-468). Each one
below is a direct reading of R1's premise that a source with no rows must not be
reported as having rows.

`@settings(max_examples=10)` per `.kiro/steering/testing-standards.md`.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

# ── Load the Lambda module ────────────────────────────────────────────────
#
# Same importlib idiom as the sibling `test_format_detection_prop.py:22-56`,
# duplicated rather than shared because that is this directory's pattern.


def _load_file_validation_module():
    """Import the file_validation Lambda without importing AWS."""
    lambda_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__),
            "..",
            "..",
            "..",
            "..",
            "lambda",
            "agentic_data_ingestion_file_validation",
        )
    )
    lambda_path = os.path.join(lambda_dir, "lambda_function.py")

    sys.path.insert(0, lambda_dir)
    shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
    sys.path.insert(0, shared_dir)

    os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
    os.environ.setdefault("DYNAMODB_TABLE", "test-table")

    mock_boto3 = MagicMock()
    mock_boto3.client.return_value = MagicMock()
    mock_boto3.resource.return_value = MagicMock()

    with patch.dict("sys.modules", {"boto3": mock_boto3}):
        sys.modules.pop("lambda_function", None)
        spec = importlib.util.spec_from_file_location("lambda_function", lambda_path)
        lf = importlib.util.module_from_spec(spec)
        sys.modules["lambda_function"] = lf
        spec.loader.exec_module(lf)

    return lf


_lf = _load_file_validation_module()
estimate_record_count = _lf.estimate_record_count


# ── Strategies ────────────────────────────────────────────────────────────
#
# Divisors and format names transcribed from `_AVG_BYTES_PER_RECORD`
# (`lambda_function.py:140-145`) rather than imported, so the producer is not its
# own oracle — the reasoning stated at `test_format_detection_prop.py:63-71`.
CSV_BYTES_PER_RECORD = 100

ESTIMABLE_FORMATS = ["csv", "tsv", "json", "jsonl"]
NON_ESTIMABLE_FORMATS = ["parquet", "avro", "orc", "excel", "unknown", ""]

any_format = st.sampled_from(ESTIMABLE_FORMATS + NON_ESTIMABLE_FORMATS)

# 5 TB is `MAX_FILE_SIZE_BYTES`, the ceiling the only existing size check uses
# (`lambda_function.py:46`, :732), so it is the top of the range a real object
# can occupy.
MAX_FILE_SIZE_BYTES = 5 * 1024 * 1024 * 1024 * 1024
plausible_sizes = st.integers(min_value=0, max_value=MAX_FILE_SIZE_BYTES)
non_positive_sizes = st.integers(min_value=-MAX_FILE_SIZE_BYTES, max_value=0)

# At least 10 names of at least 10 characters, so the joined header is always
# >= 110 bytes — comfortably over one csv divisor. Constructed to guarantee the
# over-estimate branch is reached rather than filtering with `assume`, which at
# max_examples=10 would discard most of the budget.
wide_column_lists = st.lists(
    st.from_regex(r"[a-z][a-z_]{9,19}", fullmatch=True),
    min_size=10,
    max_size=30,
)


# ── Passing today ─────────────────────────────────────────────────────────


@given(size=st.integers(min_value=-MAX_FILE_SIZE_BYTES, max_value=MAX_FILE_SIZE_BYTES), file_type=any_format)
@settings(max_examples=10)
def test_estimate_is_never_negative(size, file_type):
    """**Validates: Requirements R1.1**

    A record count below zero is not a fact about any file. Holds for every size
    and format, including the negative and non-estimable cases.
    """
    assert estimate_record_count(size, file_type) >= 0


@given(size=non_positive_sizes, file_type=any_format)
@settings(max_examples=10)
def test_no_bytes_means_no_records(size, file_type):
    """**Validates: Requirements R1.1**

    Generalises the zero-byte example: for any format, a size that is zero or
    unusable yields no estimate. This is the property 1a.3's refusal sits beside,
    and it must survive that change.
    """
    assert estimate_record_count(size, file_type) == 0


@given(size=plausible_sizes, file_type=st.sampled_from(NON_ESTIMABLE_FORMATS))
@settings(max_examples=10)
def test_non_estimable_formats_never_estimate(size, file_type):
    """**Validates: Requirements R1.1**

    Size is not a row proxy for binary or columnar formats at any size, so no
    number is offered. Zero here is what keeps a parquet run from contributing a
    fiction to the ingested-records tile (`metrics.py:1723`).
    """
    assert estimate_record_count(size, file_type) == 0


@given(
    sizes=st.tuples(plausible_sizes, plausible_sizes),
    file_type=st.sampled_from(ESTIMABLE_FORMATS),
)
@settings(max_examples=10)
def test_estimate_never_decreases_as_the_file_grows(sizes, file_type):
    """**Validates: Requirements R1.5**

    The estimate is a function of size alone, so more bytes can never mean fewer
    records. Pins the shape of the function against a future change that made it
    non-monotonic — which would let two runs over growing data report a shrinking
    count on the dashboard tile.
    """
    smaller, larger = sorted(sizes)
    assert estimate_record_count(smaller, file_type) <= estimate_record_count(larger, file_type)


# ── Not passing today ─────────────────────────────────────────────────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "p64 R1.3/R1.4, generalised from the example in "
        "test_record_count_estimate.py::test_headers_only_csv_does_not_claim_data_rows. "
        "For ANY headers-only CSV whose header exceeds one divisor, today's "
        "size // 100 credits records that do not exist. Neither 1a.3 (zero-byte "
        "only) nor 1a.4 (schema_extract, after this row is written) closes it, and "
        "R1.2 forbids the read that would make the estimate exact — so this is a "
        "visible residual, not a claimed fix. An XPASS is the prompt to remove the "
        "marker."
    ),
)
@given(columns=wide_column_lists)
@settings(max_examples=10)
def test_no_positive_estimate_for_a_file_with_no_data_rows(columns):
    """**Validates: Requirements R1.3, R1.4**

    A file consisting of a header line and nothing else has zero data rows,
    whatever its columns are named.
    """
    headers_only_csv = ",".join(columns) + "\n"
    size = len(headers_only_csv.encode("utf-8"))

    # Establishes that the generated fixture reaches the misleading branch.
    assert size >= CSV_BYTES_PER_RECORD

    assert estimate_record_count(size, "csv") == 0
