# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for create_s3_table_bucket (Task 2.14).

Validates: AC-19.1, AC-19.2, AC-19.3, AC-19.4
- Mock boto3 s3tables client
- Verify create-or-get pattern
- ConflictException handling
- Correct return shape
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Stub out Lambda-layer dependencies before importing the module.
#
# A stub of this file's own, installed for the import only. This used to reuse
# whatever `env_utils` was already in sys.modules and overwrite its
# `require_env` / `get_aws_account_id` in place — harmless while an earlier
# module had left a MagicMock there, but when the real module was loaded it
# replaced the real functions for the rest of the session (LGR-c58f276-4).
_env_utils_stub = MagicMock()
_env_utils_stub.require_env = lambda name, default=None: default or name
_env_utils_stub.get_aws_account_id = lambda: "123456789012"
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = _env_utils_stub

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add lambda directory to import path
_create_infra_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_infrastructure"
    )
)
sys.path.insert(0, _create_infra_dir)

# We need to patch boto3 before importing the module
sys.modules.pop("lambda_function", None)
# Re-insert to ensure our directory is at position 0
sys.path.insert(0, _create_infra_dir)
try:
    with patch("boto3.client"), patch("boto3.resource"):
        import lambda_function as _infra_lf
        from lambda_function import create_s3_table_bucket, create_s3_table_infrastructure
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


SAMPLE_SCHEMA = {
    "name": "test_table",
    "fields": [
        {"name": "id", "type": "integer", "nullable": False},
        {"name": "name", "type": "string", "nullable": True},
        {"name": "amount", "type": "number", "nullable": True},
    ],
}

REQUIRED_RETURN_KEYS = {
    "bucket_name",
    "table_name",
    "table_bucket_arn",
    "table_arn",
    "namespace",
    "warehouse_location",
    "metadata_location",
    "status",
}


def _make_s3tables_client(bucket_exists=False, namespace_exists=False, table_exists=False):
    """Create a mock s3tables client with configurable existence states."""
    client = MagicMock()

    # ConflictException
    conflict_exc = type("ConflictException", (Exception,), {})
    client.exceptions.ConflictException = conflict_exc

    # list_table_buckets
    if bucket_exists:
        client.list_table_buckets.return_value = {
            "tableBuckets": [
                {"name": "test-bucket", "arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket"}
            ]
        }
    else:
        client.list_table_buckets.return_value = {"tableBuckets": []}

    # create_table_bucket
    client.create_table_bucket.return_value = {"arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket"}

    # list_namespaces
    if namespace_exists:
        client.list_namespaces.return_value = {"namespaces": [{"namespace": ["test_ns"]}]}
    else:
        client.list_namespaces.return_value = {"namespaces": []}

    # list_tables
    if table_exists:
        client.list_tables.return_value = {
            "tables": [
                {
                    "name": "test_table",
                    "tableARN": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table",
                }
            ]
        }
    else:
        client.list_tables.return_value = {"tables": []}

    # create_table
    client.create_table.return_value = {
        "tableARN": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table"
    }

    # get_table
    client.get_table.return_value = {
        "metadataLocation": "s3://test-bucket/metadata/test_table",
        "warehouseLocation": "s3://test-bucket/warehouse",
    }

    return client


def _mock_pipeline_metadata(bucket_name="test-bucket", namespace_name="test_ns", table_name="test_table"):
    """Return pipeline metadata with target_config."""
    return {
        "pipeline_id": "test-pipeline",
        "target_config": {
            "bucket_name": bucket_name,
            "namespace_name": namespace_name,
            "table_name": table_name,
        },
    }


