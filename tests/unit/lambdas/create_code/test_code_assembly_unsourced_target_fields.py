# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: ``code_assembly``'s two unsourced-field helpers.

These are the ``create_code`` half of a two-copy definition. CodeGraph reports
**no covering tests** for either symbol characterized here, and both are about to
be changed, so their current behaviour is pinned first — from the docstrings and
the requirements they cite, not from a value observed by running them, because
current output is what carries the defect.

WHAT IS BEING CHARACTERIZED, AND WHY IT MATTERS
----------------------------------------------
``_has_usable_source_field`` (``code_assembly.py:138-159``) is the single
predicate two things are derived through: the DQDL Completeness/Uniqueness skip
(p37 R2.1/R2.2, ``LGR-9e76249-4`` names the deterministic half) and the transform
prompt's ``— NO SOURCE`` annotation (p37 R3.5 / C12). Its docstring states that
truthiness is deliberately NOT used, and gives the reason: a mapping record is
parsed JSON, so ``source_field`` can be ``None``, ``""``, ``"   "`` or a
non-string. A truthy non-string (``123``) is the case a truthiness test would
wave through, so it is a case here.

``_unsourced_target_field_names`` reads **both** ``unmapped_target_fields`` and
``mappings``. It did not: until wave 3 it read ``mappings`` alone, and its
docstring argued the omission was safe. That reading was the branch B defect — a
target field the mapping record carries only in ``unmapped_target_fields`` is
declared mandatory by the target-schema block in ``transform_generation.j2:65-67``
and arrived with **no** ``— NO SOURCE`` annotation, so the model invented a source
for it.

╔═══════════════════════════════════════════════════════════════════════════╗
║ WAVE 3 CONTRACT — SETTLED. THE MARKED ASSERTIONS WERE INVERTED.           ║
║                                                                           ║
║ Wave 1 marked the assertions encoding the branch B defect with            ║
║ ``BRANCH_B_DEFECT``. Wave 3 inverted them and renamed the marker to        ║
║ ``BRANCH_B_FIXED``: the assertions stay, reversed in place with their      ║
║ reason at the assertion (``LGR-60c4985-6`` / ``LGR-9e76249-10``, the       ║
║ convention that a deliberate hold is amended rather than deleted so the   ║
║ reversal reads as a reversal). ``BRANCH_B_DEFECT`` was not left on tests   ║
║ that now assert correct behaviour, which would have read as the defect    ║
║ still being pinned; grep ``BRANCH_B`` to find both generations.            ║
║                                                                           ║
║ Inverted by wave 3 (2 tests in this file):                                 ║
║   test_both_lists_are_read_BRANCH_B_FIXED                                  ║
║     (was test_unmapped_target_fields_is_not_read_at_all_BRANCH_B_DEFECT)   ║
║   test_a_field_only_in_unmapped_target_fields_is_named_BRANCH_B_FIXED      ║
║     (was ..._is_unnamed_BRANCH_B_DEFECT; now asserts ``== ["phone"]``)     ║
║                                                                           ║
║ SURVIVED UNCHANGED — every other test in this file: the four              ║
║ unusable-shape assertions, the order/dedup assertions, and                 ║
║ ``test_the_annotated_set_is_the_completeness_skipped_set`` (the branch A   ║
║ coupling).                                                                 ║
║                                                                           ║
║ THE COUPLING QUESTION, RESOLVED. Wave 1 required that a change reading     ║
║ both lists either keep the coupling test green by teaching                 ║
║ ``_generate_dqdl_rules`` the same set, or state why the coupling is        ║
║ broken. Neither was needed. The coupling test calls                       ║
║ ``_unsourced_target_field_names(mappings)`` with one list, so the new      ║
║ parameter defaults empty and its output is unchanged — it is green         ║
║ verbatim. And the coupling itself is not broken, it was stated over too    ║
║ narrow a domain: over ``mappings`` alone it holds as before, and a name    ║
║ reaching the annotated set from ``unmapped_target_fields`` is annotated    ║
║ AND carries no Completeness rule, because it never enters                  ║
║ ``_generate_dqdl_rules``' loop at all. So "annotated ⟺ no Completeness     ║
║ rule" holds over the UNION of the two lists, which is the domain           ║
║ ``schema_compliance`` actually scores.                                     ║
║ ``test_the_coupling_holds_over_both_lists`` states it there.               ║
║ Teaching ``_generate_dqdl_rules`` about ``unmapped_target_fields`` was     ║
║ rejected and must stay rejected: it emits ``Completeness >= 0.95`` on a    ║
║ column the transform sets to a constant, which is the p37 R2.1/R2.2        ║
║ defect (every row fails, the batch is quarantined).                        ║
╚═══════════════════════════════════════════════════════════════════════════╝

