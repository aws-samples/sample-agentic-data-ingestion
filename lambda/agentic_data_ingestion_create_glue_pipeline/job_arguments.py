# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Per-target Glue job argument builders and the JDBC target secret resolver.

Extracted from ``lambda_function.py`` (P21 Req 4.2) as part of decomposing the
oversized create_glue_pipeline entrypoint into focused sibling modules. This
module owns the JDBC target secret resolver (``resolve_jdbc_target_secret``,
plus ``secretsmanager_resource_arn`` for the IAM grant that goes with it), the
``DefaultArguments`` validation helper, and the per-target-type argument
builders (S3 Table Bucket, Glue Catalog, JDBC target, S3 Parquet, JDBC source).
``lambda_function.py`` imports the builders back so the orchestration functions
(``create_glue_job`` and friends) keep working unchanged.

``resolve_parameter_value`` and ``_resolve_rds_secret_field`` used to live here
too. They had no production caller — only the ``lambda_function`` re-export and
tests — and both resolved PLAINTEXT credentials from Secrets Manager inside this
Lambda, which is the mechanism standing invariant ``LGR-819980f-1`` exists to
rule out. They were deleted with the JDBC-target credential leak fix rather than
left as a working example of the forbidden pattern.

Dependency direction is one-way: ``lambda_function`` → ``job_arguments``, and
``iam_roles`` → ``job_arguments`` for the JDBC-target secret grant. This module
MUST NOT import either of them.
"""

import os
import sys
from typing import Any

import boto3
from aws_lambda_powertools import Logger
from botocore.exceptions import ClientError

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for imports (Layer_Import_Fallback — /opt/python
# in the Lambda layer, else the local ../../shared path for local/test runs).
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))


# `_get_glue_connection_tags` used to live here and has been removed. It read the
# `adi:secret_arn` tag off a Glue Connection, which is not possible: Glue rejects
# `GetTags` on a `…:connection/<name>` ARN with `InvalidInputException`, so the
# call raised every time and the helper returned `{}` on every invocation. Its
# only caller now reads `SECRET_ID` from `ConnectionProperties` directly, which is
# what it was already falling through to. Removing it also removes a
# `GetCallerIdentity` and a doomed Glue call from the credential path.


# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.7).
try:
    from env_utils import get_aws_account_id
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import get_aws_account_id

# Driver-class lookup is best-effort: used only to backfill a missing JDBC
# target driver from the engine name. If jdbc_helpers is unavailable in a
# given context, _resolve_jdbc_target_url_and_driver still takes the driver
# from the inline config, so a no-op fallback is safe.
try:
    from jdbc_helpers import get_jdbc_driver
except ImportError:  # pragma: no cover - exercised only when layer is absent

    def get_jdbc_driver(engine: str) -> str:  # type: ignore[misc]
        raise ValueError(f"jdbc_helpers unavailable; cannot resolve driver for {engine!r}")


# The one Source_Delimiter table (p55 W2 / R4.5), imported via the same
# Layer_Import_Fallback as env_utils above. Deliberately NOT a local copy of the
# {"csv": ",", "tsv": "\t"} literal — LGR-eee8ac8-5's reason applies verbatim:
# "a second definition in either Lambda would disable the check silently by
# never matching."
try:
    from format_delimiters import delimiter_for_key
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from format_delimiters import delimiter_for_key


# S3 Tables JAR path — read from env independently (mirrors lambda_function.py;
# simple env read, behavior-identical). Used by build_s3_table_bucket_arguments.
S3_TABLES_JAR_PATH = os.environ.get("S3_TABLES_JAR_PATH", "")

# Initialize AWS clients
# Convention: one client per AWS service per module (see shared/README.md), but
# created on first use and then cached rather than at import. Both halves of that
# convention matter and they pull in opposite directions at import time:
#
#   - One-per-module is what the convention is for. A client built per invocation
#     repeats session setup on a hot Lambda path.
#   - Building it AT IMPORT makes the module unimportable wherever AWS is not
#     configured: `boto3.client()` resolves a region eagerly and raises
#     NoRegionError. That took out an entire `tests/cdk/` module at collection
#     time on any machine without a region set, so the suite passed only because
#     of ambient developer credentials.
#
# Caching on first use satisfies both. `shared/jdbc_helpers.py` reaches the same
# conclusion for the same reason; see the module comment above its `_glue_client`.
#
# There is deliberately NO Secrets Manager client in this module, and that is the
# structural half of the JDBC-target credential fix. `LGR-819980f-1` (standing
# invariant): JDBC credentials are only ever referenced by Secrets Manager ARN;
# plaintext user/password never appear in Glue job arguments or ConnectionProperties.
# This Lambda used to call `GetSecretValue` for a JDBC target whose connection
# carried a SECRET_ID, copy the plaintext pair into `--{prefix}jdbc_user` /
# `--{prefix}jdbc_password`, and log the whole argument dict at INFO — so the
# credential reached the Glue console, CloudTrail, job-run history AND CloudWatch.
# The generated job now fetches the secret itself, at run time, by the reference
# this module passes; nothing here can read a credential because nothing here
# holds a client that could.
_glue_client_cache: Any = None


def _glue_client() -> Any:
    """Cached Glue client. Used by ``_read_jdbc_target_connection_properties`` to
    read a Glue Connection's properties. Patch this helper to inject a fake."""
    global _glue_client_cache
    if _glue_client_cache is None:
        _glue_client_cache = boto3.client("glue")
    return _glue_client_cache