class TestCreateS3TableBucket:
    """Tests for create_s3_table_bucket function."""

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_creates_all_resources_when_none_exist(self, mock_metadata, mock_boto_client):
        """When nothing exists, creates bucket, namespace, and table in order."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = _make_s3tables_client(bucket_exists=False, namespace_exists=False, table_exists=False)
        # create_s3_table_bucket now also creates a `glue` client to register the
        # Iceberg table in the Glue Catalog (the region bug that used to skip this
        # was fixed). Route glue to a separate mock so the s3tables call-count
        # assertions stay precise.
        glue = MagicMock()
        glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_boto_client.client.side_effect = lambda svc, *a, **k: glue if svc == "glue" else s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert result["status"] == "created"
        # The function uses bucket_name from target_config
        s3tables.create_table_bucket.assert_called_once_with(name="test-bucket")
        s3tables.create_namespace.assert_called_once()
        s3tables.create_table.assert_called_once()
        # Glue Catalog registration ran for the newly created table.
        glue.create_table.assert_called_once()

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_skips_creation_when_all_exist(self, mock_metadata, mock_boto_client):
        """When all resources exist, returns already_exists without creating."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = _make_s3tables_client(bucket_exists=True, namespace_exists=True, table_exists=True)
        # Route the `glue` client (Glue Catalog registration) to a separate mock
        # so it doesn't pollute the s3tables "not called" assertions below.
        glue = MagicMock()
        glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
        mock_boto_client.client.side_effect = lambda svc, *a, **k: glue if svc == "glue" else s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert result["status"] == "already_exists"
        s3tables.create_table_bucket.assert_not_called()
        s3tables.create_namespace.assert_not_called()
        s3tables.create_table.assert_not_called()

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_handles_bucket_conflict_exception(self, mock_metadata, mock_boto_client):
        """ConflictException on bucket creation is handled gracefully."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = _make_s3tables_client(bucket_exists=False, namespace_exists=False, table_exists=False)

        # First list returns empty, create raises conflict, second list returns the bucket
        s3tables.list_table_buckets.side_effect = [
            {"tableBuckets": []},  # initial check
            {
                "tableBuckets": [
                    {"name": "test-bucket", "arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket"}
                ]
            },  # after conflict
        ]
        s3tables.create_table_bucket.side_effect = s3tables.exceptions.ConflictException("already exists")
        mock_boto_client.client.return_value = s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert result["status"] == "created"
        assert result["table_bucket_arn"] == "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket"

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_handles_table_conflict_exception(self, mock_metadata, mock_boto_client):
        """ConflictException on table creation is handled gracefully."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = _make_s3tables_client(bucket_exists=True, namespace_exists=True, table_exists=False)

        # First list_tables returns empty, create raises conflict, second list returns the table
        s3tables.list_tables.side_effect = [
            {"tables": []},  # initial check
            {
                "tables": [
                    {
                        "name": "test_table",
                        "tableARN": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table",
                    }
                ]
            },  # after conflict
        ]
        s3tables.create_table.side_effect = s3tables.exceptions.ConflictException("already exists")
        mock_boto_client.client.return_value = s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert result["table_arn"] == "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table"

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_return_shape_has_required_keys(self, mock_metadata, mock_boto_client):
        """Return dict contains all required keys from the design spec."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = _make_s3tables_client(bucket_exists=False, namespace_exists=False, table_exists=False)
        mock_boto_client.client.return_value = s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert REQUIRED_RETURN_KEYS.issubset(result.keys()), f"Missing keys: {REQUIRED_RETURN_KEYS - result.keys()}"

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_returns_real_arns(self, mock_metadata, mock_boto_client):
        """ARNs in the response come from the API, not fabricated."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = _make_s3tables_client(bucket_exists=False, namespace_exists=False, table_exists=False)
        mock_boto_client.client.return_value = s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert result["table_bucket_arn"].startswith("arn:aws:s3tables:")
        assert result["table_arn"].startswith("arn:aws:s3tables:")

    @patch.object(_infra_lf, "boto3")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_error_returns_error_status(self, mock_metadata, mock_boto_client):
        """When all operations fail, returns error status."""
        mock_metadata.return_value = _mock_pipeline_metadata()
        s3tables = MagicMock()
        s3tables.list_table_buckets.side_effect = Exception("Service unavailable")
        s3tables.create_table_bucket.side_effect = Exception("Access denied")
        s3tables.exceptions.ConflictException = type("ConflictException", (Exception,), {})
        mock_boto_client.client.return_value = s3tables

        result = create_s3_table_bucket("test-pipeline", SAMPLE_SCHEMA)

        assert result["status"] == "error"
        assert "error_message" in result


