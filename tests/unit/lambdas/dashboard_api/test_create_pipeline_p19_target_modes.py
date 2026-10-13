# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit + property tests for P19 ``create_pipeline`` reconciliations.

Covers the two changes the P19 spec made to ``handlers.pipelines.create_pipeline``:

    * ``write_mode`` is target-type-aware (Decision 2): JDBC targets default
      to ``'overwrite'`` (Req 7.2), all other target types preserve the
      pre-P19 ``'append'`` default. The handler is the persistence boundary
      (Decision 2).
    * ``schema_source`` is persisted alongside ``schema_fields`` for
      diagnostic display on the Pipeline Overview page (Req 8.4). Validated
      against the documented set ``{'detected', 'upload', 'existing',
      'mirror_source'}``.

Test layout:
    * Property tests (P9, P10, P13) for the three universal contracts.
    * Example tests for default-``write_mode`` branching (Req 7.2) and the
      new ``schema_source`` field default (Req 8.4).

Validates: P19 Requirements 7.1, 7.2, 7.3, 8.4, 9.5
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler. Mirrors
# the pattern in test_create_pipeline_jdbc_source.py so the dashboard API
# handler imports cleanly in a unit-test process without the real Lambda
# layer.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)

# P21 Task 7.2 — create_pipeline + provisioning helpers now live in
# handlers.pipelines_crud. Bind the alias + patch targets there.
try:
    from handlers import pipelines_crud as pipelines
    from handlers.pipelines import create_pipeline
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# ---------------------------------------------------------------------------
# Test data builders
# ---------------------------------------------------------------------------

# Valid JDBC source config so source_type=JDBC requests pass validation.
_VALID_JDBC_SOURCE_CONFIG: dict = {
    "jdbc_url": "jdbc:postgresql://db.example.com:5432/orders",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_schema": "public",
    "jdbc_table": "orders",
    "secret_arn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:db-creds-AbCdEf",
    "engine": "postgres",
    "read_mode": "full_table",
}

# Minimal JDBC target config that the handler accepts. The wizard always
# resolves write_mode before submitting, but tests need to omit it
# explicitly to verify the handler's default-branching logic.
#
# p31 task 4.2: `jdbc_url` / `jdbc_driver` / `jdbc_table` are now required for
# a `JDBC` target in every connection mode (R9.2 — restoring the checks the
# `RDS_DATABASE` branch always performed, which the type consolidation to
# `JDBC` had made unreachable). They are irrelevant to the write_mode and
# schema_source behaviour under test here; they are present only so the
# payload is a complete JDBC target rather than one the API now rejects.
_JDBC_TARGET_BASE: dict = {
    "type": "JDBC",
    "database_name": "analytics",
    "table_name": "users_mirror",
    "glue_connection_name": "my-conn",
    "jdbc_url": "jdbc:postgresql://target-db.example.com:5432/analytics",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_table": "analytics.users_mirror",
}

# A complete Glue Catalog target config. `create_pipeline` now refuses every
# GLUE_CATALOG target for a new pipeline, so this is used only to prove that
# refusal; the non-JDBC defaults below are exercised through S3 Tables instead.
_GLUE_CATALOG_TARGET_BASE: dict = {
    "type": "GLUE_CATALOG",
    "database": "analytics",
    "table": "users_mirror",
}

_S3_TABLES_TARGET_BASE: dict = {
    "type": "EXISTING_S3_TABLE_BUCKET",
    "bucket_name": "my-bucket",
    "table_name": "users_mirror",
}


def _make_body(
    *,
    target_config: dict,
    schema_source: str | None = None,
    source_type: str = "S3_UPLOAD",
    source_config: dict | None = None,
    pipeline_name: str = "P19 Test Pipeline",
) -> str:
    """Build a ``create_pipeline`` request body."""
    body: dict = {
        "pipeline_name": pipeline_name,
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "target_config": target_config,
        "source_type": source_type,
    }
    if source_config is not None:
        body["source_config"] = source_config
    if schema_source is not None:
        body["schema_source"] = schema_source
    return json.dumps(body)


# Module-level mocks shared by every handler invocation.
_mock_pipelines_table = MagicMock()
_mock_state_table = MagicMock()
_mock_executions_table = MagicMock()
_mock_approvals_table = MagicMock()
_mock_s3_client = MagicMock()
_mock_dynamodb = MagicMock()


@pytest.fixture(autouse=True)
def _reset_module_mocks(monkeypatch):
    """Reset shared mocks and bind them onto the handler module."""
    _mock_pipelines_table.reset_mock()
    _mock_state_table.reset_mock()
    _mock_executions_table.reset_mock()
    _mock_approvals_table.reset_mock()
    _mock_s3_client.reset_mock()
    _mock_dynamodb.reset_mock()

    pipelines.pipelines_table = _mock_pipelines_table
    pipelines.state_table = _mock_state_table
    pipelines.executions_table = _mock_executions_table
    pipelines.approvals_table = _mock_approvals_table
    pipelines.dynamodb = _mock_dynamodb
    pipelines.s3_client = _mock_s3_client

    # The handler creates fresh boto3 clients for the S3 schema lookup +
    # STS GetCallerIdentity at line ~1418. Stub boto3.client so the
    # network calls never happen — this keeps property tests under the
    # Hypothesis deadline budget. The exception is caught and the
    # handler falls through to using the bare schema ID.
    fake_client = MagicMock()
    fake_client.get_object.side_effect = Exception("stubbed S3")
    fake_client.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(pipelines.boto3, "client", lambda *a, **kw: fake_client)

    yield


