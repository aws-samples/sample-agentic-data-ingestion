# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The non-printable character check, on all five Glue job templates (p64 R8, task 2d.2).

WHAT THIS MODULE IS ABOUT, AND THE ONE THING TO READ FIRST. Task 2d.2's own text
says the check should feed "the **same** quarantine prefix and counters as the DQ
split (R8.3)". **That half is superseded by task 2d.1, which chose FLAG.** R8.1
permits either arm ("flags **or** quarantines") and R8.3 is written conditionally
("**IF** the check quarantines"), so the conditional does not fire — this is the
requirement not applying, not a requirement being overridden. 2d.1's four reasons
are recorded in ``_partials/_data_quality.py.j2``'s header; the shortest of them is
that no DQDL rule can fail on a control character, so the quarantine write is not
even on a live path for this signal.

So the assertions below are mostly NEGATIVE, and that is the point: the check must
divert no row, add nothing to ``rows_quarantined``, and leave ``overall_status``
alone. A test suite for a flag is largely a suite about what the flag does not do.

THE THREE FORWARD CONSTRAINTS 2d.1 HANDED TO 2d.2, each with its test here:

  * the ``checks[]`` entry's status is ``warning`` — the third value
    ``QualityCheck.status`` already admits — and no fourth value is introduced
    (``test_the_report_entry_status_is_warning_and_introduces_no_new_value``);
  * ``overall_status`` is derived from the DQDL rule outcomes and must not move
    (``test_the_report_entry_leaves_overall_status_alone``);
  * ``_dq_rows_quarantined`` stays the DQ split's own number
    (``test_nothing_from_the_check_reaches_rows_quarantined``).

WHY THE CHECK HAS ITS OWN GATE RATHER THAN RIDING ``data_quality_enabled``, since
it is the structural fact the ordering assertions depend on:
``resolve_data_quality_enabled`` defaults the DQ gate OFF for four of the five
templates, so a check behind it would render NOTHING for a JDBC- or Parquet-target
pipeline that opted in — an opt-in that silently does nothing.
``test_the_check_renders_even_when_data_quality_is_off`` is that requirement, and
``test_the_report_entry_needs_the_dq_report_row`` is the cost of it, asserted rather
than hidden: with DQ off there is no report row, so the finding is detected and
logged but not persisted.

Expectations are derived from R8.1/R8.2 and 2d.1's decision, not from current
output. Everything here **fails against the pre-2d.2 tree**: the partial did not
exist, so every render lacked the block entirely.

Validates: Requirements 8.1, 8.2
"""

from __future__ import annotations

import ast
import os
import re

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

CHECK_PARTIAL = "_partials/_nonprintable_check.py.j2"
DQ_PARTIAL = "_partials/_data_quality.py.j2"
INCLUDE_SITE = '{%- include "_partials/_nonprintable_check.py.j2" %}'

CHECK_BANNER = "# === Non-printable character check (p64 R8.1/R8.2) ==="
CHECK_END = "# === End Non-printable character check ==="
ROWS_LOCAL = "_adi_np_rows"
COUNT_CALL = "_adi_np_rows = df_transformed.filter(_adi_np_match).count()"
REPORT_ENTRY = "_dq_np_check"
DQ_END = "# === End Data Quality ==="
TRANSFORMED_COUNT = "transformed_count = df_transformed.count()"

#: The guard both sites share. Written once here because the two must stay
#: identical — see ``test_the_two_guards_are_the_same_expression``.
GUARD = 'quality_config is mapping and quality_config.get("nonprintable_check_enabled") is true'

JDBC_SOURCE_TEMPLATE = "glue_job_jdbc_source.py.j2"
ALL_TEMPLATES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
    JDBC_SOURCE_TEMPLATE,
]

SAMPLE_TRANSFORM = "df_transformed = df"
SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "id" >= 0.95\n]'
QUARANTINE_BUCKET = "adi-quarantine-bucket"

ENABLED = {"dedup_enabled": False, "dedup_columns": [], "nonprintable_check_enabled": True}
DISABLED = {"dedup_enabled": False, "dedup_columns": [], "nonprintable_check_enabled": False}


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
    ordering and report assertions have something to be ordered against. The tests
    that are about the DQ-off posture override it explicitly.
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
    The include-site comments explain what the check does, so a scan of the raw
    source would match its own documentation. Same helper, same reason, as
    ``test_dedup_all_targets.py``'s ``_template_body``.
    """
    return re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)