# The two plaintext-credential keys a JDBC `target_config` may still carry: records
# written by an API client before `pipelines_crud` started refusing them, and
# fixtures. `build_jdbc_arguments` IGNORES both and logs only the key names — it
# never forwards a value, and never falls back to one when no secret resolves.
_PLAINTEXT_TARGET_CREDENTIAL_KEYS: tuple[str, ...] = ("jdbc_user", "jdbc_password")


def _jdbc_target_connection_name(target_config: dict[str, Any]) -> str:
    """The Glue Connection a JDBC target names, under either key spelling.

    ``select_rds`` / ``manual`` / legacy ``RDS_DATABASE`` records persist the
    connection under ``jdbc_connection_name``; ``glue_connection`` mode persists the
    user-selected one under ``glue_connection_name``. ``jdbc_connection_name`` is
    read FIRST, the same precedence ``build_jdbc_arguments`` (the
    ``--jdbc-connection-name`` metadata arg), ``job_builders._attach_jdbc_target_connection``
    (the connection the job actually runs through) and the JDBC-target branch of
    ``iam_roles.create_pipeline_specific_role`` (the ``glue:GetConnection`` grant)
    all use. So the connection the secret is read from is the connection the job is
    attached to. The deleted ``_resolve_jdbc_target_credentials`` read
    ``glue_connection_name`` first, which could only differ on a record carrying
    both keys; no writer produces one.
    """
    return str(target_config.get("jdbc_connection_name") or target_config.get("glue_connection_name") or "")


def _read_jdbc_target_connection_properties(connection_name: str, glue_client: Any = None) -> dict[str, Any]:
    """``ConnectionProperties`` of a JDBC target's Glue Connection.

    ``HidePassword=True`` is deliberate: a user-managed connection may still carry
    an inline ``PASSWORD`` (Glue rejects ``SECRET_ID`` alongside it, so such a
    connection has no SECRET_ID and is refused below anyway), and this Lambda has
    no use for it. Asking Glue not to return it means the plaintext never enters
    this process's memory, rather than entering it and being carefully ignored.

    Args:
        connection_name: The Glue Connection to read.
        glue_client: Optional Glue client. Defaults to this module's cached client;
            ``iam_roles`` passes its own module-level client (one client per
            service per module, ``shared/README.md``).

    Raises:
        ValueError: When Glue refuses the read (missing connection, access denied).
    """
    client = glue_client if glue_client is not None else _glue_client()
    try:
        response = client.get_connection(Name=connection_name, HidePassword=True)
    except ClientError as e:
        raise ValueError(f"Failed to read Glue Connection {connection_name!r} for the JDBC target: {e}") from e
    connection = response.get("Connection", {}) or {}
    props: dict[str, Any] = connection.get("ConnectionProperties", {}) or {}
    return props


def resolve_jdbc_target_secret(target_config: dict[str, Any], glue_client: Any = None) -> str:
    """Return the Secrets Manager reference behind a JDBC target's credentials.

    The ONE resolver for the JDBC target's credential, shared by the job argument
    (``build_jdbc_arguments`` → ``--{prefix}secret_arn``) and the IAM grant
    (``iam_roles.create_pipeline_specific_role`` → ``secretsmanager:GetSecretValue``
    on the same secret). Two independent reads are how the grant and the argument
    drifted apart before: the grant read ``target_config.get("secret_arn", <wildcard>)``
    while the argument path fell back to the connection's SECRET_ID, so a secret
    found only on the connection was passed to the job and never granted to it.

    Resolution order, and nothing else:

      1. ``target_config["secret_arn"]`` — the dashboard API copies the connection's
         ``SECRET_ID`` here at creation time (``_augment_glue_connection_config``),
         and ``select_rds`` / ``manual`` modes require it outright.
      2. ``ConnectionProperties.SECRET_ID`` of the named Glue Connection
         (``jdbc_connection_name``, else ``glue_connection_name``) — records written
         before the dashboard API copied it, or written by a client that did not.

    It NEVER calls ``GetSecretValue``: the value is a reference (an ARN, or a secret
    NAME — Glue accepts both for SECRET_ID, and so does ``get_secret_value`` in the
    generated job), passed as-is. When neither source yields one it raises rather
    than falling back to inline credentials, because there is no compliant fallback:
    a plaintext pair in job arguments is exactly what ``LGR-819980f-1`` forbids, and
    the inline-credential arm this replaces is what leaked a target credential.

    **Writes the resolved value back** to ``target_config["secret_arn"]`` when it
    came from the connection. ``create_glue_job`` passes the same ``target_config``
    object first to ``create_pipeline_specific_role`` and then to
    ``build_jdbc_arguments`` (``job_builders.py:465`` then ``:576``), so whichever
    runs first does the one ``GetConnection`` and the other reads the same value
    back — the grant and the argument cannot name different secrets. The write-back
    is in memory only: ``create_glue_job`` does not persist ``target_config``.

    Args:
        target_config: The JDBC target configuration. Mutated as described above.
        glue_client: Optional Glue client; see ``_read_jdbc_target_connection_properties``.

    Returns:
        The secret ARN or name to pass to the job as ``--{prefix}secret_arn``.

    Raises:
        ValueError: When the target has no ``secret_arn`` and either names no Glue
            connection, the connection cannot be read, or the connection carries no
            ``SECRET_ID``. The message names the connection and the fix.
    """
    secret_ref = str(target_config.get("secret_arn") or "")
    if secret_ref:
        return secret_ref

    connection_name = _jdbc_target_connection_name(target_config)
    if not connection_name:
        raise ValueError(
            "A JDBC target's credentials must be referenced through Secrets Manager: set "
            "secret_arn on the target configuration, or name a Glue connection "
            "(glue_connection_name / jdbc_connection_name) whose ConnectionProperties carry "
            "SECRET_ID. Plaintext jdbc_user / jdbc_password are not passed to the Glue job."
        )

    props = _read_jdbc_target_connection_properties(connection_name, glue_client)
    secret_ref = str(props.get("SECRET_ID") or "")
    if not secret_ref:
        # The remediation states the mutual exclusivity because that is the trap:
        # Glue refuses SECRET_ID *alongside* USERNAME/PASSWORD with "Validation for
        # connection properties failed", so adding SECRET_ID is not enough — the
        # inline pair has to go in the same call. Same wording as the source-side
        # refusal in `dashboard_api/handlers/pipelines_jdbc.py`.
        raise ValueError(
            f"The JDBC target's Glue connection {connection_name!r} carries no SECRET_ID, so its "
            "credentials cannot be referenced through Secrets Manager, and plaintext credentials "
            "are not passed to the Glue job. Fix the connection by setting SECRET_ID to the ARN of "
            "a Secrets Manager secret holding {username, password} in its ConnectionProperties "
            "— note Glue rejects SECRET_ID together with inline USERNAME/PASSWORD, so remove those "
            "in the same UpdateConnection call — then re-run the pipeline."
        )

    target_config["secret_arn"] = secret_ref
    return secret_ref


