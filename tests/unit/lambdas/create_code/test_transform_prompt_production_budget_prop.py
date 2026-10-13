# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 1 — the production render is measured, across the whole field-count range.

p52 task 2.2 / R1.1, R1.2, R1.6. The example half of this lives in
`test_transform_prompt.py`'s p52 section: two flat assertions at ten fields,
`< 600` with no target schema and `< 725` with `LIVE_TARGET_SCHEMA`, against
measured figures of 505 and 604. Those two are **unchanged by this file** and this
property does not replace them — it covers the space they can only sample.

**Validates: Requirements 1.1, 1.2, 1.6**

────────────────────────────────────────────────────────────────────────────
THE CONFLICT THIS FILE HAD TO RESOLVE, AND THE ARM TAKEN
────────────────────────────────────────────────────────────────────────────

Property 1 as `design.md` words it asks for field counts 1–20 under "the ceiling
asserted for that shape". Re-measured on this tree with the project venv against
`test_transform_prompt._estimate_tokens`, a **flat** ceiling does not survive that
range:

    ceiling 600, no target schema : 15 fields fit at 585;  16 trips it at 601
    ceiling 725, + LIVE_TARGET_SCHEMA: 17 fields fit at 716;  18 trips it at 732

So a property generating 1–20 and asserting a flat `< 600` fails at 16, 17, 18, 19
and 20 — not because production is broken, but because 600 was chosen for the
ten-field example case and a flat number cannot describe a quantity that grows with
field count. Both trip points reproduce the figures task 2.1 recorded in its comment
block; they were re-derived here rather than taken on trust.

Three arms were available. **(b) is taken.**

*(a) Narrow the generated range to what the flat ceiling supports — n ≤ 15 with no
target schema, n ≤ 17 with one.* Honest, and the smallest change. Rejected on two
grounds. It makes the property silent about exactly the field counts most likely to
grow: a mapping is a target schema's width, and R1.6's own `LIVE_TARGET_SCHEMA`
already declares eleven fields, so 15 is not a comfortable distance from live. And
the bound becomes a magic number with no stated meaning — "15" would be an artefact
of a ceiling chosen for a different field count, and the next reader has no way to
tell that from a considered limit.

*(c) Keep the flat ceiling and mark 16–20 expected-over.* Rejected. It writes a
known-failing range into the suite as acceptable, which teaches the next reader that
this property's failures are noise. That is worse than not having the property.

*(b) Assert a ceiling **derived** as a function of field count — a measured fixed
cost plus a measured per-mapping-line cost — so the property holds across the whole
1–20 range and a breach names what grew.* This is design constraint 4 and
`LGR-c284fbb-5` (**template / ratified**), which reached the same conclusion about a
14-character budget: "Because 14 characters is a snapshot rather than a margin, the
guard is a **property** … and asserts the measured offsets alongside, so a future
edit that lengthens either string names *what* grew instead of failing obscurely."
The flat 600 and 725 are snapshots at n=10 in exactly that sense. Decomposing them
is what lets one assertion cover 1–20 and still say something.

Note what arm (b) does **not** do. It does not amend, relax or duplicate the two
flat assertions (p52 do-NOT 2, do-NOT 3; the amend-in-place convention is
`LGR-9e76249-10` / `LGR-60c4985-6`, both **template / ratified** and both carrying
`NOTE: BROAD`, so conventions rather than fences on this file). Their subject is a
ceiling a human chose with stated headroom at the one field count production was
observed at; this property's subject is that the render stays *proportional* as the
field count moves. Different subjects, both worth pinning.

────────────────────────────────────────────────────────────────────────────
THE FORMULA, AND EVERY NUMBER IN IT
────────────────────────────────────────────────────────────────────────────

    ceiling_tokens = (FIXED_COST_CHARS + n × PER_LINE_CHARS) // 4 + ALLOWANCE

    where PER_LINE_CHARS = 54 + len(hint) + len(str(confidence))