def _line_of(rendered: str, marker: str, label: str) -> int:
    """0-based index of the single line whose stripped form starts with ``marker``."""
    hits = [i for i, line in enumerate(rendered.split("\n")) if line.strip().startswith(marker)]
    assert len(hits) == 1, f"{label}: expected exactly one {marker!r} line, found {len(hits)}"
    return hits[0]


def _without_check(source: str) -> str:
    """``source`` as it stood before 2d.2 — the include site and its comment gone.

    Derived live rather than stored, following ``test_dedup_all_targets.py``: a
    stored snapshot would need regenerating for every unrelated template change and
    would then stop measuring anything.

    Note it does NOT need to strip anything from ``_data_quality.py.j2``. That
    partial's ``checks[]`` append is itself behind the same ``quality_config``
    guard, so with the check disabled it renders nothing — which is the property the
    byte-identity assertion is checking, not an exemption from it.
    """
    stripped = re.sub(r"\{#- p64 R8\.1/R8\.2 \(task 2d\.2\).*?-#\}\n", "", source, flags=re.DOTALL)
    return stripped.replace(INCLUDE_SITE + "\n", "")


def _region(rendered: str) -> str:
    """The rendered check block, banner to end marker inclusive."""
    start = rendered.index(CHECK_BANNER)
    end = rendered.index(CHECK_END, start) + len(CHECK_END)
    return rendered[start:end]


def _region_code(rendered: str) -> str:
    """``_region`` with the emitted ``#`` comment lines removed.

    ``LGR-b46a5bb-4`` again, one layer in from ``_body``: the substring fences below
    are about the emitted STATEMENTS, and the block's comments necessarily explain
    what it does not do ("adds nothing to rows_quarantined", "flagged, not
    quarantined"). Scanning the raw region made every one of those fences match its
    own documentation — five failures on the first run of this module, which is the
    same lesson the record records and the same one 2c.3 hit twice. The comment is
    load-bearing for a reader of the generated job, so the scan narrows rather than
    the comment going quiet.
    """
    return "\n".join(line for line in _region(rendered).split("\n") if not line.strip().startswith("#"))


# ---------------------------------------------------------------------------
# R8.1 — off by default, and off costs nothing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_is_absent_when_quality_config_is_never_set(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """An unset ``quality_config`` disables the check — it never enables it.

    The render must also SUCCEED. That is the half worth stating: under
    ``StrictUndefined`` the obvious ``is defined and`` guard answers True for a
    foreign ``missing`` sentinel and then raises on the read behind it, which is the
    failure ``_dedup.py.j2``'s header records against five templates at once. An
    ``is mapping`` guard cannot: it is an isinstance check, so it consults no
    sentinel.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=None))

    assert CHECK_BANNER not in rendered
    assert ROWS_LOCAL not in rendered
    assert REPORT_ENTRY not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