def secretsmanager_resource_arn(secret_ref: str, region: str, account_id: str) -> str:
    """The IAM ``Resource`` that covers a Secrets Manager secret given by ARN or name.

    Glue accepts ``SECRET_ID`` as either a full ARN or a secret NAME, and the
    generated job passes it to ``get_secret_value`` as-is, which accepts both. IAM
    does not: a policy ``Resource`` must be an ARN, and a secret's ARN ends in a
    six-character random suffix the name does not carry. So a name ``n`` is granted
    as ``arn:aws:secretsmanager:{region}:{account}:secret:n-??????`` — the
    documented Secrets Manager pattern for "this secret, whatever its suffix", and
    narrower than ``n*``, which would also match ``n-other``. The region and account
    are the job's own, which is where ``get_secret_value`` resolves a bare name.

    Anything starting with ``arn:`` is returned unchanged. A *partial* ARN (one
    without the suffix) therefore does not match the real secret and fails CLOSED
    at run time with AccessDenied, which is the safe direction; nothing in this
    repo writes one.

    The one place this conversion lives — ``iam_roles`` calls it for the JDBC-target
    grant, so the grant and the argument are derived from the same reference.
    """
    if secret_ref.startswith("arn:"):
        return secret_ref
    return f"arn:aws:secretsmanager:{region}:{account_id}:secret:{secret_ref}-??????"


def _resolve_jdbc_target_url_and_driver(target_config: dict[str, Any], glue_client: Any = None) -> tuple[str, str]:
    """Backfill a JDBC target's URL and driver — the non-credential half of what was
    ``_resolve_jdbc_target_credentials``.

    In the wizard's ``glue_connection`` mode the user selects an existing Glue
    Connection by name, and the wizard forwards its URL/driver best-effort. An empty
    ``--{prefix}jdbc_url`` is rejected by ``getResolvedOptions`` at job start
    ("expected one argument"), so a missing value is filled in:

      * ``jdbc_url``    ← ``ConnectionProperties.JDBC_CONNECTION_URL``
      * ``jdbc_driver`` ← ``jdbc_helpers.get_jdbc_driver(engine)``

    Inline ``target_config`` values always win. This now runs for every JDBC target,
    including secret-backed ones, which the old secret-mode branch skipped — so a
    secret-backed record with no ``jdbc_url`` used to emit an empty argument.

    The credential half is gone, not moved: it read the connection's inline
    ``USERNAME`` / ``PASSWORD`` or called ``GetSecretValue`` on its ``SECRET_ID``, and
    copied the plaintext into job arguments (``LGR-819980f-1``).

    Returns:
        ``(jdbc_url, jdbc_driver)``, each a possibly-empty string.
    """
    jdbc_url = str(target_config.get("jdbc_url") or "")
    jdbc_driver = str(target_config.get("jdbc_driver") or "")

    if not jdbc_url:
        connection_name = _jdbc_target_connection_name(target_config)
        if connection_name:
            props = _read_jdbc_target_connection_properties(connection_name, glue_client)
            jdbc_url = str(props.get("JDBC_CONNECTION_URL") or "")

    if not jdbc_driver:
        engine = target_config.get("engine", "")
        if engine:
            try:
                jdbc_driver = get_jdbc_driver(engine)
            except ValueError:
                jdbc_driver = ""

    return jdbc_url, jdbc_driver


