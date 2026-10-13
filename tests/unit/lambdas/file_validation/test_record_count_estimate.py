# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests for `estimate_record_count` (p64 task 1a.1, R1).

`estimate_record_count` (`file_validation/lambda_function.py:148`) was
**genuinely uncovered** before this file — no test in `tests/` referenced it, and
CodeGraph agrees (⚠️ no covering tests, 2 callers, both in its own module:
`handle_prefix_trigger:398` and `lambda_handler:753`). p64 design.md §5 O5 flags
it as the one symbol whose zero-coverage flag is a true positive, and requires it
characterized **before** C1 (task 1a.3) edits its file.

**Expectations here are derived from R1, not from current output.** That
distinction is the whole point of the task: this function's headers-only
behaviour is a *known defect*, so a test written from what the code does today
would pin the bug in place. Where R1 and today's code disagree, the test is
marked `xfail` and names the requirement.

R1's premise is that a source with no rows must not be reported as having rows.
Two facts make that concrete rather than abstract:

  * `record_count_estimate` **was consulted** when this file was written —
    `_get_lines_ingested` (`dashboard_api/handlers/metrics.py`) summed it into
    the dashboard's `lines_ingested_24h` tile, so an over-estimate was a
    published lie rather than dead data. On 2026-10-10 that tile moved to the
    rows the Glue job reports writing (`_get_rows_loaded`), and nothing in the
    dashboard reads this estimate any more. The function's contract is
    unchanged and still pinned here.
  * That reader treated `0` as an **honest measured zero** rather than an
    absence. So returning 0 for a rowless source is a supported outcome, not a
    degradation.

R1.2 forbids reading object bytes at step 1, which bounds what this function can
ever know: it sees `(size, format)` only. See the xfail at the bottom for what
that costs and why R1.3 puts the headers-only refusal at step 2 instead.
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ── Load the Lambda module ────────────────────────────────────────────────
#
# Same importlib idiom as the sibling `test_format_detection_prop.py`
# (`_load_file_validation_module`, :22-56), duplicated rather than shared
# because that is this directory's existing pattern. `estimate_record_count` is
# pure — no AWS call, no fixture from `tests/fixtures/conftest.py` applies — but
# importing its module runs a body that builds boto3 clients, so boto3 is shimmed
# for the import only.


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


# ── The divisor table, transcribed ────────────────────────────────────────
#
# Transcribed from `_AVG_BYTES_PER_RECORD` (`lambda_function.py:140-145`), NOT
# imported from it — importing would make the producer its own oracle, which is
# the reasoning the sibling `test_format_detection_prop.py:63-71` states for
# `FORMAT_MAP`. If a divisor changes, or a format gains one, these tests are
# meant to fail rather than silently re-derive the new answer.
CSV_BYTES_PER_RECORD = 100
JSON_BYTES_PER_RECORD = 200

# Canonical formats (see `FORMAT_MAP`) that carry no divisor, so size is not a
# usable row proxy for them. `""`/`None` stand for an absent format.
NON_ESTIMABLE_FORMATS = ["parquet", "avro", "orc", "excel", "unknown", "", None]


# ── Passing today: what C1 (task 1a.3) must not change ────────────────────


def test_zero_byte_source_estimates_zero_records():
    """**Validates: Requirements R1.1**

    A zero-byte object has no rows, so it must not be credited with any. This is
    the one case where size alone is decisive, and it already holds — pinned here
    because 1a.3 adds its zero-byte refusal to this same file and must not
    perturb the estimate the metadata row carries.
    """
    assert estimate_record_count(0, "csv") == 0
    assert estimate_record_count(0, "json") == 0


def test_unavailable_size_estimates_zero_records():
    """**Validates: Requirements R1.1**

    No count is fabricated when the HEAD gave no usable size. R1's honesty
    premise makes silence the only correct answer here; guessing would put an
    invented number on the dashboard tile.
    """
    assert estimate_record_count(None, "csv") == 0
    assert estimate_record_count("", "csv") == 0
    assert estimate_record_count("not-a-number", "csv") == 0
    assert estimate_record_count([], "csv") == 0


def test_negative_size_estimates_zero_records():
    """A negative size is not a row count. Floor is zero, never negative."""
    assert estimate_record_count(-1, "csv") == 0
    assert estimate_record_count(-10_000, "json") == 0


@pytest.mark.parametrize("file_type", NON_ESTIMABLE_FORMATS)
def test_non_size_estimable_formats_estimate_zero(file_type):
    """**Validates: Requirements R1.1**

    Binary and columnar formats carry no byte-per-row constant, so no estimate is
    offered for them at any size. `metrics.py:1723` skips a row whose estimate is
    absent, and `0` here is what keeps a parquet run from contributing a fiction
    to the ingested-records tile.
    """
    assert estimate_record_count(10_000_000, file_type) == 0


