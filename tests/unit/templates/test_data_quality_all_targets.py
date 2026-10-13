# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R6.1/R6.2/R6.3/R6.4 — the DQ block reaches ALL FIVE templates, via the partial.

WHY THIS MODULE EXISTS. Before p64 2b, the ``EvaluateDataQuality`` call, the
row-level Passed/Failed split, the quarantine write and the DQ report row lived
**only** in ``glue_job_s3_tables.py.j2``. ``requirements.md`` R6.1 requires them
on all five Glue job templates, because R7 (dedup) and R8 (non-printable
characters) build on this block and would otherwise cover one target type of
four while appearing in the wizard for all of them. 2b.1 extracted the block to
``_partials/_data_quality.py.j2``; 2b.2 included it from the other four.

WHAT IT PINS THAT NOTHING ELSE DOES.

  * ``test_dq_quarantine.py`` holds the render-level DQ assertions, but every
    one of them renders ``glue_job_s3_tables.py.j2`` only. Its one cross-template
    test (``test_every_dqdl_applying_template_quarantines``) is a scan of
    template **source files** and is deliberately blind to which templates
    *include* the partial — it asserts the evaluator lives in exactly one file.
    That is the R6.2 half. The R6.1 half — that four more templates actually
    reach it — has no fence without this module.

  * ``test_load_counts_render.py`` renders all five, but its ``dqdl_rules`` axis
    is a by-product: for ``glue_job_jdbc_source.py.j2`` it never passes a
    ruleset at all, so that template's DQ arm would be entirely unrendered by
    any test. This module is where the ruleset-present arm of all five is
    exercised.

ORDERING IS THE PART THAT BREAKS SILENTLY, so it is asserted structurally rather
than by eye. The two stages are not interchangeable: ``'evaluate'`` must run
after the transform and any auto-cast/column selection and BEFORE
``transformed_count`` is computed (it rebinds ``df_transformed`` to the Passed
rows, so a count taken first would report the pre-split figure), and ``'report'``
must run AFTER it, because the counter it publishes as ``rows_passed`` **is**
``transformed_count`` and it reads locals only ``'evaluate'`` assigns. Under
StrictUndefined a mistyped stage fails the render loudly, but a correctly-typed
stage in the wrong PLACE renders fine and lies — hence these assertions.

do-NOT 5 / R6.3 IS ASSERTED ON THE PARSED TREE, not on text. The quarantine
write sits at the same indent as the non-blocking ``try:`` and therefore outside
it: once rules have evaluated with failures, a quarantine-write failure must fail
the job rather than lose rows. Moving it inside that ``try`` swallows the failure
and silently drops the failing rows — the row-loss defect the whole mechanism
exists to prevent — and it is the single most likely casualty of an extraction or
a re-include. A substring test cannot see nesting; ``ast`` can.

SCOPE AFTER p64 2b.4, which gated the block. This module asserts what the DQ
block does WHEN IT RENDERS, so ``_render`` supplies ``data_quality_enabled=True``
and every assertion below reads as "the enabled render, on all five templates".
R6.1 is a statement about availability — the mechanism must be reachable from
every template — and that is exactly what an enabled render measures. The
complementary half, R6.6's "a pipeline that did not opt in renders none of it",
lives in ``test_data_quality_gate.py`` rather than here, because mixing the two
would let a gate stuck permanently off satisfy this module's negative
assertions.

``LGR-819980f-1`` (INVARIANT) IS ASSERTED HERE TOO, on the two JDBC templates.
"JDBC credentials are only ever referenced by Secrets Manager ARN; plaintext
user/password never appear in Glue job arguments or ConnectionProperties" —
plaintext job args are visible in the Glue console, CloudTrail and job-run
history, and this leaked a target DB credential once already. The DQ block
interpolates ``quarantine_bucket`` and ``dqdl_rules`` and emits a quarantine
path, all of which are places a future "make quarantined rows traceable to their
source" change could put a connection string. The scan below is scoped to the
rendered DQ region so it keeps holding as the block grows (2c, 2d and 2e all add
to it).

