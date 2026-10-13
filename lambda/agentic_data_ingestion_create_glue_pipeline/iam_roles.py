# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""IAM execution-role and Glue-database provisioning.

Extracted from ``lambda_function.py`` (P21 Req 4.2) as part of decomposing the
oversized create_glue_pipeline entrypoint into focused sibling modules. This
module owns pipeline-specific (least-privilege) IAM role creation and Glue
database creation. ``lambda_function.py`` imports
``create_pipeline_specific_role`` and ``create_glue_database`` back so the
orchestration functions (``create_glue_job``, ``deploy_glue_pipeline_with_mcp``,
``lambda_handler``) keep working unchanged. The shared fallback execution role
(``create_glue_execution_role``) is removed (threat model T-02): a failure to
build the pipeline role now fails the step instead of substituting a role with
AmazonS3FullAccess attached.

Dependency direction is one-way: ``lambda_function`` → ``iam_roles``. This
module MUST NOT import ``lambda_function``.
"""

import json
import os
import sys
from datetime import UTC, datetime
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for imports (Layer_Import_Fallback — /opt/python
# in the Lambda layer, else the local ../../shared path for local/test runs).
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.7).
try:
    from env_utils import get_aws_account_id, require_env

    # S1: the S3 bucket-name validators. Each raises identifier_utils.S3BucketNameError.
    from identifier_utils import bucket_from_s3_uri, validate_s3_bucket_name, validate_s3_table_bucket_name
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import get_aws_account_id, require_env
    from identifier_utils import bucket_from_s3_uri, validate_s3_bucket_name, validate_s3_table_bucket_name

# Typed DynamoDB item accessor (P22 Req 2) — narrow boto3-stubs' wide
# AttributeValueTypeDef union to a concrete dict at the item read site.
# Deterministic, 64-char-bounded execution-role name (sibling module, no deps).
from naming import pipeline_execution_role_name

from dynamo_typing import item_dict

# Environment variables (mirror lambda_function.py so behavior is identical).
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")
AWS_REGION = os.environ.get("AWS_REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))

# Threat model T-03: the permissions boundary every per-pipeline Glue role is
# created with. Foundation deploys it (`GlueJobRoleBoundary`) and conditions
# iam:CreateRole / iam:PutRolePolicy on it for both roles that run this code, so
# a role created WITHOUT it is refused by IAM. Read here rather than required,
# so unit tests (moto) need no value; in a deployed stack an empty value makes
# CreateRole fail with AccessDenied, which is the fail-closed outcome.
GLUE_JOB_ROLE_BOUNDARY_ARN = os.environ.get("GLUE_JOB_ROLE_BOUNDARY_ARN", "")

#: CloudWatch namespaces the Glue job role may publish to. PutMetricData has no
#: resource-level scoping, so the namespace condition is the only scope:
#: "Glue" (--enable-metrics), "Glue Data Quality" (EvaluateDataQuality's
#: enableDataQualityCloudWatchMetrics, per the Glue DQ minimum-permissions
#: policy) and the custom namespace `_partials/_data_quality.py.j2` writes.
GLUE_JOB_METRIC_NAMESPACES = ["Glue", "Glue Data Quality", "AgenticDataIngestion/DataQuality"]

#: Target types whose role is granted read/write on an operator-named output
#: bucket (the S3 Parquet / plain-S3 branch of create_pipeline_specific_role).
#: The dashboard API's create/edit check mirrors this set
#: (`pipelines_crud._S3_OUTPUT_TARGET_TYPES`).
S3_OUTPUT_TARGET_TYPES = ("S3_PARQUET", "S3", "S3_BUCKET")

#: Where that branch reads the output location, first non-empty wins. The same
#: order `job_arguments.build_s3_parquet_arguments` uses for `--output_path`.
S3_OUTPUT_PATH_KEYS = ("s3_path", "output_path", "target_path")

#: Target types whose role is granted s3tables actions on a table bucket (the
#: S3 Tables arm of the S3-based branch of create_pipeline_specific_role).
S3_TABLES_TARGET_TYPES = ("S3_TABLES", "S3_TABLE_BUCKET", "NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET")

#: Target types of that S3-based branch: the S3 Tables types plus GLUE_CATALOG.
#: Each is granted general purpose S3 access on the bucket name
#: `_s3_tables_bucket_names` resolves.
S3_TABLES_OR_CATALOG_TARGET_TYPES = (*S3_TABLES_TARGET_TYPES, "GLUE_CATALOG")

#: The name the S3-based branch grants when no table bucket name is passed in.
#: Its prefix is also what sends the branch to the stored name of an earlier run.
_DEFAULT_TABLE_BUCKET_PREFIX = "agentic-data-ingestion-table-"

# Initialize AWS clients
# Convention: one module-level client per AWS service (see shared/README.md).
iam_client = boto3.client("iam")
glue_client = boto3.client("glue")
dynamodb = boto3.resource("dynamodb")

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)


def create_pipeline_specific_role(
    pipeline_id: str,
    target_config: dict[str, Any],
    s3_table_bucket: dict[str, Any] | None = None,
    source_config: dict[str, Any] | None = None,
) -> str:
    """
    Create pipeline-specific IAM role with least-privilege permissions.

    Args:
        pipeline_id: Pipeline identifier
        target_config: Target configuration (determines required permissions)
        s3_table_bucket: S3 Table Bucket info (for S3 Tables permissions)
        source_config: Source configuration. When the source is JDBC
            (``source_config`` carries ``jdbc_url`` / ``secret_arn``), the
            Glue ETL job reads from a VPC-bound relational DB and must get
            the VPC/ENI permissions, read the SOURCE Secrets Manager secret,
            and (incremental mode) read/write the watermarks table — and it
            needs these regardless of the TARGET type. The pre-existing
            JDBC-target branch below only covered the case where the TARGET
            is JDBC, so JDBC-source → S3/Glue-catalog pipelines were missing
            them (P17 follow-up).

    Returns:
        str: Role ARN

    Raises:
        S3BucketNameError: When an S3 location this role would be granted on
            (the S3 Parquet output path, or a Redshift target's ``temp_dir``)
            is present but does not name one valid bucket, or when the table
            bucket name of an S3 Tables or Glue Catalog target is not a valid
            name. See ``_operator_named_s3_buckets`` and
            ``_s3_tables_bucket_names``.
    """
    try:
        role_name = pipeline_execution_role_name(pipeline_id)

        # Security finding S1: the operator-named S3 buckets this role is granted
        # on are validated FIRST, before the cached-role return and before
        # CreateRole. Before the cached return, because a pipeline row written
        # before the dashboard API checked these values may already have a role
        # carrying `arn:aws:s3:::*/*`, and handing that role back unchanged is
        # exactly the grant this refuses. Before CreateRole, because a role
        # created and then left without its inline policy is reused unchanged by
        # that same cached return on every later run (the KNOWN RESIDUAL the
        # JDBC branch below describes). This check makes no AWS call. The raise
        # propagates through this function's `except` to the handler, which
        # records it as the step's `error_message` and fails the step — the
        # merge_key refusal's route (`job_builders._common_write_arguments`).
        target_bucket, temp_bucket = _operator_named_s3_buckets(target_config)

        target_type = target_config.get("type", "").upper()

        # If target_type is empty, try to infer from existing pipeline metadata
        if not target_type and s3_table_bucket:
            target_type = "S3_TABLES"
            logger.info("Inferred target_type as S3_TABLES from s3_table_bucket parameter")

        # The same rule for the S3-based branch's table bucket name, which comes
        # from the create_infrastructure step's output or from an earlier run's
        # stored output and reaches `arn:aws:s3:::{name}/*`. Resolved here rather
        # than in the branch for the two reasons above, so the one DynamoDB read
        # that resolution can make (`_stored_s3_tables_bucket_name`) now also runs
        # on the cached path. A failed read still falls back, as it did inline.
        bucket_name, s3_tables_bucket = "", ""
        if target_type in S3_TABLES_OR_CATALOG_TARGET_TYPES:
            bucket_name, s3_tables_bucket = _s3_tables_bucket_names(pipeline_id, target_type, s3_table_bucket)

        # Check if role already exists (caching)
        try:
            role_response = iam_client.get_role(RoleName=role_name)
            role_arn: str = role_response["Role"]["Arn"]
            logger.info(f"Using existing pipeline-specific IAM role: {role_arn}")
            return role_arn
        except iam_client.exceptions.NoSuchEntityException:
            pass  # Role doesn't exist, create it

        # Trust policy for Glue service
        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {"Effect": "Allow", "Principal": {"Service": "glue.amazonaws.com"}, "Action": "sts:AssumeRole"}
            ],
        }

        # Create the role
        create_role_kwargs: dict[str, Any] = {
            "RoleName": role_name,
            "AssumeRolePolicyDocument": json.dumps(trust_policy),
            "Description": f"Execution role for AgenticDataIngestion pipeline {pipeline_id}",
            "Tags": [
                {"Key": "Project", "Value": "AgenticDataIngestion"},
                {"Key": "PipelineId", "Value": pipeline_id},
                {"Key": "CreatedBy", "Value": "agentic-data-ingestion-create-glue-pipeline"},
            ],
        }
        # T-03: the boundary caps what this role can ever be granted, so a caller
        # able to run this code cannot mint a Glue role with more than a Glue job
        # needs. IAM refuses CreateRole without it (see the module constant).
        if GLUE_JOB_ROLE_BOUNDARY_ARN:
            create_role_kwargs["PermissionsBoundary"] = GLUE_JOB_ROLE_BOUNDARY_ARN
        role_response = iam_client.create_role(**create_role_kwargs)

        role_arn = role_response["Role"]["Arn"]
        logger.info(f"Created pipeline-specific IAM role: {role_arn}")

        # Threat model T-02: NO managed policy is attached. This role used to
        # carry `service-role/AWSGlueServiceRole`, whose first statement grants
        # `glue:*` and `s3:ListAllMyBuckets` on `*` — every Glue connection's
        # properties, and delete on every Glue database, table and job in the
        # account, reachable from AI-generated code. Everything a generated job
        # actually uses from that policy is granted below, scoped:
        # CloudWatch metrics (namespace-conditioned), Glue Data Quality
        # publishing, its own job's read actions, and catalog access on its
        # own target. Roles created before this change keep the managed policy
        # until they are deleted and recreated (docs/TROUBLESHOOTING.md).

        # Wait for IAM role to propagate (eventual consistency)
        # This prevents "Role should be given assume role permissions" errors
        import time

        logger.info("Waiting 10 seconds for IAM role to propagate...")
        time.sleep(10)

        # Build inline policy based on target type
        policy_statements: list[dict[str, Any]] = []

        # Common: CloudWatch Logs
        policy_statements.append(
            {
                "Effect": "Allow",
                "Action": ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents"],
                "Resource": [f"arn:aws:logs:{AWS_REGION}:{get_aws_account_id()}:log-group:/aws-glue/*"],
            }
        )

        # Common: DynamoDB write for inline data quality results
        policy_statements.append(
            {
                "Effect": "Allow",
                "Action": ["dynamodb:PutItem"],
                "Resource": [
                    f"arn:aws:dynamodb:{AWS_REGION}:{get_aws_account_id()}:table/agentic_data_ingestion_state"
                ],
            }
        )

        # Common (T-02): the parts of the former AWSGlueServiceRole a generated
        # job uses, each scoped. See glue_job_runtime_statements for each grant.
        policy_statements.extend(glue_job_runtime_statements(pipeline_id))

        # S3-based targets (S3 Table Buckets or Glue Catalog). `bucket_name` and
        # `s3_tables_bucket` were resolved and validated before the cached-role
        # return above (`_s3_tables_bucket_names`).
        if target_type in S3_TABLES_OR_CATALOG_TARGET_TYPES:
            policy_statements.extend(
                [
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                        "Resource": [
                            f"arn:aws:s3:::{bucket_name}/*",
                            "arn:aws:s3:::adi-generated-code-*/*",
                            "arn:aws:s3:::adi-pipeline-artifacts-*/*",
                            f"arn:aws:s3:::adi-dependencies-{get_aws_account_id()}-{AWS_REGION}/*",
                            f"arn:aws:s3:::adi-source-files-{get_aws_account_id()}-{AWS_REGION}/*",
                            f"arn:aws:s3:::adi-processed-files-{get_aws_account_id()}-{AWS_REGION}/*",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket"],
                        "Resource": [
                            f"arn:aws:s3:::{bucket_name}",
                            "arn:aws:s3:::adi-generated-code-*",
                            "arn:aws:s3:::adi-pipeline-artifacts-*",
                            f"arn:aws:s3:::adi-dependencies-{get_aws_account_id()}-{AWS_REGION}",
                            f"arn:aws:s3:::adi-source-files-{get_aws_account_id()}-{AWS_REGION}",
                            f"arn:aws:s3:::adi-processed-files-{get_aws_account_id()}-{AWS_REGION}",
                        ],
                    },
                    {
                        "Effect": "Allow",
                        "Action": GLUE_CATALOG_TABLE_ACTIONS,
                        "Resource": [
                            f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:catalog",
                            f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:database/agentic_data_ingestion_*",
                            f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:table/agentic_data_ingestion_*/*",
                        ],
                    },
                ]
            )

            # T-02: a GLUE_CATALOG target may name an operator-chosen database
            # outside the project prefix. The attached `glue:*` used to cover it;
            # with that gone the configured database and table are granted by
            # name, and nothing else in the catalog is.
            if target_type == "GLUE_CATALOG":
                target_catalog_statement = glue_catalog_target_statement(target_config)
                if target_catalog_statement:
                    policy_statements.append(target_catalog_statement)

            # S3 Tables-specific permissions for Iceberg (complete set for read/write operations)
            if target_type in S3_TABLES_TARGET_TYPES:
                policy_statements.append(
                    {
                        "Effect": "Allow",
                        "Action": [
                            # Bucket operations
                            "s3tables:GetTableBucket",
                            # Namespace operations
                            "s3tables:GetNamespace",
                            "s3tables:CreateNamespace",
                            "s3tables:ListNamespaces",
                            # Table operations - Read
                            "s3tables:GetTable",
                            "s3tables:GetTableMetadataLocation",
                            "s3tables:GetTableData",
                            "s3tables:ListTables",
                            # Table operations - Write (CRITICAL for Glue jobs)
                            "s3tables:CreateTable",
                            "s3tables:PutTableData",
                            "s3tables:UpdateTableMetadataLocation",
                        ],
                        "Resource": [
                            # Bucket-level permissions
                            f"arn:aws:s3tables:{AWS_REGION}:{get_aws_account_id()}:bucket/{s3_tables_bucket}",
                            # Namespace-level permissions (wildcard for all namespaces)
                            f"arn:aws:s3tables:{AWS_REGION}:{get_aws_account_id()}:bucket/{s3_tables_bucket}/namespace/*",
                            # Table-level permissions (wildcard for all tables in all namespaces)
                            f"arn:aws:s3tables:{AWS_REGION}:{get_aws_account_id()}:bucket/{s3_tables_bucket}/table/*",
                            # Also allow wildcard at bucket level for discovery operations
                            f"arn:aws:s3tables:{AWS_REGION}:{get_aws_account_id()}:bucket/*",
                        ],
                    }
                )

        # JDBC Database targets
        elif target_type == "JDBC":
            # Accept BOTH key spellings: glue_connection mode persists the
            # connection under ``glue_connection_name`` (the user-selected
            # existing connection), while select_rds / manual / RDS_DATABASE
            # modes use ``jdbc_connection_name``. Reading only the latter
            # produced a wrong/default connection ARN in the glue:GetConnection
            # grant for glue_connection-mode targets. That used to be masked by
            # the attached AWSGlueServiceRole managed policy; since T-02 removed
            # it, this grant is the only route to the connection, so the alias
            # handling is load-bearing. Mirrors the same alias handling in
            # job_builders._attach_jdbc_target_connection and
            # job_arguments.build_jdbc_arguments.
            connection_name = (
                target_config.get("jdbc_connection_name")
                or target_config.get("glue_connection_name")
                or f"agentic-data-ingestion-{pipeline_id}-jdbc"
            )

            # The TARGET secret grant, from the SAME resolver that builds the job's
            # `--{prefix}secret_arn` argument (`job_arguments.resolve_jdbc_target_secret`).
            # Since the JDBC-target credential fix the generated write block ALWAYS
            # calls get_secret_value at run time (`_write_jdbc.py.j2`), so this
            # grant is what makes every JDBC-target run work, not an optional
            # extra — standing invariant `LGR-819980f-1`: credentials travel by
            # Secrets Manager reference only.
            #
            # This used to be `target_config.get("secret_arn", <pipeline wildcard>)`,
            # which was wrong twice. A `.get` default only applies when the key is
            # ABSENT, so a stored `secret_arn: ""` or `None` became the Resource
            # verbatim. And the argument path could fall back to the connection's
            # SECRET_ID, which this branch never read, so a secret found only on the
            # connection was handed to the job and never granted to it. Reading both
            # through one resolver removes the second, and the resolver reads
            # `secret_arn` with `or` rather than a `.get` default, which removes the
            # first.
            #
            # The resolver writes the resolved value back to
            # `target_config["secret_arn"]`. This branch runs BEFORE
            # `build_jdbc_arguments` in `create_glue_job` (job_builders.py:465 vs
            # :576), on the same dict, so the argument builder reads it back instead
            # of calling GetConnection a second time — one resolution, one secret.
            #
            # SECRET_ID may be a secret NAME rather than an ARN (Glue accepts both);
            # `secretsmanager_resource_arn` turns a name into the
            # `…:secret:<name>-??????` form IAM needs.
            #
            # A resolution FAILURE is deliberately not raised from here: this
            # function's own `except` turns any exception into the shared fallback
            # role, which carries AmazonS3FullAccess, and leaves this pipeline's role
            # created without its inline policy. `build_jdbc_arguments` raises the
            # same actionable ValueError a moment later and fails job creation, so
            # no job runs under the pipeline-scoped pattern used below in that case.
            # KNOWN RESIDUAL, not fixed here: the role itself is kept, and the
            # `get_role` early return at the top of this function reuses an existing
            # role UNCHANGED. A role built in that failure case, or before this fix,
            # keeps its old grant after the connection is fixed, and the job then
            # fails closed with AccessDenied on GetSecretValue. TROUBLESHOOTING.md
            # ("JDBC target secret could not be resolved") gives the remedy.
            #
            # Imported here rather than at module top: this branch is its only
            # consumer in this module. `job_arguments` never imports `iam_roles`, so
            # the dependency stays one-way.
            from job_arguments import resolve_jdbc_target_secret, secretsmanager_resource_arn

            try:
                target_secret_ref = resolve_jdbc_target_secret(target_config, glue_client=glue_client)
            except ValueError as e:
                logger.warning(
                    f"Could not resolve the JDBC target secret for pipeline {pipeline_id} "
                    f"(job creation will fail with the same error): {e}"
                )
                target_secret_ref = ""
            secret_arn = (
                secretsmanager_resource_arn(target_secret_ref, AWS_REGION, get_aws_account_id())
                if target_secret_ref
                else f"arn:aws:secretsmanager:{AWS_REGION}:{get_aws_account_id()}:secret:agentic-data-ingestion/{pipeline_id}/*"
            )

            policy_statements.extend(
                [
                    # VPC Permissions (CRITICAL for JDBC)
                    {
                        "Effect": "Allow",
                        "Action": [
                            "ec2:CreateNetworkInterface",
                            "ec2:DescribeNetworkInterfaces",
                            "ec2:DeleteNetworkInterface",
                            "ec2:DescribeVpcEndpoints",
                            "ec2:DescribeRouteTables",
                            "ec2:DescribeSecurityGroups",
                            "ec2:DescribeSubnets",
                            "ec2:DescribeVpcAttribute",
                        ],
                        "Resource": "*",
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["ec2:CreateTags", "ec2:DeleteTags"],
                        "Resource": "arn:aws:ec2:*:*:network-interface/*",
                        "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": ["aws-glue-service-resource"]}},
                    },
                    # Secrets Manager — the TARGET credential, read by the job at
                    # run time. Resolved above; never read by this Lambda.
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                        "Resource": secret_arn,
                    },
                    # Glue Connection
                    {
                        "Effect": "Allow",
                        "Action": ["glue:GetConnection", "glue:GetConnections"],
                        "Resource": [
                            f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:catalog",
                            f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:connection/{connection_name}",
                        ],
                    },
                    # S3 for script + shared deps + SOURCE data. The JDBC
                    # *target* branch previously granted only the generated
                    # script + artifacts buckets, omitting the source-files
                    # bucket — so a CSV (S3_UPLOAD) -> JDBC pipeline's Glue job
                    # could not read its --source_path and failed with
                    # AnalysisException [PATH_NOT_FOUND] (an S3 list/get denial
                    # surfaces as path-not-found in Spark). Mirror the
                    # S3-target / S3_PARQUET branches: also grant the source-
                    # files, processed-files, and dependencies buckets.
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject"],
                        "Resource": [
                            "arn:aws:s3:::adi-generated-code-*/*",
                            "arn:aws:s3:::adi-pipeline-artifacts-*/*",
                            f"arn:aws:s3:::adi-dependencies-{get_aws_account_id()}-{AWS_REGION}/*",
                            f"arn:aws:s3:::adi-source-files-{get_aws_account_id()}-{AWS_REGION}/*",
                            f"arn:aws:s3:::adi-processed-files-{get_aws_account_id()}-{AWS_REGION}/*",
                        ],
                    },
                    # p64 2c.1 — the DQ QUARANTINE WRITE. Since p64 2b.2 all five
                    # PySpark templates carry the EvaluateDataQuality block, and when
                    # it splits a frame it writes the failing rows to
                    # s3://adi-generated-code-*/quarantine/<run_id>/. This branch
                    # granted `s3:GetObject` on that bucket only (the statement
                    # above), so an opted-in JDBC-target pipeline with failing rows
                    # would have failed at the quarantine write with AccessDenied —
                    # fail-closed rather than row-losing, since the write sits
                    # outside the non-blocking try (R6.3), but safe and wrong. The
                    # S3_TABLES/GLUE_CATALOG branch has granted Put/Delete all along,
                    # which is why the gap was invisible while the DQ block lived in
                    # glue_job_s3_tables.py.j2 alone.
                    #
                    # 2b.4 deliberately withheld this grant because its gate made the
                    # quarantine write unrenderable for this target type, so the
                    # AccessDenied was unreachable and adding it would have widened a
                    # runtime role to serve a code path that could not execute
                    # (`LGR-9e76249-3` and `LGR-b46a5bb-2`, do not widen
                    # speculatively). 2c.1 made `quality_config.data_quality_enabled`
                    # settable, so the path became reachable in the same change and
                    # the grant lands with it — which is exactly what the three
                    # templates' own headers said to do.
                    #
                    # A SEPARATE statement rather than adding `s3:PutObject` to the
                    # GetObject statement above, and that is the narrow choice, not
                    # the tidy-looking one: that statement's Resource list also names
                    # the source-files and processed-files buckets, so folding the
                    # action in would grant write access to the pipeline's own INPUT
                    # data — a source bucket the S3-upload EventBridge rule watches,
                    # i.e. a job able to re-trigger itself. `LGR-53765e8-1` prefers
                    # one statement when the action set is identical; here it is not,
                    # and the resource is a strict subset, so one statement per
                    # (action set, resource set) is the least-privilege expression.
                    # No DeleteObject: the job only ever writes quarantine objects.
                    {
                        "Effect": "Allow",
                        "Action": ["s3:PutObject"],
                        "Resource": ["arn:aws:s3:::adi-generated-code-*/*"],
                    },
                    {
                        "Effect": "Allow",
                        "Action": ["s3:ListBucket"],
                        "Resource": [
                            "arn:aws:s3:::adi-generated-code-*",
                            "arn:aws:s3:::adi-pipeline-artifacts-*",
                            f"arn:aws:s3:::adi-dependencies-{get_aws_account_id()}-{AWS_REGION}",
                            f"arn:aws:s3:::adi-source-files-{get_aws_account_id()}-{AWS_REGION}",
                            f"arn:aws:s3:::adi-processed-files-{get_aws_account_id()}-{AWS_REGION}",
                        ],
                    },
                ]
            )

            # Add Redshift-specific permissions if needed. `temp_bucket` was
            # resolved and validated by `_operator_named_s3_buckets` at the top
            # of this function (S1), under this same condition; a `temp_dir`
            # whose bucket is not a valid name has already refused the role.
            if target_config.get("database_type") == "redshift":
                if temp_bucket:
                    policy_statements.append(
                        {
                            "Effect": "Allow",
                            "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                            "Resource": [f"arn:aws:s3:::{temp_bucket}/*"],
                        }
                    )
                    policy_statements.append(
                        {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{temp_bucket}"]}
                    )

        # S3 Parquet / plain-S3 targets
        # Without this branch the role received only the common CloudWatch Logs
        # + DynamoDB grants (no target branch matched S3_PARQUET), so the Glue
        # job failed at launch trying to download its PySpark script from
        # adi-generated-code-*, and could neither read the source data nor
        # write the output Parquet. Grant: read the generated script + shared
        # deps + source/processed source data, and write the resolved target
        # output bucket (parsed from the configured S3 path).
        elif target_type in S3_OUTPUT_TARGET_TYPES:
            account_id = get_aws_account_id()

            # The target output bucket, `target_bucket`, was resolved from the
            # configured S3 path and validated by `_operator_named_s3_buckets` at
            # the top of this function (S1). The React wizard sends `s3_path`;
            # older configs use output_path/target_path (mirrors
            # build_s3_parquet_arguments). It used to be parsed here with
            # `split("/")` and interpolated unchecked, so `s3://*/` granted
            # Get/Put/Delete on `arn:aws:s3:::*/*`: every object in the account.

            # Read: generated PySpark script + shared deps + source/processed
            # source data (the job reads --source_path from source/processed).
            read_object_resources = [
                "arn:aws:s3:::adi-generated-code-*/*",
                "arn:aws:s3:::adi-pipeline-artifacts-*/*",
                f"arn:aws:s3:::adi-dependencies-{account_id}-{AWS_REGION}/*",
                f"arn:aws:s3:::adi-source-files-{account_id}-{AWS_REGION}/*",
                f"arn:aws:s3:::adi-processed-files-{account_id}-{AWS_REGION}/*",
            ]
            list_bucket_resources = [
                "arn:aws:s3:::adi-generated-code-*",
                "arn:aws:s3:::adi-pipeline-artifacts-*",
                f"arn:aws:s3:::adi-dependencies-{account_id}-{AWS_REGION}",
                f"arn:aws:s3:::adi-source-files-{account_id}-{AWS_REGION}",
                f"arn:aws:s3:::adi-processed-files-{account_id}-{AWS_REGION}",
            ]
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": read_object_resources,
                }
            )

            # p64 2c.1 — the DQ QUARANTINE WRITE, the identical gap the JDBC branch
            # above had and closed in the same change. `read_object_resources` names
            # adi-generated-code-* with `s3:GetObject` only, so an opted-in
            # S3-Parquet-target pipeline with failing rows would have failed at
            # s3://adi-generated-code-*/quarantine/<run_id>/ with AccessDenied.
            # Withheld by 2b.4 while nothing could opt in; landed here because 2c.1
            # made the opt-in settable. The full argument — including why this is a
            # separate statement rather than `s3:PutObject` appended to the read
            # grant, whose resource list also names the pipeline's own source bucket
            # — is at the JDBC branch's copy above; do not merge the two statements
            # here either.
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:PutObject"],
                    "Resource": ["arn:aws:s3:::adi-generated-code-*/*"],
                }
            )

            # Write: the resolved target output bucket (parsed from s3_path).
            # When the target bucket is one of the system buckets above (e.g.
            # the processed-files bucket used by the e2e suite), this augments
            # the read grant with Put/Delete — explicit and harmless.
            if target_bucket:
                policy_statements.append(
                    {
                        "Effect": "Allow",
                        "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                        "Resource": [f"arn:aws:s3:::{target_bucket}/*"],
                    }
                )
                list_bucket_resources.append(f"arn:aws:s3:::{target_bucket}")

            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": list_bucket_resources,
                }
            )

        # ── JDBC SOURCE grants (independent of target type) ──────────
        # When the SOURCE is JDBC, the Glue ETL job reads from a VPC-bound
        # relational database, so it needs the same VPC/ENI permissions a
        # JDBC *target* needs, plus read access to the SOURCE secret, plus
        # (incremental mode) read/write on the watermarks table. The
        # pre-existing `elif target_type == "JDBC"` branch only covers
        # JDBC-target pipelines, so without this block a JDBC-source →
        # S3/Glue-catalog pipeline runs outside the VPC, can't read its
        # source secret, and (incremental) can't persist watermarks.
        src_cfg = source_config or {}
        is_jdbc_source = bool(src_cfg.get("jdbc_url") or src_cfg.get("glue_connection_name"))
        if is_jdbc_source:
            # VPC / ENI — Glue creates an ENI in the source VPC to reach the
            # DB. EC2 Describe + CreateNetworkInterface don't support
            # resource-level scoping, so Resource is "*" (mirrors the
            # JDBC-target block and the gluePipelineRole crawler grants).
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": [
                        "ec2:CreateNetworkInterface",
                        "ec2:DescribeNetworkInterfaces",
                        "ec2:DeleteNetworkInterface",
                        "ec2:DescribeVpcEndpoints",
                        "ec2:DescribeRouteTables",
                        "ec2:DescribeSecurityGroups",
                        "ec2:DescribeSubnets",
                        "ec2:DescribeVpcAttribute",
                    ],
                    "Resource": "*",
                }
            )
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["ec2:CreateTags", "ec2:DeleteTags"],
                    "Resource": "arn:aws:ec2:*:*:network-interface/*",
                    "Condition": {"ForAllValues:StringEquals": {"aws:TagKeys": ["aws-glue-service-resource"]}},
                }
            )
            # Source secret — the source script calls get_secret_value on
            # source_config.secret_arn. In glue_connection mode the
            # connection may carry inline credentials and have no secret_arn;
            # only add the grant when a concrete secret ARN is present.
            source_secret_arn = src_cfg.get("secret_arn")
            if source_secret_arn:
                policy_statements.append(
                    {
                        "Effect": "Allow",
                        "Action": ["secretsmanager:GetSecretValue", "secretsmanager:DescribeSecret"],
                        "Resource": source_secret_arn,
                    }
                )
            # Source Glue connection read (when using a named connection).
            source_connection_name = (
                src_cfg.get("glue_connection_name") or f"agentic-data-ingestion-{pipeline_id}-source"
            )
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["glue:GetConnection", "glue:GetConnections"],
                    "Resource": [
                        f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:catalog",
                        f"arn:aws:glue:{AWS_REGION}:{get_aws_account_id()}:connection/{source_connection_name}",
                    ],
                }
            )
            # Dependencies bucket read — the JDBC-source job loads
            # jdbc_helpers.py from s3://adi-dependencies-.../glue-deps/
            # via --extra-py-files. The S3-target branch already grants this
            # bucket, but JDBC/other targets do not, so grant it here so the
            # grant is present for every JDBC-source pipeline.
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject"],
                    "Resource": [f"arn:aws:s3:::adi-dependencies-{get_aws_account_id()}-{AWS_REGION}/*"],
                }
            )
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:ListBucket"],
                    "Resource": [f"arn:aws:s3:::adi-dependencies-{get_aws_account_id()}-{AWS_REGION}"],
                }
            )
            # Source sample write. `glue_job_jdbc_source.py.j2` is the ONLY
            # template that emits a sample block, and it writes the frame it has
            # already read to
            # s3://adi-processed-files-{account}-{region}/samples/{pipeline_id}/source-sample.json
            # — the location `schema_extract` probes, so the key is a contract.
            # The grant belongs in THIS block for that reason: the need follows
            # the JDBC *source*, not the target, exactly like the dependencies
            # read above.
            #
            # Without it the write failed with AccessDenied on every run of every
            # JDBC-source pipeline. It is caught and logged as a warning, so the
            # job still succeeded and nothing surfaced the breakage — sampling
            # was simply never happening.
            #
            # A SEPARATE statement, and prefix-scoped, for the reason the
            # quarantine `s3:PutObject` statement records at length: the read
            # statement's Resource list also names this pipeline's own source and
            # processed-files buckets, so folding `s3:PutObject` in would grant
            # write access to the job's INPUT data. Scoped to this pipeline's
            # `samples/` prefix rather than the bucket or `samples/*`, so the job
            # cannot touch another pipeline's sample. The prefix uses the same
            # `pipeline_id` the role is named for and that the job receives as
            # `--pipeline_id`; if those ever diverged this would fail closed with
            # AccessDenied rather than write into a sibling's prefix, which is the
            # safer direction. No DeleteObject: the key is overwritten each run.
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:PutObject"],
                    "Resource": [
                        f"arn:aws:s3:::adi-processed-files-{get_aws_account_id()}-{AWS_REGION}/samples/{pipeline_id}/*"
                    ],
                }
            )
            # Watermarks table — incremental mode get_item/put_item on
            # agentic_data_ingestion_pipeline_watermarks. Scoped to that one table.
            if str(src_cfg.get("read_mode", "")).lower() == "incremental":
                policy_statements.append(
                    {
                        "Effect": "Allow",
                        "Action": ["dynamodb:GetItem", "dynamodb:PutItem"],
                        "Resource": [
                            f"arn:aws:dynamodb:{AWS_REGION}:{get_aws_account_id()}:table/agentic_data_ingestion_pipeline_watermarks"
                        ],
                    }
                )

        # Create inline policy
        inline_policy = {"Version": "2012-10-17", "Statement": policy_statements}

        # Add checkpoint bucket permissions if configured
        checkpoint_bucket = os.environ.get("CHECKPOINT_BUCKET", "")
        if checkpoint_bucket:
            policy_statements.append(
                {
                    "Effect": "Allow",
                    "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"],
                    "Resource": [f"arn:aws:s3:::{checkpoint_bucket}/*"],
                }
            )
            policy_statements.append(
                {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": [f"arn:aws:s3:::{checkpoint_bucket}"]}
            )

        iam_client.put_role_policy(
            RoleName=role_name, PolicyName="PipelineSpecificAccess", PolicyDocument=json.dumps(inline_policy)
        )

        logger.info(f"Created pipeline-specific IAM role with least-privilege permissions: {role_arn}")
        logger.info(f"Target type: {target_type}, Permissions: {len(policy_statements)} statements")

        return role_arn

    except Exception as e:
        # T-02: no fallback role. This used to return a shared
        # `AgenticDataIngestionExecutionRole` carrying AWSGlueServiceRole,
        # AmazonS3FullAccess and AmazonRDSDataFullAccess — a failure to build
        # the least-privilege role silently handed the job a far broader one.
        # In a deployed stack that fallback could not even be created (its name
        # matches neither runtime role's iam:* resource pattern), so removing
        # it changes the failure message, not the outcome: the step fails.
        logger.error(f"Error creating pipeline-specific IAM role: {e}")
        raise


#: Catalog actions a job uses to read and write its target table:
#: `create_dynamic_frame.from_catalog`, `write_dynamic_frame.from_catalog` with
#: `enableUpdateCatalog`, and `spark.read.table`. The partition reads cover a
#: partitioned target, which only `glue:*` used to reach.
GLUE_CATALOG_TABLE_ACTIONS = [
    "glue:GetDatabase",
    "glue:GetTable",
    "glue:GetTables",
    "glue:UpdateTable",
    "glue:CreateTable",
    "glue:GetPartition",
    "glue:GetPartitions",
    "glue:BatchGetPartition",
    "glue:CreatePartition",
    "glue:UpdatePartition",
    "glue:BatchCreatePartition",
]


def glue_job_runtime_statements(pipeline_id: str) -> list[dict[str, Any]]:
    """The Glue-runtime grants every per-pipeline job role carries (threat model T-02).

    These replace the parts of the AWS managed ``AWSGlueServiceRole`` that a
    generated job actually exercises, each narrowed:

    * ``cloudwatch:PutMetricData``, conditioned on ``GLUE_JOB_METRIC_NAMESPACES``.
      Needed by ``--enable-metrics`` and by the DQ partial, both its
      ``EvaluateDataQuality`` publishing options and its own ``put_metric_data``.
    * Glue Data Quality result publishing, on ``dataQualityRuleset/*`` and this
      pipeline's jobs, per the Glue DQ "minimum permissions to run a data
      quality ETL job" policy. Without it ``process_rows`` can fail inside the
      partial's non-blocking ``try`` and skip the quarantine split.
    * ``glue:GetJob`` / ``glue:GetJobBookmark`` on this pipeline's jobs, for
      ``job.init``/``job.commit`` with bookmarks enabled. Read-only.

    Deliberately NOT granted, though the managed policy did: ``glue:*`` on
    ``*``, ``s3:ListAllMyBuckets``, ``s3:GetBucketAcl``, ``iam:GetRole*`` and
    the ``aws-glue-*`` bucket grants (no job reads or writes one).
    """
    account = get_aws_account_id()
    job_arns = [
        f"arn:aws:glue:{AWS_REGION}:{account}:job/agentic-data-ingestion-job-{pipeline_id}",
        # schema_evolution.create_versioned_job_name: "...-{pipeline_id}-v{n}"
        f"arn:aws:glue:{AWS_REGION}:{account}:job/agentic-data-ingestion-job-{pipeline_id}-v*",
    ]
    ruleset_arn = f"arn:aws:glue:{AWS_REGION}:{account}:dataQualityRuleset/*"
    return [
        {
            "Effect": "Allow",
            "Action": ["cloudwatch:PutMetricData"],
            "Resource": "*",
            "Condition": {"StringEquals": {"cloudwatch:namespace": GLUE_JOB_METRIC_NAMESPACES}},
        },
        {
            "Effect": "Allow",
            "Action": ["glue:PublishDataQuality", "glue:GetDataQualityResult"],
            "Resource": [ruleset_arn],
        },
        {
            "Effect": "Allow",
            "Action": ["glue:PutDataQualityStatisticAnnotation", "glue:PutDataQualityProfileAnnotation"],
            "Resource": [ruleset_arn, *job_arns],
        },
        {
            "Effect": "Allow",
            "Action": ["glue:GetJob", "glue:GetJobBookmark"],
            "Resource": job_arns,
        },
    ]


def glue_catalog_target_statement(target_config: dict[str, Any]) -> dict[str, Any] | None:
    """Catalog grant on the GLUE_CATALOG target's own database and table, by name.

    Reads the same keys ``job_arguments.build_glue_catalog_arguments`` does
    (``database``/``glue_database`` and ``table``/``glue_table``). Returns
    ``None`` when no database is configured; argument building then refuses the
    pipeline, so there is no job for the grant to serve. A database name that
    is not a plain identifier is not interpolated into an ARN.
    """
    database = target_config.get("database") or target_config.get("glue_database") or ""
    table = target_config.get("table") or target_config.get("glue_table") or ""
    if not database or not _is_catalog_name(database):
        return None
    table_resource = table if table and _is_catalog_name(table) else "*"
    account = get_aws_account_id()
    return {
        "Effect": "Allow",
        "Action": GLUE_CATALOG_TABLE_ACTIONS,
        "Resource": [
            f"arn:aws:glue:{AWS_REGION}:{account}:catalog",
            f"arn:aws:glue:{AWS_REGION}:{account}:database/{database}",
            f"arn:aws:glue:{AWS_REGION}:{account}:table/{database}/{table_resource}",
        ],
    }


def _is_catalog_name(name: str) -> bool:
    """Glue database/table names: lowercase letters, digits, underscore (and hyphen, which Glue tolerates)."""
    return bool(name) and len(name) <= 255 and all(ch.isalnum() or ch in "_-" for ch in name) and "*" not in name


def _operator_named_s3_buckets(target_config: dict[str, Any]) -> tuple[str, str]:
    """The S3 buckets ``create_pipeline_specific_role`` grants by operator-supplied name (S1).

    Returns ``(target_bucket, temp_bucket)``, each validated by
    ``identifier_utils.bucket_from_s3_uri``, or ``""`` where the role grants no
    such bucket. The two conditions are the ones the role builder's own branches
    use, so a key a branch never reads is never consulted here:

    * ``target_bucket`` — an S3 output target (``S3_OUTPUT_TARGET_TYPES``): the
      first non-empty of ``S3_OUTPUT_PATH_KEYS``. Those keys may be absent, which
      grants no output bucket, as before.
    * ``temp_bucket`` — a ``JDBC`` target with ``database_type`` ``redshift``:
      ``temp_dir``, when present.

    A location that is present but does not name exactly one valid bucket
    raises. It is never clamped to something narrower and never dropped:
    dropping the grant would leave a role that cannot write where the job is
    told to, which looks like a permissions bug rather than a refused input. A
    path without the ``s3://`` scheme is refused too. An output path without it
    used to grant nothing, so the job's write failed with AccessDenied at run
    time instead. ``temp_dir`` used to be read with or without the scheme; a
    Redshift temporary directory is an S3 URI, so the scheme-less form is now
    refused rather than guessed at.

    Raises:
        S3BucketNameError: Naming the offending value and its key.
    """
    target_type = str(target_config.get("type") or "").upper()

    target_bucket = ""
    if target_type in S3_OUTPUT_TARGET_TYPES:
        for key in S3_OUTPUT_PATH_KEYS:
            output_path = target_config.get(key)
            if output_path:
                target_bucket = bucket_from_s3_uri(output_path, f"target_config.{key}")
                break

    temp_bucket = ""
    if target_type == "JDBC" and target_config.get("database_type") == "redshift":
        temp_dir = target_config.get("temp_dir")
        if temp_dir:
            temp_bucket = bucket_from_s3_uri(temp_dir, "target_config.temp_dir")

    return target_bucket, temp_bucket


def _s3_tables_bucket_names(
    pipeline_id: str, target_type: str, s3_table_bucket: dict[str, Any] | None
) -> tuple[str, str]:
    """The bucket names the S3-based branch grants: ``(bucket_name, s3_tables_bucket)``.

    ``bucket_name`` goes into the general purpose S3 grants
    (``arn:aws:s3:::{bucket_name}`` and ``…/*``), ``s3_tables_bucket`` into the
    s3tables ones (``arn:aws:s3tables:…:bucket/{s3_tables_bucket}``). How they
    are chosen is unchanged from when this sat inline in that branch:

    * ``bucket_name`` is ``s3_table_bucket["bucket_name"]`` when the parameter
      carries that key, else ``agentic-data-ingestion-table-{pipeline_id}``.
      When it starts with that prefix or with ``tablebucket``, a different
      non-empty name stored by the latest ``create_glue_pipeline`` row replaces
      it (``_stored_s3_tables_bucket_name``).
    * ``s3_tables_bucket`` is the parameter's name when it carries one, else
      ``bucket_name``.

    **What is validated.** Each name that came from outside this function, the
    parameter's and the stored one, is checked before it is used. Neither is
    typed into this request: the parameter is the create_infrastructure step's
    output, and the stored name is an earlier run's. Both reach an IAM
    ``Resource``, where ``*`` and ``?`` are wildcards, so a name of ``*`` used to
    grant Get/Put/Delete on ``arn:aws:s3:::*/*``, every object in the account.
    The S3 Tables rules apply for an S3 Tables target and the general purpose
    rules for GLUE_CATALOG, whose name reaches only the ``arn:aws:s3:::`` grants.
    A name that fails is refused, never replaced by the default (S1,
    ``LGR-0c0eaf2-1``).

    **What is not.** The default is composed here from ``pipeline_id``, which
    ``pipelines_crud.create_pipeline`` builds from the pipeline name's letters,
    digits and hyphens plus a UUID fragment. It can exceed 63 characters for a
    long name, and a GLUE_CATALOG target never creates that bucket, so
    validating it would fail a working pipeline over a grant on a bucket that
    does not exist.

    **What this does not narrow.** The s3tables statement also grants
    ``arn:aws:s3tables:…:bucket/*`` for discovery, which covers every table
    bucket and table in the account whatever ``s3_tables_bucket`` is. This
    check closes the general purpose S3 wildcard; it does not bound the s3tables
    grant.

    Raises:
        S3BucketNameError: Naming the value and where it came from.
    """
    validate = validate_s3_table_bucket_name if target_type in S3_TABLES_TARGET_TYPES else validate_s3_bucket_name

    if s3_table_bucket and "bucket_name" in s3_table_bucket:
        bucket_name = validate(s3_table_bucket["bucket_name"], "s3_table_bucket.bucket_name")
    else:
        bucket_name = f"{_DEFAULT_TABLE_BUCKET_PREFIX}{pipeline_id}"

    if bucket_name.startswith(_DEFAULT_TABLE_BUCKET_PREFIX) or bucket_name.startswith("tablebucket"):
        stored_name = _stored_s3_tables_bucket_name(pipeline_id, bucket_name)
        if stored_name and stored_name != bucket_name:
            logger.info(f"Found actual S3 Tables bucket name in metadata: {stored_name} (was using: {bucket_name})")
            # Validated here, outside the read's `except`, which would otherwise
            # swallow the refusal and fall back to the name it was replacing.
            bucket_name = validate(stored_name, "output_data.s3_table_bucket.bucket_name of the latest run")

    s3_tables_bucket = s3_table_bucket.get("bucket_name", bucket_name) if s3_table_bucket else bucket_name
    return bucket_name, s3_tables_bucket


def _stored_s3_tables_bucket_name(pipeline_id: str, current_name: str) -> Any:
    """The table bucket name the latest ``create_glue_pipeline`` row stored, or ``None``.

    Returned unvalidated: ``_s3_tables_bucket_names`` validates it. A failed
    read is logged and answers ``None``, so the caller keeps ``current_name``.
    """
    try:
        logger.info("Attempting to retrieve actual S3 Tables bucket name from pipeline metadata")
        # P28 R8.2 (task 14.2): rows are written under the canonical
        # "create_glue_pipeline" name; the legacy "CreateGluePipeline" filter is
        # kept because this is a cross-run read over historical rows.
        metadata_response = pipeline_runs_table.query(
            IndexName="PipelineIndex",
            KeyConditionExpression="pipeline_id = :pid",
            FilterExpression="step_name = :step OR step_name = :legacy_step",
            ExpressionAttributeValues={
                ":pid": pipeline_id,
                ":step": "create_glue_pipeline",
                ":legacy_step": "CreateGluePipeline",
            },
            ScanIndexForward=False,  # Get most recent first
            Limit=1,
        )
        if metadata_response.get("Items"):
            output_data = item_dict(metadata_response["Items"][0], "output_data")
            return item_dict(output_data, "s3_table_bucket").get("bucket_name")
    except Exception as e:
        logger.warning(f"Could not retrieve S3 Tables bucket from metadata: {e}, using default: {current_name}")
    return None


def create_glue_database(pipeline_id: str) -> str:
    """Create Glue database for the pipeline"""
    try:
        database_name = f"agentic_data_ingestion_{pipeline_id.lower().replace('-', '_')}"

        # Check if database already exists
        try:
            glue_client.get_database(Name=database_name)
            logger.info(f"Glue database {database_name} already exists")
        except glue_client.exceptions.EntityNotFoundException:
            # Create the database
            glue_client.create_database(
                DatabaseInput={
                    "Name": database_name,
                    "Description": f"AgenticDataIngestion database for pipeline {pipeline_id}",
                    "Parameters": {
                        "created_by": "agentic_data_ingestion",
                        "pipeline_id": pipeline_id,
                        "created_at": datetime.now(UTC).isoformat(),
                    },
                }
            )
            logger.info(f"Created Glue database: {database_name}")

        return database_name

    except Exception as e:
        logger.error(f"Error creating Glue database: {e}")
        raise