@pytest.mark.parametrize(
    "config",
    [DISABLED, {}, {"nonprintable_check_enabled": "true"}, {"nonprintable_check_enabled": 1}, "on", None, []],
    ids=["explicit-false", "empty-map", "string-true", "int-one", "scalar", "null", "list"],
)
def test_the_check_is_absent_for_everything_that_is_not_an_explicit_true(
    jinja_env: jinja2.Environment, template_name: str, config: object
) -> None:
    """``is true``, not truthiness — so ``"true"`` and ``1`` do not opt in either.

    Defence in depth behind ``resolve_quality_config``, which normalises a
    non-boolean to ``False`` before the template ever sees it. Both layers matter
    because they fail in the same direction: nobody gets a check they did not ask
    for, and nothing about the render depends on which layer caught it.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, quality_config=config))

    assert CHECK_BANNER not in rendered
    assert REPORT_ENTRY not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_disabled_render_is_byte_identical_to_the_pre_check_tree(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """R8.1's "defaulting to off" at its strongest: zero rendered characters.

    Behaviour preservation would be satisfied by emitting a comment. The stronger
    property is asserted for the reason ``_dedup.py.j2`` established it: it keeps the
    ten stored ``tests/fixtures/golden_scripts/`` snapshots and every other
    render-comparing module unaffected by 2d.2, and it is the only fence on the
    whitespace arithmetic — the include site's ``{%-`` strips the newline above it
    and the partial's ``{%- if %}`` / ``{%- endif -%}`` restore exactly one when
    enabled. Get either side wrong and the disabled arm gains or loses a blank line,
    which no behavioural test would notice.

    Both DQ postures, because the ``checks[]`` append lives inside the DQ arm of
    ``_data_quality.py.j2`` — so DQ-on is the case where a stray byte from that
    second site could appear.
    """
    source = _template_source(template_name)
    assert source.count(INCLUDE_SITE) == 1, (
        f"{template_name}: expected exactly 1 check include site, found "
        f"{source.count(INCLUDE_SITE)}. One is the contract — the partial has a "
        f"single emission point and no stage variable, so a second include would "
        f"count the frame twice and pay for two extra Spark actions."
    )

    pre_check_env = jinja2.Environment(
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
        before = pre_check_env.get_template(template_name).render(
            **_context(template_name, quality_config=None, data_quality_enabled=dq_enabled)
        )
        for label, config in (("unset", None), ("explicit False", DISABLED)):
            after = jinja_env.get_template(template_name).render(
                **_context(template_name, quality_config=config, data_quality_enabled=dq_enabled)
            )
            assert after == before, (
                f"{template_name} (data_quality_enabled={dq_enabled}): the {label} "
                f"render is NOT byte-identical to the pre-2d.2 template "
                f"({len(after)} chars vs {len(before)}). Almost certainly a "
                f"whitespace control slip — see _partials/_nonprintable_check.py.j2's "
                f"header, and note the DQ partial's append shares the same guard."
            )


# ---------------------------------------------------------------------------
# R8.1 — it renders on ALL FIVE templates, on its own gate
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_renders_on_every_target_type(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R8.1's opt-in has to work everywhere, which is why the gate is its own key."""
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert CHECK_BANNER in rendered
    assert COUNT_CALL in rendered
    assert CHECK_END in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_renders_even_when_data_quality_is_off(jinja_env: jinja2.Environment, template_name: str) -> None:
    """2d.1 reason (2), asserted: the check does NOT ride ``data_quality_enabled``.

    ``resolve_data_quality_enabled`` defaults that gate off for four of the five
    templates. A check behind it would render nothing for a JDBC- or Parquet-target
    pipeline that opted in — an opt-in that silently does nothing, which is the
    defect class p64 exists to remove. This is the test that would fail if someone
    "simplified" the two gates into one.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, data_quality_enabled=False))

    assert CHECK_BANNER in rendered
    assert COUNT_CALL in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_needs_the_dq_report_row(jinja_env: jinja2.Environment, template_name: str) -> None:
    """The cost of that independence, asserted rather than left as a surprise.

    The report row is written only inside ``_data_quality.py.j2``'s
    ``data_quality_enabled and dqdl_rules`` arm, and it is a single ``put_item`` on a
    PutItem-only role (``LGR-9e76249-3``), so the check cannot write its own without
    clobbering. With DQ off the finding is therefore detected and logged and NOT
    persisted. Pinned so that a later reader meets the gap here rather than in
    production, and so closing it (a row of its own, under its own ``step_name``) is
    a deliberate change that fails this test loudly.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name, data_quality_enabled=False))

    assert CHECK_BANNER in rendered, "the detection half must still render"
    assert REPORT_ENTRY not in rendered, (
        "the checks[] append rendered with DQ off, which means it is outside the DQ "
        "gate and therefore writing to a report row that does not exist"
    )