def validate_required_parameters(default_arguments: dict[str, str], required_params: list, target_type: str) -> None:
    """
    Validate that all required template parameters are present and non-empty in DefaultArguments.

    Logs warnings for missing/empty parameters and raises an error if critical parameters are missing.

    Args:
        default_arguments: The Glue job's DefaultArguments dict
        required_params: List of parameter names from getResolvedOptions
        target_type: Target type for error context

    Raises:
        ValueError: If critical parameters (source_path, warehouse_location for S3 Tables) are missing
    """
    missing = []
    empty = []

    for param in required_params:
        if param == "JOB_NAME":
            continue  # Glue provides this automatically

        key = f"--{param}"
        if key not in default_arguments:
            missing.append(param)
            logger.warning(f"Required parameter --{param} is MISSING from DefaultArguments")
        elif not default_arguments[key]:
            empty.append(param)
            logger.warning(f"Required parameter --{param} is EMPTY in DefaultArguments")

    # Critical parameters that must be present for the job to run
    critical_params = ["source_path"]
    if target_type.upper() in ["S3_TABLES", "S3_TABLE_BUCKET", "NEW_S3_TABLE_BUCKET", "EXISTING_S3_TABLE_BUCKET"]:
        critical_params.extend(["table_bucket_arn", "namespace", "table_name"])

    critical_missing = [p for p in critical_params if p in missing or p in empty]
    if critical_missing:
        raise ValueError(
            f"Critical parameters missing or empty for {target_type} target: {critical_missing}. "
            f"All missing: {missing}, empty: {empty}"
        )

    if missing or empty:
        logger.warning(f"Non-critical parameter issues for {target_type}: missing={missing}, empty={empty}")
    else:
        logger.info(f"All {len(required_params)} required parameters validated for {target_type}")


def build_s3_table_bucket_arguments(
    target_config: dict[str, Any],
    s3_table_bucket: dict[str, Any],
    source_path: str = "",
    warehouse_location: str = "",
    key_prefix: str = "",
) -> dict[str, str]:
    """
    Build Glue job arguments for S3 Table Bucket targets with Iceberg format.
    Handles both NEW_S3_TABLE_BUCKET and EXISTING_S3_TABLE_BUCKET types.

    Args:
        target_config: Target configuration from pipeline metadata
        s3_table_bucket: S3 Table Bucket info from infrastructure step (for NEW buckets)
        source_path: S3 URI of the processed source file from file_validation step
        warehouse_location: S3 Tables warehouse location from infrastructure step or S3 Tables API
        key_prefix: Prefix applied to template-matching target-side keys (default ``""`` =
            byte-identical legacy behaviour). Mirrors the Jinja ``key_prefix`` mechanism
            in ``glue_job_jdbc_source.py.j2`` — when source=JDBC the template wraps the
            target-write partials with ``{% with key_prefix = 'target_' %}``, so the
            runtime ``DefaultArguments`` must use ``--target_<key>`` (e.g.
            ``--target_table_bucket_arn``) to satisfy the template's
            ``getResolvedOptions`` declarations. Only the four target-side
            template-matching keys (``table_bucket_arn``, ``namespace``, ``table_name``,
            ``warehouse_location``) are prefixed; ``--source_path`` and operational
            metadata keys are unchanged.

    Returns:
        dict: Job arguments for S3 Table Bucket target
    """
    try:
        target_type = target_config.get("type", "").upper()

        # Determine bucket, namespace, and table names based on target type
        if target_type == "NEW_S3_TABLE_BUCKET":
            # For new buckets, prioritize target_config (user's specification) over infrastructure step
            # The infrastructure step might have created a different bucket if there was an error
            bucket_name = target_config.get("bucket_name", s3_table_bucket.get("bucket_name", ""))
            logger.info(
                f"NEW_S3_TABLE_BUCKET: bucket_name from target_config={target_config.get('bucket_name')}, from s3_table_bucket={s3_table_bucket.get('bucket_name')}, final={bucket_name}"
            )
            namespace_raw = target_config.get(
                "namespace", target_config.get("namespace_name", s3_table_bucket.get("namespace", ""))
            )
            # Handle namespace as array or string (some steps store it as array)
            namespace_name = namespace_raw[0] if isinstance(namespace_raw, list) and namespace_raw else namespace_raw
            # Prioritize target_config (user's specification) over infrastructure step
            table_name = target_config.get("table_name", s3_table_bucket.get("table_name", ""))
            table_location = s3_table_bucket.get("data_location", "")
            metadata_location = s3_table_bucket.get("iceberg_metadata_location", "")
        elif target_type == "EXISTING_S3_TABLE_BUCKET":
            # For existing buckets, use info from target_config
            bucket_name = target_config.get("bucket_name", "")
            namespace_raw = target_config.get("namespace", target_config.get("namespace_name", ""))
            # Handle namespace as array or string
            namespace_name = namespace_raw[0] if isinstance(namespace_raw, list) and namespace_raw else namespace_raw
            table_name = target_config.get("table_name", "")
            # For existing buckets, we may not have these from infrastructure step
            table_location = s3_table_bucket.get("data_location", "")
            metadata_location = s3_table_bucket.get("iceberg_metadata_location", "")
        else:
            # Fallback for backward compatibility
            bucket_name = s3_table_bucket.get("bucket_name", target_config.get("bucket_name", ""))
            namespace_raw = target_config.get(
                "namespace", target_config.get("namespace_name", s3_table_bucket.get("namespace", ""))
            )
            # Handle namespace as array or string
            namespace_name = namespace_raw[0] if isinstance(namespace_raw, list) and namespace_raw else namespace_raw
            # Prioritize target_config (user's specification) over infrastructure step for table name
            table_name = target_config.get("table_name", s3_table_bucket.get("table_name", ""))
            table_location = s3_table_bucket.get("data_location", "")
            metadata_location = s3_table_bucket.get("iceberg_metadata_location", "")

        # Build S3 Tables warehouse ARN
        aws_region = os.environ.get("AWS_REGION", "us-east-1")
        aws_account_id = get_aws_account_id()
        warehouse_arn = f"arn:aws:s3tables:{aws_region}:{aws_account_id}:bucket/{bucket_name}"

        job_arguments = {
            # Template-matching parameters (lowercase_snake_case — MUST match getResolvedOptions).
            # The four target-side keys (table_bucket_arn, namespace, table_name,
            # warehouse_location) are prefixed with ``key_prefix`` to mirror the Jinja
            # ``{% with key_prefix = 'target_' %}`` block in glue_job_jdbc_source.py.j2.
            # Default key_prefix="" yields byte-identical legacy keys (--table_bucket_arn).
            "--source_path": source_path,
            f"--{key_prefix}table_bucket_arn": warehouse_arn,
            f"--{key_prefix}namespace": namespace_name or "default",
            f"--{key_prefix}table_name": table_name,
            f"--{key_prefix}warehouse_location": warehouse_location,
            # Operational metadata (backward-compatible, NOT used by generated code)
            "--target-type": "S3_TABLE_BUCKET",
            "--target-database-type": "s3-iceberg",
            "--target-bucket-name": bucket_name,
            "--target-namespace": namespace_name,
            "--target-table-name": table_name,
            "--target-table-location": table_location,
            # Iceberg configuration
            "--iceberg-enabled": "true",
            "--table-format": "iceberg",
            # S3 Tables JAR dependency (CRITICAL for S3 Tables support)
            "--extra-jars": S3_TABLES_JAR_PATH,
            # Iceberg properties
            "--iceberg-catalog-type": "s3tables",
            "--iceberg-write-format": "parquet",
            "--iceberg-compression": "snappy",
            "--iceberg-file-format": "parquet",
            # Legacy operational metadata (kept for CloudWatch dashboards / Glue console)
            "--TARGET_FORMAT": "iceberg",
            "--TABLE_BUCKET_ARN": warehouse_arn,
            "--S3_TABLES_CATALOG_NAME": "s3tablesbucket",
            "--S3_TABLES_NAMESPACE": namespace_name or "default",
            "--S3_TABLES_TABLE_NAME": table_name,
        }

        # Add metadata location only if it's provided
        if metadata_location:
            job_arguments["--iceberg-metadata-location"] = metadata_location

        # Add optional partition keys if specified
        partition_keys = target_config.get("partition_keys", [])
        if partition_keys:
            job_arguments["--partition-keys"] = ",".join(partition_keys)

        logger.info(f"Built S3 Table Bucket job arguments for {target_type}: bucket={bucket_name}, table={table_name}")
        return job_arguments

    except Exception as e:
        logger.error(f"Error building S3 Table Bucket arguments: {e}")
        raise


