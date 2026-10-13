# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R6.5 / do-NOT 6 — every DQ template variable is set UNCONDITIONALLY.

WHY THIS MODULE EXISTS, AND WHY IT WAS A GUARD RATHER THAN A CHANGE. p64 2b.3
asked for "any new template variable set unconditionally in ``template_vars``".
2b.1 and 2b.2 introduced **none**: ``_partials/_data_quality.py.j2`` read
exactly ``dqdl_rules`` and ``quarantine_bucket`` off the render context, and
``code_assembly.generate_code`` already assigned both at its own top level
before p64 started. So R6.5 was already satisfied and there was nothing to fix.
What did not exist was anything that would *keep* it satisfied — and 2c, 2d and
2e all add to this same block, with 2c threading a new ``quality_config`` into
``template_vars`` per design C7. This module is that fence.

THE FENCE HAS SINCE BEEN EXERCISED, WHICH IS WHY THE PARAGRAPH ABOVE IS PAST
TENSE. p64 2b.4 added a THIRD variable — ``data_quality_enabled``, R6.6's opt-in
gate — and met this module on arrival: the partial reads it, so
``test_every_p64_partial_variable_is_set_unconditionally_in_template_vars``
required an unconditional ``template_vars["data_quality_enabled"] = …`` at
``generate_code``'s top level, beside the other assignments and NOT routed
through the ``target_config`` splat (design C7) or a ``| default()`` (do-NOT 6).
Nothing here was loosened to accommodate it. The assertions are derived from the
partial's own variable surface rather than from a list of names, so they widened
themselves.

Note the asymmetry between the three, because it is the point of the module.
``quarantine_bucket`` is interpolated bare, so an unset value raises.
``dqdl_rules`` and ``data_quality_enabled`` both sit behind ``is defined and``, so
an unset value raises **nothing** and silently deletes the whole DQ body. One of
three is loud; the load-bearing assertion is about ``template_vars`` precisely
because the other two are not. For the gate that silence has a direction and it
is the safe one — absent means DISABLED — which
``test_unset_data_quality_enabled_fails_CLOSED_rather_than_open`` pins.

THE MECHANISM do-NOT 6 PROTECTS AGAINST, because the fence is unreadable without
it. ``_get_pyspark_jinja_env`` uses ``StrictUndefined``, so an *unset* variable
is normally a loud render error. ``| default(...)`` converts that loud error
into a silent substitution — and the substitution is not even reliable. The
filter decides via ``isinstance(value, Undefined)``, and when a process holds
TWO copies of the jinja2 package, ``Context.resolve_or_missing`` returns copy
A's ``missing`` sentinel while the compiled template tests ``is missing``
against copy B's. ``_MissingType`` is neither an ``Undefined`` nor falsy, so
``default()`` hands the foreign sentinel straight through to whatever consumes
it. Two copies is exactly what ``mock.patch.dict("sys.modules", ...)`` leaves
behind on exit, 118 test modules in this repo use that idiom, and this is how
all 18 tests in ``tests/integration/pipeline_flow/test_code_generation_e2e.py``
broke at once with ``can't multiply sequence by non-int of type '_MissingType'``.
``_partials/_load_counts.py.j2``'s header records the full story and
``test_load_counts_code_assembly_render.py`` is its fence; this module is the
same fence over the DQ block, which had none.

Note the emphasis in do-NOT 6's own wording — "an **optional** Jinja variable
read via ``default()``". The hazard needs BOTH halves. ``default()`` on a
variable every render sets is inert: the filter sees a real value, so no
sentinel arithmetic happens whatever jinja2 is loaded. It only bites on a
variable no caller sets. That is why the load-bearing assertion below is about
``template_vars``, not about ``default()``.

WHERE THE LINE FALLS, AND WHY NOT WIDER. A blanket "no ``default()`` in
templates" fence would fail on arrival: four of the five job templates open with
``{%- set write_mode = write_mode | default('overwrite') %}`` and three more
like it, and ``_partials/_write_s3_tables.py.j2`` reads ``key_prefix`` through
``default()`` too. All of that is pre-existing and out of scope (2b.1 flagged
the ``key_prefix`` one and left it deliberately). Rather than allowlisting those
occurrences, the ``default()`` assertion here is **scoped** to the one file p64
2b created and 2c/2d/2e extend, where the current count is zero and no
exceptions are needed. That follows ``LGR-b46a5bb-4`` (template / ratified),
which rejected an exemption for exactly this reason: "an allowlist entry is a
door, so scope is the honest boundary".

