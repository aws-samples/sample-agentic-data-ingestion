# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A target S3 location is refused at create and edit unless it names one valid bucket (security finding S1).

The create_glue_pipeline role builder interpolates the bucket of a target's S3
location into the pipeline's Glue role: ``arn:aws:s3:::{bucket}/*`` with
Get/Put/Delete for an S3 Parquet output path, and the same for a Redshift JDBC
target's ``temp_dir``. IAM reads ``*`` and ``?`` there as wildcards, so
``s3_path: "s3://*/"`` granted every object in the account. ``create_pipeline``
had no S3_PARQUET branch at all, and ``edit_pipeline`` stores a submitted
``target_config`` key verbatim. The role builder now refuses these values too
(``tests/contracts/test_glue_job_role_boundary_covers_inline_policy.py``); these
tests cover the API boundary, where the user gets a 400 instead of a pipeline
whose first run fails.

Expectations come from S3's bucket naming rules: 3-63 characters, lowercase
letters, digits, dots and hyphens, beginning and ending with a letter or digit,
no two adjacent dots, not in IPv4 form, behind an ``s3://`` scheme.
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)

try:
    from handlers import pipelines_crud as pipelines
finally:
    # A stub left in sys.modules leaks into every module collected after this
    # one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINES_TABLE = "agentic_data_ingestion_pipelines"
PIPELINE_ID = "parquet-pipeline-1234abcd"
VALID_S3_PATH = "s3://customer-analytics-lake/curated/"

#: Each breaks one S3 bucket naming rule, or is not an s3:// URI at all.
HOSTILE = [
    pytest.param("s3://*/", id="wildcard-bucket"),
    pytest.param("s3://a*b/x", id="embedded-wildcard"),
    pytest.param("s3://?/", id="question-mark"),
    pytest.param("s3://Bucket/x", id="uppercase"),
    pytest.param("s3://ab/", id="two-characters"),
    pytest.param("s3://bucket..x/", id="adjacent-dots"),
    pytest.param("s3://192.168.1.1/x", id="ipv4-form"),
    pytest.param("s3:///x", id="no-bucket"),
    pytest.param("https://customer-analytics-lake/x", id="not-s3"),
    pytest.param(7, id="not-a-string"),
]

#: The spelling the wizard sends, and the two older ones the role builder still
#: grants an output bucket for.
S3_OUTPUT_TYPES = ["S3_PARQUET", "S3", "S3_BUCKET"]

JDBC_TARGET = {
    "type": "JDBC",
    "jdbc_url": "jdbc:redshift://cluster.example:5439/dev",
    "jdbc_driver": "com.amazon.redshift.jdbc.Driver",
    "jdbc_table": "public.customers",
    "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:db-Abc123",
    "database_type": "redshift",
}


def _create_body(target_config: dict) -> str:
    return json.dumps(
        {
            "pipeline_name": "s3 location probe",
            "source_type": "S3_UPLOAD",
            "target_schema": "customers-schema",
            "ai_model": "us.anthropic.claude-sonnet-4-5-20250929-v1:0",
            "target_config": target_config,
        }
    )


@pytest.fixture
def table(mock_dynamodb_tables, monkeypatch):
    table = mock_dynamodb_tables[PIPELINES_TABLE]
    monkeypatch.setattr(pipelines, "pipelines_table", table)
    return table


def _error(result: dict) -> str:
    return json.loads(result["body"])["error"]


# ── create ─────────────────────────────────────────────────────────────


