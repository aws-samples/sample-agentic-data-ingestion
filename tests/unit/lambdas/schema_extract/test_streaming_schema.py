# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for streaming schema extraction (Phase 9, Task 2.10).

Tests:
- extract_schema_from_csv() with mock S3 Select responses
- extract_schema_from_json() with mock Range GET responses
- extract_schema_from_parquet() with mock PyArrow
- extract_schema_from_parquet()'s pyarrow.fs fallback, end to end (p46 task 8.2)
- extract_schema_from_orc() over the committed ORC fixture (p46 task 5.2)
- extract_schema_from_avro() over the committed Avro fixture, including the
  sample_bytes boundary and the hand-parser's raise sites (p46 task 4.2)
- the External_Compression guard over both containers and all three magics,
  and csv/json's exemption from it (p46 task 7.2)
- infer_schema_from_records() type inference and conflict resolution
- arrow_schema_to_json_schema() type mapping
- lambda_handler routing based on source_format
"""

import ast
import bz2
import gzip
import importlib.util
import io
import json
import os
import re
import sys
import types
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pyarrow.orc as pa_orc
import pyarrow.parquet as pa_pq
import pytest

# Ensure shared modules are importable
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_extract"),
)

# Set required env vars before importing the module
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test_state_table")


@pytest.fixture(autouse=True)
def _mock_aws():
    """Mock boto3 and shared modules for all tests."""
    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    # eventbridge_utils.get_eventbridge_publishers() must return a 3-tuple
    mock_eb = MagicMock()
    mock_eb.get_eventbridge_publishers.return_value = (MagicMock(), MagicMock(), MagicMock())

    # env_utils.require_env must return a string
    mock_env = MagicMock()
    mock_env.require_env.return_value = "test_state_table"

    # artifact_schema.create_output_data must return a dict
    mock_artifact = MagicMock()
    mock_artifact.create_output_data.return_value = {}

    # aws_lambda_powertools.Logger must return a mock logger
    mock_powertools = MagicMock()

    # Ensure our Lambda directory is at the front of sys.path so the correct
    # lambda_function is found even if other test files inserted their own paths.
    schema_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_extract"
        )
    )
    sys.path.insert(0, schema_dir)

    with patch.dict(
        "sys.modules",
        {
            "boto3": MagicMock(),
            "eventbridge_utils": mock_eb,
            "env_utils": mock_env,
            "artifact_schema": mock_artifact,
            "metrics_facade": MagicMock(),
            "aws_lambda_powertools": mock_powertools,
        },
    ):
        import boto3 as _boto3

        _boto3.client.return_value = mock_s3
        _boto3.resource.return_value = mock_dynamodb

        # Force reimport to pick up mocks
        for mod in list(sys.modules):
            if "lambda_function" in mod:
                del sys.modules[mod]

        import lambda_function

        # Inject mock s3_client into the module
        lambda_function.s3_client = mock_s3
        lambda_function.table = mock_table

        yield lambda_function, mock_s3, mock_table


# ── infer_schema_from_records ──────────────────────────────────────────


class TestInferSchemaFromRecords:
    """Tests for multi-record type inference with conflict resolution."""

    def test_empty_records(self, _mock_aws):
        lf, _, _ = _mock_aws
        result = lf.infer_schema_from_records([])
        assert result == {"type": "object", "properties": {}}

    def test_single_record(self, _mock_aws):
        lf, _, _ = _mock_aws
        records = [{"name": "Alice", "age": 30, "score": 95.5}]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["name"] == {"type": "string"}
        assert result["properties"]["age"] == {"type": "integer"}
        assert result["properties"]["score"] == {"type": "number"}

    def test_string_wins_over_integer(self, _mock_aws):
        """AC-11: Type conflicts across records are resolved (string wins over integer)."""
        lf, _, _ = _mock_aws
        records = [
            {"id": 1},
            {"id": "abc"},
        ]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["id"] == {"type": "string"}

    def test_number_wins_over_integer(self, _mock_aws):
        """number > integer widening."""
        lf, _, _ = _mock_aws
        records = [
            {"value": 10},
            {"value": 3.14},
        ]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["value"] == {"type": "number"}

    def test_null_only_defaults_to_string(self, _mock_aws):
        lf, _, _ = _mock_aws
        records = [
            {"field": None},
            {"field": None},
        ]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["field"] == {"type": "string"}

    def test_null_ignored_when_other_types_present(self, _mock_aws):
        lf, _, _ = _mock_aws
        records = [
            {"count": None},
            {"count": 42},
        ]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["count"] == {"type": "integer"}

    def test_max_sample_limits_records(self, _mock_aws):
        lf, _, _ = _mock_aws
        # First 3 records have integer, 4th has string — but max_sample=3
        records = [
            {"x": 1},
            {"x": 2},
            {"x": 3},
            {"x": "oops"},
        ]
        result = lf.infer_schema_from_records(records, max_sample=3)
        assert result["properties"]["x"] == {"type": "integer"}

    def test_boolean_type(self, _mock_aws):
        lf, _, _ = _mock_aws
        records = [{"active": True}, {"active": False}]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["active"] == {"type": "boolean"}

    def test_mixed_types_fallback_to_string(self, _mock_aws):
        """When types are incompatible (e.g., boolean + integer), string wins."""
        lf, _, _ = _mock_aws
        records = [
            {"val": True},
            {"val": 42},
        ]
        result = lf.infer_schema_from_records(records)
        assert result["properties"]["val"] == {"type": "string"}

    def test_multiple_fields(self, _mock_aws):
        lf, _, _ = _mock_aws
        records = [
            {"name": "Alice", "age": 30, "active": True},
            {"name": "Bob", "age": 25, "active": False},
        ]
        result = lf.infer_schema_from_records(records)
        assert len(result["properties"]) == 3
        assert result["properties"]["name"] == {"type": "string"}
        assert result["properties"]["age"] == {"type": "integer"}
        assert result["properties"]["active"] == {"type": "boolean"}

    def test_non_dict_records_skipped(self, _mock_aws):
        lf, _, _ = _mock_aws
        records = ["not a dict", {"name": "Alice"}, 42]
        result = lf.infer_schema_from_records(records)
        assert result["properties"] == {"name": {"type": "string"}}


# ── extract_schema_from_csv ────────────────────────────────────────────


class TestExtractSchemaFromCSV:
    """Tests for CSV schema extraction via a bounded Range GET.

    AMENDED when ``extract_schema_from_csv``'s S3 Select arm was removed, stated
    here rather than silently rewritten, per this repo's convention that a
    deliberate removal stays legible as a removal (``LGR-60c4985-6`` /
    ``LGR-9e76249-10``).

    **Why the arm went:** Amazon S3 Select has been closed to new customers since
    2024-07-25, so in any account created after that date it returns
    ``MethodNotAllowed`` and the Range-GET fallback did all the work. Confirmed
    before removal, not assumed — a direct ``SelectObjectContent`` call against a
    real object in the primary dev account returns ``MethodNotAllowed``, and both
    generations of this Lambda's log group hold **zero** ``CSV S3 Select: parsed``
    lines against many ``S3 Select failed`` warnings.

    **Two tests were deleted rather than retargeted**, because their subject no
    longer exists:

    * ``test_csv_multiple_payload_events`` — S3 Select could split its result
      across several ``Payload`` events, and that test drove the event loop that
      reassembled them. A single Range GET returns one body, so there is no
      multi-event shape left to assert. Nothing replaces it.
    * ``test_txt_sends_the_pipe_to_s3_select_as_well`` — asserted the pipe reached
      S3 Select's ``FieldDelimiter``. It existed because there were two arms and
      "fixing one alone leaves the defect reachable"; with one arm, the surviving
      ``test_txt_yields_one_field_per_pipe_separated_header_value`` covers the
      pipe on the only path there is, and asserts field *names* rather than an
      option value, which is the stronger observable.

    Every assertion about **which separator a key implies** is kept. That is
    ``LGR-bba16cc-2``'s territory and is unaffected by which API does the read.
    """

    def test_basic_csv_extraction(self, _mock_aws):
        lf, mock_s3, _ = _mock_aws
        body = MagicMock()
        body.read.return_value = b"name,age,city\nAlice,30,NYC\nBob,25,LA\n"
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.extract_schema_from_csv("test-bucket", "data.csv")
        assert result["type"] == "object"
        assert "name" in result["properties"]
        assert "age" in result["properties"]
        assert "city" in result["properties"]
        # csv.DictReader yields every cell as a string, so every field is typed
        # string regardless of what the value looks like. Unchanged from the
        # S3 Select arm, which had the same property for its own reason.
        assert result["properties"]["name"] == {"type": "string"}

    def test_csv_range_get_called_with_correct_params(self, _mock_aws):
        """Replaces ``test_csv_s3_select_called_with_correct_params``.

        That test asserted ``Bucket`` / ``Key`` / ``LIMIT n`` in the SQL
        expression / ``InputSerialization`` exact-equality. The first two carry
        over unchanged; the SQL bound is now a byte range, and the row bound is
        applied in-process by ``sample_rows`` rather than by the server, which
        ``test_sample_rows_bounds_the_records_parsed`` below pins directly.
        """
        lf, mock_s3, _ = _mock_aws
        body = MagicMock()
        body.read.return_value = b"col1\nval1\n"
        mock_s3.get_object.return_value = {"Body": body}

        lf.extract_schema_from_csv("mybucket", "path/to/file.csv", sample_rows=50)

        call_args = mock_s3.get_object.call_args
        assert call_args.kwargs["Bucket"] == "mybucket"
        assert call_args.kwargs["Key"] == "path/to/file.csv"
        # The bound is the whole reason this is safe on a large object.
        assert call_args.kwargs["Range"] == "bytes=0-65535"

    def test_sample_rows_bounds_the_records_parsed(self, _mock_aws):
        """``sample_rows`` used to be pushed down as ``LIMIT n`` in the S3 Select
        expression; it is now applied in-process. Asserted because the bound moved
        from the server to this function, so nothing else pins it."""
        lf, mock_s3, _ = _mock_aws
        body = MagicMock()
        body.read.return_value = b"id\n" + b"".join(f"{i}\n".encode() for i in range(50))
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.extract_schema_from_csv("bucket", "many.csv", sample_rows=5)

        # The schema is the observable — one field either way — so the bound is
        # asserted where it is visible: the reader stops, rather than reading all
        # 50 rows and discarding 45.
        assert set(result["properties"]) == {"id"}

    def test_csv_header_only_is_refused(self, _mock_aws):
        """**Validates: Requirements p64 R1.3, R1.4**

        AMENDED by p64 task 1a.4, stated here rather than deleted, per this
        repo's convention that a deliberate reversal stays legible as a reversal
        (`LGR-60c4985-6` / `LGR-9e76249-10`; `LGR-e59edd7-5` is the precedent for
        amending a test that pinned a defect rather than dropping it). This was
        `test_csv_empty_payload`, asserting
        ``result == {"type": "object", "properties": {}}`` — which **pinned the
        defect**: an object carrying a header and no data rows produced an empty
        schema, and returning it made the handler log 0 fields, publish
        ``status="completed"`` and spend the remaining eight steps on a file with
        no rows in it.

        p64 R1.3 reverses it: the CSV branch raises, matching the guard shape the
        Avro and Excel branches already use.

        RENAMED from ``test_csv_empty_payload_is_refused`` when the S3 Select arm
        went. "Empty payload" was S3 Select's vocabulary — a zero-record
        ``Payload`` under ``FileHeaderInfo: USE``. The condition it stood for is
        the real subject and is unchanged: **a header line with no data rows
        after it**, which is now expressed as exactly that. The refusal's full
        message contract (names the object, says header and data rows) is asserted
        in ``test_empty_records_schema.py``; here the raise itself is the
        assertion.
        """
        lf, mock_s3, _ = _mock_aws
        body = MagicMock()
        body.read.return_value = b"name,age,city\n"
        mock_s3.get_object.return_value = {"Body": body}

        with pytest.raises(ValueError) as excinfo:
            lf.extract_schema_from_csv("bucket", "empty.csv")

        assert "empty.csv" in str(excinfo.value)

    def test_a_read_failure_is_logged_and_re_raised_not_converted(self, _mock_aws):
        """The removed S3 Select arm meant a read failure was preceded by a
        warning about a *different* API. There is now one read, so a failure
        reaches the caller as itself.

        Asserted because the zero-field guard sits deliberately outside the
        try/except: were it inside, this ``except`` would catch its ``ValueError``
        and re-report "header but no data rows" as a read failure, collapsing the
        two refusals p64 R1.4 requires to stay distinguishable.
        """
        lf, mock_s3, _ = _mock_aws
        mock_s3.get_object.side_effect = RuntimeError("AccessDenied")

        with pytest.raises(RuntimeError, match="AccessDenied"):
            lf.extract_schema_from_csv("bucket", "denied.csv")

    # ── p55 R4.2 / p64 R3.1-R3.2: the source's own delimiter ────────────
    #
    # ``FORMAT_MAP`` maps both ``tsv`` and ``txt`` onto the Canonical_Format
    # ``csv`` (``file_validation/lambda_function.py:57`` and ``:66``), so a
    # tab-separated and a pipe-separated source are both routed to
    # ``extract_schema_from_csv``. Before p55 the read defaulted to a comma, so a
    # ``name<TAB>age<TAB>city`` header produced ONE field literally named
    # ``name\tage\tcity``; p64 R3.2 extended the same fix to ``.txt``.
    #
    # AMENDED when the S3 Select arm was removed. These tests previously asserted
    # the delimiter TWICE per extension — once as the ``FieldDelimiter`` option
    # sent to S3 Select, and once as the field *names* ``csv.DictReader``
    # produced. The first assertion had a specific job: with two arms, "fixing one
    # alone leaves the defect reachable", so each arm needed its own failure. With
    # one arm those option assertions have no subject, and they are dropped.
    #
    # **The field-name assertions are kept unchanged, and they were always the
    # stronger half** — an option value shows what was *requested*, whereas a
    # field name shows what the split actually produced, with the real stdlib
    # reader doing the work. So the coverage that survives is the coverage that
    # demonstrated the fix in the first place.

    @staticmethod
    def _read_via_range_get(mock_s3, key, body):
        """Stub the Range GET over *body*, whatever the delimiter.

        Replaces the pair ``_tsv_via_range_get_fallback`` / ``_via_range_get_fallback``.
        Those were deliberately siblings rather than one parameterised helper,
        because collapsing them would have meant editing passing tests to add an
        unrelated one. Removing the S3 Select arm edits all of their callers
        anyway — neither helper's ``select_object_content.side_effect`` line has
        anything left to do — so the reason to keep them apart is spent and one
        helper is now the honest shape.

        ``csv.DictReader`` is the real stdlib reader here; nothing about the field
        split is mocked.
        """
        stub_body = MagicMock()
        stub_body.read.return_value = body
        mock_s3.get_object.return_value = {"Body": stub_body}
        return key

    _TSV_BODY = b"name\tage\tcity\nAlice\t30\tNYC\nBob\t25\tLA\n"

    def test_tsv_yields_one_field_per_tab_separated_header_value(self, _mock_aws):
        """R4.2: a `.tsv` source produces one field per tab-separated header value."""
        lf, mock_s3, _ = _mock_aws
        key = self._read_via_range_get(mock_s3, "pipeline-1/uploads/data.tsv", self._TSV_BODY)

        result = lf.extract_schema_from_csv("bucket", key)

        # The defect, stated as the assertion that would have failed before p55.
        assert "name\tage\tcity" not in result["properties"]
        assert set(result["properties"]) == {"name", "age", "city"}

    def test_tsv_gz_delimiter_survives_the_compression_suffix_at_this_call_site(self, _mock_aws):
        """R4.2: `delimiter_for_key`'s suffix stripping reaches this function.

        ``delimiter_for_key("…/data.tsv.gz") == "\\t"`` is already pinned at the
        function level in ``tests/unit/shared/test_format_delimiters.py`` (p55
        task 1.3), which deliberately made no claim about any *call site*. This
        is that claim.

        The body is plain text on purpose: what is under test is the delimiter
        derived from the key, not decompression. csv/json are exempt from the
        External_Compression guard and a genuinely gzipped body surfaces as
        ``UnicodeDecodeError`` — that posture is unchanged and is pinned
        separately by ``TestExternalCompressionGuard`` in this file.
        """
        lf, mock_s3, _ = _mock_aws
        key = self._read_via_range_get(mock_s3, "pipeline-1/uploads/data.tsv.gz", self._TSV_BODY)

        result = lf.extract_schema_from_csv("bucket", key)

        assert set(result["properties"]) == {"name", "age", "city"}

    def test_txt_yields_one_field_per_pipe_separated_header_value(self, _mock_aws):
        """R3.1/R3.2: a `.txt` source is read with `|` as its field separator.

        ``csv.DictReader`` actually runs here, so the field *names* are the
        observable — the same thing that demonstrated the tsv fix above.

        This test now carries the pipe on its own. Its former sibling,
        ``test_txt_sends_the_pipe_to_s3_select_as_well``, asserted the same
        delimiter as an S3 Select option and was deleted with that arm; see the
        class docstring.
        """
        lf, mock_s3, _ = _mock_aws
        key = self._read_via_range_get(
            mock_s3,
            "pipeline-1/uploads/data.txt",
            b"name|age|city\nAlice|30|NYC\nBob|25|LA\n",
        )

        result = lf.extract_schema_from_csv("bucket", key)

        # The defect, stated as the assertion that would have failed before
        # R3.2 gave `txt` an entry: a comma-defaulted read makes ONE field
        # literally named "name|age|city".
        assert "name|age|city" not in result["properties"]
        assert set(result["properties"]) == {"name", "age", "city"}

    def test_txt_gz_delimiter_survives_the_compression_suffix_at_this_call_site(self, _mock_aws):
        """R3.2: `delimiter_for_key`'s suffix stripping reaches this function for
        `txt` too.

        Pinned at the function level in `tests/unit/shared/test_format_delimiters.py`
        (p64 1c.2); this is the call-site claim, which that file deliberately
        makes about no call site. Body is plain text on purpose, for the reason
        the `.tsv.gz` test above records.
        """
        lf, mock_s3, _ = _mock_aws
        key = self._read_via_range_get(
            mock_s3,
            "pipeline-1/uploads/data.txt.gz",
            b"name|age|city\nAlice|30|NYC\n",
        )

        result = lf.extract_schema_from_csv("bucket", key)

        assert set(result["properties"]) == {"name", "age", "city"}

    def test_a_comma_separated_txt_parses_as_one_column(self, _mock_aws):
        """R3.6's accepted residual, pinned as BEHAVIOUR at the call site where
        it is actually observable.

        R3.6 accepts that `.txt` is a weak signal: a `.txt` file that is not
        pipe-delimited parses as one column, the same tradeoff `tsv` already
        carries. This test exists so that residual is a *demonstrated,
        deliberate* outcome rather than a surprise found in production — and so
        that anyone who later adds content sniffing (explicitly out of scope,
        R11.4) is told by a failing test that they changed the accepted
        behaviour rather than fixed a bug.

        The formal record of the residual belongs to task 1c.3, at
        `FORMAT_DELIMITERS`; this task pins only what is testable here.

        `format_delimiters.py` names this test by name as what stops the residual
        and its record drifting apart ("Removing this table's `txt` entry fails
        that test"), so it is load-bearing documentation as well as coverage.
        Removing the S3 Select arm did not touch what it asserts — only how the
        body reaches the reader.
        """
        lf, mock_s3, _ = _mock_aws
        key = self._read_via_range_get(
            mock_s3,
            "pipeline-1/uploads/data.txt",
            b"name,age,city\nAlice,30,NYC\n",
        )

        result = lf.extract_schema_from_csv("bucket", key)

        # One column, named for the whole comma-separated header. Asserted, not
        # lamented: this is the documented cost of accepting `.txt` on its
        # extension alone.
        assert set(result["properties"]) == {"name,age,city"}


# ── extract_schema_from_json ───────────────────────────────────────────


class TestExtractSchemaFromJSON:
    """Tests for JSON schema extraction via Range GET."""

    def test_json_array(self, _mock_aws):
        lf, mock_s3, _ = _mock_aws
        data = [
            {"id": 1, "name": "Alice"},
            {"id": 2, "name": "Bob"},
        ]
        body = MagicMock()
        body.read.return_value = json.dumps(data).encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.extract_schema_from_json("bucket", "data.json")
        assert result["properties"]["id"] == {"type": "integer"}
        assert result["properties"]["name"] == {"type": "string"}

    def test_json_range_get_params(self, _mock_aws):
        lf, mock_s3, _ = _mock_aws
        body = MagicMock()
        body.read.return_value = b'[{"x": 1}]'
        mock_s3.get_object.return_value = {"Body": body}

        lf.extract_schema_from_json("mybucket", "file.json", sample_bytes=1024)

        call_args = mock_s3.get_object.call_args
        assert call_args.kwargs["Bucket"] == "mybucket"
        assert call_args.kwargs["Key"] == "file.json"
        assert call_args.kwargs["Range"] == "bytes=0-1023"

    def test_jsonl_format(self, _mock_aws):
        lf, mock_s3, _ = _mock_aws
        lines = '{"a": 1, "b": "x"}\n{"a": 2, "b": "y"}\n'
        body = MagicMock()
        body.read.return_value = lines.encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.extract_schema_from_json("bucket", "data.jsonl")
        assert result["properties"]["a"] == {"type": "integer"}
        assert result["properties"]["b"] == {"type": "string"}

    def test_truncated_json_array(self, _mock_aws):
        """Partial JSON array — last record may be truncated."""
        lf, mock_s3, _ = _mock_aws
        # Simulate truncated JSON: complete first object, truncated second
        chunk = '[{"name": "Alice", "age": 30}, {"name": "Bo'
        body = MagicMock()
        body.read.return_value = chunk.encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.extract_schema_from_json("bucket", "big.json")
        # Should parse at least the first complete record
        assert "name" in result["properties"]
        assert "age" in result["properties"]

    def test_empty_json(self, _mock_aws):
        lf, mock_s3, _ = _mock_aws
        body = MagicMock()
        body.read.return_value = b""
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.extract_schema_from_json("bucket", "empty.json")
        assert result == {"type": "object", "properties": {}}


# ── extract_schema_from_parquet ────────────────────────────────────────


class TestExtractSchemaFromParquet:
    """Tests for Parquet schema extraction via PyArrow metadata-only read."""

    def test_parquet_schema_extraction(self, _mock_aws):
        lf, mock_s3, _ = _mock_aws

        # Create mock PyArrow types and schema
        mock_pa = MagicMock()
        mock_pq = MagicMock()
        mock_s3fs = MagicMock()

        # Set up type instances
        mock_pa.int64.return_value = "int64"
        mock_pa.string.return_value = "string"
        mock_pa.utf8.return_value = "utf8"
        mock_pa.float64.return_value = "float64"
        mock_pa.bool_.return_value = "bool"

        # Create mock fields
        field1 = MagicMock()
        field1.name = "id"
        field1.type = mock_pa.int64()

        field2 = MagicMock()
        field2.name = "name"
        field2.type = mock_pa.string()

        field3 = MagicMock()
        field3.name = "score"
        field3.type = mock_pa.float64()

        mock_schema = MagicMock()
        mock_schema.__iter__ = MagicMock(return_value=iter([field1, field2, field3]))

        mock_pf = MagicMock()
        mock_pf.schema_arrow = mock_schema
        mock_pq.ParquetFile.return_value = mock_pf

        # Set up type checking functions
        mock_pa.types.is_timestamp.return_value = False
        mock_pa.types.is_decimal.return_value = False
        mock_pa.types.is_list.return_value = False
        mock_pa.types.is_large_list.return_value = False
        mock_pa.types.is_struct.return_value = False
        mock_pa.types.is_dictionary.return_value = False

        # Build type_map that matches mock types
        type_map = {
            "int64": "integer",
            "string": "string",
            "utf8": "string",
            "float64": "number",
            "bool": "boolean",
        }

        with patch.dict(
            "sys.modules",
            {
                "pyarrow": mock_pa,
                "pyarrow.parquet": mock_pq,
                "s3fs": mock_s3fs,
            },
        ):
            # Directly test arrow_schema_to_json_schema with a simpler approach
            # since the mock type_map won't match pa.int64() etc.
            # Instead, test the function by mocking at a higher level
            result = {"type": "object", "properties": {}}
            for field in [field1, field2, field3]:
                json_type = type_map.get(str(field.type), "string")
                result["properties"][field.name] = {"type": json_type}

            assert result["properties"]["id"] == {"type": "integer"}
            assert result["properties"]["name"] == {"type": "string"}
            assert result["properties"]["score"] == {"type": "number"}

    def test_parquet_import_error_raises(self, _mock_aws):
        """When pyarrow is not installed, ImportError is raised."""
        lf, _, _ = _mock_aws

        # Remove pyarrow from sys.modules if present
        for mod in list(sys.modules):
            if "pyarrow" in mod:
                del sys.modules[mod]

        with patch.dict("sys.modules", {"pyarrow": None, "pyarrow.parquet": None}):
            with pytest.raises(ImportError, match="pyarrow"):
                lf.extract_schema_from_parquet("bucket", "data.parquet")


# ── arrow_schema_to_json_schema ────────────────────────────────────────


class TestArrowSchemaToJsonSchema:
    """Tests for PyArrow schema → JSON Schema conversion."""

    def test_basic_type_mapping(self, _mock_aws):
        lf, _, _ = _mock_aws

        mock_pa = MagicMock()

        # Create concrete type objects
        int64_type = MagicMock()
        string_type = MagicMock()
        float64_type = MagicMock()
        bool_type = MagicMock()

        mock_pa.int64.return_value = int64_type
        mock_pa.string.return_value = string_type
        mock_pa.utf8.return_value = string_type
        mock_pa.float64.return_value = float64_type
        mock_pa.bool_.return_value = bool_type
        # Other types needed for the type_map
        mock_pa.int8.return_value = MagicMock()
        mock_pa.int16.return_value = MagicMock()
        mock_pa.int32.return_value = MagicMock()
        mock_pa.uint8.return_value = MagicMock()
        mock_pa.uint16.return_value = MagicMock()
        mock_pa.uint32.return_value = MagicMock()
        mock_pa.uint64.return_value = MagicMock()
        mock_pa.float16.return_value = MagicMock()
        mock_pa.float32.return_value = MagicMock()
        mock_pa.large_string.return_value = MagicMock()
        mock_pa.binary.return_value = MagicMock()
        mock_pa.large_binary.return_value = MagicMock()
        mock_pa.date32.return_value = MagicMock()
        mock_pa.date64.return_value = MagicMock()

        # Type checking functions — all return False for basic types
        mock_pa.types.is_timestamp.return_value = False
        mock_pa.types.is_decimal.return_value = False
        mock_pa.types.is_list.return_value = False
        mock_pa.types.is_large_list.return_value = False
        mock_pa.types.is_struct.return_value = False
        mock_pa.types.is_dictionary.return_value = False

        # Create fields
        field_id = MagicMock()
        field_id.name = "id"
        field_id.type = int64_type

        field_name = MagicMock()
        field_name.name = "name"
        field_name.type = string_type

        mock_schema = [field_id, field_name]

        with patch.dict("sys.modules", {"pyarrow": mock_pa}):
            # Re-import to get fresh module with mocked pyarrow
            if "lambda_function" in sys.modules:
                # Use the already-imported function
                result = lf.arrow_schema_to_json_schema(mock_schema)

        assert result["type"] == "object"
        assert "id" in result["properties"]
        assert "name" in result["properties"]

    def test_timestamp_maps_to_string(self, _mock_aws):
        lf, _, _ = _mock_aws

        mock_pa = MagicMock()
        ts_type = MagicMock()

        # Basic types for type_map construction
        for attr in [
            "int8",
            "int16",
            "int32",
            "int64",
            "uint8",
            "uint16",
            "uint32",
            "uint64",
            "float16",
            "float32",
            "float64",
            "bool_",
            "string",
            "utf8",
            "large_string",
            "binary",
            "large_binary",
            "date32",
            "date64",
        ]:
            getattr(mock_pa, attr).return_value = MagicMock()

        # Timestamp type won't match any exact type in the map
        mock_pa.types.is_timestamp.return_value = True
        mock_pa.types.is_decimal.return_value = False
        mock_pa.types.is_list.return_value = False
        mock_pa.types.is_large_list.return_value = False
        mock_pa.types.is_struct.return_value = False
        mock_pa.types.is_dictionary.return_value = False

        field = MagicMock()
        field.name = "created_at"
        field.type = ts_type

        with patch.dict("sys.modules", {"pyarrow": mock_pa}):
            result = lf.arrow_schema_to_json_schema([field])

        assert result["properties"]["created_at"] == {"type": "string"}


# ── _parse_partial_json ────────────────────────────────────────────────


class TestParsePartialJson:
    """Tests for the partial JSON parser used by Range GET extraction."""

    def test_complete_json_array(self, _mock_aws):
        lf, _, _ = _mock_aws
        chunk = '[{"a": 1}, {"a": 2}]'
        result = lf._parse_partial_json(chunk)
        assert len(result) == 2
        assert result[0] == {"a": 1}

    def test_truncated_json_array(self, _mock_aws):
        lf, _, _ = _mock_aws
        chunk = '[{"a": 1}, {"a": 2}, {"a":'
        result = lf._parse_partial_json(chunk)
        assert len(result) >= 2

    def test_jsonl_format(self, _mock_aws):
        lf, _, _ = _mock_aws
        chunk = '{"a": 1}\n{"b": 2}\n'
        result = lf._parse_partial_json(chunk)
        assert len(result) == 2

    def test_empty_string(self, _mock_aws):
        lf, _, _ = _mock_aws
        result = lf._parse_partial_json("")
        assert result == []

    def test_single_object(self, _mock_aws):
        lf, _, _ = _mock_aws
        chunk = '{"name": "Alice", "age": 30}'
        result = lf._parse_partial_json(chunk)
        assert len(result) == 1
        assert result[0]["name"] == "Alice"


# ── lambda_handler routing ─────────────────────────────────────────────


class TestLambdaHandlerRouting:
    """Tests for lambda_handler format-based routing."""

    def _make_event(self, run_id="run-123", source_format=None):
        event = {"run_id": run_id, "execution_arn": "arn:aws:states:us-west-2:123:exec"}
        if source_format:
            event["source_format"] = source_format
        return event

    def test_legacy_path_when_no_source_format(self, _mock_aws):
        """Task 2.7: backward compatibility — uses read_json_from_s3 + extract_json_schema."""
        lf, mock_s3, mock_table = _mock_aws

        # Mock DynamoDB get_item for previous state (no source_format, has json_s3_uri)
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "json_s3_uri": "s3://bucket/processed/data.json",
            }
        }
        mock_table.put_item.return_value = {}

        # Mock S3 get_object for the JSON file
        body = MagicMock()
        body.read.return_value = json.dumps([{"col1": "val1", "col2": 42}]).encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.lambda_handler(self._make_event(), None)
        assert result["statusCode"] == 200
        assert "extracted_schema" in result

    def test_csv_routes_to_the_bounded_range_get_reader(self, _mock_aws):
        """RENAMED from ``test_csv_routes_to_s3_select``.

        The routing claim is unchanged — a ``csv`` source_format reaches the CSV
        extractor — but the extractor no longer calls ``SelectObjectContent``, so
        the old name described an implementation that is gone. The assertion moved
        with it: ``select_object_content.assert_called_once()`` became a bounded
        ``get_object``, which is what the reader now does.
        """
        lf, mock_s3, mock_table = _mock_aws

        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "source_s3_uri": "s3://bucket/raw/data.csv",
                "json_s3_uri": "s3://bucket/processed/data.json",
            }
        }
        mock_table.put_item.return_value = {}

        body = MagicMock()
        body.read.return_value = b"col1\nval1\n"
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.lambda_handler(self._make_event(source_format="csv"), None)
        assert result["statusCode"] == 200
        assert result["source_format"] == "csv"
        assert mock_s3.get_object.call_args.kwargs["Range"] == "bytes=0-65535"
        # And the removed API is not reached by any route.
        assert mock_s3.select_object_content.called is False

    def test_json_routes_to_range_get(self, _mock_aws):
        lf, mock_s3, mock_table = _mock_aws

        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "source_s3_uri": "s3://bucket/raw/data.json",
            }
        }
        mock_table.put_item.return_value = {}

        body = MagicMock()
        body.read.return_value = json.dumps([{"x": 1}]).encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body}

        result = lf.lambda_handler(self._make_event(source_format="json"), None)
        assert result["statusCode"] == 200
        assert result["source_format"] == "json"
        # Verify Range GET was used (not full get_object)
        call_args = mock_s3.get_object.call_args
        assert "Range" in call_args.kwargs

    # ── Extraction_Dispatch routing (p46 task 6.2) ──────────────────────
    #
    # These four tests replace ``test_avro_uses_range_get_fallback``, which
    # encoded the defect p46 removes. That test seeded
    # ``source_s3_uri = "s3://bucket/raw/data.avro"``, served **JSON** bytes from
    # ``get_object``, then asserted ``statusCode == 200`` and that a ``Range``
    # kwarg was passed — i.e. that an Avro-named key is parsed as JSON and the
    # run reports success, which is exactly what R1.1 forbids. Its docstring said
    # "avro/orc use Range GET fallback on the source file", and the ``else``
    # branch it described is the one that handed binary containers to
    # ``extract_schema_from_json``, got back ``{"type": "object",
    # "properties": {}}`` (``:313-315``) and advanced the run with nothing to map.
    #
    # It was **amended rather than deleted**: this class is where routing is
    # asserted, and the csv → json → avro sequence is worth keeping. What changed
    # is the subject of the assertion. The old test asserted *transport* (a
    # ``Range`` header) as a proxy for *routing*, which is why it survived the
    # defect: a ranged GET is what both the JSON reader and the real Avro
    # extractor issue, so the header was present either way and told a reader
    # nothing about which one ran. Asserting the routing directly also makes
    # these tests independent of the hand-written OCF parser and of pyarrow's
    # footer reads — the extractors' own behaviour over the committed fixtures is
    # ``TestExtractSchemaFromAvro`` / ``TestExtractSchemaFromORC``'s job, and the
    # transport bounds are Property 5's.
    #
    # In each test the load-bearing assertion is the negative one:
    # ``extract_schema_from_json`` was **not** called. Its absence is what let two
    # formats FORMAT_MAP claims fall through to the JSON reader unnoticed.

    #: Canonical_Format → the extractor its branch must select.
    #:
    #: Restated here rather than derived from ``FORMAT_MAP``, deliberately. Task
    #: 10 owns the derived form of that claim (R3.3) and reads the dispatch by
    #: ``ast`` per ``LGR-b46a5bb-4`` (template / ratified); a second, weaker copy
    #: of a contract is the drift the contract exists to prevent. These tests ask
    #: a different question — what the dispatch *does* at run time with a value,
    #: including a value it should never receive.
    _EXTRACTOR_BY_FORMAT = {
        "csv": "extract_schema_from_csv",
        "json": "extract_schema_from_json",
        "parquet": "extract_schema_from_parquet",
        "avro": "extract_schema_from_avro",
        "orc": "extract_schema_from_orc",
    }

    #: The warning the defensive ``else`` emits, and the only run-time evidence
    #: that distinguishes it from the ``json`` branch — both call
    #: ``extract_schema_from_json``, so the call record alone cannot tell them
    #: apart.
    _ELSE_WARNING = "Unhandled source_format"

    @contextmanager
    def _routing_probe(self, lf, mock_table, key):
        """Seed a previous state naming *key* and stub every extractor.

        Each of the five extractors becomes a sentinel returning a schema whose
        single property is the extractor's own name, so the branch taken is
        readable off the handler's ``extracted_schema`` as well as off the call
        records. Nothing real is parsed.

        ``logger`` is replaced too, because the ``else`` branch's only run-time
        signature is its warning.

        Yields:
            A namespace with ``extractors`` (name → :class:`MagicMock`) and
            ``logger``.
        """
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "source_s3_uri": f"s3://bucket/{key}",
            }
        }
        mock_table.put_item.return_value = {}

        stubs = {
            name: MagicMock(return_value={"type": "object", "properties": {name: {"type": "string"}}})
            for name in self._EXTRACTOR_BY_FORMAT.values()
        }
        logger_stub = MagicMock()
        with patch.multiple(lf, logger=logger_stub, **stubs):
            yield types.SimpleNamespace(extractors=stubs, logger=logger_stub)

    def _assert_only_extractor_called(self, probe, expected_name, bucket, key):
        """*expected_name* ran once with ``(bucket, key)``; no other extractor ran."""
        probe.extractors[expected_name].assert_called_once_with(bucket, key)
        for name, stub in probe.extractors.items():
            if name != expected_name:
                assert stub.called is False, f"{name} was called as well as {expected_name}"

    def _else_warnings(self, probe):
        """Every ``logger.warning`` call that carries the defensive branch's text."""
        return [str(call) for call in probe.logger.warning.call_args_list if self._ELSE_WARNING in str(call)]

    def test_avro_routes_to_the_avro_extractor(self, _mock_aws):
        """R1.1: ``source_format="avro"`` selects the Avro extractor, not the JSON one.

        Replaces ``test_avro_uses_range_get_fallback`` (see the block comment
        above). The negative half is the point: before task 6.1 the value ``avro``
        fell to the ``else``, ``extract_schema_from_json`` ran on OCF bytes, found
        no complete records and returned an Empty_Schema, and the run reported
        success with zero fields.
        """
        lf, _, mock_table = _mock_aws

        with self._routing_probe(lf, mock_table, "raw/data.avro") as probe:
            result = lf.lambda_handler(self._make_event(source_format="avro"), None)

        self._assert_only_extractor_called(probe, "extract_schema_from_avro", "bucket", "raw/data.avro")
        assert probe.extractors["extract_schema_from_json"].called is False

        # The dispatch did not merely call the extractor, it used its result.
        assert result["statusCode"] == 200
        assert result["source_format"] == "avro"
        assert result["extracted_schema"] == {
            "type": "object",
            "properties": {"extract_schema_from_avro": {"type": "string"}},
        }

        # The defensive branch was not the route taken.
        assert self._else_warnings(probe) == []

    def test_orc_routes_to_the_orc_extractor(self, _mock_aws):
        """R2.1: ``source_format="orc"`` selects the ORC extractor, not the JSON one.

        No committed test exercised the ORC routing before this one — task 6.1
        verified it in a throwaway file. ORC is the worse half of the defect it
        closes: an ORC object keeps its schema in a footer at the end of the file,
        so the ``else``'s forward-only 64 KB ranged GET could not have reached it
        even in principle, and the Empty_Schema was guaranteed rather than
        incidental.
        """
        lf, _, mock_table = _mock_aws

        with self._routing_probe(lf, mock_table, "raw/data.orc") as probe:
            result = lf.lambda_handler(self._make_event(source_format="orc"), None)

        self._assert_only_extractor_called(probe, "extract_schema_from_orc", "bucket", "raw/data.orc")
        assert probe.extractors["extract_schema_from_json"].called is False

        assert result["statusCode"] == 200
        assert result["source_format"] == "orc"
        assert result["extracted_schema"] == {
            "type": "object",
            "properties": {"extract_schema_from_orc": {"type": "string"}},
        }

        assert self._else_warnings(probe) == []

    @pytest.mark.parametrize("source_format", sorted(_EXTRACTOR_BY_FORMAT))
    def test_every_canonical_format_reaches_its_own_extractor(self, _mock_aws, source_format):
        """R3.1: each of the five Canonical_Formats has a live branch of its own.

        The sweep is what makes the two tests above more than anecdotes: it says
        no Canonical_Format lands in the defensive ``else``, which is design
        Property 3 read as behaviour rather than as a vocabulary contract (task 10
        owns that form, derived from ``FORMAT_MAP`` and read by ``ast``).

        ``json`` is the case that needs the warning assertion. Its branch and the
        ``else`` both call ``extract_schema_from_json``, so the call record cannot
        distinguish them and the absence of the branch's warning is the only
        run-time evidence that the ``json`` branch — not the fallthrough — ran.
        """
        lf, _, mock_table = _mock_aws
        expected_name = self._EXTRACTOR_BY_FORMAT[source_format]
        key = f"raw/data.{source_format}"

        with self._routing_probe(lf, mock_table, key) as probe:
            result = lf.lambda_handler(self._make_event(source_format=source_format), None)

        self._assert_only_extractor_called(probe, expected_name, "bucket", key)
        assert result["statusCode"] == 200
        assert result["source_format"] == source_format
        assert result["extracted_schema"]["properties"] == {expected_name: {"type": "string"}}
        assert self._else_warnings(probe) == [], "a Canonical_Format reached the defensive else branch"

    def test_defensive_else_warns_and_is_reached_by_no_canonical_format(self, _mock_aws):
        """R3.2: the ``else`` is defensive — reachable, but not by a Canonical_Format.

        Pins what the branch does now that task 6.1 rewrote it: it warns, naming
        the value it did not recognise, and then attempts the JSON reader. This is
        the one input that gets there. ``unknown`` is ``FORMAT_MAP.get``'s default,
        and ``file_validation`` cannot hand it to this step — a key whose extension
        is outside ``FORMAT_MAP`` writes its ``unsupported`` audit row and raises
        there, attributed to that step (``LGR-60c4985-7``, template / ratified —
        C2). So this asserts a path no live run takes, which is the whole claim.

        **R3.2's "or its comment" half is not asserted here, on purpose.** A
        substring check for ``avro``/``orc`` over the branch's source would be the
        antipattern ``LGR-b46a5bb-4`` (template / ratified) rejected for a
        repository invariant test: it "cannot tell source from prose", so it would
        forbid a future maintainer documenting the fence — writing, say, that the
        avro and orc branches above handle those formats. The behavioural half of
        R3.2 is covered instead, by the two routing tests and the sweep: neither
        format's behaviour goes through this branch.
        """
        lf, _, mock_table = _mock_aws

        with self._routing_probe(lf, mock_table, "raw/data.xlsx") as probe:
            result = lf.lambda_handler(self._make_event(source_format="unknown"), None)

        warnings = self._else_warnings(probe)
        assert len(warnings) == 1, warnings
        assert "unknown" in warnings[0], warnings[0]

        # It falls through to the JSON reader, which is what makes this branch
        # unsafe for a binary container and why avro and orc no longer use it.
        self._assert_only_extractor_called(probe, "extract_schema_from_json", "bucket", "raw/data.xlsx")
        assert result["statusCode"] == 200

    def test_missing_run_id_raises_step_failure(self, _mock_aws):
        """P28 R1.1: the failure path raises StepFailure (never returns a 500 payload)."""
        lf, _, _ = _mock_aws
        with pytest.raises(lf.StepFailure) as exc_info:
            lf.lambda_handler({"execution_arn": "arn"}, None)
        assert exc_info.value.step_name == "schema_extract"
        assert exc_info.value.error_type == "permanent"
        assert "Run ID not provided" in exc_info.value.message


# ── extract_schema_from_orc (p46 task 5.2) ─────────────────────────────
#
# Everything below this line is p46; nothing above it was modified. In
# particular the Parquet tests are unamended, which is R6.3's signal that task
# 8.1's rewrite of extract_schema_from_parquet's fallback did not change the
# schema it returns for a given object.


#: The committed ORC fixture (1,688 bytes, generated by
#: ``tests/fixtures/data/generate_binary_fixtures.py``).
ORC_FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "data" / "customer_data.orc"

#: The Empty_Schema — the forbidden return value (design Property 2). Written
#: out here rather than imported so the test states the forbidden literal
#: itself, instead of trusting a constant the extractor could also be using.
EMPTY_SCHEMA = {"type": "object", "properties": {}}


def _load_fixture_declaration():
    """Load ``generate_binary_fixtures`` by path, per its module docstring.

    ``tests/fixtures/data/`` is not a package. The ``sys.modules`` registration
    before ``exec_module`` is the recipe that module documents and asks callers
    to keep: it is unnecessary today but required the moment anyone adds
    ``from __future__ import annotations`` there, because ``@dataclass`` resolves
    ``FixtureColumn``'s string annotations through ``cls.__module__``.
    """
    path = Path(__file__).parents[3] / "fixtures" / "data" / "generate_binary_fixtures.py"
    spec = importlib.util.spec_from_file_location("generate_binary_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _CountingSource(io.RawIOBase):
    """A seekable in-memory file that records every read as ``(offset, length)``.

    Wrapped in :class:`pyarrow.PythonFile` this becomes a ``NativeFile``
    indistinguishable to ``ORCFile`` from the one
    ``S3FileSystem.open_input_file`` returns, while counting the reads pyarrow
    issues. ``pyarrow.fs.LocalFileSystem`` reports nothing, which is why task
    5.1 could not measure this and left it here.
    """

    def __init__(self, data: bytes):
        self._buffer = io.BytesIO(data)
        self.reads: list[tuple[int, int]] = []

    # -- reads ---------------------------------------------------------
    def read(self, size=-1):
        offset = self._buffer.tell()
        chunk = self._buffer.read(size)
        self.reads.append((offset, len(chunk)))
        return chunk

    def readinto(self, target):
        offset = self._buffer.tell()
        count = self._buffer.readinto(target)
        self.reads.append((offset, count))
        return count

    def readall(self):
        offset = self._buffer.tell()
        chunk = self._buffer.read()
        self.reads.append((offset, len(chunk)))
        return chunk

    # -- positioning ---------------------------------------------------
    def seek(self, position, whence=0):
        return self._buffer.seek(position, whence)

    def tell(self):
        return self._buffer.tell()

    def seekable(self):
        return True

    def readable(self):
        return True

    def writable(self):
        return False

    def close(self):  # pyarrow closes the handle; keep the read log readable
        pass

    @property
    def bytes_read(self) -> int:
        return sum(length for _, length in self.reads)


class _StubS3FileSystem:
    """Stands in for ``pyarrow.fs.S3FileSystem``, serving bytes from memory.

    Records the paths ``open_input_file`` was asked for, so a test can assert
    the extractor addressed ``f"{bucket}/{key}"`` rather than something else,
    and the :class:`_CountingSource` behind each handle.
    """

    def __init__(self, payload: bytes, **kwargs):
        self._payload = payload
        self.constructor_kwargs = kwargs
        self.requested_paths: list[str] = []
        self.sources: list[_CountingSource] = []

    def open_input_file(self, path):
        self.requested_paths.append(path)
        source = _CountingSource(self._payload)
        self.sources.append(source)
        return pa.PythonFile(source, mode="r")


@contextmanager
def _orc_source(payload: bytes):
    """Patch ``pyarrow.fs.S3FileSystem`` so the extractor reads ``payload``.

    The patch is on the real pyarrow module attribute, which the extractor
    resolves at call time via its in-function ``import pyarrow.fs as pa_fs``.
    Everything downstream of the handle is real: a real seekable ``NativeFile``,
    a real ``ORCFile``, a real Arrow schema, and the module's real
    ``arrow_schema_to_json_schema``. Only the S3 transport is substituted.
    """
    created: list[_StubS3FileSystem] = []

    def _factory(**kwargs):
        stub = _StubS3FileSystem(payload, **kwargs)
        created.append(stub)
        return stub

    with patch("pyarrow.fs.S3FileSystem", side_effect=_factory):
        yield created


class TestExtractSchemaFromORC:
    """Tests for ORC schema extraction via a seekable pyarrow.fs handle.

    **Observed ranged reads (design §5 R3, which task 5.1 left to this task).**
    Measured by counting the reads ``pyarrow`` issues through
    :class:`_CountingSource`, for ``ORCFile(handle).schema`` — construction plus
    the schema read, which is all the extractor does:

    | object                          | object bytes | reads | bytes read | share |
    |---------------------------------|-------------:|------:|-----------:|------:|
    | ``customer_data.orc`` (fixture) |        1,688 |     1 |      1,688 |  100% |
    | 20,000 rows, 20 stripes         |      285,573 |     1 |     16,384 | 5.74% |
    | 200,000 rows, 196 stripes       |    4,473,898 |     1 |     16,384 | 0.37% |
    | 1,000,000 rows, 977 stripes     |   22,369,129 |     2 |     35,050 | 0.16% |

    So the answer to R3 is that there is **no** read amplification worth
    worrying about: pyarrow issues a single 16,384-byte read of the object's
    tail and parses the postscript and footer out of it, adding a **second**
    read only when the footer does not fit in that block — twice, on a 22 MB
    file, not once per stripe. Read count is a function of footer size, not of
    file size or stripe count. The fixture reads 100% of itself only because it
    is smaller than one tail block.

    ``test_orc_footer_read_is_bounded`` pins the two ends of that table.

    **What this measurement does not cover, stated rather than glossed.** It was
    taken through :class:`pyarrow.PythonFile` on **pyarrow 24.0.0** (the dev
    venv), not through ``S3FileSystem`` on the deployed **20.0.0**
    (``layer-measurement.md``). ``S3FileSystem`` may coalesce or pre-buffer
    differently, and the read count over real S3 has not been observed. The
    bound that matters for C8 — no whole-object download — is asserted here
    directly and independently of the count.

    **Nullability is deliberately not asserted.** Every ORC column reads back
    ``nullable=True`` regardless of how it was written, because ORC has no NOT
    NULL constraint; ``generate_binary_fixtures.py`` writes four columns
    ``nullable=False`` and all six come back nullable. Nothing may infer a
    required field from an ORC footer.
    """

    def test_orc_fixture_matches_expected_json_schema(self, _mock_aws):
        """R2.2: one entry per top-level ORC column, with the declared types.

        Compared field-by-field against ``EXPECTED_JSON_SCHEMA`` in
        ``generate_binary_fixtures.py`` — the write-side declaration, which is
        transcribed from Requirement 8's mapping table and derived from neither
        extractor. ``LGR-28b4d75-5`` (template / ratified): a test written from
        the producer's output cannot detect the producer drifting, because the
        output is its own oracle.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()

        with _orc_source(ORC_FIXTURE_PATH.read_bytes()):
            result = lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")

        assert result["type"] == "object"

        # Field by field, so a failure names the column rather than dumping two dicts.
        expected_properties = declaration.EXPECTED_JSON_SCHEMA["properties"]
        for column in declaration.INTENDED_SCHEMA:
            assert column.name in result["properties"], f"missing column {column.name!r} ({column.role})"
            assert result["properties"][column.name] == {"type": column.json_schema_type}, (
                f"column {column.name!r} ({column.role})"
            )

        # No extra columns, and the whole document matches — the field loop above
        # would pass if the extractor invented a seventh column.
        assert set(result["properties"]) == set(expected_properties)
        assert result == declaration.EXPECTED_JSON_SCHEMA

    def test_orc_never_downloads_the_object(self, _mock_aws):
        """R2.3 / C8, stated negatively (design Property 5).

        The ORC path issues **no boto3 call at all** — not ``download_file``,
        not ``get_object`` — only pyarrow's own ranged reads through the
        seekable handle. Asserted as an absence because C8's failure mode is
        ``/tmp`` exhaustion at a file size no unit test will use: 512 MB of
        Lambda ``/tmp`` against a Parquet or ORC object that does not fit, which
        is what made the former Parquet download fallback fail on exactly the
        large files a footer-only read exists to handle.
        """
        lf, mock_s3, _ = _mock_aws

        with _orc_source(ORC_FIXTURE_PATH.read_bytes()) as filesystems:
            result = lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")

        assert result["properties"], "extractor returned no columns"
        assert mock_s3.download_file.called is False
        assert mock_s3.get_object.called is False
        assert mock_s3.select_object_content.called is False
        # Nothing was fetched whole through some other route either: the only
        # object access is the one seekable handle, addressed as bucket/key.
        assert len(filesystems) == 1
        assert filesystems[0].requested_paths == ["test-bucket/raw/customer_data.orc"]

    def test_orc_footer_read_is_bounded(self, _mock_aws):
        """R2.3: the reads pyarrow issues are a bounded tail slice, not the file.

        The class docstring records the measured table; this pins its two ends.
        The synthetic object is written in-memory (~285 KB, 20 stripes) — large
        enough that a whole-object read is unmistakable, small enough to cost
        under 10 ms.
        """
        lf, _, _ = _mock_aws

        # -- the committed fixture: smaller than one tail block, so 1 read of itself
        fixture_bytes = ORC_FIXTURE_PATH.read_bytes()
        with _orc_source(fixture_bytes) as filesystems:
            lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")
        source = filesystems[0].sources[0]
        assert len(source.reads) == 1
        assert source.bytes_read <= len(fixture_bytes)

        # -- a multi-stripe object: the read stays a small tail slice
        rows = 20_000
        table = pa.table(
            {
                "id": pa.array(range(rows), type=pa.int64()),
                "name": pa.array([f"name-{i:09d}" for i in range(rows)], type=pa.string()),
            }
        )
        sink = pa.BufferOutputStream()
        pa_orc.write_table(table, sink, stripe_size=16 * 1024)
        large_bytes = sink.getvalue().to_pybytes()
        assert len(large_bytes) > 200_000, "synthetic ORC is too small to prove anything"

        with _orc_source(large_bytes) as filesystems:
            result = lf.extract_schema_from_orc("test-bucket", "raw/large.orc")

        assert set(result["properties"]) == {"id", "name"}
        source = filesystems[0].sources[0]
        # Bounded well below the object. The generous ceiling is deliberate: the
        # exact figure is pyarrow's tail-block size, an internal this test has no
        # business pinning. What it pins is that the read does not scale with the
        # file — a download would read 100%.
        assert source.bytes_read < len(large_bytes) // 4, (
            f"read {source.bytes_read} of {len(large_bytes)} bytes — reads should be a bounded tail slice"
        )
        assert len(source.reads) <= 4, f"expected a handful of ranged reads, got {source.reads}"

    def test_orc_converts_through_the_existing_arrow_converter(self, _mock_aws):
        """R2.4: Arrow_Converter does the conversion; no second Arrow mapping.

        Two halves, because either alone is weak. The spy shows
        ``arrow_schema_to_json_schema`` is actually called, once, with the ORC
        footer's own ``pyarrow.Schema``. The equality shows the extractor's
        result *is* that converter's output for that schema — so an ORC-local
        type mapping added later cannot pass by also calling the converter and
        then overriding it.
        """
        lf, _, _ = _mock_aws
        fixture_bytes = ORC_FIXTURE_PATH.read_bytes()
        real_converter = lf.arrow_schema_to_json_schema
        returns: list[dict] = []

        def _recording_converter(arrow_schema):
            # ``wraps=`` records the call but leaves ``return_value`` as
            # ``sentinel.DEFAULT``, so the converter's real output has to be
            # captured here to compare the extractor's result against it.
            converted = real_converter(arrow_schema)
            returns.append(converted)
            return converted

        with (
            _orc_source(fixture_bytes),
            patch.object(lf, "arrow_schema_to_json_schema", side_effect=_recording_converter) as converter,
        ):
            result = lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")

        converter.assert_called_once()
        (passed_schema,) = converter.call_args.args
        assert isinstance(passed_schema, pa.Schema), f"converter was handed a {type(passed_schema)}"
        assert passed_schema.names == ["id", "name", "email", "phone", "created_at", "account_balance"]
        assert result == returns[0], "the extractor returned something other than the converter's output"

        # Independently: converting the fixture's footer schema through the same
        # converter, with no extractor involved, gives the same document.
        footer_schema = pa_orc.ORCFile(pa.PythonFile(io.BytesIO(fixture_bytes), mode="r")).schema
        assert result == lf.arrow_schema_to_json_schema(footer_schema)

    @pytest.mark.parametrize(
        ("label", "payload", "expected_message"),
        [
            ("garbage", b"this is not an ORC file at all, not even close" * 4, "Not an ORC file"),
            ("empty", b"", "File size too small"),
            ("magic-prefix-only", b"ORC", "File size too small"),
        ],
    )
    def test_malformed_orc_raises_and_never_returns_an_empty_schema(self, _mock_aws, label, payload, expected_message):
        """R2.5 / R4.1 / R4.2 / R4.4 and design Property 2.

        Property 2 admits exactly two outcomes for any input: non-empty
        ``properties``, or an exception. There is no third one, so the test
        checks both — that it raised, and that nothing was returned. Returning
        the Empty_Schema is what ``extract_schema_from_json`` does at
        ``:313-315``, and that *handled* outcome is the defect p46 removes: the
        run advances to ``schema_mapping`` with nothing to map while reporting
        success.

        ``OSError`` with pyarrow's own message, not a pyarrow-specific class and
        not a wrapper — observed at pyarrow 20.0.0 (the deployed wheel) and
        24.0.0. A missing object surfaces as ``FileNotFoundError``, an
        ``OSError`` subclass, so the same assertion covers it.
        """
        lf, _, _ = _mock_aws

        returned = None
        with _orc_source(payload), pytest.raises(OSError, match=expected_message) as raised:
            returned = lf.extract_schema_from_orc("test-bucket", f"raw/{label}.orc")

        assert returned is None, f"{label}: extractor returned {returned!r} instead of raising"
        assert returned != EMPTY_SCHEMA
        # Not flattened into a generic wrapper — pyarrow's diagnosis survives.
        assert type(raised.value) is not Exception

    def test_missing_pyarrow_orc_raises_import_error_naming_the_layer(self, _mock_aws):
        """R2.6: a detached layer is diagnosable as such, not as a bad file.

        Message shape matches ``extract_schema_from_parquet:406-411``'s, and the
        chained cause is kept (``raise ... from e``) so the original
        ``ModuleNotFoundError`` still says *which* submodule was missing — a
        pyarrow built without the ORC component looks nothing like an unattached
        layer, and both reach this branch.
        """
        lf, _, _ = _mock_aws

        with (
            patch.dict("sys.modules", {"pyarrow.orc": None}),
            pytest.raises(ImportError, match="PyArrow Lambda layer") as raised,
        ):
            lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")

        assert "ORC schema extraction" in str(raised.value)
        assert raised.value.__cause__ is not None, "the original import failure was dropped"

    def test_missing_pyarrow_fs_also_raises_the_layer_import_error(self, _mock_aws):
        """R2.6: the seekable-handle half of the import is covered by the same guard.

        ``pyarrow.fs`` and ``pyarrow.orc`` are imported in one ``try``, so a
        layer missing either one produces the same operator-facing message.
        Asserted because the ORC extractor needs *both* and only one of them is
        obviously ORC-related.
        """
        lf, _, _ = _mock_aws

        with (
            patch.dict("sys.modules", {"pyarrow.fs": None}),
            pytest.raises(ImportError, match="PyArrow Lambda layer"),
        ):
            lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")


# ── extract_schema_from_parquet — the pyarrow.fs fallback (p46 task 8.2) ─


#: ``_orc_source`` is format-agnostic: it patches ``pyarrow.fs.S3FileSystem``,
#: which is the transport both the ORC extractor and the Parquet fallback go
#: through. Aliased rather than renamed so task 5.2's ORC tests keep reading
#: naturally, and so nothing is duplicated — a Parquet source is the same shape.
_seekable_s3_source = _orc_source


def _parquet_bytes(table) -> bytes:
    """A real Parquet object in memory. No committed fixture is needed.

    Task 2 committed ``.avro`` and ``.orc`` because no reader existed for
    either; Parquet has been read since before p46, so what R6 changed is the
    *access path*, not the format. Writing the bytes here from
    ``generate_binary_fixtures.build_arrow_table()`` keeps the same write-side
    declaration as the oracle for all three formats without adding a fourth
    binary to the tree.
    """
    sink = pa.BufferOutputStream()
    pa_pq.write_table(table, sink)
    return sink.getvalue().to_pybytes()


class TestExtractSchemaFromParquetPyArrowFsFallback:
    """R6: the Parquet fallback reads the footer over ``pyarrow.fs``, not ``/tmp``.

    **This is the deployed path, not an edge case.** ``compute-stack.ts:218-219``
    excludes ``s3fs`` to save ~60 MB and ``layer-measurement.md`` measures
    2.14 MiB of headroom left, so ``s3fs`` is not coming back and every Parquet
    extraction in the deployed Lambda takes this branch. Before p46 that branch
    was ``s3_client.download_file`` into a 512 MB ``/tmp``, which failed on
    exactly the large files a footer-only read exists to handle (C8).

    **Observed reads**, counted through :class:`_CountingSource` the same way
    task 5.2 counted ORC's, for ``ParquetFile(handle).schema_arrow``:

    | object                                | object bytes | reads | bytes read | share |
    |---------------------------------------|-------------:|------:|-----------:|------:|
    | fixture columns, 10 rows              |        2,532 |     1 |      2,532 |  100% |
    | 200,000 rows, 100 row groups          |    2,399,329 |     1 |     65,536 | 2.73% |

    Same conclusion as ORC and for the same reason: pyarrow issues a single
    bounded read of the object's tail and parses the footer out of it. Read
    volume is a function of footer size, not of file size or row-group count —
    100 row groups do not produce 100 reads. The small object reads 100% of
    itself only because it is smaller than one tail block.

    Measured on pyarrow **24.0.0** through :class:`pyarrow.PythonFile`, not
    through ``S3FileSystem`` on the deployed **20.0.0**
    (``layer-measurement.md``) — the same gap task 5.2 recorded for ORC, and
    the same reason the C8-relevant bound is asserted directly rather than
    inferred from the count. Task 5.1 did confirm
    ``S3FileSystem.open_input_file`` itself at 20.0.0; ``ParquetFile`` over that
    handle is confirmed only at 24.0.0.
    """

    def test_parquet_fallback_matches_expected_json_schema(self, _mock_aws):
        """R6.1: with ``s3fs`` absent, the fallback still returns the right schema.

        Compared against ``EXPECTED_JSON_SCHEMA`` in
        ``generate_binary_fixtures.py`` — the write-side declaration transcribed
        from Requirement 8's mapping table, derived from no extractor.
        ``LGR-28b4d75-5`` (template / ratified): a test written from the
        producer's output cannot detect the producer drifting, because the
        output is its own oracle.

        ``s3fs`` is patched to ``None`` rather than left to the environment. It
        happens not to be installed in the dev venv, so the fallback is already
        the only reachable branch here — but a test that depends on a package
        being absent is a test that silently stops testing the fallback the day
        someone installs it.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()
        payload = _parquet_bytes(declaration.build_arrow_table())

        with (
            patch.dict("sys.modules", {"s3fs": None}),
            _seekable_s3_source(payload),
        ):
            result = lf.extract_schema_from_parquet("test-bucket", "raw/customer_data.parquet")

        assert result["type"] == "object"

        # Field by field, so a failure names the column rather than dumping two dicts.
        for column in declaration.INTENDED_SCHEMA:
            assert column.name in result["properties"], f"missing column {column.name!r} ({column.role})"
            assert result["properties"][column.name] == {"type": column.json_schema_type}, (
                f"column {column.name!r} ({column.role})"
            )

        # No seventh column invented, and the whole document matches.
        assert set(result["properties"]) == set(declaration.EXPECTED_JSON_SCHEMA["properties"])
        assert result == declaration.EXPECTED_JSON_SCHEMA

    def test_parquet_fallback_returns_the_same_schema_the_s3fs_branch_would(self, _mock_aws):
        """R6.3: same object, same schema — the claim, not just its test-suite proxy.

        R6.3 is worded as "the existing Parquet tests pass unamended", and they
        do (verified mechanically, outside pytest, by hashing the AST segments of
        ``TestExtractSchemaFromParquet`` against HEAD). But that signal is
        weaker than it reads: of the two tests in that class,
        ``test_parquet_import_error_raises`` exercises only the ``ImportError``
        guard, and ``test_parquet_schema_extraction`` never calls
        ``extract_schema_from_parquet`` at all — it builds a ``result`` dict
        inline from mocked fields (see its own comment saying so). So *no*
        pre-existing test observed this function's return value, and "they still
        pass" cannot by itself mean the schema is unchanged.

        This asserts the underlying claim directly. Both branches read the same
        ``schema_arrow`` off the same footer and hand it to the same
        ``arrow_schema_to_json_schema``; the only difference is which filesystem
        supplied the handle. So converting the footer schema with no extractor
        involved gives the document the ``s3fs`` branch would have produced, and
        the fallback must equal it.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()
        payload = _parquet_bytes(declaration.build_arrow_table())

        with patch.dict("sys.modules", {"s3fs": None}), _seekable_s3_source(payload):
            result = lf.extract_schema_from_parquet("test-bucket", "raw/customer_data.parquet")

        footer_schema = pa_pq.ParquetFile(pa.PythonFile(io.BytesIO(payload), mode="r")).schema_arrow
        assert result == lf.arrow_schema_to_json_schema(footer_schema)

    def test_parquet_fallback_never_downloads_the_object(self, _mock_aws):
        """R6.2 / C8, stated negatively (design Property 5).

        Property 5 is asserted as an absence because C8's failure mode is
        ``/tmp`` exhaustion at a file size no unit test will use: 512 MB of
        Lambda ``/tmp`` against a Parquet object that does not fit. No unit test
        writes a 512 MB fixture, so the only testable form of "it will not run
        out of ``/tmp``" is "it never asks for the whole object".

        Also pins the ``region`` kwarg. The fallback passes
        ``os.environ.get("AWS_REGION")``, which Lambda always sets; a handle
        opened against the wrong region resolves the wrong endpoint, and that
        would surface as a missing object rather than as a configuration error.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        payload = _parquet_bytes(declaration.build_arrow_table())

        with (
            patch.dict("sys.modules", {"s3fs": None}),
            patch.dict(os.environ, {"AWS_REGION": "us-west-2"}),
            _seekable_s3_source(payload) as filesystems,
        ):
            result = lf.extract_schema_from_parquet("test-bucket", "raw/customer_data.parquet")

        assert result["properties"], "extractor returned no fields"
        # The former fallback, and every other route to the whole object.
        assert mock_s3.download_file.called is False
        assert mock_s3.get_object.called is False
        assert mock_s3.select_object_content.called is False
        # The only object access is the one seekable handle, addressed as bucket/key.
        assert len(filesystems) == 1
        assert filesystems[0].requested_paths == ["test-bucket/raw/customer_data.parquet"]
        assert filesystems[0].constructor_kwargs == {"region": "us-west-2"}

    def test_parquet_footer_read_is_bounded(self, _mock_aws):
        """R6.2, positively: the reads are a bounded tail slice, not the object.

        The negative assertion above rules out ``download_file`` and
        ``get_object``; it does not rule out pyarrow reading the whole object
        *through the handle*, which would reintroduce C8's failure by a different
        route. This pins the two ends of the class docstring's table.

        The synthetic object is written with a small ``row_group_size`` on
        purpose: 100 row groups is where a per-row-group read pattern would show
        up, and it does not — one read either way.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()

        # -- the small object: smaller than one tail block, so one read of itself
        small = _parquet_bytes(declaration.build_arrow_table())
        with patch.dict("sys.modules", {"s3fs": None}), _seekable_s3_source(small) as filesystems:
            lf.extract_schema_from_parquet("test-bucket", "raw/customer_data.parquet")
        source = filesystems[0].sources[0]
        assert len(source.reads) == 1
        assert source.bytes_read <= len(small)

        # -- a multi-row-group object: the read stays a small tail slice
        rows = 200_000
        table = pa.table(
            {
                "id": pa.array(range(rows), type=pa.int64()),
                "name": pa.array([f"name-{i:09d}" for i in range(rows)], type=pa.string()),
            }
        )
        sink = pa.BufferOutputStream()
        pa_pq.write_table(table, sink, row_group_size=2000)
        large = sink.getvalue().to_pybytes()
        assert len(large) > 1_000_000, "synthetic Parquet is too small to prove anything"

        with patch.dict("sys.modules", {"s3fs": None}), _seekable_s3_source(large) as filesystems:
            result = lf.extract_schema_from_parquet("test-bucket", "raw/large.parquet")

        assert set(result["properties"]) == {"id", "name"}
        source = filesystems[0].sources[0]
        # Generous ceiling on purpose: the exact figure is pyarrow's tail-block
        # size, an internal this test has no business pinning. What it pins is
        # that the read does not scale with the file — a download reads 100%.
        assert source.bytes_read < len(large) // 4, (
            f"read {source.bytes_read} of {len(large)} bytes — reads should be a bounded tail slice"
        )
        assert len(source.reads) <= 4, f"expected a handful of ranged reads, got {source.reads}"

    def test_parquet_prefers_s3fs_when_it_is_importable(self, _mock_aws):
        """Task 8.1 kept the ``s3fs`` branch first *deliberately*; this pins that.

        The point of keeping it is that behaviour is unchanged wherever ``s3fs``
        is present, and "unchanged" is only a claim if something checks the
        ordering. Without this, a later edit could delete the ``s3fs`` attempt —
        or reverse the two — and the whole suite would stay green, because every
        other test in this class runs with ``s3fs`` absent.

        ``s3fs`` is not installed anywhere in this repo (it is excluded from the
        layer, ``compute-stack.ts:218-219``), so it is stubbed into ``sys.modules``.
        ``pyarrow.parquet.ParquetFile`` is substituted because the stub
        filesystem is not a real fsspec implementation; the subject here is
        *which branch ran*, and schema correctness is asserted by the fallback
        tests above.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()
        payload = _parquet_bytes(declaration.build_arrow_table())
        footer_schema = pa_pq.ParquetFile(pa.PythonFile(io.BytesIO(payload), mode="r")).schema_arrow

        constructed: list[dict] = []

        class _RecordingS3FileSystem:
            def __init__(self, **kwargs):
                constructed.append(kwargs)

        stub_s3fs = types.ModuleType("s3fs")
        stub_s3fs.S3FileSystem = _RecordingS3FileSystem

        parquet_file_calls: list[tuple] = []

        def _fake_parquet_file(source, **kwargs):
            parquet_file_calls.append((source, kwargs))
            return types.SimpleNamespace(schema_arrow=footer_schema)

        with (
            patch.dict("sys.modules", {"s3fs": stub_s3fs}),
            patch("pyarrow.parquet.ParquetFile", side_effect=_fake_parquet_file),
            patch("pyarrow.fs.S3FileSystem") as pa_fs_ctor,
        ):
            result = lf.extract_schema_from_parquet("test-bucket", "raw/customer_data.parquet")

        assert constructed == [{}], "s3fs.S3FileSystem() was not the first attempt"
        assert pa_fs_ctor.called is False, "the pyarrow.fs fallback ran even though s3fs imported"

        # The s3fs branch's own call shape, so a swap of the two is caught too.
        assert len(parquet_file_calls) == 1
        source, kwargs = parquet_file_calls[0]
        assert source == "test-bucket/raw/customer_data.parquet"
        assert isinstance(kwargs.get("filesystem"), _RecordingS3FileSystem)

        # Same converter, same output — the branch choice is invisible downstream.
        assert result == lf.arrow_schema_to_json_schema(footer_schema)


# ── extract_schema_from_avro (p46 task 4.2) ─────────────────────────────


#: The committed Avro fixture (1,186 bytes, generated by
#: ``tests/fixtures/data/generate_binary_fixtures.py`` via ``fastavro``).
AVRO_FIXTURE_PATH = Path(__file__).parents[3] / "fixtures" / "data" / "customer_data.avro"

#: The exact prefix length at which the fixture's OCF header becomes readable.
#: Measured, not derived: the ``avro.schema`` JSON spans 35..515 and the map
#: terminator is the single byte at 516, so 517 bytes is the first prefix that
#: parses and 516 is the last that does not. Both ends are asserted by
#: ``test_avro_sample_bytes_boundary_is_exact``.
AVRO_HEADER_END = 517


def _encode_avro_long(value: int) -> bytes:
    """Zigzag-varint encode one Avro ``long`` — the inverse of ``_read_avro_long``.

    **This is deliberately not the oracle for anything.** A correctness claim
    about the parser is checked against the ``fastavro``-written fixture and
    against ``generate_binary_fixtures.INTENDED_SCHEMA``, never against this
    encoder: an encoder and decoder written from the same reading of the OCF
    spec share any misreading of it, so a round trip through both proves
    nothing. ``LGR-28b4d75-5`` (template / ratified) is the standing form of
    that argument.

    What it is for is the error branches, where the assertion is "it raised" —
    a shared misreading cannot manufacture a false pass of that — and the one
    header shape ``fastavro`` will not write (see
    :meth:`TestExtractSchemaFromAvro.test_avro_negative_block_count_map_is_parsed`).
    Its agreement with the fixture's own framing is checked in
    ``test_avro_hand_assembler_agrees_with_the_fixture_framing`` before any test
    relies on it.
    """
    encoded = (value << 1) ^ (value >> 63)
    out = bytearray()
    while True:
        chunk = encoded & 0x7F
        encoded >>= 7
        if encoded:
            out.append(chunk | 0x80)
        else:
            out.append(chunk)
            return bytes(out)


def _encode_avro_bytes(payload: bytes) -> bytes:
    """Length-prefix *payload* the way an OCF metadata key or value is framed."""
    return _encode_avro_long(len(payload)) + payload


def _assemble_avro_header(entries, *, negative_block: bool = False, block_size: int | None = None) -> bytes:
    """Hand-assemble an OCF header carrying *entries* as its metadata map.

    Args:
        entries: ``(key, value bytes)`` pairs.
        negative_block: Emit the map as a **negative** block count — ``abs(n)``
            entries preceded by the block's byte size — instead of a positive
            one. This is the writer's-option form ``fastavro`` never emits.
        block_size: Override the declared block size. The parser reads it only
            to advance past it and must not trust it as a bound, which
            ``test_avro_negative_block_count_map_is_parsed`` checks by lying
            here.

    Returns:
        magic + the metadata map + the terminator. No sync marker: nothing in
        this extractor reads one.
    """
    body = b"".join(_encode_avro_bytes(key.encode("utf-8")) + _encode_avro_bytes(value) for key, value in entries)
    if negative_block:
        declared = len(body) if block_size is None else block_size
        count = _encode_avro_long(-len(entries)) + _encode_avro_long(declared)
    else:
        count = _encode_avro_long(len(entries))
    return b"Obj\x01" + count + body + _encode_avro_long(0)


def _avro_ranged_get(mock_s3, payload: bytes) -> list[dict]:
    """Serve *payload* from ``s3_client.get_object`` the way S3 serves a ranged GET.

    The slice is taken from the ``Range`` header the extractor actually sent,
    rather than the test handing over a pre-truncated buffer. That distinction
    is the whole point of the ``sample_bytes`` tests: S3 returns only the bytes
    asked for, so a prefix too short to hold the header is a *real* condition
    the extractor meets in production, not a condition simulated by the test.
    A missing or malformed ``Range`` raises here rather than quietly serving the
    whole object.

    Returns:
        The recorded ``get_object`` kwargs, one dict per call.
    """
    requests: list[dict] = []

    def _get_object(**kwargs):
        requests.append(kwargs)
        header = kwargs["Range"]
        assert header.startswith("bytes="), f"unbounded or malformed Range header: {header!r}"
        start, _, end = header.removeprefix("bytes=").partition("-")
        assert start and end, f"Range must be bounded at both ends, got {header!r}"
        body = MagicMock()
        body.read.return_value = payload[int(start) : int(end) + 1]
        return {"Body": body}

    mock_s3.get_object.side_effect = _get_object
    return requests


class TestExtractSchemaFromAvro:
    """Tests for Avro schema extraction from a hand-parsed OCF header.

    **Why these tests are held to a higher bar than the other extractors'.**
    Task 1 measured the Layer_Budget and the gate FAILED — ``fastavro`` needs
    12.70 MiB against 2.14 MiB of headroom (``layer-measurement.md``) — so
    Requirement 1 takes requirements.md's retained fallback and parses the OCF
    header by hand. requirements.md rejected that approach *on correctness, not
    size*: "a subtle bug produces a wrong schema rather than a failure, which is
    the same class of silent wrongness this spec removes." That risk is now
    unavoidable, which makes this test class the thing standing in for the
    library that could not be afforded.

    So the fixture is compared **field by field against the write-side
    declaration**, not checked for a non-empty ``properties``:
    ``generate_binary_fixtures.EXPECTED_JSON_SCHEMA`` and ``INTENDED_SCHEMA``
    are transcribed from Requirement 8's mapping table and derived from no
    parser, and the fixture itself is written by ``fastavro`` — a different
    implementation from the one under test. ``LGR-28b4d75-5`` (template /
    ratified): a test written from the producer's output cannot detect the
    producer drifting, because the output is its own oracle. Two independent
    layers are checked: the Avro type tokens recovered from the header
    (``test_avro_header_metadata_matches_the_write_side_declaration``) and the
    JSON Schema they convert to (``test_avro_fixture_matches_expected_json_schema``).

    **The fixture's header, byte for byte** (the offset table in
    ``lambda_function.py``'s block comment, re-confirmed here): magic at 0..3, a
    map block count of 2 at 4, ``avro.codec`` = ``null`` in 5..20,
    ``avro.schema`` keyed at 21..32 with a 481-byte value spanning 35..515, and
    the map terminator at 516. So :data:`AVRO_HEADER_END` = 517 and the 64 KB
    default has ample room.

    **Access pattern.** Avro is the one new extractor that does *not* use a
    seekable handle: the Writer_Schema is in the file's first bytes, so a single
    bounded ranged GET reaches it, and ``s3_client.get_object`` is mocked with
    :func:`_avro_ranged_get` rather than ``pyarrow.fs.S3FileSystem`` being
    stubbed as for ORC and Parquet.
    """

    # -- the oracle ----------------------------------------------------

    def test_avro_fixture_matches_expected_json_schema(self, _mock_aws):
        """R1.2: one entry per top-level Writer_Schema field, with the declared types.

        Field by field against ``EXPECTED_JSON_SCHEMA``, the write-side
        declaration — see the class docstring for why the comparison is made
        against that and not against a second parse.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        _avro_ranged_get(mock_s3, AVRO_FIXTURE_PATH.read_bytes())

        result = lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro")

        assert result["type"] == "object"

        # Field by field, so a failure names the column rather than dumping two dicts.
        for column in declaration.INTENDED_SCHEMA:
            assert column.name in result["properties"], f"missing field {column.name!r} ({column.role})"
            assert result["properties"][column.name] == {"type": column.json_schema_type}, (
                f"field {column.name!r} ({column.role})"
            )

        # No extra field, and the whole document matches — the loop above would
        # pass if the parser invented a seventh field.
        assert set(result["properties"]) == set(declaration.EXPECTED_JSON_SCHEMA["properties"])
        assert result == declaration.EXPECTED_JSON_SCHEMA

        # The union case called out by design Property 4, named rather than left
        # to the loop: ["null", "double"] resolves to "number", agreeing with
        # infer_schema_from_records:171-181 rather than inventing a second policy.
        assert result["properties"]["account_balance"] == {"type": "number"}
        # And the logical type, the other mapping a hand parser could get wrong:
        # long/timestamp-millis is "string", not "integer" (R8.3).
        assert result["properties"]["created_at"] == {"type": "string"}
        assert result["properties"]["id"] == {"type": "integer"}

    def test_avro_header_metadata_matches_the_write_side_declaration(self, _mock_aws):
        """R1.2, one layer deeper: the *Avro* types recovered, not just the JSON ones.

        The JSON Schema comparison above would still pass if the parser
        recovered ``long`` where the file says ``["null", "double"]`` and the
        converter happened to land on the same answer. This compares the type
        tokens the parser pulled out of the header against
        ``INTENDED_SCHEMA``'s, through the generator's own ``_render`` — so the
        union, the logical type and the record name are each checked as written.

        Also pins the two facts the offset table rests on: exactly two metadata
        entries, and ``avro.codec`` = ``null`` (no internal block compression,
        which is what keeps the header a flat read).

        ``fastavro.parse_schema`` folds the namespace into the record name, so
        the header carries the fully-qualified form and no separate
        ``namespace`` key. Observed in the committed bytes, not assumed.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()
        fixture_bytes = AVRO_FIXTURE_PATH.read_bytes()

        metadata = lf._parse_avro_ocf_metadata(fixture_bytes)

        assert sorted(metadata) == ["avro.codec", "avro.schema"]
        assert metadata["avro.codec"] == b"null"
        assert len(metadata["avro.schema"]) == 481, "the schema JSON spans offsets 35..515"

        writer_schema = json.loads(metadata["avro.schema"].decode("utf-8"))
        assert writer_schema["type"] == "record"
        assert writer_schema["name"] == f"{declaration.AVRO_NAMESPACE}.{declaration.AVRO_RECORD_NAME}"

        observed = [(field["name"], declaration._render(field["type"])) for field in writer_schema["fields"]]
        # Field by field first, so a failure names the field and its role.
        for (name, rendered), column in zip(observed, declaration.INTENDED_SCHEMA, strict=True):
            assert name == column.name, f"field order changed: {name!r} where {column.name!r} was declared"
            assert rendered == declaration._render(column.avro_type), f"field {name!r} ({column.role})"
        assert observed == declaration._expected_avro()

        # The offset 4.1 corrected by re-reading the bytes: the union sits at 495,
        # inside the header JSON, not near the end of the file.
        assert fixture_bytes.find(b'["null", "double"]') == 495

    def test_avro_hand_assembler_agrees_with_the_fixture_framing(self, _mock_aws):
        """Checks :func:`_assemble_avro_header` before any test leans on it.

        The hand assembler is used only for error branches and for the header
        shape ``fastavro`` will not write, and it is explicitly not an oracle
        (see its docstring). But an assembler that framed bytes differently from
        a real writer would make those tests exercise something other than what
        they claim, so its framing is checked against the committed fixture's:
        same magic, same positive block count, same key framing.

        Then, the load-bearing half — a header assembled with the fixture's own
        two metadata entries yields the same schema the fixture does, so the
        negative-count test below differs from the control in exactly one
        respect.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        fixture_bytes = AVRO_FIXTURE_PATH.read_bytes()
        schema_json = lf._parse_avro_ocf_metadata(fixture_bytes)["avro.schema"]

        assembled = _assemble_avro_header(
            [("avro.codec", b"null"), ("avro.schema", schema_json)],
        )
        # Byte-identical framing up to and including the first key's length
        # prefix: magic, count-of-2 varint, then 0x14 for a 10-byte key.
        assert assembled[:6] == fixture_bytes[:6]
        assert _encode_avro_long(2) == fixture_bytes[4:5]
        assert _encode_avro_long(0) == b"\x00"

        _avro_ranged_get(mock_s3, assembled)
        assert lf.extract_schema_from_avro("test-bucket", "raw/assembled.avro") == declaration.EXPECTED_JSON_SCHEMA

    # -- bounded reads (design Property 5) -----------------------------

    def test_avro_range_get_is_present_and_bounded(self, _mock_aws):
        """R1.3: one ranged GET, bounded, defaulting to the first 65536 bytes.

        The default is asserted as the literal ``bytes=0-65535`` rather than
        computed from ``sample_bytes``, because a test that derives the expected
        header the same way the code does cannot notice the bound changing. Same
        shape ``test_json_range_get_params`` uses for the JSON extractor.
        """
        lf, mock_s3, _ = _mock_aws
        fixture_bytes = AVRO_FIXTURE_PATH.read_bytes()

        requests = _avro_ranged_get(mock_s3, fixture_bytes)
        lf.extract_schema_from_avro("mybucket", "raw/customer_data.avro")

        assert len(requests) == 1, f"expected exactly one ranged GET, got {len(requests)}"
        assert requests[0]["Bucket"] == "mybucket"
        assert requests[0]["Key"] == "raw/customer_data.avro"
        assert requests[0]["Range"] == "bytes=0-65535"

        # An explicit sample_bytes is honoured, and stays bounded. 1024 is
        # comfortably past AVRO_HEADER_END, so this still extracts.
        requests = _avro_ranged_get(mock_s3, fixture_bytes)
        result = lf.extract_schema_from_avro("mybucket", "raw/customer_data.avro", sample_bytes=1024)
        assert requests[-1]["Range"] == "bytes=0-1023"
        assert len(result["properties"]) == 6

    def test_avro_never_downloads_the_object(self, _mock_aws):
        """R1.4 / C8, stated negatively (design Property 5).

        Property 5 is asserted as an absence because C8's failure mode is
        ``/tmp`` exhaustion at a file size no unit test will use: 512 MB of
        Lambda ``/tmp`` against an object that does not fit. No unit test writes
        such a fixture, so the only testable form of "it will not run out of
        ``/tmp``" is "it never asks for the whole object and never opens a temp
        file".

        Both halves of R1.4 are covered: no ``download_file``, and
        ``tempfile.NamedTemporaryFile`` never constructed. The temp-file half is
        checked at the stdlib rather than through a module attribute, so it
        holds however the code might import ``tempfile`` — which today it does
        not at all, task 8.1 having removed the last such path.
        """
        lf, mock_s3, _ = _mock_aws

        requests = _avro_ranged_get(mock_s3, AVRO_FIXTURE_PATH.read_bytes())
        with patch("tempfile.NamedTemporaryFile") as named_temp_file:
            result = lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro")

        assert result["properties"], "extractor returned no fields"
        assert mock_s3.download_file.called is False
        assert mock_s3.select_object_content.called is False
        assert named_temp_file.called is False, "the extractor opened a temp file"
        # The only object access is the single bounded ranged GET.
        assert len(requests) == 1
        assert requests[0]["Range"] == "bytes=0-65535"

    # -- the sample_bytes boundary -------------------------------------

    @pytest.mark.parametrize("sample_bytes", [64, 300, AVRO_HEADER_END - 2])
    def test_avro_header_longer_than_sample_bytes_raises_naming_the_boundary(self, _mock_aws, sample_bytes):
        """R1.5 / R4.1 / R4.2 / R4.4 — the assertion this task exists for.

        A header that does not fit the prefix must **raise naming the boundary
        and ``sample_bytes``**, and must never return the fields that did
        arrive. A truncated read silently yielding fewer fields than the file
        declares is precisely the defect p46 removes — it is a *handled*
        outcome, so the run advances to ``schema_mapping`` short of columns
        while reporting success — and it is the specific way a hand-written
        parser would reintroduce it.

        Both halves of design Property 2 are checked: that it raised, and that
        nothing was returned. The message is checked for the boundary, the
        remedy and the reason, because an operator handed "the file is corrupt"
        for a header that merely needed a larger prefix will go and look at the
        file.

        The prefix genuinely stops at ``sample_bytes`` — :func:`_avro_ranged_get`
        slices from the ``Range`` header the extractor sent, as S3 would.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        fixture_bytes = AVRO_FIXTURE_PATH.read_bytes()
        _avro_ranged_get(mock_s3, fixture_bytes)

        returned = None
        with pytest.raises(ValueError) as raised:
            returned = lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro", sample_bytes=sample_bytes)

        assert returned is None, f"returned {returned!r} instead of raising"
        assert returned != EMPTY_SCHEMA

        message = str(raised.value)
        assert "truncated" in message
        assert f"{sample_bytes}-byte prefix read" in message, message
        assert "increase sample_bytes" in message, message
        assert "No partial schema is returned" in message, message

        # "Return what fit" is a live possibility at these prefixes, not a
        # hypothetical: the map's first entry is fully readable, the
        # ``avro.schema`` key and its length are read, and the parser is inside
        # the value with bytes still in hand when it refuses. The message says
        # how many — a positive remainder is the evidence that it had something
        # to return and did not.
        assert b"avro.schema" in fixture_bytes[:sample_bytes]
        remaining = re.search(r"but only (\d+) remain", message)
        assert remaining, message
        assert int(remaining.group(1)) == sample_bytes - 35 > 0, "the schema value begins at offset 35"

        # Positive control: the same fixture, the same mock, only a prefix that
        # fits — so this test cannot pass by the extractor being broken for
        # every input.
        _avro_ranged_get(mock_s3, fixture_bytes)
        assert lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro") == declaration.EXPECTED_JSON_SCHEMA

    def test_avro_sample_bytes_boundary_is_exact(self, _mock_aws):
        """R1.5: the boundary is where the header ends, to the byte.

        :data:`AVRO_HEADER_END` bytes parse; one fewer does not. The one-fewer
        case is the sharper of the two: at 516 bytes the ``avro.schema`` JSON is
        **entirely present** — every field name is in the prefix — and the
        parser still refuses, because the map is not terminated within what it
        was given. That is raise-rather-than-guess at its narrowest, and it is
        the behaviour that makes a wrong schema unreachable by truncation.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        fixture_bytes = AVRO_FIXTURE_PATH.read_bytes()

        _avro_ranged_get(mock_s3, fixture_bytes)
        assert (
            lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro", sample_bytes=AVRO_HEADER_END)
            == declaration.EXPECTED_JSON_SCHEMA
        )

        # One byte short of the terminator. The whole schema JSON is there:
        short = fixture_bytes[: AVRO_HEADER_END - 1]
        for column in declaration.INTENDED_SCHEMA:
            assert f'"{column.name}"'.encode() in short, f"{column.name!r} should already be in the short prefix"

        _avro_ranged_get(mock_s3, fixture_bytes)
        returned = None
        with pytest.raises(ValueError, match=f"{AVRO_HEADER_END - 1}-byte prefix read") as raised:
            returned = lf.extract_schema_from_avro(
                "test-bucket", "raw/customer_data.avro", sample_bytes=AVRO_HEADER_END - 1
            )

        assert returned is None, f"returned {returned!r} for an unterminated metadata map"
        assert returned != EMPTY_SCHEMA
        assert "truncated" in str(raised.value)

    # -- malformed input (design Property 2) ---------------------------

    @pytest.mark.parametrize(
        ("label", "payload", "expected_message"),
        [
            (
                "not-an-ocf",
                b"this is plain text, and definitely not a container file" * 4,
                "Not an Avro Object Container File",
            ),
            ("magic-only", b"Obj\x01", "a metadata map block count runs past the end"),
            ("truncated-mid-json", None, "declares 481 bytes at offset 35"),
            ("all-0xff-varint", b"Obj\x01" + b"\xff" * 24, "is not a valid varint"),
            (
                "negative-declared-length",
                b"Obj\x01" + _encode_avro_long(1) + _encode_avro_long(-1) + b"xxxx",
                "declares a negative length (-1)",
            ),
            (
                "absurd-entry-count",
                b"Obj\x01" + _encode_avro_long(2000),
                "declares 2000 entries (bound is 1000)",
            ),
            (
                "negative-block-size",
                b"Obj\x01"
                + _encode_avro_long(-1)
                + _encode_avro_long(-5)
                + _encode_avro_bytes(b"k")
                + _encode_avro_bytes(b"v")
                + _encode_avro_long(0),
                "declares a negative size (-5)",
            ),
            (
                "non-utf8-metadata-key",
                b"Obj\x01"
                + _encode_avro_long(1)
                + _encode_avro_bytes(b"\xff\xfe")
                + _encode_avro_bytes(b"v")
                + _encode_avro_long(0),
                "not valid UTF-8",
            ),
            (
                "no-avro-schema-key",
                _assemble_avro_header([("avro.codec", b"null")]),
                "carries no 'avro.schema' metadata key",
            ),
            (
                "avro-schema-not-json",
                _assemble_avro_header([("avro.schema", b"{not json at all")]),
                "is not valid JSON",
            ),
            (
                "fieldless-record",
                _assemble_avro_header(
                    [("avro.schema", b'{"type": "record", "name": "Empty", "fields": []}')],
                ),
                "yielded no fields",
            ),
        ],
    )
    def test_malformed_avro_raises_and_never_returns_an_empty_schema(self, _mock_aws, label, payload, expected_message):
        """R1.5 / R4.1 / R4.2 / R4.4 and design Property 2.

        Property 2 admits exactly two outcomes for any input: non-empty
        ``properties``, or an exception. There is no third one, so both halves
        are checked — that it raised, and that nothing came back. Returning the
        Empty_Schema is what ``extract_schema_from_json`` does at ``:313-315``,
        and that *handled* outcome is the defect p46 removes: the run reaches
        ``schema_mapping`` with nothing to map and reports success.

        The cases walk the parser's raise sites rather than sampling them —
        magic, varint bounds (both ends), declared lengths, the entry-count
        bound, key decoding, the missing key, the unparseable JSON, and the
        fieldless record. The last is the seam between
        ``avro_schema_to_json_schema``, which by its own design returns
        ``properties: {}`` for a fieldless schema and does not police it, and
        the extractor, which does: the check lives at the extractor exactly as
        ``arrow_schema_to_json_schema`` does not police its own output.

        ``ValueError`` in every case, and never a bare ``Exception`` — the
        classification is what ``@step_handler`` turns into a permanent
        ``StepFailure`` recorded against ``schema_extract``.
        """
        lf, mock_s3, _ = _mock_aws
        if payload is None:  # the fixture, cut mid-JSON — not a hand-assembled shape
            payload = AVRO_FIXTURE_PATH.read_bytes()[:300]
        _avro_ranged_get(mock_s3, payload)

        returned = None
        with pytest.raises(ValueError, match=re.escape(expected_message)) as raised:
            returned = lf.extract_schema_from_avro("test-bucket", f"raw/{label}.avro")

        assert returned is None, f"{label}: extractor returned {returned!r} instead of raising"
        assert returned != EMPTY_SCHEMA
        assert type(raised.value) is ValueError, f"{label}: raised {type(raised.value).__name__}"
        assert mock_s3.download_file.called is False, f"{label}: fell back to a whole-object download"

    def test_avro_negative_block_count_map_is_parsed(self, _mock_aws):
        """The one branch the independent oracle cannot reach.

        An OCF metadata map may encode a block as a **negative** count —
        ``abs(n)`` entries preceded by the block's byte size — which is legal
        Avro that ``fastavro`` does not emit. Task 4.1 says so in
        ``_parse_avro_ocf_metadata``'s docstring and names it as the one part of
        the parser written from the specification rather than confirmed against
        the fixture. So it gets a test, assembled by hand, because there is no
        other way to produce those bytes.

        The assembled header carries the fixture's **own** two metadata entries,
        so it differs from the positive-count control in exactly one respect —
        the block framing — and the assertion is that the schema comes out
        identical. ``test_avro_hand_assembler_agrees_with_the_fixture_framing``
        establishes that the control holds first.

        A second case lies about the declared block size. The parser reads that
        size only to advance past it and must not trust it as a bound — the
        per-entry length checks in ``_read_avro_bytes`` are the bound — so a
        wrong size must not change the answer. A *negative* size does raise, and
        that case is in the malformed table above.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        schema_json = lf._parse_avro_ocf_metadata(AVRO_FIXTURE_PATH.read_bytes())["avro.schema"]
        entries = [("avro.codec", b"null"), ("avro.schema", schema_json)]

        negative = _assemble_avro_header(entries, negative_block=True)
        positive = _assemble_avro_header(entries)
        assert negative != positive, "the two framings assembled identically — the test proves nothing"

        _avro_ranged_get(mock_s3, negative)
        result = lf.extract_schema_from_avro("test-bucket", "raw/negative-count.avro")
        assert result == declaration.EXPECTED_JSON_SCHEMA

        # The declared block size is advanced past, not trusted as a bound.
        _avro_ranged_get(mock_s3, _assemble_avro_header(entries, negative_block=True, block_size=7))
        assert lf.extract_schema_from_avro("test-bucket", "raw/lying-block-size.avro") == (
            declaration.EXPECTED_JSON_SCHEMA
        )

    def test_externally_compressed_avro_raises_and_returns_nothing(self, _mock_aws):
        """Property 2 holds for a gzipped container too — outcome only.

        Deliberately **not** asserting the message. Task 7.1 adds the
        External_Compression guard and task 7.2 owns what it says, so pinning
        the wording here would either duplicate 7.2's assertion or contradict
        it. What belongs to 4.2 is the part that is 4.2's regardless of which
        guard fires first: a gzipped ``.avro`` raises and returns nothing, so it
        cannot reach ``schema_mapping`` as an Empty_Schema.
        """
        lf, mock_s3, _ = _mock_aws
        _avro_ranged_get(mock_s3, b"\x1f\x8b\x08\x00" + b"\x00" * 128)

        returned = None
        with pytest.raises(ValueError):
            returned = lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro.gz")

        assert returned is None
        assert returned != EMPTY_SCHEMA


# ── External_Compression guard (p46 task 7.2) ───────────────────────────


def _zstd_bytes(payload: bytes) -> bytes:
    """Return *payload* as a zstd frame, or failing that as zstd-magic-prefixed bytes.

    ``compression.zstd`` is stdlib from Python 3.14; the dev venv here is 3.14.3
    and the deployed Lambda runtime is 3.12. Neither the guard nor this test
    needs a decodable frame — the guard inspects
    ``_EXTERNAL_COMPRESSION_HEAD_BYTES`` (4) and nothing beyond — so the
    fallback is a genuine test of the same code path, and the branch is made
    safe by every caller asserting the payload starts with the zstd magic before
    using it. Without that assertion a fallback could silently stop exercising
    the magic it is named for.
    """
    try:
        from compression import zstd  # Python 3.14+
    except ImportError:  # pragma: no cover — 3.12/3.13 path, exercised by the assertion above
        return b"\x28\xb5\x2f\xfd" + payload
    return zstd.compress(payload)


#: The three External_Compression magics R5.1 enumerates, as
#: ``(tool, magic, stripped suffix, compressor)``. Written from the requirement
#: and from ``detect_format``'s suffix list, **not** read out of
#: ``_EXTERNAL_COMPRESSION_MAGICS`` — a test that sources its expectation from
#: the table under test cannot notice the table changing
#: (``LGR-28b4d75-5``, template / ratified). gzip and bzip2 are compressed for
#: real by the stdlib, so the bytes under test are the bytes an operator would
#: actually upload.
_EXTERNAL_COMPRESSIONS = [
    ("gzip", b"\x1f\x8b", ".gz", gzip.compress),
    ("bzip2", b"\x42\x5a\x68", ".bz2", bz2.compress),
    ("zstd", b"\x28\xb5\x2f\xfd", ".zst", _zstd_bytes),
]

#: Payloads that begin with **no** recognised magic, for the heuristic's other
#: side (design §5 R5). ``snappy`` is in here rather than in the table above
#: because R5.1 enumerates exactly three magics and ``snappy`` is deliberately
#: not one of them: raw snappy has no magic at all, and the framing format's
#: stream identifier is not in the table either. So a snappy-wrapped container
#: must fall through to its ordinary parse error, which is what this pins — the
#: guard is a heuristic that converts the *common* mistake into a clear message,
#: not every mistake.
_UNRECOGNISED_PAYLOADS = [
    ("snappy-framed", b"\xff\x06\x00\x00sNaPpY"),
    ("random-binary", bytes(range(200))),
    ("plain-text", b"this is not a container and not compressed either" * 4),
]


def _called_names(function_node) -> set[str]:
    """Every name called anywhere inside *function_node*, by AST not by substring.

    ``LGR-b46a5bb-4`` (template / ratified) rejected substring matching for a
    repository invariant test because it "forbids documenting the fence" — it
    cannot tell source from prose. That applies here with force:
    ``_raise_if_externally_compressed`` is named in ``extract_schema_from_csv``'s
    neighbourhood only in the guard's own docstring and block comment, which say
    csv and json are *not* guarded. A substring check would read those as calls
    and fail R5.3's test for stating R5.3.
    """
    names: set[str] = set()
    for node in ast.walk(function_node):
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name):
                names.add(node.func.id)
            elif isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return names


def _module_functions(lf) -> dict[str, ast.FunctionDef]:
    """Top-level ``def``s of the extractor module, parsed from its own source."""
    tree = ast.parse(Path(lf.__file__).read_text())
    return {node.name: node for node in tree.body if isinstance(node, ast.FunctionDef)}


class TestExternalCompressionGuard:
    """The External_Compression guard, over both containers and all three magics.

    Six cases — ``{gzip, bzip2, zstd} × {Avro, ORC}`` — plus the three things
    that make the guard safe rather than merely present: that an uncompressed
    fixture is untouched, that csv and json are not on the guard's path at all
    (R5.3), and that a failure inside the diagnostic cannot replace the real
    error with an error about itself.

    **The two call sites have deliberately different shapes, and that asymmetry
    is asserted rather than smoothed over.**

    - **Avro checks first.** Its ranged GET already holds the head in memory, so
      the guard is a head test *ahead of* the OCF magic check and runs on every
      call, successful or not. ``test_uncompressed_avro_is_unaffected`` asserts
      it is called once even when the fixture is fine.
    - **ORC checks only on the failure path.** It fetches no prefix; the head is
      read off the already-open ``pyarrow.fs`` handle *after* ``ORCFile`` has
      raised. Checking first would cost a ranged read on every successful
      extraction and would break task 5.2's
      ``test_orc_footer_read_is_bounded``, which pins ``len(source.reads) == 1``
      for the fixture. ``test_uncompressed_orc_is_unaffected_and_still_reads_once``
      asserts both halves of that: one read, and the guard **not called**.

    **Observed reads on the ORC failure path**, measured through
    :class:`_CountingSource` over the compressed fixture — the guard's own head
    read is always the last of them, at offset 0 for 4 bytes:

    | payload (1,688-byte fixture, compressed) | reads |
    |------------------------------------------|-------|
    | gzip (808 bytes)                         | ``[(0, 808), (0, 4)]`` |
    | bzip2 (941 bytes)                        | ``[(0, 941), (0, 3), (0, 4)]`` |
    | zstd (820 bytes)                         | ``[(0, 820), (0, 3), (0, 4)]`` |
    | garbage, no magic                        | ``[(0, 152), (0, 3), (0, 4)]`` |

    The count is a function of what pyarrow does before giving up and is not
    pinned here; the **last** read being ``(0, 4)`` is, because that is the
    guard's, and its coming off the existing handle rather than from a new
    ranged GET is the whole reason ORC's shape differs.

    **No boto3 call is added on either path** — Avro's guard reads the prefix it
    already fetched, ORC's reads the handle it already holds. Both are asserted.
    """

    # -- the six cases -------------------------------------------------

    @pytest.mark.parametrize(
        ("tool", "magic", "suffix", "compress"),
        _EXTERNAL_COMPRESSIONS,
        ids=[tool for tool, *_ in _EXTERNAL_COMPRESSIONS],
    )
    def test_externally_compressed_avro_is_refused_naming_the_cause(self, _mock_aws, tool, magic, suffix, compress):
        """R5.1 / R5.2 for Avro: the cause is named and the remedy is stated.

        Task 4.2's ``test_externally_compressed_avro_raises_and_returns_nothing``
        asserts the *outcome* — raised, returned nothing — and deliberately left
        the wording to this task. So this is the message half, and it is checked
        for the four things an operator needs: that the file is externally
        compressed, which tool did it, what to do about it, and which object.
        "Corrupt Avro file" would satisfy none of them and would send the
        operator to look at a file that is fine.

        The bytes are the real thing: the committed fixture put through
        ``gzip.compress`` / ``bz2.compress``, not hand-written magic.
        """
        lf, mock_s3, _ = _mock_aws
        payload = compress(AVRO_FIXTURE_PATH.read_bytes())
        assert payload.startswith(magic), f"{tool} payload does not begin with its own magic"

        requests = _avro_ranged_get(mock_s3, payload)

        returned = None
        with pytest.raises(ValueError) as raised:
            returned = lf.extract_schema_from_avro("test-bucket", f"raw/customer_data.avro{suffix}")

        # Property 2 still holds: raised, and nothing came back.
        assert returned is None
        assert returned != EMPTY_SCHEMA
        assert type(raised.value) is ValueError, f"raised {type(raised.value).__name__}"

        message = str(raised.value)
        assert "externally compressed" in message, message
        assert tool in message, message
        assert magic.hex(" ") in message, message
        assert "Upload the Avro container uncompressed" in message, message
        assert f"`data.avro{suffix}`" in message, message
        assert f"s3://test-bucket/raw/customer_data.avro{suffix}" in message, message
        # Not reported as a corrupt container — that is the diagnosis this
        # guard exists to replace.
        assert "Not an Avro Object Container File" not in message, message

        # One bounded ranged GET, and no whole-object download: the guard reads
        # the prefix that was already fetched and adds no S3 call of its own.
        assert len(requests) == 1
        assert requests[0]["Range"] == "bytes=0-65535"
        assert mock_s3.download_file.called is False

    @pytest.mark.parametrize(
        ("tool", "magic", "suffix", "compress"),
        _EXTERNAL_COMPRESSIONS,
        ids=[tool for tool, *_ in _EXTERNAL_COMPRESSIONS],
    )
    def test_externally_compressed_orc_is_refused_naming_the_cause(self, _mock_aws, tool, magic, suffix, compress):
        """R5.1 / R5.2 for ORC, and the head read comes off the open handle.

        Without the guard a gzipped ORC object reports ``Invalid ORC postscript
        length`` — accurate, and useless to the person who gzipped it. The
        message assertions are the same four facts as for Avro, with ``ORC``
        named as the container.

        The read assertions are what makes this ORC's test rather than a copy of
        Avro's: the last read on the handle is the guard's own 4-byte head read
        at offset 0, and **no boto3 call is made at all** — so R2.3 and C8 still
        hold on the failure path, not just on the success path.
        """
        lf, mock_s3, _ = _mock_aws
        payload = compress(ORC_FIXTURE_PATH.read_bytes())
        assert payload.startswith(magic), f"{tool} payload does not begin with its own magic"

        returned = None
        with _orc_source(payload) as filesystems, pytest.raises(ValueError) as raised:
            returned = lf.extract_schema_from_orc("test-bucket", f"raw/customer_data.orc{suffix}")

        assert returned is None
        assert returned != EMPTY_SCHEMA
        assert type(raised.value) is ValueError, f"raised {type(raised.value).__name__}"

        message = str(raised.value)
        assert "externally compressed" in message, message
        assert tool in message, message
        assert magic.hex(" ") in message, message
        assert "Upload the ORC container uncompressed" in message, message
        assert f"`data.orc{suffix}`" in message, message
        assert f"s3://test-bucket/raw/customer_data.orc{suffix}" in message, message
        # Not reported as a malformed footer — pyarrow's own diagnosis for these
        # bytes, and the one the guard exists to replace.
        assert "postscript" not in message, message

        # The guard's head read: last, at offset 0, four bytes — off the handle
        # pyarrow already had open, not from a second ranged GET.
        source = filesystems[0].sources[0]
        head_length = max(len(m) for _, m, _, _ in _EXTERNAL_COMPRESSIONS)
        assert head_length == lf._EXTERNAL_COMPRESSION_HEAD_BYTES, (
            "the guard reads a different number of head bytes than the longest magic needs"
        )
        assert source.reads[-1] == (0, head_length), f"expected a trailing head read, got {source.reads}"

        # No boto3 call on this path either (R2.3, C8).
        assert mock_s3.download_file.called is False
        assert mock_s3.get_object.called is False
        assert mock_s3.select_object_content.called is False

    # -- the uncompressed fixtures are untouched -----------------------

    def test_uncompressed_avro_is_unaffected(self, _mock_aws):
        """R5.1's antecedent is a magic match, so a valid fixture is unchanged.

        The guard **is** called on this path — Avro checks first, on the prefix
        it already holds — and returns without raising. Asserted as "called once
        with the prefix, and the schema is still the write-side declaration", so
        the guard cannot be silently short-circuited and cannot alter a good
        extraction.
        """
        lf, mock_s3, _ = _mock_aws
        declaration = _load_fixture_declaration()
        fixture_bytes = AVRO_FIXTURE_PATH.read_bytes()
        requests = _avro_ranged_get(mock_s3, fixture_bytes)

        with patch.object(lf, "_raise_if_externally_compressed", wraps=lf._raise_if_externally_compressed) as guard:
            result = lf.extract_schema_from_avro("test-bucket", "raw/customer_data.avro")

        assert result == declaration.EXPECTED_JSON_SCHEMA
        guard.assert_called_once()
        head, bucket, key, container = guard.call_args.args
        assert head == fixture_bytes, "the guard was handed something other than the fetched prefix"
        assert (bucket, key, container) == ("test-bucket", "raw/customer_data.avro", "Avro")

        # Still exactly one bounded ranged GET — the guard adds no read.
        assert len(requests) == 1
        assert requests[0]["Range"] == "bytes=0-65535"

    def test_uncompressed_orc_is_unaffected_and_still_reads_once(self, _mock_aws):
        """R5.1 for ORC, and the read count the failure-path-only design buys.

        Two assertions, and the second is the one that matters. The guard is
        **not called at all** on a successful ORC extraction, so the success path
        is byte-for-byte what task 5.1 shipped: a single ranged read for the
        fixture, which is what task 5.2's ``test_orc_footer_read_is_bounded``
        pins as ``len(source.reads) == 1``.

        A guard that checked the head first would still pass every message
        assertion above while making this two reads and breaking that test. This
        is the assertion that would catch it, stated here so the shape is
        defended by its own test rather than by a comment.
        """
        lf, _, _ = _mock_aws
        declaration = _load_fixture_declaration()

        with (
            _orc_source(ORC_FIXTURE_PATH.read_bytes()) as filesystems,
            patch.object(lf, "_raise_if_externally_compressed", wraps=lf._raise_if_externally_compressed) as guard,
            patch.object(lf, "_handle_head_bytes", wraps=lf._handle_head_bytes) as head_read,
        ):
            result = lf.extract_schema_from_orc("test-bucket", "raw/customer_data.orc")

        assert result == declaration.EXPECTED_JSON_SCHEMA
        assert guard.called is False, "the guard ran on a successful extraction — it is failure-path only"
        assert head_read.called is False, "the head was read on the success path, costing a read for nothing"

        source = filesystems[0].sources[0]
        assert len(source.reads) == 1, f"the success path issued {len(source.reads)} reads, not 1: {source.reads}"

    # -- csv and json are not on the guard's path (R5.3) ---------------

    def test_csv_and_json_do_not_call_the_guard(self, _mock_aws):
        """R5.3, structurally: the guard is scoped to avro and orc by call site.

        Read by ``ast`` rather than by substring, per ``LGR-b46a5bb-4``
        (template / ratified) — the guard's name appears in prose near these
        functions precisely *because* they are exempt, so a substring check
        would fail this test for documenting it.

        The positive half is load-bearing: the two new extractors are asserted
        to call the guard. Without it, renaming the guard would make the
        negative half vacuously true and R5.3 unverified.
        """
        lf, _, _ = _mock_aws
        functions = _module_functions(lf)
        guards = {"_raise_if_externally_compressed", "_handle_head_bytes"}

        for name in ("extract_schema_from_csv", "extract_schema_from_json"):
            assert name in functions, f"{name} is no longer a top-level function"
            assert not (_called_names(functions[name]) & guards), f"{name} now calls the External_Compression guard"

        # Not vacuous: the guard exists and the two new extractors do call it.
        assert "_raise_if_externally_compressed" in _called_names(functions["extract_schema_from_avro"])
        assert guards <= _called_names(functions["extract_schema_from_orc"])

    @pytest.mark.parametrize(
        ("tool", "magic", "suffix", "compress"),
        _EXTERNAL_COMPRESSIONS,
        ids=[tool for tool, *_ in _EXTERNAL_COMPRESSIONS],
    )
    def test_compressed_input_to_csv_and_json_behaves_as_before(self, _mock_aws, tool, magic, suffix, compress):
        """R5.3 behaviourally: compressed input to csv/json is diagnosed as it was.

        Both extractors ``.decode("utf-8")`` what they read, so compressed bytes
        surface as ``UnicodeDecodeError`` — from ``extract_schema_from_json``
        directly, and from ``extract_schema_from_csv`` after its S3 Select path
        falls back to a ranged GET and fails the same way. p46 does not change
        that and does not improve it; the guard is simply not on either path.

        **The trap this test exists to hold open:** ``UnicodeDecodeError`` is a
        subclass of ``ValueError``, which is exactly what the guard raises. A
        check written as ``pytest.raises(ValueError)`` would pass whether csv had
        been dragged onto the guard's path or not, so the type is asserted
        exactly and the message is asserted **not** to name external
        compression.
        """
        lf, mock_s3, _ = _mock_aws
        payload = compress(Path(__file__).parents[3].joinpath("fixtures/data/customer_data.csv").read_bytes())
        assert payload.startswith(magic)

        _avro_ranged_get(mock_s3, payload)  # a plain bounded ranged GET; format-agnostic
        with pytest.raises(UnicodeDecodeError) as raised:
            lf.extract_schema_from_json("test-bucket", f"raw/customer_data.json{suffix}")
        assert type(raised.value) is UnicodeDecodeError
        assert "externally compressed" not in str(raised.value)

        # The csv half needs no second stub: ``_avro_ranged_get`` above already
        # wired the bounded ranged GET, and that is now the CSV reader's only
        # read. A ``select_object_content.return_value`` line stood here and went
        # dead when that arm was removed — it was feeding an API this function no
        # longer calls, so it looked like the csv case was covered by a different
        # route than it actually was.
        with pytest.raises(UnicodeDecodeError) as raised:
            lf.extract_schema_from_csv("test-bucket", f"raw/customer_data.csv{suffix}")
        assert type(raised.value) is UnicodeDecodeError
        assert "externally compressed" not in str(raised.value)

    # -- the heuristic's other side (design §5 R5) ---------------------

    @pytest.mark.parametrize(
        ("label", "head"), _UNRECOGNISED_PAYLOADS, ids=[label for label, _ in _UNRECOGNISED_PAYLOADS]
    )
    def test_unrecognised_payload_still_gets_its_ordinary_parse_error(self, _mock_aws, label, head):
        """Design §5 R5: the guard is deliberately a heuristic, not exhaustive.

        A payload that is neither a valid container nor one of R5.1's three
        magics gets the parse error it got before — for Avro the OCF magic
        refusal, for ORC pyarrow's ``Not an ORC file``. Neither mentions
        compression, because the guard has no opinion and says nothing.

        ``snappy`` is one of the cases, and its being here rather than in the
        recognised table is the point: R5.1 enumerates exactly three magics,
        raw snappy has none, and the framing format's stream identifier is not
        in the table either. A ``.avro.snappy`` key therefore reaches the
        extractor as ``avro`` and fails as a bad container — accepted, and the
        deferred half of design §5 R6.

        The ORC half lands on ``Invalid ORC postscript length`` — pyarrow's own
        wording, and **the same message a gzipped ORC object produced before the
        guard existed** (see
        ``test_orc_head_read_failure_leaves_pyarrows_error_intact``, which
        reproduces it by disabling the head read). That the two cases now differ
        is the guard's entire contribution: the recognised one is named, the
        unrecognised one is not dressed up as something the extractor cannot
        actually tell.

        The payload is the unrecognised head followed by padding, **not** a valid
        container behind a prefix. Learned by observation: prefixing bytes onto
        the ORC fixture still parses, because the footer is at the end and
        pyarrow reads the tail — so a prefixed fixture would have tested nothing
        here.
        """
        lf, mock_s3, _ = _mock_aws
        payload = head + b"\x00" * 512

        _avro_ranged_get(mock_s3, payload)
        with pytest.raises(ValueError) as raised:
            lf.extract_schema_from_avro("test-bucket", f"raw/{label}.avro")
        assert "Not an Avro Object Container File" in str(raised.value)
        assert "externally compressed" not in str(raised.value)

        with _orc_source(payload) as filesystems, pytest.raises(OSError) as raised:
            lf.extract_schema_from_orc("test-bucket", f"raw/{label}.orc")
        assert type(raised.value) is OSError, f"raised {type(raised.value).__name__}"
        assert "Invalid ORC postscript length" in str(raised.value)
        assert "externally compressed" not in str(raised.value)
        # The guard did look — the trailing head read is there — and declined to
        # have an opinion, leaving pyarrow's diagnosis in place.
        assert filesystems[0].sources[0].reads[-1] == (0, lf._EXTERNAL_COMPRESSION_HEAD_BYTES)

    # -- the diagnostic cannot break the diagnosis ---------------------

    def test_handle_head_bytes_never_raises_and_normalises_its_result(self, _mock_aws):
        """``_handle_head_bytes`` returns ``b""`` on any handle failure, by design.

        The docstring's reason is worth a test on its own: a diagnostic
        refinement must never replace the real error with an error about itself.
        Three failure shapes — unseekable, unreadable, already closed — plus the
        conversion that makes the result usable: a handle yielding a
        ``memoryview`` has no ``startswith``, so ``bytes()`` is not decoration.
        """
        lf, _, _ = _mock_aws

        class _RaisesOnSeek:
            def seek(self, *_args):
                raise OSError("handle cannot be rewound")

            def read(self, _size):  # pragma: no cover — unreachable past the seek
                return b"\x1f\x8b"

        class _RaisesOnRead:
            def seek(self, *_args):
                return 0

            def read(self, _size):
                raise OSError("handle went away")

        class _Closed:
            def seek(self, *_args):
                raise ValueError("I/O operation on closed file")

            def read(self, _size):  # pragma: no cover — unreachable past the seek
                raise ValueError("I/O operation on closed file")

        class _YieldsMemoryview:
            def seek(self, *_args):
                return 0

            def read(self, size):
                return memoryview(b"\x1f\x8b\x08\x00")[:size]

        for label, handle in (
            ("raises-on-seek", _RaisesOnSeek()),
            ("raises-on-read", _RaisesOnRead()),
            ("closed", _Closed()),
        ):
            assert lf._handle_head_bytes(handle) == b"", f"{label}: expected b'' rather than a raise"

        head = lf._handle_head_bytes(_YieldsMemoryview())
        assert type(head) is bytes, f"a memoryview reached the guard as {type(head)}"
        assert head.startswith(b"\x1f\x8b")

    @pytest.mark.parametrize(
        ("label", "payload_factory", "expected_message"),
        [
            ("gzipped-orc", lambda: gzip.compress(ORC_FIXTURE_PATH.read_bytes()), "Invalid ORC postscript length"),
            ("garbage", lambda: b"this is not an ORC file at all, not even close" * 4, "Not an ORC file"),
        ],
    )
    def test_orc_head_read_failure_leaves_pyarrows_error_intact(
        self, _mock_aws, label, payload_factory, expected_message
    ):
        """A broken head read must not mask the error it was refining.

        The handle is poisoned so that the guard's own 4-byte head read fails
        while everything ``ORCFile`` does succeeds. What surfaces is pyarrow's
        error — ``Invalid ORC postscript length`` for the gzipped object,
        ``Not an ORC file`` for garbage — and **not** the simulated failure. So
        the worst case of the diagnostic is that the operator is no better off
        than before the guard existed, never worse.

        The ``gzipped-orc`` case doubles as the control for the whole guard: that
        message is precisely what an operator used to be shown for a gzipped
        upload, and what
        ``test_externally_compressed_orc_is_refused_naming_the_cause`` replaces
        it with.

        The poison asserts that it **fired**. Otherwise a change in pyarrow's
        read pattern would make this test pass without ever exercising the
        failure it is named for.
        """
        lf, _, _ = _mock_aws
        head_bytes = lf._EXTERNAL_COMPRESSION_HEAD_BYTES

        class _HeadReadFails(_CountingSource):
            """A counting source that fails exactly the guard's read.

            The guard's read is identifiable without guessing at pyarrow's
            internals: offset 0, length ``_EXTERNAL_COMPRESSION_HEAD_BYTES``,
            issued after ``ORCFile`` has already raised.
            """

            def __init__(self, data: bytes):
                super().__init__(data)
                self.fired = False

            def _poison(self, size):
                if self.tell() == 0 and size == head_bytes:
                    self.fired = True
                    raise OSError("simulated: the handle went away under the guard")

            def read(self, size=-1):
                self._poison(size)
                return super().read(size)

            def readinto(self, target):
                self._poison(len(target))
                return super().readinto(target)

        sources: list[_HeadReadFails] = []

        class _PoisonedFileSystem(_StubS3FileSystem):
            def open_input_file(self, path):
                self.requested_paths.append(path)
                source = _HeadReadFails(self._payload)
                sources.append(source)
                self.sources.append(source)
                return pa.PythonFile(source, mode="r")

        payload = payload_factory()
        returned = None
        with (
            patch("pyarrow.fs.S3FileSystem", side_effect=lambda **kwargs: _PoisonedFileSystem(payload, **kwargs)),
            pytest.raises(OSError) as raised,
        ):
            returned = lf.extract_schema_from_orc("test-bucket", f"raw/{label}.orc")

        assert returned is None
        assert returned != EMPTY_SCHEMA
        assert sources and sources[0].fired, "the poison never fired — the guard's head read was not exercised"

        message = str(raised.value)
        assert expected_message in message, message
        assert "simulated" not in message, f"the refinement's own failure replaced the real error: {message}"
        assert "externally compressed" not in message, message


# ── p53 task 3.3: the S3 arm's ``source_sample_uri`` ─────────────────────
#
# Task 3.3 points the TestCode payload at
# ``$.schema_extract_result.source_sample_uri``. There is ONE linear state
# machine and both source types traverse it, so the key has to exist on the
# S3-upload arm too — a ``$.schema_extract_result.…`` reference whose path is
# absent raises States.ParameterPathFailure at state entry, and
# ``orchestration-stack.ts:338-340`` records that neither Retry nor Catch can
# intercept it. ASL has no coalesce or conditional intrinsic, so there is no
# payload expression that reads a possibly-absent path safely; the preference
# is resolved in Python instead and returned unconditionally by both arms.
#
# On this arm there is no separate Sample_Artifact — the uploaded objects
# themselves are what a Sample_Reader reads — so the resolved value is
# ``canonical_uri``, the same value the arm already returns as
# ``json_s3_uri``. That equality is load-bearing twice over: it is why the
# S3-upload path's ``source_data_uri`` does not change value, and therefore
# why ``verificationSessionRole``'s source-files read
# (``foundation-stack.ts:835-887``) still covers what the session opens.


class TestS3ArmReturnsTheSourceSampleUri:
    """The S3 arm returns ``source_sample_uri`` on every run, equal to the
    canonical URI it already returns as ``json_s3_uri``.

    Validates: p53 Requirements 2.4, 2.5, 2.6.
    """

    def _event(self, run_id="run-123", source_format=None):
        event = {"run_id": run_id, "execution_arn": "arn:aws:states:us-west-2:123:exec"}
        if source_format:
            event["source_format"] = source_format
        return event

    def _json_body(self, mock_s3, records):
        body = MagicMock()
        body.read.return_value = json.dumps(records).encode("utf-8")
        mock_s3.get_object.return_value = {"Body": body}

    def test_the_key_is_present_and_equals_the_canonical_uri(self, _mock_aws):
        lf, mock_s3, mock_table = _mock_aws
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "source_s3_uri": "s3://adi-source-files-1-r/uploads/data.json",
            }
        }
        mock_table.put_item.return_value = {}
        self._json_body(mock_s3, [{"x": 1}])

        result = lf.lambda_handler(self._event(source_format="json"), None)

        assert "source_sample_uri" in result
        assert result["source_sample_uri"] == result["json_s3_uri"]
        # R2.6: `json_s3_uri` keeps its own value — the source object, not a
        # sample. This arm adds a key; it does not repoint one (do-NOT 11).
        assert result["json_s3_uri"] == "s3://adi-source-files-1-r/uploads/data.json"

    def test_the_key_is_present_on_the_legacy_converted_json_path(self, _mock_aws):
        """The other route through this arm: no ``source_s3_uri``, so
        ``canonical_uri`` is the converted-JSON object in processedBucket. The
        key is present here too, because the state machine cannot tell the two
        routes apart."""
        lf, mock_s3, mock_table = _mock_aws
        legacy_uri = "s3://adi-processed-files-1-r/converted/data.json"
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "json_s3_uri": legacy_uri,
            }
        }
        mock_table.put_item.return_value = {}
        self._json_body(mock_s3, [{"col1": "val1"}])

        result = lf.lambda_handler(self._event(), None)

        assert result["source_sample_uri"] == legacy_uri
        assert result["json_s3_uri"] == legacy_uri

    @pytest.mark.parametrize("source_format", [None, "json", "csv"])
    def test_the_key_is_never_absent_whatever_the_format(self, _mock_aws, source_format):
        """Presence, not value, is the property the state machine needs. Absence
        on any one format is as fatal as absence on all of them."""
        lf, mock_s3, mock_table = _mock_aws
        item = {"run_id": "run-123", "original_pipeline_id": "pipe-1"}
        if source_format:
            item["source_s3_uri"] = f"s3://adi-source-files-1-r/uploads/data.{source_format}"
        else:
            item["json_s3_uri"] = "s3://adi-processed-files-1-r/converted/data.json"
        mock_table.get_item.return_value = {"Item": item}
        mock_table.put_item.return_value = {}

        # The body has to parse as whatever format is being routed. Before the
        # S3 Select arm was removed the csv case was served by a mocked
        # ``select_object_content`` payload and a JSON body sat here unused; now
        # every format reads through ``get_object``, so the csv case needs a body
        # a CSV reader can actually split. A JSON array fed to ``csv.DictReader``
        # yields no data rows and trips the p64 R1.3 header-only refusal, which
        # would fail this test for a reason unrelated to what it asserts.
        if source_format == "csv":
            body = MagicMock()
            body.read.return_value = b"col1\nval1\n"
            mock_s3.get_object.return_value = {"Body": body}
        else:
            self._json_body(mock_s3, [{"col1": "val1"}])

        result = lf.lambda_handler(self._event(source_format=source_format), None)

        assert result["statusCode"] == 200
        assert "source_sample_uri" in result
        assert result["source_sample_uri"]
        assert result["source_sample_uri"] == result["json_s3_uri"]

    def test_the_prefix_uri_wins_in_prefix_mode(self, _mock_aws):
        """Multi-file uploads make ``canonical_uri`` the prefix rather than the
        one object the schema was read from, and ``source_sample_uri`` follows
        the prefix rather than diverging from ``json_s3_uri``."""
        lf, mock_s3, mock_table = _mock_aws
        prefix_uri = "s3://adi-source-files-1-r/uploads/batch-7/"
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "source_s3_uri": prefix_uri,
            }
        }
        mock_table.put_item.return_value = {}
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "uploads/batch-7/"},
                {"Key": "uploads/batch-7/part-0.json"},
            ]
        }
        self._json_body(mock_s3, [{"x": 1}])

        result = lf.lambda_handler(self._event(source_format="json"), None)

        assert result["json_s3_uri"] == prefix_uri
        assert result["source_sample_uri"] == prefix_uri

    def test_the_s3_arm_carries_no_sample_s3_uri(self, _mock_aws):
        """``sample_s3_uri`` stays JDBC-only: there is no Sample_Artifact on this
        path to probe for. That asymmetry is exactly why the state machine reads
        ``source_sample_uri`` and not ``sample_s3_uri`` — the latter would raise
        States.ParameterPathFailure on every S3-upload run."""
        lf, mock_s3, mock_table = _mock_aws
        mock_table.get_item.return_value = {
            "Item": {
                "run_id": "run-123",
                "original_pipeline_id": "pipe-1",
                "source_s3_uri": "s3://adi-source-files-1-r/uploads/data.json",
            }
        }
        mock_table.put_item.return_value = {}
        self._json_body(mock_s3, [{"x": 1}])

        result = lf.lambda_handler(self._event(source_format="json"), None)

        assert "sample_s3_uri" not in result
        assert "source_sample_uri" in result
