# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Properties 2 and 3 — the mapping line's two-branch structure, and the inert schema slot.

p52 task 2.4. Property 2 (R2.1, R2.3, R2.4) and Property 3 (R4.4).

**Validates: Requirements 2.1, 2.3, 2.4, 4.4**

────────────────────────────────────────────────────────────────────────────
WHAT PROPERTY 2 ASSERTS THAT TASK 2.3's EXAMPLE GUARD DOES NOT
────────────────────────────────────────────────────────────────────────────
`test_prompt_fixture_shape_guard.py` already asserts branch **coverage** — that every
key `transform_generation.j2` branches on appears in some fixture, that the keys
production writes appear in the Production_Shape fixture specifically, that the primary
`transformation` branch renders, and that Fixture_Shape cannot reach it. It does that on
**two fixed, homogeneous fixtures**: `_make_mappings(6)` and `_make_production_mappings(6)`,
every record in each list carrying the same keys.

Re-asserting coverage here at a different field count would add nothing, so this file
does not. What it varies is exactly what 2.3 holds fixed:

* **field count** — 1 to 8 records per render, versus 6 and 1;
* **presence, absence AND falsiness of each branch key, independently per record** —
  2.3's records are all-present or all-absent for a given key, so it cannot see a
  template that treats `''`/`None`/`0` as present;
* **mixed shapes inside one list** — a Fixture_Shape record next to a Production_Shape
  record next to a record carrying neither hint key. Every budget assertion and every
  guard in this directory renders a homogeneous list, so nothing today would catch a
  template whose mapping line rendered correctly only for the first record, or only for a
  list whose records agree.

And what it asserts is not coverage but **branch selection**: each rendered line is
compared, in full and by position, against the line its own key set predicts. That makes
the whole two-branch structure the subject —

    {% if m.get('transformation') %}…{% elif m.get('transform') %}…{% endif %}
    {% if m.get('confidence') %} [confidence: …]{% endif %}

— rather than the presence of a key somewhere in a fixture. Structural comparison over
substring matching is `LGR-b46a5bb-4` and `LGR-9e76249-5` (both **template / ratified**,
the second recording that substring matching "fails in both directions on real inputs");
here it is not merely preferable but necessary, because a generated hint may legitimately
contain `,`, `[`, `]`, `→` or the other record's hint text, and any `in prompt` check
would then pass or fail for the wrong reason.

Three findings fall out of the generative form and are asserted rather than left implicit:

1. **A Production_Shape record whose `transformation` is falsy renders no hint at all.**
   There is no fallback for it to reach: production writes no `transform` key (p52 gate
   1.1), so the `elif` is dead on the live path. "Production takes the primary branch" is
   therefore a claim about the record's *value*, not about its *shape* — 2.3's
   `hint="direct"` fixture cannot distinguish the two.
2. **`notes` contributes zero bytes.** It is in Production_Shape because the producer
   writes it, and the template never reads it. The oracle ignores it, so every render
   whose base record carries it proves the cost is nil — the reason
   `_make_production_mappings`' docstring can call it free.
3. **The primary branch wins differentially, not just per the oracle.** When a record
   carries both hint keys, the whole list is re-rendered with `transform` stripped and the
   two renders must be identical. That is a statement about the real template with no
   oracle in the path, which is what keeps R2.4 from resting on this file's own model of
   the mapping line.

────────────────────────────────────────────────────────────────────────────
"PRODUCTION-SHAPED", AND WHY THE TITLE OF PROPERTY 2 IS NOT TAKEN LITERALLY
────────────────────────────────────────────────────────────────────────────
Property 2's title in `design.md` reads "covered by a **production-shaped** fixture", and
inherits the defect gate 1.1 flagged in R2.1: three of the five keys the template branches
on — `source_type`, `target_type`, `transform` — are **precisely the keys production never
writes**. A Production_Shape fixture carrying `transform` would not be Production_Shape,
and would re-create the divergence p52 exists to remove.

This file follows the resolution `test_prompt_fixture_shape_guard.py`'s module docstring
sets out at length under "HOW R2.1 IS READ HERE, AND WHY", and does not re-decide it:

* **union coverage** — the branch keys are exercised across the module's fixtures,
  Fixture_Shape ∪ Production_Shape. Here that is generalised: the *generator's* key space
  is asserted equal to the set discovered from the template, so a new branch key fails
  `test_the_oracle_covers_every_key_the_template_branches_on` naming the key (R2.3)
  instead of going silently ungenerated.
* **Production_Shape-specific coverage** — the branch keys production *does* write,
  computed as `template branch keys ∩ producer keys` and never transcribed, must be
  exercised by a Production_Shape render.
  `test_a_production_render_takes_every_branch_production_writes` is that half, and it
  reads both sides through 2.3's own discovery helpers so the intersection is derived
  here too. On this tree it resolves to `{confidence, transformation}`.

Both helpers — `_template_branch_keys()` and `_sole_mapping_record_writer()` — are
**imported** from `test_prompt_fixture_shape_guard.py` rather than reimplemented. Two
independent raw-template parsers in one directory is its own drift source, and the second
would be the more likely to rot: the AST sweep in particular asserts a repo-wide hit count
of exactly 1, so a duplicate would have to be kept in step with `LGR-7d19eca-2`
(**standing invariant** — discover writers by key shape, never enumerate them by file and
line).