Measured with `agentic_data_ingestion/.venv/bin/python` against this module's
imported `_estimate_tokens` (`len(text) // 4`), rendering the real template through
the real `load_prompt_template`:

  * **FIXED_COST_CHARS = 1402** with no target schema, **1799** with
    `LIVE_TARGET_SCHEMA`. Both taken at n=0, i.e. an empty `mappings` list. The
    difference, **397 chars**, is the target-schema block for eleven declared fields,
    and it is *constant in n* — verified across n=0…20, which is why the two shapes
    need two constants rather than a per-field schema term.
  * **54 chars** of per-mapping-line structure, around whatever the record carries.
    The rendered line is
    `  source_field_<i> → target_field_<i>, <hint> [confidence: <conf>]` plus its
    newline, and there are **no type parentheses** — production writes neither
    `source_type` nor `target_type` (p52 gate 1.1). Verified as an identity, not a
    curve fit: the observed per-line cost equals `54 + len(hint) + len(str(conf))`
    for every hint from 1 to 160 chars and every `confidence` in 0.01…1.00, with
    zero mismatches over a 2000-point grid.
  * The **54** uses the two-digit field index, which is the worst case for n ≤ 20.
    For n ≤ 10 every index is one digit and costs 52, so the bound over-estimates by
    up to 5 tokens there. That is correct behaviour for a ceiling and is **not** part
    of the allowance below.
  * **ALLOWANCE = 40 tokens** (160 chars). This is the only chosen number in the
    formula, and it is an allowance for *template* growth rather than headroom over a
    measurement — the two terms above already track the render to within those 5
    tokens. Sensitivity, so the choice is legible: a fixed-cost edit of +160 chars
    trips it at every field count; a per-mapping-line edit trips it at +160 chars for
    n=1, +16 for n=10, +8 for n=20. It is deliberately absolute rather than
    proportional, so the allowance does not silently widen as field counts grow.

    It is deliberately **larger** than the 22-char regression the template's own
    comment records (~22 tag-only lines took a 10-field render from 498 to 504). That
    incident is a *fixed-cost* regression and the four `< 500` assertions are its
    detector, with about 5 tokens of headroom. Reproducing that sensitivity here would
    make two tests fail for one cause and tell a reader nothing new. What this
    property adds is the axis no other assertion covers: per-line growth at high field
    counts.

Verified before this file was written: the formula holds with zero failures over
n=1…20 × both target-schema shapes × ten hint lengths (1 to 160 chars) × five
confidence values — 2000 renders — with slack between 40 and 45 tokens throughout.
Tightest cell: n=1, no target schema, 6-char hint, confidence 0.01 (366 tokens
against 406).

Relationship to the flat ceilings, stated because the derived form is **tighter**
where they were chosen. At n=10, hint `direct`, confidence 0.95: derived 550 vs flat
600 (actual 505) with no target schema, and derived 649 vs flat 725 (actual 604) with
one. So a template edit that pushed the ten-field render to 560 would fail here and
pass there. That is the property doing its job — the flat number was chosen with
headroom for a human to read, the derived one is a statement about proportionality —
and it is why this file is additive rather than a replacement.

────────────────────────────────────────────────────────────────────────────
WHAT IS GENERATED, AND WHY THAT RANGE
────────────────────────────────────────────────────────────────────────────

**Field count 1–20**, per the task. n=0 is excluded: it is the fixed cost, which the
formula's own constant is taken from, so generating it would assert a tautology.

**Both target-schema shapes**, per R1.6, which names `LIVE_TARGET_SCHEMA`
specifically — the eleven-field live shape p37 measured, imported rather than rebuilt
so it cannot drift from the shape the `< 650` and `< 725` assertions use. Coverage of
both is *guaranteed* by `@example`, not left to sampling.

**Hint text of 1 to 160 chars, over hypothesis's full default alphabet**, and this is
the deliberate part. Hint text is authored by the **model**, so p52 risk N3 accepts
that a run whose model writes a long hint on every field renders 865/964 tokens and
exceeds both flat ceilings with no test failing. A flat ceiling cannot honour that
axis; a derived one can, because `len(hint)` is a term in it — what is asserted is
that the *template* adds no more than 54 chars of structure around whatever the
record carries, which is the only part of the render this repository controls.
Confirmed in the worst cell: 20 fields each carrying a 150-char hint renders
1385/1484 tokens against derived bounds of 1430/1529. 160 is the upper bound because
risk N3's observed worst case is 150, plus margin.

Newlines and control characters are **included**, having been checked rather than
assumed: `Token_Estimate` is `len // 4` and a newline is one character like any
other, so the identity above holds for `'a\\nb'`, `'a\\rb'`, tabs, non-ASCII and
zero-width characters alike. Jinja does no escaping here — `load_prompt_template`
leaves `autoescape` at its default (`prompt_template_loader.py:52-58`) — so
`'<&">'`, `"a'b"` and even `'{{ x }}'` pass through at their own length and are not
re-evaluated. All verified before this file was written.

