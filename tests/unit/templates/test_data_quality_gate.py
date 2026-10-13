# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R6.6 / O4 — the DQ block is gated, and the gate does not reclassify anyone.

WHAT WENT WRONG THAT THIS MODULE EXISTS TO PREVENT. Before p64,
``glue_job_s3_tables.py.j2`` was the only template with an ``EvaluateDataQuality``
block. R6.1/R6.2 (tasks 2b.1, 2b.2) moved it into
``_partials/_data_quality.py.j2`` and included it from all five. That single
change silently gave every EXISTING pipeline of every other target type
row-level Passed/Failed splitting, a quarantine write and a DQ report row it had
never had — and it fired ``LGR-eee8ac8-6``'s ``revisit_when`` verbatim ("a second
template gains an ``EvaluateDataQuality`` block"), because
``shared/quality_verdict.py`` derives its reported mode from the rendered
template. R6.6: "the verdict mode must not silently start reporting ``enforced``
for pipelines whose behaviour did not change."

THE GATE HAS TWO DIRECTIONS AND BOTH ARE ASSERTED HERE. A flat ``False`` default
would satisfy R6.6's letter for the four newly-ported templates and violate its
intent for the fifth: it would strip the DQ block from every existing S3 Tables
pipeline, moving that pipeline's verdict from ``enforced`` to ``advisory`` and
removing a quarantine mechanism it has had since P28. So the default is the
posture each template had BEFORE p64 —
``quality_verdict.DQ_DEFAULT_ON_TEMPLATES`` — and the assertions below pin both
arms: nothing renders for the four by default, everything renders for the fifth
by default.

WHY THIS IS A SEPARATE MODULE FROM ``test_data_quality_all_targets.py``. That one
asserts what the block does once it renders (R6.1's four markers, the stage
order, R6.3's fence outside the non-blocking try, R6.4's verbatim refusal), so it
renders with the gate ON. Folding R6.6's negative assertions into it would let a
gate stuck permanently OFF satisfy them while quietly making every one of that
module's positive assertions unreachable. Kept apart, the two modules fail in
opposite directions and cannot both pass on a broken gate.

THE TRAP THIS MODULE IS BUILT AROUND. "The DQ body is absent" is the assertion,
and it is trivially true for a second, unrelated reason: an empty ``dqdl_rules``
already deletes the same body (``{% if data_quality_enabled and dqdl_rules … %}``
— two conditions, one outcome). A test that only checked absence under
``gate=False`` would pass just as happily if the gate were ignored entirely and
the ruleset were the thing doing the work. Every negative test below therefore
carries a PAIRED positive with the same ruleset and the gate flipped, so the
absence is attributable to the gate and nothing else.

