# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render assertions for p37 C1 that go through ``code_assembly``'s REAL env.

WHY THIS MODULE EXISTS, AND WHY IT IS NOT A DUPLICATE OF
``test_load_counts_render.py``.

That module builds its own ``jinja2.Environment``. So does
``test_code_assembly.py``, ``test_checkpoint_templates.py``, and
``scripts/_regen_golden_snapshots.py``. **A test that constructs its own
environment cannot catch a defect that lives in the real one, or in the
process state the real one runs under.** It renders past it.

That is not hypothetical. The defect this module was written for broke all 18
tests in ``tests/integration/pipeline_flow/test_code_generation_e2e.py`` with

    TypeError: can't multiply sequence by non-int of type '_MissingType'

raised from ``jinja2.filters.do_indent`` — while
``test_load_counts_render.py`` (48 passed) rendered the same five templates
green, and ``make test-fast`` never ran the failing layer because
``make test-fast`` excludes integration.

THE MECHANISM, because the fence only makes sense with it stated.
``unittest.mock.patch.dict("sys.modules", ...)`` does not just restore the
keys it overrode on exit: ``_unpatch_dict`` CLEARS the dict and re-applies the
snapshot taken on entry. Every module first imported inside the block is
therefore evicted from ``sys.modules``. 118 test modules in this repo use that
idiom to stub ``strands``/``env_utils``/``code_validator`` before importing
``lambda_function``, and ``lambda_function`` imports ``code_assembly``, which
imports ``jinja2``. When ``jinja2`` was not already loaded, the process ends up
holding TWO independent copies of the package: the one ``code_assembly`` is
bound to, and the one a later ``import jinja2`` (including the one the
compiled template performs, ``from jinja2.runtime import ... missing``) gets
from ``sys.modules``.

With two copies, ``missing`` is two distinct sentinels. ``resolve_or_missing``
returns copy A's; the generated template code tests ``is missing`` against
copy B's; the test fails, so an *undefined* variable is handed to filters as a
raw ``_MissingType`` instead of as an ``Undefined``. ``_MissingType`` is truthy
and is not an ``Undefined``, so ``default(x, 8, true)`` returned the sentinel
verbatim and ``indent`` got it as its ``width``.

The fix removed the fragile construct — the counts partial now derives its
indent from ``load_counts_stage`` inline, both branches int literals, so no
sentinel can reach ``indent``. The fences below are the durable half:

  * they render through ``code_assembly.generate_code`` / ``code_assembly``'s
    own ``_get_pyspark_jinja_env`` rather than a locally built environment, so
    env-configuration drift is visible here; and
  * ``test_load_counts_survives_a_second_jinja2_copy_in_sys_modules``
    reproduces the two-copies condition deterministically **in the unit
    layer**, so this class of defect fails in ``make test-fast`` instead of in
    a layer that target skips.

Validates: Requirements 1.1, 1.3, 1.11
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import os
import sys
from collections.abc import Iterator

import jinja2
import pytest

# Set region before any boto3-adjacent import, matching the other create_code
# unit modules.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_code"),
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# ``code_assembly`` is import-light (no strands, no boto3 clients), so it is
# imported directly — NOT through ``lambda_function`` behind a
# ``patch.dict("sys.modules", ...)`` block. That matters here: the patch.dict
# idiom is the thing whose fallout this module tests, so using it to reach the
# code under test would hide the very condition being asserted.
import code_assembly

PARTIAL = "_partials/_load_counts.py.j2"

# Rendered statements, never prose (the partial's Jinja header comment names
# several of these deliberately and never reaches the output).
LOAD_METRICS_ROW = '"step_name": "load_metrics"'
BLOCK_HEADER = "# p37 R1.1/R1.11: record Rows_Read / Rows_Written for this run"
WRITE_CONFIRMED_FALSE = "_adi_lc_write_confirmed = bool(False)"
WRITE_CONFIRMED_TRUE = "_adi_lc_write_confirmed = bool(True)"
ROWS_WRITTEN_ZERO = "_adi_lc_rows_written = int(0)"
ROWS_WRITTEN_TRANSFORMED = "_adi_lc_rows_written = int(transformed_count)"