THE ONE ASYMMETRY THAT LINE LEAVES, recorded because it is a live hazard and not
this task's to fix. ``glue_job_s3_tables.py.j2:11`` carries a **pre-existing**
``{%- set quarantine_bucket = quarantine_bucket | default('') %}`` from P28,
when it was the only template with a DQ block. The four templates 2b.2 ported to
have no such line. So if ``template_vars["quarantine_bucket"]`` ever became
conditional, four templates would fail the render loudly and s3_tables would
quietly render ``_dq_quarantine_bucket = ""`` — which then makes R6.4's refusal
fire at Glue runtime on a job that generated cleanly. That split is measured
rather than asserted by eye (see
``test_exactly_one_template_masks_an_unset_quarantine_bucket``), and pinned at
exactly one member so a fifth template acquiring a ``default()`` fails here.

AND THE WORSE CASE, which is why the ``template_vars`` assertion is the
load-bearing one rather than the loudness one. ``quarantine_bucket`` is
interpolated bare, so an unset value raises. ``dqdl_rules`` is read behind
``{% if dqdl_rules is defined and dqdl_rules %}``, so an unset value raises
**nothing** — the banner still renders and the entire DQ body silently vanishes
from all five templates. No test that renders with a complete context can see
that, and it is precisely the partial-coverage shape R6.1 exists to end.

Validates: Requirements 6.5
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
from jinja2 import meta

# Region before any boto3-adjacent import, matching the sibling create_code
# template modules.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_create_code"),
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Imported directly, NOT through ``lambda_function`` behind a
# ``patch.dict("sys.modules", ...)`` block — that idiom is the thing whose
# fallout this module tests, so reaching the code under test through it would
# hide the condition being asserted. Same reasoning as
# ``test_load_counts_code_assembly_render.py``.
import code_assembly

DQ_PARTIAL = "_partials/_data_quality.py.j2"

# p64 2c.3's partial, added to this module's scope on arrival rather than left
# outside it. The header above says "2c, 2d and 2e all add to this same block,
# with 2c threading a new ``quality_config`` into ``template_vars``" — and 2c.1
# did thread it, deliberately writing the subscript as a STRING LITERAL so
# ``_template_vars_target``'s ``ast.Constant`` slice check could see it. That
# preparation bought nothing until something actually READ ``quality_config`` off
# the render context: ``_dq_partial_variables`` derives the required set from the
# DQ partial's surface alone, so a variable read only by a NEW partial would have
# been invisible here and the fence 2c.1 set up would have sat inert.
#
# Widening the reader rather than adding ``quality_config`` to a list of names is
# the same choice the original module made and for the same reason — the
# assertions stay derived from what the templates actually read, so 2d's and 2e's
# variables will widen this themselves.
DEDUP_PARTIAL = "_partials/_dedup.py.j2"

# p64 2d.2's partial, added here on arrival for the reason the paragraph above
# gives: this tuple is EXPLICIT, not discovered, so a new partial is outside both
# fences below until it is named. The task brief that produced it assumed the
# variable-surface fence covered a new partial automatically — it does not, and
# that assumption is exactly how 2c.3's `quality_config` nearly escaped.
#
# Adding it costs no new exclusions, which was a design constraint rather than
# luck: the check partial reads exactly ONE context variable, `quality_config`,
# already required below. It takes no stage argument and carries no
# never-defined sentinel — its `checks[]` entry is emitted inline by
# `_data_quality.py.j2` under a duplicate of its own guard, rather than by a
# second stage here — so `CALLER_SUPPLIED` and `NEVER_DEFINED_SENTINEL` stay
# single names.
NONPRINTABLE_PARTIAL = "_partials/_nonprintable_check.py.j2"
# p64 2e.1's partial, added here on arrival for the same reason 2d.2's was: this
# tuple is EXPLICIT, not discovered, so a new partial is outside both fences below
# until it is named.
#
# It costs no new exclusions either — it reads exactly ONE context variable,
# `quality_config`, already required below, takes no stage argument and carries no
# never-defined sentinel. Its `checks[]` entry is emitted inline by
# `_data_quality.py.j2` under a duplicate of its own guard.
#
# One difference worth knowing while reading the fences below: this partial has
# only TWO include sites, not five, because the count it takes is only computable
# where an auto-cast block exists. That does not weaken the variable-surface fence,
# which is about which names ``template_vars`` must assign rather than how many
# templates read them.
CAST_NULL_PARTIAL = "_partials/_cast_null_check.py.j2"