Validates: Requirements 6.6
"""

from __future__ import annotations

import ast
import os
import pathlib
import sys

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SHARED_DIR = str(pathlib.Path(__file__).resolve().parent.parent.parent.parent / "shared")
if SHARED_DIR not in sys.path:
    sys.path.insert(0, SHARED_DIR)

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_code"),
)

# Imported directly rather than through ``lambda_function`` behind a
# ``patch.dict("sys.modules", …)`` block, matching the sibling create_code
# template modules — that idiom leaves a second jinja2 in ``sys.modules``, which
# is the condition ``test_dq_template_vars_contract.py`` exists to fence.
import code_assembly

from quality_verdict import (
    DQ_DEFAULT_ON_TEMPLATES,
    FALLBACK_TEMPLATE_MARKER,
    FROM_CREATE_CODE_ROW,
    FROM_MISSING_CREATE_CODE_ROW,
    MODE_ADVISORY,
    MODE_ENFORCED,
    derive_quality_verdict,
)

SAMPLE_TRANSFORM = 'df_transformed = df.withColumn("name", col("name").cast(StringType()))'
SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "id" >= 0.95\n]'
QUARANTINE_BUCKET = "adi-quarantine-bucket"

EVALUATE_BANNER = "# === Glue Data Quality Evaluation ==="
EVALUATE_END = "# === End Data Quality ==="
QUARANTINE_WRITE = '_dq_failed_df.write.mode("overwrite").parquet(_dq_quarantine_path)'
PASSED_REBIND = "df_transformed = _dq_passed_df"
DQ_STATE_ROW = '"step_name": "data_quality"'

# Every marker that must vanish when the gate is off. Deliberately the union of
# the four R6.1 enumerates plus the two side effects — a gate that suppressed the
# evaluator but still published counters or wrote a state row would report on an
# evaluation that never happened.
DQ_BODY_MARKERS = (
    "EvaluateDataQuality",
    QUARANTINE_WRITE,
    PASSED_REBIND,
    DQ_STATE_ROW,
    "put_metric_data",
    "no quarantine bucket is configured",
    "_dq_quarantine_bucket",
)

S3_TABLES_TEMPLATE = "glue_job_s3_tables.py.j2"
JDBC_SOURCE_TEMPLATE = "glue_job_jdbc_source.py.j2"
PORTED_TEMPLATES = [
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
    JDBC_SOURCE_TEMPLATE,
]
ALL_TEMPLATES = [S3_TABLES_TEMPLATE, *PORTED_TEMPLATES]


@pytest.fixture
def jinja_env() -> jinja2.Environment:
    """StrictUndefined, matching ``code_assembly._get_pyspark_jinja_env``."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(
    env: jinja2.Environment,
    name: str,
    *,
    data_quality_enabled: bool,
    dqdl_rules: str = SAMPLE_DQDL,
    target_type: str = "S3_TABLES",
) -> str:
    """Render ``name``. ``data_quality_enabled`` is REQUIRED — never defaulted here."""
    template = env.get_template(name)
    if name == JDBC_SOURCE_TEMPLATE:
        return template.render(
            engine="postgres",
            target_type=target_type,
            read_mode="incremental",
            write_mode="append",
            initial_watermark="2024-01-01T00:00:00Z",
            partition_columns="",
            merge_key="id",
            transform_block=SAMPLE_TRANSFORM,
            dqdl_rules=dqdl_rules,
            quarantine_bucket=QUARANTINE_BUCKET,
            data_quality_enabled=data_quality_enabled,
        )
    return template.render(
        transform_block=SAMPLE_TRANSFORM,
        source_format="csv",
        source_delimiter=",",
        checkpoint_enabled=False,
        checkpoint_bucket="",
        write_mode="overwrite",
        merge_key="id",
        partition_columns="",
        column_rename_block="",
        dqdl_rules=dqdl_rules,
        quarantine_bucket=QUARANTINE_BUCKET,
        data_quality_enabled=data_quality_enabled,
    )