def _captured_put_item() -> dict:
    """Return the ``Item`` kwarg passed to ``pipelines_table.put_item``."""
    assert _mock_pipelines_table.put_item.called, "Expected create_pipeline to write a row to pipelines_table"
    return _mock_pipelines_table.put_item.call_args[1]["Item"]


# ---------------------------------------------------------------------------
# Example tests — Req 7.2: default write_mode branches by target type.
# ---------------------------------------------------------------------------


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestDefaultWriteModeBranching:
    """Default-``write_mode`` branching by target type (P19 Decision 2).

    Validates: P19 Requirements 7.2, 9.5.
    """

    def test_jdbc_target_omitted_write_mode_defaults_to_overwrite(self, _mock_provision):
        """Req 7.2: JDBC target with write_mode omitted → 'overwrite'."""
        target_config = dict(_JDBC_TARGET_BASE)
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 201, f"Expected 201, got {result['statusCode']}: {result['body']}"

        item = _captured_put_item()
        # The handler persists target_config back onto the item with the
        # resolved write_mode value.
        persisted_target = item.get("target_config") or {}
        assert persisted_target["write_mode"] == "overwrite"

    @pytest.mark.parametrize("type_value", ["GLUE_CATALOG", "glue_catalog"])
    def test_glue_catalog_target_is_refused_for_a_new_pipeline(self, _mock_provision, type_value: str):
        """A complete GLUE_CATALOG target is refused with a 400 and writes nothing.

        The type is matched case-insensitively, as every other target type is,
        so the lower-case spelling the wizard state uses is refused too.
        """
        target_config = dict(_GLUE_CATALOG_TARGET_BASE)
        target_config["type"] = type_value
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 400, f"Expected 400, got {result['statusCode']}: {result['body']}"
        err = json.loads(result["body"]).get("error", "")
        assert "GLUE_CATALOG is no longer offered" in err
        assert "S3_PARQUET" in err
        _mock_pipelines_table.put_item.assert_not_called()
        _mock_provision.assert_not_called()

    def test_s3_tables_target_omitted_write_mode_defaults_to_append(self, _mock_provision):
        """Req 9.5: S3 Tables target preserves pre-P19 'append' default."""
        target_config = dict(_S3_TABLES_TARGET_BASE)
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 201
        item = _captured_put_item()
        persisted_target = item.get("target_config") or {}
        assert persisted_target["write_mode"] == "append"

    def test_jdbc_explicit_write_mode_is_persisted_verbatim(self, _mock_provision):
        """Req 7.1: explicit write_mode is persisted as sent (no defaulting)."""
        target_config = dict(_JDBC_TARGET_BASE)
        target_config["write_mode"] = "append"
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 201
        item = _captured_put_item()
        persisted_target = item.get("target_config") or {}
        assert persisted_target["write_mode"] == "append"


# ---------------------------------------------------------------------------
# Example test — Req 8.4: schema_source default and persistence.
# ---------------------------------------------------------------------------


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestSchemaSourcePersistence:
    """schema_source field default and persistence (P19 Req 8.4)."""

    def test_omitted_schema_source_defaults_to_detected(self, _mock_provision):
        """Req 8.4: schema_source omitted from request → 'detected'."""
        target_config = dict(_S3_TABLES_TARGET_BASE)
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 201
        item = _captured_put_item()
        assert item["schema_source"] == "detected"

    @pytest.mark.parametrize("value", ["detected", "upload", "existing", "mirror_source"])
    def test_documented_schema_sources_accepted(self, _mock_provision, value: str):
        """Each documented value round-trips into the persisted record."""
        target_config = dict(_S3_TABLES_TARGET_BASE)
        body = _make_body(target_config=target_config, schema_source=value)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 201, (
            f"Expected 201 for schema_source={value!r}, got {result['statusCode']}: {result['body']}"
        )
        item = _captured_put_item()
        assert item["schema_source"] == value


# ---------------------------------------------------------------------------
# Property tests
# ---------------------------------------------------------------------------