def build_glue_catalog_arguments(
    target_config: dict[str, Any], source_path: str = "", key_prefix: str = ""
) -> dict[str, str]:
    """
    Build Glue job arguments for Glue Catalog table targets.

    Args:
        target_config: Target configuration with database, table, format, etc.
        source_path: S3 URI of the processed source file from file_validation step
        key_prefix: Prefix applied to template-matching target-side keys (default ``""`` =
            byte-identical legacy behaviour). Mirrors the Jinja ``key_prefix`` mechanism
            in ``glue_job_jdbc_source.py.j2``. Only the two target-side template-matching
            keys (``database_name``, ``table_name``) are prefixed; ``--source_path`` and
            operational metadata keys are unchanged.

    Returns:
        dict: Job arguments for Glue Catalog target
    """
    try:
        # Extract Glue Catalog configuration (support both old and new field names)
        glue_database = target_config.get("database") or target_config.get("glue_database", "")
        glue_table = target_config.get("table") or target_config.get("glue_table", "")
        output_format = target_config.get("format", "parquet").lower()
        partition_keys = target_config.get("partition_keys", [])

        if not glue_database or not glue_table:
            raise ValueError("database and table are required for GLUE_CATALOG target")

        job_arguments = {
            # Template-matching parameters (lowercase_snake_case — MUST match getResolvedOptions).
            # The two target-side keys (database_name, table_name) are prefixed with
            # ``key_prefix`` to mirror the Jinja ``{% with key_prefix = 'target_' %}``
            # block in glue_job_jdbc_source.py.j2. Default key_prefix="" yields
            # byte-identical legacy keys (--database_name, --table_name).
            "--source_path": source_path,
            f"--{key_prefix}database_name": glue_database,
            f"--{key_prefix}table_name": glue_table,
            # Operational metadata (backward-compatible, NOT used by generated code)
            "--target-type": "GLUE_CATALOG",
            "--target-database-type": "glue-catalog",
            "--target-database": glue_database,
            "--target-table": glue_table,
            "--output-format": output_format,
            # Compression settings based on format
            "--compression": "snappy" if output_format in ["parquet", "orc"] else "gzip",
            # Write mode
            "--write-mode": target_config.get("write_mode", "append"),
        }

        # Add partition keys if specified
        if partition_keys:
            job_arguments["--partition-keys"] = ",".join(partition_keys)
            job_arguments["--enable-partition-filtering"] = "true"

        # SerDe configuration based on format
        if output_format == "parquet":
            job_arguments["--serde"] = "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe"
        elif output_format == "orc":
            job_arguments["--serde"] = "org.apache.hadoop.hive.ql.io.orc.OrcSerde"
        elif output_format == "json":
            job_arguments["--serde"] = "org.openx.data.jsonserde.JsonSerDe"
        elif output_format == "csv":
            job_arguments["--serde"] = "org.apache.hadoop.hive.serde2.lazy.LazySimpleSerDe"
            # R4.5: the field-delimiter argument carries the SOURCE's
            # Source_Delimiter, read off the source object's own extension
            # rather than hardcoded to a comma (p55 W2 — a TSV upload was
            # accepted and then parsed as one column). ``source_path`` is the
            # URI of the object the Glue job actually READS: it comes from
            # ``artifacts.get_source_path``, which returns the
            # ``readable_source_uri`` ``schema_extract`` resolved (p55 W3, task
            # 4.3). For a Native_Format_Set member that IS the original upload,
            # so the extension here is the real one (``.tsv``, ``.csv``,
            # ``.tsv.gz``); for an Excel_Workbook it is the converted ``.csv``,
            # whose delimiter is a comma — which is the right answer for that
            # object rather than for the workbook it came from. Either way the
            # extension read here belongs to the object being read, which is the
            # property this line needs. ``delimiter_for_key`` accepts a full
            # ``s3://`` URI because only the tail is read.
            #
            # The lookup is total and defaults to a comma (R4.6), so this is a
            # no-op for every non-TSV source — asserted in
            # ``tests/unit/lambdas/create_glue_pipeline/test_job_arguments_field_delimiter.py``
            # rather than assumed.
            job_arguments["--field-delimiter"] = delimiter_for_key(source_path)

        logger.info(f"Built Glue Catalog job arguments for {glue_database}.{glue_table}: {job_arguments}")
        return job_arguments

    except Exception as e:
        logger.error(f"Error building Glue Catalog arguments: {e}")
        raise