LEDGER STATUS
-------------
``why`` on ``code_assembly.py`` returns the p37 commit ``9e76249a33`` (80 files —
broad; treat as background) plus the p33 commit. The records whose bodies
actually name this work are ``LGR-9e76249-4`` (the contradiction check records
and never blocks), ``LGR-9e76249-5`` (AST, not substring matching) and
``LGR-9e76249-7`` (C10 ships before C9; it is the record that states
``unsourced_target_field_names`` reads BOTH lists — on the *schema_mapping*
side). **No record governed ``code_assembly._unsourced_target_field_names``
reading only one list**; that asymmetry was stated only in the function's own
docstring, which wave 3 falsified and rewrote. Still recorded as a gap in the
ledger: nothing ratified the asymmetry, so nothing had to be superseded to
remove it. ``why`` on ``mapping_resolution.py`` — where wave 3 added the sibling
extractor — returns only the 634-file snapshot ``LGR-0176ccc-1`` with no records
at all, which is a second gap in the same area.
"""

import os
import sys

import jinja2
import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

# `code_assembly` is imported DIRECTLY rather than through `lambda_function`.
# Neither helper is re-exported by `lambda_function` (its `from code_assembly
# import (...)` block at `:72-78` lists four names and neither of these is one),
# and importing the handler module would put this file in the `sys.modules
# ["lambda_function"]` tug-of-war the other create_code test files manage with an
# autouse fixture. `code_assembly` owns no AWS clients and reads no environment
# (module docstring: "This module owns NO AWS clients and does NOT import
# strands"), so a direct import needs no stubs.
import code_assembly as _ca

from identifier_utils import IdentifierValidationError

# ---------------------------------------------------------------------------
# Every `source_field` shape the predicate must call unusable.
#
# The four the docstring names, plus two non-strings. `123` is included because
# it is TRUTHY: it is the shape that separates this predicate from a truthiness
# test, which is the distinction the docstring exists to record.
# ---------------------------------------------------------------------------

_ABSENT = object()

UNUSABLE_SOURCE_FIELDS = [
    pytest.param(_ABSENT, id="absent"),
    pytest.param(None, id="None"),
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param(123, id="non-string-truthy"),
    pytest.param(["mobile"], id="non-string-list"),
]


def _entry(target_field: str, source_field=_ABSENT) -> dict:
    """One Field_Mapping_Entry; ``source_field`` omitted when ``_ABSENT``."""
    entry: dict = {"target_field": target_field, "confidence": 0.9}
    if source_field is not _ABSENT:
        entry["source_field"] = source_field
    return entry


# ===========================================================================
# `_has_usable_source_field` — the four unusable shapes (item 1)
# ===========================================================================


@pytest.mark.parametrize("source_field", UNUSABLE_SOURCE_FIELDS)
def test_has_usable_source_field_rejects_every_unusable_shape(source_field):
    """``None``, ``""``, ``"   "`` and a non-string all answer False.

    The docstring's own list, asserted rather than trusted: "a mapping record is
    parsed JSON, so the value can be ``None``, ``""``, ``"   "`` or a
    non-string."
    """
    assert _ca._has_usable_source_field(_entry("phone", source_field)) is False


def test_has_usable_source_field_is_not_a_truthiness_test():
    """The distinction the docstring exists to record, isolated.

    ``123`` is truthy and unusable; ``"   "`` is truthy and unusable. A
    ``bool(source_field)`` implementation would accept both. Conversely ``"0"``
    is a legitimate column name that a naive numeric coercion would drop.
    """
    assert bool(123) is True
    assert _ca._has_usable_source_field(_entry("phone", 123)) is False

    assert bool("   ") is True
    assert _ca._has_usable_source_field(_entry("phone", "   ")) is False

    assert _ca._has_usable_source_field(_entry("phone", "0")) is True


@pytest.mark.parametrize("source_field", ["mobile", "  mobile  ", "0"])
def test_has_usable_source_field_accepts_a_named_source_column(source_field):
    """A string that survives ``.strip()`` names a column, padding and all."""
    assert _ca._has_usable_source_field(_entry("phone", source_field)) is True


@pytest.mark.parametrize("mapping", [None, 3, "phone", ["phone"], (), object()])
def test_has_usable_source_field_is_total_on_a_non_dict_entry(mapping):
    """A ``mappings`` element can be a bare string — model output parsed as
    returned. The predicate answers False rather than raising."""
    assert _ca._has_usable_source_field(mapping) is False


# ===========================================================================
# `_unsourced_target_field_names` — what it names (item 3)
# ===========================================================================


@pytest.mark.parametrize("source_field", UNUSABLE_SOURCE_FIELDS)
def test_an_entry_claiming_a_target_without_a_source_is_named(source_field):
    """The ``phone`` shape: a target field claimed with no source column.

    Observed on pipeline ``newtest-ab2a08a0`` / run
    ``run-2026-08-09-15-38-19-5c5f5f8a`` — the incident the docstring cites.
    """
    assert _ca._unsourced_target_field_names([_entry("phone", source_field)]) == ["phone"]


def test_a_sourced_entry_is_not_named():
    """The contrast case: a mapping that names a source column is not unsourced."""
    assert _ca._unsourced_target_field_names([_entry("phone", "mobile")]) == []


def test_names_are_order_preserving():
    """"Order-preserving and de-duplicated" — the order half.

    Declared order of the ``mappings`` list, not sorted and not set order. The
    prompt annotation is per-field, so a stable order keeps a regeneration's
    prompt byte-comparable against the previous attempt's.
    """
    mappings = [
        _entry("zeta", None),
        _entry("alpha", ""),
        _entry("email", "email_addr"),
        _entry("middle", "   "),
    ]
    assert _ca._unsourced_target_field_names(mappings) == ["zeta", "alpha", "middle"]


def test_a_repeated_target_field_is_named_once():
    """"Order-preserving and de-duplicated" — the dedup half.

    Deduplicated on the STRIPPED name, so ``"phone"`` and ``" phone "`` are one
    field. Two prompt annotations for one column would read as two columns.
    """
    mappings = [_entry("phone", None), _entry(" phone ", ""), _entry("fax", None)]
    assert _ca._unsourced_target_field_names(mappings) == ["phone", "fax"]


def test_names_are_stripped():
    """The name rendered into the prompt is the stripped one — it has to match
    the target-schema key the same template renders from
    ``target_schema["properties"]``."""
    assert _ca._unsourced_target_field_names([_entry("  phone  ", None)]) == ["phone"]


@pytest.mark.parametrize(
    ("label", "mappings"),
    [
        ("None", None),
        ("empty list", []),
        ("non-dict elements", [None, 3, "phone", ["phone"]]),
        ("no target_field", [{"source_field": None}]),
        ("target_field None", [{"target_field": None, "source_field": None}]),
        ("target_field blank", [{"target_field": "   ", "source_field": None}]),
        ("target_field non-string", [{"target_field": 7, "source_field": None}]),
    ],
)
def test_is_total_on_degenerate_input(label, mappings):
    """The reader never raises and names nothing it cannot name.

    An entry that names no target field contributes no name — naming one would
    mean inventing an empty column name and annotating it in the prompt.
    """
    assert _ca._unsourced_target_field_names(mappings) == [], label


# ===========================================================================
# THE ASYMMETRY — branch B, INVERTED by wave 3 (item 2)
#
# See the WAVE 3 CONTRACT block in the module docstring. The two tests below
# asserted the defect; they now assert the fix, reversed in place.
# ===========================================================================


def test_both_lists_are_read_BRANCH_B_FIXED():
    """``unmapped_target_fields`` reaches the output. INVERTED by wave 3.

    ⚠️ **WAS ``test_unmapped_target_fields_is_not_read_at_all_BRANCH_B_DEFECT``**,
    which asserted the second list could not reach this reader at all — the
    signature took a ``field_mappings`` list and nothing else, and the docstring
    argued the omission was safe because such a field "carries no rule and the
    model is not told to produce it from anything". The target-schema block in
    ``transform_generation.j2:65-67`` falsified that: it tells the model to
    produce every declared field, unconditionally.

    Asserted as the DIFFERENCE between two calls over the same ``mappings``, so
    the claim is "this input changes the answer" rather than "this input happens
    to produce that answer".
    """
    sourced_only = [_entry("email", "email_addr")]

    # Nothing unsourced in `mappings`, and no second list: still nothing named.
    assert _ca._unsourced_target_field_names(sourced_only) == []

    # The same `mappings`, with the record's second list supplied: named.
    record_shaped = {"mappings": sourced_only, "unmapped_target_fields": [{"field": "phone"}]}
    assert _ca._unsourced_target_field_names(record_shaped["mappings"], record_shaped["unmapped_target_fields"]) == [
        "phone"
    ]

    # And it agrees with the sibling reader on the mapping-record side, which has
    # read both lists since p37 C10 (LGR-9e76249-7, template / ratified). That
    # cross-Lambda comparison lives in
    # tests/unit/lambdas/schema_mapping/test_unsourced_definition_parity.py;
    # this assertion is the create_code half of it.
    assert _ca._unsourced_target_field_names([], [{"field": "phone"}]) == ["phone"]


def test_a_field_only_in_unmapped_target_fields_is_named_BRANCH_B_FIXED():
    """A target field living only in ``unmapped_target_fields`` IS named, so the
    prompt annotates it. INVERTED by wave 3.

    ⚠️ **WAS ``test_a_field_only_in_unmapped_target_fields_is_unnamed_BRANCH_B_DEFECT``**,
    which asserted ``named == []``.

    This is the exact shape p37 C9's ``_normalise_mapping_structure`` produces:
    it MOVES a source-less entry out of ``mappings`` and into
    ``unmapped_target_fields``. So after C9 landed, the normal path for an
    unsourced field was the one this reader could not see — C9's fix on the
    mapping side had made the codegen side blind. It is the case the annotation
    exists for, and it is now covered.
    """
    after_c9_normalisation = {
        "mappings": [_entry("email", "email_addr")],
        "unmapped_target_fields": [
            {"field": "phone", "default_value": None, "reason": "No source field in generated mapping"}
        ],
    }

    named = _ca._unsourced_target_field_names(
        after_c9_normalisation["mappings"], after_c9_normalisation["unmapped_target_fields"]
    )

    assert named == ["phone"], "the normalised-away field is visible to codegen"
    # `default_value` is carried by the entry and deliberately not rendered — the
    # annotation text is pinned, so widening WHICH fields are annotated must not
    # widen HOW. See test_transform_prompt_unsourced_annotation.py.
    assert "email" not in named


def test_a_name_from_either_list_is_named_once():
    """Cross-LIST de-duplication, the axis the single-list reader could not have.

    Reachable on a record written before C9 and then re-normalised, or one an
    operator edited. Two annotations for one column would read as two columns.
    The within-list dedup case is ``test_a_repeated_target_field_is_named_once``.
    """
    mappings = [_entry("email", "email_addr"), _entry("phone", None)]
    unmapped = [{"field": "phone"}, "fax"]

    assert _ca._unsourced_target_field_names(mappings, unmapped) == ["phone", "fax"]


def test_unmapped_names_come_before_mapping_names():
    """The declared read order: list 1 then list 2, each order-preserving.

    The same order ``schema_mapping.unsourced_target_field_names`` declares
    (before its ``required`` partition, which this reader has no schema for), so
    the two produce identical LISTS and not merely identical sets — which is what
    ``test_unsourced_asymmetry_prop.py``'s P1 and P2 compare.
    """
    mappings = [_entry("from_mappings_a", None), _entry("from_mappings_b", "")]
    unmapped = ["from_unmapped_a", {"field": "from_unmapped_b"}]

    assert _ca._unsourced_target_field_names(mappings, unmapped) == [
        "from_unmapped_a",
        "from_unmapped_b",
        "from_mappings_a",
        "from_mappings_b",
    ]


@pytest.mark.parametrize("unmapped_entry", [{"field": "phone"}, "phone", {"field": "  phone  "}, "  phone  "])
def test_both_unmapped_element_shapes_are_read(unmapped_entry):
    """A dict's ``field`` and a bare string both name a field, stripped.

    Two producers write that list and disagree on the element shape — a dict from
    ``schema_mapping.generate_simple_mapping``, and a bare string because a
    Bedrock response is model output parsed as returned.
    ``schema_mapping._unmapped_target_field_name`` reads both; so does this side's
    copy of it.
    """
    assert _ca._unsourced_target_field_names([], [unmapped_entry]) == ["phone"]


@pytest.mark.parametrize(
    ("label", "unmapped"),
    [
        ("None", None),
        ("empty list", []),
        ("junk elements", [None, 7, ["phone"], (), object()]),
        ("dict with no field key", [{"no_field_key": "phone"}]),
        ("dict with non-string field", [{"field": 3}]),
        ("dict with blank field", [{"field": "   "}]),
        ("empty dict", [{}]),
        ("blank string", ["   "]),
    ],
)
def test_the_unmapped_list_is_total_on_degenerate_input(label, unmapped):
    """The second list never raises and names nothing it cannot name.

    Same total-ness the first list has: this feeds a prompt annotation, so it
    must never be the thing that fails code generation.
    """
    assert _ca._unsourced_target_field_names([], unmapped) == [], label
    # And it cannot suppress a name the mappings list supplies either.
    assert _ca._unsourced_target_field_names([_entry("phone", None)], unmapped) == ["phone"], label


# ===========================================================================
# THE BRANCH A COUPLING (item 6)
#
# `_unsourced_target_field_names` and `_generate_dqdl_rules` are handed the SAME
# list — `_extract_field_mappings(mapping_result)` — through the SAME predicate,
# so "the field the prompt marks unsourced is exactly the field that gets no
# Completeness rule" (docstring, `code_assembly.py:167-173`).
#
# MUST SURVIVE. Wave 2 fails codegen on a `source_field` the source schema does
# not declare; that must not silently split these two derivations apart.
# ===========================================================================


def _completeness_named(dqdl: str) -> set[str]:
    """Target fields carrying a Completeness rule in a rendered ruleset."""
    return {
        line.split('"')[1] for line in dqdl.splitlines() if "Completeness" in line and '"' in line
    }


@pytest.mark.parametrize("source_field", UNUSABLE_SOURCE_FIELDS)
def test_the_annotated_set_is_the_completeness_skipped_set(source_field):
    """The annotated set and the Completeness-skipped set are the same set.

    Computed independently from the two functions over one list, then compared —
    rather than asserting each against a literal, which would pass even if the
    two drifted in the same direction.

    Every target field here is distinct, which the coupling needs: a name
    appearing twice, once sourced and once not, would be annotated (the
    unsourced entry names it) AND carry a Completeness rule (the sourced entry
    emits one), and the two sets would legitimately differ. That case is a
    mapping-record anomaly, not a coupling failure.
    """
    mappings = [
        _entry("cust_id", "id"),
        _entry("email", "email_addr"),
        _entry("phone", source_field),
    ]
    declared = {m["target_field"] for m in mappings}

    annotated = set(_ca._unsourced_target_field_names(mappings))
    skipped = declared - _completeness_named(_ca._generate_dqdl_rules(mappings))

    assert annotated == skipped == {"phone"}


@pytest.mark.parametrize("source_field", UNUSABLE_SOURCE_FIELDS)
def test_the_coupling_holds_over_both_lists(source_field):
    """The coupling, restated over the domain the two lists actually cover.

    The test above states it over ``{m["target_field"] for m in mappings}``. That
    domain cannot see a field living only in ``unmapped_target_fields``, so once
    the reader was widened the coupling had to be restated rather than repaired —
    the alternative, teaching ``_generate_dqdl_rules`` about the unmapped list, is
    the p37 R2.1/R2.2 defect (a ``Completeness >= 0.95`` rule on a column the
    transform sets to a constant, which quarantines every row).

    Over the UNION of the two lists — which is what the target schema declares
    and therefore what ``schema_compliance`` scores — "annotated ⟺ no
    Completeness rule" still holds, and holds for a better reason: a field from
    list 1 is not in ``mappings``, so it never reaches
    ``_generate_dqdl_rules``' loop and cannot acquire a rule.

    ``fax`` is the list-1-only field, ``phone`` the list-2 unsourced one, so both
    arms of the union are exercised in every case.
    """
    mappings = [
        _entry("cust_id", "id"),
        _entry("email", "email_addr"),
        _entry("phone", source_field),
    ]
    unmapped = [{"field": "fax", "default_value": None, "reason": "no source"}]
    declared = {m["target_field"] for m in mappings} | {"fax"}

    annotated = set(_ca._unsourced_target_field_names(mappings, unmapped))
    skipped = declared - _completeness_named(_ca._generate_dqdl_rules(mappings))

    assert annotated == skipped == {"phone", "fax"}
    # And the ruleset really did stay out of it: `fax` carries no rule because it
    # never entered `mappings`, not because a skip was added for it.
    assert "fax" not in _ca._generate_dqdl_rules(mappings)


def test_the_coupling_holds_when_every_entry_is_unsourced():
    """The boundary case: nothing is sourced, so everything is annotated and
    nothing carries a Completeness rule. RowCount survives — this is not the
    empty-mappings case, which returns ``""``."""
    mappings = [_entry("phone", None), _entry("fax", "  ")]
    declared = {m["target_field"] for m in mappings}
    dqdl = _ca._generate_dqdl_rules(mappings)

    assert set(_ca._unsourced_target_field_names(mappings)) == declared
    assert _completeness_named(dqdl) == set()
    assert "RowCount > 0" in dqdl


def test_the_coupling_holds_when_nothing_is_unsourced():
    """The other boundary: every field sourced, nothing annotated, every field
    carries a Completeness rule."""
    mappings = [_entry("cust_id", "id"), _entry("email", "email_addr")]

    assert _ca._unsourced_target_field_names(mappings) == []
    assert _completeness_named(_ca._generate_dqdl_rules(mappings)) == {"cust_id", "email"}


def test_the_coupling_has_one_boundary_the_two_functions_do_not_share():
    """A blank-but-truthy ``target_field`` is where the two readers part.

    ``_unsourced_target_field_names`` tests ``isinstance(str)`` and
    ``.strip()``, so ``"   "`` names nothing and is skipped silently.
    ``_generate_dqdl_rules`` skips on plain falsiness (``if not target_field``),
    so ``"   "`` reaches ``validate_identifier`` and RAISES — and that ordering
    is load-bearing on purpose (p37 R2.7, the comment at
    ``code_assembly.py``'s skip: "an unsourced field with a hostile name must
    STILL fail code generation naming the identifier. Do not restructure into
    'skip first, validate later'").

    Pinned so wave 2, which adds a loud failure of its own to this same
    function, does not quietly move the raise or convert it to a skip.
    """
    blank = [{"target_field": "   ", "source_field": None}]

    assert _ca._unsourced_target_field_names(blank) == []
    with pytest.raises(IdentifierValidationError):
        _ca._generate_dqdl_rules(blank)


def test_generate_code_hands_both_derivations_the_same_list():
    """The coupling at the CALLER, not just between the two helpers.

    ``generate_code`` computes ``unsourced_target_fields`` and the DQDL ruleset
    from one ``_extract_field_mappings(mapping_result)`` result. Captured at both
    exits — the injected transform generator's kwargs and the Jinja render
    context — so a future refactor that recomputes one of them from a different
    source fails here.
    """
    captured: dict = {}
    real_render = jinja2.Template.render

    def _capture_render(self, *args, **kwargs):
        captured.update(kwargs)
        return real_render(self, *args, **kwargs)

    def _generate_transform(**kwargs):
        captured["unsourced_kwarg"] = kwargs.get("unsourced_target_fields")
        return "df_transformed = df"

    mapping_result = {
        "field_mappings": [
            _entry("cust_id", "id"),
            _entry("phone", None),
        ]
    }
    pipeline_metadata = {
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
    }

    original = jinja2.Template.render
    try:
        jinja2.Template.render = _capture_render
        _ca.generate_code(
            pipeline_metadata=pipeline_metadata,
            mapping_result=mapping_result,
            generate_transform=_generate_transform,
            quarantine_bucket="adi-quarantine-test",
        )
    finally:
        jinja2.Template.render = original

    assert captured["unsourced_kwarg"] == ["phone"]
    assert _completeness_named(captured["dqdl_rules"]) == {"cust_id"}
    assert set(captured["unsourced_kwarg"]) == {"cust_id", "phone"} - _completeness_named(captured["dqdl_rules"])