# ---------------------------------------------------------------------------
# It FLAGS: the negative assertions 2d.1's decision turns into requirements
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_diverts_no_rows(jinja_env: jinja2.Environment, template_name: str) -> None:
    """2d.1's decision, stated as the property that makes it a flag rather than a gate.

    What the region must not contain: a rebinding of ``df_transformed`` (that is what
    quarantining looks like — ``df_transformed = _dq_passed_df`` in the DQ partial),
    any write call, and any of the quarantine mechanism's own names. The frame the
    target receives is byte-for-byte the frame that entered the block.

    THE FENCE IS THE MECHANISM, NOT THE WORD, and that distinction cost two
    iterations of this test — ``LGR-b46a5bb-4`` a third time. Scanning for
    "quarantine" matched the block's own comments, and then, once comments were
    stripped, its ``logger.warning`` text: "flagged, not quarantined". That message is
    the most useful sentence in the block for an operator reading CloudWatch, because
    it is what tells them the rows still reached the target. A fence that forbids
    saying so would be trading a real signal for a cheaper assertion, so it names the
    mechanism's identifiers instead — which is also the stronger claim.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    assert "df_transformed =" not in code, (
        "the check region rebinds df_transformed. 2d.1 chose FLAG: every row "
        "continues to the target, so nothing here may replace the frame."
    )
    assert ".write" not in code
    for mechanism in ("_dq_quarantine", "_dq_failed_df", "_dq_passed_df", "quarantine/"):
        assert mechanism not in code, (
            f"the check region reaches {mechanism!r}, which is part of the quarantine "
            f"mechanism. R8.3's quarantine arm is the one 2d.1 rejected — see "
            f"_partials/_data_quality.py.j2's header for the four reasons."
        )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_nothing_from_the_check_reaches_rows_quarantined(jinja_env: jinja2.Environment, template_name: str) -> None:
    """2d.1's third forward constraint: ``_dq_rows_quarantined`` stays the DQ split's own number.

    The R7.6 misattribution 2c.4 spent a task closing. Read as "add to the same
    counter", R8.3 would make ``rows_quarantined`` mean "failed a DQDL rule UNION
    contains a control character", and ``rows_read - rows_written`` would stop
    decomposing. Asserted by pinning the ONLY two statements allowed to write that
    local, both of which predate 2d.2.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    writers = [line.strip() for line in rendered.split("\n") if re.match(r"\s*_dq_rows_quarantined\s*(=|\+=)", line)]
    assert writers == ["_dq_rows_quarantined = 0", "_dq_rows_quarantined = _dq_failed_df.count()"], (
        f"{template_name}: rows_quarantined is written by {writers}. 2d.1's third "
        f"forward constraint is that nothing from R8 is added to it — the check "
        f"loses no rows, so it has no term to contribute."
    )
    assert f"_dq_rows_quarantined + {ROWS_LOCAL}" not in rendered
    assert f"{ROWS_LOCAL} + _dq_rows_quarantined" not in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_leaves_overall_status_alone(jinja_env: jinja2.Environment, template_name: str) -> None:
    """2d.1's second forward constraint: the verdict is the DQDL rules' verdict.

    ``overall_status`` is derived from ``_dq_fail_count``. A flag that does not gate
    the write must not change it, or a run whose declared rules all passed reports
    ``fail`` — which would also move ``shared/quality_verdict.py``'s reported mode
    and give ``derive_load_outcome`` a fourth unnamed term.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert '"overall_status": "pass" if _dq_fail_count == 0 else "fail",' in rendered, (
        "the overall_status expression changed. It must stay derived from "
        "_dq_fail_count over the DQDL rule outcomes alone."
    )
    for counter in ("rules_passed", "rules_failed", "rules_total", "root_cause"):
        line = next(line for line in rendered.split("\n") if f'"{counter}"' in line)
        assert ROWS_LOCAL not in line, (
            f"{counter} reads {ROWS_LOCAL}. The character check is not a DQDL rule, "
            f"so it is counted in none of the rules_* figures and sets no root_cause."
        )


# ---------------------------------------------------------------------------
# The report entry itself (R8.3's "same report", honoured as a checks[] entry)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_status_is_warning_and_introduces_no_new_value(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """2d.1's first forward constraint, plus the "no fourth status" half.

    ``warning`` is the third value ``QualityCheck.status`` already admits and
    ``QualityReportCard.checkStatusType`` already renders, so the entry needs no
    schema widening. The clean-run arm emits ``pass``, also already admitted — 2d.2
    chose to emit the entry on every run rather than only on a finding, so that
    "check ran and found nothing" is distinguishable from "check never ran". That is
    the one place 2d.2 went beyond the letter of the brief, and it adds no value
    outside the existing three.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    assert f'"status": "warning" if {ROWS_LOCAL} > 0 else "pass",' in rendered
    statuses = set(re.findall(r'"status": "(\w+)"', rendered))
    assert statuses <= {"pass", "fail", "warning"}, (
        f"{template_name}: emitted check statuses {sorted(statuses)} include a value "
        f"outside QualityCheck.status's three. 2d.1 forbids adding a fourth."
    )
    assert '"status": "fail"' not in _dq_np_region(rendered)