class TestCreate:
    @pytest.mark.parametrize("target_type", S3_OUTPUT_TYPES)
    @pytest.mark.parametrize("s3_path", HOSTILE)
    def test_a_hostile_s3_path_is_a_400_and_writes_nothing(self, table, target_type, s3_path):
        result = pipelines.create_pipeline(_create_body({"type": target_type, "s3_path": s3_path}), event={})

        assert result["statusCode"] == 400, result["body"]
        assert "target_config.s3_path" in _error(result)
        assert table.scan()["Items"] == [], "a refused create writes no record"

    @pytest.mark.parametrize("target_type", S3_OUTPUT_TYPES)
    @pytest.mark.parametrize(
        "target_config",
        [
            pytest.param({}, id="absent"),
            pytest.param({"s3_path": ""}, id="empty"),
            pytest.param({"s3_path": None}, id="null"),
            pytest.param({"output_path": VALID_S3_PATH}, id="only-the-legacy-output_path"),
        ],
    )
    def test_an_s3_output_target_without_s3_path_is_a_400(self, table, target_type, target_config):
        result = pipelines.create_pipeline(_create_body({"type": target_type, **target_config}), event={})

        assert result["statusCode"] == 400, result["body"]
        assert "s3_path is required" in _error(result)
        assert table.scan()["Items"] == []

    @pytest.mark.parametrize("key", ["output_path", "target_path"])
    @pytest.mark.parametrize("value", HOSTILE)
    def test_a_hostile_legacy_output_key_is_a_400_beside_a_valid_s3_path(self, table, key, value):
        """Supplied keys are checked too, though the role builder reads `s3_path` first."""
        target_config = {"type": "S3_PARQUET", "s3_path": VALID_S3_PATH, key: value}

        result = pipelines.create_pipeline(_create_body(target_config), event={})

        assert result["statusCode"] == 400, result["body"]
        assert f"target_config.{key}" in _error(result)
        assert table.scan()["Items"] == []

    @pytest.mark.parametrize("target_type", ["JDBC", "RDS_DATABASE"])
    @pytest.mark.parametrize("temp_dir", HOSTILE)
    def test_a_hostile_temp_dir_on_a_jdbc_target_is_a_400_before_any_glue_call(
        self, table, monkeypatch, target_type, temp_dir
    ):
        augment = MagicMock()
        monkeypatch.setattr(pipelines, "_augment_glue_connection_config", augment)

        result = pipelines.create_pipeline(
            _create_body({**JDBC_TARGET, "type": target_type, "temp_dir": temp_dir}), event={}
        )

        assert result["statusCode"] == 400, result["body"]
        assert "target_config.temp_dir" in _error(result)
        augment.assert_not_called()
        assert table.scan()["Items"] == []

    @pytest.mark.parametrize("target_type", S3_OUTPUT_TYPES)
    def test_a_valid_s3_path_creates_the_pipeline_and_stores_it_verbatim(self, table, target_type):
        result = pipelines.create_pipeline(_create_body({"type": target_type, "s3_path": VALID_S3_PATH}), event={})

        assert result["statusCode"] == 201, result["body"]
        [item] = table.scan()["Items"]
        assert item["target_config"]["s3_path"] == VALID_S3_PATH


# ── edit ───────────────────────────────────────────────────────────────


def _store(table, target_config: dict) -> None:
    table.put_item(
        Item={
            "pipeline_id": PIPELINE_ID,
            "pipeline_name": "parquet",
            "status": "active",
            "source_type": "S3_UPLOAD",
            "target_config": target_config,
        }
    )


def _edit(target_config: dict) -> dict:
    return pipelines.edit_pipeline(PIPELINE_ID, json.dumps({"target_config": target_config}), event={})


def _stored_target(table) -> dict:
    return table.get_item(Key={"pipeline_id": PIPELINE_ID})["Item"]["target_config"]