# ---------------------------------------------------------------------------
# The render: the gate suppresses the body, and only the gate does
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_gate_off_removes_the_whole_dq_body_even_with_a_ruleset(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R6.6: a non-opted-in pipeline renders no DQ, on any target type.

    Rendered WITH a non-empty ruleset on purpose — that is the reclassification
    case. A pipeline that never configured rules was already unaffected, because
    the body sits behind ``dqdl_rules`` too; the pipelines R6.6 protects are the
    ones that HAVE a ruleset and previously had a non-s3_tables target, and they
    are only distinguishable from the empty-ruleset case by supplying rules.

    The paired positive below is what makes this attributable: the same ruleset
    with the gate on renders every one of these markers.
    """
    rendered = _render(jinja_env, template_name, data_quality_enabled=False)
    ast.parse(rendered)

    for marker in DQ_BODY_MARKERS:
        assert marker not in rendered, (
            f"{template_name}: {marker!r} rendered with data_quality_enabled=False and a "
            f"non-empty ruleset. R6.6: the ported block must not reach a pipeline that did "
            f"not opt in — that is what would move its quality verdict from advisory to "
            f"enforced without anyone asking (LGR-eee8ac8-6's revisit_when)."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_gate_on_renders_the_whole_dq_body(jinja_env: jinja2.Environment, template_name: str) -> None:
    """The paired positive: the absence above is the gate's doing, not the ruleset's."""
    rendered = _render(jinja_env, template_name, data_quality_enabled=True)
    ast.parse(rendered)

    for marker in DQ_BODY_MARKERS:
        assert marker in rendered, (
            f"{template_name}: {marker!r} did NOT render with data_quality_enabled=True. "
            f"Without this, the gate-off assertions are vacuous — they would pass on a "
            f"gate that suppresses the block unconditionally."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_banner_renders_either_way_so_the_gate_is_not_invisible(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The residue, pinned: two comments and nothing executable.

    The banners sit OUTSIDE the ``{% if %}``, so a gated-off render differs from
    the pre-p64 script by exactly these two comment lines. Measured across the
    axis matrix during 2b.4, and asserted here so the residue cannot grow into
    something executable without a test saying so.
    """
    rendered = _render(jinja_env, template_name, data_quality_enabled=False)
    assert EVALUATE_BANNER in rendered and EVALUATE_END in rendered, (
        f"{template_name}: the DQ banners must render regardless of the gate — they are "
        f"what makes a gated-off job legible as 'DQ considered, not enabled' rather than "
        f"'DQ never existed here'."
    )
    # Nothing executable between the two banners.
    body = rendered.split(EVALUATE_BANNER, 1)[1].split(EVALUATE_END, 1)[0]
    assert not [line for line in body.split("\n") if line.strip() and not line.strip().startswith("#")], (
        f"{template_name}: the gated-off DQ region is not comment-only:\n{body!r}"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_an_empty_ruleset_still_wins_over_an_opt_in(jinja_env: jinja2.Environment, template_name: str) -> None:
    """Both conditions are required, not either — the gate does not override R7.4's degradation.

    ``_generate_dqdl_rules`` returns ``""`` when no field mappings reached code
    generation. Opting in cannot conjure rules to evaluate, and a DQ block with an
    empty ruleset would fail at Glue run time on an empty DQDL string. So the
    ``and`` is load-bearing in both directions.
    """
    rendered = _render(jinja_env, template_name, data_quality_enabled=True, dqdl_rules="")
    ast.parse(rendered)
    for marker in DQ_BODY_MARKERS:
        assert marker not in rendered, (
            f"{template_name}: {marker!r} rendered with an opt-in but an EMPTY ruleset. "
            f"The guard is `data_quality_enabled AND dqdl_rules`, not either."
        )


@pytest.mark.parametrize("target_type", ["S3_TABLES", "GLUE_CATALOG", "JDBC", "RDS_DATABASE", "S3_PARQUET"])
def test_jdbc_source_is_gated_for_every_target_type(jinja_env: jinja2.Environment, target_type: str) -> None:
    """The gate sits outside the per-target ``{% elif %}`` dispatch, like the include does.

    ``glue_job_jdbc_source.py.j2`` branches on ``target_type`` for auto-cast and
    for the write partial, with the DQ include between them. A gate that landed
    inside one of those arms would suppress the block for one target and leak it
    for four — the same partial-coverage shape R6.1 exists to end, and invisible
    to a single-target test.
    """
    off = _render(jinja_env, JDBC_SOURCE_TEMPLATE, data_quality_enabled=False, target_type=target_type)
    on = _render(jinja_env, JDBC_SOURCE_TEMPLATE, data_quality_enabled=True, target_type=target_type)
    ast.parse(off)
    ast.parse(on)
    label = f"{JDBC_SOURCE_TEMPLATE} target={target_type}"
    assert "EvaluateDataQuality" not in off, f"{label}: DQ leaked past a gated-off render"
    assert "EvaluateDataQuality" in on, f"{label}: DQ did not render when opted in"


# ---------------------------------------------------------------------------
# The resolver: what the DEFAULT is, which is the whole of R6.6
# ---------------------------------------------------------------------------


class TestDefaultPosture:
    """``resolve_data_quality_enabled`` with no ``quality_config`` on the record.

    This is the state of every pipeline in existence until 2c ships the wizard
    control, so these four assertions are what "no existing pipeline changes"
    actually means.
    """

    @pytest.mark.parametrize("template_name", PORTED_TEMPLATES)
    def test_the_four_ported_templates_default_off(self, template_name: str) -> None:
        assert code_assembly.resolve_data_quality_enabled({}, template_name) is False, (
            f"{template_name} defaults to DQ ON. R6.6: it had no EvaluateDataQuality block "
            f"before p64, so an existing pipeline on it must render exactly what it "
            f"rendered before."
        )

    def test_s3_tables_defaults_on(self) -> None:
        assert code_assembly.resolve_data_quality_enabled({}, S3_TABLES_TEMPLATE) is True, (
            "glue_job_s3_tables.py.j2 defaults to DQ OFF. That is R6.6's other direction: "
            "this template has had the block since P28, so defaulting it off would strip a "
            "quarantine mechanism from every existing S3 Tables pipeline and move its "
            "verdict from enforced to advisory — the same silent reclassification."
        )

    def test_the_default_set_is_the_one_in_shared_not_a_local_copy(self) -> None:
        """One authority for the default, so renderer and reporter cannot diverge."""
        assert code_assembly.DQ_DEFAULT_ON_TEMPLATES is DQ_DEFAULT_ON_TEMPLATES, (
            "code_assembly holds its OWN copy of the default-on set. Two copies is how the "
            "rendered script and the dashboard's reported mode come to disagree about "
            "whether a job gates its write — import it from shared/quality_verdict.py."
        )

    @pytest.mark.parametrize("template_name", [None, ""])
    def test_no_template_means_no_dq_whatever_the_pipeline_asked_for(self, template_name: str | None) -> None:
        """The full-AI fallback renders no template, so there is no block to enable.

        ``None`` and ``""`` are the values the resolver actually sees on that path:
        ``_select_template`` returns ``None`` when no rule matches and the handler
        threads ``template_name_j2`` — which is falsy there — straight through.
        ``"fallback_full_ai_v1"`` is a different string, written to
        ``summary.template_used`` for the READ side, and it is rejected by
        ``derive_quality_verdict`` rather than here
        (``test_an_opt_in_cannot_make_the_fallback_path_claim_enforced``). Two
        names, two owners, deliberately — the resolver's input is a template
        filename and the verdict's is a recorded label.
        """
        opted_in = {"quality_config": {"data_quality_enabled": True}}
        assert code_assembly.resolve_data_quality_enabled(opted_in, template_name) is False, (
            f"template_name={template_name!r} resolved to DQ ON. The fallback path renders no "
            f"template at all, so claiming a DQ block would describe code that does not exist."
        )


class TestExplicitOptIn:
    """The setting overrides the default, in both directions, and only when it is a real bool."""

    @pytest.mark.parametrize("template_name", PORTED_TEMPLATES)
    def test_opting_in_enables_a_ported_template(self, template_name: str) -> None:
        metadata = {"quality_config": {"data_quality_enabled": True}}
        assert code_assembly.resolve_data_quality_enabled(metadata, template_name) is True

    def test_opting_out_disables_s3_tables(self) -> None:
        """The default is a default, not a floor — an S3 Tables pipeline may opt out."""
        metadata = {"quality_config": {"data_quality_enabled": False}}
        assert code_assembly.resolve_data_quality_enabled(metadata, S3_TABLES_TEMPLATE) is False

    def test_the_setting_is_not_read_from_target_config(self) -> None:
        """Design C7: a data-quality setting is not target configuration.

        ``generate_code`` splats ``target_config`` wholesale into the render
        context, so a flag there would become a template variable with no backend
        change and no validation — the cheap route C7 explicitly rejects, because
        ``target_config`` is free-form ``Record<string, string>``. Asserted rather
        than trusted: this is the shortcut a later change is most likely to take.
        """
        metadata = {"target_config": {"data_quality_enabled": True, "type": "jdbc"}}
        assert code_assembly.resolve_data_quality_enabled(metadata, "glue_job_jdbc.py.j2") is False, (
            "the opt-in was honoured from target_config. design.md C7 routes it through a "
            "`quality_config` peer of approval_config instead, so it has a validated home."
        )

    @pytest.mark.parametrize("value", ["true", "false", 1, 0, None, [], {}, "yes"])
    def test_a_non_bool_setting_falls_through_to_the_default(self, value: object) -> None:
        """Truthiness on an unvalidated value would make the string ``"false"`` mean enabled.

        2c adds the validator at the API boundary (R10.4). Until then — and after,
        for a record written before the validator existed — anything that is not a
        ``bool`` is "not set" rather than coerced.
        """
        metadata = {"quality_config": {"data_quality_enabled": value}}
        assert code_assembly.resolve_data_quality_enabled(metadata, "glue_job_jdbc.py.j2") is False
        assert code_assembly.resolve_data_quality_enabled(metadata, S3_TABLES_TEMPLATE) is True

    @pytest.mark.parametrize("quality_config", [None, "enabled", 42, []])
    def test_a_non_dict_quality_config_falls_through_to_the_default(self, quality_config: object) -> None:
        metadata = {"quality_config": quality_config}
        assert code_assembly.resolve_data_quality_enabled(metadata, S3_TABLES_TEMPLATE) is True
        assert code_assembly.resolve_data_quality_enabled(metadata, "glue_job_jdbc.py.j2") is False

    def test_a_missing_pipeline_record_does_not_raise(self) -> None:
        """A provenance-adjacent resolution must never be the reason a step fails."""
        assert code_assembly.resolve_data_quality_enabled(None, S3_TABLES_TEMPLATE) is True


# ---------------------------------------------------------------------------
# The verdict: the half of R6.6 the render alone cannot satisfy
# ---------------------------------------------------------------------------


class TestVerdictModeFollowsTheGate:
    """R6.6 as written — "must not silently start reporting ``enforced``"."""

    @pytest.mark.parametrize("template_name", PORTED_TEMPLATES)
    def test_a_pipeline_that_did_not_opt_in_stays_advisory(self, template_name: str) -> None:
        """The literal requirement, on the four templates R6.1 ported the block to."""
        verdict = derive_quality_verdict(
            template_used=template_name,
            dqdl_rules=SAMPLE_DQDL,
            data_quality_enabled=code_assembly.resolve_data_quality_enabled({}, template_name),
        )
        assert verdict["mode"] == MODE_ADVISORY, (
            f"{template_name}: verdict mode moved to {verdict['mode']} for a pipeline that "
            f"did not opt in. R6.6 forbids exactly this."
        )
        assert verdict["evaluation_point"] == "none"
        assert verdict["data_quality_enabled"] is False

    def test_an_existing_s3_tables_pipeline_stays_enforced(self) -> None:
        """The other direction: the pre-p64 ``enforced`` label is preserved too."""
        verdict = derive_quality_verdict(
            template_used=S3_TABLES_TEMPLATE,
            dqdl_rules=SAMPLE_DQDL,
            target_type="s3_tables",
            data_quality_enabled=code_assembly.resolve_data_quality_enabled({}, S3_TABLES_TEMPLATE),
        )
        assert verdict["mode"] == MODE_ENFORCED
        assert verdict["evaluation_point"] == "before_write"

    @pytest.mark.parametrize("template_name", PORTED_TEMPLATES)
    def test_mode_moves_when_a_human_opts_in(self, template_name: str) -> None:
        """ "so mode moves only on opt-in" — the affirmative half.

        Without this, the requirement would be satisfiable by a verdict that can
        never report ``enforced`` for these templates, which would under-report an
        opted-in pipeline: a job that quarantines rows while the dashboard says
        nothing gated the write. That is the same class of lie in mirror image.
        """
        metadata = {"quality_config": {"data_quality_enabled": True}}
        verdict = derive_quality_verdict(
            template_used=template_name,
            dqdl_rules=SAMPLE_DQDL,
            data_quality_enabled=code_assembly.resolve_data_quality_enabled(metadata, template_name),
        )
        assert verdict["mode"] == MODE_ENFORCED, (
            f"{template_name}: an opted-in pipeline reports {verdict['mode']}, but its "
            f"generated job DOES split rows and quarantine the failures."
        )
        assert verdict["enforcement_scope"] == "row_level"
        assert verdict["evaluation_point"] == "before_write"

    def test_an_absent_fact_falls_back_to_the_default_not_to_off(self) -> None:
        """A state row written before p64 must read exactly as it did before p64.

        ``None`` means "the caller could not establish it". Treating it as ``False``
        would relabel every historical S3 Tables run from ``enforced`` to
        ``advisory`` — R6.6's own failure mode, applied retroactively to rows whose
        jobs demonstrably did quarantine rows.
        """
        pre_p64 = derive_quality_verdict(
            template_used=S3_TABLES_TEMPLATE,
            dqdl_rules=SAMPLE_DQDL,
            target_type="s3_tables",
            data_quality_enabled=None,
        )
        assert pre_p64["mode"] == MODE_ENFORCED
        assert pre_p64["data_quality_enabled"] is True

        for template_name in PORTED_TEMPLATES:
            row = derive_quality_verdict(template_used=template_name, dqdl_rules=SAMPLE_DQDL, data_quality_enabled=None)
            assert row["mode"] == MODE_ADVISORY, f"{template_name}: a pre-p64 row must read as advisory"

    def test_an_opt_in_cannot_make_the_fallback_path_claim_enforced(self) -> None:
        """Fail-safe: no template rendered means no block, whatever was recorded."""
        verdict = derive_quality_verdict(
            template_used=FALLBACK_TEMPLATE_MARKER,
            dqdl_rules=SAMPLE_DQDL,
            data_quality_enabled=True,
        )
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["evaluation_point"] == "none"

    def test_unestablished_provenance_still_wins_over_the_gate(self) -> None:
        """The pre-existing precedence is unchanged: unreadable facts are never ``enforced``."""
        verdict = derive_quality_verdict(
            template_used=S3_TABLES_TEMPLATE,
            dqdl_rules=SAMPLE_DQDL,
            determined_from=FROM_MISSING_CREATE_CODE_ROW,
            data_quality_enabled=True,
        )
        assert verdict["mode"] == MODE_ADVISORY
        assert verdict["evaluation_point"] == "unknown"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_rendered_block_and_the_reported_mode_agree(jinja_env: jinja2.Environment, template_name: str) -> None:
    """The end-to-end invariant O4 is actually about, across all four combinations.

    ``enforced`` must mean the rendered script contains the evaluator and the
    quarantine write, and ``advisory`` must mean it does not. Asserted by rendering
    and deriving from the SAME resolved gate — which is the whole design: one
    resolution, used by the renderer and recorded for the reporter, rather than two
    derivations free to drift (``LGR-eee8ac8-6``).
    """
    for metadata in ({}, {"quality_config": {"data_quality_enabled": True}}):
        enabled = code_assembly.resolve_data_quality_enabled(metadata, template_name)
        rendered = _render(jinja_env, template_name, data_quality_enabled=enabled)
        verdict = derive_quality_verdict(
            template_used=template_name,
            dqdl_rules=SAMPLE_DQDL,
            data_quality_enabled=enabled,
            determined_from=FROM_CREATE_CODE_ROW,
        )
        renders_dq = "EvaluateDataQuality" in rendered and QUARANTINE_WRITE in rendered
        claims_dq = verdict["mode"] == MODE_ENFORCED
        assert renders_dq == claims_dq, (
            f"{template_name} (quality_config={metadata.get('quality_config')}): the rendered "
            f"job {'does' if renders_dq else 'does NOT'} evaluate and quarantine, but the "
            f"verdict reports {verdict['mode']!r}. The script and the dashboard's claim about "
            f"it have diverged — the failure O4 exists to prevent."
        )