def _dq_np_region(rendered: str) -> str:
    """The rendered ``checks[]`` append for the character check."""
    start = rendered.index(f"{REPORT_ENTRY} = {{")
    end = rendered.index(f"_dq_checks.append({REPORT_ENTRY})", start)
    return rendered[start:end]


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_report_entry_joins_the_dict_already_being_built(jinja_env: jinja2.Environment, template_name: str) -> None:
    """``LGR-9e76249-3``: one ``put_item``, so the entry appends rather than writing a row.

    The Glue runtime role is PutItem-only on the state table, with no UpdateItem, so
    a second writer would overwrite the DQ report rather than merge with it. The
    append therefore sits between the DQDL rule loop and the ``_dq_report`` literal,
    and the number of ``put_item`` calls does not change.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    append_line = _line_of(rendered, f"_dq_checks.append({REPORT_ENTRY})", template_name)
    report_line = _line_of(rendered, "_dq_report = {", template_name)
    rule_loop_line = _line_of(rendered, "_dq_checks.append(_check)", template_name)

    assert rule_loop_line < append_line < report_line, (
        f"{template_name}: the check's entry must be appended after the DQDL rule "
        f"loop and before the _dq_report literal that reads _dq_checks."
    )
    assert rendered.count("_tbl.put_item(") == 1, (
        f"{template_name}: {rendered.count('_tbl.put_item(')} put_item calls on the "
        f"report row. LGR-9e76249-3 — a second writer clobbers rather than merges."
    )


# ---------------------------------------------------------------------------
# Placement, and the guard-pairing hazard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_follows_the_dq_split_and_precedes_the_transformed_count(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """Both halves of the placement are load-bearing.

    AFTER the DQ 'evaluate' stage, because that stage rebinds ``df_transformed`` to
    the Passed split. Counting before it would count rows about to be quarantined and
    never written, so the number would not answer "how much of what I wrote is
    contaminated".

    BEFORE ``transformed_count``, because the ``checks[]`` append is assembled in the
    'report' stage that follows that count, and it reads the local this block leaves
    behind.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))

    dq_end_line = _line_of(rendered, DQ_END, template_name)
    banner_line = _line_of(rendered, CHECK_BANNER, template_name)
    count_line = _line_of(rendered, TRANSFORMED_COUNT, template_name)

    assert dq_end_line < banner_line < count_line, (
        f"{template_name}: check at line {banner_line}, DQ split ends at "
        f"{dq_end_line}, transformed_count at {count_line}. The check must sit "
        f"between them — see the partial's header for both reasons."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_sits_at_the_job_body_indent(jinja_env: jinja2.Environment, template_name: str) -> None:
    """4 spaces — the ``try:`` body of the job, not nested inside a narrower block.

    The whitespace-stripping closing tag that eats the trailing newline also eats
    the indentation behind it if it is used on the opening side, which produces a
    statement at column 0 inside a ``try:`` and a job that does not compile.
    ``_dedup.py.j2``'s header records that being written first and caught by
    ``ast.parse``; this asserts the outcome directly.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    lines = rendered.split("\n")

    for marker in (CHECK_BANNER, CHECK_END, f"{ROWS_LOCAL} = 0"):
        idx = _line_of(rendered, marker, template_name)
        indent = len(lines[idx]) - len(lines[idx].lstrip())
        assert indent == 4, f"{template_name}: {marker!r} at indent {indent}, expected 4"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_enabled_render_is_valid_python(jinja_env: jinja2.Environment, template_name: str) -> None:
    """Both gates, all four combinations — the block must parse in each.

    The DQ-off combination is the one worth having: it is the arm where the check
    renders and the ``checks[]`` append does not, so a stray reference between the
    two sites would show up here as a SyntaxError rather than at runtime.
    """
    for dq_enabled in (True, False):
        for config in (ENABLED, DISABLED):
            rendered = jinja_env.get_template(template_name).render(
                **_context(template_name, quality_config=config, data_quality_enabled=dq_enabled)
            )
            ast.parse(rendered)


def test_the_two_guards_are_the_same_expression() -> None:
    """The check's guard and the DQ partial's append guard must not drift apart.

    The append reads ``_adi_np_rows``, which only the check block defines. If the two
    conditions ever disagree, the 'report' stage dereferences an undefined name — and
    because the whole report write is inside ``except Exception as _ddb_err``, the
    consequence is not a crash but the silent loss of the entire DQ report row. That
    is a worse failure than the one it replaces, and it is invisible in any render
    that does not execute, so it is pinned textually.
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
        f"only the check block defines, and a mismatch silently drops the whole DQ "
        f"report row."
    )


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_lives_in_the_partial_and_is_never_inlined(template_name: str) -> None:
    """R6.1's shape, applied to 2d.2: one file holds the block, five include it."""
    body = _body(_template_source(template_name))

    assert INCLUDE_SITE in body
    assert CHECK_BANNER not in body, (
        f"{template_name}: the check banner appears in the template body, so the "
        f"block has been inlined rather than included. One copy, in "
        f"{CHECK_PARTIAL} — a sixth copy is what R6.1 exists to prevent."
    )


