# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The type-cast failure count (p64 R9.1/R9.3, design C10, task 2e.1).

WHAT THE DEFECT IS, BECAUSE THE ASSERTIONS ONLY MAKE SENSE AGAINST IT. Spark's
``cast`` returns NULL on failure rather than raising. A source value of
``"twenty-five"`` cast to ``int`` becomes NULL, and from that point on it is
indistinguishable from a row whose source genuinely had no value. The DQ
``Completeness`` rule counts both identically, which is how a bad value passes as
missing data. R9.1 also corrects the reliability report's account of its own Test
7: what caught ``"twenty-five"`` was NOT type validation — no type rule exists
anywhere in the generated job — it was the auto-cast block nulling the value and
the NULL tripping ``Completeness "age" >= 0.95``. R9.3 requires the two be
distinguishable in whatever is reported.

THE ONE TEST TO READ FIRST IS
``test_the_count_is_taken_before_the_conversion_that_produces_the_nulls``. The
placement is not a preference — it is the entire mechanism. The auto-cast loop
rebinds each column to its converted value, so one statement later the
pre-conversion value is gone and ``col(name).isNotNull()`` is False for exactly
the cells being counted. A count taken after the loop is not merely less tidy; it
is silently always zero. That is the failure mode this module exists to prevent,
and it is invisible in any render that does not execute.

THE SECOND IS ``test_the_predicate_requires_a_non_null_source``. Counting "the
converted value is NULL" would count source NULLs too and reproduce the exact
conflation R9.3 forbids, one layer up. The predicate has to be a conjunction.

