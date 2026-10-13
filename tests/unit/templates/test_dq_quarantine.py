# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R11.1 — DQ quarantine template tests.

The s3_tables template (and any template applying DQDL) must split DQDL
row-level outcomes: Passed rows continue to the target write, Failed rows are
written to ``quarantine/<run_id>/`` (parquet), and ``rows_passed`` /
``rows_quarantined`` counters are emitted to CloudWatch job metrics and the
run state record. The old only-'Passed'-rows-written-silently behaviour must
be gone.

Quarantine bucket decision (documented): the generated-code bucket
(``adi-generated-code-*``) — NOT the S3 Tables target bucket, which is a
table bucket that only accepts Iceberg table writes (no loose parquet
objects). The Glue job role already has ``s3:PutObject`` on
``adi-generated-code-*`` (iam_roles.py), so no new permissions are needed.
"""

import ast
import glob
import os

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = "df_transformed = df"

SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "customer_id" >= 0.95\n]'

QUARANTINE_BUCKET = "adi-generated-code-123456789012-us-east-1"

DQ_RENDER_VARS = {
    "transform_block": SAMPLE_TRANSFORM,
    "source_format": "json",
    "partition_columns": "",
    "write_mode": "append",
    "merge_key": "id",
    "checkpoint_enabled": False,
    "checkpoint_bucket": "",
    "dqdl_rules": SAMPLE_DQDL,
    "quarantine_bucket": QUARANTINE_BUCKET,
    # p64 R6.6 / 2b.4. The DQ body is now behind `data_quality_enabled` as well as
    # `dqdl_rules`, and this module's subject is the block's rendered structure —
    # the non-blocking try, the row split, the quarantine write, the counters — so
    # it renders the ENABLED arm. True is also this template's production default
    # (`quality_verdict.DQ_DEFAULT_ON_TEMPLATES`), so every assertion here still
    # describes what an existing S3 Tables pipeline actually gets. The gate's own
    # behaviour is asserted in `test_data_quality_gate.py`.
    "data_quality_enabled": True,
}


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


@pytest.fixture
def rendered_with_dq(jinja_env):
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    return template.render(**DQ_RENDER_VARS)


@pytest.mark.parametrize("write_mode", ["overwrite", "append", "merge"])
def test_dq_render_is_valid_python(jinja_env, write_mode):
    """The DQ-enabled render must remain syntactically valid Python in every write mode."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    rendered = template.render(**{**DQ_RENDER_VARS, "write_mode": write_mode})
    ast.parse(rendered)


def test_quarantine_write_path_with_run_id_interpolation(rendered_with_dq):
    """Failing rows are written to s3://<quarantine-bucket>/quarantine/<run_id>/ as parquet."""
    # The rendered bucket value is baked in at render time.
    assert f'_dq_quarantine_bucket = "{QUARANTINE_BUCKET}"' in rendered_with_dq
    # The quarantine prefix interpolates the per-run id.
    assert 'f"s3://{_dq_quarantine_bucket}/quarantine/{_dq_quarantine_run_id}/"' in rendered_with_dq
    # The failing split is persisted as parquet.
    assert '_dq_failed_df.write.mode("overwrite").parquet(_dq_quarantine_path)' in rendered_with_dq
    # run_id comes from the --run_id runtime argument (run_pipeline always
    # passes it) resolved via getResolvedOptions through the shared
    # _runtime_arg mechanism (P28 R19.9), falling back to the Glue-provided
    # JOB_RUN_ID for manual runs.
    assert '_adi_run_id = _runtime_arg("run_id")' in rendered_with_dq
    assert '_adi_effective_run_id = _adi_run_id or args.get("JOB_RUN_ID") or "unknown"' in rendered_with_dq
    assert "_dq_run_id = _adi_run_id" in rendered_with_dq
    assert "_dq_quarantine_run_id = _adi_effective_run_id" in rendered_with_dq
    # ONE resolution mechanism: the old raw-sys.argv scanner is gone.
    assert "_dq_cli_arg" not in rendered_with_dq
    assert "getResolvedOptions(sys.argv, [_name])" in rendered_with_dq


def test_outcome_split_keeps_passed_and_quarantines_failed(rendered_with_dq):
    """The row-level outcome is split into a Failed frame and a Passed frame."""
    assert "DataQualityEvaluationResult != 'Passed'" in rendered_with_dq
    assert "DataQualityEvaluationResult = 'Passed'" in rendered_with_dq
    # Passed rows continue to the target write.
    assert "df_transformed = _dq_passed_df" in rendered_with_dq


def test_passed_only_silent_filter_removed(rendered_with_dq):
    """The old silent drop ('writing only passed rows', no quarantine) is gone."""
    assert "writing only passed rows" not in rendered_with_dq
    # The old single-sided filter kept only Passed rows and discarded the rest.
    assert "passed_df = row_outcomes.toDF().filter" not in rendered_with_dq


def test_counters_emitted_to_cloudwatch_metrics(rendered_with_dq):
    """rows_passed + rows_quarantined go to CloudWatch job metrics."""
    assert "put_metric_data" in rendered_with_dq
    assert '"MetricName": "RowsPassed"' in rendered_with_dq
    assert '"MetricName": "RowsQuarantined"' in rendered_with_dq
    assert 'Namespace="AgenticDataIngestion/DataQuality"' in rendered_with_dq