def build_jdbc_arguments(target_config: dict[str, Any], source_path: str = "", key_prefix: str = "") -> dict[str, str]:
    """
    Build Glue job arguments for JDBC database targets.

    The credential travels as ONE argument, ``--{prefix}secret_arn``, holding a
    Secrets Manager reference that the generated write block resolves at run time
    (``pyspark_templates/_partials/_write_jdbc.py.j2``). It is emitted for EVERY JDBC
    target. ``--{prefix}jdbc_user`` / ``--{prefix}jdbc_password`` are never emitted,
    whatever ``target_config`` carries — standing invariant ``LGR-819980f-1``.

    Args:
        target_config: Target configuration with jdbc_connection_name /
            glue_connection_name, jdbc_table (or database_name + table_name),
            secret_arn, etc. ``secret_arn`` may be written back by
            ``resolve_jdbc_target_secret`` when it is resolved from the connection.
        source_path: S3 URI of the processed source file from file_validation step
        key_prefix: Prefix applied to template-matching target-side keys (default ``""`` =
            byte-identical legacy behaviour). Mirrors the Jinja ``key_prefix`` mechanism
            in ``glue_job_jdbc_source.py.j2``. Only the four target-side template-matching
            keys (``jdbc_url``, ``jdbc_table``, ``jdbc_driver``, ``secret_arn``) are
            prefixed; ``--source_path`` and operational metadata keys are unchanged.

    Returns:
        dict: Job arguments for JDBC target

    Raises:
        ValueError: When no connection or target table is configured, or when no
            Secrets Manager reference can be resolved (see
            ``resolve_jdbc_target_secret``).
    """
    try:
        # Extract JDBC configuration.
        #
        # Connection name: the wizard's glue_connection mode persists the
        # connection under ``glue_connection_name``; older/RDS_DATABASE
        # payloads use ``jdbc_connection_name``. Accept either. The name here
        # feeds the operational-metadata ``--jdbc-connection-name`` arg
        # (NOT read by the generated code) and is where a missing secret
        # reference is read from — the connection that actually routes the job
        # into the VPC is attached to job ``Connections`` by
        # job_builders._add_jdbc_source_job_wiring / the JDBC-target branch.
        jdbc_connection_name = _jdbc_target_connection_name(target_config)
        database_name = target_config.get("database_name", "")
        table_name = target_config.get("table_name", "")
        write_mode = target_config.get("write_mode", "overwrite")

        # The generated PySpark target write consumes ``{prefix}jdbc_table``
        # (e.g. ``target_jdbc_table``) — that is the single required write
        # target. Resolve it from the explicit ``jdbc_table`` or derive it
        # from database_name.table_name. ``database_name``/``table_name`` are
        # only used for the non-functional operational-metadata args, so we
        # no longer hard-require them — requiring them broke glue_connection
        # mode, where the wizard supplies ``jdbc_table`` (and the database is
        # already embedded in the JDBC URL).
        resolved_jdbc_table = target_config.get("jdbc_table") or (
            f"{database_name}.{table_name}" if (database_name and table_name) else ""
        )

        if not jdbc_connection_name:
            raise ValueError(
                "A Glue connection is required for a JDBC target: set "
                "glue_connection_name (existing connection) or "
                "jdbc_connection_name."
            )
        if not resolved_jdbc_table:
            raise ValueError(
                "A target table is required for a JDBC target: set jdbc_table "
                "(schema-qualified, e.g. 'public.customers') or both "
                "database_name and table_name."
            )

        # Plaintext credential keys on the target config are IGNORED, never
        # forwarded and never used as a fallback when no secret resolves. Before
        # this fix an inline pair here "always won" and went straight into
        # `--{prefix}jdbc_user` / `--{prefix}jdbc_password`. The dashboard API now
        # refuses both keys on create and edit (`pipelines_crud`), so this branch
        # only sees records written before that refusal existed. The warning names
        # the KEYS only — a value in a log line is the same leak as a value in a job
        # argument, one console over.
        plaintext_keys = [key for key in _PLAINTEXT_TARGET_CREDENTIAL_KEYS if key in target_config]
        if plaintext_keys:
            logger.warning(
                f"JDBC target config for connection={jdbc_connection_name} carries plaintext credential "
                f"key(s) {plaintext_keys}; they are ignored. The Glue job reads its credentials from "
                f"Secrets Manager by reference. Remove the keys from the stored target_config."
            )

        # The credential reference: target_config.secret_arn, else the connection's
        # SECRET_ID, else ValueError. The same resolver feeds the IAM grant in
        # iam_roles, so the job is granted exactly the secret it is told to read.
        target_secret_arn = resolve_jdbc_target_secret(target_config)
        resolved_jdbc_url, resolved_jdbc_driver = _resolve_jdbc_target_url_and_driver(target_config)

        # Template-matching parameters. The target-side keys are prefixed with
        # ``key_prefix`` to mirror the Jinja ``{% with key_prefix = 'target_' %}``
        # block in glue_job_jdbc_source.py.j2. Default key_prefix="" yields
        # the keys glue_job_jdbc.py.j2 declares (--jdbc_url, ..., --secret_arn).
        job_arguments = {
            "--source_path": source_path,
            f"--{key_prefix}jdbc_url": resolved_jdbc_url,
            f"--{key_prefix}jdbc_table": resolved_jdbc_table,
            f"--{key_prefix}jdbc_driver": resolved_jdbc_driver,
            # A reference, not a credential. The generated write block resolves it
            # with get_secret_value at run time; the Glue console, CloudTrail and
            # job-run history only ever see this string.
            f"--{key_prefix}secret_arn": target_secret_arn,
            # Operational metadata (backward-compatible, NOT used by generated code)
            "--target-type": "JDBC",
            "--target-database-type": "jdbc",
            "--jdbc-connection-name": jdbc_connection_name,
            "--target-database": database_name,
            "--target-table": table_name,
            # Write configuration
            "--write-mode": write_mode,
            "--batch-size": str(target_config.get("batch_size", 10000)),
            "--num-partitions": str(target_config.get("num_partitions", 4)),
            # Performance tuning
            "--enable-jdbc-batch-insert": "true",
            "--jdbc-fetch-size": "1000",
        }

        # Add upsert configuration if write_mode is upsert
        if write_mode == "upsert":
            upsert_keys = target_config.get("upsert_keys", [])
            if not upsert_keys:
                raise ValueError("upsert_keys are required when write_mode is 'upsert'")

            job_arguments["--upsert-keys"] = ",".join(upsert_keys)
            job_arguments["--upsert-method"] = target_config.get("upsert_method", "merge")

        # KEY NAMES only. This line used to interpolate the whole dict, which put
        # the plaintext `--{prefix}jdbc_password` into CloudWatch at INFO on every
        # inline-credential pipeline. No credential is in the dict any more, but the
        # values are still not logged: a future key must not be able to reopen the
        # same hole by being added to the dict.
        logger.info(
            f"Built JDBC job arguments for connection={jdbc_connection_name}, "
            f"table={resolved_jdbc_table}: keys={sorted(job_arguments)}"
        )
        return job_arguments

    except Exception as e:
        logger.error(f"Error building JDBC arguments: {e}")
        raise


