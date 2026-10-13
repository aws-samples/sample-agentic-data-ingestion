# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Test_Pipeline_Provisioner — creates throwaway pipelines for e2e runs.

Creates a dedicated, test-owned pipeline via ``POST /api/pipelines`` (SigV4)
and tears it down (archive → delete) on completion. Every provisioned pipeline
is automatically registered with the ``e2e_ledger`` for fail-safe cleanup.

For a ``NEW_S3_TABLE_BUCKET`` target the **Glue mirror** ``create_infrastructure``
writes for the provisioned Iceberg table is registered too — see
``_register_glue_mirror``. Without it every such run left an undeletable
``glue_catalog`` row on the Schemas page.

Supported ``target_config.type`` values:
  - ``NEW_S3_TABLE_BUCKET`` — requires ``bucket_name``, ``namespace`` (or ``namespace_name``), ``table_name``
  - ``GLUE_CATALOG`` — requires ``database``, ``table``
  - ``RDS_DATABASE`` — requires ``jdbc_url``, ``jdbc_driver``, ``jdbc_table``
  - ``S3_PARQUET`` — uses ``s3_path``

Usage (via the ``provisioner`` fixture):

    def test_csv_run(provisioner, e2e_ledger):
        pipeline = provisioner.create(
            target_type="NEW_S3_TABLE_BUCKET",
            target_config={"type": "NEW_S3_TABLE_BUCKET"},
        )
        # ... trigger run against pipeline.pipeline_id ...
