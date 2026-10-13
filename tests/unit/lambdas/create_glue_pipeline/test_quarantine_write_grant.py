# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p64 2c.1 — the per-pipeline Glue role can write the DQ quarantine, on every target.

WHY THIS MODULE EXISTS. p64 2b.2 put the ``EvaluateDataQuality`` block into all
five PySpark templates. When that block splits a frame it writes the failing rows
to ``s3://adi-generated-code-*/quarantine/<run_id>/`` — and the per-pipeline Glue
execution role built at run time by ``iam_roles.create_pipeline_specific_role``
granted ``s3:PutObject`` on that bucket for the S3_TABLES / GLUE_CATALOG branch
only. The ``JDBC`` and ``S3_PARQUET`` branches granted ``s3:GetObject``, so an
opted-in pipeline on either would have failed at the quarantine write with
AccessDenied. Fail-closed rather than row-losing, since the write sits outside the
non-blocking try (R6.4) — safe, and wrong.

2b.4 made it UNREACHABLE rather than fixing it, by gating the block on
``quality_config.data_quality_enabled``, which defaults to OFF for both target
types. It withheld the grant on purpose: widening a runtime role to serve a code
path that cannot execute is against the posture ``LGR-9e76249-3`` and
``LGR-b46a5bb-2`` both record. 2c.1 made the opt-in **settable**, so the path
became reachable, and the grant lands in the same change — which is what the three
templates' own headers asked for by name.

EXPECTATIONS HERE ARE DERIVED FROM R7, NOT FROM THE CURRENT POLICY. R7.1 requires
that a pipeline be *able* to carry the opt-in and have it work; a settable opt-in
whose quarantine write is denied is not a working opt-in. So the assertions state
what the role must permit for the feature to function, and the two Put assertions
below **fail against unfixed code** — before 2c.1 neither branch granted
``s3:PutObject`` on ``adi-generated-code-*/*`` at all.

THE THIRD ASSERTION IS THE ONE THAT WILL EARN ITS KEEP. The grant is a **separate**
policy statement rather than ``s3:PutObject`` appended to the existing ``GetObject``
statement, because that statement's ``Resource`` list also names
``adi-source-files-*`` and ``adi-processed-files-*``. Folding the action in would
hand the job write access to its own INPUT bucket — the one the S3-upload
EventBridge rule watches — so a job could re-trigger itself on its own output.
``test_the_quarantine_grant_does_not_reach_the_source_bucket`` is what catches that
"tidy-up", which is otherwise a one-line diff that reads like a simplification.
``LGR-53765e8-1`` prefers a single statement when the action set is identical
across resources; here it is not, and the resource is a strict subset, so one
statement per (action set, resource set) is the least-privilege expression rather
than a stylistic choice.

Validates: Requirements 7.1
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
sys.path.insert(0, LAMBDA_DIR)

os.environ["AGENTIC_DATA_INGESTION_ENV"] = "local"
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")

ACCOUNT = "123456789012"
REGION = "us-west-2"

#: The quarantine prefix's bucket pattern, as the templates' render context spells
#: it. ``code_assembly`` passes the generated-code bucket as ``quarantine_bucket``
#: (P28 R11.1) because an S3 Tables target bucket is a table bucket that cannot
#: accept loose parquet objects.
QUARANTINE_OBJECTS = "arn:aws:s3:::adi-generated-code-*/*"

#: The pipeline's own INPUT bucket. The S3-upload EventBridge rule watches it, so a
#: job able to write here could re-trigger itself.
SOURCE_OBJECTS = f"arn:aws:s3:::adi-source-files-{ACCOUNT}-{REGION}/*"

JDBC_TARGET = {
    "type": "JDBC",
    "jdbc_url": "jdbc:postgresql://host:5432/warehouse",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_table": "public.customers",
    "jdbc_connection_name": "agentic-data-ingestion-pid-jdbc",
    "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:db-Abc",
}

# An output bucket that is NOT one of the system buckets, so the S3_PARQUET
# branch's own target-bucket write grant cannot be mistaken for the quarantine
# grant this module is about.
S3_PARQUET_TARGET = {"type": "S3_PARQUET", "s3_path": "s3://customer-analytics-lake/curated/"}

S3_TABLES_TARGET = {"type": "S3_TABLES", "bucket_name": "agentic-data-ingestion-table-pid"}