**Confidence 0.01 … 1.00**, as `i / 100`, which keeps `str()` to three or four
characters and so keeps the formula's `len(str(confidence))` term honest — an
arbitrary float can repr to 18 characters and would be measuring Python's float
formatting rather than the template. Always truthy, deliberately: the template guards
the annotation with `{% if m.get('confidence') %}`, and production always writes a
truthy confidence (gate 1.1 — the deterministic fallback writes
`min(best_score, FALLBACK_CONFIDENCE_CAP)`), so a falsy value would render a line
production does not send.

────────────────────────────────────────────────────────────────────────────
DELIBERATELY NOT ASSERTED HERE
────────────────────────────────────────────────────────────────────────────

* **A wider target schema than `LIVE_TARGET_SCHEMA`.** R1.6 names two shapes and the
  block's cost is a constant in this formula. The axis figure is recorded instead:
  ~25 chars (~6 tokens) per declared field, and 29 declared fields still fits the
  flat 725 at 719. A property over schema width belongs with `:260`/`:264`, whose
  subject that is.
* **The Fixed_Cost.** Owned by the four `< 500` assertions (p52 R1.5). This property
  reads the fixed cost only to name it in a failure message.
* **What the template renders.** Property 2 in `test_prompt_fixture_shape_guard_prop.py`
  and the guard in `test_prompt_fixture_shape_guard.py` own branch coverage and the
  fixture-versus-producer key set. The Production_Shape render-shape check below is
  scoped to keeping *this* measurement honest — a fixture that stopped rendering the
  production branch would make the figures meaningless — not to re-asserting R2.