class TestCreateS3TableInfrastructure:
    """Tests for create_s3_table_infrastructure wrapper function."""

    @patch.object(_infra_lf, "create_s3_table_bucket")
    def test_success_wraps_bucket_result(self, mock_create):
        """Successful creation wraps result in infrastructure_status format."""
        mock_create.return_value = {
            "bucket_name": "test-bucket",
            "table_name": "test_table",
            "table_bucket_arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket",
            "table_arn": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table",
            "namespace": "test_ns",
            "warehouse_location": "s3://test-bucket/warehouse",
            "metadata_location": "s3://test-bucket/metadata/test_table",
            "status": "created",
        }

        result = create_s3_table_infrastructure("test-pipeline", SAMPLE_SCHEMA)

        assert result["overall_status"] == "success"
        assert "s3_table_bucket" in result
        assert result["s3_table_bucket"]["bucket_name"] == "test-bucket"

    @patch.object(_infra_lf, "create_s3_table_bucket")
    def test_error_propagates(self, mock_create):
        """Error from create_s3_table_bucket propagates correctly."""
        mock_create.return_value = {"status": "error", "error_message": "Something went wrong"}

        result = create_s3_table_infrastructure("test-pipeline", SAMPLE_SCHEMA)

        assert result["overall_status"] == "error"
        assert "Something went wrong" in result["error_message"]

    @patch.object(_infra_lf, "create_s3_table_bucket")
    def test_no_agent_fields_in_response(self, mock_create):
        """Response does not contain agent-related metadata fields."""
        mock_create.return_value = {
            "bucket_name": "test-bucket",
            "table_name": "test_table",
            "table_bucket_arn": "arn:aws:s3tables:us-west-2:123456789012:bucket/test-bucket",
            "table_arn": "arn:aws:s3tables:us-west-2:123456789012:table/test-bucket/test_table",
            "namespace": "test_ns",
            "warehouse_location": "s3://test-bucket/warehouse",
            "metadata_location": "s3://test-bucket/metadata/test_table",
            "status": "created",
        }

        result = create_s3_table_infrastructure("test-pipeline", SAMPLE_SCHEMA)

        # Verify no agent-related fields exist
        flat_str = json.dumps(result)
        assert "agent_used" not in flat_str
        assert "agent_rationale" not in flat_str
        assert "agent_integration" not in flat_str


class TestLambdaHandlerFailsLoudly:
    """The handler must RAISE (not return) when S3 Tables infrastructure
    creation errors and no human-approval gate will review it.

    Previously the handler fell through to a statusCode=200 return with an
    empty s3_table_bucket, swallowing the failure so it surfaced two steps
    later as a misleading "Glue job name not found" at RunPipeline. Raising
    routes the Step Functions CreateInfrastructure task to its Catch with the
    real cause (e.g. the S3 Tables CreateTableBucket account-quota error).
    """

    @patch.object(_infra_lf, "create_infrastructure_approval_payload")
    @patch.object(_infra_lf, "determine_infrastructure_approval_requirement")
    @patch.object(_infra_lf, "create_s3_table_infrastructure")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_raises_when_infra_errors_and_no_approval(
        self, mock_metadata, mock_create_infra, mock_approval_req, mock_approval_payload
    ):
        """Errored infra + approval NOT required => handler raises with the real cause."""
        mock_metadata.return_value = {
            "pipeline_id": "test-pipeline",
            "target_config": {"type": "S3_TABLES", "bucket_name": "test-bucket"},
        }
        mock_create_infra.return_value = {
            "overall_status": "error",
            "error_message": "CreateTableBucket account quota exceeded",
        }
        mock_approval_req.return_value = False
        mock_approval_payload.return_value = {"approval_type": "infrastructure"}

        event = {
            "run_id": "run-123",
            "pipeline_id": "test-pipeline",
            "target_schema": SAMPLE_SCHEMA,
        }

        # Force the non-AgentCore path regardless of ambient env.
        # P28 R1.1: the handler wraps the failure in StepFailure so the SFN
        # task fails and Retry/Catch fire (RuntimeError preserved as cause).
        with patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}):
            with pytest.raises(_infra_lf.StepFailure) as exc_info:
                _infra_lf.lambda_handler(event, None)

        # The real cause must propagate, not be swallowed into a 200/500 body.
        assert "CreateTableBucket account quota exceeded" in str(exc_info.value)
        assert "test-pipeline" in str(exc_info.value)
        assert exc_info.value.step_name == "create_infrastructure"
        assert isinstance(exc_info.value.__cause__, RuntimeError)

    @patch.object(_infra_lf, "create_infrastructure_approval_payload")
    @patch.object(_infra_lf, "determine_infrastructure_approval_requirement")
    @patch.object(_infra_lf, "create_s3_table_infrastructure")
    @patch.object(_infra_lf, "get_pipeline_metadata")
    def test_does_not_raise_when_infra_errors_but_approval_required(
        self, mock_metadata, mock_create_infra, mock_approval_req, mock_approval_payload
    ):
        """Errored infra + approval REQUIRED => preserve the review path (no raise)."""
        mock_metadata.return_value = {
            "pipeline_id": "test-pipeline",
            "target_config": {"type": "S3_TABLES", "bucket_name": "test-bucket"},
        }
        mock_create_infra.return_value = {
            "overall_status": "error",
            "error_message": "CreateTableBucket account quota exceeded",
        }
        mock_approval_req.return_value = True
        mock_approval_payload.return_value = {"approval_type": "infrastructure"}

        event = {
            "run_id": "run-123",
            "pipeline_id": "test-pipeline",
            "target_schema": SAMPLE_SCHEMA,
        }

        with patch.dict("os.environ", {"USE_AGENTCORE_RUNTIME": "false"}):
            result = _infra_lf.lambda_handler(event, None)

        # Approval-review-on-error path is preserved: still returns 200.
        assert result["statusCode"] == 200
        assert result["approval_required"] is True