# (source_type, target_config, source_config) per template, in the persisted
# shape generate_code actually receives — the type under `type`, not
# `target_type`.
PIPELINES: dict[str, dict] = {
    "glue_job_s3_tables.py.j2": {
        "source_type": "S3_UPLOAD",
        "target_config": {
            "type": "s3_tables",
            "source_format": "json",
            "bucket_name": "my-bucket",
            "namespace_name": "my_ns",
            "table_name": "my_table",
            "warehouse_location": "s3://my-bucket/warehouse",
        },
    },
    "glue_job_glue_catalog.py.j2": {
        "source_type": "S3_UPLOAD",
        "target_config": {
            "type": "glue_catalog",
            "source_format": "csv",
            "database_name": "my_db",
            "table_name": "my_table",
        },
    },
    "glue_job_jdbc.py.j2": {
        "source_type": "S3_UPLOAD",
        "target_config": {
            "type": "jdbc",
            "source_format": "json",
            "jdbc_url": "jdbc:postgresql://host:5432/db",
            "jdbc_table": "public.my_table",
            "jdbc_driver": "org.postgresql.Driver",
        },
    },
    "glue_job_s3_parquet.py.j2": {
        "source_type": "S3_UPLOAD",
        "target_config": {
            "type": "s3_parquet",
            "source_format": "parquet",
            "output_path": "s3://output-bucket/data/",
            "partition_columns": "",
        },
    },
    # The fifth template, reached only from a JDBC source. It has no golden
    # snapshot test, so every fence it has is a render assertion.
    "glue_job_jdbc_source.py.j2": {
        "source_type": "JDBC",
        "source_config": {"engine": "postgres", "jdbc_url": "jdbc:postgresql://src:5432/srcdb"},
        "target_config": {
            "type": "jdbc",
            "jdbc_url": "jdbc:postgresql://tgt:5432/tgtdb",
            "jdbc_table": "public.customers",
            "write_mode": "overwrite",
        },
    },
}

TEMPLATE_IDS = list(PIPELINES)


def _generate(template_name: str) -> str:
    """Render through the production entry point: ``code_assembly.generate_code``."""
    spec = PIPELINES[template_name]
    pipeline_metadata = {
        "source_type": spec["source_type"],
        "source_config": spec.get("source_config", {}),
        "target_config": spec["target_config"],
        "source_schema": {"properties": {"id": {"type": "integer"}}},
    }
    return code_assembly.generate_code(
        pipeline_metadata,
        {"field_mappings": [{"source_field": "id", "target_field": "id"}]},
        generate_transform=lambda **_kw: "df_transformed = df",
        quarantine_bucket="adi-quarantine-bucket",
    )


@contextlib.contextmanager
def _second_jinja2_copy_in_sys_modules() -> Iterator[jinja2.__class__]:
    """Evict ``jinja2`` from ``sys.modules`` and re-import it, then restore.

    Reproduces exactly what ``patch.dict("sys.modules", ...)`` leaves behind:
    the already-imported ``code_assembly`` keeps its binding to the ORIGINAL
    package object, while anything that imports ``jinja2`` from here on — the
    compiled template's own ``from jinja2.runtime import ... missing``
    included — gets a second, unrelated copy.

    Restoring is done by key, not by ``patch.dict``, so this helper does not
    reproduce the hazard it is testing for the tests that follow it.
    """
    saved = {name: mod for name, mod in sys.modules.items() if name == "jinja2" or name.startswith("jinja2.")}
    assert saved, "expected jinja2 to be imported already"
    for name in saved:
        del sys.modules[name]
    try:
        second = importlib.import_module("jinja2")
        assert second is not saved["jinja2"], "expected a genuinely separate module object"
        assert second.runtime.missing is not saved["jinja2"].runtime.missing
        yield second
    finally:
        for name in [n for n in sys.modules if n == "jinja2" or n.startswith("jinja2.")]:
            del sys.modules[name]
        sys.modules.update(saved)


# ---------------------------------------------------------------------------
# The environment code_assembly actually builds
# ---------------------------------------------------------------------------


def test_code_assembly_env_is_strict_and_can_load_the_counts_partial() -> None:
    """The real env, asserted directly rather than mirrored in a fixture.

    Every other template test builds its own environment and mirrors these two
    settings by hand. That mirror has drifted before — the docstring of
    ``scripts/_regen_golden_snapshots.py`` says so about itself. Asserting the
    production env here means drift shows up as a failure rather than as two
    suites quietly disagreeing.
    """
    env = code_assembly._get_pyspark_jinja_env()
    assert env.undefined is jinja2.StrictUndefined
    assert env.keep_trailing_newline is True
    # The partial must be reachable through the real searchpath, not just
    # through a test-local one.
    assert env.get_template(PARTIAL) is not None


# ---------------------------------------------------------------------------
# Both include positions, through the production entry point
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", TEMPLATE_IDS, ids=TEMPLATE_IDS)
def test_generate_code_emits_both_counts_positions(template_name: str) -> None:
    """`R1.1`/`R1.3`: two load_metrics rows per generated script, and it parses."""
    code = _generate(template_name)
    assert code.count(LOAD_METRICS_ROW) == 2, (
        f"{template_name}: expected the load_metrics row at exactly 2 positions, found {code.count(LOAD_METRICS_ROW)}"
    )
    ast.parse(code)


