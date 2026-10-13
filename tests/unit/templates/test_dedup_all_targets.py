# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R7.1/R7.2 — the dedup drop reaches all five templates, and is off by default.

WHY THIS MODULE EXISTS. p64 2c.1 gave ``quality_config`` a validated home on the
pipeline record and threaded it into ``template_vars``; 2c.2 gave the wizard a
way to set it. Neither made a generated job do anything: nothing read the
setting. 2c.3 is the read, and this module is its fence.

WHAT IT PINS THAT NOTHING ELSE DOES.

  * ``test_dq_template_vars_contract.py`` asserts that ``quality_config`` is
    assigned unconditionally in ``template_vars`` — the producer side. It says
    nothing about whether any template consumes it, or what it emits.
  * ``test_data_quality_all_targets.py`` renders all five templates with the DQ
    block enabled, but supplies no ``quality_config`` at all, so every one of its
    renders takes the dedup-disabled arm. That is deliberate and it is why the
    enabled arm needs its own module.
  * The golden snapshot modules render with ``quality_config`` unset too, so they
    pin the disabled arm's output by accident. This module pins it on purpose,
    against the pre-dedup tree rather than against a stored blob — see
    ``test_the_disabled_render_is_byte_identical_to_the_pre_dedup_tree``.

THE ORDERING ASSERTION IS THE LOAD-BEARING ONE. A ``dropDuplicates`` call is
correct Python wherever it lands, so a misplacement renders cleanly, compiles
cleanly, and then silently misattributes row loss. Three positions are wrong and
each is wrong for its own reason:

  * AFTER the DQ 'evaluate' split — ``_generate_dqdl_rules`` can emit
    ``Uniqueness "f" >= 0.95``, so duplicate rows can fail a DQDL rule and be
    quarantined before dedup ever sees them. ``rows_quarantined`` then absorbs the
    dedup drops, which is precisely what R7.6 forbids and what 2c.4 needs
    separable.
  * BEFORE the auto-cast/column selection — R7.2's default is "all mapped target
    columns", and only after the select does the frame carry exactly those. Two
    rows identical in every target column but differing in a source-only column
    would both survive, leaving duplicates in the target.
  * AFTER ``transformed_count`` — that count is published as the DQ report's
    ``rows_passed`` and as ``_load_counts``' ``rows_written``, so both would
    describe a frame larger than the one written.

THE EMPTY-LIST CASE IS NOT COSMETIC. ``dropDuplicates([])`` is **not**
``dropDuplicates()``: PySpark passes the subset straight to the JVM, so an empty
subset deduplicates on zero columns and collapses the frame to one row. A test
that only checked "a dropDuplicates call is present" would pass on that. The
assertions below check the argument.

``LGR-819980f-1`` (INVARIANT) IS ASSERTED HERE TOO, on the two JDBC templates.
The partial interpolates exactly one render variable and adds nothing to any
``getResolvedOptions`` key list, so no credential can reach a job argument, a
rendered string or the emitted log. The scan is scoped to the rendered dedup
region so it keeps holding as the block grows.

