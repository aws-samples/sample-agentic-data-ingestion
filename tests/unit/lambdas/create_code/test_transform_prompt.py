# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: prompt builder produces < 500 tokens for 10-field mapping."""

import os
import sys

# Add shared to path so we can import the template loader
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

from prompt_template_loader import load_prompt_template


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English/code text."""
    return len(text) // 4


def _make_mappings(n: int) -> list:
    """Generate n sample field mappings."""
    types = ["string", "integer", "double", "boolean", "date", "timestamp"]
    transforms = [None, "trim", "to_upper", None, "to_date", None]
    mappings = []
    for i in range(n):
        t = types[i % len(types)]
        mappings.append(
            {
                "source_field": f"source_field_{i}",
                "source_type": t,
                "target_field": f"target_field_{i}",
                "target_type": t,
                "transform": transforms[i % len(transforms)],
            }
        )
    return mappings


def _make_production_mappings(n: int, hint: str = "direct", confidence: float = 0.95) -> list:
    """Generate n mapping records in the shape PRODUCTION writes (p52 R1.1).

    `_make_mappings` above is a *fixture* shape: it carries `transform`, a key
    no producer in this repository ever writes, and omits `confidence`, which
    every producer always writes. So every budget assertion built on it renders
    a prompt no pipeline sends. This helper is the honest one.

    EXACTLY FIVE KEYS, derived from the producer rather than invented (p52 gate
    1.1, R2.2). The deterministic fallback at
    `lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1220-1227`
    is the one place in the repository a mapping record is written literally,
    and it writes `target_field`, `source_field`, `transformation`,
    `confidence`, `notes`. The Bedrock response contract at `:1089-1093`
    declares the same five, so the model path and the deterministic path do not
    disagree about the key set. All five survive to the template unchanged:
    `_normalise_mapping_structure` only moves whole entries between lists,
    `_extract_field_mappings` (`create_code/mapping_resolution.py:48`) is a pure
    locator, and `code_assembly.generate_code` passes the list through.

    WHY `source_type` AND `target_type` ARE ABSENT, and why adding them would be
    a defect rather than a fix. The template reads them
    (`transform_generation.j2:4`, `m.get('source_type')` / `m.get('target_type')`)
    but production never writes them, so the honest production render carries
    **no type parentheses at all** — the line is
    `source_field_0 → target_field_0, direct [confidence: 0.95]`. Adding the two
    keys here to make a render look richer would put this fixture back in the
    same position `_make_mappings` is in: measuring a shape production does not
    send. That is the whole defect p52 exists to remove. It is also not
    hypothetical — see the divergence note on the ceilings below, where those two
    keys account for exactly 48 tokens of a figure recorded during design.

    `notes` is carried because production writes it, even though the template
    never reads it; the assertions below are about what the template renders, and
    an unread key costs zero tokens. Keeping it makes the fixture the producer's
    shape rather than the template's, which is what R2.2 asks for.
    """
    return [
        {
            "target_field": f"target_field_{i}",
            "source_field": f"source_field_{i}",
            "transformation": hint,
            "confidence": confidence,
            "notes": f"Name similarity match (score: {confidence:.2f})",
        }
        for i in range(n)
    ]


def test_prompt_under_500_tokens_for_10_fields():
    """AC-18.3: transform prompt must be under 500 tokens for a 10-field mapping.

    SUBJECT: the template's FIXED COST — its instructions, structure and rules —
    and NOT the production render (p52 R1.5). `_make_mappings` builds
    Fixture_Shape, so this renders the `elif m.get('transform')` fallback branch,
    a hint on only five of ten fields, and no `confidence` annotation at all;
    the production render takes the primary branch, hints every field, and
    annotates every one. What this assertion and production genuinely share is
    the template's own text, which is what makes 500 a useful *template*-growth
    detector: the 498 → 504 regression the template's comment records was caught
    here, by a fixed cost. The ceiling is deliberately NOT raised to admit the
    production shape (p52 do-NOT 2, do-NOT 3) — raising it would trade that
    detector for a number that moves with every hint the model writes. The
    production figure is asserted separately, in
    `test_production_shape_prompt_budget` below.
    """
    mappings = _make_mappings(10)
    prompt = load_prompt_template("transform_generation.j2", mappings=mappings)
    tokens = _estimate_tokens(prompt)
    assert tokens < 500, f"Prompt is ~{tokens} tokens (limit: 500). Length: {len(prompt)} chars"


def test_prompt_under_500_tokens_for_5_fields():
    """Smaller mapping should also be well under 500 tokens.

    SUBJECT: the template's FIXED COST at a smaller field count, not the
    production render (p52 R1.5) — `_make_mappings` is Fixture_Shape, as at
    `test_prompt_under_500_tokens_for_10_fields` (`:41` before p52). Retained at
    500 unchanged; see that assertion for why the ceiling is not raised, and
    `test_production_shape_prompt_budget` for the figure that does measure what
    a pipeline sends.
    """
    mappings = _make_mappings(5)
    prompt = load_prompt_template("transform_generation.j2", mappings=mappings)
    tokens = _estimate_tokens(prompt)
    assert tokens < 500, f"Prompt is ~{tokens} tokens for 5 fields"


def test_prompt_contains_mapping_instructions():
    """Prompt must contain key instructions for the AI."""
    mappings = _make_mappings(3)
    prompt = load_prompt_template("transform_generation.j2", mappings=mappings)
    assert "df_transformed" in prompt
    assert "Do NOT create classes" in prompt
    assert "source_field_0" in prompt


def test_prompt_includes_transform_hints():
    """When a mapping has a transform, it should appear in the prompt."""
    mappings = [
        {
            "source_field": "name",
            "source_type": "string",
            "target_field": "name_clean",
            "target_type": "string",
            "transform": "trim",
        },
    ]
    prompt = load_prompt_template("transform_generation.j2", mappings=mappings)
    assert "trim" in prompt


def test_prompt_empty_mappings():
    """Empty mappings should still render a valid prompt."""
    prompt = load_prompt_template("transform_generation.j2", mappings=[])
    assert "df_transformed" in prompt


# ---------------------------------------------------------------------------
# p32 T11a — the STRUCTURE block must not contradict itself (2.12), and the
# template must render under StrictUndefined both with and without the
# regeneration-feedback context (V7, LGR-d3c81fc-1).
# ---------------------------------------------------------------------------

# A regeneration render context in the shape `_build_feedback_section`
# (create_code/transform_generation.py:32) produces on a second attempt.
REGENERATION_FEEDBACK = (
    "PREVIOUS ATTEMPT WAS REJECTED BY CODE REVIEW. You MUST fix these issues:\n"
    "- [high] (correctness) to_date called without an explicit format\n"
    "Reviewer summary: fix the date parsing"
)


def test_prompt_does_not_ask_the_model_to_chain_withcolumn():
    """2.12: the prompt must not demand one-line statements AND chained .withColumn.

    Chaining two `.withColumn` calls in a single statement is exactly the shape
    the converter rejects (`bugfix.md` 1.7 — the live failure). One
    `withColumn` per physical line is a shape the converter already converts
    (3.2), so the prompt asks for that instead.
    """
    prompt = load_prompt_template("transform_generation.j2", mappings=_make_mappings(3))

    # The instruction that produced the live failure is gone.
    assert "Chain single-line .withColumn steps" not in prompt
    # ... and is replaced by the one-per-statement shape the converter accepts.
    assert "ONE .withColumn per statement, one statement per line" in prompt
    # Each step must read the previous frame, or a later line drops an earlier
    # line's column (3.2 binds inner refs to each statement's own source frame).
    assert "first reads df, each next reads df_transformed" in prompt
    # `.select()` stays allowed, but only standalone — chaining it onto a
    # `.withColumn` statement is the silent-wrong-answer shape (1.10).
    assert ".select() only as a final standalone line" in prompt


def test_prompt_still_forbids_passthrough_output():
    """T11a constraint: de-conflicting the STRUCTURE block must not make the
    passthrough `df_transformed = df` output acceptable again (LGR-d3c81fc-1
    records that passthrough was itself a defect). The no-no-ops rule stays.
    """
    prompt = load_prompt_template("transform_generation.j2", mappings=_make_mappings(3))
    assert "No no-ops (self-rename/alias, unchanged re-select)" in prompt


def test_prompt_renders_with_regeneration_context():
    """V7: renders under StrictUndefined when feedback_section IS supplied."""
    prompt = load_prompt_template(
        "transform_generation.j2",
        mappings=_make_mappings(3),
        feedback_section=REGENERATION_FEEDBACK,
    )
    assert "PREVIOUS ATTEMPT WAS REJECTED BY CODE REVIEW" in prompt
    assert "to_date called without an explicit format" in prompt
    # The de-conflicted STRUCTURE rules survive the regeneration render too.
    assert "ONE .withColumn per statement" in prompt


def test_prompt_renders_without_regeneration_context():
    """V7: renders under StrictUndefined when feedback_section is ABSENT.

    `transform_generation.j2` renders with `undefined=StrictUndefined`
    (`shared/prompt_template_loader.py:58`), so a referenced-but-unsupplied
    variable raises at render time — mid-pipeline, not at synth
    (`LGR-d3c81fc-1`). The feedback slot is read through `| default('')` so an
    omitted context is safe; this test is what keeps it that way.
    """
    prompt = load_prompt_template("transform_generation.j2", mappings=_make_mappings(3))
    assert "PREVIOUS ATTEMPT WAS REJECTED" not in prompt
    assert "df_transformed" in prompt


def test_prompt_renders_with_empty_regeneration_context():
    """V7: the first-attempt production shape passes feedback_section="" —
    it must render and emit no feedback block
    (`create_code/transform_generation.py:102`).
    """
    prompt = load_prompt_template(
        "transform_generation.j2",
        mappings=_make_mappings(3),
        feedback_section="",
    )
    assert "PREVIOUS ATTEMPT WAS REJECTED" not in prompt
    assert "ONE .withColumn per statement" in prompt


# ---------------------------------------------------------------------------
# p37 task 11.7 / R3.5 — a SECOND budget assertion, for the shape the first one
# never reached, plus the two render guarantees C12 rests on.
#
# WHY A SECOND ASSERTION EXISTS AT ALL. Every render above passes `mappings`
# and nothing else, so every one of them takes the ZERO-BYTE branch of the
# target-schema block. The 500-token assertion has therefore never exercised
# that block, and the live prompt — the one a pipeline with a target schema
# actually sends — has been 597 tokens since p33 D10 shipped (`LGR-b2f79ef-10`,
# template / ratified). The budget test was guarding the pre-p33 shape. The two
# tokens of headroom it has are real, and they are headroom on a case p37 does
# not touch. Nothing was relaxed here: the 500-token assertions above are
# unchanged and still pass at a measured 498.
#
# MEASURED, NOT ESTIMATED (p37 do-NOT 10). Rendered with the project venv
# (`agentic_data_ingestion/.venv/bin/python`) against this module's own
# `_estimate_tokens` (`len(text) // 4`).
#
# CORRECTED BY p52 TASK 2.5 (R3.1, R3.2, R3.3). Every figure below was
# re-measured on this tree rather than transcribed, and each row now names the
# SHAPE that rendered it — because "the prompt is N tokens" means nothing without
# the fixture behind it:
#
#   * Fixture_Shape — `_make_mappings`: `source_field`, `source_type`,
#     `target_field`, `target_type`, `transform`. No producer in this repository
#     writes `transform`, and every producer writes `confidence`, so this is a
#     shape production never sends.
#   * Production_Shape — `_make_production_mappings`: the five keys the producer
#     does write (p52 gate 1.1). This is the shape a pipeline actually sends.
#
#   |   | shape            | variant                                | tokens | chars |
#   |---|------------------|----------------------------------------|--------|-------|
#   | 1 | Fixture_Shape    | 10 mappings, no `target_schema`         |    495 |  1983 |
#   | 2 | Fixture_Shape    | + 11-field `target_schema` (live shape) |    595 |  2380 |
#   | 3 | Fixture_Shape    | same, nothing unsourced                 |    595 |  +0   |
#   | 4 | Fixture_Shape    | same, `phone` unsourced                 |    601 |  +26  |
#   | 5 | Production_Shape | 10 mappings, no `target_schema`         |    505 |  2022 |
#   | 6 | Production_Shape | + 11-field `LIVE_TARGET_SCHEMA`         |    604 |  2419 |
#
# WHAT MOVED ROWS 1-4 (R3.3). p49 task 4.1 corrected the template's "an explicit
# format from the hint or samples" instruction — the transform prompt carries no
# samples, so a third of that instruction was unfollowable — and the corrected
# wording is **11 characters shorter**. Rows 1-4 read 498/1994, 597/2391, 597/+0
# and 604/+26 before this correction, and every one of them moved by exactly that
# edit: rows 1 and 2 lose 11 absolute chars, while rows 3 and 4 state a DELTA
# against row 2 and so keep +0 and +26 unchanged. All four token figures fall by
# 2 or 3 as `len(text) // 4` crosses a boundary. The p49 edit rode inline rather
# than adding a line (that spec's own do-NOT 2), which is why nothing else in the
# render moved. `LGR-1e135d2-9` (**template /
# ratified**) recorded this table as stale in all four rows and named it "one
# additional artefact for that spec"; this is that artefact.
#
# The two figures in the PROSE above — 498 and 597 — are left as p37 wrote them.
# They are that spec's narrative of the tree it shipped against, not a claim about
# this one; the table is the current-state artefact and the one R3.1 governs.
#
# A CORRECTION, NOT A FIX — and it must not be read as evidence of one. Every
# assertion these rows describe passed before p52 task 2.5 and passes after: row 1
# under `test_prompt_under_500_tokens_for_10_fields`'s `< 500` (495, four tokens
# spare — p49 task 4.1 *improved* that headroom from one token), rows 2 and 4 under
# both `< 650` ceilings in
# `test_prompt_under_650_tokens_for_the_target_schema_shape` (595 and 601), and
# rows 5 and 6 under the p52 ceilings in `test_production_shape_prompt_budget` and
# `test_production_shape_prompt_budget_with_target_schema` (600 and 725). No
# ceiling, fixture or test body changed here; four stale COMMENTS did. The suite
# was never red.
#
# ROWS 5 AND 6 ARE NOT THE FIGURES p52's design.md PREDICTED, and a reader
# comparing the two needs to know why. design.md records 553/2212 and 652/2609 for
# these two rows; re-measured here they are 505/2022 and 604/2419 — 48 tokens and
# 190 chars lower in both. design.md measured them with a fixture carrying
# `source_type` and `target_type`, two keys production never writes (gate 1.1,
# verified against the producer). Adding those keys back reproduces 553/2212 and
# 652/2609 exactly, which is how the cause was established rather than guessed:
# two ` (<type>)` groups per mapping line over ten lines is 190 characters, and
# 190 // 4 = 47.5. The p52 task 2.1 block below carries the same finding at the
# assertions it governs — this is that one measurement, not a second one.
#
# TWO CONSEQUENCES OF THAT, because design.md states the opposite of the first:
#   1. design.md's "652 means the 650 ceiling fails too" is **false** on this
#      tree. Row 6 — Production_Shape with `LIVE_TARGET_SCHEMA` — is 604 and
#      PASSES both `< 650` assertions below with 46 tokens spare. There is no
#      Production_Shape failure there to fix, which is why those two keep their
#      ceiling and their fixture (p52 C1).
#   2. The `< 500` defect stands and is **smaller** than design.md says. Row 5 is
#      the cheapest production render there is — every `transformation` the single
#      word `direct`, which is exactly what the deterministic fallback writes —
#      and at 505 it exceeds the 500 ceiling those Fixture_Shape assertions use by
#      **5** tokens, not 54. The conclusion is unchanged: the budget fails on the
#      cheapest production render. The magnitude is not.
#
# WHY 650 AND NOT SOMETHING TIGHTER. Both axes were measured, so the number is
# a choice with known trip points rather than a round figure:
#
#   * annotations (what C12 adds, +26 chars each): 8 of the 11 fields annotated
#     still fits at 649; the 9th trips it at 656.
#   * a wider target schema (+25 chars per declared field): 18 declared fields
#     fits at 641; 21 trips it at 660.
#
# A tighter ceiling of 620 would trip at FOUR annotated fields (623), which is a
# perfectly ordinary mapping, so it would cry wolf. 650 leaves 46 tokens over
# the asserted fixture while still tripping on a genuinely wide target schema —
# which is exactly the condition `LGR-b2f79ef-10` named for revisiting what the
# prompt renders ("the prompt gains a size budget that a wide target schema
# would exceed"). Before this assertion existed that condition could not be
# observed, because no test passed `target_schema` at all.
# ---------------------------------------------------------------------------

# The live shape p37 measured: the ten mapped target names plus `phone`, every
# field declared `string`. Declaring each field its mapping's own type instead
# renders 599 / 2396 — a fixture difference, not a difference in what the
# annotation costs.
LIVE_TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        **{f"target_field_{i}": {"type": "string"} for i in range(10)},
        "phone": {"type": "string"},
    },
}

# The annotation as the template renders it, inline on the existing per-field
# line. Exactly 26 characters, and it must stay a suffix rather than becoming a
# line of its own: ~22 tag-only lines are what took a 10-field mapping from 498
# to 504 once already (the template's own comment records it).
UNSOURCED_ANNOTATION = " — NO SOURCE: null/default"


def _target_block_lines(prompt: str) -> list[str]:
    """The per-field lines of the TARGET SCHEMA block, in render order."""
    if "TARGET SCHEMA declares" not in prompt:
        return []
    block = prompt.split("TARGET SCHEMA declares", 1)[1].split("Generate ONLY", 1)[0]
    return [line for line in block.splitlines() if line.startswith("  ")]


def test_prompt_under_650_tokens_for_the_target_schema_shape():
    """R3.5: the live `target_schema` shape has a measured budget of its own.

    Two renders, because the annotation is the only thing p37 adds to this
    shape and its cost should be inside the same ceiling as the shape without
    it. Measured at the time the ceiling was set: 597 unannotated, 604 with
    `phone` annotated.

    SUBJECT (p52 R2.5): the TARGET-SCHEMA BLOCK's cost, which Fixture_Shape
    measures correctly — the block's size is driven by the schema's declared
    field list, not by the mapping lines. So both ceilings stay at 650 and the
    fixture stays `_make_mappings`, deliberately (p52 C1). The Production_Shape
    figure for this same shape is **604** tokens and is asserted separately in
    `test_production_shape_prompt_budget_with_target_schema` below; note that it
    fits under 650 with 46 tokens to spare, so nothing here is an unfixed
    failure. p52's design predicted 652 for that render and thereby predicted
    this ceiling failing too — it does not, and the reason is recorded at the new
    assertion: the 652 was measured with a fixture carrying `source_type` and
    `target_type`, which production never writes.

    **Validates: Requirements 3.5**
    """
    mappings = _make_mappings(10)

    plain = load_prompt_template("transform_generation.j2", mappings=mappings, target_schema=LIVE_TARGET_SCHEMA)
    annotated = load_prompt_template(
        "transform_generation.j2",
        mappings=mappings,
        target_schema=LIVE_TARGET_SCHEMA,
        unsourced_target_fields=["phone"],
    )

    plain_tokens = _estimate_tokens(plain)
    annotated_tokens = _estimate_tokens(annotated)

    assert plain_tokens < 650, (
        f"the target-schema prompt is ~{plain_tokens} tokens (ceiling: 650; measured 597 when "
        f"this ceiling was set). Length: {len(plain)} chars"
    )
    assert annotated_tokens < 650, (
        f"the annotated target-schema prompt is ~{annotated_tokens} tokens (ceiling: 650; "
        f"measured 604 when this ceiling was set). Length: {len(annotated)} chars"
    )
    # The block really did render — otherwise both figures would be the 498 case
    # and this test would be a second copy of the one above.
    assert "TARGET SCHEMA declares these 11 field(s)" in plain


def test_the_unsourced_annotation_renders_only_for_named_fields():
    """R3.5: only a named unsourced field is annotated, and inline.

    Three things a reviewer would otherwise have to take on trust: a field that
    is not in the list carries no annotation; a name the target schema does not
    declare renders nothing at all rather than an orphan line; and the
    annotation rides on the existing per-field line, so the render gains no
    lines. That last one is the 498 → 504 fence, asserted rather than assumed.

    **Validates: Requirements 3.5**
    """
    mappings = _make_mappings(10)
    plain = load_prompt_template("transform_generation.j2", mappings=mappings, target_schema=LIVE_TARGET_SCHEMA)
    annotated = load_prompt_template(
        "transform_generation.j2",
        mappings=mappings,
        target_schema=LIVE_TARGET_SCHEMA,
        unsourced_target_fields=["phone"],
    )

    lines = _target_block_lines(annotated)
    named = [line for line in lines if UNSOURCED_ANNOTATION in line]
    assert named == ["  phone (string)" + UNSOURCED_ANNOTATION], (
        f"expected exactly the `phone` line annotated, got {named!r}"
    )
    # Every other declared field is untouched — the annotation is not a blanket
    # warning on the block.
    assert annotated.count(UNSOURCED_ANNOTATION) == 1
    assert len(_target_block_lines(plain)) == len(lines) == 11
    assert len(annotated.splitlines()) == len(plain.splitlines()), (
        "the annotation added a line; it must stay inline on the per-field line"
    )

    # A name the schema does not declare renders nothing: the annotation is
    # driven by the schema's own field list, not by the list of names.
    unknown = load_prompt_template(
        "transform_generation.j2",
        mappings=mappings,
        target_schema=LIVE_TARGET_SCHEMA,
        unsourced_target_fields=["a_field_the_schema_never_declared"],
    )
    assert unknown == plain
    assert UNSOURCED_ANNOTATION not in unknown


def test_an_omitted_unsourced_target_fields_renders_byte_identically():
    """R3.5: omitting the new variable changes no byte of the prompt.

    `| default([], true)` has to absorb three live shapes — the variable
    omitted, an explicit `None` (`generate_transform_with_ai`'s parameter
    default), and `[]` (a mapping with nothing unsourced) — under a loader that
    renders with `StrictUndefined`, where a referenced-but-unsupplied variable
    raises mid-pipeline rather than at synth (`LGR-d3c81fc-1`).

    WHAT THIS FORM GUARANTEES AND WHAT IT DOES NOT. It pins the three shapes to
    each other and pins the annotation out of all of them, in both the
    with-`target_schema` and no-`target_schema` contexts. It does **not** pin
    them to any particular past revision — a later edit that changed all three
    together would still pass here, and the 500-token assertion above is what
    would catch that for the no-schema case. Byte-identity against the pre-C12
    template was verified once, at task 11.6, by rendering both revisions side
    by side; that check cannot live in the suite because it would rot the first
    time the template legitimately changes.

    **Validates: Requirements 3.5**
    """
    mappings = _make_mappings(10)

    for target_schema in ({}, LIVE_TARGET_SCHEMA):
        kwargs = {"mappings": mappings, "target_schema": target_schema}
        omitted = load_prompt_template("transform_generation.j2", **kwargs)
        none = load_prompt_template("transform_generation.j2", **kwargs, unsourced_target_fields=None)
        empty = load_prompt_template("transform_generation.j2", **kwargs, unsourced_target_fields=[])

        assert omitted == none == empty, (
            "omitted, None and [] must render identically — a caller that does not "
            "supply unsourced_target_fields keeps its exact pre-C12 prompt"
        )
        for rendered in (omitted, none, empty):
            assert UNSOURCED_ANNOTATION not in rendered
            assert "NO SOURCE" not in rendered

    # The variable is also inert with no target schema to annotate, which is why
    # the 500-token case above stays at 498 whatever a caller passes.
    #
    # SUBJECT (p52 R1.5): as in `test_prompt_under_500_tokens_for_10_fields`, the
    # ceiling below is on the template's FIXED COST and not on the production
    # render — `mappings` here is
    # `_make_mappings(10)`, i.e. Fixture_Shape. Its job in this test is narrower
    # still: it pins that an inert `unsourced_target_fields` costs nothing, so
    # 500 is a floor-under-a-no-op rather than a production budget. Retained
    # unchanged at 500 (p52 do-NOT 2, do-NOT 3).
    with_names = load_prompt_template("transform_generation.j2", mappings=mappings, unsourced_target_fields=["phone"])
    assert with_names == load_prompt_template("transform_generation.j2", mappings=mappings)
    assert _estimate_tokens(with_names) < 500


# ---------------------------------------------------------------------------
# p52 task 2.1 / R1.1, R1.2, R1.3, R1.6 — the budget that measures the shape
# production actually sends.
#
# WHY THESE EXIST. Every budget assertion above renders `_make_mappings`, which
# builds a key set no producer writes (`transform`) and omits one every producer
# writes (`confidence`). So until now the transform prompt's token budget had
# never once measured the prompt a pipeline sends: CI was green, and a green
# suite guaranteed nothing about production. Nothing above was failing and
# nothing above is a bug — the assertions are correct about their own subject,
# the template's fixed cost. What was missing is this pair.
#
# MEASURED, NOT ESTIMATED. Rendered with the project venv
# (`agentic_data_ingestion/.venv/bin/python`) against this module's own
# `_estimate_tokens` (`len(text) // 4`), using `_make_production_mappings(10)`:
#
#   | Production_Shape variant                     | tokens | chars |
#   |----------------------------------------------|--------|-------|
#   | 10 fields, all `"direct"`, no target schema  |    505 |  2022 |
#   | same + 11-field `LIVE_TARGET_SCHEMA`         |    604 |  2419 |
#
# **505 is the honest production floor.** It carries no long hint anywhere —
# every `transformation` is the single word `direct`, which is exactly what the
# deterministic fallback writes — and it still exceeds the `< 500` ceiling above.
# The budget does not merely fail on a pathological hint; it fails on the
# cheapest production render there is. That is the defect, and it survives the
# correction below.
#
# ⚠ DIVERGENCE FROM p52 design.md, STATED RATHER THAN PAPERED OVER (R1.2).
# design.md records 553 / 2212 and 652 / 2609 for these two rows. The figures
# here are 505 / 2022 and 604 / 2419 — **48 tokens lower in both cases**. The
# cause is not a template change and not a measurement error on either side:
# design.md's figures were taken with a fixture that carried `source_type` and
# `target_type`. Adding those two keys back reproduces 553 / 2212 and 652 / 2609
# **exactly**, which is how the cause was established rather than guessed. The
# arithmetic closes completely: two ` (<type>)` groups per mapping line over ten
# lines is 190 characters, and 190 // 4 = 47.5, i.e. the 48 tokens and the
# 190-char gap in both rows.
#
# Production writes neither key (p52 gate 1.1, verified against
# `schema_mapping/lambda_function.py:1220-1227` and the Bedrock contract at
# `:1089-1093`), so the honest render carries no type parentheses at all. The
# figures above are therefore the correct ones and 553 / 652 are not; the
# fixture was NOT adjusted to reproduce them, because a fixture carrying keys
# production never writes is the exact defect this pair exists to remove.
#
# TWO CONSEQUENCES OF THE CORRECTION, recorded so a later reader does not have
# to re-derive them:
#   1. The `< 500` breach is real but smaller — 5 tokens over, not 54. The
#      conclusion is unchanged.
#   2. design.md's claim that "652 means the 650 ceiling fails too" does NOT
#      hold. Production_Shape with `LIVE_TARGET_SCHEMA` is 604, which passes
#      the two `< 650` ceilings in
#      `test_prompt_under_650_tokens_for_the_target_schema_shape` with 46 tokens
#      spare (`:260`/`:264` before p52). Those two assertions were
#      left alone on the ground that their subject is the target-schema block's
#      cost (p52 C1, risk N7); this measurement independently strengthens that
#      choice, because there is no Production_Shape failure there to fix.
#
# CEILINGS: 600 and 725, unchanged from p52's design. They are set with headroom
# over the measurement rather than tightened onto it, so an ordinary mapping does
# not cry wolf — 95 tokens of headroom on the first, 121 on the second. A lower
# measurement does not tighten a chosen ceiling.
#
# PER-AXIS COSTS, so a breach names WHAT grew rather than failing obscurely
# (p52 constraint 4, `LGR-c284fbb-5`). All measured on this tree:
#
#   * `confidence`: ~5 tokens per field (47 tokens across 10 fields).
#   * a declared target field: ~25 chars, i.e. ~6 tokens (measured 125 chars for
#     5 added fields).
#   * a real 150-char hint: ~36 tokens per field carrying one (+36 for one field,
#     +360 for all ten). p52's design says ~37; 36 is what this tree renders.
#
# TRIP POINTS, both axes, so each ceiling is a choice with known behaviour
# rather than a round number:
#
#   * 600 (no target schema): 15 fields fit at 585; 16 trips it at 601. At 10
#     fields, three 150-char hints fit at 577 and four trip it at 613.
#   * 725 (+ `LIVE_TARGET_SCHEMA`): 17 fields fit at 716; 18 trips it at 732. At
#     10 fields, three 150-char hints fit at 712 and four trip it at 748. A
#     wider target schema alone is the slack axis: 29 declared fields still fits
#     at 719.
#
# ACCEPTED, and it is p52 risk N3 rather than an oversight: hint text is authored
# by the MODEL, so a run whose model writes a long hint on every field renders
# 865 / 964 and would exceed both ceilings in production without any test
# failing. These ceilings are sized for the observed shape — short hints, one or
# two long ones — not for a worst case. The per-axis costs above are what make
# such a breach legible when it does surface.
# ---------------------------------------------------------------------------

# Kept beside the ceilings so a failure message can quote them without a reader
# going hunting. See the per-axis block above for how each was measured.
_PRODUCTION_AXIS_COSTS = (
    "per-axis costs: `confidence` ~5 tokens/field; a declared target field ~25 chars (~6 tokens); "
    "a 150-char hint ~36 tokens on each field carrying one"
)


def test_production_shape_prompt_budget():
    """R1.1, R1.2, R1.6: the no-target-schema render, in Production_Shape.

    Ceiling **600** against a **measured 505** (2022 chars) — 95 tokens of
    headroom, tripping at 16 fields or at four 150-char hints on a 10-field
    mapping. See the block above for the measurement method, the 48-token
    divergence from p52 design.md's 553 and its established cause, and the
    per-axis costs.

    This is the assertion the two `< 500` tests above could never be: they render
    Fixture_Shape and measure the template's fixed cost, which is a useful but
    different subject. Both are retained unchanged at 500 on purpose.

    **Validates: Requirements 1.1, 1.2, 1.6**
    """
    mappings = _make_production_mappings(10)
    prompt = load_prompt_template("transform_generation.j2", mappings=mappings)
    tokens = _estimate_tokens(prompt)

    assert tokens < 600, (
        f"the Production_Shape prompt is ~{tokens} tokens (ceiling: 600; measured 505 when this "
        f"ceiling was set). Length: {len(prompt)} chars. {_PRODUCTION_AXIS_COSTS}"
    )

    # The fixture really is rendering the production branch and the production
    # annotation — otherwise this would be a differently-spelled copy of the
    # 10-field fixed-cost test above.
    # `transformation` takes the template's PRIMARY branch; `transform` (the
    # Fixture_Shape key) reaches only the `elif` fallback.
    assert "direct" in prompt
    assert "[confidence: 0.95]" in prompt
    # ...and the type parentheses are genuinely absent, which is the whole reason
    # this figure is 505 rather than design.md's 553.
    assert "source_field_0 → target_field_0, direct [confidence: 0.95]" in prompt


def test_production_shape_prompt_budget_with_target_schema():
    """R1.1, R1.2, R1.6: the live target-schema render, in Production_Shape.

    Ceiling **725** against a **measured 604** (2419 chars) — 121 tokens of
    headroom, tripping at 18 fields or at four 150-char hints on a 10-field
    mapping. A wider target schema on its own is the slack axis: 29 declared
    fields still fits at 719.

    `LIVE_TARGET_SCHEMA` is the eleven-field shape p37 measured (defined above), which
    R1.6 names specifically: ten mapped target names plus `phone`.

    NOTE the relationship to `test_prompt_under_650_tokens_for_the_target_schema_shape`
    (`:260`/`:264` before p52), which asserts `< 650` on this same
    target-schema shape with a Fixture_Shape fixture. p52's design expected this
    render to measure 652 and therefore expected those two to be failing against
    production. They are not: the honest figure is 604 and passes 650 with 46
    tokens spare. The 652 came from a fixture carrying `source_type` and
    `target_type` — see the divergence note above. So those two keep their
    ceiling, their fixture and their subject (the target-schema block's cost),
    and this assertion carries the production figure for the same shape.

    **Validates: Requirements 1.1, 1.2, 1.6**
    """
    mappings = _make_production_mappings(10)
    prompt = load_prompt_template(
        "transform_generation.j2",
        mappings=mappings,
        target_schema=LIVE_TARGET_SCHEMA,
    )
    tokens = _estimate_tokens(prompt)

    assert tokens < 725, (
        f"the Production_Shape target-schema prompt is ~{tokens} tokens (ceiling: 725; measured 604 "
        f"when this ceiling was set). Length: {len(prompt)} chars. {_PRODUCTION_AXIS_COSTS}"
    )

    # The target-schema block really rendered, so this is not a second copy of
    # the no-schema case above — the same guard the `< 650` test uses.
    assert "TARGET SCHEMA declares these 11 field(s)" in prompt
    # ...and the mapping lines are still Production_Shape inside it.
    assert "source_field_0 → target_field_0, direct [confidence: 0.95]" in prompt