#: Every partial p64 owns and whose variable surface ``template_vars`` must cover.
#: NOT a list of all partials: ``_load_counts.py.j2`` has its own fence in
#: ``test_load_counts_code_assembly_render.py``, and the four ``_write_*``
#: partials predate p64 and carry pre-existing ``default()`` filters that
#: ``LGR-b46a5bb-4`` says to leave outside this scope rather than allowlist.
P64_PARTIALS = (DQ_PARTIAL, DEDUP_PARTIAL, NONPRINTABLE_PARTIAL, CAST_NULL_PARTIAL)

# Names the DQ partial reads that do NOT come from ``template_vars``, each with
# the reason it is excluded. Both are structural, not conveniences.
#
#   data_quality_stage — supplied by the CALLER, per include site, through
#       ``{% with data_quality_stage = 'evaluate' %}``. There is deliberately no
#       default: an omitted or mistyped stage must fail the render loudly rather
#       than render nothing. ``test_data_quality_all_targets.py`` asserts both
#       stages are set at all five templates' include sites.
#   data_quality_stage_must_be_either_… — the partial's else-arm sentinel, a name
#       that is deliberately never defined anywhere so that a mistyped stage
#       raises and names itself. Asserted absent from ``template_vars`` below,
#       so "defining it" is never mistaken for a fix.
CALLER_SUPPLIED = "data_quality_stage"
NEVER_DEFINED_SENTINEL = "data_quality_stage_must_be_either_evaluate_or_report"

ALL_TEMPLATES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_jdbc_source.py.j2",
]

# The one template carrying a pre-existing P28 ``| default('')`` on a DQ
# variable, from when it was the only template with a DQ block at all.
PRE_EXISTING_DEFAULT_TEMPLATE = "glue_job_s3_tables.py.j2"

SAMPLE_DQDL = 'Rules = [\n    RowCount > 0,\n    Completeness "id" >= 0.95\n]'
QUARANTINE_BUCKET = "adi-quarantine-bucket"

EVALUATE_BANNER = "# === Glue Data Quality Evaluation ==="
QUARANTINE_WRITE = '_dq_failed_df.write.mode("overwrite").parquet(_dq_quarantine_path)'


# ---------------------------------------------------------------------------
# Helpers — the render context, and the AST read of ``template_vars``
# ---------------------------------------------------------------------------