Validates: Requirements 7.1, 7.2
"""

from __future__ import annotations

import ast
import os
import re

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

DEDUP_PARTIAL = "_partials/_dedup.py.j2"
INCLUDE_SITE = '{%- include "_partials/_dedup.py.j2" %}'

SAMPLE_TRANSFORM = 'df_transformed = df_transformed.withColumn("name", col("name").cast(StringType()))'
SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "id" >= 0.95\n]'
QUARANTINE_BUCKET = "adi-quarantine-bucket"

# --- rendered markers ------------------------------------------------------
DEDUP_BANNER = "# === Deduplication (p64 R7.1/R7.2) ==="
DEDUP_END = "# === End Deduplication ==="
EVALUATE_BANNER = "# === Glue Data Quality Evaluation ==="
TRANSFORMED_COUNT = "transformed_count = df_transformed.count()"
BARE_CALL = "df_transformed = df_transformed.dropDuplicates()"

S3_SOURCE_TEMPLATES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
]
JDBC_SOURCE_TEMPLATE = "glue_job_jdbc_source.py.j2"
ALL_TEMPLATES = [*S3_SOURCE_TEMPLATES, JDBC_SOURCE_TEMPLATE]

# The two templates ``LGR-819980f-1`` names.
JDBC_TEMPLATES = ["glue_job_jdbc.py.j2", JDBC_SOURCE_TEMPLATE]

# Templates whose auto-cast block selects the target columns before this point,
# so a bare ``dropDuplicates()`` coincides with "all mapped target columns".
# Derived from the templates' own text below rather than trusted from here.
SELECTING_TEMPLATES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    JDBC_SOURCE_TEMPLATE,
]

ENABLED = {"dedup_enabled": True, "dedup_columns": ["id", "email"]}
ENABLED_ALL_COLUMNS = {"dedup_enabled": True, "dedup_columns": []}
DISABLED = {"dedup_enabled": False, "dedup_columns": []}


@pytest.fixture
def jinja_env() -> jinja2.Environment:
    """StrictUndefined, matching ``code_assembly._get_pyspark_jinja_env``."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _context(name: str, **overrides: object) -> dict[str, object]:
    """The variable surface StrictUndefined demands, minus keys set to ``None``.

    A key whose value is ``None`` is REMOVED rather than passed as ``None`` — that
    is how a test below expresses "this variable was never set", which is the
    condition the partial's ``is defined`` guard exists to survive.

    ``data_quality_enabled=True`` throughout, so the DQ block renders and the
    ordering assertions have something to be ordered against. Under a False gate
    the 'evaluate' banner still renders (the gate suppresses only the body), so the
    ordering assertions would still work — but the ``Uniqueness``-quarantine hazard
    they are about is only real when DQ actually runs, and asserting order against
    a suppressed block would read as covering a case it does not.
    """
    if name == JDBC_SOURCE_TEMPLATE:
        base: dict[str, object] = {
            "engine": "postgres",
            "target_type": "S3_TABLES",
            "read_mode": "incremental",
            "write_mode": "append",
            "initial_watermark": "2024-01-01T00:00:00Z",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": SAMPLE_TRANSFORM,
            "dqdl_rules": SAMPLE_DQDL,
            "quarantine_bucket": QUARANTINE_BUCKET,
            "data_quality_enabled": True,
        }
    else:
        base = {
            "transform_block": SAMPLE_TRANSFORM,
            "source_format": "csv",
            "source_delimiter": ",",
            "checkpoint_enabled": False,
            "checkpoint_bucket": "",
            "write_mode": "overwrite",
            "merge_key": "id",
            "partition_columns": "",
            "column_rename_block": "",
            "dqdl_rules": SAMPLE_DQDL,
            "quarantine_bucket": QUARANTINE_BUCKET,
            "data_quality_enabled": True,
        }
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def _line_of(rendered: str, marker: str, label: str) -> int:
    """0-based index of the single line whose stripped form starts with ``marker``."""
    hits = [i for i, line in enumerate(rendered.split("\n")) if line.strip().startswith(marker)]
    assert len(hits) == 1, f"{label}: expected exactly one {marker!r} line, found {len(hits)}"
    return hits[0]


def _template_source(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name)) as handle:
        return handle.read()


def _template_body(name: str) -> str:
    """``name``'s source with every Jinja comment removed.

    ``LGR-b46a5bb-4`` (template / ratified) recorded that this repo's template
    fences are substring assertions and that "substring matching forbids
    documenting the fence" — it failed against a docstring that named
    ``threading.Thread`` on purpose. 2c.3 reproduced that live: the
    never-inlined assertion below first scanned the raw source, and four of the
    five templates failed it because the include-site comments each explain what
    a bare ``dropDuplicates()`` keys on. The fence is about the template BODY, so
    the comments come out before the scan rather than the explanation coming out
    of the comments.
    """
    return re.sub(r"\{#.*?#\}", "", _template_source(name), flags=re.DOTALL)


