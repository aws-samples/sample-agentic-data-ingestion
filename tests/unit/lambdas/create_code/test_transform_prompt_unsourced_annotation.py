# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: the ``— NO SOURCE: null/default`` annotation in the prompt.

The render-level consequence of ``code_assembly._unsourced_target_field_names``.
``transform_generation.j2:65-67`` emits one line per declared target field under
a header stating ``df_transformed MUST contain a column for every one``, and
appends ``— NO SOURCE: null/default`` when the field name is in
``unsourced_target_fields``. So the annotation is the ONLY thing standing between
"produce this column" and "invent a source for this column".

WHAT IS NEW HERE, AND WHAT IS DELIBERATELY NOT DUPLICATED
---------------------------------------------------------
``test_target_schema_in_transform_prompt.py`` already covers the target-schema
block at four layers, and three of its tests are adjacent enough to name:

* ``test_a_declared_field_no_mapping_covers_still_reaches_the_prompt`` (``:305``)
  already pins that an uncovered target field REACHES the prompt. Not repeated.
  What this file adds is that it arrives **unannotated**, which is the defect.
* ``test_render_tolerates_an_omitted_or_none_target_schema`` (``:396``) pins the
  ``| default({}, true)`` tolerance for the *target_schema* slot. The
  ``unsourced_target_fields`` slot has its own ``| default([], true)`` for the
  same ``StrictUndefined`` reason (``LGR-d3c81fc-1``) and no test, so its
  tolerance IS asserted here.
* ``test_source_schema_is_still_not_rendered_and_that_is_deliberate`` (``:471``)
  pins that ``source_schema`` is accepted-and-dropped. Not repeated. It is the
  fact that makes branch A possible — the prompt is never shown the source
  column list — but this file does not re-assert it.

The template's own comment records a measured token budget: "each unsourced
field costs +7 tokens / +26 chars", against a 500-token budget
``test_transform_prompt.py`` asserts. The cost is asserted here as a range
rather than a fixed number, because the exact figure depends on the field name.

