# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: format detection correctness (Property 37).

**Validates: Requirements 24.1**

Property 37: For any supported file extension (including compressed variants),
detect_format() returns the correct format identifier.

Uses @given with random filenames + extensions and @settings(max_examples=10).
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
from hypothesis import assume, given, settings

# ── Load detect_format via importlib ──────────────────────────────────────


def _load_file_validation_module():
    """Import file_validation Lambda using importlib to avoid module collisions."""
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
detect_format = _lf.detect_format
FORMAT_MAP = _lf.FORMAT_MAP


# ── Strategies ────────────────────────────────────────────────────────────

# All supported plain extensions and their expected canonical format.
#
# Transcribed from `FORMAT_MAP`, not imported from it — importing would make the
# producer its own oracle. p55 task 4.2 kept the transcription in sync when
# `xlsx → "excel"` was added (R6.1). Both properties below read this table and
# both go wrong if it drifts: Property 37 claims to hold "for any supported file
# extension", so a missing entry is silently uncovered ground; and
# `test_unsupported_extension_returns_unknown`'s `assume` excludes candidates
# using THIS table, so a supported extension absent here would be drawn as
# "unsupported" and asserted to detect as `unknown`.
EXTENSION_TO_FORMAT = {
    "csv": "csv",
    "tsv": "csv",
    # p64 1c.1 kept the transcription in sync when `txt → "csv"` was added
    # (R3.1), the same maintenance p55 task 4.2 did for `xlsx`. Both properties
    # depend on it: without this entry a `.txt` key would be drawn by
    # `test_unsupported_extension_returns_unknown`'s `assume` and asserted to
    # detect as `unknown`, which it no longer does.
    "txt": "csv",
    "json": "json",
    "jsonl": "json",
    "ndjson": "json",
    "parquet": "parquet",
    "pq": "parquet",
    "avro": "avro",
    "orc": "orc",
    "xlsx": "excel",
}

COMPRESSION_SUFFIXES = ["gz", "bz2", "snappy", "zst"]

# Strategy: random filename stem (alphanumeric, 1-20 chars)
filename_stems = st.from_regex(r"[a-zA-Z0-9_\-]{1,20}", fullmatch=True)

# Strategy: optional S3 prefix path segments
path_prefixes = st.from_regex(r"([a-z0-9\-]{1,10}/){0,3}", fullmatch=True)

# Strategy: a supported extension
supported_extensions = st.sampled_from(list(EXTENSION_TO_FORMAT.keys()))

# Strategy: optional compression suffix
compression_suffixes = st.one_of(st.none(), st.sampled_from(COMPRESSION_SUFFIXES))


# ── Property tests ────────────────────────────────────────────────────────


@given(
    prefix=path_prefixes,
    stem=filename_stems,
    ext=supported_extensions,
    compression=compression_suffixes,
)
@settings(max_examples=10)
def test_detect_format_returns_correct_identifier(prefix, stem, ext, compression):
    """**Validates: Requirements 24.1**

    Property 37: For any supported file extension (including compressed
    variants), detect_format() returns the correct format identifier.
    """
    if compression:
        key = f"{prefix}{stem}.{ext}.{compression}"
    else:
        key = f"{prefix}{stem}.{ext}"

    expected = EXTENSION_TO_FORMAT[ext]
    result = detect_format(key)

    assert result == expected, f"detect_format({key!r}) returned {result!r}, expected {expected!r}"


@given(
    stem=filename_stems,
    ext=st.from_regex(r"[a-z]{2,5}", fullmatch=True),
)
@settings(max_examples=10)
def test_unsupported_extension_returns_unknown(stem, ext):
    """Unsupported extensions return 'unknown'."""
    assume(ext not in EXTENSION_TO_FORMAT)
    assume(ext not in COMPRESSION_SUFFIXES)

    key = f"{stem}.{ext}"
    assert detect_format(key) == "unknown"