def _without_dedup(source: str) -> str:
    """The template as it stood before 2c.3 — the include site and its comment gone.

    Used to reconstruct the pre-dedup tree for the byte-identity assertion, rather
    than storing a snapshot of it. A stored blob would have to be regenerated for
    every unrelated template change and would then stop measuring anything; this
    derives the comparison from the live file, so it keeps holding.
    """
    stripped = re.sub(r"\{#- p64 R7\.1/R7\.2 .*?-#\}\n", "", source, flags=re.DOTALL)
    return stripped.replace(INCLUDE_SITE + "\n", "")


# ---------------------------------------------------------------------------
# R7.1 — off by default, and off costs nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_dedup_is_absent_when_quality_config_is_never_set(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R7.1: an unset ``quality_config`` disables dedup — it never enables it.

    The partial reads the variable behind ``is defined and``, the same idiom
    ``dqdl_rules`` and ``data_quality_enabled`` use, so an omitted variable is
    silent rather than a render error. What matters is the DIRECTION of that
    silence, and this pins it: a render path that forgets the variable emits no
    dedup at all. A gate whose absence enabled the drop would silently start
    deleting rows from every pipeline that never asked.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=None))
    ast.parse(rendered)

    assert "dropDuplicates" not in rendered, (
        f"{template_name}: dropDuplicates rendered with quality_config UNSET. The "
        f"guard must fail CLOSED — R7.1 requires the default to be off so no "
        f"existing pipeline's behaviour changes."
    )
    assert DEDUP_BANNER not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_dedup_is_absent_when_the_flag_is_false(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R7.1: an explicit ``False`` is the same as absent, and the pair is not vacuous.

    Paired with the enabled render on the same template, so a partial that emitted
    nothing under every input could not pass both.
    """
    off = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=DISABLED))
    ast.parse(off)
    assert "dropDuplicates" not in off

    on = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    assert "dropDuplicates" in on, (
        f"{template_name}: dedup did not render with dedup_enabled=True either, so "
        f"the negative assertion above is measuring an inert partial rather than a gate"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_disabled_render_is_byte_identical_to_the_pre_dedup_tree(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R7.1 at its strongest: a non-opted-in pipeline's job does not change at all.

    Behaviour-preservation would be satisfied by emitting a comment. This asserts
    the stronger property — zero rendered characters — because it is the one a test
    can check cheaply and because it is what keeps the golden snapshots and every
    other render-comparing module unaffected by 2c.3.

    It is also the fence on the whitespace arithmetic the partial's header
    describes: the include site's ``{%-`` strips the newline above it and the
    partial's own ``{%- if %}`` / ``{%- endif -%}`` restore exactly one when
    enabled. Get either side wrong and the disabled arm gains or loses a blank
    line, which this catches and no behavioural test would.

    The comparison is against the template with the 2c.3 lines removed, derived
    live rather than stored, so it does not need regenerating when something else
    in the template changes.
    """
    source = _template_source(template_name)
    assert source.count(INCLUDE_SITE) == 1, (
        f"{template_name}: expected exactly 1 dedup include site, found "
        f"{source.count(INCLUDE_SITE)}. One is the contract — the partial has a "
        f"single emission point and no stage variable, so a second include would "
        f"deduplicate twice."
    )

    pre_dedup_env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader({template_name: _without_dedup(source)}),
                jinja2.FileSystemLoader(TEMPLATES_DIR),
            ]
        ),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    before = pre_dedup_env.get_template(template_name).render(**_context(template_name, quality_config=None))

    for label, config in (("unset", None), ("explicit False", DISABLED)):
        after = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=config))
        assert after == before, (
            f"{template_name}: the {label} render is NOT byte-identical to the "
            f"pre-dedup template ({len(after)} chars vs {len(before)}). R7.1 requires "
            f"that no existing pipeline's behaviour changes, and 2c.3 holds itself to "
            f"the stronger no-rendered-bytes reading. Almost certainly a whitespace "
            f"control slip — see _partials/_dedup.py.j2's header."
        )