**One transcription is deliberate and fenced.** `_expected_mapping_line` names all five
branch keys, because an oracle for a rendering cannot be written without knowing what each
key renders as. What makes that safe is `test_the_oracle_covers_every_key_the_template_
branches_on`, which asserts the transcribed set **equals** the discovered set. The
transcription cannot drift silently; it can only fail loudly.

────────────────────────────────────────────────────────────────────────────
PROPERTY 3 — WHAT IS GENERATED, AND WHAT BYTE-IDENTITY DOES AND DOES NOT PROVE
────────────────────────────────────────────────────────────────────────────
R4.4: the no-target-schema render is byte-identical whatever `unsourced_target_fields`
carries. `test_transform_prompt.py`'s
`test_an_omitted_unsourced_target_fields_renders_byte_identically` covers three shapes —
omitted, `None`, `[]` — against each other and against the bare render. This generates
over the space instead:

* **`unsourced_target_fields`**: omitted, `None`, `[]`, lists of 1–6 names (including the
  real `target_field_<i>` names, so a *matching* name is reached and not just a stranger),
  lists carrying non-string members (`int`, `bool`, `None`, tuple, dict), a bare string,
  and a dict. The last three are not list-shaped at all: `| default([], true)` only
  absorbs a falsy value, so a truthy non-list reaches `{% if name in unsourced_fields %}`
  as-is and its inertness is a fact about the block never rendering, not about the filter.
* **the no-schema shapes it is crossed with**: `target_schema` omitted, `None`, `{}`,
  `{"type": "object"}` (no `properties` at all — the shape the template's own comment says
  "renders no block"), and `{"type": "object", "properties": {}}`. Only the first and
  third are covered anywhere today.
* **mapping lists**: Fixture_Shape, Production_Shape and the mixed generator above, 0–6
  records, so the identity is not an artefact of one fixture. Zero records is included
  here (unlike Property 1, where it would assert the fixed cost against itself).

**Why this matters more than it looks.** The template's target-schema block emits **zero
bytes** when there is no schema, and that is a consequence of whitespace control: every
tag in it shares a line with its content and uses the trailing-dash form, and the
`unsourced_fields` `set` shares a line with the `target_fields` `set` above it. The
template's own comment records the incident — ~22 tag-only lines took a 10-field render
from 498 to 504 against a 500-token ceiling — and p52 **do-NOT 5** forbids tidying those
forms. This property pins the *consequence*: whatever a caller passes for
`unsourced_target_fields`, a no-schema render cannot gain a byte.

Stated honestly, in the shape that example test's own docstring uses: byte-identity
between two renders that both omit `target_schema` proves the slot is **inert**. It does
not by itself prove the block emits zero *bytes* — a tidy-up that added a newline would
add it to both sides and pass here. That failure has a different detector: the four
`< 500` Fixed_Cost assertions, which sit ~5 tokens from their ceiling for exactly this
reason. The two together are what keep do-NOT 5 enforced.

`LGR-a419843-2` (**template / ratified**) — the fence that keeps the source schema out of
this prompt — is honoured **by construction**: p52 edits no prompt template, and this file
only reads one. Its `revisit_when` reads "the transform prompt starts being shown the
source schema", and nothing here shows it anything new, so that condition has **not**
fired (p52 do-NOT 11).

────────────────────────────────────────────────────────────────────────────
DO NOT ADD PROSE TO `transform_generation.j2`
────────────────────────────────────────────────────────────────────────────
Reached from this file only through 2.3's `_mapping_line_loop_body()`, which reads the
template raw. `tests/unit/lambdas/test_code/test_converter_vocabulary_closure_prop.py`
also reads it raw and harvests function names out of it by regex — prose in its comments
has already injected two non-existent function names once. Read this template; never
write to it (p52 do-NOT 7).

────────────────────────────────────────────────────────────────────────────
NON-VACUITY, VERIFIED BY MUTATION
────────────────────────────────────────────────────────────────────────────
Task 2.3 verified its guard by mutation; the same was done here before this file was
considered done, because a property that passes on a broken template is worse than none.
Method: the template was **copied** to a temp directory and mutated there, and the loader
was pointed at the copy through its own `PROMPT_TEMPLATES_DIR` escape hatch
(`prompt_template_loader.py:42-51`, which logs a warning for exactly this reason). The
versioned template was never written to (do-NOT 7). Five mutants, all killed:

1. hint branch order swapped, so the fallback wins → the per-record oracle and the
   differential check both fail;
2. the two hint branches made independent `{% if %}`s, so both render → the differential
   check fails, which is the failure mode that would silently under-measure every budget
   figure for a record carrying only one key;
3. `confidence` guarded on presence (`'confidence' in m`) rather than truthiness → the
   oracle fails on the present-but-falsy records, which no fixed fixture in this directory
   builds;
4. the `source_type` branch deleted → `test_the_oracle_covers_every_key_the_template_
   branches_on` fails on its *second* direction, the stale-key one;