def build_s3_parquet_arguments(
    target_config: dict[str, Any], source_path: str = "", key_prefix: str = ""
) -> dict[str, str]:
    """
    Build Glue job arguments for S3 Parquet targets.

    Args:
        target_config: Target configuration with output_path, format, etc.
        source_path: S3 URI of the processed source file from file_validation step
        key_prefix: Prefix applied to template-matching target-side keys (default ``""`` =
            byte-identical legacy behaviour). Mirrors the Jinja ``key_prefix`` mechanism
            in ``glue_job_jdbc_source.py.j2``. Only the single target-side template-matching
            key (``output_path``) is prefixed; ``--source_path`` and operational metadata
            keys are unchanged.

    Returns:
        dict: Job arguments for S3 Parquet target
    """
    try:
        # The React wizard sends `s3_path` for S3_PARQUET targets; older configs
        # use output_path/target_path. Accept all so the generated Glue job gets a
        # non-empty --output_path (an empty value makes getResolvedOptions reject
        # the flag at job start).
        output_path = (
            target_config.get("s3_path") or target_config.get("output_path") or target_config.get("target_path") or ""
        )

        job_arguments = {
            # Template-matching parameters (lowercase_snake_case — MUST match getResolvedOptions).
            # The single target-side key (output_path) is prefixed with ``key_prefix`` to
            # mirror the Jinja ``{% with key_prefix = 'target_' %}`` block in
            # glue_job_jdbc_source.py.j2. Default key_prefix="" yields byte-identical
            # legacy keys (--output_path).
            "--source_path": source_path,
            f"--{key_prefix}output_path": output_path,
            # Operational metadata (backward-compatible, NOT used by generated code)
            "--target-type": target_config.get("type", "S3_PARQUET"),
            "--target-database-type": "s3-parquet",
            "--output-format": "parquet",
        }

        logger.info(f"Built S3 Parquet job arguments: output_path={output_path}")
        return job_arguments

    except Exception as e:
        logger.error(f"Error building S3 Parquet arguments: {e}")
        raise


