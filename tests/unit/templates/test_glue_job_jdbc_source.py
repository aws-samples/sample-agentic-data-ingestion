# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for ``glue_job_jdbc_source.py.j2`` (P17 Task 10.5).

These tests verify the new JDBC-source PySpark template renders to syntactically
valid Python across the full ``(engine × target × read_mode)`` matrix, and that
the rendered output preserves the structural invariants the runtime depends on:

* Identifier quoting goes through ``quote_identifier(engine, …)`` with ``engine``
  baked at render time. The actual backtick-vs-double-quote dispatch lives in
  ``jdbc_helpers.py`` and is exercised in :mod:`tests.unit.shared.test_jdbc_helpers`.
* The watermark predicate goes through ``build_watermark_predicate(engine, …)``
  with ``engine`` baked at render time. The per-engine ``::timestamp`` /
  ``TO_TIMESTAMP`` / ``CONVERT(datetime2,…)`` fragments are exercised there too.
* Incremental mode produces ``df.cache() → MAX(df) → transform → target write
  → conditional PutItem → df.unpersist()`` in that order (Req 7.6).
* Full-table mode emits NO watermark machinery at all — no ``df.cache()``,
  no ``spark_max(col(args['watermark_column']))``, no ``_watermarks_table``,
  no ``df.unpersist()``.
* Identifier and watermark validation calls (``validate_identifier`` and
  ``validate_watermark_value``) appear in the rendered output as defence-in-depth
  per Req 7.5.

Validates: Requirements 22.1, 7.5, 7.6, 8.1-8.10
"""

from __future__ import annotations

import ast
import os
import textwrap
from typing import Any

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

# Sample transform body — same shape used across the existing template tests.
SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

# Engine families. Per the task spec, each family rolls up multiple
# ``SUPPORTED_ENGINES`` entries — we cover at least one engine per family
# plus the family-specific Aurora / edition variants where they affect
# downstream codegen at the ``jdbc_helpers`` layer.
ENGINES_BY_FAMILY: dict[str, list[str]] = {
    "postgres": ["postgres", "aurora-postgresql"],
    "mysql": ["mysql", "aurora-mysql"],
    "mariadb": ["mariadb"],
    "oracle": ["oracle-ee", "oracle-se2"],
    "sqlserver": ["sqlserver-ee", "sqlserver-se", "sqlserver-ex", "sqlserver-web"],
}
ALL_ENGINES: list[str] = [e for engines in ENGINES_BY_FAMILY.values() for e in engines]

# Engines whose runtime ``quote_identifier`` returns backticks rather than
# double quotes. The choice is made at runtime by ``jdbc_helpers``; this
# mapping is used here only to assert correct family classification.
BACKTICK_ENGINES: set[str] = {"mysql", "aurora-mysql", "mariadb"}

TARGET_TYPES: list[str] = [
    "S3_TABLES",
    "GLUE_CATALOG",
    "JDBC",
    "RDS_DATABASE",
    "S3_PARQUET",
]


def _render_kwargs(target_type: str) -> dict[str, object]:
    """Target-specific render-time variables.

    The S3_TABLES partial reads ``partition_columns`` and ``merge_key``;
    the S3_PARQUET / GLUE_CATALOG partials read ``partition_columns``;
    the JDBC partial reads neither. We supply benign defaults across the
    board so the parametrised matrix renders uniformly.
    """
    return {
        "partition_columns": "",
        "merge_key": "id",
    }


@pytest.fixture
def jinja_env() -> jinja2.Environment:
    """Jinja2 environment with ``StrictUndefined`` so missing variables surface."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


@pytest.fixture
def template(jinja_env: jinja2.Environment) -> jinja2.Template:
    return jinja_env.get_template("glue_job_jdbc_source.py.j2")