5. the target-schema block made to fire on a truthy `unsourced_fields` → Property 3 fails.

Generation reach, measured over 300 examples of `_mixed_mapping_lists()` rather than
assumed: 453 records with both hint keys truthy, 232 examples containing records of
differing rendered shape, 203 mixing a fixture base with a production base, and every one
of the five branch keys hit in all three arms — absent, present-and-falsy,
present-and-truthy — several hundred times each.

Two of this file's own drafts were falsified by hypothesis rather than by review, which is
recorded because both were the kind of mistake that reads as correct: a differential check
that stripped `transform` from *every* record (wrong — a record carrying only `transform`
legitimately loses its hint), and a `confidence` strategy that fed a string to
`_make_production_mappings`, whose `notes` field formats it with `:.2f`.

────────────────────────────────────────────────────────────────────────────
FIGURES
────────────────────────────────────────────────────────────────────────────
No token figure is asserted here; budgets are Property 1's subject
(`test_transform_prompt_production_budget_prop.py`) and the flat ceilings'
(`test_transform_prompt.py`). Where a figure is mentioned in passing, note that
`design.md`'s 553 / 652 for the ten-field Production_Shape renders are **not** current:
task 2.1 measured **505** and **604**, and established the 48-token cause (design.md's
fixture carried `source_type` and `target_type`). Do not cite 553 / 652.

Pure: no AWS, no moto, no network. Renders the real template through the real loader, and
reads files only via the two imported discovery helpers.
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

# Discovery, imported rather than reimplemented — see the module docstring on why a
# second raw-template parser (and a second repo-wide AST sweep) in this directory would
# be a drift source of its own.
from .test_prompt_fixture_shape_guard import _sole_mapping_record_writer, _template_branch_keys
from .test_transform_prompt import _make_mappings, _make_production_mappings

_TEMPLATE = "transform_generation.j2"

#: Discovered from the raw template. `frozenset` so the module-level cost is paid once.
_TEMPLATE_BRANCH_KEYS = frozenset(_template_branch_keys())

#: The producer's own key set, from the single literal mapping-record write the AST sweep
#: finds. The path and line are kept only for failure messages — pinning them would be
#: the enumeration `LGR-7d19eca-2` rejects.
_PRODUCER_PATH, _PRODUCER_LINE, _PRODUCER_KEYS = _sole_mapping_record_writer()

#: The branch keys production actually writes. **Computed, never transcribed** (R2.2's
#: mechanism, `LGR-28b4d75-5`). `{confidence, transformation}` on this tree.
_PRODUCTION_BRANCH_KEYS = _TEMPLATE_BRANCH_KEYS & _PRODUCER_KEYS

#: The keys `_expected_mapping_line` below knows how to render. The one deliberate
#: transcription in this file, fenced by
#: `test_the_oracle_covers_every_key_the_template_branches_on`.
_ORACLE_BRANCH_KEYS = frozenset(
    {
        "source_type",
        "target_type",
        "transformation",
        "transform",
        "confidence",
    }
)


# ──────────────────────────────────────────────────────────────────────
# The oracle: what the template's mapping line renders for a given record
# ──────────────────────────────────────────────────────────────────────


def _expected_mapping_line(record: dict) -> str:
    """The single line `transform_generation.j2` renders for one mapping record.

    A transcription of the template's mapping line, deliberately (see the module
    docstring). Note three things it encodes that no other test in this directory does:

    * **truthiness, not presence.** Every branch is `{% if m.get(k) %}`, so `''`, `None`,
      `0` and `0.0` render nothing while still being present in the record.
    * **the `elif`.** `transform` is consulted only when `transformation` is falsy.
    * **`str()`**, not `repr()`, for non-string values — jinja's own conversion. Verified
      against the real render for `int`, `bool`, `float`, numeric strings and `1e-05`.

    Keys the template never reads (`notes`, and anything else a record carries) are
    ignored here, which is how their zero cost is asserted.
    """
    line = f"  {record['source_field']}"
    if record.get("source_type"):
        line += f" ({record['source_type']})"
    line += f" → {record['target_field']}"
    if record.get("target_type"):
        line += f" ({record['target_type']})"
    if record.get("transformation"):
        line += f", {record['transformation']}"
    elif record.get("transform"):
        line += f", {record['transform']}"
    if record.get("confidence"):
        line += f" [confidence: {record['confidence']}]"
    return line


def _rendered_mapping_lines(prompt: str) -> list[str]:
    """The rendered mapping lines, in render order.

    Located structurally rather than by slicing at a line number: a mapping line is
    indented two spaces and carries the `→` separator. The target-schema block's lines
    are also two-space indented but carry no arrow, and the one other arrow in the
    template (`all-direct → df_transformed = df`, in CRITICAL RULES) is not indented.

    This is why every generated value below excludes line-boundary characters: a hint
    containing `\\n` — or `\\v`, `\\x1c`, `\\u2028` and the rest of what
    `str.splitlines()` breaks on — would split one mapping line into two and make the
    positional comparison meaningless. Excluding them is a limit of the oracle, not a
    claim about the template.
    """
    return [line for line in prompt.splitlines() if line.startswith("  ") and "→" in line]