def test_data_bearing_csv_keeps_its_positive_estimate():
    """**Validates: Requirements R1.5**

    R1.5 requires a source that has rows to keep its current path. A 10 KB CSV
    estimates 100 records at 100 B/row, and must go on doing so after 1a.3.
    """
    assert estimate_record_count(10_000, "csv") == 10_000 // CSV_BYTES_PER_RECORD
    assert estimate_record_count(10_000, "csv") > 0


def test_json_uses_its_own_divisor():
    """**Validates: Requirements R1.5**

    json/jsonl estimate at 200 B/record, so the same bytes yield half the csv
    count. Pinned because a single shared divisor would be an easy, invisible
    "simplification".
    """
    assert estimate_record_count(10_000, "json") == 10_000 // JSON_BYTES_PER_RECORD
    assert estimate_record_count(10_000, "jsonl") == 10_000 // JSON_BYTES_PER_RECORD
    assert estimate_record_count(10_000, "json") != estimate_record_count(10_000, "csv")


def test_format_lookup_is_case_insensitive():
    """Format arrives from `detect_format`, but the lookup lower-cases anyway."""
    assert estimate_record_count(10_000, "CSV") == estimate_record_count(10_000, "csv")
    assert estimate_record_count(10_000, "Json") == estimate_record_count(10_000, "json")


def test_small_data_bearing_csv_estimates_zero_records():
    """**Validates: Requirements R1.5** — characterized, and it is a finding.

    The under-count direction of the same imprecision the xfail below covers: a
    CSV with real data rows but fewer than 100 bytes estimates **zero** records.
    Integer division floors, so anything below one divisor's worth reports as
    nothing.

    This passes today and is left passing deliberately — R1 does not require it
    fixed, and `estimate_record_count` cannot tell 99 bytes of data from 99 bytes
    of header without a read (R1.2). It is recorded because it falsifies the
    reader's assumption at `metrics.py:1659-1662` that a `0` is "an empty but
    successfully validated file": a small, non-empty, correctly-ingested CSV
    produces the identical `0`. Zero is therefore not evidence of emptiness.
    """
    header = "id,name\n"
    two_rows = "1,alice\n2,bob\n"
    size = len((header + two_rows).encode("utf-8"))

    assert size < CSV_BYTES_PER_RECORD, "precondition: the fixture must be under one divisor"
    assert estimate_record_count(size, "csv") == 0


# ── Not passing today: R1's requirement, and the defect it names ──────────


@pytest.mark.xfail(
    strict=True,
    reason=(
        "p64 R1.3/R1.4: a headers-only CSV has no data rows, so nothing may report "
        "that it has records. Today `estimate_record_count` returns size // 100 and "
        "credits a wide header row with records that do not exist (the dashboard "
        "published this estimate until 2026-10-10, when its tile moved to Glue's "
        "rows_written). NEITHER 1a.3 NOR 1a.4 CLOSES THIS: 1a.3 refuses only "
        "zero-byte objects and a headers-only file is not zero-byte, and 1a.4's "
        "refusal is in schema_extract, after this metadata row is already written. "
        "R1.2 also forbids reading object bytes at step 1, so no implementation of "
        "estimate_record_count(size, format) can separate a 116-byte header from a "
        "116-byte two-row CSV — which is exactly why R1.3 puts the headers-only "
        "refusal at step 2. Recorded as a visible residual rather than claimed "
        "closed; an XPASS here means the estimate stopped being a bare count or "
        "gained information it does not have today, and is the prompt to remove "
        "this marker."
    ),
)
def test_headers_only_csv_does_not_claim_data_rows():
    """**Validates: Requirements R1.3, R1.4**

    The misleading case p64 design.md:467 names: a non-zero estimate for a file
    with no rows. The header below is a realistic wide one, and the precondition
    assert is load-bearing — with a header under 100 bytes the estimate would be
    0 by accident of flooring and this test would pass while proving nothing.
    """
    columns = [
        "patient_id",
        "first_name",
        "last_name",
        "date_of_birth",
        "medical_record_number",
        "admission_date",
        "discharge_date",
        "primary_diagnosis",
    ]
    headers_only_csv = ",".join(columns) + "\n"
    size = len(headers_only_csv.encode("utf-8"))

    # Not an assertion about the code under test — it establishes that this
    # fixture actually reaches the misleading branch.
    assert size >= CSV_BYTES_PER_RECORD, (
        f"precondition: the header must be at least one divisor ({CSV_BYTES_PER_RECORD} B) "
        f"for the over-estimate to be reachable; got {size} B"
    )

    assert estimate_record_count(size, "csv") == 0, (
        "a file carrying a header and no data rows must not be credited with records"
    )
