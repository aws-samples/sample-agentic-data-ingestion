# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Threat model T-02 / T-03: the per-pipeline Glue role is least-privilege and bounded.

What these assert, and where each expectation comes from:

* **No managed policy is attached** (T-02). The role used to carry
  ``service-role/AWSGlueServiceRole``, whose first statement grants ``glue:*``
  and ``s3:ListAllMyBuckets`` on ``*``. The threat model names exactly that as
  the blast radius of AI-generated code, so its absence is the requirement.
* **What the job needs from that policy is still granted, scoped.** Taken from
  what the rendered job calls, not from the current policy:
  ``--enable-metrics`` and the DQ partial publish CloudWatch metrics (namespaces
  ``Glue``, ``Glue Data Quality`` per the Glue DQ minimum-permissions policy,
  and the partial's own ``AgenticDataIngestion/DataQuality``);
  ``EvaluateDataQuality`` with results publishing needs ``glue:PublishDataQuality``;
  a GLUE_CATALOG target may live in an operator-chosen database outside the
  project prefix, which only ``glue:*`` used to reach.
* **No shared fallback role** (T-02). A failure to build the role used to
  return ``AgenticDataIngestionExecutionRole`` with ``AmazonS3FullAccess``
  attached; it must now raise.
* **The permissions boundary is set when configured** (T-03). Foundation
  conditions ``iam:CreateRole`` on it, so the role must be created with it.
* **The inline policy fits IAM's 10,240-character limit** on the largest branch
  combination, because a ``put_role_policy`` that fails on size now fails the
  step rather than falling back.
"""

import importlib.util
import json
import os
import sys

import boto3
import pytest
from moto import mock_aws

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_glue_pipeline"
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

#: IAM's aggregate inline-policy limit for a role, whitespace excluded.
IAM_ROLE_INLINE_POLICY_LIMIT = 10_240

TARGETS = {
    "s3-tables": ({"type": "S3_TABLES", "bucket_name": "agentic-data-ingestion-table-pid"}, None),
    "glue-catalog": ({"type": "GLUE_CATALOG", "database": "sales_db", "table": "orders"}, None),
    "jdbc": (
        {
            "type": "JDBC",
            "jdbc_url": "jdbc:postgresql://host:5432/warehouse",
            "jdbc_table": "public.customers",
            "jdbc_connection_name": "agentic-data-ingestion-pid-jdbc",
            "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:db-Abc",
        },
        None,
    ),
    "s3-parquet": ({"type": "S3_PARQUET", "s3_path": "s3://customer-analytics-lake/curated/"}, None),
    "jdbc-source-to-jdbc": (
        {
            "type": "JDBC",
            "jdbc_url": "jdbc:postgresql://host:5432/warehouse",
            "jdbc_table": "public.customers",
            "jdbc_connection_name": "agentic-data-ingestion-pid-jdbc",
            "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:db-Abc",
        },
        {
            "jdbc_url": "jdbc:postgresql://src:5432/app",
            "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:src-Xyz",
            "read_mode": "incremental",
        },
    ),
}


def _load(mod_name: str):
    """Load a sibling module standalone, as test_quarantine_write_grant.py does."""
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(f"cgp_{mod_name}", mod_path, submodule_search_locations=[LAMBDA_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"cgp_{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_env_utils():
    """A private copy of ``shared/env_utils.py``, as test_source_sample_write_grant.py loads it.

    Several test modules install a ``MagicMock`` as ``sys.modules["env_utils"]`` at
    module scope, so in a full ``tests/unit/`` run ``iam_roles`` would bind a mocked
    ``get_aws_account_id`` and every ARN would carry a mock repr. Swapped in with
    ``monkeypatch.setitem`` so the previous entry is restored after the test.
    """
    spec = importlib.util.spec_from_file_location("env_utils", os.path.join(SHARED_DIR, "env_utils.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_iam_roles(monkeypatch):
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    monkeypatch.setitem(sys.modules, "env_utils", _real_env_utils())
    iam_roles = _load("iam_roles")
    assert iam_roles.get_aws_account_id() == ACCOUNT, "iam_roles bound a stubbed account resolver"
    return iam_roles


def _build(monkeypatch, pipeline_id: str, target_config: dict, source_config: dict | None, boundary: str = ""):
    monkeypatch.setenv("GLUE_JOB_ROLE_BOUNDARY_ARN", boundary)
    monkeypatch.setenv("CHECKPOINT_BUCKET", f"adi-checkpoints-{ACCOUNT}-{REGION}")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    iam_roles = _load_iam_roles(monkeypatch)
    s3_table_bucket = {"bucket_name": target_config["bucket_name"]} if "bucket_name" in target_config else None
    role_arn = iam_roles.create_pipeline_specific_role(
        pipeline_id, dict(target_config), s3_table_bucket, source_config=source_config
    )
    return iam_roles, role_arn


def _role_name(pipeline_id: str) -> str:
    return f"AgenticDataIngestion-{pipeline_id}-ExecutionRole"


def _statements(pipeline_id: str) -> list[dict]:
    doc = boto3.client("iam", region_name=REGION).get_role_policy(
        RoleName=_role_name(pipeline_id), PolicyName="PipelineSpecificAccess"
    )
    return doc["PolicyDocument"]["Statement"]


def _as_list(value) -> list:
    return [value] if isinstance(value, str) else list(value or [])


def _granted(statements: list[dict], action: str) -> list[dict]:
    return [s for s in statements if action in _as_list(s.get("Action"))]


@pytest.mark.parametrize("label", list(TARGETS))
@mock_aws
def test_no_managed_policy_is_attached(monkeypatch, label):
    target_config, source_config = TARGETS[label]
    _build(monkeypatch, f"pid-{label}", target_config, source_config)

    attached = boto3.client("iam", region_name=REGION).list_attached_role_policies(RoleName=_role_name(f"pid-{label}"))

    assert attached["AttachedPolicies"] == [], (
        f"the {label} pipeline role has managed policies attached: {attached['AttachedPolicies']}. "
        "AWSGlueServiceRole grants glue:* on * (threat model T-02); the role must carry inline grants only."
    )


@pytest.mark.parametrize("label", list(TARGETS))
@mock_aws
def test_no_statement_grants_a_glue_wildcard_or_bucket_listing(monkeypatch, label):
    target_config, source_config = TARGETS[label]
    _build(monkeypatch, f"pid-{label}", target_config, source_config)

    for statement in _statements(f"pid-{label}"):
        actions = _as_list(statement.get("Action"))
        assert "glue:*" not in actions, f"glue:* granted on the {label} role: {statement}"
        assert "s3:ListAllMyBuckets" not in actions, f"s3:ListAllMyBuckets granted on the {label} role"
        if any(a.startswith("glue:") for a in actions):
            assert "*" not in _as_list(statement.get("Resource")), (
                f"a glue action on Resource '*' on the {label} role: {statement}"
            )


@pytest.mark.parametrize("label", list(TARGETS))
@mock_aws
def test_the_job_can_still_publish_metrics_and_data_quality_results(monkeypatch, label):
    target_config, source_config = TARGETS[label]
    _build(monkeypatch, f"pid-{label}", target_config, source_config)
    statements = _statements(f"pid-{label}")

    metric_grants = _granted(statements, "cloudwatch:PutMetricData")
    assert metric_grants, "no cloudwatch:PutMetricData: --enable-metrics and the DQ partial both publish metrics"
    namespaces = set()
    for grant in metric_grants:
        namespaces.update(_as_list(grant["Condition"]["StringEquals"]["cloudwatch:namespace"]))
    assert {"Glue", "Glue Data Quality", "AgenticDataIngestion/DataQuality"} <= namespaces

    publish = _granted(statements, "glue:PublishDataQuality")
    assert publish, "no glue:PublishDataQuality: EvaluateDataQuality publishes results (_data_quality.py.j2)"
    assert any(r.endswith(":dataQualityRuleset/*") for g in publish for r in _as_list(g["Resource"]))


@mock_aws
def test_a_glue_catalog_target_outside_the_project_prefix_is_granted_by_name(monkeypatch):
    target_config, _ = TARGETS["glue-catalog"]
    _build(monkeypatch, "pid-gc", target_config, None)
    statements = _statements("pid-gc")

    resources = {r for s in _granted(statements, "glue:GetPartitions") for r in _as_list(s["Resource"])}

    assert f"arn:aws:glue:{REGION}:{ACCOUNT}:database/sales_db" in resources
    assert f"arn:aws:glue:{REGION}:{ACCOUNT}:table/sales_db/orders" in resources
    assert not any(r.endswith(":database/*") or r.endswith(":table/*/*") for r in resources), (
        "the catalog grant widened to every database"
    )


@mock_aws
def test_a_wildcard_database_name_is_not_interpolated_into_an_arn(monkeypatch):
    _build(monkeypatch, "pid-gcw", {"type": "GLUE_CATALOG", "database": "*", "table": "orders"}, None)

    resources = {r for s in _statements("pid-gcw") for r in _as_list(s.get("Resource"))}

    assert f"arn:aws:glue:{REGION}:{ACCOUNT}:database/*" not in resources


@mock_aws
def test_the_permissions_boundary_is_set_when_configured(monkeypatch):
    iam = boto3.client("iam", region_name=REGION)
    boundary = iam.create_policy(
        PolicyName="AgenticDataIngestion-GlueJobRoleBoundary-test",
        PolicyDocument=json.dumps(
            {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "s3:GetObject", "Resource": "*"}]}
        ),
    )["Policy"]["Arn"]

    _build(monkeypatch, "pid-bound", *TARGETS["s3-parquet"], boundary=boundary)

    role = iam.get_role(RoleName=_role_name("pid-bound"))["Role"]
    assert role.get("PermissionsBoundary", {}).get("PermissionsBoundaryArn") == boundary


@mock_aws
def test_a_role_build_failure_raises_instead_of_falling_back(monkeypatch):
    monkeypatch.setenv("GLUE_JOB_ROLE_BOUNDARY_ARN", "")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    iam_roles = _load_iam_roles(monkeypatch)

    def _refuse(**_kwargs):
        raise RuntimeError("PutRolePolicy denied")

    monkeypatch.setattr(iam_roles.iam_client, "put_role_policy", _refuse)

    # A valid bucket name: a one-character bucket such as `b` is now refused
    # before CreateRole (security finding S1), which is not the failure under test.
    with pytest.raises(RuntimeError, match="PutRolePolicy denied"):
        iam_roles.create_pipeline_specific_role(
            "pid-fail", {"type": "S3_PARQUET", "s3_path": "s3://customer-analytics-lake/p/"}
        )

    names = {r["RoleName"] for r in boto3.client("iam", region_name=REGION).list_roles()["Roles"]}
    assert "AgenticDataIngestionExecutionRole" not in names, "the shared fallback role was created"
    assert not hasattr(iam_roles, "create_glue_execution_role"), "the fallback role builder still exists"


@mock_aws
def test_the_largest_branch_combination_fits_the_inline_policy_limit(monkeypatch):
    """JDBC source + JDBC target + checkpoint bucket is the most statements any role gets."""
    _build(monkeypatch, "pid-largest-combination", *TARGETS["jdbc-source-to-jdbc"])

    document = {"Version": "2012-10-17", "Statement": _statements("pid-largest-combination")}
    size = len(json.dumps(document, separators=(",", ":")))

    assert size < IAM_ROLE_INLINE_POLICY_LIMIT, (
        f"the inline policy is {size} characters; IAM refuses more than {IAM_ROLE_INLINE_POLICY_LIMIT}, "
        "and without the fallback role that refusal fails the step"
    )