def _render(records: list) -> str:
    return load_prompt_template(_TEMPLATE, mappings=records)


# ──────────────────────────────────────────────────────────────────────
# Strategies
# ──────────────────────────────────────────────────────────────────────


class _Absent:
    """Sentinel: this key is not in the record / this kwarg is not passed at all.

    A distinct type rather than `None`, because `None` is itself one of the values under
    test — the template must treat a present-but-`None` key differently from an absent
    one only insofar as both render nothing, and conflating them would hide that.
    """

    def __repr__(self) -> str:
        return "<absent>"


_ABSENT = _Absent()

#: Full unicode minus everything `str.splitlines()` treats as a line boundary: `Cc`
#: covers `\n`, `\r`, `\v`, `\f`, `\x1c`–`\x1e` and `\x85`; `Zl` is U+2028 and `Zp` is
#: U+2029. `Cs` (surrogates) excluded as the repo's other property files do. Verified by
#: category lookup rather than assumed.
_LINE_SAFE_TEXT = st.text(
    alphabet=st.characters(codec="utf-8", exclude_categories=("Cs", "Cc", "Zl", "Zp")),
    min_size=1,
    max_size=24,
)

#: Hint text in the shape the producers write it. `"direct"` is the literal the
#: deterministic fallback emits; the rest are the kind of short phrase the Bedrock
#: contract asks the model for. Sampled alongside arbitrary text so the common case is
#: hit often and the awkward one is still reached.
_REALISTIC_HINTS = st.sampled_from(
    [
        "direct",
        "trim",
        "to_upper",
        "to_date",
        "cast to string",
        "parse with format yyyy-MM-dd",
    ]
)

#: Values that make a branch render. Deliberately includes text carrying the template's
#: own punctuation — `,`, `[`, `]`, `→` — which is what a substring check could not
#: survive.
_TRUTHY_TEXT = st.one_of(
    _REALISTIC_HINTS,
    _LINE_SAFE_TEXT,
    st.sampled_from(["a, b", "[bracketed]", "x → y", 'quote " here', "{{ 1 + 1 }}", " "]),
)

#: Values that leave a branch unrendered while still being present in the record.
_FALSY_TEXT = st.sampled_from(["", None])

#: `confidence` as production writes it (`min(best_score, FALLBACK_CONFIDENCE_CAP)`) plus
#: the forms a model might return, including the non-numeric ones. Kept to short `str()`
#: renderings on purpose: an arbitrary float reprs to 18 characters and would be measuring
#: float formatting. Used only where the record is built here.
_TRUTHY_CONFIDENCE = st.one_of(
    st.integers(min_value=1, max_value=100).map(lambda i: i / 100),
    st.sampled_from([1, 0.5, "0.95", "high"]),
)

#: The numeric-only subset, for `_make_production_mappings`, whose `notes` field formats
#: confidence with `:.2f` (`test_transform_prompt.py:79`) and so raises on a string.
#: Hypothesis found that by falsifying an earlier `@example` here; the fixture is right and
#: the strategy was wrong — production writes `min(best_score, FALLBACK_CONFIDENCE_CAP)`,
#: always a float. Same construction Property 1's file uses, for the same reason.
_TRUTHY_NUMERIC_CONFIDENCE = st.integers(min_value=1, max_value=100).map(lambda i: i / 100)

_FALSY_CONFIDENCE = st.sampled_from([0, 0.0, None, ""])


def _branch_slot(key: str) -> st.SearchStrategy:
    """Absent, present-and-falsy, or present-and-truthy — for one branch key.

    All three arms for every key, so presence and truthiness vary independently per
    record. `_ABSENT` first so shrinking drives towards the smaller record.
    """
    if key == "confidence":
        return st.one_of(st.just(_ABSENT), _FALSY_CONFIDENCE, _TRUTHY_CONFIDENCE)
    return st.one_of(st.just(_ABSENT), _FALSY_TEXT, _TRUTHY_TEXT)


#: Built once from the oracle's key set, so adding a key to the oracle without adding it
#: here is impossible — the dict comprehension is the single source.
_BRANCH_SLOTS = {key: _branch_slot(key) for key in sorted(_ORACLE_BRANCH_KEYS)}


@st.composite
def _mixed_mapping_lists(draw, min_fields: int = 1, max_fields: int = 8) -> list:
    """Mapping lists whose records may each be a different shape.

    Each record starts from **one of the two real fixtures** at the same index — imported,
    not reinvented, so the non-branch part of the record (`source_field`, `target_field`,
    and `notes` on the production side) is whatever those fixtures actually build, and the
    two agree on field names at a given index so the list is always well-formed. The
    branch keys are then stripped and re-drawn independently, which is the axis 2.3
    cannot vary.
    """
    field_count = draw(st.integers(min_value=min_fields, max_value=max_fields))
    fixture_shape = _make_mappings(field_count)
    production_shape = _make_production_mappings(field_count)

    records = []
    for index in range(field_count):
        base = production_shape[index] if draw(st.booleans()) else fixture_shape[index]
        record = {key: value for key, value in base.items() if key not in _ORACLE_BRANCH_KEYS}
        for key, slot in _BRANCH_SLOTS.items():
            value = draw(slot)
            if not isinstance(value, _Absent):
                record[key] = value
        records.append(record)
    return records