def _context(template_name: str, **overrides: object) -> dict[str, object]:
    """The full variable surface ``StrictUndefined`` demands, minus overrides.

    A key whose value is ``None`` is REMOVED rather than passed as ``None`` —
    that is how the tests below express "this variable was never set", which is
    the condition ``StrictUndefined`` is there to catch.
    """
    if template_name == "glue_job_jdbc_source.py.j2":
        base: dict[str, object] = {
            "engine": "postgres",
            "target_type": "S3_TABLES",
            "read_mode": "incremental",
            "write_mode": "append",
            "initial_watermark": "2024-01-01T00:00:00Z",
            "partition_columns": "",
            "merge_key": "id",
            "transform_block": "df_transformed = df",
            "dqdl_rules": SAMPLE_DQDL,
            "quarantine_bucket": QUARANTINE_BUCKET,
            # p64 2b.4's gate. True by default so this module keeps measuring the
            # ENABLED render — the arm where the DQ body exists and its variables
            # are actually read. Under a False default, "an unset dqdl_rules drops
            # the whole body" would be true for a second, unrelated reason and the
            # exposure this module records would stop being visible.
            "data_quality_enabled": True,
        }
    else:
        base = {
            "transform_block": "df_transformed = df",
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


def _partial_variables(partial: str) -> set[str]:
    """Variables ``partial`` reads off the render context.

    Read through ``code_assembly``'s own environment and loader rather than a
    hand-built path, so a searchpath change is visible here and the file parsed
    is the one production renders — not a stale copy under ``cdk.out/``.
    """
    env = code_assembly._get_pyspark_jinja_env()
    source = env.loader.get_source(env, partial)[0]
    return set(meta.find_undeclared_variables(env.parse(source)))


def _dq_partial_variables() -> set[str]:
    """The DQ partial's surface alone — for the assertions that are about it."""
    return _partial_variables(DQ_PARTIAL)


def _p64_partial_variables() -> set[str]:
    """The union over every partial p64 owns.

    The union, not the DQ partial alone: R6.5's requirement is that a variable
    reaching one of these partials is assigned unconditionally in
    ``template_vars``, and which FILE reads it is immaterial to that. Scoping the
    requirement to one partial would mean a new partial silently escapes it, which
    is exactly what 2c.3's ``quality_config`` would have done.
    """
    return set().union(*(_partial_variables(name) for name in P64_PARTIALS))


def _template_vars_target(node: ast.AST) -> str | None:
    """``"x"`` for a ``template_vars["x"] = …`` assignment target, else ``None``."""
    if not isinstance(node, ast.Assign):
        return None
    for target in node.targets:
        if (
            isinstance(target, ast.Subscript)
            and isinstance(target.value, ast.Name)
            and target.value.id == "template_vars"
            and isinstance(target.slice, ast.Constant)
            and isinstance(target.slice.value, str)
        ):
            return target.slice.value
    return None


def _render_site_function() -> ast.FunctionDef:
    """The single function holding ``template.render(**template_vars)``.

    Located by the render call rather than by name, so this module cannot end up
    reading a differently-named function that no longer renders anything. That
    there is exactly ONE such function is itself the guarantee that makes
    "assigned here" mean "assigned on every render" — the claim
    ``code_assembly.py:1902-1904`` makes in prose, asserted mechanically.
    """
    with open(code_assembly.__file__) as handle:
        tree = ast.parse(handle.read())

    def renders(fn: ast.AST) -> bool:
        return any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "render"
            and any(
                kw.arg is None and isinstance(kw.value, ast.Name) and kw.value.id == "template_vars"
                for kw in node.keywords
            )
            for node in ast.walk(fn)
        )

    sites = [
        node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and renders(node)
    ]
    assert len(sites) == 1, (
        f"expected exactly one function in {code_assembly.__file__} calling "
        f"template.render(**template_vars), found {len(sites)}: "
        f"{[fn.name for fn in sites]}. Every assertion in this module rests on "
        f"there being one render site, so a second one invalidates them rather "
        f"than merely widening them — the new site needs its own coverage."
    )
    return sites[0]  # type: ignore[return-value]


def _template_var_assignments() -> tuple[dict[str, int], dict[str, int], list[str]]:
    """``(unconditional, conditional, splatted)`` for the render site's context.

    ``unconditional`` — ``template_vars["x"] = …`` as a DIRECT statement of the
    function body, so it executes on every call. Values are line numbers.
    ``conditional`` — the same assignment nested inside anything at all (``if``,
    ``for``, ``try``, ``with``), so whether it runs depends on the input.
    ``splatted`` — names passed to ``template_vars.update(…)``, which inject keys
    this AST read cannot see. Reported so a variable arriving that way is named
    as invisible rather than silently counted as missing.
    """
    fn = _render_site_function()
    unconditional: dict[str, int] = {}
    conditional: dict[str, int] = {}
    splatted: list[str] = []

    for stmt in fn.body:
        direct = _template_vars_target(stmt)
        if direct is not None:
            unconditional[direct] = stmt.lineno
        for node in ast.walk(stmt):
            if node is stmt:
                continue
            nested = _template_vars_target(node)
            if nested is not None:
                conditional[nested] = node.lineno  # type: ignore[attr-defined]

    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "update"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "template_vars"
        ):
            splatted.extend(ast.unparse(arg) for arg in node.args)

    return unconditional, conditional, splatted