def test_the_partial_is_the_only_file_holding_the_check() -> None:
    """No second copy anywhere under ``pyspark_templates/``."""
    holders = []
    for root, _dirs, files in os.walk(TEMPLATES_DIR):
        for name in files:
            if not name.endswith(".j2"):
                continue
            path = os.path.join(root, name)
            with open(path) as handle:
                if COUNT_CALL in _body(handle.read()):
                    holders.append(os.path.relpath(path, TEMPLATES_DIR))

    assert holders == [os.path.join("_partials", "_nonprintable_check.py.j2")], (
        f"the check's count call is emitted by {holders}. Exactly one file may hold it."
    )


# ---------------------------------------------------------------------------
# R8.2 — a Spark predicate, not a DQDL rule; and R8.5 / LGR-819980f-1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_is_a_spark_predicate_and_not_a_dqdl_rule(jinja_env: jinja2.Environment, template_name: str) -> None:
    """R8.2: DQDL has no character-class rule, so the check cannot be one.

    The ruleset emitted into ``dq_ruleset`` must be untouched by 2d.2 — the
    vocabulary ``_generate_dqdl_rules`` can produce is ``RowCount``,
    ``Completeness`` and ``Uniqueness``, and inventing a rule name Glue does not
    know would fail the evaluation rather than the check.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    ruleset = rendered[rendered.index('dq_ruleset = """') : rendered.index('"""', rendered.index("dq_ruleset") + 20)]

    assert "NonPrintable" not in ruleset
    assert "rlike" in _region_code(rendered), "the check must be expressed as a Spark predicate"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_column_list_is_read_off_the_frame_and_not_interpolated(
    jinja_env: jinja2.Environment, template_name: str
) -> None:
    """No pipeline-supplied identifier reaches the emitted check — so it opens no sink.

    This is why 2d.2 needed no ``validate_identifier`` call of its own and why
    R8.5 / do-NOT 7's ordering rule is untouched by it: the column list is derived
    from ``df_transformed.dtypes`` at runtime, so there is no name here to validate.
    ``LGR-819980f-1`` (INVARIANT) holds for the same reason — the only render
    variable the partial interpolates is the guard's own boolean, so no credential
    can reach a rendered string, a log line or a Glue job argument.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    assert "df_transformed.dtypes" in code
    for forbidden in ("password", "secret", "user=", "jdbc:"):
        assert forbidden not in code.lower(), f"{forbidden!r} appears in the check region"


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_the_check_costs_exactly_one_extra_spark_action(jinja_env: jinja2.Environment, template_name: str) -> None:
    """One ``count()``, paid only inside the enabled arm.

    Pinned because the obvious implementations cost more: a ``collect()`` of the
    offending rows, or a count per column instead of one over an OR-ed predicate.
    The single pass is what makes the check cheap enough to leave on.
    """
    rendered = jinja_env.get_template(template_name).render(**_context(template_name))
    code = _region_code(rendered)

    assert code.count(".count()") == 1, f"{template_name}: {code.count('.count()')} actions in the check region"
    assert ".collect()" not in code
    assert code.count(".filter(") == 1