# ──────────────────────────────────────────────────────────────────────
# The oracle is tied to the template, and is not degenerate
# ──────────────────────────────────────────────────────────────────────


def test_the_oracle_covers_every_key_the_template_branches_on():
    """R2.1, R2.3: the generated key space is derived from the template, not guessed.

    `_expected_mapping_line` and `_BRANCH_SLOTS` both key off `_ORACLE_BRANCH_KEYS`, so
    this single equality is what stops the property below from going quietly incomplete:
    a branch key the template gains but the oracle does not know about would otherwise be
    neither generated nor predicted, and every example would still pass.

    Equality in both directions, deliberately. A key in the template and not the oracle is
    an unexercised branch (R2.3). A key in the oracle and not the template is a branch
    that no longer exists, which means the property is asserting a rendering rule the
    template has dropped — quieter, and worse.

    **Validates: Requirements 2.1, 2.3**
    """
    missing_from_oracle = sorted(_TEMPLATE_BRANCH_KEYS - _ORACLE_BRANCH_KEYS)
    stale_in_oracle = sorted(_ORACLE_BRANCH_KEYS - _TEMPLATE_BRANCH_KEYS)

    assert not missing_from_oracle, (
        f"transform_generation.j2's mapping line branches on key(s) {missing_from_oracle} that this "
        f"file neither generates nor predicts, so that branch is never exercised by Property 2. "
        f"Extend `_ORACLE_BRANCH_KEYS`, teach `_expected_mapping_line` what the key renders as, and "
        f"give `_branch_slot` its value strategy — in that order. Do NOT widen the oracle key set "
        f"alone: `_expected_mapping_line` would then silently ignore the new branch and every "
        f"example would pass. Template branches on {sorted(_TEMPLATE_BRANCH_KEYS)}."
    )
    assert not stale_in_oracle, (
        f"this file predicts a rendering for key(s) {stale_in_oracle} that transform_generation.j2 "
        f"no longer branches on. Re-read the template's mapping line before deleting anything: if a "
        f"branch was removed deliberately, the fixtures and budget figures that rendered through it "
        f"have moved too (`_make_mappings` reaches only the `elif transform` fallback, so losing "
        f"that branch changes every `< 500` Fixed_Cost figure). Template branches on "
        f"{sorted(_TEMPLATE_BRANCH_KEYS)}."
    )


def test_the_oracle_distinguishes_the_branches_it_predicts():
    """Non-vacuity: a wrong prediction really would fail.

    Task 2.3 verified its own guard by mutation. The equivalent here is cheaper to state
    directly: the oracle must produce *different* lines for the records whose branch
    selection differs. Without this, an oracle that ignored `transformation` (say by a
    typo in the key) would agree with the template on every record that has no hint, and
    the property would look healthy at a much lower example count than it takes to notice.

    Each pair below differs in exactly one branch decision.
    """
    base = {"source_field": "s", "target_field": "t"}
    bare = _expected_mapping_line(base)

    assert _expected_mapping_line({**base, "transformation": "P"}) != bare
    assert _expected_mapping_line({**base, "transform": "F"}) != bare
    assert _expected_mapping_line({**base, "confidence": 0.5}) != bare
    assert _expected_mapping_line({**base, "source_type": "string"}) != bare
    assert _expected_mapping_line({**base, "target_type": "string"}) != bare

    # Present-but-falsy must predict the same line as absent — the branches are
    # truthiness tests, not `in` tests.
    for key, falsy in (("transformation", ""), ("transform", None), ("confidence", 0), ("source_type", "")):
        assert _expected_mapping_line({**base, key: falsy}) == bare, key

    # The `elif` is an elif: the primary wins and the fallback is not appended.
    both = _expected_mapping_line({**base, "transformation": "P", "transform": "F"})
    assert both == _expected_mapping_line({**base, "transformation": "P"})
    assert both != _expected_mapping_line({**base, "transform": "F"})


# ──────────────────────────────────────────────────────────────────────
# Property 2 — every branch the template takes is covered, per record
# ──────────────────────────────────────────────────────────────────────