THE SURFACE CHOICE, since the task brief left it open and it is the thing a later
reader will want the reason for: the finding lands as a ``checks[]`` entry on the
data quality report (2d.3's shape), NOT on the ``(run_id, "load_metrics")`` row
that 2c.4's ``rows_deduplicated`` went to. R7.6's decomposition is an arithmetic
identity over ROWS, and a conversion NULL is not a term in it — the row is
written, in full, with one field nulled. Putting a cell count on that row would
let a reader summing its terms over-count losses that never happened. The
positive reason is R9.3: the place a SOURCE null is reported is the
``Completeness`` rule's outcome in ``checks[]``, so "distinguishable" is only
satisfied if the correction is legible in the same document.

CONSEQUENTLY 2d.3's THREE CONSTRAINTS APPLY VERBATIM, and each has its test here:
status is ``warning`` and no fourth value is introduced; the entry is counted in
none of ``rules_passed`` / ``rules_failed`` / ``rules_total``; it moves neither
``overall_status`` nor ``rows_quarantined``. Like 2d.2's, this is largely a suite
about what a flag does NOT do.

REACH IS NARROWER THAN 2d.2's AND THAT IS ASSERTED RATHER THAN ASSUMED. Two
include sites, both S3 Tables arms, because the count is only computable where an
auto-cast block exists.
``test_the_three_templates_without_an_auto_cast_block_have_no_measurement`` pins
the negative half, and
``test_the_report_entry_is_guarded_on_the_measurement_having_happened`` pins the
consequence: the DQ partial renders on all five templates, so its append has to
tolerate the local being absent or it loses the whole report row inside the
enclosing ``except``. An unmeasured run reports NO entry rather than zero —
absent and zero are different claims, the same line 2c.4 drew.

Expectations are derived from R9.1/R9.3 and design C10, not from current output.
Everything here fails against the pre-2e.1 tree: the partial did not exist.

Validates: Requirements 9.1, 9.3
"""

from __future__ import annotations

import ast
import os
import re

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

CHECK_PARTIAL = "_partials/_cast_null_check.py.j2"
DQ_PARTIAL = "_partials/_data_quality.py.j2"
INCLUDE_SITE = '{%- include "_partials/_cast_null_check.py.j2" %}'

CHECK_BANNER = "# === Type-cast failure count (p64 R9.1/R9.3) ==="
CHECK_END = "# === End Type-cast failure count ==="
CELLS_LOCAL = "_adi_cast_null_cells"
DETAIL_LOCAL = "_adi_cast_null_detail"
REPORT_ENTRY = "_dq_cast_check"
CAST_LOOP_BANNER = "# === Auto-cast"
PRESENCE_GUARD = f'if "{CELLS_LOCAL}" in locals():'

#: The guard both sites share. Written once because the two must stay identical —
#: see ``test_the_two_guards_are_the_same_expression``.
GUARD = 'quality_config is mapping and quality_config.get("cast_null_check_enabled") is true'

JDBC_SOURCE_TEMPLATE = "glue_job_jdbc_source.py.j2"
S3_TABLES_TEMPLATE = "glue_job_s3_tables.py.j2"

#: The templates whose auto-cast block uses the cached ``_target_schema`` local,
#: which is the precondition for taking this count at all.
MEASURING_TEMPLATES = [S3_TABLES_TEMPLATE, JDBC_SOURCE_TEMPLATE]

#: The other three, each for a stated reason rather than by omission.
#:
#:   ``glue_job_glue_catalog.py.j2`` — casts inside a ``try`` whose target schema
#:       comes from a runtime ``create_dynamic_frame.from_catalog`` read, so there
#:       is no local to read before the loop and the block is skipped entirely
#:       when the target table does not exist yet. This is the ONE real R9.3 gap,
#:       deferred and currently unowned; design O8 carries it. NOT task 2e.2's
#:       residual — 2e.2's text is R9.2, whose two clauses do not cover widening
#:       this reach. Earlier revisions named 2e.2 here, which was wrong.
#:   ``glue_job_jdbc.py.j2`` / ``glue_job_s3_parquet.py.j2`` — contain no
#:       conversion at all, so no conversion can null a value. An absence of the
#:       phenomenon, not a coverage gap.
NON_MEASURING_TEMPLATES = [
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
]

ALL_TEMPLATES = MEASURING_TEMPLATES + NON_MEASURING_TEMPLATES

SAMPLE_TRANSFORM = "df_transformed = df"
SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "id" >= 0.95\n]'
QUARANTINE_BUCKET = "adi-quarantine-bucket"

_OFF = {"dedup_enabled": False, "dedup_columns": [], "nonprintable_check_enabled": False}
ENABLED = {**_OFF, "cast_null_check_enabled": True}
DISABLED = {**_OFF, "cast_null_check_enabled": False}


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
    is how the tests below express "this variable was never set", which is the
    condition the partial's ``is mapping`` guard exists to survive.

    ``data_quality_enabled=True`` by default so the DQ block renders and the
    report assertions have something to assert against.
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
    base["quality_config"] = ENABLED
    base.update(overrides)
    return {key: value for key, value in base.items() if value is not None}


def _template_source(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, name)) as handle:
        return handle.read()


def _partial_source(name: str) -> str:
    with open(os.path.join(TEMPLATES_DIR, *name.split("/"))) as handle:
        return handle.read()


def _body(source: str) -> str:
    """``source`` with every Jinja comment removed.

    ``LGR-b46a5bb-4`` (template / ratified): this repo's template fences are
    substring assertions, and "substring matching forbids documenting the fence".
    The include-site comments explain what the count does, so a scan of the raw
    source would match its own documentation. Same helper, same reason, as
    ``test_nonprintable_check_all_targets.py``'s and ``test_dedup_all_targets.py``'s.
    """
    return re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)


def _line_of(rendered: str, marker: str, label: str) -> int:
    """0-based index of the single line whose stripped form starts with ``marker``."""
    hits = [i for i, line in enumerate(rendered.split("\n")) if line.strip().startswith(marker)]
    assert len(hits) == 1, f"{label}: expected exactly one {marker!r} line, found {len(hits)}"
    return hits[0]


def _without_check(source: str) -> str:
    """``source`` as it stood before 2e.1 — the include site and its comment gone.

    Derived live rather than stored, following the two sibling modules: a stored
    snapshot would need regenerating for every unrelated template change and would
    then stop measuring anything.

    It does NOT need to strip anything from ``_data_quality.py.j2``: that partial's
    ``checks[]`` append is behind the same ``quality_config`` guard, so with the
    count disabled it renders nothing — which is the property the byte-identity
    assertion is checking, not an exemption from it.
    """
    stripped = re.sub(r"\{#- p64 R9\.1/R9\.3 \(design C10\).*?-#\}\n", "", source, flags=re.DOTALL)
    return stripped.replace(INCLUDE_SITE + "\n", "")


def _region(rendered: str) -> str:
    """The rendered count block, banner to end marker inclusive."""
    start = rendered.index(CHECK_BANNER)
    end = rendered.index(CHECK_END, start) + len(CHECK_END)
    return rendered[start:end]


def _region_code(rendered: str) -> str:
    """``_region`` with the emitted ``#`` comment lines removed.

    ``LGR-b46a5bb-4`` again, one layer in from ``_body``: the fences below are about
    the emitted STATEMENTS, and the block's comments necessarily explain what it
    does not do ("Rows were kept", "counts cells, not rows"). Scanning the raw
    region makes those fences match their own documentation — the failure 2c.3 hit
    twice and 2d.2 twice more. The comment is load-bearing for a reader of the
    generated job, so the scan narrows rather than the comment going quiet.
    """
    return "\n".join(line for line in _region(rendered).split("\n") if not line.strip().startswith("#"))


#: ``from pyspark.sql.types import <real> as <alias>`` — the count block imports the
#: two type classes under ``_adi_``-prefixed aliases, so an arm-name comparison
#: against the loop has to resolve them first.
_TYPE_IMPORT_ALIAS = re.compile(r"^\s*from pyspark\.sql\.types import (\w+) as (\w+)\s*$", re.MULTILINE)

#: ``isinstance(<something>.dataType, <TypeName>)`` — the shape both the count block
#: and the auto-cast loop use to pick a conversion arm.
_DISPATCH_ARM = re.compile(r"isinstance\([\w.]+,\s*(\w+)\)")


def _cast_loop_region(rendered: str) -> str:
    """The rendered auto-cast loop, its banner to its end marker, statements only.

    Bounded from ``CAST_LOOP_BANNER`` because the count block is included BEFORE the
    loop, so the first occurrence of that banner is always past ``CHECK_END``.
    """
    start = rendered.index(CAST_LOOP_BANNER)
    end = rendered.index("# === End Auto-cast", start)
    return "\n".join(line for line in rendered[start:end].split("\n") if not line.strip().startswith("#"))


def _dispatch_arms(region: str) -> list[str]:
    """The type names this region's ``isinstance`` arms dispatch on, in order.

    Aliases introduced by the region's own ``from pyspark.sql.types import X as Y``
    lines are resolved back to ``X``, so the count block's ``_adi_cast_date_type``
    and the loop's ``DateType`` compare equal. A region with no alias imports (the
    loop) is unaffected, because the map is simply empty.
    """
    aliases = {alias: real for real, alias in _TYPE_IMPORT_ALIAS.findall(region)}
    return [aliases.get(name, name) for name in _DISPATCH_ARM.findall(region)]


def _entry_region(rendered: str) -> str:
    """The rendered ``checks[]`` append for the count, statements only."""
    start = rendered.index(f"{REPORT_ENTRY} = {{")
    end = rendered.index(f"_dq_checks.append({REPORT_ENTRY})", start)
    return "\n".join(line for line in rendered[start:end].split("\n") if not line.strip().startswith("#"))


# ---------------------------------------------------------------------------
# Off by default, and off costs nothing (R6.6 / 2b.4's posture)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_count_is_absent_when_quality_config_is_never_set(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """An unset ``quality_config`` disables the count — it never enables it.

    The render must also SUCCEED, which is the half worth stating. Under
    ``StrictUndefined`` the obvious ``is defined and`` guard answers True for a
    foreign ``missing`` sentinel and then raises on the read behind it — the failure
    ``_dedup.py.j2``'s header records against five templates at once. An
    ``is mapping`` guard cannot: it is an isinstance check, so it consults no
    sentinel.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=None))

    assert CHECK_BANNER not in rendered
    assert CELLS_LOCAL not in rendered
    assert REPORT_ENTRY not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