class TestInfrastructureApprovalGateIsAProducerlessSeam:
    """`determine_infrastructure_approval_requirement` returns False for every real record.

    Validates: Requirements 10.3

    p64 task 2a.3 took R10.3's second arm — record
    ``approval_config.infrastructure_approval_enabled`` as a producer-less seam,
    following ``LGR-5d68eeb-3``. The full record is the docstring at
    ``lambda_function.py:606``; read it before changing anything here.

    This class is the **behavioural** half of the pin: that the gate is closed for
    every ``approval_config`` shape a pipeline can actually have today. The
    **structural** half — that no writer gains the key and no Step Functions Choice
    consumes the answer — is
    ``tests/contracts/test_approval_config_casing_cross_language.py::TestTheInfrastructureGateHasNoProducer``,
    and it is the one that fails if a producer lands. Both are needed: this file
    cannot see the writers, and a source scan cannot show what the gate returns.

    Distinct from the two sibling tests above, which mock this function out
    entirely to exercise the handler's branch on its result. Nothing before this
    exercised the function itself, so nothing established that the branch taken in
    production is the ``False`` one.
    """

    #: The four keys `_default_approval_config` actually seeds (p64 task 2a.1).
    #: Spelled out so a reader can see the gate key is not among them.
    SEEDED_APPROVAL_CONFIG = {
        "schema_mapping_enabled": True,
        "schema_mapping_confidence_threshold": 0.85,
        "code_generation_enabled": True,
        "always_approve_schema_changes": True,
    }

    ERRORED_INFRA = {"overall_status": "error", "error_message": "CreateTableBucket account quota exceeded"}

    @pytest.mark.parametrize(
        ("approval_config", "shape"),
        [
            (None, "no approval_config attribute at all — a pre-p64 record"),
            ({}, "an explicitly empty map — arm 2 of _submitted_approval_config"),
            (SEEDED_APPROVAL_CONFIG, "the seeded defaults — what an omitted key writes"),
        ],
    )
    def test_the_gate_is_closed_for_every_shape_a_record_can_have(self, approval_config, shape):
        """Errored infra still returns False, so the `:937` fail-loud stays reachable.

        The error case is the discriminating one: on success the function returns
        False by a different route (the final ``return False``), so a success-only
        test would pass even if the gate key were being honoured.
        """
        metadata = {"pipeline_id": "test-pipeline"}
        if approval_config is not None:
            metadata["approval_config"] = approval_config

        with patch.object(_infra_lf, "get_pipeline_metadata", return_value=metadata):
            required = _infra_lf.determine_infrastructure_approval_requirement("test-pipeline", self.ERRORED_INFRA)

        assert required is False, f"the infrastructure approval gate opened for {shape}"

    def test_only_an_explicit_truthy_key_opens_it_and_nothing_writes_that(self):
        """The mechanism, pinned so the seam's consequence is visible rather than implied.

        This is the one input that opens the gate, and reaching it needs a
        hand-written request body — the residual the guard's docstring names, since
        ``_submitted_approval_config`` persists verbatim and
        ``_validate_approval_config`` does not filter unrecognised keys. Pinned
        rather than fixed: a boolean ``True`` is what does the damage, so a
        boolean check at the API boundary would not close it.

        Opening the gate is not a feature here — it makes ``lambda_handler`` skip
        the ``:937`` raise and return 200 for failed provisioning, with no Choice
        downstream to review it.
        """
        metadata = {"pipeline_id": "test-pipeline", "approval_config": {"infrastructure_approval_enabled": True}}

        with patch.object(_infra_lf, "get_pipeline_metadata", return_value=metadata):
            required = _infra_lf.determine_infrastructure_approval_requirement("test-pipeline", self.ERRORED_INFRA)

        assert required is True

    def test_a_metadata_read_failure_closes_the_gate(self):
        """The blanket `except Exception` returns False, keeping the fail-loud reachable.

        CHARACTERIZATION, and the reason
        ``test_get_pipeline_metadata_characterization.py::test_a_non_client_error_is_not_degraded_into_the_sentinel``
        exists: this wrapper would otherwise present an infrastructure error as
        "no approval required". Closed is the safe direction — it preserves the
        ``:937`` raise — but it is safe by luck rather than by design, so it is
        pinned here so a future change to that ``except`` is visible.
        """
        with patch.object(_infra_lf, "get_pipeline_metadata", side_effect=RuntimeError("connection reset")):
            required = _infra_lf.determine_infrastructure_approval_requirement("test-pipeline", self.ERRORED_INFRA)

        assert required is False
