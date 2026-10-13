# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for detect_format() in file_validation Lambda.

Tests all supported extensions including compressed variants (.gz, .bz2, .snappy, .zst).
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_file_validation"),
)

# Set required env vars before importing the module
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test_table")

# Mock boto3 to prevent real AWS calls during import
with patch.dict("sys.modules", {"boto3": MagicMock()}):
    import boto3 as _boto3

    _boto3.client.return_value = MagicMock()
    _boto3.resource.return_value = MagicMock()
    if "lambda_function" in sys.modules:
        del sys.modules["lambda_function"]
    from lambda_function import FORMAT_MAP, NATIVE_FORMATS, detect_format


# ── Basic extension tests ─────────────────────────────────────────────────


class TestDetectFormatBasic:
    """Test detect_format for all supported plain extensions."""

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("data.csv", "csv"),
            ("data.tsv", "csv"),
            ("data.json", "json"),
            ("data.jsonl", "json"),
            ("data.ndjson", "json"),
            ("data.parquet", "parquet"),
            ("data.pq", "parquet"),
            ("data.avro", "avro"),
            ("data.orc", "orc"),
        ],
    )
    def test_plain_extensions(self, key, expected):
        assert detect_format(key) == expected

    @pytest.mark.parametrize(
        "key,expected",
        [
            ("DATA.CSV", "csv"),
            ("DATA.JSON", "json"),
            ("DATA.Parquet", "parquet"),
            ("DATA.AVRO", "avro"),
            ("DATA.ORC", "orc"),
        ],
    )
    def test_case_insensitive(self, key, expected):
        assert detect_format(key) == expected

    def test_no_extension(self):
        assert detect_format("noextension") == "unknown"

    def test_unknown_extension(self):
        # AMENDED by p55 W3 (fence F1). This assertion pinned
        # `detect_format("data.xlsx") == "unknown"`, and it was a deliberate
        # fence: `LGR-60c4985-7` (template / ratified) named it by line when it
        # rejected "add a FORMAT_MAP entry so .xlsx is supported" as "a product
        # change wearing a bugfix's clothes". p55 R6.1 IS that product change,
        # and the same record's `revisit_when` authorises retiring this fence —
        # "a format is added to `FORMAT_MAP` (which retires the two fences
        # deliberately)". So this is a change WITHIN the record, not against it.
        #
        # Amended rather than deleted (p55 do-NOT 6, per `LGR-60c4985-6` /
        # `LGR-9e76249-10`): a reversal stays legible as a reversal.
        assert detect_format("data.xlsx") == "excel"

        # The class's actual subject — an extension outside FORMAT_MAP — kept
        # under a genuinely unrecognised extension, so this test still tests
        # what its name says.
        assert detect_format("data.rtf") == "unknown"

    def test_empty_string(self):
        assert detect_format("") == "unknown"


# ── Compressed variant tests ──────────────────────────────────────────────


class TestDetectFormatCompressed:
    """Test detect_format strips compression suffixes to find the real format."""

    @pytest.mark.parametrize("compression", ["gz", "bz2", "snappy", "zst"])
    @pytest.mark.parametrize(
        "base_ext,expected",
        [
            ("csv", "csv"),
            ("tsv", "csv"),
            ("json", "json"),
            ("jsonl", "json"),
            ("parquet", "parquet"),
            ("avro", "avro"),
            ("orc", "orc"),
        ],
    )
    def test_compressed_variants(self, base_ext, compression, expected):
        key = f"pipeline/data.{base_ext}.{compression}"
        assert detect_format(key) == expected

    def test_compression_only_no_base(self):
        """A file like 'data.gz' with no recognizable base extension."""
        assert detect_format("data.gz") == "unknown"


# ── S3 key path tests ────────────────────────────────────────────────────


class TestDetectFormatWithPaths:
    """Test detect_format with realistic S3 key paths."""

    def test_nested_path_csv(self):
        assert detect_format("my-pipeline/uploads/2024/data.csv") == "csv"

    def test_nested_path_parquet_gz(self):
        assert detect_format("pipeline-123/raw/events.parquet.gz") == "parquet"

    def test_dots_in_folder_name(self):
        assert detect_format("v1.2.3/output/result.json") == "json"

    def test_multiple_dots_in_filename(self):
        assert detect_format("my.data.file.csv") == "csv"


# ── FORMAT_MAP and NATIVE_FORMATS constants ──────────────────────────────


class TestConstants:
    """Verify FORMAT_MAP and NATIVE_FORMATS are correctly defined."""

    def test_format_map_has_all_extensions(self):
        # AMENDED TWICE, and both amendments are the SAME authorisation firing:
        # `LGR-60c4985-7`'s `revisit_when` is "a format is added to `FORMAT_MAP`".
        #
        #   * p55 W3 (fence F2): TEN keys, not nine — `xlsx` added by R6.1.
        #   * p64 1c.1: ELEVEN keys, not ten — `txt` added by R3.1, mapping to
        #     the `csv` Canonical_Format with `|` as its Source_Delimiter.
        #
        # `txt` adds a KEY and no new VALUE, which is the whole reason R3 is
        # cheap: the value set stays six, so `schema_extract`'s dispatch chain,
        # `NATIVE_FORMATS` (R3.4) and the Conversion_Required_Set are all
        # untouched. `test_native_formats` below is deliberately unchanged.
        #
        # Kept as exact set equality rather than loosened to a superset check:
        # pinning the whole vocabulary is the point of the assertion, and
        # loosening it here would silently retire the fence instead of amending
        # it (p55 do-NOT 6).
        expected_keys = {"csv", "tsv", "txt", "json", "jsonl", "ndjson", "parquet", "pq", "avro", "orc", "xlsx"}
        assert set(FORMAT_MAP.keys()) == expected_keys

    def test_native_formats(self):
        # AMENDED by p55 W3 (fence F3): FIVE members, not three.
        #
        # The reason is a change of MEANING, not merely of membership. `csv` and
        # `json` were ALWAYS native under this name — Spark reads both — so the
        # old `{"parquet", "avro", "orc"}` did not mean "what a Glue job reads
        # natively"; it meant something narrower and unnamed, closest to
        # "columnar or container formats needing no text parsing". p55 R13.1/
        # R13.2 fix the meaning as *no conversion of the source object is
        # required*, and this amendment is that fixed meaning written down.
        #
        # Fixed BEFORE the gate's consumer is wired in `schema_extract`, because
        # a consumer pointed at the old set would have concluded a CSV needs
        # converting — the concrete hazard R13 exists to remove.
        #
        # `excel` is deliberately absent and must never be added: Spark has no
        # Excel datasource, so it is the Conversion_Required_Set's only member
        # (R13.7's anti-vacuity floor depends on that complement being
        # non-empty). And this stays a hand-written literal on the production
        # side — never `set(FORMAT_MAP.values()) - {"excel"}` (p55 do-NOT 8).
        assert {"csv", "json", "parquet", "avro", "orc"} == NATIVE_FORMATS

    def test_csv_tsv_map_to_same_format(self):
        assert FORMAT_MAP["csv"] == FORMAT_MAP["tsv"] == "csv"

    def test_json_variants_map_to_same_format(self):
        assert FORMAT_MAP["json"] == FORMAT_MAP["jsonl"] == FORMAT_MAP["ndjson"] == "json"

    def test_parquet_variants_map_to_same_format(self):
        assert FORMAT_MAP["parquet"] == FORMAT_MAP["pq"] == "parquet"