@pytest.mark.parametrize(
    "config",
    [DISABLED, {}, {"cast_null_check_enabled": "true"}, {"cast_null_check_enabled": 1}, "on", None, []],
    ids=["explicit-false", "empty-map", "string-true", "int-one", "scalar", "null", "list"],
)
def test_the_count_is_absent_for_everything_that_is_not_an_explicit_true(
    jinja_env: jinja2.Environment, template_name: str, config: object
) -> None:
    """``is true``, not truthiness — so ``"true"`` and ``1`` do not opt in either.

    Defence in depth behind ``resolve_quality_config``, which normalises a
    non-boolean to ``False`` before the template sees it. Both layers matter because
    they fail in the same direction: nobody gets a count they did not ask for, and
    nothing about the render depends on which layer caught it.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=config))

    assert CHECK_BANNER not in rendered
    assert REPORT_ENTRY not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_disabled_render_is_byte_identical_to_the_pre_count_tree(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R6.6 at its strongest: zero rendered characters, so no existing job changes.

    Behaviour preservation would be satisfied by emitting a comment. The stronger
    property is asserted for the reason ``_dedup.py.j2`` established it: it keeps
    the stored ``tests/fixtures/golden_scripts/`` snapshots and every other
    render-comparing module unaffected by 2e.1, and it is the only fence on the
    whitespace arithmetic.

    THE TWO INCLUDE SITES ARE NOT COPY-IDENTICAL and this is the test that catches
    it. ``glue_job_s3_tables.py.j2`` has a blank line at its insertion point and
    ``glue_job_jdbc_source.py.j2`` does not, so the surrounding newlines differ by
    one. Copying either site to the other adds or drops a blank line, which no
    behavioural test would notice.

    Both DQ postures, because the ``checks[]`` append lives inside the DQ arm of
    ``_data_quality.py.j2`` — so DQ-on is the case where a stray byte from that
    second site could appear.
    """
    source = _template_source(template_name)
    expected_sites = 1 if template_name in MEASURING_TEMPLATES else 0
    assert source.count(INCLUDE_SITE) == expected_sites, (
        f"{template_name}: expected {expected_sites} include site(s), found "
        f"{source.count(INCLUDE_SITE)}. The count is only computable where an "
        f"auto-cast block using the cached _target_schema exists, and a second site "
        f"in one template would count the frame twice and pay for two Spark actions."
    )

    pre_count_env = jinja2.Environment(
        loader=jinja2.ChoiceLoader(
            [
                jinja2.DictLoader({template_name: _without_check(source)}),
                jinja2.FileSystemLoader(TEMPLATES_DIR),
            ]
        ),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )

    for dq_enabled in (True, False):
        before = pre_count_env.get_template(template_name).render(
            **_context(template_name, quality_config=None, data_quality_enabled=dq_enabled)
        )
        for label, config in (("unset", None), ("explicit False", DISABLED)):
            after = jinja_env.get_template(template_name).render(
                **_context(template_name, quality_config=config, data_quality_enabled=dq_enabled)
            )
            assert after == before, (
                f"{template_name} (data_quality_enabled={dq_enabled}): the {label} "
                f"render is NOT byte-identical to the pre-2e.1 template "
                f"({len(after)} chars vs {len(before)}). Almost certainly a "
                f"whitespace control slip — see {CHECK_PARTIAL}'s header, and note "
                f"the two include sites need DIFFERENT surrounding blank lines."
            )