def _render(
    template: jinja2.Template,
    *,
    engine: str = "postgres",
    target_type: str = "S3_TABLES",
    read_mode: str = "full_table",
    write_mode: str = "overwrite",
    watermark_type: str | None = None,
    initial_watermark: str = "",
    partition_columns: str = "",
    merge_key: str = "id",
) -> str:
    """Helper — render the template with the canonical variable surface."""
    return template.render(
        engine=engine,
        target_type=target_type,
        read_mode=read_mode,
        write_mode=write_mode,
        partition_columns=partition_columns,
        merge_key=merge_key,
        initial_watermark=initial_watermark,
        transform_block=SAMPLE_TRANSFORM,
    )


# ---------------------------------------------------------------------------
# Matrix coverage — full-table mode against every target type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_type", TARGET_TYPES)
def test_full_table_renders_valid_python_for_each_target(template: jinja2.Template, target_type: str) -> None:
    """Full-table mode must render valid Python for every target type."""
    rendered = _render(template, target_type=target_type, read_mode="full_table")
    ast.parse(rendered)


# ---------------------------------------------------------------------------
# Matrix coverage — incremental mode with both watermark types
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_type", TARGET_TYPES)
@pytest.mark.parametrize(
    "watermark_type,initial_watermark",
    [
        ("numeric", "0"),
        ("timestamp", "2024-01-01T00:00:00Z"),
    ],
)
def test_incremental_renders_valid_python_for_each_target_and_watermark_type(
    template: jinja2.Template,
    target_type: str,
    watermark_type: str,
    initial_watermark: str,
) -> None:
    """Incremental mode must render valid Python for every (target, watermark_type) pair."""
    rendered = _render(
        template,
        target_type=target_type,
        read_mode="incremental",
        watermark_type=watermark_type,
        initial_watermark=initial_watermark,
        write_mode="append",
    )
    ast.parse(rendered)


# ---------------------------------------------------------------------------
# Matrix coverage — every engine family, both modes, default S3_TABLES target
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
@pytest.mark.parametrize("read_mode", ["full_table", "incremental"])
def test_every_engine_renders_valid_python(template: jinja2.Template, engine: str, read_mode: str) -> None:
    """Every supported engine must render valid Python in both modes."""
    rendered = _render(
        template,
        engine=engine,
        target_type="S3_TABLES",
        read_mode=read_mode,
        watermark_type="timestamp" if read_mode == "incremental" else None,
        initial_watermark="2024-01-01T00:00:00Z" if read_mode == "incremental" else "",
        write_mode="append",
    )
    ast.parse(rendered)


# ---------------------------------------------------------------------------
# Engine-string baking — quote_identifier and build_watermark_predicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("engine", ALL_ENGINES)
def test_quote_identifier_engine_baked_into_render(template: jinja2.Template, engine: str) -> None:
    """``quote_identifier(engine, …)`` must include the engine string verbatim.

    The actual choice of backticks vs double quotes happens at runtime in
    ``jdbc_helpers.py`` based on this string — that path is unit-tested
    separately. Here we only assert the engine string is correctly threaded
    through.
    """
    rendered = _render(template, engine=engine, target_type="S3_TABLES")
    assert f"quote_identifier(\"{engine}\", args['jdbc_schema'])" in rendered
    assert f"quote_identifier(\"{engine}\", args['jdbc_table'])" in rendered


@pytest.mark.parametrize("engine", ALL_ENGINES)
@pytest.mark.parametrize("watermark_type", ["numeric", "timestamp"])
def test_build_watermark_predicate_engine_baked_into_render(
    template: jinja2.Template, engine: str, watermark_type: str
) -> None:
    """``build_watermark_predicate("<engine>", …)`` must appear in incremental mode.

    The per-engine timestamp comparison fragment (``::timestamp`` for Postgres,
    ``TO_TIMESTAMP(…)`` for Oracle, ``CONVERT(datetime2,…)`` for SQL Server,
    plain string literal for MySQL/MariaDB, raw value for numeric) is selected
    at runtime by the helper; here we only assert the helper is invoked with
    the correct engine string.
    """
    initial_watermark = "0" if watermark_type == "numeric" else "2024-01-01T00:00:00Z"
    rendered = _render(
        template,
        engine=engine,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type=watermark_type,
        initial_watermark=initial_watermark,
        write_mode="append",
    )
    assert f'build_watermark_predicate(\n            "{engine}"' in rendered