def test_counters_written_to_run_state_record(rendered_with_dq):
    """rows_passed + rows_quarantined + quarantine_path land in the state-table DQ report."""
    assert '"rows_passed": transformed_count,' in rendered_with_dq
    assert '"rows_quarantined": _dq_rows_quarantined,' in rendered_with_dq
    assert '"quarantine_path": _dq_quarantine_path or None,' in rendered_with_dq
    # Written to the state table the job already targets (--state_table + run_id).
    assert '_dq_state_table = _runtime_arg("state_table", "agentic_data_ingestion_state")' in rendered_with_dq
    assert '_boto3.resource("dynamodb").Table(_dq_state_table)' in rendered_with_dq


def test_zero_failing_rows_skips_quarantine_write_but_keeps_counters(rendered_with_dq):
    """No failing rows → no quarantine write (no empty objects); counters still recorded."""
    # The write is guarded by an actual failing-row count...
    assert "_dq_rows_quarantined = _dq_failed_df.count()" in rendered_with_dq
    assert "if _dq_rows_quarantined > 0:" in rendered_with_dq
    # ...and the counter default is 0, recorded unconditionally in the report.
    assert "_dq_rows_quarantined = 0" in rendered_with_dq


def test_quarantine_write_failure_is_not_swallowed(rendered_with_dq):
    """The split/quarantine block sits OUTSIDE the non-blocking eval try/except.

    A quarantine-write failure must fail the job — losing rows silently is the
    exact behaviour R11.1 removes. String-level proxy: the non-blocking handler
    appears BEFORE the quarantine write in the rendered script.
    """
    non_blocking = rendered_with_dq.index("Data quality evaluation failed (non-blocking)")
    quarantine_write = rendered_with_dq.index('_dq_failed_df.write.mode("overwrite").parquet')
    assert non_blocking < quarantine_write
    # Misconfiguration (no bucket) with failing rows raises instead of dropping.
    assert "refusing to silently drop failing rows" in rendered_with_dq


def test_render_without_dqdl_has_no_quarantine_block(jinja_env):
    """Without dqdl_rules the DQ/quarantine block must not render (golden-compat)."""
    template = jinja_env.get_template("glue_job_s3_tables.py.j2")
    vars_no_dq = {k: v for k, v in DQ_RENDER_VARS.items() if k not in ("dqdl_rules", "quarantine_bucket")}
    rendered = template.render(**vars_no_dq)
    ast.parse(rendered)
    assert "quarantine" not in rendered
    assert "put_metric_data" not in rendered


def test_every_dqdl_applying_template_quarantines():
    """Coverage guard: any template that runs EvaluateDataQuality must quarantine.

    R11.1 covers "the s3_tables template (and any template applying DQDL)".
    If DQDL evaluation is added to another template without quarantine
    handling, this test fails.

    AMENDED BY p64 2b.1 (R6.1/R6.2), DELIBERATELY — read the reason at the
    assertion below before changing it back. The expected file moved from
    ``glue_job_s3_tables.py.j2`` to ``_partials/_data_quality.py.j2``: the
    block was extracted verbatim to a partial so the other four templates can
    include it rather than carry four copies. ``glue_job_s3_tables.py.j2``
    still applies DQDL — it now does so through an ``{% include %}``, which
    this file-level scan cannot see but every render-level assertion in this
    module still does (``rendered_with_dq`` renders that template and asserts
    the quarantine markers are present).
    """
    template_files = glob.glob(os.path.join(TEMPLATES_DIR, "*.j2")) + glob.glob(
        os.path.join(TEMPLATES_DIR, "_partials", "*.j2")
    )
    assert template_files, "no templates found — check TEMPLATES_DIR"

    dqdl_appliers = []
    for path in template_files:
        with open(path) as f:
            source = f.read()
        if "EvaluateDataQuality" in source:
            dqdl_appliers.append((os.path.basename(path), source))

    # ONE file, and it is the partial. p64 R6.2 requires the mechanism to be
    # extracted to a `_partials/` include rather than copied into each target
    # template, so the DQDL evaluator living in exactly one file is now the
    # invariant rather than an accident of only one target supporting it. That
    # makes this guard STRICTER than the pre-extraction form it replaces: a
    # template that grows its own inline `EvaluateDataQuality` — the copy-paste
    # R6.2 exists to prevent — fails here even if that copy quarantines
    # correctly. Extend the include, not this list.
    assert [name for name, _ in dqdl_appliers] == ["_data_quality.py.j2"], (
        "DQDL-applying template set changed — the evaluator belongs in "
        "_partials/_data_quality.py.j2 alone (p64 R6.2). If a template now "
        "evaluates DQDL inline, make it include the partial instead; if a "
        "second partial legitimately evaluates DQDL, extend quarantine "
        "coverage to it and say why here."
    )
    for name, source in dqdl_appliers:
        assert "/quarantine/" in source, f"{name} applies DQDL but has no quarantine write"
        assert "rows_quarantined" in source, f"{name} applies DQDL but emits no quarantine counter"