Validates: Requirements 6.1, 6.2, 6.3, 6.4
"""

from __future__ import annotations

import ast
import os
import re

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = 'df_transformed = df.withColumn("name", col("name").cast(StringType()))'

# Multi-line on purpose. `dqdl_rules` is interpolated into a triple-quoted
# `dq_ruleset` literal, and a real ruleset spans lines — the case an `indent`
# filter on the partial would corrupt (it would add leading spaces to the
# continuation lines and change the DQDL evaluated at runtime, which is why the
# partial carries literal indentation instead). A single-line ruleset cannot
# detect that class of mistake.
SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "id" >= 0.95\n]'

QUARANTINE_BUCKET = "adi-quarantine-bucket"

# --- rendered markers ------------------------------------------------------
EVALUATE_BANNER = "# === Glue Data Quality Evaluation ==="
EVALUATE_END = "# === End Data Quality ==="
REPORT_BANNER = "# === Data Quality counters"
REPORT_END = "# === End Data Quality counters ==="
TRANSFORMED_COUNT = "transformed_count = df_transformed.count()"
QUARANTINE_WRITE = '_dq_failed_df.write.mode("overwrite").parquet(_dq_quarantine_path)'
PASSED_REBIND = "df_transformed = _dq_passed_df"
DQ_STATE_ROW = '"step_name": "data_quality"'
INCLUDE_SITE = '{% include "_partials/_data_quality.py.j2" %}'

S3_SOURCE_TEMPLATES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
]
JDBC_SOURCE_TEMPLATE = "glue_job_jdbc_source.py.j2"
ALL_TEMPLATES = [*S3_SOURCE_TEMPLATES, JDBC_SOURCE_TEMPLATE]

# The two templates `LGR-819980f-1` names. Both resolve their JDBC credential
# from Secrets Manager by ARN at run time: `glue_job_jdbc.py.j2` (a JDBC target)
# through the shared `_write_jdbc.py.j2` partial, by `args['secret_arn']` — it used
# to read args['jdbc_user'] / args['jdbc_password'] until the JDBC-target
# credential fix — and `glue_job_jdbc_source.py.j2` for its source and any JDBC
# target.
JDBC_TEMPLATES = ["glue_job_jdbc.py.j2", JDBC_SOURCE_TEMPLATE]

JDBC_SOURCE_TARGETS = ["S3_TABLES", "GLUE_CATALOG", "JDBC", "RDS_DATABASE", "S3_PARQUET"]


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
    dqdl_rules: str = SAMPLE_DQDL,
    target_type: str = "S3_TABLES",
    data_quality_enabled: bool = True,
) -> str:
    """Render ``name`` with the full variable surface StrictUndefined demands.

    ``data_quality_enabled`` defaults to ``True`` here — p64 2b.4 gated the DQ
    body on it, and every assertion in this module is about what the block does
    once it renders (R6.1's four markers, the stage order, R6.3's fence, R6.4's
    refusal). Those questions are unchanged by the gate; they are simply now
    questions about the ENABLED render. R6.6's own question — that a pipeline
    which did not opt in renders none of it — belongs to
    ``test_data_quality_gate.py``, and the one assertion here that would be
    silently satisfied by a gate stuck off
    (``test_empty_ruleset_renders_only_the_banner``) is paired there with its
    ``dqdl_rules``-present twin so the two cannot be confused.

    Defaulting it to ``True`` rather than threading it per test is deliberate: a
    default of ``False`` would make every R6.1 assertion below vacuously pass by
    asserting absence, which is the failure mode 2b.3's header calls out for
    ``dqdl_rules``.
    """
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
        dqdl_rules=dqdl_rules,
        quarantine_bucket=QUARANTINE_BUCKET,
        column_rename_block="",
        data_quality_enabled=data_quality_enabled,
    )


def _line_of(rendered: str, marker: str, label: str) -> int:
    """0-based index of the single line whose stripped form starts with ``marker``."""
    hits = [i for i, line in enumerate(rendered.split("\n")) if line.strip().startswith(marker)]
    assert len(hits) == 1, f"{label}: expected exactly one {marker!r} line, found {len(hits)}"
    return hits[0]


def _dq_regions(rendered: str, label: str) -> tuple[range, range]:
    """``(evaluate_region, report_region)`` as 0-based line ranges, banner-delimited."""
    ev_start = _line_of(rendered, EVALUATE_BANNER, label)
    ev_end = _line_of(rendered, EVALUATE_END, label)
    rp_start = _line_of(rendered, REPORT_BANNER, label)
    rp_end = _line_of(rendered, REPORT_END, label)
    return range(ev_start, ev_end + 1), range(rp_start, rp_end + 1)


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_every_template_renders_the_full_dq_mechanism(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R6.1: evaluation, the row split, the quarantine write and the report row — all five.

    The four markers are the four things R6.1 enumerates, and they are asserted
    together on purpose: a template that evaluated rules but did not quarantine
    would drop the failing rows, and one that quarantined but wrote no report row
    would leave the dashboard unable to say why rows are missing.
    """
    rendered = _render(jinja_env, template_name)
    ast.parse(rendered)

    for marker in ("EvaluateDataQuality", QUARANTINE_WRITE, PASSED_REBIND, DQ_STATE_ROW):
        assert marker in rendered, (
            f"{template_name}: rendered job is missing {marker!r}. R6.1 requires the "
            f"whole DQ mechanism on all five templates — evaluation, the Passed/Failed "
            f"split, the quarantine write and the DQ report row."
        )
    assert f'_dq_quarantine_bucket = "{QUARANTINE_BUCKET}"' in rendered, (
        f"{template_name}: the configured quarantine bucket did not reach the render"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_block_is_included_twice_and_never_copied(template_name: str) -> None:
    """R6.2: two include sites per template, and no inline evaluator.

    The caller-side half of R6.2. ``test_dq_quarantine.py`` asserts the
    evaluator lives in exactly one FILE; this asserts each template reaches it
    by ``{% include %}``, with both stages named. Read as template source rather
    than rendered output because an include site is Jinja, not Python.
    """
    with open(os.path.join(TEMPLATES_DIR, template_name)) as handle:
        source = handle.read()

    assert source.count(INCLUDE_SITE) == 2, (
        f"{template_name}: expected exactly 2 DQ include sites, found "
        f"{source.count(INCLUDE_SITE)}. R6.2 requires an include rather than a copy, "
        f"and the partial requires BOTH stages — 'report' reads locals only "
        f"'evaluate' assigns."
    )
    for stage in ("'evaluate'", "'report'"):
        assert f"data_quality_stage = {stage}" in source, (
            f"{template_name}: no include site sets data_quality_stage = {stage}"
        )
    assert "EvaluateDataQuality" not in source, (
        f"{template_name}: evaluates DQDL inline. R6.2 requires the mechanism to live "
        f"in _partials/_data_quality.py.j2 alone — extend the include, do not copy it."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_evaluate_precedes_and_report_follows_the_transformed_count(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R6.1: the stage order, which renders fine when wrong and then lies.

    ``'evaluate'`` rebinds ``df_transformed`` to the Passed rows, so a
    ``transformed_count`` taken before it would report the pre-split figure and
    ``rows_passed`` would silently include quarantined rows. ``'report'``
    publishes ``transformed_count`` as ``rows_passed`` and reads
    ``_dq_rows_quarantined`` / ``_dq_quarantine_path``, which only ``'evaluate'``
    assigns — placed first it would raise ``NameError`` at runtime, in a job
    whose data was fine.
    """
    rendered = _render(jinja_env, template_name)
    evaluate, report = _dq_regions(rendered, template_name)
    count_line = _line_of(rendered, TRANSFORMED_COUNT, template_name)

    assert evaluate.stop - 1 < count_line, (
        f"{template_name}: the 'evaluate' stage must end BEFORE "
        f"`{TRANSFORMED_COUNT}` (stage ends at line {evaluate.stop - 1}, count at "
        f"{count_line}) — it rebinds df_transformed to the Passed rows."
    )
    assert count_line < report.start, (
        f"{template_name}: the 'report' stage must start AFTER "
        f"`{TRANSFORMED_COUNT}` (count at {count_line}, stage starts at "
        f"{report.start}) — rows_passed IS transformed_count."
    )
    assert _line_of(rendered, PASSED_REBIND, template_name) in evaluate, (
        f"{template_name}: the Passed-rows rebind must sit inside the 'evaluate' region"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_quarantine_write_stays_outside_the_non_blocking_try(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R6.3 / do-NOT 5, on the parsed tree — the fence most likely to break.

    The ``EvaluateDataQuality`` call is wrapped in a ``try`` whose handlers
    downgrade any failure to ``logger.warning``: a missing ``awsgluedq`` or a DQ
    service error must not fail a job whose data is fine. The quarantine write
    is at the SAME indent as that ``try`` and therefore outside it. Swallowed by
    those handlers, a failed quarantine write would silently drop the failing
    rows.

    Nesting is invisible to a substring test, so the ``try`` is located by its
    contents and the write is tested against its line span.
    """
    rendered = _render(jinja_env, template_name)
    tree = ast.parse(rendered)

    # Identified by its HANDLERS, not by its body: every template wraps its
    # whole job in an outer `try:` whose `finally` commits, and that outer node
    # also contains the EvaluateDataQuality call. The DQ try is the one binding
    # `_dq_err`, which is unique to it.
    non_blocking = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Try) and any(handler.name == "_dq_err" for handler in node.handlers)
    ]
    assert len(non_blocking) == 1, (
        f"{template_name}: expected exactly one try: whose handler binds `_dq_err` "
        f"(the non-blocking DQ evaluation), found {len(non_blocking)}"
    )
    assert "EvaluateDataQuality" in ast.unparse(non_blocking[0].body), (
        f"{template_name}: the `_dq_err` try: no longer wraps the EvaluateDataQuality "
        f"call — the non-blocking scope moved, so this test is measuring the wrong node"
    )
    guarded = non_blocking[0]
    # ast linenos are 1-based; the rendered marker index is 0-based.
    write_line = _line_of(rendered, QUARANTINE_WRITE, template_name) + 1
    assert not (guarded.lineno <= write_line <= (guarded.end_lineno or guarded.lineno)), (
        f"{template_name}: the quarantine write (line {write_line}) is INSIDE the "
        f"non-blocking try (lines {guarded.lineno}-{guarded.end_lineno}). R6.3 / "
        f"design.md do-NOT 5: its handlers downgrade failures to logger.warning, so a "
        f"failed quarantine write there silently drops the failing rows — the row-loss "
        f"defect the quarantine mechanism exists to prevent."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_missing_quarantine_bucket_refuses_rather_than_dropping_rows(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R6.4: the fail-closed arm, preserved verbatim on every template.

    The ``RuntimeError`` is the other half of the row-loss guarantee: with rows
    failing and no bucket configured there is nowhere to put them, and the job
    must stop rather than continue with the Passed split. Asserted as the exact
    rendered text because R6.4 says "preserved verbatim in behaviour" and the
    message is what an operator reads.
    """
    rendered = _render(jinja_env, template_name)
    assert '"Data quality rows failed but no quarantine bucket is configured — "' in rendered, (
        f"{template_name}: the R6.4 RuntimeError message is not rendered verbatim"
    )
    assert '"refusing to silently drop failing rows"' in rendered, (
        f"{template_name}: the R6.4 RuntimeError's second line is not rendered verbatim"
    )

    tree = ast.parse(rendered)
    raises = [
        node for node in ast.walk(tree) if isinstance(node, ast.Raise) and "no quarantine bucket" in ast.unparse(node)
    ]
    assert len(raises) == 1, (
        f"{template_name}: expected exactly one raise for the missing-bucket case, "
        f"found {len(raises)} — R6.4 is a refusal, not a warning"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_empty_ruleset_renders_only_the_banner(jinja_env: jinja2.Environment, template_name: str) -> None:
    """A pipeline with no DQDL rules is unaffected — the golden-compat arm.

    ``code_assembly`` sets ``dqdl_rules`` unconditionally and it is empty
    whenever rule inference produces nothing, so this is the common case, not an
    edge case. The banner still renders (it is outside the ``{% if %}``), which
    is what the regenerated golden snapshots record.
    """
    rendered = _render(jinja_env, template_name, dqdl_rules="")
    ast.parse(rendered)

    assert EVALUATE_BANNER in rendered and EVALUATE_END in rendered, (
        f"{template_name}: the DQ banner must render even with no rules"
    )
    for absent in ("quarantine", "EvaluateDataQuality", "put_metric_data", DQ_STATE_ROW):
        assert absent not in rendered, (
            f"{template_name}: {absent!r} rendered with an EMPTY ruleset. The DQ body "
            f"must stay behind its `{{% if dqdl_rules %}}` guard — a pipeline with no "
            f"rules must be byte-unaffected apart from the banner."
        )


@pytest.mark.parametrize("target_type", JDBC_SOURCE_TARGETS)
def test_jdbc_source_renders_the_dq_block_for_every_target_type(
    jinja_env: jinja2.Environment, target_type: str
) -> None:
    """R6.1 on the template whose include sits after a per-target conditional.

    ``glue_job_jdbc_source.py.j2`` dispatches auto-cast/column selection and the
    write partial on ``target_type``. The DQ include sits between those two, so a
    placement that landed inside one of those ``{% elif %}`` arms would render
    the block for one target and drop it for four — the exact partial-coverage
    shape R6.1 exists to end, and invisible to a single-target test.
    """
    rendered = _render(jinja_env, JDBC_SOURCE_TEMPLATE, target_type=target_type)
    ast.parse(rendered)
    label = f"{JDBC_SOURCE_TEMPLATE} target={target_type}"

    assert "EvaluateDataQuality" in rendered, f"{label}: no DQ evaluation rendered"
    assert QUARANTINE_WRITE in rendered, f"{label}: no quarantine write rendered"
    evaluate, report = _dq_regions(rendered, label)
    count_line = _line_of(rendered, TRANSFORMED_COUNT, label)
    assert evaluate.stop - 1 < count_line < report.start, (
        f"{label}: stage order is wrong (evaluate ends {evaluate.stop - 1}, count "
        f"{count_line}, report starts {report.start})"
    )


@pytest.mark.parametrize("template_name", JDBC_TEMPLATES)
def test_no_credential_reaches_the_dq_block_or_the_quarantine_path(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """``LGR-819980f-1`` (INVARIANT), on the two JDBC templates 2b.2 touches.

    "JDBC credentials are only ever referenced by Secrets Manager ARN; plaintext
    user/password never appear in Glue job arguments or ConnectionProperties."
    Plaintext job args are visible in the Glue console, CloudTrail and job-run
    history — this leaked a target DB credential once already.

    Scoped to the rendered DQ region rather than the whole script, deliberately:
    both templates legitimately resolve a secret by ARN in their write block
    (``glue_job_jdbc.py.j2`` read ``args['jdbc_user']`` /
    ``args['jdbc_password']`` there until the JDBC-target credential fix; the
    whole-script fence for that lives in ``test_pyspark_templates.py``). Neither
    is this test's business. What is its business is that the DQ block — which
    interpolates ``quarantine_bucket`` and ``dqdl_rules``, logs, and composes an
    S3 path — never becomes a second place a credential is written. 2c, 2d and 2e
    all add to this block; this is the fence they will meet.
    """
    rendered = _render(jinja_env, template_name)
    lines = rendered.split("\n")
    evaluate, report = _dq_regions(rendered, template_name)
    region = [lines[i] for i in [*evaluate, *report]]

    forbidden = (
        "jdbc_user",
        "jdbc_password",
        "password",
        "passwd",
        "secret",
        "credential",
        "SECRET_ID",
        "ConnectionProperties",
        "jdbc_properties",
        "jdbc_url",
    )
    for token in forbidden:
        offending = [line.strip() for line in region if token.lower() in line.lower()]
        assert not offending, (
            f"{template_name}: {token!r} appears in the rendered DQ region: "
            f"{offending[:3]}. LGR-819980f-1 is a standing invariant — a credential in "
            f"a job argument, a log line or a quarantine path is visible in the Glue "
            f"console, CloudTrail and job-run history, and has leaked a real credential "
            f"once. Reference the secret by ARN or do not reference it."
        )

    quarantine_path_lines = [line.strip() for line in region if "_dq_quarantine_path = f" in line]
    assert quarantine_path_lines == [
        '_dq_quarantine_path = f"s3://{_dq_quarantine_bucket}/quarantine/{_dq_quarantine_run_id}/"'
    ], (
        f"{template_name}: the quarantine path is no longer bucket + run_id only: "
        f"{quarantine_path_lines}. It must not gain a connection detail for "
        f"'traceability' — the path is recorded on the DQ report row the dashboard "
        f"renders (LGR-819980f-1)."
    )


# ---------------------------------------------------------------------------
# Producer/consumer drift guard for the `checks[]` names
# ---------------------------------------------------------------------------
#
# This partial's 'report' stage appends check entries whose `actual` is an
# absolute COUNT — `float(_adi_np_rows)` (R8.1) and `float(_adi_cast_null_cells)`
# (R9.1). The dashboard's `QualityReportCard` treats any check name it does not
# recognise as a 0-1 RATIO and multiplies it by 100, so an unclassified count is
# rendered as a fabricated percentage: three flagged rows as "300%".
#
# That defect has now happened twice for the same reason. Task 2d.3 fixed it for
# `NonPrintableCharacters`; task 2e.1 added `TypeCastFailures` to this partial two
# tasks later and reintroduced it, because 2e.1 touched no frontend file and so
# never ran `make test-frontend`. A frontend-side assertion cannot close that —
# the tier rules run `make test-frontend` only when `dashboard-react/` changes,
# which is exactly the case a producer-only change is not.
#
# So the guard lives HERE, on the producer side: a change to this partial runs
# `make test-scope DIR=tests/unit/templates/`, and that is the run that must fail
# when a new check name has no unit on the consumer.
#
# NOT COVERED, deliberately: the DQDL rule names, which arrive as
# `_row["Rule"]` from Glue's own outcome rows rather than as literals here.
# `RowCount` is one of those and is classified on the consumer already.

QUALITY_CARD_TSX = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "dashboard-react", "src", "components", "QualityReportCard.tsx"
)

DQ_PARTIAL_SOURCE = os.path.join(TEMPLATES_DIR, "_partials", "_data_quality.py.j2")

#: A literal check name in the partial: `"name": "TypeCastFailures"`. Excludes the
#: DQDL passthrough, whose value is an expression rather than a string literal.
_LITERAL_CHECK_NAME = re.compile(r'"name":\s*"([A-Za-z][A-Za-z0-9_]*)"')

#: The consumer's classification map, parsed rather than transcribed: a copy of
#: the key list here could not detect the consumer dropping one.
_COUNT_MAP_BLOCK = re.compile(r"const COUNT_VALUED_CHECKS:[^=]*=\s*\{(.*?)\};", re.DOTALL)
_COUNT_MAP_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*:\s*['\"]([^'\"]+)['\"]")


def _literal_check_names() -> set[str]:
    with open(DQ_PARTIAL_SOURCE, encoding="utf-8") as fh:
        source = fh.read()
    return set(_LITERAL_CHECK_NAME.findall(source))


def _consumer_count_units() -> dict[str, str]:
    with open(QUALITY_CARD_TSX, encoding="utf-8") as fh:
        source = fh.read()
    block = _COUNT_MAP_BLOCK.search(source)
    assert block is not None, (
        f"`const COUNT_VALUED_CHECKS = {{...}}` was not found in {QUALITY_CARD_TSX}. This guard "
        f"parses that declaration; if it was renamed or reshaped, update this test rather than "
        f"leaving it to pass vacuously — a guard that cannot find its subject protects nothing."
    )
    return {name: unit for name, unit in _COUNT_MAP_KEY.findall(block.group(1))}


def test_the_frontend_file_this_guard_reads_exists() -> None:
    """Fail loudly on a moved component instead of quietly asserting nothing."""
    assert os.path.isfile(QUALITY_CARD_TSX), (
        f"{QUALITY_CARD_TSX} is missing. This guard derives its expectations from that file; "
        f"if the component moved, repoint the path."
    )
    assert os.path.isfile(DQ_PARTIAL_SOURCE), f"{DQ_PARTIAL_SOURCE} is missing."


def test_the_two_halves_of_this_guard_are_both_non_empty() -> None:
    """The paired positive: neither regex may silently match nothing.

    Both sides are parsed out of source with a regex, so either one degrading to
    an empty set would make the coverage assertion below trivially true. This is
    the test that fails first if that happens.
    """
    names = _literal_check_names()
    units = _consumer_count_units()

    assert names, (
        f'No literal `"name": "…"` entries were found in {DQ_PARTIAL_SOURCE}. Either the '
        f"partial stopped emitting named checks or `_LITERAL_CHECK_NAME` no longer matches it."
    )
    assert units, (
        "`COUNT_VALUED_CHECKS` parsed as empty. Either the consumer's map lost every entry or "
        "`_COUNT_MAP_KEY` no longer matches its shape."
    )
    # The two the partial emits today, asserted by name so a silent removal is a
    # failure here rather than a shrinking guard nobody notices.
    assert {"NonPrintableCharacters", "TypeCastFailures"} <= names, (
        f"the partial no longer emits both p64 count checks by name: found {sorted(names)}"
    )


def test_every_literal_check_name_is_classified_by_the_dashboard() -> None:
    """R8.4/R9.1: a count-valued check the consumer cannot classify is rendered as a percentage.

    This is the assertion that would have caught task 2e.1's defect on the gate
    2e.1 itself ran. If it fails, the fix is to add the new name to
    `COUNT_VALUED_CHECKS` in `QualityReportCard.tsx` with the unit the producer
    counts — the noun from the producer's own `failure_reason`, not a guess, and
    not "rows" by default: `TypeCastFailures` counts CELLS.
    """
    names = _literal_check_names()
    units = _consumer_count_units()

    unclassified = sorted(name for name in names if name.lower() not in units)

    assert not unclassified, (
        f"{unclassified} are appended to `checks[]` by {DQ_PARTIAL_SOURCE} but carry no unit in "
        f"`COUNT_VALUED_CHECKS` ({sorted(units)}) in QualityReportCard.tsx. Every one of them has "
        f"an `actual` that is an absolute count, and the consumer's fall-through branch multiplies "
        f"an unclassified value by 100 — so each renders as a percentage of nothing (three flagged "
        f"rows as '300%'). Add the name with the noun the producer counts. This exact defect was "
        f"fixed for NonPrintableCharacters by task 2d.3 and reintroduced by task 2e.1."
    )


def test_the_classification_is_whole_name_and_not_a_substring() -> None:
    """The property the previous anchored regex had, preserved across the map rewrite.

    A hypothetical `NonPrintableCharactersRate` must fall through to the ratio
    branch rather than inherit the count branch from a prefix match. Asserted on
    the consumer's lookup form: an exact key lookup has that property, a
    `startsWith`/`includes`/unanchored-regex test does not.
    """
    with open(QUALITY_CARD_TSX, encoding="utf-8") as fh:
        source = fh.read()

    marker = "hasOwnProperty.call(COUNT_VALUED_CHECKS, identifier.toLowerCase())"
    assert marker in source, (
        f"`formatActualValue` no longer looks the check name up as a WHOLE key "
        f"({marker!r} not found). Matching must stay exact: a substring or prefix test would "
        f"give a future `NonPrintableCharactersRate` — a genuine 0-1 ratio — the count branch, "
        f"reintroducing the fabricated-unit defect from the other direction."
    )
    for loose in ("COUNT_VALUED_CHECKS).some(", "startsWith", "COUNT_VALUED_CHECKS_RE"):
        assert loose not in source, (
            f"{loose!r} appears in QualityReportCard.tsx — the check-name lookup must not be "
            f"relaxed into a prefix or substring match."
        )