╔═══════════════════════════════════════════════════════════════════════════╗
║ WAVE 3 CONTRACT — SETTLED. THE MARKED ASSERTIONS WERE INVERTED.           ║
║                                                                           ║
║ Three tests here encoded the branch B defect. Wave 3 inverted them and     ║
║ renamed the marker ``BRANCH_B_DEFECT`` → ``BRANCH_B_FIXED``; the           ║
║ assertions stay, reversed in place with their reason at the assertion      ║
║ (``LGR-60c4985-6`` / ``LGR-9e76249-10``). Grep ``BRANCH_B``.               ║
║                                                                           ║
║   test_a_normalised_away_field_is_annotated_BRANCH_B_FIXED                 ║
║     (was ..._is_mandatory_but_unannotated_BRANCH_B_DEFECT)                 ║
║   test_the_live_record_shape_is_annotated_too_BRANCH_B_FIXED               ║
║     (was ..._is_equally_unannotated_BRANCH_B_DEFECT)                       ║
║   test_both_unmapped_element_shapes_are_annotated_BRANCH_B_FIXED           ║
║     (was ..._are_equally_invisible_BRANCH_B_DEFECT; 2 parametrised cases)  ║
║                                                                           ║
║ SURVIVED UNCHANGED: everything else here — the annotated/not-annotated     ║
║ pair, the wording assertions, the line-count identity, the per-field byte  ║
║ budget, and the byte-identity of the empty slot. Wave 3 widened WHICH      ║
║ fields are annotated and changed nothing about HOW: the annotation string, ║
║ its placement on the existing field line and the zero-cost empty case are  ║
║ all as they were. ``unmapped_target_fields`` entries carry a               ║
║ ``default_value``; it is deliberately NOT rendered, because that would     ║
║ change the annotation text these tests pin.                                ║
╚═══════════════════════════════════════════════════════════════════════════╝
"""

import json
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

# The REAL loader and the REAL template. A mocked loader would make every
# assertion here a statement about the mock (`LGR-28b4d75-5`, the reasoning
# `test_target_schema_in_transform_prompt.py` records for the same choice).
import code_assembly as _ca
import transform_generation as _tg

from prompt_template_loader import load_prompt_template

_ANNOTATION = "— NO SOURCE: null/default"

TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "cust_id": {"type": "string"},
        "email": {"type": "string"},
        "phone": {"type": "string"},
    },
}

SOURCED_MAPPINGS = [
    {"source_field": "id", "source_type": "string", "target_field": "cust_id", "transformation": "direct"},
    {"source_field": "email_addr", "source_type": "string", "target_field": "email", "transformation": "direct"},
]


def _field_line(prompt: str, name: str) -> str:
    """The single TARGET SCHEMA block line for ``name``.

    Scoped to the block on purpose: a target field is usually also some
    mapping's ``target_field``, so a whole-prompt search would match the mapping
    lines and prove nothing about the annotation.
    """
    assert "TARGET SCHEMA declares" in prompt, "no target block rendered"
    block = prompt.split("TARGET SCHEMA declares", 1)[1].split("Generate ONLY", 1)[0]
    lines = [ln for ln in block.splitlines() if ln.startswith("  ") and ln.strip().startswith(name)]
    assert len(lines) == 1, f"expected exactly one block line for {name!r}, got {lines}"
    return lines[0]


def _prompt(unsourced=None, target_schema=None, mappings=None):
    return load_prompt_template(
        "transform_generation.j2",
        mappings=SOURCED_MAPPINGS if mappings is None else mappings,
        target_schema=TARGET_SCHEMA if target_schema is None else target_schema,
        unsourced_target_fields=unsourced,
    )


# ===========================================================================
# The annotation is present exactly for the named fields (item 7)
# ===========================================================================


def test_a_named_field_is_annotated_and_an_unnamed_one_is_not():
    """The whole point of the slot, as one comparison.

    ``phone`` is in ``unsourced_target_fields`` and carries the annotation;
    ``email`` is not and does not. Both are rendered on the same run, so this
    cannot pass by the annotation being globally on or globally off.
    """
    prompt = _prompt(unsourced=["phone"])

    assert _ANNOTATION in _field_line(prompt, "phone")
    assert _ANNOTATION not in _field_line(prompt, "email")
    assert _ANNOTATION not in _field_line(prompt, "cust_id")


def test_the_annotation_rides_the_existing_field_line_and_adds_none():
    """It ANNOTATES rather than adding a line or a block.

    The template's comment states why: the 498 → 504 token incident recorded in
    the same file was caused by tag-only lines, "and a second sentence here
    would cost the same way". So the field count and the line count of the
    target block must not move when a field becomes unsourced.
    """
    without = _prompt(unsourced=None)
    with_one = _prompt(unsourced=["phone"])

    assert "TARGET SCHEMA declares these 3 field(s)" in without
    assert "TARGET SCHEMA declares these 3 field(s)" in with_one
    assert len(without.splitlines()) == len(with_one.splitlines())


def test_the_annotation_says_set_it_to_null_never_omit_the_column():
    """The wording is load-bearing, not cosmetic.

    ``_build_validation_code`` derives ``expected`` from
    ``target_schema["properties"].keys()``, so a missing target column fails the
    ``schema_compliance`` gate. Telling the model to skip the field would trade a
    quarantine for a failed run — the template comment states this and
    ``LGR-b2f79ef-1`` is the record that made ``schema_compliance`` able to fail
    a run at all.
    """
    line = _field_line(_prompt(unsourced=["phone"]), "phone")

    assert "null/default" in line
    assert "omit" not in line.lower()
    assert "skip" not in line.lower()
    # The mandatory-column header still applies to it — the annotation qualifies
    # the instruction, it does not withdraw it.
    assert "MUST contain a column for every one" in _prompt(unsourced=["phone"])


def test_every_declared_field_can_be_annotated_at_once():
    """No field is exempt: a schema where nothing has a source annotates all
    three. The DQDL side's matching boundary case is
    ``test_code_assembly_unsourced_target_fields.py::
    test_the_coupling_holds_when_every_entry_is_unsourced``."""
    prompt = _prompt(unsourced=["cust_id", "email", "phone"])

    for name in TARGET_SCHEMA["properties"]:
        assert _ANNOTATION in _field_line(prompt, name), name


def test_a_name_not_declared_by_the_target_schema_annotates_nothing():
    """The slot is matched against the target schema's declared keys.

    A name in ``unsourced_target_fields`` that the schema does not declare has no
    line to annotate, and must not manufacture one — the block is generated from
    ``target_schema["properties"]``, which is the set the compliance gate scores.
    """
    prompt = _prompt(unsourced=["not_a_declared_field"])

    assert _ANNOTATION not in prompt
    assert "not_a_declared_field" not in prompt
    assert "TARGET SCHEMA declares these 3 field(s)" in prompt


def test_the_slot_is_matched_on_the_exact_declared_name():
    """``name in unsourced_fields`` is exact membership, not a prefix or
    substring test. ``phone_number`` must not annotate ``phone``."""
    schema = {"type": "object", "properties": {"phone": {"type": "string"}}}
    prompt = _prompt(unsourced=["phone_number"], target_schema=schema)

    assert _ANNOTATION not in _field_line(prompt, "phone")


# ===========================================================================
# StrictUndefined tolerance for the new slot (LGR-d3c81fc-1)
# ===========================================================================


def test_an_omitted_none_or_empty_unsourced_list_renders_identically():
    """Omitted, ``None`` and ``[]`` all render, and all render byte-identically.

    The loader uses ``StrictUndefined``, so a referenced-but-unsupplied variable
    raises **mid-pipeline** rather than at synth (``LGR-d3c81fc-1``).
    ``generate_transform_with_ai``'s parameter defaults to ``None`` and
    ``generate_code`` passes ``[]`` when nothing is unsourced, so all three
    shapes are live. ``| default([], true)`` rather than a bare ``| default`` is
    what covers ``None``.

    Byte-identity also means a pipeline with nothing unsourced keeps its
    pre-p37 prompt exactly, so C12 cannot move those generated scripts.
    """
    omitted = load_prompt_template(
        "transform_generation.j2", mappings=SOURCED_MAPPINGS, target_schema=TARGET_SCHEMA
    )
    none = _prompt(unsourced=None)
    empty = _prompt(unsourced=[])

    assert omitted == none == empty
    assert _ANNOTATION not in omitted


def test_the_slot_costs_zero_bytes_when_there_is_no_target_schema_either():
    """With no target schema there is no block to annotate, and the combined
    slot still emits nothing — the condition
    ``test_target_schema_in_transform_prompt.py::
    test_the_empty_target_slot_costs_zero_tokens`` guards for the sibling slot."""
    a = load_prompt_template("transform_generation.j2", mappings=SOURCED_MAPPINGS)
    b = load_prompt_template(
        "transform_generation.j2", mappings=SOURCED_MAPPINGS, unsourced_target_fields=["phone"]
    )

    assert a == b
    assert "TARGET SCHEMA" not in a


def test_each_annotated_field_costs_a_bounded_amount_of_prompt():
    """The measured cost, held to a range rather than a fixed number.

    The template comment records +7 tokens / +26 chars per unsourced field
    against the 500-token budget ``test_transform_prompt.py`` asserts. Asserted
    as ``<= 40`` chars per field so a rewrite that turns the annotation into a
    sentence is caught here rather than as a budget failure in another file.
    """
    base = len(_prompt(unsourced=None))
    one = len(_prompt(unsourced=["phone"]))
    three = len(_prompt(unsourced=["cust_id", "email", "phone"]))

    assert 0 < one - base <= 40
    assert 0 < (three - base) / 3 <= 40


# ===========================================================================
# THE DEFECT AT RENDER LEVEL — through generate_code (item 7 / branch B)
# ===========================================================================


def _prompt_through_generate_code(mapping_result, target_schema):
    """Render through ``code_assembly.generate_code`` with only the model stubbed.

    The generator is the real ``transform_generation.generate_transform_with_ai``
    with an injected ``agent_factory``, so the render context is production's —
    including whatever ``generate_code`` computed for ``unsourced_target_fields``.
    """
    seen: list[str] = []

    def _agent_factory(**_kwargs):
        def _agent(prompt_text):
            seen.append(prompt_text)
            return "df_transformed = df"

        return _agent

    def _generate_transform(**kwargs):
        return _tg.generate_transform_with_ai(agent_factory=_agent_factory, **kwargs)

    _ca.generate_code(
        pipeline_metadata={
            "source_type": "S3_UPLOAD",
            "source_config": {},
            "target_config": {
                "type": "s3_tables",
                "bucket_name": "my-table-bucket",
                "namespace": "analytics",
                "table_name": "orders",
                "write_mode": "append",
                "source_format": "json",
            },
            "source_schema": {"properties": {"id": {"type": "string"}, "email_addr": {"type": "string"}}},
            "target_schema": target_schema,
        },
        mapping_result=mapping_result,
        generate_transform=_generate_transform,
        quarantine_bucket="adi-quarantine-test",
    )
    assert len(seen) == 1, "expected exactly one model call through generate_code"
    return seen[0]


def test_an_entry_claiming_a_target_with_no_source_is_annotated_end_to_end():
    """Branch-A-shaped input, through the real chain: the ``phone`` shape works.

    A ``mappings`` entry carrying ``target_field: phone`` and no
    ``source_field`` — the pre-C9 live shape — is annotated in the prompt
    ``generate_code`` actually sends. This is the case C12 was built for, and it
    is green.
    """
    mapping_result = {
        "field_mappings": [*SOURCED_MAPPINGS, {"target_field": "phone", "source_field": None, "confidence": 0.2}]
    }

    prompt = _prompt_through_generate_code(mapping_result, TARGET_SCHEMA)

    assert _ANNOTATION in _field_line(prompt, "phone")
    assert _ANNOTATION not in _field_line(prompt, "email")


def test_a_normalised_away_field_is_annotated_BRANCH_B_FIXED():
    """A target field living only in ``unmapped_target_fields`` is annotated.
    INVERTED by wave 3.

    ⚠️ **WAS ``test_a_normalised_away_field_is_mandatory_but_unannotated_BRANCH_B_DEFECT``**,
    which asserted ``_ANNOTATION not in _field_line(prompt, "phone")``.

    This is the defect at the layer where it did its damage. The prompt told the
    model ``df_transformed MUST contain a column for every one`` and listed
    ``phone`` with no qualification, and the mapping block above names no source
    for it — so the model produced the column from the nearest plausible input.
    The generated transform then read a column the strictly-schema-derived test
    frame does not have, which is the ``KeyError: 'email'`` the run failed on.

    ``test_target_schema_in_transform_prompt.py::
    test_a_declared_field_no_mapping_covers_still_reaches_the_prompt`` pins that
    the field REACHES the prompt; what this adds is that it now arrives
    qualified.

    The mapping record below is exactly what p37 C9's
    ``_normalise_mapping_structure`` produces — it MOVES a source-less entry out
    of ``mappings`` into ``unmapped_target_fields`` — so this is the normal
    post-C9 shape, not an exotic one.
    """
    mapping_result = {
        "field_mappings": SOURCED_MAPPINGS,
        "unmapped_target_fields": [
            {"field": "phone", "default_value": None, "reason": "No source field in generated mapping"}
        ],
    }

    prompt = _prompt_through_generate_code(mapping_result, TARGET_SCHEMA)

    # The field is still declared mandatory — the annotation qualifies the
    # instruction, it does not withdraw it ...
    assert "MUST contain a column for every one" in prompt
    assert "phone" in _field_line(prompt, "phone")
    # ... and it now carries the annotation. INVERTED; was `not in`.
    assert _ANNOTATION in _field_line(prompt, "phone")
    # Only that field. The two sourced fields are untouched, so this cannot pass
    # by the annotation being globally on.
    assert _ANNOTATION not in _field_line(prompt, "email")
    assert _ANNOTATION not in _field_line(prompt, "cust_id")
    # The mapping block above still names no source for it — that has not
    # changed and is not this fix's business; what changed is that the model is
    # now told so instead of left to improvise.
    assert "phone" not in prompt.split("TARGET SCHEMA declares", 1)[0]
    # `default_value` is on the entry and is deliberately not rendered: widening
    # WHICH fields are annotated must not widen HOW.
    assert "default_value" not in prompt
    assert "No source field in generated mapping" not in prompt


def test_the_live_record_shape_is_annotated_too_BRANCH_B_FIXED():
    """The record shape production actually writes is annotated. INVERTED by wave 3.

    ⚠️ **WAS ``test_the_live_record_shape_is_equally_unannotated_BRANCH_B_DEFECT``.**

    ``_extract_field_mappings``' own docstring ranks the three storage variants
    and calls the third — ``mapping_result`` as a JSON **string** with a
    ``mappings`` key — the "current schema_mapping output". The test above uses
    the legacy top-level variant; this one uses the live one, where
    ``unmapped_target_fields`` is nested *inside the JSON string that
    ``_extract_field_mappings`` parses and then discards* (it returns
    ``parsed.get("mappings", [])`` and nothing else).

    So the fix is not only a signature change on
    ``_unsourced_target_field_names``: reaching ``unmapped_target_fields`` from
    ``generate_code`` needed a sibling extractor resolving the **same three
    variants**, which is ``mapping_resolution._extract_unmapped_target_fields``
    (characterized in ``test_extract_field_mappings_characterization.py``). A fix
    that read ``mapping_result["unmapped_target_fields"]`` directly would pass the
    previous test and fail this one. That is why both shapes are pinned.
    """
    mapping_result = {
        "mapping_result": json.dumps(
            {
                "mappings": SOURCED_MAPPINGS,
                "unmapped_target_fields": [
                    {"field": "phone", "default_value": None, "reason": "No source field in generated mapping"}
                ],
            }
        )
    }

    prompt = _prompt_through_generate_code(mapping_result, TARGET_SCHEMA)

    # The mappings really did resolve out of the JSON string, so the fixture is
    # exercising the variant it claims to.
    assert "email_addr" in prompt
    assert _ANNOTATION in _field_line(prompt, "phone")
    assert _ANNOTATION not in _field_line(prompt, "email")


@pytest.mark.parametrize("unmapped_entry", [{"field": "phone"}, "phone"])
def test_both_unmapped_element_shapes_are_annotated_BRANCH_B_FIXED(unmapped_entry):
    """Both ``unmapped_target_fields`` element shapes are annotated. INVERTED.

    ⚠️ **WAS ``test_both_unmapped_element_shapes_are_equally_invisible_BRANCH_B_DEFECT``.**

    Two producers write that list and disagree on the element shape — a dict from
    ``generate_simple_mapping`` and, because a Bedrock response is model output
    parsed as returned, a bare string. ``schema_mapping``'s
    ``_unmapped_target_field_name`` reads both, and so does
    ``code_assembly._unmapped_target_field_name``, its copy. Parametrised so a fix
    that only read the dict shape fails here.
    """
    mapping_result = {"field_mappings": SOURCED_MAPPINGS, "unmapped_target_fields": [unmapped_entry]}

    prompt = _prompt_through_generate_code(mapping_result, TARGET_SCHEMA)

    assert _ANNOTATION in _field_line(prompt, "phone")


def test_an_unmapped_name_the_target_schema_does_not_declare_annotates_nothing():
    """The widened SOURCE of names must not widen the annotated SET.

    ``unmapped_target_fields`` can name a field the target schema does not
    declare — the two are written by different producers at different times. The
    block is generated from ``target_schema["properties"]``, which is the set the
    compliance gate scores, so such a name has no line to annotate and must not
    manufacture one. The direct-call form of this is
    ``test_a_name_not_declared_by_the_target_schema_annotates_nothing`` above;
    this one runs it through ``generate_code``.
    """
    mapping_result = {
        "field_mappings": SOURCED_MAPPINGS,
        "unmapped_target_fields": [{"field": "not_a_declared_field"}, {"field": "phone"}],
    }

    prompt = _prompt_through_generate_code(mapping_result, TARGET_SCHEMA)

    assert "not_a_declared_field" not in prompt
    assert "TARGET SCHEMA declares these 3 field(s)" in prompt
    assert _ANNOTATION in _field_line(prompt, "phone")


def test_a_record_with_nothing_unsourced_renders_the_pre_fix_prompt():
    """A pipeline with neither list contributing a name keeps its prompt exactly.

    The zero-cost case, through ``generate_code`` rather than the loader: an empty
    ``unmapped_target_fields`` must render byte-identically to a record that has
    no such key at all, so the fix cannot move the generated script for any
    pipeline it does not apply to.
    """
    without_key = _prompt_through_generate_code({"field_mappings": SOURCED_MAPPINGS}, TARGET_SCHEMA)
    with_empty = _prompt_through_generate_code(
        {"field_mappings": SOURCED_MAPPINGS, "unmapped_target_fields": []}, TARGET_SCHEMA
    )

    assert without_key == with_empty
    assert _ANNOTATION not in without_key