# ---------------------------------------------------------------------------
# R8.1 — the character class itself
# ---------------------------------------------------------------------------


def _emitted_pattern() -> str:
    """The character class as the rendered job will hand it to Spark.

    Read out of the PARTIAL by AST rather than asserted as a literal, so this and
    the property module measure what is emitted rather than what was expected.
    """
    source = _partial_source(CHECK_PARTIAL)
    match = re.search(r'_adi_np_pattern = r"([^"]+)"', source)
    assert match, "could not find the emitted pattern assignment in the partial"
    return match.group(1)


def test_the_pattern_is_emitted_as_a_raw_string() -> None:
    """Load-bearing: the ``\\x`` escapes must reach Spark's ``rlike`` as regex source.

    Written as a normal string literal, Python itself would consume the escapes and
    embed real control characters in the pattern text — which happens to work for a
    plain range and is one ``\\x0B`` away from meaning something else.
    """
    source = _partial_source(CHECK_PARTIAL)

    assert '_adi_np_pattern = r"' in source, (
        "the pattern lost its raw-string prefix. Python would then interpret the "
        "escapes instead of passing them through to the JVM regex engine."
    )


@pytest.mark.parametrize("codepoint", [0x00, 0x01, 0x07, 0x08, 0x0B, 0x0C, 0x0E, 0x1B, 0x1F])
def test_the_class_flags_every_c0_control_it_should(codepoint: int) -> None:
    """R8.1 names NUL (0x00), BEL (0x07) and ESC (0x1B) explicitly; the rest of C0 follows.

    Compiled with Python's ``re`` rather than executed on Spark. The class is a plain
    bracketed range of hex escapes, which Java and Python agree on exactly, so the
    engine difference does not reach this assertion — and a unit test that needs no
    Spark session is the difference between running here and running in e2e only.
    """
    assert re.search(_emitted_pattern(), chr(codepoint)), f"U+{codepoint:04X} is not flagged but R8.1 requires it"


@pytest.mark.parametrize(
    ("codepoint", "why"),
    [
        (0x09, "tab — R8.1 excludes it, and a quoted CSV field legitimately carries one"),
        (0x0A, "newline — same"),
        (0x0D, "carriage return — same"),
        (0x20, "space is printable"),
        (0x41, "'A' is printable"),
        (0x7E, "'~' is printable"),
        (0x7F, "DEL is not a C0 control; R8.1's enumeration stops at 0x1F"),
        (0x85, "C1 range — in a mis-decoded Latin-1 file these bytes are printable text"),
        (0x9F, "C1 range — same"),
    ],
)
def test_the_class_leaves_everything_else_alone(codepoint: int, why: str) -> None:
    """The exclusions are the decision, so each one is pinned with its reason.

    The three R8.1 names are excluded because the requirement says so and because
    including them would flag most text-sourced pipelines on their first run. DEL and
    the C1 range are excluded because R8.1's parenthetical defines the class as C0,
    and widening later is additive while narrowing later silently stops reporting a
    finding an operator has been watching.
    """
    assert not re.search(_emitted_pattern(), chr(codepoint)), f"U+{codepoint:04X} is flagged: {why}"