# Feature: p19-jdbc-target-modes, Property 9: create_pipeline handler persists write_mode verbatim
class TestProperty9WriteModeVerbatim:
    """Property 9: ``create_pipeline`` handler persists ``write_mode`` verbatim.

    *For any* string value ``w ∈ {'overwrite', 'append'}`` sent in
    ``target_config.write_mode`` of a JDBC pipeline-creation body, the
    persisted DDB item's ``target_config.write_mode`` field equals ``w``
    exactly (no transformation, normalization, or defaulting).

    ``merge`` left this set on 2026-10-10: the JDBC writer implements only
    overwrite and append, so a stored ``merge`` ran as append and reported
    success. It is now refused with a 400 (Property 10, and
    ``shared/write_modes.py``).

    Validates: P19 Requirements 7.1
    """

    @given(w=st.sampled_from(["overwrite", "append"]))
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_write_mode_persisted_verbatim_for_jdbc_target(self, _mock_provision, w: str):
        """Each valid write_mode value round-trips byte-for-byte."""
        _mock_pipelines_table.reset_mock()
        target_config = dict(_JDBC_TARGET_BASE)
        target_config["write_mode"] = w
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})
        assert result["statusCode"] == 201, f"Expected 201 for write_mode={w!r}, got {result['statusCode']}"
        item = _captured_put_item()
        persisted_target = item.get("target_config") or {}
        assert persisted_target["write_mode"] == w, (
            f"write_mode={w!r} was not persisted verbatim; got {persisted_target.get('write_mode')!r}"
        )


# Feature: p19-jdbc-target-modes, Property 10: Handler enforces the write_mode allow-list
class TestProperty10WriteModeAllowList:
    """Property 10: handler enforces the ``write_mode`` allow-list.

    *For any* string value ``w``, ``POST /api/pipelines/provision`` with a JDBC
    ``target_config.write_mode = w`` accepts the request (returns 200/201)
    if and only if ``w ∈ {'overwrite', 'append'}``. Any other
    value (including ``merge``, empty string, mixed-case variants, arbitrary
    strings) results in HTTP 400 with an error message naming the value.

    ``merge`` is sampled explicitly rather than left to ``st.text``, which
    would almost never produce it: it is the one value whose acceptance
    changed on 2026-10-10 (``shared/write_modes.py``).

    Validates: P19 Requirements 7.3
    """

    @given(w=st.one_of(st.just("merge"), st.text(min_size=0, max_size=32)))
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_write_mode_allow_list_biconditional(self, _mock_provision, w: str):
        """Accept iff w is in {'overwrite', 'append'} for a JDBC target."""
        _mock_pipelines_table.reset_mock()
        target_config = dict(_JDBC_TARGET_BASE)
        target_config["write_mode"] = w
        # A merge_key is supplied so a `merge` refusal is about the target, not
        # about the missing key.
        if w == "merge":
            target_config["merge_key"] = "id"
        body = _make_body(target_config=target_config)

        result = create_pipeline(body, event={})

        if w in ("overwrite", "append"):
            assert result["statusCode"] == 201, (
                f"Allowed write_mode={w!r} must be accepted; got {result['statusCode']}: {result['body']}"
            )
        else:
            assert result["statusCode"] == 400, (
                f"Disallowed write_mode={w!r} must be 400; got {result['statusCode']}: {result['body']}"
            )
            err = json.loads(result["body"]).get("error", "")
            # Error message names the invalid value verbatim.
            assert repr(w) in err or w in err, f"Error message must reference the invalid value {w!r}; got {err!r}"
            # No DDB write for invalid input.
            _mock_pipelines_table.put_item.assert_not_called()


# Feature: p19-jdbc-target-modes, Property 13: Handler accepts schema_source iff in the documented set
class TestProperty13SchemaSourceAllowList:
    """Property 13: handler accepts ``schema_source`` iff in the documented set.

    *For any* string value ``s``, ``POST /api/pipelines/provision`` with
    ``schema_source = s`` accepts the request (returns 200/201 and
    persists ``s`` to the DDB item's ``schema_source`` field) if and only
    if ``s ∈ {'detected', 'upload', 'existing', 'mirror_source'}``. Any
    other value results in HTTP 400.

    Validates: P19 Requirements 8.4
    """

    @given(s=st.text(min_size=0, max_size=32))
    @settings(
        max_examples=100,
        deadline=None,
        suppress_health_check=[HealthCheck.function_scoped_fixture],
    )
    @patch("handlers.pipelines_crud._provision_pipeline_resources")
    def test_schema_source_allow_list_biconditional(self, _mock_provision, s: str):
        """Accept + persist iff s is in the documented set."""
        _mock_pipelines_table.reset_mock()
        target_config = dict(_S3_TABLES_TARGET_BASE)
        body = _make_body(target_config=target_config, schema_source=s)

        result = create_pipeline(body, event={})

        if s in ("detected", "upload", "existing", "mirror_source"):
            assert result["statusCode"] == 201, (
                f"Allowed schema_source={s!r} must be accepted; got {result['statusCode']}: {result['body']}"
            )
            item = _captured_put_item()
            assert item["schema_source"] == s, (
                f"schema_source={s!r} was not persisted; got {item.get('schema_source')!r}"
            )
        else:
            assert result["statusCode"] == 400, (
                f"Disallowed schema_source={s!r} must be 400; got {result['statusCode']}: {result['body']}"
            )
            _mock_pipelines_table.put_item.assert_not_called()