class TestEdit:
    @pytest.mark.parametrize("s3_path", HOSTILE)
    def test_a_hostile_s3_path_is_a_400_and_leaves_the_record_unchanged(self, table, s3_path):
        stored = {"type": "S3_PARQUET", "s3_path": VALID_S3_PATH, "write_mode": "append"}
        _store(table, stored)

        result = _edit({"s3_path": s3_path})

        assert result["statusCode"] == 400, result["body"]
        assert "target_config.s3_path" in _error(result)
        assert _stored_target(table) == stored

    @pytest.mark.parametrize("temp_dir", HOSTILE)
    def test_a_hostile_temp_dir_is_a_400_and_leaves_the_record_unchanged(self, table, temp_dir):
        stored = {**JDBC_TARGET, "write_mode": "overwrite"}
        _store(table, stored)

        result = _edit({"temp_dir": temp_dir})

        assert result["statusCode"] == 400, result["body"]
        assert "target_config.temp_dir" in _error(result)
        assert _stored_target(table) == stored

    def test_switching_to_s3_parquet_under_a_stored_hostile_path_is_a_400(self, table):
        """The EFFECTIVE value is checked: the edit carries only `type`, the path is stored."""
        stored = {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "append", "s3_path": "s3://*/"}
        _store(table, stored)

        result = _edit({"type": "S3_PARQUET"})

        assert result["statusCode"] == 400, result["body"]
        assert "target_config.s3_path" in _error(result)
        assert _stored_target(table) == stored

    def test_switching_to_s3_parquet_with_no_location_is_a_400(self, table):
        stored = {"type": "NEW_S3_TABLE_BUCKET", "write_mode": "append"}
        _store(table, stored)

        result = _edit({"type": "S3_PARQUET"})

        assert result["statusCode"] == 400, result["body"]
        assert "output location" in _error(result)
        assert _stored_target(table) == stored

    def test_clearing_the_only_location_of_an_s3_parquet_target_is_a_400(self, table):
        stored = {"type": "S3_PARQUET", "s3_path": VALID_S3_PATH, "write_mode": "append"}
        _store(table, stored)

        result = _edit({"s3_path": ""})

        assert result["statusCode"] == 400, result["body"]
        assert _stored_target(table) == stored

    def test_resaving_a_stored_hostile_path_is_a_400(self, table):
        """The Edit Pipeline dialog re-submits the whole stored target_config."""
        stored = {"type": "S3_PARQUET", "s3_path": "s3://*/", "write_mode": "append"}
        _store(table, stored)

        result = _edit(dict(stored))

        assert result["statusCode"] == 400, result["body"]
        assert _stored_target(table) == stored

    def test_a_stored_hostile_path_can_be_corrected(self, table):
        _store(table, {"type": "S3_PARQUET", "s3_path": "s3://*/", "write_mode": "append"})

        result = _edit({"type": "S3_PARQUET", "s3_path": VALID_S3_PATH, "write_mode": "append"})

        assert result["statusCode"] == 200, result["body"]
        assert _stored_target(table)["s3_path"] == VALID_S3_PATH

    def test_a_valid_s3_path_is_stored_and_the_other_keys_are_kept(self, table):
        _store(table, {"type": "S3_PARQUET", "s3_path": VALID_S3_PATH, "write_mode": "append"})

        result = _edit({"s3_path": "s3://customer-analytics-lake/v2/"})

        assert result["statusCode"] == 200, result["body"]
        assert _stored_target(table) == {
            "type": "S3_PARQUET",
            "s3_path": "s3://customer-analytics-lake/v2/",
            "write_mode": "append",
        }

    def test_a_legacy_output_path_pipeline_can_still_be_saved(self, table):
        """On edit any of the three output keys is a location, as the role builder reads them."""
        stored = {"type": "S3_PARQUET", "output_path": VALID_S3_PATH, "write_mode": "append"}
        _store(table, stored)

        result = _edit({**stored, "write_mode": "overwrite"})

        assert result["statusCode"] == 200, result["body"]
        assert _stored_target(table)["write_mode"] == "overwrite"

    def test_a_valid_temp_dir_is_stored(self, table):
        _store(table, {**JDBC_TARGET, "write_mode": "overwrite"})

        result = _edit({"temp_dir": "s3://redshift-temp-bucket/tmp/"})

        assert result["statusCode"] == 200, result["body"]
        assert _stored_target(table)["temp_dir"] == "s3://redshift-temp-bucket/tmp/"
