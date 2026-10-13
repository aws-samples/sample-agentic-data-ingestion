# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Single source of truth for JDBC URL/driver/port logic across the backend.

This module centralises the JDBC connection metadata that has historically
been duplicated across the codebase:

- The frontend ``JDBC_DRIVER_MAP`` constant in
  ``dashboard-react/src/pages/wizard/WizardStepTarget.tsx``.
- The ``build_jdbc_url`` / ``get_jdbc_driver`` / ``get_default_port``
  functions in ``lambda/agentic_data_ingestion_create_glue_pipeline/lambda_function.py``.
- The ``_auto_create_rds_glue_connection`` helper in
  ``lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines.py``.

Subsequent P17 tasks (2.2-2.4) will move the URL/driver/port functions,
identifier and watermark validation helpers, and the canonical
``create_glue_jdbc_connection`` function into this module. This first
revision (Task 2.1) defines only the engine-keyed constants so that later
tasks have a stable foundation to import from.

The module is bundled into the Strands Lambda layer at
``/opt/python/jdbc_helpers.py`` via CDK layer asset bundling (Task 2.2).
"""

from __future__ import annotations

import re

# Engines supported as JDBC sources and targets. The list MUST match the
# keys of ``JDBC_DRIVER_MAP`` below and the frontend ``JDBC_DRIVER_MAP``
# in ``WizardStepTarget.tsx``.
SUPPORTED_ENGINES: list[str] = [
    "postgres",
    "aurora-postgresql",
    "mysql",
    "aurora-mysql",
    "mariadb",
    "oracle-ee",
    "oracle-se2",
    "sqlserver-ee",
    "sqlserver-se",
    "sqlserver-ex",
    "sqlserver-web",
]

# Per-engine JDBC driver class, default port, and URL protocol fragment.
# This MUST stay byte-for-byte aligned with the frontend ``JDBC_DRIVER_MAP``
# in ``dashboard-react/src/pages/wizard/WizardStepTarget.tsx`` so that
# wizard-time auto-population and backend URL construction agree.
JDBC_DRIVER_MAP: dict[str, dict[str, str | int]] = {
    "postgres": {
        "driver": "org.postgresql.Driver",
        "port": 5432,
        "protocol": "postgresql",
    },
    "aurora-postgresql": {
        "driver": "org.postgresql.Driver",
        "port": 5432,
        "protocol": "postgresql",
    },
    "mysql": {
        "driver": "com.mysql.cj.jdbc.Driver",
        "port": 3306,
        "protocol": "mysql",
    },
    "aurora-mysql": {
        "driver": "com.mysql.cj.jdbc.Driver",
        "port": 3306,
        "protocol": "mysql",
    },
    "mariadb": {
        "driver": "org.mariadb.jdbc.Driver",
        "port": 3306,
        "protocol": "mariadb",
    },
    "oracle-ee": {
        "driver": "oracle.jdbc.OracleDriver",
        "port": 1521,
        "protocol": "oracle:thin:@",
    },
    "oracle-se2": {
        "driver": "oracle.jdbc.OracleDriver",
        "port": 1521,
        "protocol": "oracle:thin:@",
    },
    "sqlserver-ee": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
    "sqlserver-se": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
    "sqlserver-ex": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
    "sqlserver-web": {
        "driver": "com.microsoft.sqlserver.jdbc.SQLServerDriver",
        "port": 1433,
        "protocol": "sqlserver",
    },
}


# ---------------------------------------------------------------------------
# Identifier and watermark validation regexes
# ---------------------------------------------------------------------------
#
# These regexes are the source of truth for SQL identifier and watermark-value
# validation across the backend. They are applied:
#
#   * At wizard time, by the dashboard API, to surface friendly errors before
#     a pipeline is created (Req 3.5, 3.6).
#   * At job runtime, by the rendered PySpark template, as defence-in-depth
#     against SQL injection (Req 7.5). Spark JDBC does not support bind
#     parameters on the ``query`` option, so the only safe approach is strict
#     allow-list validation followed by string interpolation.
#
# The patterns MUST stay aligned with the wizard-time validation in the
# dashboard handlers and with the design document.

# Allowed SQL identifier: letter or underscore, followed by letters, digits,
# or underscores. Used for ``jdbc_schema``, ``jdbc_table``, and
# ``watermark_column``.
#
# We use ``\A``/``\Z`` (absolute string anchors) rather than ``^``/``$``
# because Python's ``$`` matches *before* a trailing newline by default —
# so ``re.match(r"^[A-Za-z_]...$", "A\n")`` would incorrectly succeed,
# letting an attacker smuggle a newline past the validator. ``\A``/``\Z``
# anchor at the absolute start/end of the string with no such carve-out,
# making the validators fail closed for newline-containing input.
WATERMARK_COLUMN_RE = re.compile(r"\A[A-Za-z_][A-Za-z0-9_]*\Z")

# Numeric watermark value: signed integer or decimal (no scientific notation,
# no leading ``+``).
WATERMARK_NUMERIC_RE = re.compile(r"\A-?\d+(\.\d+)?\Z")

# Timestamp watermark value: ISO 8601 date with optional time (``T`` or space
# separator), optional fractional seconds, and optional timezone offset
# (``Z`` or ``±HH:MM`` / ``±HHMM``).
WATERMARK_TIMESTAMP_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?\Z")


def get_jdbc_driver(engine: str) -> str:
    """Return the JDBC driver class name for the given engine.

    Args:
        engine: One of the keys in :data:`SUPPORTED_ENGINES`.

    Returns:
        The fully-qualified driver class name (e.g. ``"org.postgresql.Driver"``).

    Raises:
        ValueError: If ``engine`` is not a recognised engine.
    """
    try:
        return str(JDBC_DRIVER_MAP[engine]["driver"])
    except KeyError as exc:
        raise ValueError(f"Unsupported JDBC engine: {engine!r}. Supported engines: {SUPPORTED_ENGINES}") from exc


def get_default_port(engine: str) -> int:
    """Return the default TCP port for the given engine.

    Args:
        engine: One of the keys in :data:`SUPPORTED_ENGINES`.

    Returns:
        The default port (e.g. ``5432`` for Postgres).

    Raises:
        ValueError: If ``engine`` is not a recognised engine.
    """
    try:
        return int(JDBC_DRIVER_MAP[engine]["port"])
    except KeyError as exc:
        raise ValueError(f"Unsupported JDBC engine: {engine!r}. Supported engines: {SUPPORTED_ENGINES}") from exc


def build_jdbc_url(engine: str, host: str, port: int, database: str) -> str:
    """Construct the JDBC connection URL for the given engine.

    The URL formats are preserved byte-for-byte from the original implementation
    in ``lambda/agentic_data_ingestion_create_glue_pipeline/lambda_function.py`` so
    that previously-created Glue Connections continue to function unchanged.

    Args:
        engine: One of the keys in :data:`SUPPORTED_ENGINES`.
        host: The database hostname (no scheme, no path).
        port: The TCP port. Use :func:`get_default_port` to derive the default
            for an engine when the caller does not have one.
        database: The database / schema namespace identifier (database name for
            MySQL/SQL Server, schema for Postgres, service name for Oracle).

    Returns:
        A fully-qualified ``jdbc:<protocol>://...`` URL.

    Raises:
        ValueError: If ``engine`` is not a recognised engine.
    """
    if engine not in JDBC_DRIVER_MAP:
        raise ValueError(f"Unsupported JDBC engine: {engine!r}. Supported engines: {SUPPORTED_ENGINES}")

    if engine in ("postgres", "aurora-postgresql"):
        return f"jdbc:postgresql://{host}:{port}/{database}?ssl=true&sslmode=require"
    if engine == "mariadb":
        return f"jdbc:mariadb://{host}:{port}/{database}?useSSL=true&requireSSL=true"
    if engine in ("mysql", "aurora-mysql"):
        return f"jdbc:mysql://{host}:{port}/{database}?useSSL=true&requireSSL=true"
    if engine in ("oracle-ee", "oracle-se2"):
        return f"jdbc:oracle:thin:@{host}:{port}/{database}"
    if engine in ("sqlserver-ee", "sqlserver-se", "sqlserver-ex", "sqlserver-web"):
        return f"jdbc:sqlserver://{host}:{port};databaseName={database};encrypt=true"

    # Defensive: SUPPORTED_ENGINES and JDBC_DRIVER_MAP must stay in sync with
    # this dispatcher. Reaching this point indicates a developer added a new
    # engine to the map but forgot to extend the URL builder.
    raise ValueError(
        f"build_jdbc_url has no URL pattern for engine {engine!r}; update the dispatcher to match JDBC_DRIVER_MAP"
    )


def validate_identifier(name: str) -> None:
    """Raise :class:`ValueError` if ``name`` is not a valid SQL identifier.

    The allowed shape is ``^[A-Za-z_][A-Za-z0-9_]*$`` — a letter or underscore
    followed by any number of letters, digits, or underscores. Empty strings
    and ``None``-ish values are rejected.

    This is applied to schema, table, and watermark column names. It is the
    same regex used by the wizard at pipeline-creation time, re-checked at job
    runtime as defence-in-depth (Req 3.5, 7.5).

    Args:
        name: Candidate identifier (schema, table, or column name).

    Raises:
        ValueError: If ``name`` does not match :data:`WATERMARK_COLUMN_RE`.
    """
    if not isinstance(name, str) or not WATERMARK_COLUMN_RE.match(name):
        raise ValueError(f"Invalid SQL identifier: {name!r}. Must match ^[A-Za-z_][A-Za-z0-9_]*$")


def validate_watermark_value(value: str, watermark_type: str) -> None:
    """Raise :class:`ValueError` if ``value`` is not valid for ``watermark_type``.

    Args:
        value: The watermark value as a string (e.g. ``"42"`` or
            ``"2024-12-15T08:42:31Z"``).
        watermark_type: Either ``"numeric"`` or ``"timestamp"``.

    Raises:
        ValueError: If ``watermark_type`` is unknown or ``value`` does not
            match the regex associated with the type.
    """
    if watermark_type == "numeric":
        if not isinstance(value, str) or not WATERMARK_NUMERIC_RE.match(value):
            raise ValueError(f"Invalid numeric watermark value: {value!r}. Must match ^-?\\d+(\\.\\d+)?$")
        return

    if watermark_type == "timestamp":
        if not isinstance(value, str) or not WATERMARK_TIMESTAMP_RE.match(value):
            raise ValueError(
                f"Invalid timestamp watermark value: {value!r}. "
                "Must be ISO 8601 (e.g. 2024-12-15 or 2024-12-15T08:42:31Z)"
            )
        return

    raise ValueError(f"Unknown watermark_type: {watermark_type!r}. Expected 'numeric' or 'timestamp'.")


#: The ``source_config`` key the first-run watermark lower bound is stored under.
#: It is what the wizard writes (``JdbcSourceForm``) and what the pipeline page
#: displays, alongside ``watermark_column`` and ``watermark_type``.
WATERMARK_INITIAL_VALUE_KEY = "watermark_initial_value"

#: Older spellings, still read so a record stored under one keeps its seed.
#: Until 2026-10-10 the value had three names: the wizard wrote
#: ``watermark_initial_value``, the API validated ``initial_watermark_value``,
#: and code generation read ``initial_watermark`` (the name the docs gave API
#: callers). Nothing translated between them, so a seed entered in the wizard
#: was never validated and never reached the job.
LEGACY_INITIAL_WATERMARK_KEYS: tuple[str, ...] = ("initial_watermark", "initial_watermark_value")


def initial_watermark_entry(source_config: dict[str, Any]) -> tuple[str, str] | None:
    """The first-run watermark lower bound as ``(key, value)``, or ``None``.

    ONE resolver for the API validator and for code generation, so the value
    that is validated is the value the job uses. The canonical key wins; the
    legacy keys are read in order after it. An empty or ``None`` value counts as
    absent, because the wizard sends ``""`` for "no seed". A non-string value is
    returned as its string form, which is what the validator and the rendered
    template both handle.
    """
    for key in (WATERMARK_INITIAL_VALUE_KEY, *LEGACY_INITIAL_WATERMARK_KEYS):
        value = source_config.get(key)
        if value is not None and value != "":
            return key, str(value)
    return None


# Engine families that quote identifiers with backticks rather than double
# quotes. Any engine outside this set uses the SQL-standard double quote.
_BACKTICK_ENGINES: frozenset[str] = frozenset({"mysql", "aurora-mysql", "mariadb"})


def quote_identifier(engine: str, name: str) -> str:
    """Return ``name`` quoted per the engine's identifier syntax.

    MySQL, MariaDB, and Aurora-MySQL use backticks (`` `name` ``). Postgres,
    Aurora-Postgres, Oracle, and SQL Server all accept the SQL-standard
    double-quoted form (``"name"``).

    The function calls :func:`validate_identifier` first as defence-in-depth —
    callers should already have validated, but this guarantees that no
    unvalidated string reaches the SQL output regardless of caller correctness.

    Args:
        engine: One of the keys in :data:`SUPPORTED_ENGINES`.
        name: A SQL identifier to quote.

    Returns:
        The quoted identifier ready for direct interpolation into SQL.

    Raises:
        ValueError: If ``name`` is not a valid identifier.
    """
    validate_identifier(name)
    if engine in _BACKTICK_ENGINES:
        return f"`{name}`"
    return f'"{name}"'


def build_watermark_predicate(
    engine: str,
    watermark_column: str,
    watermark_type: str,
    validated_value: str,
) -> str:
    """Return the engine-specific ``WHERE`` fragment for the watermark filter.

    The shape is ``<quoted_column> > <comparison>`` where ``<comparison>`` is:

    +---------------------------------+----------------------------------------+
    | Engine                          | timestamp comparison                   |
    +=================================+========================================+
    | postgres / aurora-postgresql    | ``'<value>'::timestamp``               |
    +---------------------------------+----------------------------------------+
    | mysql / aurora-mysql / mariadb  | ``'<value>'`` (implicit cast)          |
    +---------------------------------+----------------------------------------+
    | oracle-ee / oracle-se2          | ``TO_TIMESTAMP('<value>',              |
    |                                 |  'YYYY-MM-DD"T"HH24:MI:SS.FF"Z"')``    |
    +---------------------------------+----------------------------------------+
    | sqlserver-* (all editions)      | ``CONVERT(datetime2, '<value>', 127)`` |
    +---------------------------------+----------------------------------------+

    Numeric comparisons are uniform across engines: the value is interpolated
    raw (no quoting, no cast), since :data:`WATERMARK_NUMERIC_RE` guarantees
    the string contains only ``-``, digits, and an optional decimal point.

    The function calls :func:`validate_identifier` and
    :func:`validate_watermark_value` first as defence-in-depth — callers should
    already have validated, but this guarantees no unvalidated string reaches
    the SQL output.

    Args:
        engine: One of the keys in :data:`SUPPORTED_ENGINES`.
        watermark_column: The column to filter on.
        watermark_type: Either ``"numeric"`` or ``"timestamp"``.
        validated_value: The watermark value (must match the type's regex).

    Returns:
        A ``WHERE``-fragment ready for direct interpolation into SQL, e.g.
        ``"\"updated_at\" > '2024-12-15T08:42:31Z'::timestamp"``.

    Raises:
        ValueError: If ``engine`` is unknown, the column name is invalid, or
            the value does not match the type's regex.
    """
    validate_identifier(watermark_column)
    validate_watermark_value(validated_value, watermark_type)
    quoted_col = quote_identifier(engine, watermark_column)

    if watermark_type == "numeric":
        # Numeric values are emitted raw — no quotes, no cast. The numeric
        # regex guarantees no SQL-special characters are present.
        return f"{quoted_col} > {validated_value}"

    # watermark_type == "timestamp" — already validated by
    # validate_watermark_value above.
    if engine in ("postgres", "aurora-postgresql"):
        return f"{quoted_col} > '{validated_value}'::timestamp"
    if engine in ("mysql", "aurora-mysql", "mariadb"):
        return f"{quoted_col} > '{validated_value}'"
    if engine in ("oracle-ee", "oracle-se2"):
        return f"{quoted_col} > TO_TIMESTAMP('{validated_value}', 'YYYY-MM-DD\"T\"HH24:MI:SS.FF\"Z\"')"
    if engine in ("sqlserver-ee", "sqlserver-se", "sqlserver-ex", "sqlserver-web"):
        return f"{quoted_col} > CONVERT(datetime2, '{validated_value}', 127)"

    raise ValueError(f"Unsupported JDBC engine: {engine!r}. Supported engines: {SUPPORTED_ENGINES}")


# ---------------------------------------------------------------------------
# Canonical Glue Connection create-or-update
# ---------------------------------------------------------------------------
#
# ``create_glue_jdbc_connection`` is the single source of truth for
# provisioning Glue JDBC Connections in AgenticDataIngestion. It replaces two earlier
# implementations (Task 5.5 in the P17 plan):
#
#   * ``create_glue_pipeline.create_jdbc_connection`` — set
#     ``PhysicalConnectionRequirements`` correctly but is otherwise unused.
#   * ``handlers/pipelines._auto_create_rds_glue_connection`` — used by P14
#     but did NOT set ``PhysicalConnectionRequirements``, which is the root
#     cause of the P14 connectivity gap (Glue runs the job in its own VPC
#     instead of the customer's RDS VPC).
#
# Glue Connections do NOT carry an execution role — the role is attached to
# the Glue Job / Crawler that USES the connection. Hence this function
# deliberately accepts no role argument.
#
# Design notes (cross-references to design.md ``Functions`` block):
#   * Idempotent — ``get_connection`` is probed first; a hit becomes
#     ``update_connection``, an ``EntityNotFoundException`` becomes
#     ``create_connection``.
#   * ``PhysicalConnectionRequirements`` is only emitted when the caller
#     supplies a dict containing at least ``SubnetId`` and
#     ``SecurityGroupIdList``. ``AvailabilityZone`` is forwarded as-is when
#     present (Glue accepts it as optional).
#   * ``Tags`` is omitted entirely when ``None`` — some Glue API versions
#     reject an empty ``Tags={}`` payload.
#
# The boto3 client is lazily created via ``_glue_client()`` so that
# importing this module at unit-test time does not trigger a real boto3
# session / network lookup. Tests can monkey-patch ``_glue_client`` to
# return a fake.

from typing import Any

_glue_client_cache: Any = None
_sts_client_cache: Any = None
_account_id_cache: str | None = None


def _glue_client() -> Any:
    """Return a process-wide cached boto3 Glue client.

    Using a lazy module-level cache (rather than a top-level
    ``boto3.client("glue")`` call) keeps unit tests fast and import-safe:
    nothing reaches the AWS SDK until the first real call. Tests can
    monkey-patch this helper to inject a stub client.

    Returns:
        A boto3 Glue client. Typed as ``Any`` because ``boto3-stubs``
        does not give us a clean module-level handle here without
        importing the whole client class hierarchy.
    """
    global _glue_client_cache
    if _glue_client_cache is None:
        import boto3

        _glue_client_cache = boto3.client("glue")
    return _glue_client_cache


def _sts_client() -> Any:
    """Return a process-wide cached boto3 STS client.

    Lazy and patchable for the same reasons ``_glue_client`` is, stated in the
    module comment above: importing this module must not reach the AWS SDK, and
    a test must be able to inject a fake.

    Returns:
        A boto3 STS client, typed ``Any`` for the reason given on
        ``_glue_client``.
    """
    global _sts_client_cache
    if _sts_client_cache is None:
        import boto3

        _sts_client_cache = boto3.client("sts")
    return _sts_client_cache


def _account_id() -> str:
    """Return the caller's AWS account id, resolved once per process.

    Cached because it cannot change within a process — a Lambda container holds
    one execution role in one account — so without the cache every tagged
    connection would cost an extra STS round trip.
    """
    global _account_id_cache
    if _account_id_cache is None:
        _account_id_cache = str(_sts_client().get_caller_identity()["Account"])
    return _account_id_cache


def _build_glue_connection_arn(connection_name: str) -> str:
    """Build a Glue Connection ARN for tagging operations.

    Format: arn:aws:glue:{region}:{account}:connection/{name}

    The account id comes from ``_account_id()`` rather than a ``boto3`` client
    built here. A client built inline sat behind no seam, so it could not be
    patched the way ``_glue_client`` can, and it made ``create_glue_jdbc_connection``
    reach STS for real. Two ``tests/cdk/`` tests consequently required live AWS
    credentials and failed on any machine without them, which is the opposite of
    what the module comment above promises.
    """
    import os

    region = os.environ.get("AWS_REGION", "us-east-1")
    return f"arn:aws:glue:{region}:{_account_id()}:connection/{connection_name}"


#: Start of the ``Description`` that :func:`create_glue_jdbc_connection` writes on
#: every connection it creates or updates. It is the only provenance marker that
#: can be read back from ``GetConnections``: Glue rejects ``GetTags`` on a
#: connection ARN (``LGR-cb3fa38-1``), so the ``Type`` and ``PipelineId`` tags
#: the callers pass are write-only. The provisioner builds its Description from
#: this constant, so the writer and :func:`is_managed_glue_connection` cannot
#: drift apart.
MANAGED_CONNECTION_DESCRIPTION_PREFIX = "AgenticDataIngestion JDBC connection ("

#: The names the provisioner's callers mint, as a backstop for a connection whose
#: Description has been edited by hand:
#:
#: * ``agentic-data-ingestion-{pipeline_id}-source`` and ``-rds``, the per-pipeline
#:   source and target connections (``handlers/pipelines_jdbc.py``). A pipeline id
#:   is ``{slug}-{8 hex}`` (``create_pipeline``), so the 8-hex segment is required.
#:   That keeps a name such as ``agentic-data-ingestion-test-fixtures-mysql-source``,
#:   which a user is meant to pick, out of the pattern.
#: * ``agentic-data-ingestion-discover-{12 hex}``, the wizard's Detect Schema
#:   connection (``handlers/jdbc_resources.py``).
_MANAGED_CONNECTION_NAME_RE = re.compile(
    r"\Aagentic-data-ingestion-(?:discover-[0-9a-f]{12}|.+-[0-9a-f]{8}-(?:source|rds))\Z"
)


def is_managed_glue_connection(name: str, description: str | None = None) -> bool:
    """True when a Glue connection is one this system created for its own use.

    Such a connection belongs to one pipeline or to one schema detection, and its
    lifecycle is tied to that owner, not to anyone who later references it.
    ``delete_pipeline`` deletes a pipeline's ``-source`` connection, and the daily
    discovery janitor deletes a ``discover-`` connection seven days after its
    crawler was created. A second pipeline that picked one in "Use Glue Connection"
    mode would break when the owner goes, and nothing would warn it. So the
    connection picker leaves these out (``handlers/rds_resources.py``).

    Args:
        name: The connection's ``Name``.
        description: The connection's ``Description``, when the caller has it.
            ``GetConnections`` returns it, so the listing passes it.

    Returns:
        ``True`` if the Description carries
        :data:`MANAGED_CONNECTION_DESCRIPTION_PREFIX` or the name matches a name
        this system mints.
    """
    if description and description.startswith(MANAGED_CONNECTION_DESCRIPTION_PREFIX):
        return True
    return _MANAGED_CONNECTION_NAME_RE.match(name or "") is not None


def create_glue_jdbc_connection(
    connection_name: str,
    jdbc_url: str,
    jdbc_driver: str,
    secret_arn: str | None,
    physical_requirements: dict[str, Any] | None,
    tags: dict[str, str] | None = None,
) -> str:
    """Idempotently create or update a Glue JDBC Connection.

    This is the canonical Glue Connection provisioning function for
    AgenticDataIngestion. It replaces ``create_jdbc_connection`` in
    ``create_glue_pipeline`` and ``_auto_create_rds_glue_connection`` in
    ``dashboard_api/handlers/pipelines.py`` (Tasks 5.5 / 14.5 in the P17
    plan). Setting ``PhysicalConnectionRequirements`` when supplied closes
    the P14 gap that left RDS-target pipelines unable to reach the
    customer's private VPC.

    Behaviour:
        * Calls ``glue:GetConnection(Name=connection_name)``. If a
          connection exists, ``glue:UpdateConnection`` is called; if
          ``EntityNotFoundException`` is raised, ``glue:CreateConnection``
          is called instead.
        * ``ConnectionInput.ConnectionType`` is always ``"JDBC"``.
        * ``ConnectionProperties`` always includes ``JDBC_CONNECTION_URL``
          and ``JDBC_ENFORCE_SSL="true"``. ``JDBC_DRIVER_CLASS_NAME`` is
          intentionally omitted for built-in engines (Glue auto-detects
          the driver from the URL prefix). When ``secret_arn`` is
          provided, ``SECRET_ID`` is set — Glue REQUIRES credentials on a
          JDBC connection, so this is mandatory, not optional.
        * ``PhysicalConnectionRequirements`` is included only when
          ``physical_requirements`` is not ``None`` and contains both
          ``SubnetId`` and ``SecurityGroupIdList``. Any other keys
          (notably ``AvailabilityZone``) are forwarded verbatim.
        * ``Tags`` is forwarded directly when supplied; ``None`` (the
          default) omits the parameter entirely — passing ``Tags={}`` is
          rejected by some Glue API versions.

    Args:
        connection_name: Name of the Glue Connection (e.g.
            ``"agentic-data-ingestion-{pipeline_id}-source"``). Doubles as the AWS
            resource identifier.
        jdbc_url: Fully-qualified JDBC URL (use :func:`build_jdbc_url`).
        jdbc_driver: Fully-qualified driver class name (use
            :func:`get_jdbc_driver`).
        secret_arn: Optional Secrets Manager ARN containing the
            ``{username, password}`` JSON. Pass ``None`` only when the
            connection is for crawler-only discovery and credentials
            come from elsewhere.
        physical_requirements: Optional dict with ``SubnetId`` (str),
            ``SecurityGroupIdList`` (list[str]), and optional
            ``AvailabilityZone`` (str). When ``None`` or missing the
            required keys, the connection is created without VPC
            attachment — only valid for publicly-routable JDBC
            endpoints.
        tags: Optional ``{key: value}`` dict to attach to the
            connection. ``None`` (the default) sends no ``Tags``
            parameter at all.

    Returns:
        The ``connection_name`` argument, unchanged. Returned for
        symmetry with the legacy callers and to make the call site read
        as ``conn = create_glue_jdbc_connection(...)``.

    Raises:
        botocore.exceptions.ClientError: On any unrecoverable Glue API
            error other than ``EntityNotFoundException`` (which is
            handled internally as the create-vs-update branch).
    """
    import logging

    from botocore.exceptions import ClientError

    logger = logging.getLogger(__name__)

    glue_client = _glue_client()

    # JDBC_DRIVER_CLASS_NAME is intentionally NOT set on the connection
    # for built-in engines (MySQL, Postgres, MariaDB, Oracle, SQL Server,
    # Aurora). Glue auto-detects the driver class from the JDBC URL's
    # protocol prefix (jdbc:mysql:, jdbc:postgresql:, ...). When
    # JDBC_DRIVER_CLASS_NAME IS set on a connection, Glue treats it as
    # a custom-driver hint and demands JDBC_DRIVER_JAR_URI alongside —
    # which we don't provide for built-in engines. The driver class is
    # still passed at runtime via Glue job arguments and Spark JDBC
    # `properties.driver`, so generated code uses the correct driver.
    connection_properties: dict[str, str] = {
        "JDBC_CONNECTION_URL": jdbc_url,
        "JDBC_ENFORCE_SSL": "true",
    }
    # AWS Glue REQUIRES credentials on a JDBC connection. A connection
    # carrying only JDBC_CONNECTION_URL is rejected by both
    # CreateConnection and UpdateConnection with the (unhelpfully generic)
    # error "Validation for connection properties failed". SECRET_ID is
    # the documented, supported property for attaching Secrets Manager
    # credentials, so it MUST live in ConnectionProperties.
    #
    # (A previous change moved SECRET_ID out to a tag on the mistaken
    # belief that Glue had started rejecting it; verified against the live
    # Glue API on 2026-06-22 that the opposite is true — URL-only fails,
    # URL+SECRET_ID succeeds. That change broke ALL JDBC connection
    # provisioning: wizard schema discovery and source/target creation.)
    #
    # We ALSO mirror the secret ARN into the adi:secret_arn tag so the
    # read path can resolve credentials without depending on the property
    # name. That read path is PLURAL — not one function, so do not go
    # looking for a single resolver: ``_augment_glue_connection_config``
    # (``handlers/pipelines_jdbc.py:247``) reads
    # ``tags.get("adi:secret_arn") or props.get("SECRET_ID")`` at ``:333``,
    # and ``create_glue_pipeline/job_arguments.py:149`` does the identical
    # read for the job's runtime arguments.
    #
    # Both copies are load-bearing — ``LGR-819980f-2``: SECRET_ID in
    # ConnectionProperties is what Glue consults at job runtime, the tag is
    # a read-path mirror, and removing either broke all JDBC provisioning
    # once already. Do NOT "clean up" the duplication.
    connection_tags = dict(tags) if tags else {}
    if secret_arn:
        connection_properties["SECRET_ID"] = secret_arn
        connection_tags["adi:secret_arn"] = secret_arn

    connection_input: dict[str, Any] = {
        "Name": connection_name,
        # Read back by `is_managed_glue_connection`; built from the same constant.
        "Description": f"{MANAGED_CONNECTION_DESCRIPTION_PREFIX}{connection_name})",
        "ConnectionType": "JDBC",
        "ConnectionProperties": connection_properties,
    }

    # Only include PhysicalConnectionRequirements when the caller supplies
    # both required keys. Glue rejects partial physical-requirements
    # payloads, so a dict missing SubnetId or SecurityGroupIdList is
    # treated as "no physical requirements" rather than passed through
    # and erroring at API time.
    if (
        physical_requirements is not None
        and "SubnetId" in physical_requirements
        and "SecurityGroupIdList" in physical_requirements
    ):
        connection_input["PhysicalConnectionRequirements"] = physical_requirements

    # Probe for existence first. EntityNotFoundException → create branch.
    # Any other ClientError propagates.
    try:
        glue_client.get_connection(Name=connection_name)
        exists = True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "")
        if error_code == "EntityNotFoundException":
            exists = False
        else:
            logger.error("Glue get_connection failed for %s: %s", connection_name, exc)
            raise

    try:
        if exists:
            glue_client.update_connection(
                Name=connection_name,
                ConnectionInput=connection_input,
            )
            logger.info("Updated Glue JDBC connection: %s", connection_name)
            # Update tags separately (UpdateConnection doesn't accept Tags)
            if connection_tags:
                try:
                    conn_arn = _build_glue_connection_arn(connection_name)
                    glue_client.tag_resource(ResourceArn=conn_arn, TagsToAdd=connection_tags)
                except ClientError as tag_exc:
                    logger.warning(
                        "Failed to update tags on connection %s: %s",
                        connection_name,
                        tag_exc,
                    )
        else:
            create_kwargs: dict[str, Any] = {"ConnectionInput": connection_input}
            if connection_tags:
                create_kwargs["Tags"] = connection_tags
            glue_client.create_connection(**create_kwargs)
            logger.info("Created Glue JDBC connection: %s", connection_name)
    except ClientError as exc:
        logger.error(
            "Glue %s_connection failed for %s: %s",
            "update" if exists else "create",
            connection_name,
            exc,
        )
        raise

    return connection_name