# ---------------------------------------------------------------------------
# R7.2 — which columns identify a duplicate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_configured_columns_reach_the_drop_as_a_list_literal(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R7.2: the configured columns, emitted as a quoted list rather than spliced in.

    ``tojson`` rather than bare interpolation, the rule ``column_rename_block``
    already follows (do-NOT 17). Every entry has passed the identifier allowlist
    twice before reaching here — at the API boundary and again in
    ``resolve_quality_config`` — so this is the third layer, and it is what makes a
    name that somehow bypassed both into a Python string rather than syntax.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    ast.parse(rendered)

    assert 'df_transformed = df_transformed.dropDuplicates(["id", "email"])' in rendered, (
        f"{template_name}: the configured dedup columns did not reach the emitted dropDuplicates call as a list literal"
    )
    assert rendered.count("dropDuplicates") == 1, (
        f"{template_name}: {rendered.count('dropDuplicates')} dropDuplicates calls "
        f"rendered, expected 1 — a second one would drop rows twice"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_an_empty_column_list_emits_the_no_argument_call(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R7.2's default, and the one emission that must NOT be an empty list.

    ``dropDuplicates([])`` is not ``dropDuplicates()``. PySpark hands the subset to
    the JVM unchanged, so an empty subset deduplicates on zero columns and collapses
    the whole frame to a single row — silent, catastrophic, and valid Python. 2c.2
    settled that ``[]`` and an omitted ``dedup_columns`` both mean "all mapped
    target columns", which is what the no-argument call already does, so the empty
    case needs no sentinel; what it does need is this assertion, because the
    plausible wrong rendering compiles.
    """
    rendered = jinja_env.get_template(template_name).render(
        **_context(template_name, quality_config=ENABLED_ALL_COLUMNS)
    )
    ast.parse(rendered)

    assert BARE_CALL in rendered, (
        f"{template_name}: an empty dedup_columns did not render the no-argument dropDuplicates() call"
    )
    assert "dropDuplicates([])" not in rendered, (
        f"{template_name}: rendered dropDuplicates([]), which deduplicates on ZERO "
        f"columns and collapses the frame to one row. The empty list must become the "
        f"no-argument call — see _partials/_dedup.py.j2's header."
    )


def test_the_no_argument_default_follows_the_column_selection_where_one_exists() -> None:
    """R7.2's "all mapped target columns" claim, checked against the templates.

    The bare call keys on every column of the frame it is handed, so the claim
    holds exactly where a target-column select has already run. Three templates
    select and two do not, and the partial's header says so rather than
    generalising — this pins the split so the header cannot quietly become wrong.

    Derived from the template text rather than asserted from the list at the top of
    this module: the membership is computed and then compared, so a template gaining
    or losing its select fails here instead of passing against a stale list.
    """
    selecting = [
        name for name in ALL_TEMPLATES if "df_transformed.select(" in _template_source(name).split(INCLUDE_SITE)[0]
    ]
    assert selecting == SELECTING_TEMPLATES, (
        f"templates selecting target columns before the dedup include: {selecting}, "
        f"expected {SELECTING_TEMPLATES}. A NEW entry means a template gained a "
        f"select, which is an improvement — update the partial's header, which "
        f"currently names the two without one. A MISSING entry means R7.2's default "
        f"silently stopped meaning 'all mapped target columns' for that template."
    )


# ---------------------------------------------------------------------------
# Placement — correct Python in the wrong place, which is the real hazard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_dedup_precedes_the_dq_split_and_the_transformed_count(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The placement decision, asserted structurally because it renders fine when wrong.

    Dedup must land BEFORE the DQ 'evaluate' stage: that stage can quarantine rows
    a ``Uniqueness`` rule failed, and duplicates are exactly such rows, so dedup
    placed after it would have its drops counted as quality failures.
    ``rows_quarantined`` would absorb them and R7.6's requirement that dedup drops
    be attributed separately would be unsatisfiable — 2c.4 depends on this order.

    And before ``transformed_count``, which the DQ report publishes as
    ``rows_passed`` and ``_load_counts`` records as ``rows_written``. Dedup after
    that count would make both describe a larger frame than the one written.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))

    banner = _line_of(rendered, DEDUP_BANNER, template_name)
    end = _line_of(rendered, DEDUP_END, template_name)
    call = _line_of(rendered, "df_transformed = df_transformed.dropDuplicates(", template_name)
    evaluate = _line_of(rendered, EVALUATE_BANNER, template_name)
    count = _line_of(rendered, TRANSFORMED_COUNT, template_name)

    assert banner < call < end, (
        f"{template_name}: the dropDuplicates call must sit inside its own banner "
        f"region (banner {banner}, call {call}, end {end})"
    )
    assert end < evaluate, (
        f"{template_name}: dedup must end BEFORE the DQ 'evaluate' stage (dedup ends "
        f"at {end}, evaluate starts at {evaluate}). After it, duplicate rows failing a "
        f"Uniqueness rule are quarantined first and the dedup drops are folded into "
        f"rows_quarantined — which R7.6 forbids and 2c.4 cannot undo."
    )
    assert call < count, (
        f"{template_name}: dedup must precede `{TRANSFORMED_COUNT}` (call at {call}, "
        f"count at {count}) — that count is published as rows_passed and recorded as "
        f"rows_written."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_dedup_sits_at_the_job_body_indent_and_not_inside_a_narrower_block(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The drop runs on every row of the frame, not inside a loop or a conditional.

    Written because the failure it catches was real: the first draft closed the
    partial's inner ``{% if %}`` with the whitespace-stripping form, which ate the
    four spaces of indentation as well as the newline and emitted the call at column
    zero inside the job's ``try:``. That one did not compile, so ``ast.parse``
    caught it — but the neighbouring mistake, landing the call at a DEEPER indent
    inside the auto-cast ``for field in _target_schema`` loop, compiles perfectly
    and deduplicates once per target column. Nesting is invisible to a substring
    test; the parsed tree is what can see it.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    tree = ast.parse(rendered)

    lines = rendered.split("\n")
    call_line = _line_of(rendered, "df_transformed = df_transformed.dropDuplicates(", template_name)
    assert lines[call_line].startswith("    df_transformed"), (
        f"{template_name}: the dropDuplicates call is not at the job body's 4-space indent: {lines[call_line]!r}"
    )

    # The only enclosing statement may be the job's own outer `try:` — the one
    # whose `finally` commits. Anything else (a `for`, an `if`, a nested `try`)
    # means the drop is conditional or repeated.
    #
    # Only compound statements are considered, and only their statement-list
    # fields. A blanket `getattr(node, field)` over everything `ast.walk` yields
    # raises: `IfExp.body` and `Lambda.body` are single expressions, not lists,
    # and the templates contain both.
    compound = (ast.For, ast.AsyncFor, ast.While, ast.If, ast.Try, ast.With, ast.AsyncWith)
    enclosing = []
    for node in ast.walk(tree):
        if not isinstance(node, compound):
            continue
        blocks = [getattr(node, field, None) or [] for field in ("body", "orelse", "finalbody")]
        blocks += [handler.body for handler in getattr(node, "handlers", None) or []]
        for block in blocks:
            if any(isinstance(child, ast.stmt) and child.lineno == call_line + 1 for child in block):
                enclosing.append(node)

    assert enclosing, (
        f"{template_name}: the dropDuplicates call has no enclosing compound "
        f"statement at all — it should sit inside the job's outer try:, so this "
        f"assertion is no longer measuring what it claims"
    )
    assert all(isinstance(node, ast.Try) for node in enclosing), (
        f"{template_name}: the dropDuplicates call is nested inside "
        f"{[type(node).__name__ for node in enclosing]} — it must sit directly in the "
        f"job's outer try body, so it runs exactly once over the whole frame."
    )


# ---------------------------------------------------------------------------
# R6.2's shape, one partial over: included, never copied
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_drop_lives_in_the_partial_and_is_never_inlined(template_name: str) -> None:
    """One definition, five callers — the same rule R6.2 sets for the DQ block.

    Read as template source rather than rendered output, because an include site is
    Jinja and not Python. The negative half is what matters: a template that emitted
    its own ``dropDuplicates`` would drift from the other four the moment the
    partial changed, which is the partial-coverage shape 2b.1 removed for DQ.

    Scanned over ``_template_body`` — comments stripped — for the reason that
    helper records: the include-site comments legitimately name the call while
    explaining what its empty-argument form keys on.
    """
    assert _template_source(template_name).count(INCLUDE_SITE) == 1, (
        f"{template_name}: expected exactly 1 dedup include site, found "
        f"{_template_source(template_name).count(INCLUDE_SITE)}"
    )
    assert "dropDuplicates" not in _template_body(template_name), (
        f"{template_name}: emits dropDuplicates inline. R7.1's mechanism lives in "
        f"{DEDUP_PARTIAL} alone — extend the include, do not copy it."
    )


def test_the_partial_is_the_only_file_holding_the_drop() -> None:
    """The one-definition half, measured over the whole template directory.

    Bodies, not raw sources — five templates' comments mention the call and the
    partial's own header discusses it at length. Deriving the set and then
    comparing it means a sixth file acquiring the call fails here rather than
    passing against a name list.
    """
    holders = []
    for root, _dirs, files in os.walk(TEMPLATES_DIR):
        for name in sorted(files):
            if not name.endswith(".j2"):
                continue
            path = os.path.join(root, name)
            relative = os.path.relpath(path, TEMPLATES_DIR)
            with open(path) as handle:
                body = re.sub(r"\{#.*?#\}", "", handle.read(), flags=re.DOTALL)
            if "dropDuplicates" in body:
                holders.append(relative)

    assert holders == [DEDUP_PARTIAL.replace("/", os.sep)], (
        f"template files whose BODY contains dropDuplicates: {holders}, expected only {DEDUP_PARTIAL}"
    )


# ---------------------------------------------------------------------------
# LGR-819980f-1 — no credential in the rendered dedup region
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", JDBC_TEMPLATES)
def test_no_credential_reaches_the_rendered_dedup_region(jinja_env: jinja2.Environment, template_name: str) -> None:
    """``LGR-819980f-1`` (INVARIANT), on the two templates it names.

    "JDBC credentials are only ever referenced by Secrets Manager ARN; plaintext
    user/password never appear in Glue job arguments or ConnectionProperties" —
    plaintext job args are visible in the Glue console, CloudTrail and job-run
    history, and this leaked a target DB credential once already. A dedup key is a
    column name, and the plausible future change that would break this is a
    well-meant "log which connection this dedup applied to". Scoped to the rendered
    region so it keeps holding as 2c.4 adds counters here.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    lines = rendered.split("\n")
    region = "\n".join(
        lines[_line_of(rendered, DEDUP_BANNER, template_name) : _line_of(rendered, DEDUP_END, template_name) + 1]
    )

    for forbidden in ("jdbc_user", "jdbc_password", "jdbc_url", "password", "secret_arn", "SECRET_ID"):
        assert forbidden not in region, (
            f"{template_name}: {forbidden!r} appears in the rendered dedup region. "
            f"LGR-819980f-1 is an invariant: a dedup key is a column name, never a "
            f"connection detail, however useful it would be for traceability."
        )


# ---------------------------------------------------------------------------
# R7.6 — the drop is attributed, not folded into quarantine or the transform
# ---------------------------------------------------------------------------
#
# 2c.3 put the drop in a position where a count either side of it brackets the
# dedup drops and nothing else. 2c.4 takes those counts and carries the number
# onto the ``(run_id, "load_metrics")`` state row. What these assertions defend
# is the ATTRIBUTION, which is the whole of R7.6: a number that is right but
# sourced from the wrong pair of counts is the defect, not a lesser version of
# it. The two wrong pairs are both available in scope at that point and both
# render as clean Python.

DEDUP_PRE_COUNT = "_adi_dedup_rows_before = df_transformed.count()"
DEDUP_DROPPED = "_adi_dedup_dropped = _adi_dedup_rows_before - df_transformed.count()"
DEDUP_FIELD = '"rows_deduplicated": _adi_lc_rows_deduplicated,'
DEDUP_VALUE_ZERO = "_adi_lc_rows_deduplicated = int(0)"
DEDUP_VALUE_LOCAL = "_adi_lc_rows_deduplicated = int(_adi_dedup_dropped)"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_drop_is_bracketed_by_its_own_two_counts(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R7.6: a count immediately before the drop and one immediately after it.

    The ordering is the assertion. A pre-count that landed after the
    ``dropDuplicates`` call, or a post-count before it, still renders and still
    parses — it just reports zero forever.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    ast.parse(rendered)

    pre = _line_of(rendered, DEDUP_PRE_COUNT, template_name)
    drop = _line_of(rendered, "df_transformed = df_transformed.dropDuplicates(", template_name)
    dropped = _line_of(rendered, DEDUP_DROPPED, template_name)

    assert pre < drop < dropped, (
        f"{template_name}: expected pre-count < dropDuplicates < post-count, got "
        f"{pre} / {drop} / {dropped}. The two counts exist to bracket the drop; in "
        f"any other order they measure something else and R7.6 reports a wrong number."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_dedup_count_is_not_derived_from_the_two_wrong_brackets(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R7.6's actual content: neither existing count may be reused for this.

    Both are in scope and both are the obvious saving, so this is the fence. Each
    is wrong in its own direction:

      * ``source_count`` is pre-transform, so ``source_count - <post-drop>`` folds
        TRANSFORM drops into the dedup number.
      * ``transformed_count`` is post-quarantine — the DQ 'evaluate' stage
        reassigns ``df_transformed = _dq_passed_df`` between the drop and that
        count — so ``<pre-drop> - transformed_count`` folds QUARANTINE drops into
        it. That is R7.6's own defect with the labels swapped: the requirement
        exists because ``rows_read - rows_written`` "would attribute deduplication
        to quarantine or to the transform", and this would attribute quarantine to
        deduplication instead.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    dropped_line = rendered.split("\n")[_line_of(rendered, "_adi_dedup_dropped =", template_name)]

    for wrong in ("source_count", "transformed_count"):
        assert wrong not in dropped_line, (
            f"{template_name}: the dedup count is derived from {wrong!r} "
            f"({dropped_line.strip()!r}). {wrong} does not bracket the drop — "
            f"source_count is pre-transform and transformed_count is post-quarantine — "
            f"so this reports a misattributed number, which is exactly what R7.6 forbids."
        )

    assert dropped_line.strip() == DEDUP_DROPPED, (
        f"{template_name}: expected the dedup count to be the difference of its own "
        f"two counts, got {dropped_line.strip()!r}"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_dedup_count_reaches_the_load_metrics_row_at_both_sites(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R7.6: the number is persisted, not merely logged.

    Both ``_load_counts`` sites carry the field, so a run that exits early and a
    run that completes are both decomposable. ``int(...)`` at the value site is
    ``LGR-9d1c0e4-1``: a bare ``except`` once swallowed a Decimal serialisation
    error and dropped a metric every run, so no Spark/py4j numeric may arrive at
    DynamoDB unwrapped.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    ast.parse(rendered)

    assert rendered.count(DEDUP_FIELD) == 2, (
        f"{template_name}: expected rows_deduplicated on both load_metrics rows, "
        f"found {rendered.count(DEDUP_FIELD)}. p37 writes the row at two sites "
        f"(empty_source and post_write); a field on only one of them makes the "
        f"early-exit run undecomposable."
    )
    assert rendered.count("_adi_lc_rows_deduplicated = int(") == 2, (
        f"{template_name}: every rows_deduplicated value site must be wrapped in "
        f"int() per LGR-9d1c0e4-1, so no float or py4j numeric reaches DynamoDB"
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_empty_source_site_states_zero_rather_than_reading_the_local(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R7.6 at the early-exit site, where the dedup local does not exist.

    ``empty_source`` sits inside the zero-row early exit, ABOVE the dedup block,
    so ``_adi_dedup_dropped`` is unbound there. Reading it would raise NameError
    inside a ``try`` that logs and swallows — dropping the entire metrics row and
    taking ``rows_read``/``rows_written`` with it. Zero rows were read, so zero
    were deduplicated, and a stage-conditional literal says so without depending
    on a local. ``_load_counts`` has no defaults by design, so this is a literal
    rather than an optional variable read.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    lines = rendered.split("\n")

    zero = _line_of(rendered, DEDUP_VALUE_ZERO, template_name)
    local = _line_of(rendered, DEDUP_VALUE_LOCAL, template_name)
    defined = _line_of(rendered, DEDUP_DROPPED, template_name)

    assert zero < defined, (
        f"{template_name}: the literal-zero site is at line {zero}, after the dedup "
        f"block at {defined}. The zero site is the early exit and must precede the "
        f"block — if it does not, the stage mapping has moved."
    )
    assert lines[zero].startswith(" " * 8), (
        f"{template_name}: expected the empty_source value site at indent 8 (inside "
        f"try: + if source_count == 0:), got {lines[zero]!r}"
    )
    assert defined < local, (
        f"{template_name}: rows_deduplicated is read at line {local} but "
        f"_adi_dedup_dropped is only assigned at {defined} — NameError inside a "
        f"best-effort try, which silently drops the whole load_metrics row."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_no_dedup_field_is_written_when_dedup_is_off(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R7.1 applied to 2c.4's field: off emits the key not at all, not as a zero.

    A zero would change every non-opted-in pipeline's rendered job, breaking the
    stored golden scripts and giving up the no-rendered-bytes standard 2c.3 holds
    itself to. It would also conflate two different claims — "dedup ran and
    removed nothing" with "dedup did not run" — which is a smaller version of the
    misattribution R7.6 is about. Readers must tolerate absence regardless: every
    row written before 2c.4 lacks the key.

    Paired with the enabled render so it cannot pass on an inert template.
    """
    for label, config in (("unset", None), ("explicit False", DISABLED)):
        off = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=config))
        ast.parse(off)
        assert "rows_deduplicated" not in off, (
            f"{template_name}: rows_deduplicated rendered with dedup {label}. Off must "
            f"cost zero rendered bytes — a zero here changes every existing pipeline's "
            f"job and conflates 'deduplicated nothing' with 'did not deduplicate'."
        )
        assert "_adi_dedup" not in off, f"{template_name}: a dedup local leaked into the {label} render"

    on = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=ENABLED))
    assert "rows_deduplicated" in on, (
        f"{template_name}: rows_deduplicated did not render with dedup enabled either, "
        f"so the negative assertions above are measuring an inert field rather than a gate"
    )