@settings(max_examples=300, deadline=None)
@given(records=_mixed_mapping_lists())
# The cells that carry the findings, guaranteed rather than sampled.
@example(  # R2.4: both hint keys truthy — the primary wins, differentially
    records=[
        {
            "target_field": "target_field_0",
            "source_field": "source_field_0",
            "transformation": "PRIMARY_BRANCH_RENDERED",
            "transform": "FALLBACK_BRANCH_RENDERED",
            "confidence": 0.95,
            "notes": "Name similarity match (score: 0.95)",
        }
    ]
)
@example(  # finding 1: Production_Shape with a FALSY transformation renders NO hint
    records=[
        {
            "target_field": "target_field_0",
            "source_field": "source_field_0",
            "transformation": "",
            "confidence": 0.95,
            "notes": "Name similarity match (score: 0.95)",
        }
    ]
)
@example(  # mixed shapes in one list: production, fixture, bare, types-only
    records=[
        {
            "source_field": "source_field_0",
            "target_field": "target_field_0",
            "transformation": "direct",
            "confidence": 0.95,
        },
        {"source_field": "source_field_1", "target_field": "target_field_1", "transform": "trim"},
        {"source_field": "source_field_2", "target_field": "target_field_2"},
        {
            "source_field": "source_field_3",
            "source_type": "string",
            "target_field": "target_field_3",
            "target_type": "date",
        },
    ]
)
@example(  # falsy everywhere, present everywhere — nothing may render
    records=[
        {
            "source_field": "source_field_0",
            "target_field": "target_field_0",
            "source_type": "",
            "target_type": None,
            "transformation": None,
            "transform": "",
            "confidence": 0,
        }
    ]
)
def test_each_record_takes_the_branch_its_key_set_predicts(records: list) -> None:
    """Property 2: every branch the template takes is exercised, and takes the right one.

    *For any* list of mapping records, each rendered mapping line is exactly the line
    that record's own key set predicts — the primary `transformation` hint when it is
    truthy, the `elif transform` fallback only when it is not, the confidence annotation
    only when `confidence` is truthy, and the type parentheses only where a type key is.

    Coverage of the branch keys is the subject of
    `test_prompt_fixture_shape_guard.py` and of
    `test_the_oracle_covers_every_key_the_template_branches_on` above. What is asserted
    here is *selection*, per record, in lists whose records disagree — see the module
    docstring for what that adds over 2.3's two homogeneous fixtures.

    **Validates: Requirements 2.1, 2.3, 2.4**
    """
    prompt = _render(records)
    lines = _rendered_mapping_lines(prompt)

    assert len(lines) == len(records), (
        f"expected one rendered mapping line per record: {len(records)} records produced "
        f"{len(lines)} lines. Either the loop stopped rendering one line per mapping, or a "
        f"rendered value carried a line-boundary character the generator was supposed to exclude "
        f"(see `_rendered_mapping_lines`). Lines: {lines!r}"
    )

    for index, (record, line) in enumerate(zip(records, lines, strict=True)):
        expected = _expected_mapping_line(record)
        assert line == expected, (
            f"record {index} rendered a line its key set does not predict.\n"
            f"  record   : {record!r}\n"
            f"  branch keys present: "
            f"{ {key: record[key] for key in sorted(_ORACLE_BRANCH_KEYS) if key in record}!r}\n"
            f"  rendered : {line!r}\n"
            f"  predicted: {expected!r}\n"
            f"This is the template's mapping line disagreeing with `_expected_mapping_line`. Read "
            f"the loop body in transform_generation.j2 before changing either — if the template "
            f"changed on purpose, the oracle follows it; if not, the branch selection is the bug. "
            f"Note the branches test TRUTHINESS (`{{% if m.get(k) %}}`), so a present-but-empty key "
            f"renders nothing, and `transform` is consulted ONLY when `transformation` is falsy."
        )

    # R2.4, differentially — no oracle in the path. Stripping the fallback key from the
    # records whose PRIMARY branch already claimed the hint slot must not change a byte,
    # which is what makes `{% elif %}` an elif rather than a second independent `{% if %}`.
    #
    # Scoped to those records deliberately: hypothesis falsified a first draft that
    # stripped `transform` from every record, because a record carrying ONLY `transform`
    # legitimately loses its hint. The over-broad form would have failed on a correct
    # template — a false gate, which `LGR-9e76249-4` (template / ratified) rates worse
    # than a missed finding.
    if any(record.get("transformation") and record.get("transform") for record in records):
        without_fallback = [
            {**record, "transform": None} if (record.get("transformation") and record.get("transform")) else record
            for record in records
        ]
        assert _rendered_mapping_lines(_render(without_fallback)) == lines, (
            "removing `transform` changed the render of a record that also carries a truthy "
            "`transformation`, so the fallback branch is contributing to a line the PRIMARY branch "
            "already claimed. `{% elif m.get('transform') %}` must stay an `elif`: as an "
            "independent `{% if %}` a record carrying both keys renders two hints, and every "
            "Production_Shape budget figure under-measures any record carrying only one."
        )

    # Finding 1, stated where a reader meets it: a Production_Shape record has no fallback
    # to fall back to, so a falsy `transformation` renders no hint at all.
    for record, line in zip(records, lines, strict=True):
        if set(record) <= set(_PRODUCER_KEYS) and not record.get("transformation"):
            assert line == f"  {record['source_field']} → {record['target_field']}" + (
                f" [confidence: {record['confidence']}]" if record.get("confidence") else ""
            ), (
                "a record in the producer's own key set, with a falsy `transformation`, rendered a "
                f"hint from somewhere: {line!r}. Production writes no `transform` key "
                f"(the producer at {_PRODUCER_PATH}:{_PRODUCER_LINE} writes "
                f"{sorted(_PRODUCER_KEYS)}), so the `elif` is dead on the live path and there is "
                "nothing for this line to fall back to."
            )


