# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for get_source_path() -- mock the state table and verify which
object the generated Glue job is pointed at.

Validates AC-3 from Phase 7 requirements.

AMENDED IN PLACE by p55 task 4.3, with the reason stated at each assertion
(``LGR-60c4985-6`` / ``LGR-9e76249-10``: a deliberate change to an assertion
stays legible as a change and is never silently deleted).

What moved, and why. Before p55 this function read ``original_s3_uri`` off the
**file_validation** step, unconditionally. p55 W3 makes an Excel_Workbook a
supported source, and Spark has no Excel datasource, so ``schema_extract``
converts the workbook and resolves ``readable_source_uri`` -- the object the job
should actually read -- once, in Python (R13.3/R13.4, ``LGR-5c539a6-2``). This
function now reads that key off the **schema_extract** step, which
``LGR-28b4d75-1``'s derive-once rule requires: the conversion gate is read in one
place and every consumer takes the resolved value.

For every Native_Format_Set member ``readable_source_uri`` IS the original
upload, so nothing about a CSV, JSON, Parquet, Avro or ORC pipeline changes --
which is why these tests assert the *same returned URI* through a different key
rather than a different outcome.

The nested ``output_data.s3_uris.source_file`` fallback is retained and its
subject changed: it now covers a **pre-p55 schema_extract row**, which carries no
``readable_source_uri``. That is risk R5.4's accepted cost (task 4.4 measured 19
replayable runs) handled as a Python fallback, and it is correct by construction
-- no Conversion_Required_Set format existed before p55, so such a row's readable
object is necessarily its original.
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import boto3
import pytest
from moto import mock_aws

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_glue_pipeline",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ["MCP_SIGV4_ENABLED"] = "false"
os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_mods = {
    "strands": mock.MagicMock(),
    "strands.agent": mock.MagicMock(),
    "strands.tools.mcp": mock.MagicMock(),
    "streamable_http_sigv4": mock.MagicMock(),
    "mcp": mock.MagicMock(),
    "mcp.client": mock.MagicMock(),
    "mcp.client.streamable_http": mock.MagicMock(),
    "schema_evolution": mock.MagicMock(),
    "metrics_facade": mock.MagicMock(),
    "prompt_template_loader": mock.MagicMock(
        load_prompt_template=mock.MagicMock(return_value="mock"),
    ),
}


def _load_module():
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "glue_pipeline_lf",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["glue_pipeline_lf"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()
# P21 Task 7.3: get_source_path moved to the sibling ``artifacts`` module. Patch
# the state table on the executing module's globals (resolved via ``__globals__``)
# so the fixture reaches whichever ``artifacts`` instance the re-exported function
# actually runs in — robust against the importlib/patch.dict module-identity split.
get_source_path = _lf.get_source_path
_artifacts_globals = get_source_path.__globals__

TABLE_NAME = "agentic_data_ingestion_state"
RUN_ID = "run-2025-01-15-12-00-00-aaaa1111"
EXPECTED = "s3://adi-processed-files-dev/pipe-abc/data.json"


def _create_table(ddb):
    ddb.create_table(
        TableName=TABLE_NAME,
        KeySchema=[
            {"AttributeName": "run_id", "KeyType": "HASH"},
            {"AttributeName": "step_name", "KeyType": "RANGE"},
        ],
        AttributeDefinitions=[
            {"AttributeName": "run_id", "AttributeType": "S"},
            {"AttributeName": "step_name", "AttributeType": "S"},
        ],
        BillingMode="PAY_PER_REQUEST",
    )


@pytest.fixture
def state_table():
    with mock_aws():
        os.environ["AWS_DEFAULT_REGION"] = "us-east-1"
        os.environ["AWS_ACCESS_KEY_ID"] = "testing"
        os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
        os.environ["AWS_SECURITY_TOKEN"] = "testing"
        os.environ["AWS_SESSION_TOKEN"] = "testing"
        ddb = boto3.resource("dynamodb", region_name="us-east-1")
        _create_table(ddb)
        table = ddb.Table(TABLE_NAME)
        orig = _artifacts_globals["pipeline_runs_table"]
        _artifacts_globals["pipeline_runs_table"] = table
        yield table
        _artifacts_globals["pipeline_runs_table"] = orig


class TestGetSourcePathTopLevel:
    def test_returns_the_readable_source_uri(self, state_table):
        """AMENDED (p55 4.3): the key is ``readable_source_uri`` on the
        ``schema_extract`` row, not ``original_s3_uri`` on ``file_validation``.

        For a Native_Format_Set member the value is identical -- ``EXPECTED``
        below is the same URI this test asserted before p55 -- so this is a change
        of which attribute is authoritative, not of what a CSV pipeline reads.
        """
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "schema_extract",
                "status": "success",
                "readable_source_uri": EXPECTED,
                "readable_source_format": "json",
            }
        )
        assert get_source_path(RUN_ID) == EXPECTED

    def test_returns_the_converted_object_for_a_conversion_required_format(self, state_table):
        """R13.3: an Excel run is pointed at the converted CSV, not the workbook.

        NEW in p55 4.3, and the whole point of the change -- risk R5.2 is that a
        consumer keeps reading the original URI, in which case Spark is handed a
        workbook. The row here is what ``schema_extract``'s ``excel`` branch
        writes.
        """
        converted = "s3://adi-processed-files-dev/converted/pipe-abc/run-1/source.csv"
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "schema_extract",
                "status": "success",
                # `json_s3_uri` stays the workbook -- it is NOT repointed
                # (`LGR-5c539a6-2`), which is why the readable URI is its own key.
                "json_s3_uri": "s3://adi-source-files-dev/uploads/book.xlsx",
                "readable_source_uri": converted,
                "readable_source_format": "csv",
            }
        )
        assert get_source_path(RUN_ID) == converted