# ---------------------------------------------------------------------------
# C10's mechanism: the count is taken where the answer is still available
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_count_is_taken_before_the_conversion_that_produces_the_nulls(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """THE mechanism assertion. Placement here is correctness, not tidiness.

    The auto-cast loop rebinds each column to its converted value
    (``df_transformed = df_transformed.withColumn(field.name, …)``). After that the
    pre-conversion value is gone, and a predicate asking "was the source non-null"
    evaluates False for precisely the cells being counted — because the cell IS
    null now. So a count moved below the loop does not merely lose precision; it
    reports zero on every run, for every input, and nothing about the rendered job
    looks wrong.

    That is a silent-always-zero failure invisible to every other test in this
    module, which is why the ordering is pinned rather than left to the header.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    banner_line = _line_of(rendered, CHECK_BANNER, template_name)
    end_line = _line_of(rendered, CHECK_END, template_name)
    cast_lines = [i for i, line in enumerate(rendered.split("\n")) if line.strip().startswith(CAST_LOOP_BANNER)]
    assert cast_lines, f"{template_name}: no auto-cast block found, so this template should not measure"

    assert end_line < min(cast_lines), (
        f"{template_name}: the count block ends at line {end_line} but the auto-cast "
        f"block starts at {min(cast_lines)}. The count MUST complete before the "
        f"conversion rebinds the columns, or it reads the converted value and "
        f"reports zero on every run."
    )
    assert banner_line < end_line


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_predicate_requires_a_non_null_source(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R9.3's whole point, as the shape of one expression.

    Counting "the converted value is NULL" would count source NULLs too and
    reproduce the conflation R9.3 forbids one layer up — a number that claims to
    separate the two while being the sum of both. The predicate has to be a
    conjunction of "the source held something" AND "the conversion produced
    nothing".
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    assert "_adi_cast_source.isNotNull() & _adi_cast_result.isNull()" in code, (
        f"{template_name}: the counting predicate is not the conjunction of a "
        f"non-null source and a null conversion result. Either half alone counts "
        f"the wrong thing — dropping the isNotNull() term makes this a second "
        f"completeness rule, which is the defect R9.1 records."
    )


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_all_three_conversion_arms_are_mirrored(jinja_env: jinja2.Environment, template_name: str) -> None:
    """``to_date`` and ``to_timestamp`` null an unparseable value exactly as ``cast`` does.

    The auto-cast loop has three arms — a date arm, a timestamp arm, and a plain
    ``cast`` — and all three yield NULL for input they cannot represent. Measuring
    only the ``cast`` arm would report a date column's unparseable values as source
    NULLs, which is the same defect in a narrower place.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    for conversion in ("to_date(", "to_timestamp(", ".cast("):
        assert conversion in code, (
            f"{template_name}: the count block does not mirror the {conversion!r} arm "
            f"of the auto-cast loop. All three arms null an unrepresentable value, so "
            f"all three must be counted or a column's failures read as source NULLs."
        )


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_arm_conditions_match_the_loop_and_not_just_the_arm_bodies(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The count RE-DERIVES the three arms; this pins the derivation to its original.

    ``test_all_three_conversion_arms_are_mirrored`` above asserts that all three
    conversion CALLS appear in the count block. That is necessary and not
    sufficient: it says nothing about the ``isinstance`` conditions that choose
    between them, so the loop could be changed to dispatch on a fourth type, or the
    two could be reordered so a ``TimestampType`` took the date arm, and the token
    assertion would still pass while the count silently measured a different
    conversion than the one that runs.

    The count cannot observe the real conversion — it has to run BEFORE it, which is
    design C10's whole mechanism and the reason a post-loop measurement reads zero.
    Re-derivation is therefore structural, not a shortcut, and the drift it admits
    has to be closed by an assertion rather than by removing the duplication.

    Compared as an ORDERED list because ``isinstance`` dispatch is first-match:
    equal sets in a different order is a real behaviour change, not a formatting
    one. Aliases are resolved by ``_dispatch_arms`` so the count block's
    ``_adi_cast_date_type`` compares against the loop's ``DateType``.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    count_arms = _dispatch_arms(_region_code(rendered))
    loop_arms = _dispatch_arms(_cast_loop_region(rendered))

    assert count_arms == ["DateType", "TimestampType"], (
        f"{template_name}: the count block dispatches on {count_arms}, expected "
        f"['DateType', 'TimestampType'] followed by a plain cast fallback. Pinned "
        f"literally so a silent third arm cannot appear on one side only."
    )
    assert count_arms == loop_arms, (
        f"{template_name}: the count block dispatches on {count_arms} but the "
        f"auto-cast loop it mirrors dispatches on {loop_arms}. The count runs BEFORE "
        f"the conversion (design C10) so it re-derives these arms rather than "
        f"observing them — if the loop's arms change, this block must change in the "
        f"same commit or the count measures a conversion that does not happen."
    )


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_count_costs_exactly_one_spark_action(jinja_env: jinja2.Environment, template_name: str) -> None:
    """One pass over the frame regardless of how many columns convert.

    Every per-column sum goes into a SINGLE ``agg``, collected once. A per-column
    ``count()`` would be correct and would scale the cost with the schema width,
    which on a wide table is the difference between one pass and dozens. Paid only
    inside the enabled arm — a pipeline that has not opted in pays nothing, which is
    the same bargain ``_dedup.py.j2``'s two counts strike.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    assert code.count(".agg(") == 1, f"{template_name}: {code.count('.agg(')} agg calls, expected 1"
    assert code.count(".collect()") == 1, (
        f"{template_name}: {code.count('.collect()')} collect calls, expected 1 — "
        f"the Spark action count is the cost being bounded here."
    )
    assert ".count()" not in code, (
        f"{template_name}: the block calls .count(), which is a second action. The "
        f"per-column sums belong in the single agg."
    )


# ---------------------------------------------------------------------------
# Reach: two sites, and the consequence of that being narrower than the DQ partial
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_count_renders_on_both_s3_tables_arms(jinja_env: jinja2.Environment, template_name: str) -> None:
    """Both arms use the cached ``_target_schema``, which is what lets one partial serve both."""
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert CHECK_BANNER in rendered
    assert f"{CELLS_LOCAL} = 0" in rendered
    assert CHECK_END in rendered


@pytest.mark.parametrize("template_name", NON_MEASURING_TEMPLATES)
def test_the_three_templates_without_an_auto_cast_block_have_no_measurement(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The negative half of reach, asserted so it is a decision rather than an oversight.

    Glue Catalog casts inside a ``try`` whose schema is read from the catalog at
    runtime, so there is no local to read before the loop and the whole block is
    skipped when the target table does not exist yet — deferred and currently
    unowned, recorded as design O8 (NOT task 2e.2's residual; 2e.2 is R9.2). The
    JDBC and Parquet templates convert nothing at all, so nothing can be counted,
    which makes them vacuously fine rather than gaps — so Glue Catalog is the only
    template on which R9.3 is genuinely unsatisfied.

    This test is what makes widening the reach later a deliberate change: adding an
    include site to any of these three fails here loudly.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert CHECK_BANNER not in rendered
    assert f"{CELLS_LOCAL} = 0" not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_is_guarded_on_the_measurement_having_happened(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """The cost of that narrower reach, and the reason it is not a landmine.

    The DQ partial renders on all five templates, so on the three that do not
    measure, the ``checks[]`` append would dereference a name nothing defined. The
    whole report write sits inside ``except Exception as _ddb_err``, so the
    consequence would not be a crash — it would be the SILENT LOSS OF THE ENTIRE DQ
    REPORT ROW. That is strictly worse than the reporting gap this task closes, and
    it is the failure ``test_nonprintable_check_all_targets.py``'s guard-pairing
    test warns about for the sibling feature.

    So the append tests ``locals()`` membership first. An unmeasured run produces NO
    entry, which is also the honest answer: ABSENT and ZERO are different claims,
    and "no conversion nulled a value" is not something a run that never looked can
    say. Task 2c.4 drew the same line for an omitted key versus a written zero.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert PRESENCE_GUARD in rendered, (
        f"{template_name}: the checks[] append is not guarded on {CELLS_LOCAL} "
        f"existing. Without it the three non-measuring templates raise NameError "
        f"inside the report write's except and lose the whole report row silently."
    )
    guard_line = _line_of(rendered, PRESENCE_GUARD, template_name)
    entry_line = _line_of(rendered, f"{REPORT_ENTRY} = {{", template_name)
    assert guard_line < entry_line, f"{template_name}: the presence guard must precede the entry it protects."


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_needs_the_dq_report_row(jinja_env: jinja2.Environment, template_name: str) -> None:
    """With DQ off there is no report row, so the finding is logged and not persisted.

    The row is written only inside ``_data_quality.py.j2``'s
    ``data_quality_enabled and dqdl_rules`` arm, and it is a single ``put_item`` on
    a PutItem-only role (``LGR-9e76249-3``), so this count cannot write its own
    without clobbering. Pinned so a later reader meets the gap here rather than in
    production, and so closing it — a row of its own under its own ``step_name`` —
    is a deliberate change that fails this test loudly.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, data_quality_enabled=False))

    assert REPORT_ENTRY not in rendered, (
        "the checks[] append rendered with DQ off, which means it is outside the DQ "
        "gate and therefore writing to a report row that does not exist"
    )
    if template_name in MEASURING_TEMPLATES:
        assert CHECK_BANNER in rendered, "the measurement half must still render"


# ---------------------------------------------------------------------------
# It counts; it does not divert. 2d.3's three constraints, inherited.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_count_diverts_no_rows(jinja_env: jinja2.Environment, template_name: str) -> None:
    """Every row continues to the target, with its NULL in place.

    What the region must not contain: a rebinding of ``df_transformed`` (which is
    what diversion looks like — ``df_transformed = _dq_passed_df`` in the DQ
    partial), any write call, and any of the quarantine mechanism's own names. The
    frame the auto-cast loop receives is the frame that entered this block.

    THE FENCE IS THE MECHANISM, NOT THE WORD — ``LGR-b46a5bb-4`` for the fourth
    time in p64. Scanning for the word matches the block's own comments and then its
    log text, which is the most useful sentence in it for an operator because it is
    what says the rows still reached the target. A fence that forbids saying so
    trades a real signal for a cheaper assertion, so it names identifiers instead,
    which is the stronger claim anyway.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    assert "df_transformed =" not in code, (
        "the count region rebinds df_transformed. It counts cells and diverts "
        "nothing — no row may be removed or replaced here."
    )
    assert ".write" not in code
    for mechanism in ("_dq_quarantine", "_dq_failed_df", "_dq_passed_df", "quarantine/"):
        assert mechanism not in code, (
            f"the count region reaches {mechanism!r}, part of the quarantine "
            f"mechanism. A conversion NULL loses no row, so it has nothing to "
            f"contribute to a row-diversion path."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_nothing_from_the_count_reaches_rows_quarantined(jinja_env: jinja2.Environment, template_name: str) -> None:
    """2d.3's third constraint: ``_dq_rows_quarantined`` stays the DQ split's own number.

    This is the R7.6 misattribution 2c.4 spent a task closing, in a new place. A
    cell count added to a row counter would make ``rows_read - rows_written`` stop
    decomposing, and would do it while looking like extra rigour. Asserted by
    pinning the ONLY two statements allowed to write that local, both of which
    predate 2e.1.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    writers = [line.strip() for line in rendered.split("\n") if re.match(r"\s*_dq_rows_quarantined\s*(=|\+=)", line)]
    assert writers == ["_dq_rows_quarantined = 0", "_dq_rows_quarantined = _dq_failed_df.count()"], (
        f"{template_name}: rows_quarantined is written by {writers}. A conversion "
        f"NULL loses no row, so it has no term to contribute to that counter."
    )
    assert f"_dq_rows_quarantined + {CELLS_LOCAL}" not in rendered
    assert f"{CELLS_LOCAL} + _dq_rows_quarantined" not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_leaves_overall_status_and_the_rule_counts_alone(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """2d.3's first and second constraints: the verdict is the DQDL rules' verdict.

    ``overall_status`` is derived from ``_dq_fail_count``. A count that gates
    nothing must not change it, or a run whose declared rules all passed reports
    ``fail`` — which would also move ``shared/quality_verdict.py``'s reported mode
    and hand ``derive_load_outcome`` a term it has no name for.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert '"overall_status": "pass" if _dq_fail_count == 0 else "fail",' in rendered, (
        "the overall_status expression changed. It must stay derived from "
        "_dq_fail_count over the DQDL rule outcomes alone."
    )
    for counter in ("rules_passed", "rules_failed", "rules_total", "root_cause"):
        line = next(line for line in rendered.split("\n") if f'"{counter}"' in line)
        assert CELLS_LOCAL not in line, (
            f"{counter} reads {CELLS_LOCAL}. The count is not a DQDL rule, so it is "
            f"counted in none of the rules_* figures and sets no root_cause."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_status_is_warning_and_introduces_no_new_value(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """``warning`` is the third value ``QualityCheck.status`` admits. There is no fourth.

    2d.3 pinned that at the contract (``QUALITY_CHECK_SCHEMA``) and in the frontend
    type, and ``fail`` is reserved for an outcome that removed rows from the frame —
    which this did not. Because the schema already admits ``warning``, an
    unconstrained ``name`` and a numeric ``actual``, this entry needs NO wire-shape
    change and no dashboard change: it is a new value in a shape 2d.3 already
    declared.

    The clean-run arm emits ``pass``, also already admitted, so that "the count ran
    and found nothing" is distinguishable from "the count never ran" — which is the
    same distinction the ``locals()`` guard preserves one level up.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert f'"status": "warning" if {CELLS_LOCAL} > 0 else "pass",' in rendered
    statuses = set(re.findall(r'"status": "(\w+)"', rendered))
    assert statuses <= {"pass", "fail", "warning"}, (
        f"{template_name}: emitted check statuses {sorted(statuses)} include a value "
        f"outside QualityCheck.status's three. 2d.3 forbids adding a fourth."
    )
    assert '"status": "fail"' not in _entry_region(rendered)


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_joins_the_list_already_being_built(jinja_env: jinja2.Environment, template_name: str) -> None:
    """``LGR-9e76249-3``: one ``put_item``, so the entry appends rather than writing a row.

    The Glue runtime role is PutItem-only on the state table with no UpdateItem, so
    a second writer overwrites the DQ report rather than merging with it. The append
    therefore sits between the DQDL rule loop and the ``_dq_report`` literal, and
    the number of ``put_item`` calls does not change.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    append_line = _line_of(rendered, f"_dq_checks.append({REPORT_ENTRY})", template_name)
    report_line = _line_of(rendered, "_dq_report = {", template_name)
    rule_loop_line = _line_of(rendered, "_dq_checks.append(_check)", template_name)

    assert rule_loop_line < append_line < report_line, (
        f"{template_name}: the entry must be appended after the DQDL rule loop and "
        f"before the _dq_report literal that reads _dq_checks."
    )
    assert rendered.count("_tbl.put_item(") == 1, (
        f"{template_name}: {rendered.count('_tbl.put_item(')} put_item calls on the "
        f"report row. LGR-9e76249-3 — a second writer clobbers rather than merges."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_failure_reason_says_the_source_was_not_empty(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R9.3 is a reporting requirement, so the wording carries it.

    ``failure_reason`` is the only field on the entry that can state the
    distinction: that these cells HAD a source value, and that the completeness rule
    cannot tell them from cells that did not. Without that sentence the entry is a
    bare number beside a ``Completeness`` outcome and a reader has no reason to
    connect them. The column breakdown is included for the same reason the skip
    records in R5 thread their detail — a count nobody can act on is a count nobody
    reads.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    entry = _entry_region(rendered)

    assert '"failure_reason"' in entry
    assert DETAIL_LOCAL in entry, (
        f"{template_name}: the entry does not carry {DETAIL_LOCAL}, so the report "
        f"states a total with no indication of which columns produced it."
    )
    assert "not" in entry and "empty sources" in entry, (
        f"{template_name}: the failure_reason does not state that these cells had a "
        f"source value. That sentence is the whole of R9.3 on the wire."
    )


# ---------------------------------------------------------------------------
# Structure, and the guard-pairing hazard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", MEASURING_TEMPLATES)
def test_the_count_sits_at_the_job_body_indent(jinja_env: jinja2.Environment, template_name: str) -> None:
    """4 spaces — the ``try:`` body of the job, not nested inside a narrower block.

    The whitespace-stripping closing tag that eats the trailing newline also eats
    the indentation behind it if used on the opening side, which produces a
    statement at column 0 inside a ``try:`` and a job that does not compile.
    ``_dedup.py.j2``'s header records that being written first and caught by
    ``ast.parse``; this asserts the outcome directly.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    lines = rendered.split("\n")

    for marker in (CHECK_BANNER, CHECK_END, f"{CELLS_LOCAL} = 0"):
        idx = _line_of(rendered, marker, template_name)
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        assert indent == 4, f"{template_name}: {marker!r} at indent {indent}, expected 4"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_enabled_render_is_valid_python(jinja_env: jinja2.Environment, template_name: str) -> None:
    """Both gates, all four combinations — the block must parse in each.

    The combinations worth having are the mismatched ones: DQ on with the count off,
    and the count on for a template that does not measure. Both are arms where one
    of the two sites renders and the other does not, so a stray reference between
    them shows up here as a SyntaxError rather than at runtime.
    """
    for dq_enabled in (True, False):
        for config in (ENABLED, DISABLED):
            rendered = jinja_env.get_template(template_name).render(
                **_context(template_name, quality_config=config, data_quality_enabled=dq_enabled)
            )
            ast.parse(rendered)


def test_the_two_guards_are_the_same_expression() -> None:
    """The count's guard and the DQ partial's append guard must not drift apart.

    The append reads ``_adi_cast_null_cells``, which only the count block defines.
    The ``locals()`` test downstream makes a mismatch non-fatal rather than
    catastrophic, but a mismatch would still mean the entry silently never appears
    for a pipeline that asked for it — a feature that reports nothing while claiming
    to be on, which is the defect class p64 exists to remove. Pinned textually
    because it is invisible in any render that does not execute.
    """
    check = _partial_source(CHECK_PARTIAL)
    dq = _partial_source(DQ_PARTIAL)

    assert _body(check).count(GUARD) == 1, (
        f"{CHECK_PARTIAL}: expected exactly one occurrence of the guard expression "
        f"in the template body. If it was reworded, reword the DQ partial's copy in "
        f"the same commit."
    )
    assert _body(dq).count(GUARD) == 1, (
        f"{DQ_PARTIAL}: the checks[] append's guard is no longer the same expression "
        f"as {CHECK_PARTIAL}'s. They must stay identical — the append reads a local "
        f"only the count block defines."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_count_lives_in_the_partial_and_is_never_inlined(template_name: str) -> None:
    """R6.1's shape, applied to 2e.1: one file holds the block, its callers include it."""
    body = _body(_template_source(template_name))

    if template_name in MEASURING_TEMPLATES:
        assert INCLUDE_SITE in body
    else:
        assert INCLUDE_SITE not in body
    assert CHECK_BANNER not in body, (
        f"{template_name}: the count banner appears in the template body, so the "
        f"block has been inlined rather than included. One copy, in {CHECK_PARTIAL} "
        f"— a third copy is what R6.1 exists to prevent."
    )


def test_the_partial_is_the_only_file_holding_the_count() -> None:
    """No second copy anywhere under ``pyspark_templates/``."""
    holders = []
    for root, _dirs, files in os.walk(TEMPLATES_DIR):
        for name in files:
            if not name.endswith(".j2"):
                continue
            path = os.path.join(root, name)
            with open(path) as handle:
                if CHECK_BANNER in _body(handle.read()):
                    holders.append(os.path.relpath(path, TEMPLATES_DIR))

    assert holders == [os.path.join("_partials", "_cast_null_check.py.j2")], (
        f"the count block appears in {holders}. Exactly one file may hold it."
    )


def test_the_partial_reads_only_the_one_context_variable() -> None:
    """Its whole context surface is ``quality_config``, plus caller locals.

    Asserted because it is what lets ``test_dq_template_vars_contract.py``'s
    ``P64_PARTIALS`` fence cover this partial with no new exclusions: no stage
    argument, no never-defined sentinel, and nothing that has to be added to
    ``template_vars``. ``_target_schema`` and ``df_transformed`` are Python locals
    the auto-cast block below every include site already reads, not render
    variables.
    """
    body = _body(_partial_source(CHECK_PARTIAL))
    referenced = set(re.findall(r"\{\{\s*(\w+)", body)) | set(re.findall(r"\{%-?\s*if\s+(\w+)", body))

    assert referenced == {"quality_config"}, (
        f"{CHECK_PARTIAL} reads render variables {sorted(referenced)}. Keeping the "
        f"surface to quality_config alone is what keeps the variable-surface fence "
        f"free of new exclusions."
    )
