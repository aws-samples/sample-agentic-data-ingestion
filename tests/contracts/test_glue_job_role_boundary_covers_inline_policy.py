# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract: the Glue job permissions boundary covers every grant the role builder makes.

Threat model T-03 put a permissions boundary (``GlueJobRoleBoundary`` in
``deploy-cdk/lib/foundation-stack.ts``) on every per-pipeline Glue role that
``create_glue_pipeline/iam_roles.py`` creates at run time. A boundary is an
intersection: an action the inline policy grants but the boundary omits is
silently ineffective, and surfaces as an ``AccessDenied`` inside a Glue job
run, never at deploy time or in a unit test.

So the two producers are checked against each other here, across language
boundaries, in the style of this directory's other cross-language contracts
(``LGR-28b4d75-5``, template / ratified: derive the expectation from the
producer's own source). The consumer side is the real role builder, run under
moto for every target branch; the cap side is the boundary as written in the
TypeScript source. Every (action, resource) the builder grants must fall
under some boundary statement carrying that action with a matching resource
pattern. Conditions are not compared: a boundary condition can only narrow.

The same built roles carry the security finding S1 checks at the end of this
module, because the boundary is no help there: its S3 statement is ``*``. No S3
grant may name a wildcard bucket other than the two fixed system families, an
operator-named bucket is granted by its exact name, and an S3 location that
breaks a bucket naming rule refuses the role at build, before any role exists.
"""

import fnmatch
import importlib.util
import re
import sys
from pathlib import Path

import boto3
import pytest
from moto import mock_aws

_REPO_ROOT = Path(__file__).resolve().parents[2]
_FOUNDATION = _REPO_ROOT / "deploy-cdk" / "lib" / "foundation-stack.ts"
_LAMBDA_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_create_glue_pipeline"
_SHARED_DIR = _REPO_ROOT / "shared"

ACCOUNT = "123456789012"
REGION = "us-west-2"

#: One configuration per inline-policy branch in create_pipeline_specific_role,
#: plus the JDBC-source block on top of a JDBC target (the largest role).
BRANCHES = {
    "s3-tables": ({"type": "S3_TABLES", "bucket_name": "agentic-data-ingestion-table-pid"}, None),
    "glue-catalog": ({"type": "GLUE_CATALOG", "database": "sales_db", "table": "orders"}, None),
    "s3-parquet": ({"type": "S3_PARQUET", "s3_path": "s3://customer-analytics-lake/curated/"}, None),
    "jdbc-redshift": (
        {
            "type": "JDBC",
            "jdbc_url": "jdbc:redshift://cluster:5439/dev",
            "jdbc_table": "public.customers",
            "jdbc_connection_name": "agentic-data-ingestion-pid-jdbc",
            "secret_arn": f"arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:db-Abc",
            # iam_roles.py keys the Redshift temp-dir grant on database_type.
            "database_type": "redshift",
            "temp_dir": "s3://redshift-temp-bucket/tmp/",
        },
        None,
    ),
    "jdbc-source-incremental": (
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


# ── The cap side: the boundary as written in foundation-stack.ts ──────


def _match_close(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the delimiter closing the one at ``start``; string contents are opaque."""
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            i = text.index(ch, i + 1)
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced {opener!r} from offset {start}")


def _literals(array_body: str) -> list[str]:
    return [m.group(2) for m in re.finditer(r"(['`])(.*?)\1", array_body)]


def _boundary_statements() -> list[tuple[list[str], list[str]]]:
    source = _FOUNDATION.read_text(encoding="utf-8")
    match = re.search(r"this\.glueJobRoleBoundary\s*=\s*new\s+iam\.ManagedPolicy\(", source)
    assert match, "this.glueJobRoleBoundary = new iam.ManagedPolicy(...) not found in foundation-stack.ts"
    definition = source[match.end() - 1 : _match_close(source, match.end() - 1, "(", ")")]

    statements = []
    for found in re.finditer(r"new iam\.PolicyStatement\(\{", definition):
        body = definition[found.end() - 1 : _match_close(definition, found.end() - 1, "{", "}")]
        actions = re.search(r"actions:\s*\[(.*?)\]", body, re.DOTALL)
        resources = re.search(r"resources:\s*\[(.*?)\]", body, re.DOTALL)
        assert actions and resources, f"a boundary statement without literal actions/resources: {body}"
        concrete = [
            r.replace("${region}", REGION).replace("${account}", ACCOUNT) for r in _literals(resources.group(1))
        ]
        statements.append((_literals(actions.group(1)), concrete))
    return statements


# ── The grant side: the real role builder, per branch ─────────────────


def _load(mod_name: str):
    for path in (_SHARED_DIR, _LAMBDA_DIR):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))
    spec = importlib.util.spec_from_file_location(
        f"cgp_contract_{mod_name}", _LAMBDA_DIR / f"{mod_name}.py", submodule_search_locations=[str(_LAMBDA_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"cgp_contract_{mod_name}"] = module
    spec.loader.exec_module(module)
    return module


def _as_list(value) -> list:
    return [value] if isinstance(value, str) else list(value or [])


def _role_builder(monkeypatch):
    """The real ``iam_roles`` module, loaded under this contract's environment."""
    for key, value in {
        "AWS_REGION": REGION,
        "AWS_DEFAULT_REGION": REGION,
        "AWS_ACCOUNT_ID": ACCOUNT,
        "STATE_TABLE": "agentic_data_ingestion_state",
        "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
        "AGENTIC_DATA_INGESTION_ENV": "local",
        "CHECKPOINT_BUCKET": f"adi-checkpoints-{ACCOUNT}-{REGION}",
        "GLUE_JOB_ROLE_BOUNDARY_ARN": "",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    # A test module elsewhere may have installed a MagicMock as
    # sys.modules["env_utils"]; under it every ARN carries a mock repr and no
    # boundary pattern matches. Load the real module for this test only.
    env_spec = importlib.util.spec_from_file_location("env_utils", _SHARED_DIR / "env_utils.py")
    real_env_utils = importlib.util.module_from_spec(env_spec)
    env_spec.loader.exec_module(real_env_utils)
    monkeypatch.setitem(sys.modules, "env_utils", real_env_utils)

    iam_roles = _load("iam_roles")
    assert iam_roles.get_aws_account_id() == ACCOUNT, "iam_roles bound a stubbed account resolver"
    return iam_roles


def _inline_grants(monkeypatch, label: str) -> list[tuple[str, str]]:
    target_config, source_config = BRANCHES[label]
    iam_roles = _role_builder(monkeypatch)
    pipeline_id = f"pid-{label}"
    s3_table_bucket = {"bucket_name": target_config["bucket_name"]} if "bucket_name" in target_config else None
    iam_roles.create_pipeline_specific_role(
        pipeline_id, dict(target_config), s3_table_bucket, source_config=source_config
    )

    document = boto3.client("iam", region_name=REGION).get_role_policy(
        RoleName=f"AgenticDataIngestion-{pipeline_id}-ExecutionRole", PolicyName="PipelineSpecificAccess"
    )["PolicyDocument"]
    return [
        (action, resource)
        for statement in document["Statement"]
        if statement.get("Effect", "Allow") == "Allow"
        for action in _as_list(statement.get("Action"))
        for resource in _as_list(statement.get("Resource"))
    ]


def test_the_boundary_parse_is_not_vacuous():
    statements = _boundary_statements()
    actions = {a for acts, _ in statements for a in acts}

    assert len(statements) >= 8
    assert {"glue:PublishDataQuality", "secretsmanager:GetSecretValue", "s3:PutObject"} <= actions
    assert not any(a.startswith(("iam:", "sts:", "lambda:", "states:")) for a in actions), (
        "the boundary allows identity, Lambda or Step Functions actions: it no longer caps escalation"
    )
    assert "glue:*" not in actions


@pytest.mark.parametrize("label", list(BRANCHES))
@mock_aws
def test_every_inline_grant_falls_under_the_boundary(monkeypatch, label):
    boundary = _boundary_statements()
    grants = _inline_grants(monkeypatch, label)
    assert grants, f"the {label} role has no inline grants; the builder did not run"

    uncovered = sorted(
        {
            (action, resource)
            for action, resource in grants
            if not any(
                action in actions and any(fnmatch.fnmatchcase(resource, pattern) for pattern in patterns)
                for actions, patterns in boundary
            )
        }
    )

    assert not uncovered, (
        f"the {label} pipeline role is granted {uncovered}, which GlueJobRoleBoundary does not allow. "
        "A boundary intersects with the inline policy, so these grants are dead and the Glue job "
        "fails with AccessDenied at run time. Add them to the boundary in foundation-stack.ts."
    )


# ── Security finding S1: no operator-named bucket is a wildcard ───────
#
# The boundary cannot catch an over-broad S3 grant, because its own S3
# statement is already `*`. So the bucket position of every S3 ARN the builder
# emits is checked here directly. IAM reads `*` and `?` in a Resource as
# wildcards; the S3 Parquet output path and a Redshift target's `temp_dir` used
# to be split on "/" and interpolated unchecked, so `s3://*/` granted
# Get/Put/Delete on `arn:aws:s3:::*/*`, every object in the account.

#: The only wildcard bucket positions the builder writes: fixed name families,
#: literals in iam_roles.py, granted to every pipeline role and capped by the
#: boundary. Anything else with `*` or `?` in the bucket position is a grant on
#: buckets nobody named, and must be reviewed rather than added here.
_SYSTEM_BUCKET_FAMILIES = frozenset({"adi-generated-code-*", "adi-pipeline-artifacts-*"})

_S3_ARN_PREFIX = "arn:aws:s3:::"

#: The operator-named bucket each branch must be granted, by exact name. A
#: refusal that dropped the grant instead would pass the wildcard check above
#: and fail this.
_OPERATOR_BUCKET = {"s3-parquet": "customer-analytics-lake", "jdbc-redshift": "redshift-temp-bucket"}

#: Each breaks an S3 bucket naming rule: wildcard, embedded wildcard, uppercase,
#: under 3 characters, adjacent dots, IPv4 form.
HOSTILE_LOCATIONS = [
    pytest.param("s3://*/", id="wildcard-bucket"),
    pytest.param("s3://a*b/x", id="embedded-wildcard"),
    pytest.param("s3://Bucket/x", id="uppercase"),
    pytest.param("s3://ab/", id="two-characters"),
    pytest.param("s3://bucket..x/", id="adjacent-dots"),
    pytest.param("s3://192.168.1.1/x", id="ipv4-form"),
]

#: Each site the builder interpolates an operator-named bucket from: the target
#: config with the hostile value placed, and the key the refusal must name.
_SITES = {
    "s3-parquet-s3_path": (lambda uri: {"type": "S3_PARQUET", "s3_path": uri}, "s3_path"),
    "s3-parquet-output_path": (lambda uri: {"type": "S3_PARQUET", "output_path": uri}, "output_path"),
    "s3-parquet-target_path": (lambda uri: {"type": "S3_PARQUET", "target_path": uri}, "target_path"),
    "s3-bucket-alias": (lambda uri: {"type": "S3_BUCKET", "s3_path": uri}, "s3_path"),
    "jdbc-redshift-temp_dir": (lambda uri: {**BRANCHES["jdbc-redshift"][0], "temp_dir": uri}, "temp_dir"),
}


def _s3_bucket_error(iam_roles):
    """The exact class the loaded builder raises, from the module its validator came from."""
    return iam_roles.bucket_from_s3_uri.__globals__["S3BucketNameError"]


@pytest.mark.parametrize("label", list(BRANCHES))
@mock_aws
def test_no_s3_grant_names_a_wildcard_bucket(monkeypatch, label):
    grants = _inline_grants(monkeypatch, label)

    wildcard_buckets = sorted(
        {
            (action, resource)
            for action, resource in grants
            if resource.startswith(_S3_ARN_PREFIX)
            and any(ch in resource[len(_S3_ARN_PREFIX) :].split("/", 1)[0] for ch in "*?")
            and resource[len(_S3_ARN_PREFIX) :].split("/", 1)[0] not in _SYSTEM_BUCKET_FAMILIES
        }
    )

    assert not wildcard_buckets, (
        f"the {label} pipeline role is granted {wildcard_buckets}: a wildcard in an S3 bucket position "
        "grants buckets nobody configured (security finding S1)"
    )


@pytest.mark.parametrize("label", list(_OPERATOR_BUCKET))
@mock_aws
def test_the_operator_named_bucket_is_granted_by_exact_name(monkeypatch, label):
    bucket = _OPERATOR_BUCKET[label]
    grants = set(_inline_grants(monkeypatch, label))

    for action in ("s3:GetObject", "s3:PutObject", "s3:DeleteObject"):
        assert (action, f"{_S3_ARN_PREFIX}{bucket}/*") in grants
    assert ("s3:ListBucket", f"{_S3_ARN_PREFIX}{bucket}") in grants


@pytest.mark.parametrize("site", list(_SITES))
@pytest.mark.parametrize("uri", HOSTILE_LOCATIONS)
@mock_aws
def test_a_hostile_s3_location_refuses_the_role_and_creates_none(monkeypatch, site, uri):
    build_config, key = _SITES[site]
    iam_roles = _role_builder(monkeypatch)
    pipeline_id = f"pid-hostile-{site}"

    with pytest.raises(_s3_bucket_error(iam_roles)) as refused:
        iam_roles.create_pipeline_specific_role(pipeline_id, build_config(uri), None)

    assert f"target_config.{key}" in str(refused.value)
    assert refused.value.value == uri, "the refusal must carry the configured value verbatim"
    iam = boto3.client("iam", region_name=REGION)
    with pytest.raises(iam.exceptions.NoSuchEntityException):
        iam.get_role(RoleName=iam_roles.pipeline_execution_role_name(pipeline_id))


@pytest.mark.parametrize("site", list(_SITES))
@mock_aws
def test_a_hostile_s3_location_refuses_an_existing_role_too(monkeypatch, site):
    """A role built before the check existed is not handed back for a hostile config.

    The builder returns an existing role unchanged, so a pipeline whose role was
    minted with `arn:aws:s3:::*/*` would keep that grant forever if the check sat
    after the cached return.
    """
    build_config, _ = _SITES[site]
    iam_roles = _role_builder(monkeypatch)
    pipeline_id = f"pid-legacy-{site}"
    boto3.client("iam", region_name=REGION).create_role(
        RoleName=iam_roles.pipeline_execution_role_name(pipeline_id),
        AssumeRolePolicyDocument='{"Version": "2012-10-17", "Statement": []}',
    )

    with pytest.raises(_s3_bucket_error(iam_roles)):
        iam_roles.create_pipeline_specific_role(pipeline_id, build_config("s3://*/"), None)


# ── S1 follow-up: the S3-based branch's table bucket name ─────────────
#
# The S3 Tables and Glue Catalog branch grants `arn:aws:s3:::{name}` and
# `arn:aws:s3:::{name}/*` on a bucket name it takes from the create_infrastructure
# step's output (the `s3_table_bucket` parameter) or from the latest
# create_glue_pipeline row's stored output. Both used to be interpolated
# unchecked, so a name of `*` granted Get/Put/Delete on every object in the
# account. Expectations come from the naming rules: the S3 Tables rules for an
# S3 Tables target (no dots), the general purpose rules for GLUE_CATALOG.

#: Each breaks an S3 table bucket naming rule.
HOSTILE_TABLE_BUCKET_NAMES = [
    pytest.param("*", id="wildcard"),
    pytest.param("a*b", id="embedded-wildcard"),
    pytest.param("?", id="question-mark"),
    pytest.param("agentic-data-ingestion-table-*", id="default-prefix-then-wildcard"),
    pytest.param("Orders", id="uppercase"),
    pytest.param("my.bucket", id="dot"),
    pytest.param("my_bucket", id="underscore"),
    pytest.param("ab", id="two-characters"),
]

_TABLE_TARGET_TYPES = ["S3_TABLES", "S3_TABLE_BUCKET", "NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET"]

_STATE_TABLE = "agentic_data_ingestion_state"


def _role_exists(iam_roles, pipeline_id) -> bool:
    iam = boto3.client("iam", region_name=REGION)
    try:
        iam.get_role(RoleName=iam_roles.pipeline_execution_role_name(pipeline_id))
    except iam.exceptions.NoSuchEntityException:
        return False
    return True


def _granted(pipeline_id) -> set[tuple[str, str]]:
    document = boto3.client("iam", region_name=REGION).get_role_policy(
        RoleName=f"AgenticDataIngestion-{pipeline_id}-ExecutionRole", PolicyName="PipelineSpecificAccess"
    )["PolicyDocument"]
    return {
        (action, resource)
        for statement in document["Statement"]
        for action in _as_list(statement.get("Action"))
        for resource in _as_list(statement.get("Resource"))
    }


def _store_bucket_name(table, pipeline_id, bucket_name):
    """The row `_stored_s3_tables_bucket_name` reads: the latest create_glue_pipeline output."""
    table.put_item(
        Item={
            "run_id": f"run-{pipeline_id}",
            "step_name": "create_glue_pipeline",
            "pipeline_id": pipeline_id,
            "timestamp": "2026-10-01T00:00:00+00:00",
            "output_data": {"s3_table_bucket": {"bucket_name": bucket_name}},
        }
    )


@pytest.mark.parametrize("target_type", _TABLE_TARGET_TYPES)
@pytest.mark.parametrize("name", HOSTILE_TABLE_BUCKET_NAMES)
@mock_aws
def test_a_hostile_table_bucket_name_refuses_the_role_and_creates_none(monkeypatch, target_type, name):
    iam_roles = _role_builder(monkeypatch)
    pipeline_id = "pid-hostile-table"

    with pytest.raises(_s3_bucket_error(iam_roles)) as refused:
        iam_roles.create_pipeline_specific_role(pipeline_id, {"type": target_type}, {"bucket_name": name})

    assert "s3_table_bucket.bucket_name" in str(refused.value)
    assert refused.value.value == name, "the refusal must carry the passed name verbatim"
    assert not _role_exists(iam_roles, pipeline_id)


@pytest.mark.parametrize("target_type", _TABLE_TARGET_TYPES)
@mock_aws
def test_a_hostile_table_bucket_name_refuses_an_existing_role_too(monkeypatch, target_type):
    """A role built before the check existed is not handed back for a hostile name."""
    iam_roles = _role_builder(monkeypatch)
    pipeline_id = "pid-legacy-table"
    boto3.client("iam", region_name=REGION).create_role(
        RoleName=iam_roles.pipeline_execution_role_name(pipeline_id),
        AssumeRolePolicyDocument='{"Version": "2012-10-17", "Statement": []}',
    )

    with pytest.raises(_s3_bucket_error(iam_roles)):
        iam_roles.create_pipeline_specific_role(pipeline_id, {"type": target_type}, {"bucket_name": "*"})


@pytest.mark.parametrize("name", ["*", "a*b", "Bucket"])
@mock_aws
def test_a_glue_catalog_target_refuses_a_hostile_passed_name_too(monkeypatch, name):
    iam_roles = _role_builder(monkeypatch)
    target_config = dict(BRANCHES["glue-catalog"][0])

    with pytest.raises(_s3_bucket_error(iam_roles)):
        iam_roles.create_pipeline_specific_role("pid-catalog-hostile", target_config, {"bucket_name": name})

    assert not _role_exists(iam_roles, "pid-catalog-hostile")


@mock_aws
def test_a_glue_catalog_target_keeps_the_general_purpose_rules(monkeypatch):
    """GLUE_CATALOG's name reaches only `arn:aws:s3:::`, so a dotted name stays valid there."""
    iam_roles = _role_builder(monkeypatch)
    target_config = dict(BRANCHES["glue-catalog"][0])

    iam_roles.create_pipeline_specific_role("pid-catalog-dotted", target_config, {"bucket_name": "my.lake"})

    assert ("s3:GetObject", f"{_S3_ARN_PREFIX}my.lake/*") in _granted("pid-catalog-dotted")


@pytest.mark.parametrize("target_type", _TABLE_TARGET_TYPES)
@mock_aws
def test_a_valid_passed_name_is_granted_by_exact_name(monkeypatch, target_type):
    iam_roles = _role_builder(monkeypatch)
    pipeline_id = "pid-table-ok"

    iam_roles.create_pipeline_specific_role(pipeline_id, {"type": target_type}, {"bucket_name": "orders-tables"})

    grants = _granted(pipeline_id)
    assert ("s3:PutObject", f"{_S3_ARN_PREFIX}orders-tables/*") in grants
    assert ("s3:ListBucket", f"{_S3_ARN_PREFIX}orders-tables") in grants
    assert ("s3tables:PutTableData", f"arn:aws:s3tables:{REGION}:{ACCOUNT}:bucket/orders-tables") in grants


@pytest.mark.parametrize("name", HOSTILE_TABLE_BUCKET_NAMES)
def test_a_hostile_stored_name_refuses_the_role_and_creates_none(monkeypatch, mock_dynamodb_tables, name):
    """The stored name is validated outside the read's `except`, which would swallow the refusal.

    No name is passed, so the branch falls to its default and then to the stored
    name, the path that used to override the default with whatever was stored.
    """
    iam_roles = _role_builder(monkeypatch)
    table = mock_dynamodb_tables[_STATE_TABLE]
    monkeypatch.setattr(iam_roles, "pipeline_runs_table", table)
    pipeline_id = "pid-stored-hostile"
    _store_bucket_name(table, pipeline_id, name)

    with pytest.raises(_s3_bucket_error(iam_roles)) as refused:
        iam_roles.create_pipeline_specific_role(pipeline_id, {"type": "S3_TABLES"}, None)

    assert "output_data.s3_table_bucket.bucket_name" in str(refused.value)
    assert refused.value.value == name
    assert not _role_exists(iam_roles, pipeline_id)


def test_a_hostile_stored_name_refuses_an_existing_role_too(monkeypatch, mock_dynamodb_tables):
    iam_roles = _role_builder(monkeypatch)
    table = mock_dynamodb_tables[_STATE_TABLE]
    monkeypatch.setattr(iam_roles, "pipeline_runs_table", table)
    pipeline_id = "pid-stored-legacy"
    _store_bucket_name(table, pipeline_id, "*")
    boto3.client("iam", region_name=REGION).create_role(
        RoleName=iam_roles.pipeline_execution_role_name(pipeline_id),
        AssumeRolePolicyDocument='{"Version": "2012-10-17", "Statement": []}',
    )

    with pytest.raises(_s3_bucket_error(iam_roles)):
        iam_roles.create_pipeline_specific_role(pipeline_id, {"type": "S3_TABLES"}, None)


def test_a_valid_stored_name_still_replaces_the_default(monkeypatch, mock_dynamodb_tables):
    """Moving the read ahead of the cached return kept what it does: the stored name is granted."""
    iam_roles = _role_builder(monkeypatch)
    table = mock_dynamodb_tables[_STATE_TABLE]
    monkeypatch.setattr(iam_roles, "pipeline_runs_table", table)
    pipeline_id = "pid-stored-ok"
    _store_bucket_name(table, pipeline_id, "orders-tables")

    iam_roles.create_pipeline_specific_role(pipeline_id, {"type": "S3_TABLES"}, None)

    grants = _granted(pipeline_id)
    assert ("s3:GetObject", f"{_S3_ARN_PREFIX}orders-tables/*") in grants
    assert ("s3tables:CreateTable", f"arn:aws:s3tables:{REGION}:{ACCOUNT}:bucket/orders-tables") in grants
    assert not any(f"agentic-data-ingestion-table-{pipeline_id}" in r for _, r in grants)
