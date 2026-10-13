# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The per-pipeline Glue role can write the source sample — and only its own.

WHY THIS MODULE EXISTS. `glue_job_jdbc_source.py.j2` writes the frame it has just
read to
``s3://adi-processed-files-{account}-{region}/samples/{pipeline_id}/source-sample.json``,
the location `schema_extract` probes, so the key is a contract. The per-pipeline
role built by `iam_roles.create_pipeline_specific_role` granted `s3:PutObject` on
``adi-generated-code-*/*`` only — the quarantine prefix — and named the
processed-files bucket solely in its read and `ListBucket` statements. So every run
of every JDBC-source pipeline failed that write with AccessDenied.

It failed SILENTLY, which is why this module exists rather than a one-line fix.
The sample write sits in its own `try` and logs a warning, so the job still
reported SUCCEEDED and nothing surfaced the breakage — sampling was simply never
happening. Observed on run-2026-09-23-15-22-35-f826bece:

    Failed to write source sample: An error occurred (AccessDenied) when calling
    the PutObject operation: ... is not authorized to perform: s3:PutObject on
    resource: "arn:aws:s3:::adi-processed-files-.../samples/.../source-sample.json"

EXPECTATIONS HERE ARE DERIVED FROM THE TEMPLATE'S KEY, NOT FROM THE CURRENT
POLICY. The assertions state the ARN the template will actually PutObject to, so
the first one fails against unfixed code.

THE GRANT FOLLOWS THE SOURCE, NOT THE TARGET, and lives in the `is_jdbc_source`
block for that reason — the sample block is emitted by exactly one template, the
JDBC-source one, whatever the target is. `test_a_non_jdbc_source_gets_no_sample_grant`
is what keeps it from drifting into a target branch, where it would be granted to
pipelines that never write a sample and withheld from ones that do.

THE NARROWING ASSERTIONS ARE THE ONES THAT WILL EARN THEIR KEEP. Two tidy-looking
one-line changes would each break least privilege while leaving the feature
working, so both are asserted by consequence rather than by statement shape:

  * Appending `s3:PutObject` to the existing `s3:GetObject` statement. That
    statement's Resource list also names `adi-source-files-*` and the whole of
    `adi-processed-files-*`, so folding the action in hands the job write access to
    its own INPUT data — the bucket the S3-upload EventBridge rule watches, i.e. a
    job able to re-trigger itself. `test_quarantine_write_grant.py` documents the
    same trap for the quarantine grant and cites the record behind it.
  * Widening the resource to the bucket or to `samples/*`. Either lets one
    pipeline's job overwrite another pipeline's sample, which `schema_extract`
    would then read as its own.

THE ROLE IS BUILT AGAINST THE REAL `env_utils`, NOT WHATEVER `sys.modules` HOLDS.
Several tracked test modules install a `MagicMock` as `sys.modules["env_utils"]` at
module scope (the approval_handler, approval_timeout and dashboard_api create-pipeline
suites among them). pytest imports every module during collection, so in a full
`tests/unit/` run `iam_roles`' `from env_utils import get_aws_account_id` bound that
mock, and every ARN came out as `adi-processed-files-<MagicMock ...>-us-west-2`. Three
cases here then failed and, worse, the two narrowing tests passed vacuously: an ARN
carrying a mock repr can never equal a forbidden one. `_statements` therefore loads
`shared/env_utils.py` from its file path and swaps it in with `monkeypatch.setitem`
for the duration of the import only, and `_assert_real_account_resolver` fails
loudly if the loaded role builder still resolves to anything but the real
resolver. This follows `LGR-c58f276-4`, which records the same hazard for
`audit_utils` and leaves the polluters themselves as the open root cause.
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
PROCESSED_BUCKET = f"adi-processed-files-{ACCOUNT}-{REGION}"

#: The pipeline's own INPUT bucket. The S3-upload EventBridge rule watches it, so a
#: job able to write here could re-trigger itself.
SOURCE_OBJECTS = f"arn:aws:s3:::adi-source-files-{ACCOUNT}-{REGION}/*"

#: Every object in the processed-files bucket — what a bucket-wide widening looks
#: like, and what the read statement already names for GetObject.
PROCESSED_ALL_OBJECTS = f"arn:aws:s3:::{PROCESSED_BUCKET}/*"

JDBC_SOURCE = {
    "jdbc_url": "jdbc:mysql://src.example:3306/appdb",
    "jdbc_driver": "com.mysql.cj.jdbc.Driver",
    "jdbc_schema": "appdb",
    "jdbc_table": "customers",
    "engine": "mysql",
    "read_mode": "full_table",
    "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:src-AbCdEf",
}

JDBC_TARGET = {
    "type": "JDBC",
    "jdbc_url": "jdbc:postgresql://tgt.example:5432/warehouse",
    "jdbc_driver": "org.postgresql.Driver",
    "jdbc_table": "public.customers",
    "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:tgt-AbCdEf",
}

# A non-JDBC target, to pin that the grant follows the SOURCE.
S3_PARQUET_TARGET = {"type": "S3_PARQUET", "s3_path": "s3://customer-analytics-lake/curated/"}


def _sample_objects(pipeline_id: str) -> str:
    """The ARN the template's own key resolves to, for ``pipeline_id``."""
    return f"arn:aws:s3:::{PROCESSED_BUCKET}/samples/{pipeline_id}/*"


def _load(mod_name: str):
    """Load a sibling module of the create_glue_pipeline Lambda standalone.

    Same helper as `test_quarantine_write_grant.py` and
    `test_jdbc_source_job_wiring.py`; kept local for the reason theirs are.
    """
    mod_path = os.path.join(LAMBDA_DIR, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(f"cgp_{mod_name}", mod_path, submodule_search_locations=[LAMBDA_DIR])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"cgp_{mod_name}"] = mod
    spec.loader.exec_module(mod)
    return mod


def _real_env_utils():
    """A fresh private copy of ``shared/env_utils.py``, loaded from its file path.

    Never published to ``sys.modules`` by this function, so it neither trusts nor
    overwrites the entry another test module may have installed there. A fresh copy
    per call also keeps the module-level account-id cache from carrying between tests.
    """
    spec = importlib.util.spec_from_file_location("env_utils", os.path.join(SHARED_DIR, "env_utils.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _assert_real_account_resolver(iam_roles) -> None:
    """Fail loudly if the role builder bound a stubbed account resolver.

    Without this, the narrowing tests below pass vacuously under a stub: every ARN
    carries a mock repr instead of an account id, so it can never equal a forbidden one.
    """
    resolver = iam_roles.get_aws_account_id
    assert type(resolver).__module__ != "unittest.mock", (
        "iam_roles bound a mocked get_aws_account_id; the env_utils swap in _statements did not take effect"
    )
    assert resolver() == ACCOUNT, f"iam_roles resolves account {resolver()!r}, expected {ACCOUNT!r}"


def _statements(monkeypatch, pipeline_id: str, target_config: dict, source_config: dict | None) -> list[dict]:
    """The inline policy statements of the role built for these configs."""
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT)
    # Scoped to this test: monkeypatch restores whatever entry was there before.
    monkeypatch.setitem(sys.modules, "env_utils", _real_env_utils())
    iam_roles = _load("iam_roles")
    _assert_real_account_resolver(iam_roles)
    iam_roles.iam_client.attach_role_policy = mock.MagicMock()
    monkeypatch.setattr("time.sleep", lambda *_: None)

    iam_roles.create_pipeline_specific_role(
        pipeline_id,
        target_config,
        s3_table_bucket=None,
        source_config=source_config,
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
    [("pid-smp-jdbc", JDBC_TARGET), ("pid-smp-parquet", S3_PARQUET_TARGET)],
    ids=["jdbc-target", "s3-parquet-target"],
)
@mock_aws
def test_the_source_sample_write_is_permitted(monkeypatch, pipeline_id, target_config):
    """**Fails against unfixed code** — this was AccessDenied on every run."""
    statements = _statements(monkeypatch, pipeline_id, target_config, JDBC_SOURCE)
    put_resources = _resources_for(statements, "s3:PutObject")

    assert _sample_objects(pipeline_id) in put_resources, (
        f"the per-pipeline Glue role cannot write {_sample_objects(pipeline_id)}, so "
        f"the JDBC-source sample write fails with AccessDenied and sampling silently "
        f"never happens. s3:PutObject is granted on: {sorted(put_resources)}"
    )


@mock_aws
def test_a_non_jdbc_source_gets_no_sample_grant(monkeypatch):
    """The need follows the SOURCE — only the JDBC-source template samples.

    Keeps the grant from drifting into a target branch, where it would be handed
    to pipelines that never write a sample.
    """
    statements = _statements(monkeypatch, "pid-smp-s3upload", S3_PARQUET_TARGET, None)
    put_resources = _resources_for(statements, "s3:PutObject")

    sample_grants = {r for r in put_resources if "/samples/" in r}
    assert not sample_grants, f"an S3-upload-source pipeline was granted a sample write: {sorted(sample_grants)}"


@mock_aws
def test_the_sample_grant_does_not_reach_the_source_bucket(monkeypatch):
    """Asserted by consequence: the grant must be its own statement.

    Appending `s3:PutObject` to the read statement would satisfy the test above
    while granting write on `adi-source-files-*`, the bucket the S3-upload
    EventBridge rule watches.
    """
    statements = _statements(monkeypatch, "pid-smp-narrow", JDBC_TARGET, JDBC_SOURCE)
    put_resources = _resources_for(statements, "s3:PutObject")

    assert SOURCE_OBJECTS not in put_resources, (
        "s3:PutObject is granted on the pipeline's own input bucket, so the job can "
        "re-trigger itself — the sample grant has been folded into the read statement"
    )


@mock_aws
def test_the_sample_grant_is_prefix_scoped_not_bucket_wide(monkeypatch):
    """A bucket-wide grant would let a job overwrite a sibling's sample."""
    statements = _statements(monkeypatch, "pid-smp-scope", JDBC_TARGET, JDBC_SOURCE)
    put_resources = _resources_for(statements, "s3:PutObject")

    assert PROCESSED_ALL_OBJECTS not in put_resources, (
        f"s3:PutObject is granted on all of {PROCESSED_BUCKET}, not just this pipeline's samples/ prefix"
    )
    assert f"arn:aws:s3:::{PROCESSED_BUCKET}/samples/*" not in put_resources, (
        "s3:PutObject is granted on every pipeline's samples/ prefix, so one "
        "pipeline's job can overwrite another's sample"
    )


@mock_aws
def test_the_grant_names_only_this_pipelines_prefix(monkeypatch):
    """The prefix carries the role's own pipeline_id, so it cannot reach a sibling."""
    statements = _statements(monkeypatch, "pid-smp-mine", JDBC_TARGET, JDBC_SOURCE)
    put_resources = _resources_for(statements, "s3:PutObject")

    assert _sample_objects("pid-smp-mine") in put_resources
    assert _sample_objects("pid-smp-someone-else") not in put_resources