Pure: no AWS, no moto, no network. Renders the real template and reads no file
directly.
"""

import os
import sys

import hypothesis.strategies as st
from hypothesis import example, given, settings

# Region is pinned by tests/unit/conftest.py (`LGR-3e45e65-1`, template /
# ratified); these are no-ops kept for parity with the sibling files here.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# The same shared-path insert `test_transform_prompt.py:8` uses, so the real
# `prompt_template_loader` renders the real template.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

from prompt_template_loader import load_prompt_template

from .test_transform_prompt import (
    LIVE_TARGET_SCHEMA,
    _estimate_tokens,
    _make_production_mappings,
)

# ──────────────────────────────────────────────────────────────────────
# The measured constants. See the module docstring for how each was taken.
#
# All four are amended IN PLACE with the new measurement and the reason, never
# deleted and never widened to make a red go away — `LGR-9e76249-10` /
# `LGR-60c4985-6`, the amend-in-place convention for a budget assertion.
# ──────────────────────────────────────────────────────────────────────

#: Chars rendered with an empty `mappings` list and no `target_schema`. n=0.
_FIXED_COST_CHARS_NO_TARGET_SCHEMA = 1402

#: Chars rendered with an empty `mappings` list and `LIVE_TARGET_SCHEMA`. n=0.
_FIXED_COST_CHARS_LIVE_TARGET_SCHEMA = 1799

#: The eleven-field target-schema block's own cost. Constant in field count —
#: verified across n=0…20, which is what makes two fixed constants sufficient.
_LIVE_TARGET_SCHEMA_BLOCK_CHARS = _FIXED_COST_CHARS_LIVE_TARGET_SCHEMA - _FIXED_COST_CHARS_NO_TARGET_SCHEMA

#: Per-mapping-line structure, excluding the hint and the confidence value: the
#: two-space indent, both `<name>_field_<i>` names, ` → `, `, `,
#: ` [confidence: `, `]` and the newline. Uses the TWO-digit field index, the
#: worst case for n ≤ 20; a one-digit index costs 52, so the bound
#: over-estimates by up to 5 tokens for n ≤ 10. Correct for a ceiling.
_PER_MAPPING_LINE_STRUCTURE_CHARS = 54

#: The one chosen number here: allowance for TEMPLATE growth, not headroom over
#: a measurement. 40 tokens = 160 chars. Absolute rather than proportional so it
#: cannot widen as field counts grow. See the docstring for its sensitivity and
#: for why it is deliberately looser than the 22-char fixed-cost incident.
_TEMPLATE_GROWTH_ALLOWANCE_TOKENS = 40

#: Recorded beside the ceiling so a breach names what grew rather than failing
#: obscurely (p52 constraint 4, `LGR-c284fbb-5`). Measured on this tree.
_AXIS_COSTS = (
    "per-axis costs: `confidence` ~5 tokens/field; a declared target field ~25 chars (~6 tokens); "
    "a 150-char hint ~36 tokens on each field carrying one"
)

#: Sampled as a label rather than as the schema dict itself, so a hypothesis
#: falsifying example reads `'live-target-schema'` instead of an eleven-key dict.
_NO_SCHEMA = "no-target-schema"
_LIVE_SCHEMA = "live-target-schema"

#: Hints in the shape production writes them. `"direct"` is the literal the
#: deterministic fallback emits (`schema_mapping/lambda_function.py:1222`); the
#: rest are the kind of short phrase the Bedrock contract asks the model for.
_REALISTIC_HINTS = [
    "direct",
    "trim",
    "to_upper",
    "to_date",
    "cast to string",
    "parse with format yyyy-MM-dd",
]


def _derived_ceiling_tokens(field_count: int, schema_label: str, hint: str, confidence: float) -> int:
    """The ceiling for one render, derived from the measured axis costs.

    Not a prediction of the render — a bound on it. The two structural terms are
    measured constants and the two data terms (`hint`, `confidence`) are the
    record's own content, so what this bounds is the template's structure. See
    the module docstring for the derivation and for every figure.
    """
    fixed = _FIXED_COST_CHARS_LIVE_TARGET_SCHEMA if schema_label == _LIVE_SCHEMA else _FIXED_COST_CHARS_NO_TARGET_SCHEMA
    per_line = _PER_MAPPING_LINE_STRUCTURE_CHARS + len(hint) + len(str(confidence))
    return (fixed + field_count * per_line) // 4 + _TEMPLATE_GROWTH_ALLOWANCE_TOKENS


def _render(field_count: int, schema_label: str, hint: str, confidence: float) -> str:
    """Render the real template with a Production_Shape mapping list."""
    kwargs = {"mappings": _make_production_mappings(field_count, hint=hint, confidence=confidence)}
    if schema_label == _LIVE_SCHEMA:
        kwargs["target_schema"] = LIVE_TARGET_SCHEMA
    return load_prompt_template("transform_generation.j2", **kwargs)


@settings(max_examples=200, deadline=None)
@given(
    field_count=st.integers(min_value=1, max_value=20),
    schema_label=st.sampled_from([_NO_SCHEMA, _LIVE_SCHEMA]),
    hint=st.one_of(st.sampled_from(_REALISTIC_HINTS), st.text(min_size=1, max_size=160)),
    confidence=st.integers(min_value=1, max_value=100).map(lambda i: i / 100),
)
# Coverage of the cells that matter is GUARANTEED here rather than left to
# sampling — R1.6 names both target-schema shapes, and the trip points are the
# whole reason this property is derived rather than flat.
@example(field_count=10, schema_label=_NO_SCHEMA, hint="direct", confidence=0.95)  # the flat `< 600` cell
@example(field_count=10, schema_label=_LIVE_SCHEMA, hint="direct", confidence=0.95)  # the flat `< 725` cell
@example(field_count=1, schema_label=_NO_SCHEMA, hint="direct", confidence=0.95)  # tightest cell measured
@example(field_count=16, schema_label=_NO_SCHEMA, hint="direct", confidence=0.95)  # flat 600 trips here (601)
@example(field_count=18, schema_label=_LIVE_SCHEMA, hint="direct", confidence=0.95)  # flat 725 trips here (732)
@example(field_count=20, schema_label=_NO_SCHEMA, hint="x" * 150, confidence=1.0)  # risk N3 territory
@example(field_count=20, schema_label=_LIVE_SCHEMA, hint="x" * 150, confidence=1.0)  # risk N3, both shapes
def test_the_production_render_stays_within_its_derived_ceiling(
    field_count: int, schema_label: str, hint: str, confidence: float
) -> None:
    """Property 1: the production render is measured, for every field count 1–20.

    *For any* field count between 1 and 20, rendering `transform_generation.j2`
    with Production_Shape mapping records — with and without
    `LIVE_TARGET_SCHEMA` — produces a Token_Estimate at or below the ceiling
    derived for that shape.

    The ceiling is a function of field count rather than a flat number, because
    a flat one is a snapshot at one field count and demonstrably does not hold
    over this range (600 trips at 16 fields, 725 at 18). The module docstring
    carries the three arms, the measurements, and why this one.

    **Validates: Requirements 1.1, 1.2, 1.6**
    """
    prompt = _render(field_count, schema_label, hint, confidence)
    tokens = _estimate_tokens(prompt)
    ceiling = _derived_ceiling_tokens(field_count, schema_label, hint, confidence)

    if tokens > ceiling:
        # Only on failure: re-measure each term so the message names WHAT grew
        # instead of only that something did (p52 constraint 4,
        # `LGR-c284fbb-5`). Both renders are cheap and neither is asserted on.
        fixed_chars = len(_render(0, schema_label, hint, confidence))
        expected_fixed = (
            _FIXED_COST_CHARS_LIVE_TARGET_SCHEMA if schema_label == _LIVE_SCHEMA else _FIXED_COST_CHARS_NO_TARGET_SCHEMA
        )
        per_line_chars = (len(prompt) - fixed_chars) / field_count
        expected_per_line = _PER_MAPPING_LINE_STRUCTURE_CHARS + len(hint) + len(str(confidence))

        raise AssertionError(
            "the Production_Shape transform prompt is over its derived ceiling.\n"
            f"  field count      : {field_count}\n"
            f"  target schema    : {schema_label}"
            f"{' (11 declared fields)' if schema_label == _LIVE_SCHEMA else ''}\n"
            f"  hint             : {len(hint)} chars {hint[:40]!r}{'…' if len(hint) > 40 else ''}\n"
            f"  confidence       : {confidence!r} ({len(str(confidence))} chars rendered)\n"
            f"  MEASURED         : ~{tokens} tokens / {len(prompt)} chars\n"
            f"  derived ceiling  : {ceiling} tokens "
            f"= ({expected_fixed} + {field_count} × {expected_per_line}) // 4 "
            f"+ {_TEMPLATE_GROWTH_ALLOWANCE_TOKENS}\n"
            "  which term grew:\n"
            f"    fixed cost       : measured {fixed_chars} chars, expected {expected_fixed} "
            f"(delta {fixed_chars - expected_fixed:+d})\n"
            f"    per mapping line : measured {per_line_chars:.1f} chars, expected <= {expected_per_line} "
            f"(delta {per_line_chars - expected_per_line:+.1f})\n"
            f"  {_AXIS_COSTS}\n"
            "  If the growth is deliberate, AMEND the constant that moved with the new "
            "measurement and the reason beside it — do not widen "
            "_TEMPLATE_GROWTH_ALLOWANCE_TOKENS to absorb it, and do not touch the flat "
            "`< 500` / `< 600` / `< 725` assertions in test_transform_prompt.py, whose "
            "subjects are different (p52 do-NOT 2, do-NOT 3)."
        )

    # The render really is Production_Shape, so the figure above is a measurement
    # of the prompt a pipeline sends and not of a differently-spelled fixture.
    # Narrow on purpose: R2's branch coverage is owned by
    # test_prompt_fixture_shape_guard.py, and this only keeps THIS measurement
    # honest.
    #
    # `transformation` takes the template's PRIMARY branch (`transform`, the
    # Fixture_Shape key, reaches only the `elif` fallback), the confidence
    # annotation renders, and there are NO type parentheses — production writes
    # neither `source_type` nor `target_type`, which is why these figures sit 48
    # tokens below the ones p52's design.md recorded at ten fields.
    assert f"  source_field_0 → target_field_0, {hint} [confidence: {confidence}]" in prompt, (
        "the Production_Shape mapping line has changed shape, so the token figure above is no "
        "longer a measurement of the production render. Note the absence of type parentheses is "
        "deliberate — see `_make_production_mappings`' docstring in test_transform_prompt.py."
    )
    if schema_label == _LIVE_SCHEMA:
        assert "TARGET SCHEMA declares these 11 field(s)" in prompt, (
            "the target-schema block did not render, so this example is a duplicate of the "
            "no-target-schema case and R1.6's second shape is going unmeasured."
        )
    else:
        assert "TARGET SCHEMA declares" not in prompt, (
            "the no-target-schema render emitted a target-schema block, so the 1402-char fixed "
            "cost this ceiling is derived from no longer describes it."
        )
