# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Render assertions for p37 C1's load-counts block — one set per template.

p37 task 3.4. These are the fences on what **C1 must not break**. They are
render assertions rather than snapshots, and they exist alongside the goldens
rather than instead of them, because there are **five** templates and only
**four** have goldens.

``glue_job_jdbc_source.py.j2`` HAS NO GOLDEN AND ONE IS DELIBERATELY NOT
INVENTED FOR IT. ``tests/unit/templates/test_golden_script_snapshot.py`` holds
10 goldens across 4 templates; that template has never had one. Manufacturing a
baseline for it here would create a snapshot nobody has reviewed and call it a
fence. So its assertion set below is the ONLY fence it has, which is why this
module treats it as a first-class case rather than an afterthought.

The five assertion sets are the five parametrisations of this module's tests —
``TEMPLATE_SPECS`` has exactly five entries and every test in this file is
parametrised over all of them.

Measured `.count()` totals (re-measured for this task with the project venv
against the on-disk templates, NOT inherited from task 3.3's handoff — they
agree exactly):

    glue_job_glue_catalog.py.j2   2   (3 when dqdl_rules is non-empty — see below)
    glue_job_jdbc.py.j2           2   (3 when dqdl_rules is non-empty — see below)
    glue_job_s3_parquet.py.j2     2   (3 when dqdl_rules is non-empty — see below)
    glue_job_s3_tables.py.j2      3   (4 when dqdl_rules is non-empty —
                                       the quarantine split's _dq_failed_df.count())
    glue_job_jdbc_source.py.j2    2   (3 for the S3_TABLES target, whose write
                                       partial re-reads the table to reconcile;
                                       +1 again if a non-empty ruleset is
                                       rendered, which this module's generator
                                       does not do — see ``_jdbc_source_variants``)

AMENDED BY p64 2b.2 (R6.1/R6.2), DELIBERATELY. The three parenthesised "+1 when
dqdl_rules is non-empty" clauses on the first three lines are new, and they are
not a weakening of the fence — the fence is that **C1 adds no count**, and it
still adds none. What changed is the templates: p64 2b.2 makes all five include
``_partials/_data_quality.py.j2``, whose row-level split calls
``_dq_failed_df.count()`` inside its ``{% if dqdl_rules %}`` arm. That count was
already present in ``glue_job_s3_tables.py.j2`` and is the SAME one, now reached
from four more call sites rather than copied — R6.2 requires an include, not four
copies. The dqdl-conditional totals stay computed rather than hardcoded, so an
unconditional DQ count (one that fired with no ruleset) would still fail here.

`R1.1` is a persistence change, not a computation change, so those totals are
the fence: the counts block reuses the existing ``source_count`` /
``transformed_count`` locals and adds no action of its own.

TWO SUBSTRING TRAPS, AND WHY THE ASSERTIONS BELOW ARE SHAPED AROUND THEM.
``LGR-b46a5bb-4`` (template / ratified) records the governing lesson: the
repository thread-antipattern test uses AST analysis rather than substring
matching, because substring matching "forbids documenting the fence" — it
failed against a docstring that names ``threading.Thread`` on purpose.

  1. ``job.commit()``. ``glue_job_jdbc_source.py.j2``'s empty-source early exit
     carries a ``# NOTE: do NOT call job.commit() here...`` comment explaining
     why it has none (``LGR-0450623-1``, backfilled, records the redundant one
     being removed because the ``finally`` block already commits;
     ``LGR-334667e-1``, backfilled, is the double-commit history behind it).
     A naive ``"job.commit()" not in rendered`` MATCHES that prose. Measured:
     the rendered jdbc_source script contains ``job.commit()`` **three** times
     — twice as prose in that NOTE, once as the real statement in ``finally:``.
     So the assertion is STRUCTURAL: no line in the early-exit region whose
     *stripped* form starts with ``job.commit()``. The prose lines start with
     ``#``. ``test_jdbc_source_empty_exit_documents_the_fence_without_tripping_it``
     asserts the prose is present *and* the structural check still passes, so
     the trap is proven closed rather than assumed closed.

  2. ``.count()`` versus ``count``. An earlier handoff claimed the rendered
     counts block "contains no ``count`` substring at all". That is FALSE and
     was measured to be false: the block contains ``source_count``,
     ``transformed_count``, and the word "counts" in its own comment. What is
     true is that it contains no ``.count()``. A bare-``count`` assertion would
     be a false fence; ``rendered.count(".count()")`` is sound. Note also that
     ``_partials/_load_counts.py.j2``'s own header comment names ``.count()``,
     ``df.cache()`` and ``df.unpersist()`` while documenting that it emits none
     of them — a Jinja comment, so it never reaches the rendered output. That
     is the same trap a third time, and it is why every assertion here runs
     against ``rendered`` and never against template source.

Validates: Requirements 1.1, 1.3, 1.11
"""

from __future__ import annotations

import ast
import os
from collections.abc import Callable, Iterator

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

# Same transform body the other template test modules use.
SAMPLE_TRANSFORM = (
    'df_transformed = df.withColumn("name", col("name").cast(StringType()))\n'
    'df_transformed = df_transformed.withColumnRenamed("old_col", "new_col")'
)

# A non-empty ruleset, so the `{% if dqdl_rules %}` arms of
# `_partials/_data_quality.py.j2` (inline DQ + quarantine split + DQ state
# write) render. Shape only — no rule is evaluated here. Named the s3_tables
# template until p64 2b.2; the block now lives in that partial and all five
# templates include it, so this axis is live for four of them here.
SAMPLE_DQDL = 'Rules = [ RowCount > 0, Completeness "id" >= 0.95 ]'

# The one target type whose jdbc_source write partial re-reads the table.
JDBC_SOURCE_TARGETS = ["S3_TABLES", "GLUE_CATALOG", "JDBC", "RDS_DATABASE", "S3_PARQUET"]

# --- markers emitted by _partials/_load_counts.py.j2 -----------------------
#
# Anchored on rendered statements, never on prose. `step_name` identifies the
# row; the `bool(...)` literals identify the STAGE, which is the only place
# risk N1's distinction (rows handed to the writer vs rows confirmed written)
# survives into the data.
LOAD_METRICS_ROW = '"step_name": "load_metrics"'
WRITE_CONFIRMED_FALSE = "_adi_lc_write_confirmed = bool(False)"
WRITE_CONFIRMED_TRUE = "_adi_lc_write_confirmed = bool(True)"
ROWS_WRITTEN_ZERO = "_adi_lc_rows_written = int(0)"
ROWS_WRITTEN_TRANSFORMED = "_adi_lc_rows_written = int(transformed_count)"

# --- markers emitted by _partials/_runtime_args.py.j2 ---------------------
RUNTIME_ARG_HELPER = 'def _runtime_arg(_name, _default=""):'
RUNTIME_ARG_RUN_ID = '_adi_run_id = _runtime_arg("run_id")'


@pytest.fixture
def jinja_env() -> jinja2.Environment:
    """StrictUndefined, matching ``code_assembly._get_pyspark_jinja_env``."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


class Variant:
    """One rendered script plus the facts that vary with its render context."""

    def __init__(self, label: str, rendered: str, *, expected_count_calls: int, read_mode: str = "n/a"):
        self.label = label
        self.rendered = rendered
        self.expected_count_calls = expected_count_calls
        self.read_mode = read_mode

    def __repr__(self) -> str:  # pragma: no cover - test diagnostics only
        return f"<Variant {self.label}>"


def _s3_source_variants(env: jinja2.Environment, name: str, base_count_calls: int) -> Iterator[Variant]:
    """The variation space for the four S3_UPLOAD-source templates.

    ``checkpoint_enabled`` x ``write_mode`` x ``dqdl_rules`` — the exact three
    variables `R1.11` and task 3.4 name. Until p64 2b.2 only
    glue_job_s3_tables.py.j2 had ``{% if dqdl_rules %}`` arms and the dqdl axis
    was a no-op for the other three; R6.1/R6.2 gave all four the same
    ``_partials/_data_quality.py.j2`` include, so the axis is now live for all
    four and the count expectation below is no longer template-specific.
    """
    template = env.get_template(name)
    for checkpoint_enabled in (False, True):
        for write_mode in ("overwrite", "append", "merge"):
            for dqdl_rules in ("", SAMPLE_DQDL):
                rendered = template.render(
                    transform_block=SAMPLE_TRANSFORM,
                    source_format="json",
                    partition_columns="",
                    merge_key="id",
                    write_mode=write_mode,
                    checkpoint_enabled=checkpoint_enabled,
                    checkpoint_bucket="cp-bucket" if checkpoint_enabled else "",
                    dqdl_rules=dqdl_rules,
                    quarantine_bucket="quarantine-bucket",
                    # p64 2b.4 gated the DQ body on this as well as on
                    # `dqdl_rules`. Passed as True so the `dqdl_rules` axis below
                    # keeps meaning what it says — with the gate absent (it fails
                    # CLOSED), the dqdl=True arm would render no DQ block and the
                    # `+1` count would be silently wrong for the wrong reason.
                    # The gate is NOT a fifth axis here: its own arms are covered
                    # by `test_data_quality_gate.py`, and adding it would double
                    # this generator's variant count to re-assert that a block
                    # which does not render adds no `.count()` call.
                    data_quality_enabled=True,
                )
                # The DQ quarantine split adds `_dq_failed_df.count()` when the
                # ruleset is non-empty. p64 2b.2 REMOVED the
                # `and name == "glue_job_s3_tables.py.j2"` clause that used to
                # be here, deliberately: that clause encoded "only the s3_tables
                # template evaluates DQDL", which R6.1 exists to end. All four
                # S3-source templates now include
                # `_partials/_data_quality.py.j2`, so all four gain the same one
                # count from the same source. The condition stays keyed on
                # `dqdl_rules` — a DQ block that counted with no ruleset
                # configured would still fail `test_no_new_count_call`, which is
                # the property worth keeping.
                expected = base_count_calls
                if dqdl_rules:
                    expected += 1
                yield Variant(
                    f"{name} checkpoint={checkpoint_enabled} write_mode={write_mode} dqdl={bool(dqdl_rules)}",
                    rendered,
                    expected_count_calls=expected,
                )


def _jdbc_source_variants(env: jinja2.Environment, name: str, base_count_calls: int) -> Iterator[Variant]:
    """The variation space for ``glue_job_jdbc_source.py.j2``.

    This template has no ``checkpoint_enabled`` arm, so its axes are
    ``target_type`` x ``read_mode`` x ``write_mode``.

    ONE HALF OF THAT SENTENCE STOPPED BEING TRUE AT p64 2b.2, and the axis was
    still not added — read this before "fixing" it. It used to also say the
    template has no ``dqdl_rules`` arms because "JDBC source does not run inline
    DQ in V1 — see its header comment"; R6.1 put the DQ block on all five
    templates and corrected that header entry, so this template does now have a
    dqdl arm. This generator deliberately keeps rendering it with ``dqdl_rules``
    ABSENT, which exercises the banner-only arm and keeps
    ``base_count_calls=2`` honest. That leaves the ruleset-present arm of THIS
    template uncovered here on purpose: it is covered by
    ``test_data_quality_all_targets.py``, which renders all five templates with
    a non-empty ruleset. Widening the axes here would duplicate that module and
    double this one's variant count for no new fence. The
    ``_runtime_args`` claim still has to hold across all of them, which is
    what makes this template part of the "all five regardless of
    checkpoint_enabled / dqdl_rules / write_mode" assertion rather than an
    exception to it.
    """
    template = env.get_template(name)
    for target_type in JDBC_SOURCE_TARGETS:
        for read_mode in ("full_table", "incremental"):
            for write_mode in ("overwrite", "append", "merge"):
                rendered = template.render(
                    engine="postgres",
                    target_type=target_type,
                    read_mode=read_mode,
                    write_mode=write_mode,
                    partition_columns="",
                    merge_key="id",
                    initial_watermark="2024-01-01T00:00:00Z" if read_mode == "incremental" else "",
                    transform_block=SAMPLE_TRANSFORM,
                )
                # Only the S3_TABLES write partial re-reads the target to
                # reconcile record counts (_write_s3_tables.py.j2).
                expected = base_count_calls + (1 if target_type == "S3_TABLES" else 0)
                yield Variant(
                    f"{name} target={target_type} read_mode={read_mode} write_mode={write_mode}",
                    rendered,
                    expected_count_calls=expected,
                    read_mode=read_mode,
                )


class TemplateSpec:
    """Per-template facts the assertions below are written against."""

    def __init__(
        self,
        name: str,
        *,
        variants: Callable[[jinja2.Environment, str, int], Iterator[Variant]],
        base_count_calls: int,
        write_sentinel: str,
        commits_in_empty_exit: bool,
        has_golden: bool,
    ):
        self.name = name
        self._variants = variants
        self.base_count_calls = base_count_calls
        # The write call site. Used with `rindex` so a template whose write
        # has a fallback arm (s3_tables merge/append) anchors on the last one.
        self.write_sentinel = write_sentinel
        # Whether the empty-source early exit calls job.commit() before
        # sys.exit(0). True for the four S3-source templates; deliberately
        # False for glue_job_jdbc_source.py.j2 (LGR-0450623-1).
        self.commits_in_empty_exit = commits_in_empty_exit
        self.has_golden = has_golden

    def render_variants(self, env: jinja2.Environment) -> list[Variant]:
        return list(self._variants(env, self.name, self.base_count_calls))


TEMPLATE_SPECS: list[TemplateSpec] = [
    TemplateSpec(
        "glue_job_s3_tables.py.j2",
        variants=_s3_source_variants,
        base_count_calls=3,
        write_sentinel="writeTo(target_table)",
        commits_in_empty_exit=True,
        has_golden=True,
    ),
    TemplateSpec(
        "glue_job_glue_catalog.py.j2",
        variants=_s3_source_variants,
        base_count_calls=2,
        write_sentinel="glue_context.write_dynamic_frame.from_catalog",
        commits_in_empty_exit=True,
        has_golden=True,
    ),
    TemplateSpec(
        "glue_job_jdbc.py.j2",
        variants=_s3_source_variants,
        base_count_calls=2,
        write_sentinel="df_transformed.write.jdbc(",
        commits_in_empty_exit=True,
        has_golden=True,
    ),
    TemplateSpec(
        "glue_job_s3_parquet.py.j2",
        variants=_s3_source_variants,
        base_count_calls=2,
        write_sentinel=".parquet(output_path)",
        commits_in_empty_exit=True,
        has_golden=True,
    ),
    # The one with no golden. Its assertion set is its only fence.
    TemplateSpec(
        "glue_job_jdbc_source.py.j2",
        variants=_jdbc_source_variants,
        base_count_calls=2,
        # Per-target dispatch, so the sentinel is resolved per variant below.
        write_sentinel="",
        commits_in_empty_exit=False,
        has_golden=False,
    ),
]

SPEC_IDS = [spec.name for spec in TEMPLATE_SPECS]

JDBC_SOURCE_WRITE_SENTINELS = {
    "S3_TABLES": "writeTo(target_table)",
    "GLUE_CATALOG": "glue_context.write_dynamic_frame.from_catalog",
    "JDBC": "df_transformed.write.jdbc(",
    "RDS_DATABASE": "df_transformed.write.jdbc(",
    "S3_PARQUET": ".parquet(output_path)",
}


def _write_sentinel_for(spec: TemplateSpec, variant: Variant) -> str:
    if spec.write_sentinel:
        return spec.write_sentinel
    target = variant.label.split("target=")[1].split(" ")[0]
    return JDBC_SOURCE_WRITE_SENTINELS[target]


def _statement_line_numbers(rendered: str, statement: str) -> list[int]:
    """Line numbers whose STRIPPED form starts with ``statement``.

    Structural, not substring: a comment mentioning the statement starts with
    ``#`` and is therefore not matched. This is the shape ``LGR-b46a5bb-4``
    (template / ratified) requires — substring matching a fence forbids
    documenting it.
    """
    return [index for index, line in enumerate(rendered.split("\n")) if line.strip().startswith(statement)]


def _early_exit_region(rendered: str) -> tuple[int, int]:
    """Line-number bounds of the empty-source early-exit branch.

    Opens at ``if source_count == 0:`` and closes at the ``sys.exit(0)`` that
    terminates it. All five templates have exactly one of each.
    """
    lines = rendered.split("\n")
    opens = [i for i, ln in enumerate(lines) if ln.strip() == "if source_count == 0:"]
    exits = _statement_line_numbers(rendered, "sys.exit(0)")
    assert len(opens) == 1, f"expected exactly one empty-source guard, found {len(opens)}"
    assert len(exits) == 1, f"expected exactly one sys.exit(0), found {len(exits)}"
    return opens[0], exits[0]


# ---------------------------------------------------------------------------
# Set 1 of 5 concerns — the counts block renders at BOTH positions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_counts_block_renders_at_both_positions(jinja_env: jinja2.Environment, spec: TemplateSpec) -> None:
    """`R1.1`/`R1.3`: two include sites per template, in every render.

    One position is not a partial implementation of C1 — it is a template that
    cannot report ``empty_source``. Position (i) is the only place the counts
    exist before the job is gone; position (ii) is the only place
    ``rows_written`` can be non-zero.
    """
    variants = spec.render_variants(jinja_env)
    assert variants, f"no variants generated for {spec.name}"
    for variant in variants:
        assert variant.rendered.count(LOAD_METRICS_ROW) == 2, (
            f"{variant.label}: expected the load_metrics row at exactly 2 positions, "
            f"found {variant.rendered.count(LOAD_METRICS_ROW)}"
        )


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_empty_source_position_sits_inside_the_early_exit_branch(
    jinja_env: jinja2.Environment, spec: TemplateSpec
) -> None:
    """`R1.3`: position (i) is between the guard and the exit, not near it.

    Outside that branch the block is unreachable on an empty source, and
    ``empty_source`` becomes unrecordable — the job is gone before
    ``transformed_count`` exists.
    """
    for variant in spec.render_variants(jinja_env):
        guard_line, exit_line = _early_exit_region(variant.rendered)
        stage_lines = _statement_line_numbers(variant.rendered, WRITE_CONFIRMED_FALSE)
        assert len(stage_lines) == 1, f"{variant.label}: expected one empty_source stage marker"
        assert guard_line < stage_lines[0] < exit_line, (
            f"{variant.label}: the empty_source counts block must sit inside the "
            f"early-exit branch (guard at {guard_line}, exit at {exit_line}, "
            f"block at {stage_lines[0]})"
        )


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_post_write_position_follows_the_target_write(jinja_env: jinja2.Environment, spec: TemplateSpec) -> None:
    """`R1.1` / risk N1: position (ii) is AFTER the write, not merely after the transform.

    ``transformed_count`` is computed before the write in every template, so a
    block placed before the write would stamp ``write_confirmed`` on a write
    that had not happened. Placement is what gives that field meaning.
    """
    for variant in spec.render_variants(jinja_env):
        sentinel = _write_sentinel_for(spec, variant)
        write_index = variant.rendered.rindex(sentinel)
        post_write_index = variant.rendered.index(WRITE_CONFIRMED_TRUE)
        assert write_index < post_write_index, (
            f"{variant.label}: the post_write counts block must follow the target write ({sentinel!r})"
        )


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_write_confirmed_is_false_at_empty_source_and_true_after_the_write(
    jinja_env: jinja2.Environment, spec: TemplateSpec
) -> None:
    """Risk N1's whole point: the two stages stamp DIFFERENT ``write_confirmed``.

    A template that stamped ``True`` at both positions would pass an
    "appears twice" test while claiming a confirmed write on an empty source.
    ``rows_written`` is asserted alongside it because the pair is what the
    Load_Outcome_Deriver reads: ``empty_source`` requires
    ``rows_written == 0``, and only the post-write site may report
    ``transformed_count``.
    """
    for variant in spec.render_variants(jinja_env):
        assert variant.rendered.count(WRITE_CONFIRMED_FALSE) == 1, variant.label
        assert variant.rendered.count(WRITE_CONFIRMED_TRUE) == 1, variant.label
        assert variant.rendered.count(ROWS_WRITTEN_ZERO) == 1, variant.label
        assert variant.rendered.count(ROWS_WRITTEN_TRANSFORMED) == 1, variant.label
        # And they are paired with the right stage, not merely both present.
        false_index = variant.rendered.index(WRITE_CONFIRMED_FALSE)
        true_index = variant.rendered.index(WRITE_CONFIRMED_TRUE)
        assert variant.rendered.index(ROWS_WRITTEN_ZERO) < false_index, variant.label
        assert variant.rendered.index(ROWS_WRITTEN_TRANSFORMED) < true_index, variant.label
        assert false_index < true_index, f"{variant.label}: the empty_source site must precede the post_write site"


# ---------------------------------------------------------------------------
# Set 2 of 5 concerns — no NEW `.count()` call
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_no_new_count_call(jinja_env: jinja2.Environment, spec: TemplateSpec) -> None:
    """`R1.1` is a persistence change, not a computation change.

    The rendered source must contain exactly the PRE-EXISTING count sites. The
    figures in ``TEMPLATE_SPECS`` were measured against the on-disk templates
    for this task rather than inherited.

    The anchor is ``.count()`` and not a bare ``count``: the rendered counts
    block legitimately contains ``source_count``, ``transformed_count`` and the
    word "counts" in its own comment, so a bare-``count`` assertion would be a
    false fence. The context-dependent totals are all computed rather than
    hardcoded — each of the four S3-source templates gains the DQ quarantine
    split's count with a non-empty ruleset (p64 2b.2: it was s3_tables alone
    until R6.1 gave all five the same include), and
    glue_job_jdbc_source.py.j2 gains the S3_TABLES write partial's
    reconciliation re-read.
    """
    for variant in spec.render_variants(jinja_env):
        actual = variant.rendered.count(".count()")
        assert actual == variant.expected_count_calls, (
            f"{variant.label}: expected {variant.expected_count_calls} `.count()` "
            f"call sites, found {actual}. C1 must add none."
        )


# ---------------------------------------------------------------------------
# Set 3 of 5 concerns — `_runtime_args` renders across the whole space
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_runtime_args_renders_regardless_of_checkpoint_dqdl_and_write_mode(
    jinja_env: jinja2.Environment, spec: TemplateSpec
) -> None:
    """`R1.11`: all FIVE templates, across the whole variation space.

    Task 3.1 removed the four ``{% if %}`` guards and amended
    ``test_checkpoint_templates.py::test_checkpoint_off_still_emits_runtime_arg_helper``
    to cover the narrow ``checkpoint_enabled=False`` case for the four
    S3-source templates. It deliberately left this broader claim here, so this
    test covers the space that one does not: the cartesian product of
    ``checkpoint_enabled`` x ``write_mode`` x ``dqdl_rules`` for those four,
    and ``target_type`` x ``read_mode`` x ``write_mode`` for
    ``glue_job_jdbc_source.py.j2`` — which has no checkpoint or dqdl arms and
    was already unconditional, and is the existing proof the unconditional
    include is safe.

    Without the resolved ``run_id``, the counts row cannot be keyed and C1's
    write silently degrades to the manual-run branch on every run.
    """
    for variant in spec.render_variants(jinja_env):
        assert RUNTIME_ARG_HELPER in variant.rendered, variant.label
        assert RUNTIME_ARG_RUN_ID in variant.rendered, variant.label
        # The counts block resolves pipeline_id and state_table through the
        # same helper; the partial does not resolve them itself.
        assert '_adi_lc_pipeline_id = _runtime_arg("pipeline_id")' in variant.rendered, variant.label
        assert '_runtime_arg("state_table"' in variant.rendered, variant.label


# ---------------------------------------------------------------------------
# Set 4 of 5 concerns — the empty exit's `job.commit()` asymmetry, structurally
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_empty_exit_commit_asymmetry_is_preserved(jinja_env: jinja2.Environment, spec: TemplateSpec) -> None:
    """The four S3-source templates commit in the early exit; jdbc_source does NOT.

    Asserted in both directions so the asymmetry stays a decision rather than
    becoming an accident. ``glue_job_jdbc_source.py.j2``'s ``finally`` block
    already commits and ``sys.exit(0)`` unwinds through it, so a commit here
    would double-commit — ``LGR-0450623-1`` (backfilled) records the redundant
    one being removed and ``LGR-334667e-1`` (backfilled) carries the history.
    Both are the author's own words, unconfirmed history; the held assertion is
    this test.

    STRUCTURAL, per ``LGR-b46a5bb-4`` (template / ratified). A stripped-prefix
    line test, not a substring test — see
    ``test_jdbc_source_empty_exit_documents_the_fence_without_tripping_it``.
    """
    for variant in spec.render_variants(jinja_env):
        guard_line, exit_line = _early_exit_region(variant.rendered)
        commits_in_region = [
            line for line in _statement_line_numbers(variant.rendered, "job.commit()") if guard_line < line < exit_line
        ]
        if spec.commits_in_empty_exit:
            assert len(commits_in_region) == 1, (
                f"{variant.label}: expected exactly one job.commit() statement in the "
                f"empty-source early exit, found {len(commits_in_region)}"
            )
        else:
            assert commits_in_region == [], (
                f"{variant.label}: glue_job_jdbc_source.py.j2's empty-source early exit "
                f"must emit NO job.commit() statement — the finally block already "
                f"commits and sys.exit(0) unwinds through it (LGR-0450623-1). "
                f"Found statements at lines {commits_in_region}."
            )


def test_jdbc_source_empty_exit_documents_the_fence_without_tripping_it(
    jinja_env: jinja2.Environment,
) -> None:
    """The prose that a substring test would trip on is present, and harmless.

    This is the trap closed rather than assumed closed. Measured: the rendered
    jdbc_source script contains the ``job.commit()`` substring THREE times —
    twice inside the ``# NOTE: do NOT call job.commit() here`` comment, once as
    the real ``finally:`` statement. So:

      * a substring test (``"job.commit()" not in region``) FAILS on the prose;
      * the structural test above passes, because comment lines start with
        ``#``;
      * exactly one real ``job.commit()`` statement exists in the whole script,
        and it is in ``finally:`` — after the early exit, not inside it.

    ``LGR-b46a5bb-4`` (template / ratified) is the record: substring matching
    an invariant forbids documenting it. Asserting the documentation is present
    means a future edit that deletes the explanation is visible here too.
    """
    spec = next(s for s in TEMPLATE_SPECS if s.name == "glue_job_jdbc_source.py.j2")
    variant = spec.render_variants(jinja_env)[0]
    rendered = variant.rendered
    guard_line, exit_line = _early_exit_region(rendered)
    region = "\n".join(rendered.split("\n")[guard_line : exit_line + 1])

    # The documentation is there, and it is what a substring test would trip on.
    assert "# NOTE: do NOT call job.commit() here." in region
    assert region.count("job.commit()") == 2, (
        "expected the NOTE's two prose mentions of job.commit() in the early-exit "
        "region — if this changed, re-read the comment before touching the "
        "structural assertion above"
    )

    # The real statement exists exactly once, in `finally:`, after the exit.
    commit_statements = _statement_line_numbers(rendered, "job.commit()")
    assert len(commit_statements) == 1, (
        f"expected exactly one job.commit() STATEMENT in the whole script, found "
        f"{len(commit_statements)} at lines {commit_statements}"
    )
    assert commit_statements[0] > exit_line, (
        "the single job.commit() statement must live in the finally block, after the empty-source early exit"
    )


# ---------------------------------------------------------------------------
# Set 5 of 5 concerns — the cache/unpersist fence, scoped to full-table
# ---------------------------------------------------------------------------


def test_jdbc_source_full_table_emits_no_cache_or_unpersist(jinja_env: jinja2.Environment) -> None:
    """``LGR-0450623-1`` (backfilled): full-table mode emits neither, and C1 adds neither.

    EXTENDS rather than duplicates
    ``test_glue_job_jdbc_source.py::test_full_table_omits_watermark_block_entirely``,
    which already asserts absence for all five target types. What that test
    cannot catch is a future partial adding a ``cache()`` in a *reachable*
    place, because absence is already zero — so this asserts the exact
    occurrence COUNT, over the write-mode axis that test does not vary.

    The fence is scoped to full-table mode SPECIFICALLY. Over-broadening it to
    "no unpersist anywhere" would be a false fence, worse than none: the
    incremental branch legitimately calls ``df.cache()`` and ``df.unpersist()``
    (Req 7.6, so the MAX and the transform do not re-read the source database).
    The record is about a reviewer's ``cache()`` addition to the FULL-TABLE
    path being reverted, and it says so.
    """
    spec = next(s for s in TEMPLATE_SPECS if s.name == "glue_job_jdbc_source.py.j2")
    full_table = [v for v in spec.render_variants(jinja_env) if v.read_mode == "full_table"]
    assert full_table, "expected full-table variants"
    for variant in full_table:
        assert variant.rendered.count("df.cache()") == 0, variant.label
        assert variant.rendered.count("df.unpersist()") == 0, variant.label
        assert variant.rendered.count(".persist(") == 0, variant.label


def test_jdbc_source_incremental_keeps_its_legitimate_cache_and_unpersist(
    jinja_env: jinja2.Environment,
) -> None:
    """The other half of the fence: the incremental branch is NOT covered by it.

    Written as an explicit expectation so nobody "tightens" the assertion above
    into a template-wide ban. Measured occurrence counts in incremental mode:
    one ``df = df.cache()``, and two ``df.unpersist()`` — the empty-source
    guard's and the post-write cleanup's. C1's counts block adds none of them,
    which is what pinning the counts rather than mere presence proves.
    """
    spec = next(s for s in TEMPLATE_SPECS if s.name == "glue_job_jdbc_source.py.j2")
    incremental = [v for v in spec.render_variants(jinja_env) if v.read_mode == "incremental"]
    assert incremental, "expected incremental variants"
    for variant in incremental:
        assert variant.rendered.count("df = df.cache()") == 1, variant.label
        assert variant.rendered.count("df.unpersist()") == 2, variant.label


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_counts_block_adds_no_cache_or_unpersist_to_any_template(
    jinja_env: jinja2.Environment, spec: TemplateSpec
) -> None:
    """The four S3-source templates never cached, and C1 must not start.

    ``_partials/_load_counts.py.j2``'s header comment names ``df.cache()`` and
    ``df.unpersist()`` while documenting that it emits neither. That comment is
    Jinja, so it never reaches the rendered output — which is why this
    assertion runs against ``rendered``. Running it against template source
    would fail on the documentation, the same trap as ``job.commit()``.
    """
    if spec.name == "glue_job_jdbc_source.py.j2":
        pytest.skip("covered with the correct read_mode scoping by the two tests above")
    for variant in spec.render_variants(jinja_env):
        assert "df.cache()" not in variant.rendered, variant.label
        assert "df.unpersist()" not in variant.rendered, variant.label


# ---------------------------------------------------------------------------
# The counts block's indentation is valid Python at BOTH positions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("spec", TEMPLATE_SPECS, ids=SPEC_IDS)
def test_every_variant_parses(jinja_env: jinja2.Environment, spec: TemplateSpec) -> None:
    """The partial's ``indent()`` filter must produce valid Python at both sites.

    Position (i) nests two levels deeper than position (ii) (inside ``try:``
    *and* ``if source_count == 0:``), so the block is emitted at two different
    indents from one partial. Existing ast checks cover each template's other
    axes; this covers the axis C1 introduced.
    """
    for variant in spec.render_variants(jinja_env):
        try:
            ast.parse(variant.rendered)
        except SyntaxError as exc:  # pragma: no cover - diagnostic path
            pytest.fail(f"{variant.label}: rendered script is not valid Python: {exc}")


# ---------------------------------------------------------------------------
# The template with no golden, stated as a fact rather than left implicit
# ---------------------------------------------------------------------------


def test_jdbc_source_is_covered_by_no_golden_snapshot_test() -> None:
    """`glue_job_jdbc_source.py.j2` is the reason this module exists.

    A CORRECTION TO THE SPEC'S OWN WORDING, made by reading the tree rather
    than inheriting it. ``design.md`` C1 and ``tasks.md`` task 3.4 both say
    this template "has no golden at all". That is imprecise in a way that
    matters: ``tests/fixtures/golden_scripts/`` DOES contain five
    ``jdbc_source_*.py`` files, produced by
    ``scripts/_render_jdbc_golden_snapshots.py`` during P17
    (``LGR-d0c8e4f-1``, backfilled, names "JDBC source template + golden
    snapshots"). What is true — and is the fact task 3.4 actually rests on — is
    that **no snapshot test compares this template against them**.
    ``test_golden_script_snapshot.py::GOLDEN_CONFIGS`` and
    ``test_golden_snapshot_prop.py::GOLDEN_MAPPINGS`` each hold exactly the
    same ten entries over four templates, and none names
    ``glue_job_jdbc_source.py.j2``.

    So the template has golden FILES and no golden FENCE, and those five files
    are stale the moment C1 lands — they are the only fixtures in that
    directory p37 task 3.5 does not regenerate, precisely because nothing reads
    them.

    The rule task 3.4 states still holds and is what this test pins: a baseline
    is NOT manufactured for this template. Wiring the five stale files in would
    be worse than inventing one — it would install a **pre-C1** snapshot nobody
    has reviewed and call it a fence. The strong fences for this template are
    the assertion sets above.

    Scanned as text rather than imported, so this test does not couple to two
    modules another task is editing concurrently.
    """
    template_name = "glue_job_jdbc_source.py.j2"
    here = os.path.dirname(__file__)

    for golden_module in ("test_golden_script_snapshot.py", "test_golden_snapshot_prop.py"):
        path = os.path.join(here, golden_module)
        assert os.path.exists(path), f"expected {golden_module} to exist"
        with open(path) as handle:
            source = handle.read()
        assert template_name not in source, (
            f"{golden_module} now renders {template_name}. p37 task 3.4 is explicit "
            f"that a golden baseline must NOT be manufactured for this template — and "
            f"the five pre-existing tests/fixtures/golden_scripts/jdbc_source_*.py "
            f"files are STALE relative to C1, so wiring them in would install an "
            f"unreviewed pre-change snapshot as a fence. Its fence is the render "
            f"assertions in this module."
        )

    # The stale files are named here so their existence is a known fact rather
    # than a surprise to the next reader. Their presence is not asserted as
    # desirable; it is asserted as unreferenced.
    golden_dir = os.path.join(here, "..", "..", "fixtures", "golden_scripts")
    assert os.path.isdir(golden_dir), f"golden_scripts fixture dir not found at {golden_dir}"
    goldens = sorted(f for f in os.listdir(golden_dir) if f.endswith(".py"))
    assert len(goldens) >= 10, f"expected at least the 10 fenced goldens, found {len(goldens)}"