@settings(max_examples=150, deadline=None)
@given(
    field_count=st.integers(min_value=1, max_value=12),
    hint=_TRUTHY_TEXT,
    confidence=_TRUTHY_NUMERIC_CONFIDENCE,
)
@example(field_count=10, hint="direct", confidence=0.95)  # the shape the budget assertions render
@example(field_count=1, hint="a, b [c] → d", confidence=0.95)  # punctuation a substring check could not survive
@example(field_count=12, hint="parse with format yyyy-MM-dd", confidence=0.01)  # uniformity across a longer list
def test_a_production_render_takes_every_branch_production_writes(
    field_count: int, hint: str, confidence: float
) -> None:
    """R2.1, R2.4: the Production_Shape-specific half, under generation.

    The expected key set is **computed** — `template branch keys ∩ producer keys`, read
    through 2.3's discovery helpers — so neither side is transcribed (R2.2's mechanism,
    `LGR-28b4d75-5`). On this tree it resolves to `{confidence, transformation}`.

    What this adds over `test_prompt_fixture_shape_guard.py`'s example form, which pins
    the same claim at one field count with `hint="direct"`:

    * the guarantee is **uniform across the list** — every line, not the first. A
      one-record render cannot see a template that hints only the first mapping.
    * it holds for any hint and confidence **production could write**, including hint text
      carrying the template's own punctuation.
    * the fallback's unreachability is asserted **differentially**: adding a `transform`
      value to every Production_Shape record must not change a byte. That is stronger than
      "the fallback text is absent from the prompt", which a coincidental substring could
      satisfy.

    **Validates: Requirements 2.1, 2.4**
    """
    records = _make_production_mappings(field_count, hint=hint, confidence=confidence)

    # The fixture really does carry every branch key production writes — otherwise the
    # render below would exercise fewer branches than the claim needs, and pass.
    for record in records:
        for key in sorted(_PRODUCTION_BRANCH_KEYS):
            assert record.get(key), (
                f"`_make_production_mappings` produced a record whose `{key}` is absent or falsy: "
                f"{record!r}. `{key}` is branched on by transform_generation.j2 AND written by the "
                f"producer at {_PRODUCER_PATH}:{_PRODUCER_LINE}, so a Production_Shape render must "
                f"exercise it. Fix the FIXTURE, never this assertion: the producer is the oracle."
            )

    lines = _rendered_mapping_lines(_render(records))
    assert len(lines) == field_count

    for index, line in enumerate(lines):
        expected = f"  source_field_{index} → target_field_{index}, {hint} [confidence: {confidence}]"
        assert line == expected, (
            f"line {index} of {field_count} is not the production mapping line.\n"
            f"  rendered : {line!r}\n"
            f"  predicted: {expected!r}\n"
            f"Note there are NO type parentheses, deliberately: production writes neither "
            f"`source_type` nor `target_type` (the producer writes {sorted(_PRODUCER_KEYS)}), so "
            f"the honest render carries no type annotation at all. See "
            f"`_make_production_mappings`' docstring."
        )

    # Differential R2.4: the `elif` is unreachable from this shape, at every field count.
    with_fallback = [{**record, "transform": "FALLBACK_BRANCH_RENDERED"} for record in records]
    assert _rendered_mapping_lines(_render(with_fallback)) == lines, (
        "adding a `transform` value to every Production_Shape record changed the render, so the "
        "production path can reach the `elif transform` fallback. It must not: `transformation` is "
        "truthy on every record here, so the primary branch claims the hint slot."
    )


# ──────────────────────────────────────────────────────────────────────
# Property 3 — the target-schema slot stays inert when absent
# ──────────────────────────────────────────────────────────────────────

#: Every `target_schema` value that renders NO target-schema block. `{}` and omitted are
#: the two the example test covers; the other three are not covered anywhere today.
#: `{"type": "object"}` is the shape the template's own comment describes — "a schema that
#: normalisation did not recognise carries no `properties` and renders no block".
_NO_SCHEMA_SHAPES = st.sampled_from(
    [
        ("omitted", _ABSENT),
        ("None", None),
        ("empty dict", {}),
        ("no properties key", {"type": "object"}),
        ("empty properties", {"type": "object", "properties": {}}),
    ]
)

#: Names worth putting in `unsourced_target_fields`: the real target names the fixtures
#: build (so a MATCHING name is reached, not only a stranger), plus arbitrary text.
_TARGET_NAMES = st.one_of(
    st.sampled_from([f"target_field_{i}" for i in range(8)] + ["phone", "source_field_0"]),
    _LINE_SAFE_TEXT,
)

#: `| default([], true)` absorbs a falsy value, so `None` and `[]` never reach the block.
#: A TRUTHY non-list does — a bare string, or a dict — and its inertness is a fact about
#: the block never rendering rather than about the filter. Both are generated for that
#: reason.
_UNSOURCED_SHAPES = st.one_of(
    st.just(_ABSENT),
    st.none(),
    st.just([]),
    st.lists(_TARGET_NAMES, max_size=6),
    st.lists(
        st.one_of(st.integers(), st.booleans(), st.none(), st.just(("target_field_0",)), st.just({"a": 1})),
        max_size=4,
    ),
    _LINE_SAFE_TEXT,
    st.dictionaries(st.sampled_from(["target_field_0", "phone"]), st.integers(), max_size=2),
)