@contextlib.contextmanager
def _second_jinja2_copy_in_sys_modules() -> Iterator[object]:
    """Evict ``jinja2`` and re-import it, so ``missing`` is two objects.

    Reproduces what ``patch.dict("sys.modules", …)`` leaves behind: the
    already-imported ``code_assembly`` keeps its binding to the ORIGINAL package
    while anything importing ``jinja2`` from here on — the compiled template's
    own ``from jinja2.runtime import … missing`` included — gets a second copy.

    Restored by key rather than with ``patch.dict``, so this helper does not
    reproduce the hazard it exists to test. Lifted from
    ``test_load_counts_code_assembly_render.py``, which owns the same fence for
    the counts partial; kept as a local copy rather than shared because these
    two modules are the only callers and a shared helper would need a home that
    imports cleanly under both.
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
# The load-bearing assertion: R6.5 itself
# ---------------------------------------------------------------------------


def test_every_p64_partial_variable_is_set_unconditionally_in_template_vars() -> None:
    """R6.5 / do-NOT 6: the partials' whole variable surface, or the render lies.

    THIS is the fence 2c, 2d and 2e meet. Each of them adds to the DQ block, and
    design C7 threads a new ``quality_config`` into ``template_vars``
    explicitly — rather than through ``target_config``, which
    ``template_vars.update`` would splat in for free with no backend change and
    no validation. A variable that reaches either partial without an
    unconditional assignment here fails this test whether it was omitted,
    assigned inside an ``if``, or routed through the splat.

    RENAMED from ``test_every_dq_partial_variable_…`` by 2c.3, when the required
    set stopped being one partial's surface. The rename is the point rather than
    tidying: 2c.3's ``quality_config`` is read by ``_partials/_dedup.py.j2`` and
    by nothing in the DQ partial, so under the old name and the old reader it was
    outside the fence entirely — assigned correctly, and unguarded. Nothing was
    loosened to accommodate it; the reader widened, and both variables are now
    required.

    It is the load-bearing assertion rather than the loudness one below because
    the failure mode is not symmetric. ``quarantine_bucket`` is interpolated
    bare, so an unset value raises. ``dqdl_rules`` sits behind
    ``{% if dqdl_rules is defined and dqdl_rules %}``, so an unset value raises
    nothing at all: the banner renders, the entire DQ body disappears from all
    five templates, and every render-with-a-complete-context test still passes.
    """
    required = _p64_partial_variables() - {CALLER_SUPPLIED, NEVER_DEFINED_SENTINEL}
    unconditional, conditional, splatted = _template_var_assignments()

    assert required, (
        "the p64 partials read no context variables at all — either a partial "
        "moved or find_undeclared_variables is being given the wrong source, and "
        "either way this module is asserting nothing"
    )
    # Pinned by name, not derived, because these two are the ones the module's
    # own header reasons about: `dqdl_rules` and `data_quality_enabled` are read
    # behind `is defined and` so their absence is silent, and `quality_config` is
    # 2c.3's addition and the reason the reader was widened. A partial rewritten
    # to stop reading one of them makes every assertion below narrower without
    # failing anything, which is the vacuous-pass shape this module exists to
    # avoid.
    for expected in ("dqdl_rules", "data_quality_enabled", "quality_config"):
        assert expected in required, (
            f"{expected!r} is no longer read by any partial in {list(P64_PARTIALS)} — "
            f"the fence narrowed silently. If that is deliberate, say so here rather "
            f"than leaving this module measuring less than it claims."
        )

    missing = sorted(required - set(unconditional))
    assert not missing, (
        f"{missing} reach one of {list(P64_PARTIALS)} but are NOT assigned "
        f"unconditionally in {code_assembly.__file__}'s render site.\n"
        f"  unconditional: {sorted(unconditional)}\n"
        f"  conditional:   {sorted(conditional)}\n"
        f"  splatted via template_vars.update: {splatted}\n"
        f"R6.5 / design.md do-NOT 6: set it beside the others at the function's "
        f"top level. Do NOT reach for `| default(...)` in the template and do NOT "
        f"route it through source_config/target_config — design C7 rejects the "
        f"latter (a data-quality setting is not target configuration, and "
        f"target_config is unvalidated free-form). An unset `dqdl_rules` does not "
        f"raise: it renders the banner and silently drops the whole DQ body from "
        f"all five templates."
    )


def test_no_dq_variable_is_assigned_conditionally() -> None:
    """The inverse of "unconditionally": no second, nested assignment either.

    Passing the test above by assigning at the top level and then overriding
    inside an ``if`` would satisfy ``StrictUndefined`` while still making the
    rendered value depend on a branch. That is the same silent-divergence shape,
    one step further in.

    Reads the same widened set as the test above (2c.3), so ``quality_config``
    cannot be top-level-assigned and then overridden in a branch either.
    """
    required = _p64_partial_variables() - {CALLER_SUPPLIED, NEVER_DEFINED_SENTINEL}
    _, conditional, _ = _template_var_assignments()

    offending = sorted(required & set(conditional))
    assert not offending, (
        f"{offending} are assigned into template_vars from a NESTED statement "
        f"(line(s) {[conditional[name] for name in offending]}) — inside an if/for/"
        f"try/with rather than at the render site's top level. R6.5 requires the "
        f"assignment to be unconditional; a top-level assignment plus a nested "
        f"override is not."
    )


def test_the_never_defined_stage_sentinel_stays_undefined() -> None:
    """The else-arm sentinel must never become a real variable.

    ``_partials/_data_quality.py.j2``'s ``else`` arm dereferences a name nothing
    defines, so under ``StrictUndefined`` an omitted or mistyped
    ``data_quality_stage`` fails the render and names itself in the error.
    Defining that name — in ``template_vars``, or with a ``{% set %}`` — would
    turn the loud failure into a stage that renders nothing, which is worse than
    a wrong value: the DQ block would vanish from a template that appears to
    include it.
    """
    assert NEVER_DEFINED_SENTINEL in _dq_partial_variables(), (
        f"{NEVER_DEFINED_SENTINEL!r} is no longer read by the DQ partial — the "
        f"else-arm guard changed shape, so this assertion is measuring nothing"
    )
    unconditional, conditional, _ = _template_var_assignments()
    assert NEVER_DEFINED_SENTINEL not in unconditional and NEVER_DEFINED_SENTINEL not in conditional, (
        f"{NEVER_DEFINED_SENTINEL!r} is now assigned in template_vars. It exists "
        f"to be UNDEFINED: that is what makes a mistyped data_quality_stage a "
        f"loud render error instead of a silently empty DQ block."
    )


# ---------------------------------------------------------------------------
# do-NOT 6's letter, scoped to the file p64 2b owns
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("partial", P64_PARTIALS)
def test_the_p64_partials_read_nothing_through_default(partial: str) -> None:
    """do-NOT 6 as written, scoped to the partials p64 owns.

    Scoped rather than allowlisted, per ``LGR-b46a5bb-4``: four of the five job
    templates open with pre-existing ``| default(...)`` filters on ``write_mode``
    / ``merge_key`` / ``checkpoint_*`` / ``target_type`` and friends, and
    ``_partials/_write_s3_tables.py.j2`` reads ``key_prefix`` that way too. None
    of that is p64's, and enumerating it here would be "a door" rather than a
    fence. Both p64 files' counts are zero and need no exceptions.

    A ``default()`` on a variable ``template_vars`` always sets is inert — the
    filter never sees an ``Undefined``. This is a hygiene fence, not the safety
    one: the assertion above is what stops a variable being optional in the
    first place. The two together are do-NOT 6.
    """
    env = code_assembly._get_pyspark_jinja_env()
    source = env.loader.get_source(env, partial)[0]
    lines = [
        f"{number}: {line.strip()}" for number, line in enumerate(source.split("\n"), start=1) if "default(" in line
    ]
    assert not lines, (
        f"{partial} reads a variable through `default()`:\n  "
        + "\n  ".join(lines)
        + "\ndesign.md do-NOT 6 / R6.5: set the variable unconditionally in "
        "code_assembly's template_vars instead. `default()` converts "
        "StrictUndefined's loud error into a silent substitution, and under two "
        "jinja2 copies in one process it substitutes a foreign `missing` "
        "sentinel rather than the default — which broke all 18 tests in "
        "tests/integration/pipeline_flow/test_code_generation_e2e.py once "
        "(see _partials/_load_counts.py.j2's header)."
    )


def test_exactly_one_template_masks_an_unset_quarantine_bucket() -> None:
    """The pre-existing/new line, measured rather than declared.

    ``glue_job_s3_tables.py.j2:11`` carries a P28-era
    ``{%- set quarantine_bucket = quarantine_bucket | default('') %}`` from when
    it was the only template with a DQ block. The four 2b.2 ported to have no
    such line, so an unset ``quarantine_bucket`` fails their render loudly. That
    asymmetry matters: were ``template_vars["quarantine_bucket"]`` to become
    conditional, four templates would refuse to render and s3_tables would
    quietly emit ``_dq_quarantine_bucket = ""``, deferring the failure to R6.4's
    runtime refusal on a job that generated cleanly.

    The masking set is derived from behaviour and pinned at exactly one member,
    so this is not an allowlist of names: a fifth template acquiring a
    ``default()`` on a DQ variable lands in the set and fails here, and the
    pre-existing one being removed fails here too rather than passing silently.
    """
    env = code_assembly._get_pyspark_jinja_env()
    masking: list[str] = []
    for template_name in ALL_TEMPLATES:
        try:
            rendered = env.get_template(template_name).render(**_context(template_name, quarantine_bucket=None))
        except jinja2.exceptions.UndefinedError as error:
            assert "quarantine_bucket" in str(error), (
                f"{template_name}: an unset quarantine_bucket raised, but naming "
                f"something else ({error}) — the refusal is not attributable"
            )
            continue
        masking.append(template_name)
        assert '_dq_quarantine_bucket = ""' in rendered, (
            f"{template_name}: rendered without quarantine_bucket and did not raise, "
            f"yet did not fall back to the empty string either — the masking value "
            f"changed, so R6.4's runtime refusal may no longer be what fires"
        )

    assert masking == [PRE_EXISTING_DEFAULT_TEMPLATE], (
        f"templates masking an unset quarantine_bucket: {masking}, expected exactly "
        f"[{PRE_EXISTING_DEFAULT_TEMPLATE!r}].\n"
        f"That one is pre-existing (P28, glue_job_s3_tables.py.j2:11) and out of "
        f"p64 2b's scope. A NEW entry means a template acquired a `| default(...)` "
        f"on a DQ variable — do-NOT 6 forbids it; set the variable unconditionally "
        f"in template_vars. A MISSING entry means that P28 line went away, which is "
        f"an improvement, but update this test to say so deliberately rather than "
        f"leaving it red."
    )


# ---------------------------------------------------------------------------
# The loudness StrictUndefined provides, and the arm where it provides none
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", [name for name in ALL_TEMPLATES if name != PRE_EXISTING_DEFAULT_TEMPLATE])
def test_unset_quarantine_bucket_fails_the_render_loudly(template_name: str) -> None:
    """The four ported templates refuse rather than substituting.

    Asserted through ``code_assembly``'s own environment so the guarantee is
    tied to the one production builds, not to a test-local environment that
    happens to be configured the same way today. The paired positive — the same
    render succeeding once the variable is supplied — is what makes the failure
    attributable to the missing variable and nothing else.
    """
    env = code_assembly._get_pyspark_jinja_env()
    with pytest.raises(jinja2.exceptions.UndefinedError, match="quarantine_bucket"):
        env.get_template(template_name).render(**_context(template_name, quarantine_bucket=None))

    rendered = env.get_template(template_name).render(**_context(template_name))
    assert f'_dq_quarantine_bucket = "{QUARANTINE_BUCKET}"' in rendered


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_unset_data_quality_enabled_fails_CLOSED_rather_than_open(template_name: str) -> None:
    """R6.6's gate: an omitted gate disables the block, it never enables it.

    ``data_quality_enabled`` is read through ``is defined and`` — the same idiom
    ``dqdl_rules`` beside it uses — so an unset value is silent, exactly like an
    unset ``dqdl_rules``. That is deliberate and it is the whole reason the gate
    could be added without touching every render site in the suite; the partial's
    header records the two rejected alternatives (a bare ``{% if %}``, and
    ``| default(false)``, which is do-NOT 6).

    What matters, and what this pins, is the DIRECTION of that silence. A gate
    whose absence enabled the block would defeat R6.6 on any render path that
    forgot it. This asserts the opposite: absent means off, on all five templates,
    with a ruleset present so the suppression is attributable to the gate rather
    than to an empty ruleset.

    The paired positive is what keeps it from being vacuous — the same context with
    the gate supplied renders the block. And the guarantee that production never
    reaches the silent arm at all is
    ``test_every_p64_partial_variable_is_set_unconditionally_in_template_vars``,
    not this test.
    """
    env = code_assembly._get_pyspark_jinja_env()
    without_gate = env.get_template(template_name).render(**_context(template_name, data_quality_enabled=None))
    ast.parse(without_gate)

    assert EVALUATE_BANNER in without_gate, f"{template_name}: the banner renders regardless of the gate"
    for absent in ("EvaluateDataQuality", QUARANTINE_WRITE, '"step_name": "data_quality"'):
        assert absent not in without_gate, (
            f"{template_name}: {absent!r} rendered with data_quality_enabled UNSET. The gate "
            f"must fail CLOSED — if an omitted gate enables the DQ block, every render path "
            f"that forgets the variable silently reclassifies the pipeline, which is R6.6's "
            f"own failure mode reintroduced through the template."
        )

    with_gate = env.get_template(template_name).render(**_context(template_name))
    assert "EvaluateDataQuality" in with_gate and QUARANTINE_WRITE in with_gate


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_unset_dqdl_rules_is_silent_and_drops_the_whole_dq_body(template_name: str) -> None:
    """The arm with NO loudness — pinned so its absence is not mistaken for safety.

    ``dqdl_rules`` is read behind ``{% if dqdl_rules is defined and dqdl_rules %}``,
    so ``StrictUndefined`` never fires for it: the banner renders, the evaluator,
    the row split, the quarantine write and the report row all vanish, and the
    generated job is valid Python that quietly runs no quality checks. Rendering
    with a complete context cannot detect this, which is exactly why
    ``test_every_p64_partial_variable_is_set_unconditionally_in_template_vars``
    is the load-bearing guard and not this one.

    This asserts the CURRENT behaviour to make the exposure legible, not to bless
    it. Do not "fix" it by adding ``| default('')`` — that is do-NOT 6. The guard
    is upstream, in ``template_vars``.
    """
    env = code_assembly._get_pyspark_jinja_env()
    rendered = env.get_template(template_name).render(**_context(template_name, dqdl_rules=None))
    ast.parse(rendered)

    assert EVALUATE_BANNER in rendered, f"{template_name}: the banner renders regardless of the ruleset"
    for absent in ("EvaluateDataQuality", QUARANTINE_WRITE, '"step_name": "data_quality"'):
        assert absent not in rendered, (
            f"{template_name}: {absent!r} rendered with dqdl_rules UNSET. If this now "
            f"raises or renders instead, the `is defined` guard changed shape and the "
            f"silent-drop exposure this test records has moved — re-read R6.5."
        )


# ---------------------------------------------------------------------------
# The regression fence for the 18-test mechanism, over the DQ block
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("template_name", ALL_TEMPLATES)
def test_dq_block_survives_a_second_jinja2_copy_in_sys_modules(template_name: str) -> None:
    """The DQ block renders correctly when ``missing`` is no longer one object.

    The counts partial has this fence
    (``test_load_counts_code_assembly_render.py``); the DQ block had none, and
    2b.2 just put it on four more templates. Under two jinja2 copies any
    construct whose result depends on ``value is missing`` or
    ``isinstance(value, Undefined)`` silently changes meaning — that includes
    ``default()`` and, for an UNSET variable, ``is defined``. The DQ block must
    contain nothing of the kind that can bite: it has no ``default()`` at all,
    and its one ``is defined`` reads a variable ``template_vars`` always sets, so
    it is answered by a real value rather than by sentinel identity.

    Run in the unit layer deliberately. The failure this reproduces surfaced in
    ``tests/integration/pipeline_flow/test_code_generation_e2e.py``, a layer
    ``make test-fast`` does not run.
    """
    env = code_assembly._get_pyspark_jinja_env()
    expected = env.get_template(template_name).render(**_context(template_name))

    with _second_jinja2_copy_in_sys_modules():
        second_env = code_assembly._get_pyspark_jinja_env()
        under_two_copies = second_env.get_template(template_name).render(**_context(template_name))

    assert under_two_copies == expected, (
        f"{template_name}: the rendered job differs when a second jinja2 copy is in "
        f"sys.modules. Something in the DQ block now depends on `missing`/`Undefined` "
        f"identity — almost certainly a `| default(...)` (do-NOT 6). This is the "
        f"condition that broke all 18 tests in test_code_generation_e2e.py."
    )
    ast.parse(under_two_copies)
    assert "EvaluateDataQuality" in under_two_copies and QUARANTINE_WRITE in under_two_copies


def test_second_jinja2_copy_helper_is_restored() -> None:
    """The helper must not leak a foreign jinja2 into the rest of the session."""
    before = sys.modules["jinja2"]
    with _second_jinja2_copy_in_sys_modules() as second:
        assert sys.modules["jinja2"] is second
    assert sys.modules["jinja2"] is before
    assert code_assembly.jinja2 is before