"""

from __future__ import annotations

import logging
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# ``shared/`` is not an installed package, so it goes on the path the way every
# other test module that needs it does (see
# ``tests/unit/shared/test_glue_mirror_identifiers.py:36``).
_SHARED_DIR = Path(__file__).resolve().parents[3] / "shared"
if str(_SHARED_DIR) not in sys.path:
    sys.path.insert(0, str(_SHARED_DIR))

# The ONE definition of an S3 Tables table's Glue mirror ``(database, table)``
# pair. Imported, never re-derived: ``LGR-acca05d-1`` (**template / ratified**)
# states its ``revisit_when`` as *precisely* "``create_infrastructure`` changes
# either derivation", and a second expression of the pair here is how a teardown
# and the writer it exists to undo drift apart without either being wrong.
from glue_mirror_identifiers import glue_mirror_identifiers

logger = logging.getLogger(__name__)

# Sensible defaults for pipeline creation
DEFAULT_AI_MODEL = "us.anthropic.claude-sonnet-4-20250514"
# The API expects target_schema to be a string identifier (not a dict).
# The backend resolves the schema from S3 using this ID; if not found it
# logs a warning but continues — safe for e2e where the schema is extracted
# from the uploaded source file at run time.
DEFAULT_TARGET_SCHEMA = "e2e_test_schema"


@dataclass
class ProvisionedPipeline:
    """A test-owned pipeline created by the provisioner."""

    pipeline_id: str
    target_type: str  # target_config.type enum value
    target_config: dict


class TestPipelineProvisioner:
    """Creates and manages throwaway e2e test pipelines.

    Args:
        rest_client: SigV4-signed REST client for the dashboard API.
        ledger: ResourceLedger for auto-registering teardown.
        boto3_session: Optional boto3 session for S3 schema upload.
    """

    def __init__(self, rest_client: Any, ledger: Any, boto3_session: Any = None) -> None:
        self._rest_client = rest_client
        self._ledger = ledger
        self._session = boto3_session

    def create(
        self,
        *,
        target_type: str,
        target_config: dict,
        name_prefix: str = "e2e-test",
        ai_model: str = DEFAULT_AI_MODEL,
        target_schema: str | None = None,
    ) -> ProvisionedPipeline:
        """Provision a new throwaway pipeline via POST /api/pipelines.

        Args:
            target_type: The ``target_config.type`` enum value
                (e.g. ``NEW_S3_TABLE_BUCKET``, ``GLUE_CATALOG``,
                ``RDS_DATABASE``, ``S3_PARQUET``).
            target_config: Full target configuration dict including ``type``.
            name_prefix: Prefix for the pipeline name (default ``"e2e-test"``).
            ai_model: Bedrock model ID for AI steps.
            target_schema: Schema ID string (e.g. ``"e2e_test_schema"``);
                defaults to :data:`DEFAULT_TARGET_SCHEMA`.

        Returns:
            A ``ProvisionedPipeline`` with the created ``pipeline_id``.

        Raises:
            RuntimeError: If the API returns a non-2xx status.
        """
        if target_schema is None:
            target_schema = DEFAULT_TARGET_SCHEMA

        # Upload target schema to S3 so the schema-mapping Lambda can find it
        if self._session:
            self._upload_target_schema(target_schema)

        # Ensure target_config includes the type field
        if "type" not in target_config:
            target_config = {**target_config, "type": target_type}

        # Auto-generate required fields for NEW_S3_TABLE_BUCKET if not provided
        if target_type == "NEW_S3_TABLE_BUCKET":
            if "bucket_name" not in target_config:
                target_config["bucket_name"] = f"e2e-s3tables-{uuid.uuid4().hex[:8]}"
            if "namespace_name" not in target_config and "namespace" not in target_config:
                target_config["namespace_name"] = "e2e_namespace"
            if "table_name" not in target_config:
                target_config["table_name"] = f"e2e_target_{uuid.uuid4().hex[:8]}"

        unique_suffix = uuid.uuid4().hex[:8]
        pipeline_name = f"{name_prefix}-{target_type.lower().replace('_', '-')}-{unique_suffix}"

        payload: dict[str, Any] = {
            "pipeline_name": pipeline_name,
            "target_schema": target_schema,
            "ai_model": ai_model,
            "target_config": target_config,
            "created_by": "e2e-harness",
        }

        logger.info(
            "Provisioning pipeline: name=%s, target_type=%s",
            pipeline_name,
            target_type,
        )

        resp = self._rest_client.post("/api/pipelines", payload)

        if resp.status_code not in (200, 201):
            raise RuntimeError(f"Failed to create pipeline (HTTP {resp.status_code}): {resp.body}")

        # Extract pipeline_id from the response
        body = resp.body if isinstance(resp.body, dict) else {}
        pipeline_id = body.get("pipeline_id") or body.get("id", "")

        if not pipeline_id:
            raise RuntimeError(f"Pipeline creation response did not include pipeline_id: {resp.body}")

        logger.info("Provisioned pipeline: %s (id=%s)", pipeline_name, pipeline_id)

        # Register with ledger for automatic cleanup
        self._ledger.add_pipeline(pipeline_id)

        # ...and the Glue mirror the S3 Tables provisioning will write, which
        # nothing used to clean up. Registered here rather than after the run
        # because the ledger is a *fail-safe*: a run that dies mid-flight must
        # still tear the mirror down, and by then this is the only place that
        # knows the three inputs the pair is derived from.
        if target_type == "NEW_S3_TABLE_BUCKET":
            self._register_glue_mirror(pipeline_id, target_config)

        return ProvisionedPipeline(
            pipeline_id=pipeline_id,
            target_type=target_type,
            target_config=target_config,
        )

    def _register_glue_mirror(self, pipeline_id: str, target_config: dict) -> None:
        """Register the S3 Tables table's Glue mirror for teardown.

        ``create_infrastructure`` registers every S3 Tables Iceberg table in the
        Glue Data Catalog so the Data Quality console has something to describe
        (``LGR-7c18e81-1``, backfilled). Nothing in this harness deleted that
        mirror, so every ``NEW_S3_TABLE_BUCKET`` e2e run left a permanent
        ``glue_catalog`` row on the Schemas page: once the table bucket is gone
        the mirror is orphaned, ``Schema_Listing``'s exclusion no longer has a
        live ``s3_table_bucket`` row to suppress it with, and the dashboard's
        ``DELETE /api/schemas/{id}`` refuses a catalog-derived id by design.

        **The three inputs are read exactly as ``create_infrastructure`` reads
        them**, at ``lambda_function.py:280-286``, because a teardown that
        addresses a differently-derived pair deletes nothing and reports success:

        * ``bucket_name`` — ``target_config["bucket_name"]``.
        * ``namespace_name`` — ``target_config["namespace_name"]``, defaulting
          to ``pipeline_id.lower().replace("-", "_")``. Note ``namespace`` is
          **not** consulted: ``create_infrastructure`` does not read that key, so
          a ``target_config`` carrying only ``namespace`` gets the pipeline-id
          default there and must get it here too.
        * ``table_name`` — ``target_config["table_name"]``.

        The ``namespace.replace("-", "_")`` at ``:286`` is applied here and not
        inside ``glue_mirror_identifiers``: that module's docstring records that
        a second normalisation there would make the helper disagree with the
        writer it exists to describe, so normalisation belongs at the caller.

        A missing ``bucket_name`` or ``table_name`` registers nothing and warns.
        The pair cannot be derived without them, and guessing one would address
        some other pipeline's mirror — which is the one failure mode a cleanup
        tool must not have.
        """
        bucket_name = target_config.get("bucket_name", "")
        table_name = target_config.get("table_name", "")
        if not bucket_name or not table_name:
            logger.warning(
                "Not registering a Glue mirror for %s: target_config carries "
                "bucket_name=%r table_name=%r, and the mirror pair cannot be derived "
                "without both",
                pipeline_id,
                bucket_name,
                table_name,
            )
            return

        namespace_name = target_config.get("namespace_name", pipeline_id.lower().replace("-", "_"))
        namespace = namespace_name.replace("-", "_")

        database, table = glue_mirror_identifiers(bucket_name, namespace, table_name)

        # Database first so LIFO deletes the table first.
        self._ledger.add_glue_database(database)
        self._ledger.add_glue_table(database, table)
        logger.info("Registered Glue mirror for teardown: %s.%s", database, table)

    def _upload_target_schema(self, schema_id: str) -> None:
        """Upload the target schema JSON to S3 for the schema-mapping Lambda.

        Places the schema at the ``shared/{schema_id}.json`` path in the
        ``adi-target-schemas-{account}-{region}`` bucket — the fallback
        location used by the schema-mapping Lambda's
        ``get_uploaded_json_schema()`` function.
        """
        import json

        sts = self._session.client("sts")
        account_id = sts.get_caller_identity()["Account"]
        region = self._session.region_name
        bucket = f"adi-target-schemas-{account_id}-{region}"

        schema_json = {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "name": {"type": "string"},
                "value": {"type": "string"},
            },
        }

        key = f"shared/{schema_id}.json"
        s3 = self._session.client("s3")
        s3.put_object(
            Bucket=bucket,
            Key=key,
            Body=json.dumps(schema_json).encode("utf-8"),
            ContentType="application/json",
        )
        logger.info("Uploaded target schema to s3://%s/%s", bucket, key)
        # Register for cleanup
        self._ledger.add_s3_object(bucket, key)

    def teardown(self, pipeline_id: str) -> None:
        """Explicitly tear down a pipeline (archive → delete).

        Normally cleanup is handled by the ledger, but this method is
        available for cases where explicit teardown timing is needed.
        """
        logger.info("Tearing down pipeline: %s", pipeline_id)
        # Archive first — delete requires status=="archived"
        archive_resp = self._rest_client.put(f"/api/pipelines/{pipeline_id}/archive")
        if archive_resp.status_code not in (200, 204):
            logger.warning(
                "Archive returned %d for %s: %s",
                archive_resp.status_code,
                pipeline_id,
                archive_resp.body,
            )
        # Then delete
        delete_resp = self._rest_client.delete(f"/api/pipelines/{pipeline_id}")
        if delete_resp.status_code not in (200, 204):
            logger.warning(
                "Delete returned %d for %s: %s",
                delete_resp.status_code,
                pipeline_id,
                delete_resp.body,
            )