class TestGetSourcePathNested:
    def test_returns_from_output_data_when_the_schema_extract_row_predates_p55(self, state_table):
        """AMENDED (p55 4.3): the fallback's subject is a pre-p55 row.

        The nested ``output_data.s3_uris.source_file`` read is unchanged and still
        part of the ONE derivation; what changed is when it fires. A
        ``schema_extract`` row carrying no ``readable_source_uri`` was written
        before p55, and for such a row the readable object IS the original one,
        because no Conversion_Required_Set format existed then (risk R5.4, task
        4.4's 19 replayable runs).
        """
        nested = "s3://adi-processed-files-dev/pipe-xyz/nested.json"
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "schema_extract",
                "status": "success",
                "json_s3_uri": nested,
            }
        )
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "file_validation",
                "status": "completed",
                "output_data": {"s3_uris": {"source_file": nested}},
            }
        )
        assert get_source_path(RUN_ID) == nested

    def test_the_fallback_fires_when_the_schema_extract_row_is_absent_entirely(self, state_table):
        nested = "s3://adi-processed-files-dev/pipe-xyz/nested.json"
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "file_validation",
                "status": "completed",
                "output_data": {"s3_uris": {"source_file": nested}},
            }
        )
        assert get_source_path(RUN_ID) == nested


class TestGetSourcePathMissing:
    def test_raises_when_no_step(self, state_table):
        with pytest.raises(ValueError, match="no file_validation"):
            get_source_path(RUN_ID)

    def test_raises_when_no_uri(self, state_table):
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "file_validation",
                "status": "completed",
            }
        )
        with pytest.raises(ValueError, match="No source URI found"):
            get_source_path(RUN_ID)


class TestGetSourcePathPrecedence:
    def test_the_readable_source_uri_wins_over_the_nested_fallback(self, state_table):
        """AMENDED (p55 4.3): the resolved key outranks the pre-p55 fallback.

        Same claim as the pre-p55 ``test_top_level_wins`` -- the authoritative
        value is preferred and the fallback is only reached when it is absent --
        stated over the two attributes that now hold those roles.
        """
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "schema_extract",
                "status": "success",
                "readable_source_uri": EXPECTED,
                "readable_source_format": "json",
            }
        )
        state_table.put_item(
            Item={
                "run_id": RUN_ID,
                "step_name": "file_validation",
                "status": "completed",
                "output_data": {"s3_uris": {"source_file": "s3://other/o.json"}},
            }
        )
        assert get_source_path(RUN_ID) == EXPECTED

    def test_no_source_format_argument_is_accepted(self, state_table):
        """p55 task 4.1, finding 1: the dead ``source_format`` parameter is gone.

        It was declared ``source_format: str = ""``, referenced nowhere in the
        body, and passed a real value by ``lambda_function.py``. The docstring
        described behaviour the body did not have. Asserted rather than assumed,
        so a future reader cannot reintroduce a format opinion here -- the
        conversion gate has exactly one reader and it is ``schema_extract``
        (``LGR-28b4d75-1``).
        """
        import inspect

        assert list(inspect.signature(get_source_path).parameters) == ["run_id"]