_MAPPING_LISTS = st.one_of(
    st.integers(min_value=0, max_value=6).map(_make_mappings),
    st.integers(min_value=0, max_value=6).map(_make_production_mappings),
    _mixed_mapping_lists(min_fields=0, max_fields=6),
)


@settings(max_examples=300, deadline=None)
@given(
    schema_shape=_NO_SCHEMA_SHAPES,
    unsourced=_UNSOURCED_SHAPES,
    records=_MAPPING_LISTS,
)
# The three shapes the example test covers, plus the two it does not, plus the
# non-list arms — guaranteed rather than sampled.
@example(schema_shape=("omitted", _ABSENT), unsourced=_ABSENT, records=_make_production_mappings(10))
@example(schema_shape=("omitted", _ABSENT), unsourced=None, records=_make_production_mappings(10))
@example(schema_shape=("omitted", _ABSENT), unsourced=[], records=_make_production_mappings(10))
@example(
    schema_shape=("omitted", _ABSENT),
    unsourced=["target_field_0", "phone"],
    records=_make_production_mappings(10),
)
@example(
    schema_shape=("no properties key", {"type": "object"}),
    unsourced=[1, True, None, {"a": 1}],
    records=_make_mappings(10),
)
@example(schema_shape=("empty properties", {"type": "object", "properties": {}}), unsourced="phone", records=[])
def test_the_no_schema_render_is_byte_identical_whatever_unsourced_carries(
    schema_shape: tuple, unsourced: object, records: list
) -> None:
    """Property 3: the target-schema slot stays inert when absent.

    *For any* value of `unsourced_target_fields` — omitted, `None`, `[]`, a list of names
    (matching or not), a list carrying non-strings, a bare string, a dict — and *for any*
    mapping list, a render with no `target_schema` is byte-identical to a render with
    neither variable supplied.

    Generated rather than relying on the three-shape comparison in
    `test_an_omitted_unsourced_target_fields_renders_byte_identically`. See the module
    docstring for the shape list, and for the precise limit of what byte-identity proves:
    it pins the slot **inert**, and the `< 500` Fixed_Cost assertions are what would catch
    a whitespace-control tidy-up that added the same newline to both sides (p52 do-NOT 5).

    **Validates: Requirements 4.4**
    """
    schema_label, schema_value = schema_shape

    baseline = _render(records)

    kwargs: dict = {"mappings": records}
    if not isinstance(schema_value, _Absent):
        kwargs["target_schema"] = schema_value
    if not isinstance(unsourced, _Absent):
        kwargs["unsourced_target_fields"] = unsourced

    rendered = load_prompt_template(_TEMPLATE, **kwargs)

    assert rendered == baseline, (
        "a no-target-schema render changed when `unsourced_target_fields` / `target_schema` were "
        "supplied, so the target-schema slot is no longer inert (R4.4).\n"
        f"  target_schema        : {schema_label} = {schema_value!r}\n"
        f"  unsourced_target_fields: {unsourced!r}\n"
        f"  mapping records      : {len(records)}\n"
        f"  baseline : {len(baseline)} chars\n"
        f"  rendered : {len(rendered)} chars (delta {len(rendered) - len(baseline):+d})\n"
        "The annotation is driven by the SCHEMA's declared field list, not by the names list, so "
        "with no declared fields there is nothing for a name to match and the block must emit "
        "zero bytes. A caller that supplies neither variable keeps its exact prompt, which is what "
        "lets `| default({}, true)` / `| default([], true)` absorb an omitted variable and an "
        "explicit None under StrictUndefined without changing the render."
    )


def test_none_of_the_no_schema_shapes_renders_a_target_schema_block():
    """R4.4's other half: each of the five shapes really does take the zero-byte branch.

    Byte-identity above proves the shapes agree with each other. It cannot prove they
    agree on rendering *nothing* — five shapes that all emitted the same block would pass
    it. This is the check that says the block is absent, and it is example-based on
    purpose: it reads fixed fixture text, so asserting the absence of the block's own
    strings cannot be confounded by a generated hint that happens to contain them.

    Two of the five shapes are covered nowhere else: `target_schema=None` and a schema
    carrying no `properties` key at all.

    **Validates: Requirements 4.4**
    """
    for label, schema_value in (
        ("omitted", _ABSENT),
        ("None", None),
        ("empty dict", {}),
        ("no properties key", {"type": "object"}),
        ("empty properties", {"type": "object", "properties": {}}),
    ):
        kwargs: dict = {"mappings": _make_production_mappings(4), "unsourced_target_fields": ["target_field_0"]}
        if not isinstance(schema_value, _Absent):
            kwargs["target_schema"] = schema_value
        rendered = load_prompt_template(_TEMPLATE, **kwargs)

        assert "TARGET SCHEMA declares" not in rendered, (
            f"the `{label}` target_schema shape rendered a target-schema block. It declares no "
            f"fields, so the block must emit zero bytes."
        )
        assert "NO SOURCE" not in rendered, (
            f"the `{label}` target_schema shape rendered the unsourced annotation for "
            f"`target_field_0`. The annotation is driven by the schema's declared field list; with "
            f"no declared fields there is nothing to annotate."
        )