@pytest.mark.parametrize(
    "engine,family",
    [
        ("postgres", "postgres"),
        ("aurora-postgresql", "postgres"),
        ("mysql", "mysql"),
        ("aurora-mysql", "mysql"),
        ("mariadb", "mariadb"),
        ("oracle-ee", "oracle"),
        ("oracle-se2", "oracle"),
        ("sqlserver-ee", "sqlserver"),
        ("sqlserver-se", "sqlserver"),
        ("sqlserver-ex", "sqlserver"),
        ("sqlserver-web", "sqlserver"),
    ],
)
def test_engine_classified_into_correct_family(engine: str, family: str) -> None:
    """Family classification sanity check used by other tests in this module.

    Backtick engines (MySQL family) are a strict subset; the rest receive
    double-quote treatment in ``jdbc_helpers.quote_identifier``. This test
    pins that classification so future engine additions are forced to make a
    deliberate choice in the assertion table above.
    """
    if family in {"mysql", "mariadb"}:
        assert engine in BACKTICK_ENGINES
    else:
        assert engine not in BACKTICK_ENGINES


# ---------------------------------------------------------------------------
# Incremental ordering invariants (Req 7.6)
# ---------------------------------------------------------------------------


def test_incremental_cache_precedes_max(template: jinja2.Template) -> None:
    """Req 7.6: ``df.cache()`` must appear before the MAX computation.

    Without a cache, Spark's lazy evaluation re-reads the JDBC source for
    each action — once for ``MAX``, again for the transform — doubling load
    on the customer's database.
    """
    rendered = _render(
        template,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    cache_idx = rendered.index("df = df.cache()")
    max_idx = rendered.index("spark_max(col(args['watermark_column']))")
    assert cache_idx < max_idx, "df.cache() must precede MAX computation"


def test_incremental_max_computed_on_df_not_df_transformed(
    template: jinja2.Template,
) -> None:
    """Req 7.6: MAX is computed from ``df``, not ``df_transformed``.

    Transforms may filter rows or rename columns; computing MAX from the
    source DataFrame ensures the next run picks up exactly where this one
    left off.
    """
    rendered = _render(
        template,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    # The MAX-aggregation call site reads from `df`, the source DataFrame.
    assert "df.agg(spark_max(col(args['watermark_column']))" in rendered
    # And NOT from `df_transformed`.
    assert "df_transformed.agg(spark_max" not in rendered


def test_incremental_putitem_after_target_write(template: jinja2.Template) -> None:
    """Req 7.6: PutItem appears AFTER the target write block.

    If the target write fails, the watermark must NOT advance — otherwise
    rows would be silently skipped on the next run.
    """
    rendered = _render(
        template,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    transform_end_idx = rendered.index("=== End Transform Block ===")
    putitem_idx = rendered.index("_watermarks_table.put_item(")
    assert transform_end_idx < putitem_idx, (
        "PutItem must appear after the AI transform block (and therefore after the target write that follows it)"
    )


def test_incremental_unpersist_after_putitem(template: jinja2.Template) -> None:
    """Req 7.6: ``df.unpersist()`` must appear AFTER PutItem.

    The early ``df.unpersist()`` in the empty-source guard branch is a
    different occurrence; we assert ordering against the LAST occurrence,
    which is the post-write cleanup.
    """
    rendered = _render(
        template,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    putitem_idx = rendered.index("_watermarks_table.put_item(")
    last_unpersist_idx = rendered.rindex("df.unpersist()")
    assert putitem_idx < last_unpersist_idx, (
        "df.unpersist() must appear after PutItem (release the cache only "
        "after the watermark has been durably recorded)"
    )


def test_incremental_full_ordering_chain(template: jinja2.Template) -> None:
    """Combined ordering check matching the smoke-script invariants.

    cache() < MAX < transform_block < put_item < df.unpersist() (last)
    """
    rendered = _render(
        template,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    cache_idx = rendered.index("df = df.cache()")
    max_idx = rendered.index("spark_max(col(args['watermark_column']))")
    transform_idx = rendered.index("=== AI-Generated Transform Block ===")
    putitem_idx = rendered.index("_watermarks_table.put_item(")
    unpersist_idx = rendered.rindex("df.unpersist()")
    assert cache_idx < max_idx < transform_idx < putitem_idx < unpersist_idx


# ---------------------------------------------------------------------------
# Full-table mode — NO watermark machinery
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_type", TARGET_TYPES)
def test_full_table_omits_watermark_block_entirely(template: jinja2.Template, target_type: str) -> None:
    """Full-table mode must NOT emit any watermark code path.

    Specifically: no ``df.cache()``, no MAX-of-watermark-column, no
    ``_watermarks_table`` references, no ``df.unpersist()``. The
    ``spark_max`` import remains (it's at module top), so we anchor the
    negative assertion on the watermark-specific call site.

    AMENDED, not deleted, by p37 task 3.3 (`LGR-4c4b4c9-6` /
    `LGR-60c4985-6`, template / ratified, supply the amend-in-place
    convention). The last assertion read ``"put_item(" not in rendered``.
    That was a valid *proxy* for the watermark PutItem only while the
    watermark write was the sole ``put_item`` this template could render.
    p37 C1 adds a second, unrelated one — the best-effort
    ``(run_id, "load_metrics")`` counts row from
    ``_partials/_load_counts.py.j2``, which is emitted in BOTH read modes by
    design (R1.3's ``empty_source`` is otherwise unrecordable). The subject
    of this test is the watermark machinery, so the assertion is narrowed to
    the watermark-specific call site — the same anchor this file already uses
    at the three ordering tests above and ``scripts/_render_jdbc_template_check.py:91``.
    Nothing about full-table mode's watermark exclusion is relaxed:
    ``_watermarks_table`` is still asserted absent two lines up, which is
    strictly stronger than the old substring test for this template.
    """
    rendered = _render(template, target_type=target_type, read_mode="full_table")
    assert "df.cache()" not in rendered
    assert "df = df.cache()" not in rendered
    assert "spark_max(col(args['watermark_column']))" not in rendered
    assert "_watermarks_table" not in rendered
    assert "df.unpersist()" not in rendered
    # Conditional PutItem belongs only to incremental mode.
    assert "ConditionExpression=" not in rendered
    assert "_watermarks_table.put_item(" not in rendered


@pytest.mark.parametrize("target_type", TARGET_TYPES)
def test_full_table_omits_watermark_specific_args(template: jinja2.Template, target_type: str) -> None:
    """``getResolvedOptions`` must NOT declare watermark args in full-table mode (Req 8.3)."""
    rendered = _render(template, target_type=target_type, read_mode="full_table")
    assert "'watermark_column'" not in rendered
    assert "'watermark_type'" not in rendered
    assert "'watermarks_table_name'" not in rendered


@pytest.mark.parametrize("target_type", TARGET_TYPES)
def test_incremental_declares_watermark_specific_args(template: jinja2.Template, target_type: str) -> None:
    """``getResolvedOptions`` MUST declare watermark args in incremental mode (Req 8.3)."""
    rendered = _render(
        template,
        target_type=target_type,
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    assert "'watermark_column'" in rendered
    assert "'watermark_type'" in rendered
    assert "'watermarks_table_name'" in rendered


# ---------------------------------------------------------------------------
# Validation calls appear in the rendered output (Req 7.5 defence-in-depth)
# ---------------------------------------------------------------------------


def test_identifier_validation_calls_present_in_output(
    template: jinja2.Template,
) -> None:
    """Req 7.5: ``validate_identifier(args['jdbc_schema'])`` and ``…['jdbc_table']``
    must appear in EVERY rendered output as runtime defence-in-depth.

    Spark's JDBC ``query`` option does not support bind parameters; the
    template interpolates identifiers directly into SQL, so re-validating at
    job runtime is mandatory even though the dashboard API also validates
    at wizard time.
    """
    rendered = _render(template, target_type="S3_TABLES", read_mode="full_table")
    assert "validate_identifier(args['jdbc_schema'])" in rendered
    assert "validate_identifier(args['jdbc_table'])" in rendered


def test_identifier_validation_imported_from_jdbc_helpers(
    template: jinja2.Template,
) -> None:
    """The ``jdbc_helpers`` import surface must include all four validators."""
    rendered = _render(template, target_type="S3_TABLES", read_mode="full_table")
    assert "from jdbc_helpers import" in rendered
    assert "validate_identifier" in rendered
    assert "validate_watermark_value" in rendered
    assert "quote_identifier" in rendered
    assert "build_watermark_predicate" in rendered


def test_watermark_validation_call_present_in_incremental_mode(
    template: jinja2.Template,
) -> None:
    """Req 7.5: ``validate_watermark_value(...)`` must appear in incremental mode."""
    rendered = _render(
        template,
        target_type="S3_TABLES",
        read_mode="incremental",
        watermark_type="timestamp",
        initial_watermark="2024-01-01T00:00:00Z",
        write_mode="append",
    )
    assert "validate_watermark_value(prev_value, args['watermark_type'])" in rendered
    # And the watermark column itself is validated as an identifier.
    assert "validate_identifier(args['watermark_column'])" in rendered


def test_watermark_validation_omitted_in_full_table_mode(
    template: jinja2.Template,
) -> None:
    """Full-table mode must NOT CALL ``validate_watermark_value``.

    Note: ``validate_watermark_value`` still appears in the module-top
    ``from jdbc_helpers import (...)`` statement (the import surface is
    static so the script's bytecode is identical across modes). We assert
    that the *call site* — ``validate_watermark_value(prev_value, ...)`` —
    is absent, and that ``args['watermark_column']`` is never validated
    since the key isn't even declared in ``getResolvedOptions`` for
    full-table mode.
    """
    rendered = _render(template, target_type="S3_TABLES", read_mode="full_table")
    assert "validate_watermark_value(" not in rendered
    assert "validate_identifier(args['watermark_column'])" not in rendered


# ---------------------------------------------------------------------------
# StrictUndefined contract — missing required vars must raise UndefinedError
# ---------------------------------------------------------------------------


def test_missing_required_variable_raises_under_strict_undefined(
    jinja_env: jinja2.Environment,
) -> None:
    """``StrictUndefined`` must surface missing render-time variables as errors.

    This is a meta-check: the test would silently pass with default ``Undefined``
    (which renders missing vars as the empty string). We render with the
    required ``transform_block`` deliberately omitted to exercise the strict
    behaviour — this is what catches typos in render-side variable names
    before they ship as silent bugs in production code.
    """
    tpl = jinja_env.get_template("glue_job_jdbc_source.py.j2")
    with pytest.raises(jinja2.UndefinedError):
        tpl.render(
            engine="postgres",
            target_type="S3_TABLES",
            read_mode="full_table",
            write_mode="overwrite",
            partition_columns="",
            merge_key="id",
            initial_watermark="",
            # transform_block deliberately omitted
        )


# ---------------------------------------------------------------------------
# Source-read invariants (apply to every render)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("target_type", TARGET_TYPES)
@pytest.mark.parametrize("read_mode", ["full_table", "incremental"])
def test_source_read_uses_jdbc_format(template: jinja2.Template, target_type: str, read_mode: str) -> None:
    """Every render must read via ``spark.read.format("jdbc")``."""
    rendered = _render(
        template,
        target_type=target_type,
        read_mode=read_mode,
        watermark_type="timestamp" if read_mode == "incremental" else None,
        initial_watermark="2024-01-01T00:00:00Z" if read_mode == "incremental" else "",
        write_mode="append",
    )
    assert 'spark.read.format("jdbc")' in rendered


@pytest.mark.parametrize("target_type", TARGET_TYPES)
def test_credentials_fetched_from_secrets_manager_at_runtime(template: jinja2.Template, target_type: str) -> None:
    """Credentials must be fetched at runtime from Secrets Manager.

    They must NEVER be baked into the rendered template — that would log
    them into CloudFormation, the Glue script bucket, and CloudTrail.
    """
    rendered = _render(template, target_type=target_type, read_mode="full_table")
    assert 'boto3.client("secretsmanager").get_secret_value' in rendered


# ---------------------------------------------------------------------------
# Per-target dispatch — write-partial sentinel substring
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_type,sentinel",
    [
        ("S3_TABLES", "writeTo(target_table)"),
        ("GLUE_CATALOG", "glue_context.write_dynamic_frame.from_catalog"),
        ("JDBC", "df_transformed.write.jdbc("),
        ("RDS_DATABASE", "df_transformed.write.jdbc("),
        ("S3_PARQUET", ".parquet(output_path)"),
    ],
)
def test_target_dispatch_emits_correct_partial(template: jinja2.Template, target_type: str, sentinel: str) -> None:
    """Each ``target_type`` must dispatch to the matching write partial."""
    rendered = _render(template, target_type=target_type, read_mode="full_table")
    assert sentinel in rendered


@pytest.mark.parametrize("target_type", ["JDBC", "RDS_DATABASE"])
def test_jdbc_target_uses_target_prefixed_args(template: jinja2.Template, target_type: str) -> None:
    """JDBC and RDS_DATABASE targets must read from ``args['target_jdbc_url']``.

    The ``target_`` key prefix prevents collision with the SOURCE-side
    ``args['jdbc_url']`` (Req 11.6).
    """
    rendered = _render(template, target_type=target_type, read_mode="full_table")
    assert "args['target_jdbc_url']" in rendered


# ---------------------------------------------------------------------------
# The incremental read, executed (2026-10-10)
# ---------------------------------------------------------------------------
#
# Every test above inspects the rendered TEXT, and none of them could see that
# the bounded read was unrunnable: the template handed Spark's `query` option
# `(SELECT ... WHERE ...) AS subq`, and Spark wraps that option itself as
# `SELECT * FROM (<query>) SPARK_GEN_SUBQ_n`, so PostgreSQL received
# `FROM ((SELECT ...) AS subq) SPARK_GEN_SUBQ_0` and rejected it as a syntax
# error. A first run with no seed takes the `dbtable` path, so the defect showed
# only on the second run.
#
# These tests slice the rendered read span out of the script, run it against a
# recording Spark reader and a stub watermark table, and assert on what Spark
# would receive. The expectations are written from the requirement — `query` is
# a plain SELECT bounded by the stored watermark, else the seed, else the whole
# table is read through `dbtable` — not from the template's current output.

_READ_SPAN_OPEN = "_watermarks_table = boto3.resource("
_READ_SPAN_CLOSE = "# Cache `df` BEFORE computing MAX"


def _incremental_read_span(template: jinja2.Template, *, engine: str = "postgres", initial_watermark: str = "") -> str:
    """The rendered incremental read, from the watermark lookup to the cache, dedented."""
    lines = _render(template, engine=engine, read_mode="incremental", initial_watermark=initial_watermark).splitlines()
    start = next(i for i, line in enumerate(lines) if _READ_SPAN_OPEN in line)
    end = next(i for i, line in enumerate(lines) if _READ_SPAN_CLOSE in line)
    return textwrap.dedent("\n".join(lines[start:end]))


class _RecordingReader:
    """`spark.read.format(...).option(...)....load()`, recording every option."""

    def __init__(self) -> None:
        self.options: dict[str, str] = {}

    def format(self, name: str) -> _RecordingReader:
        assert name == "jdbc"
        return self

    def option(self, key: str, value: str) -> _RecordingReader:
        self.options[key] = value
        return self

    def load(self) -> str:
        return "loaded-frame"


class _StubWatermarkTable:
    def __init__(self, item: dict[str, Any] | None) -> None:
        self._item = item

    def get_item(self, Key: dict[str, Any]) -> dict[str, Any]:
        return {"Item": self._item} if self._item is not None else {}


def _run_read(
    template: jinja2.Template,
    *,
    stored: str | None,
    initial_watermark: str = "",
    watermark_type: str = "numeric",
    engine: str = "postgres",
) -> dict[str, str]:
    """Execute the rendered read span; return the options Spark received."""
    import sys
    from types import SimpleNamespace

    shared = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
    if shared not in sys.path:
        sys.path.insert(0, shared)
    from jdbc_helpers import build_watermark_predicate, quote_identifier, validate_watermark_value

    reader = _RecordingReader()
    table = _StubWatermarkTable({"last_value": stored} if stored is not None else None)
    namespace: dict[str, Any] = {
        "args": {
            "watermarks_table_name": "agentic_data_ingestion_pipeline_watermarks",
            "pipeline_id": "pipe-1",
            "watermark_column": "id",
            "watermark_type": watermark_type,
            "jdbc_url": "jdbc:postgresql://db.example:5432/app",
            "jdbc_driver": "org.postgresql.Driver",
        },
        "boto3": SimpleNamespace(resource=lambda _service: SimpleNamespace(Table=lambda _name: table)),
        "spark": SimpleNamespace(read=reader),
        "logger": SimpleNamespace(info=lambda *_a, **_k: None),
        "db_user": "user-from-secret",
        "db_password": "password-from-secret",
        "_sch": quote_identifier(engine, "public"),
        "_tbl": quote_identifier(engine, "orders"),
        "validate_watermark_value": validate_watermark_value,
        "build_watermark_predicate": build_watermark_predicate,
    }
    span = _incremental_read_span(template, engine=engine, initial_watermark=initial_watermark)
    exec(compile(span, "<glue_job_jdbc_source.py.j2:incremental-read>", "exec"), namespace)
    assert namespace["df"] == "loaded-frame"
    return reader.options


def test_a_stored_watermark_bounds_a_plain_select(template: jinja2.Template) -> None:
    options = _run_read(template, stored="100")
    assert "dbtable" not in options, "query and dbtable must never both be set"
    assert options["query"] == 'SELECT * FROM "public"."orders" WHERE "id" > 100'


def test_the_query_survives_the_wrapper_spark_adds(template: jinja2.Template) -> None:
    """Spark parenthesises `query` and aliases it. What it receives must be a
    bare statement: no leading parenthesis and no alias of its own, or the
    wrapped form is `FROM ((...) AS x) SPARK_GEN_SUBQ_0`."""
    query = _run_read(template, stored="100")["query"]
    assert query.startswith("SELECT ")
    assert not query.lstrip().startswith("(")
    assert " AS " not in query.upper()


def test_the_seed_bounds_the_first_run(template: jinja2.Template) -> None:
    options = _run_read(template, stored=None, initial_watermark="2024-01-01T00:00:00Z", watermark_type="timestamp")
    assert options["query"] == 'SELECT * FROM "public"."orders" WHERE "id" > \'2024-01-01T00:00:00Z\'::timestamp'


def test_a_stored_watermark_wins_over_the_seed(template: jinja2.Template) -> None:
    options = _run_read(template, stored="500", initial_watermark="100")
    assert options["query"].endswith('"id" > 500')


def test_no_bound_reads_the_whole_table_through_dbtable(template: jinja2.Template) -> None:
    options = _run_read(template, stored=None)
    assert "query" not in options
    assert options["dbtable"] == '"public"."orders"'


@pytest.mark.parametrize("engine", ["mysql", "oracle-ee", "sqlserver-ee"])
def test_every_engine_gets_a_plain_select(template: jinja2.Template, engine: str) -> None:
    query = _run_read(template, stored="100", engine=engine)["query"]
    assert query.startswith("SELECT * FROM ")
    assert query.endswith("> 100")
    assert " AS " not in query.upper()


def test_a_stored_watermark_that_fails_validation_never_reaches_spark(template: jinja2.Template) -> None:
    with pytest.raises(ValueError):
        _run_read(template, stored="1; DROP TABLE orders")