def build_jdbc_source_arguments(source_config: dict[str, Any]) -> dict[str, str]:
    """
    Build Glue job source-side arguments for JDBC source pipelines.

    Produces the source-side ``--<key>`` arguments declared in the JDBC source
    PySpark template's ``getResolvedOptions`` call (Requirement 8.3 source-side
    list). The output MUST match the template's render-time
    ``getResolvedOptions`` keys exactly: ``getResolvedOptions`` requires every
    declared key to be present in ``sys.argv``, so the incremental-only keys
    (``--watermark_column``, ``--watermark_type``, ``--watermarks_table_name``)
    are emitted ONLY when ``source_config.read_mode == "incremental"`` —
    mirroring the template's ``{% if read_mode == "incremental" %}`` block.

    Keys intentionally NOT emitted:
        - ``JOB_NAME`` — provided automatically by Glue.
        - ``--pipeline_id`` / ``--run_id`` — runtime args supplied by
          ``run_pipeline.py`` via ``start_job_run``'s ``Arguments`` parameter
          (matching the existing ``build_jdbc_arguments`` pattern, where
          ``--source_path`` is the runtime-supplied analogue).

    Args:
        source_config: JDBC source configuration from the pipeline record.
            Required keys: ``jdbc_url``, ``jdbc_driver``, ``jdbc_schema``,
            ``jdbc_table``, ``secret_arn``, ``read_mode``.
            Required when ``read_mode == "incremental"``: ``watermark_column``,
            ``watermark_type``. The ``watermarks_table_name`` value is resolved
            from the ``WATERMARKS_TABLE`` environment variable (default
            ``agentic_data_ingestion_pipeline_watermarks``) — it is a system-managed DDB
            table name, not a user-supplied source_config field.

    Returns:
        dict mapping ``--<key>`` to value strings, ready for merging into a
        Glue job's ``DefaultArguments``.

    Raises:
        ValueError: If a required source_config field is missing or empty.
    """
    try:
        # Always-present source-side keys (Req 8.3 — every render).
        always_required = ("jdbc_url", "jdbc_driver", "jdbc_schema", "jdbc_table", "secret_arn", "read_mode")
        for key in always_required:
            if not source_config.get(key):
                raise ValueError(f"Missing required source_config field for JDBC source: '{key}'")

        read_mode = source_config["read_mode"]
        job_arguments: dict[str, str] = {
            "--jdbc_url": source_config["jdbc_url"],
            "--jdbc_driver": source_config["jdbc_driver"],
            "--jdbc_schema": source_config["jdbc_schema"],
            "--jdbc_table": source_config["jdbc_table"],
            "--secret_arn": source_config["secret_arn"],
            "--read_mode": read_mode,
        }

        # Incremental-only keys (Req 8.3 — only when read_mode == "incremental"):
        # MUST mirror the template's render-time conditional getResolvedOptions
        # list. Emitting these for snapshot mode would cause a render mismatch.
        if read_mode == "incremental":
            for key in ("watermark_column", "watermark_type"):
                if not source_config.get(key):
                    raise ValueError(f"Missing required source_config field for incremental JDBC source: '{key}'")
            # System-managed DDB table — resolved from env, not source_config.
            # Follows the inline `os.environ.get` precedent used by
            # build_s3_table_bucket_arguments (AWS_REGION, S3_TABLES_JAR_PATH).
            watermarks_table_name = os.environ.get("WATERMARKS_TABLE", "agentic_data_ingestion_pipeline_watermarks")
            job_arguments["--watermark_column"] = source_config["watermark_column"]
            job_arguments["--watermark_type"] = source_config["watermark_type"]
            job_arguments["--watermarks_table_name"] = watermarks_table_name

        logger.info(
            f"Built JDBC source job arguments: read_mode={read_mode}, "
            f"schema={source_config['jdbc_schema']}, table={source_config['jdbc_table']}"
        )
        return job_arguments

    except Exception as e:
        logger.error(f"Error building JDBC source arguments: {e}")
        raise