@pytest.mark.parametrize("template_name", TEMPLATE_IDS, ids=TEMPLATE_IDS)
def test_generate_code_keeps_write_confirmed_split_between_the_stages(template_name: str) -> None:
    """Risk N1 through the real path: ``empty_source`` False, ``post_write`` True.

    ``transformed_count`` is rows handed to the writer, not rows confirmed
    written, and ``write_confirmed`` is the only place that distinction
    survives into the data.
    """
    code = _generate(template_name)
    assert code.count(WRITE_CONFIRMED_FALSE) == 1, template_name
    assert code.count(WRITE_CONFIRMED_TRUE) == 1, template_name
    assert code.count(ROWS_WRITTEN_ZERO) == 1, template_name
    assert code.count(ROWS_WRITTEN_TRANSFORMED) == 1, template_name
    assert code.index(ROWS_WRITTEN_ZERO) < code.index(WRITE_CONFIRMED_FALSE) < code.index(WRITE_CONFIRMED_TRUE)
    assert code.index(ROWS_WRITTEN_TRANSFORMED) < code.index(WRITE_CONFIRMED_TRUE)


@pytest.mark.parametrize("template_name", TEMPLATE_IDS, ids=TEMPLATE_IDS)
def test_generate_code_indents_the_two_positions_differently(template_name: str) -> None:
    """The indent widths, asserted as widths — this is what the defect destroyed.

    Position (i) sits inside ``try:`` *and* ``if source_count == 0:`` (8
    spaces); position (ii) sits in the module body inside ``try:`` (4). One
    partial, two indents, derived from the stage. ``ast.parse`` above catches a
    *broken* indent; only measuring the widths catches a *wrong* one.
    """
    code = _generate(template_name)
    widths = sorted(len(line) - len(line.lstrip(" ")) for line in code.split("\n") if line.strip() == BLOCK_HEADER)
    assert widths == [4, 8], f"{template_name}: expected block headers at indents 4 and 8, got {widths}"


# ---------------------------------------------------------------------------
# The regression fence for the defect itself
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", TEMPLATE_IDS, ids=TEMPLATE_IDS)
def test_load_counts_survives_a_second_jinja2_copy_in_sys_modules(template_name: str) -> None:
    """Renders correctly when ``missing`` is no longer a single object.

    This is the defect, reproduced in the layer ``make test-fast`` runs. Under
    two jinja2 copies, any construct whose result depends on
    ``value is missing`` or ``isinstance(value, Undefined)`` silently changes
    meaning. The counts partial must not contain such a construct: its indent
    comes from ``load_counts_stage``, which the caller always supplies through
    ``{% with %}`` and which therefore arrives as a real string, and both
    branches of the width expression are int literals.

    Against the pre-fix template — ``load_counts_indent | default(8 if ... else
    4, true)`` — this test fails with
    ``TypeError: can't multiply sequence by non-int of type '_MissingType'``,
    which is verbatim the failure the integration layer showed.
    """
    with _second_jinja2_copy_in_sys_modules():
        code = _generate(template_name)
    assert code.count(LOAD_METRICS_ROW) == 2, template_name
    widths = sorted(len(line) - len(line.lstrip(" ")) for line in code.split("\n") if line.strip() == BLOCK_HEADER)
    assert widths == [4, 8], f"{template_name}: expected block headers at indents 4 and 8, got {widths}"
    ast.parse(code)


def test_second_jinja2_copy_helper_is_restored() -> None:
    """The helper must not leak a foreign jinja2 into the rest of the session."""
    before = sys.modules["jinja2"]
    with _second_jinja2_copy_in_sys_modules() as second:
        assert sys.modules["jinja2"] is second
    assert sys.modules["jinja2"] is before
    assert code_assembly.jinja2 is before


# ---------------------------------------------------------------------------
# The loudness property the `{% with %}` shape exists to provide
# ---------------------------------------------------------------------------


def test_omitted_stage_still_fails_the_render_loudly() -> None:
    """An include that forgets ``load_counts_stage`` must NOT render.

    The stage is stated by the caller and never guessed — there is deliberately
    no default, because a guessed stage stamps a ``write_confirmed`` nobody
    chose. Asserted through ``code_assembly``'s own env so the guarantee is
    tied to the environment production uses (``StrictUndefined``), not to a
    test-local one that happens to be configured the same way today.
    """
    env = code_assembly._get_pyspark_jinja_env()
    with pytest.raises(jinja2.exceptions.UndefinedError, match="load_counts_stage"):
        env.get_template(PARTIAL).render()

    # And with the stage supplied, the same render succeeds — so the failure
    # above is attributable to the missing stage and nothing else.
    rendered = env.get_template(PARTIAL).render(load_counts_stage="empty_source")
    assert WRITE_CONFIRMED_FALSE in rendered