def _load(mod_name: str):
    """Load a sibling module of the create_glue_pipeline Lambda standalone.

    Same helper as ``test_jdbc_source_job_wiring.py``, which owns the other
    ``create_pipeline_specific_role`` assertions; kept as a local copy for the
    reason that module's own helper is local — these are the only callers and a
    shared one would need a home that imports cleanly under both.
    """
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(f"cgp_{mod_name}", mod_path, submodule_search_locations=[LAMBDA_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"cgp_{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _statements(monkeypatch, pipeline_id: str, target_config: dict) -> list[dict]:
    """The inline policy statements of the role built for ``target_config``."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    iam_roles = _load("iam_roles")
    iam_roles.iam_client.attach_role_policy = mock.MagicMock()
    monkeypatch.setattr("time.sleep", lambda *_: None)

    iam_roles.create_pipeline_specific_role(
        pipeline_id,
        target_config,
        s3_table_bucket={"bucket_name": target_config["bucket_name"]} if "bucket_name" in target_config else None,
    )

    iam_client = boto3.client("iam", region_name=REGION)
    doc = iam_client.get_role_policy(
        RoleName=f"AgenticDataIngestion-{pipeline_id}-ExecutionRole",
        PolicyName="PipelineSpecificAccess",
    )
    statements: list[dict] = doc["PolicyDocument"]["Statement"]
    return statements


def _resources_for(statements: list[dict], action: str) -> set[str]:
    """Every resource ARN granted ``action`` anywhere in the policy.

    Actions and resources are both spelled as either a bare string or a list in
    IAM, and this policy uses both forms, so normalise rather than assume.
    """
    granted: set[str] = set()
    for statement in statements:
        actions = statement.get("Action", [])
        actions = [actions] if isinstance(actions, str) else actions
        if action not in actions:
            continue
        resources = statement.get("Resource", [])
        resources = [resources] if isinstance(resources, str) else resources
        granted.update(resources)
    return granted


@pytest.mark.parametrize(
    "pipeline_id,target_config",
    [
        ("pid-jdbc", JDBC_TARGET),
        ("pid-parquet", S3_PARQUET_TARGET),
    ],
    ids=["jdbc-target", "s3-parquet-target"],
)
@mock_aws
def test_the_quarantine_write_is_permitted(monkeypatch, pipeline_id, target_config):
    """R7.1: the two target types that could not quarantine, now can.

    **Fails against unfixed code** — before 2c.1 neither branch granted
    ``s3:PutObject`` on the generated-code bucket, so an opted-in pipeline
    generated cleanly and then refused at the quarantine write.
    """
    statements = _statements(monkeypatch, pipeline_id, target_config)
    put_resources = _resources_for(statements, "s3:PutObject")

    assert QUARANTINE_OBJECTS in put_resources, (
        f"{target_config['type']}: the per-pipeline Glue role cannot write "
        f"{QUARANTINE_OBJECTS}, so an opted-in DQ block fails at the quarantine "
        f"write with AccessDenied. s3:PutObject is granted on: {sorted(put_resources)}"
    )


@pytest.mark.parametrize(
    "pipeline_id,target_config",
    [
        ("pid-jdbc-narrow", JDBC_TARGET),
        ("pid-parquet-narrow", S3_PARQUET_TARGET),
    ],
    ids=["jdbc-target", "s3-parquet-target"],
)
@mock_aws
def test_the_quarantine_grant_does_not_reach_the_source_bucket(monkeypatch, pipeline_id, target_config):
    """The grant is a separate statement, asserted by its consequence.

    The cheap-looking way to add the quarantine write is to append
    ``s3:PutObject`` to the existing ``s3:GetObject`` statement, whose ``Resource``
    list already names ``adi-generated-code-*/*``. That statement also names
    ``adi-source-files-*`` — the bucket the S3-upload EventBridge rule watches — so
    the one-line version hands the job write access to its own input and makes a
    self-re-triggering pipeline possible. ``LGR-4ea43f3-7`` is the record that
    identifies that loop as the thing a fail-closed refusal exists to prevent.

    This asserts the outcome rather than the statement count, so it stays true if
    the branch is restructured for an unrelated reason and false the moment the two
    statements are merged.
    """
    statements = _statements(monkeypatch, pipeline_id, target_config)
    put_resources = _resources_for(statements, "s3:PutObject")

    assert SOURCE_OBJECTS not in put_resources, (
        f"{target_config['type']}: s3:PutObject is granted on the pipeline's own "
        f"SOURCE bucket ({SOURCE_OBJECTS}). That is what happens when the quarantine "
        f"grant is folded into the GetObject statement instead of being its own — the "
        f"job can then write into the bucket the S3-upload EventBridge rule watches "
        f"and re-trigger itself. Keep the two statements separate.\n"
        f"s3:PutObject is granted on: {sorted(put_resources)}"
    )


@mock_aws
def test_the_s3_tables_branch_still_grants_it(monkeypatch):
    """The branch that always could, still can — the regression baseline.

    S3_TABLES and GLUE_CATALOG have granted ``s3:PutObject`` on the generated-code
    bucket since P28, which is why the gap on the other two was invisible while the
    DQ block lived in ``glue_job_s3_tables.py.j2`` alone. Asserted so that closing
    the gap on two branches cannot quietly move it on a third.
    """
    statements = _statements(monkeypatch, "pid-tables", S3_TABLES_TARGET)

    assert QUARANTINE_OBJECTS in _resources_for(statements, "s3:PutObject")
