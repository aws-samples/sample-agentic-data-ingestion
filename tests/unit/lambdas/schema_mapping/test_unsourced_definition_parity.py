# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization: the two ``_has_usable_source_field`` copies, and the
asymmetry between the two ``unsourced_target_field_names``.

TWO COPIES, NO IMPORT BETWEEN THEM
----------------------------------
``_has_usable_source_field`` exists twice:

* ``create_code/code_assembly.py:138-159`` — feeds the DQDL skip (p37 R2.1/R2.2)
  and the transform prompt's annotation (p37 R3.5 / C12).
* ``schema_mapping/lambda_function.py:1011-1028`` — feeds
  ``unsourced_target_field_names`` (the C10 reader that decides whether a human
  is required) and ``_normalise_mapping_structure`` (the C9 writer that moves the
  entry).

They live in different Lambdas and **cannot import each other**. The
``code_assembly`` docstring states the consequence plainly: "the shared
definition is the docstring in both plus the tests that pin the four unusable
shapes." Those tests did not exist. This file is them, so a test that the two
copies AGREE is the only mechanical link between the two definitions — and the
only thing that will notice if one copy is changed and the other is not.

THE ASYMMETRY, AND WHY IT IS NOT THE SAME KIND OF THING
-------------------------------------------------------
The two near-namesakes are NOT two copies of one function; they read different
inputs and that difference is the branch B defect:

* ``schema_mapping.unsourced_target_field_names`` reads **both**
  ``unmapped_target_fields`` and ``mappings``. ``LGR-9e76249-7`` (**template /
  ratified**) is the record: "``unsourced_target_field_names`` reads **both**
  lists ... so the approval decision, the approval payload and the normaliser
  cannot drift about what 'unsourced' means", and reading only one list "would
  make the caller order-dependent on C9".
* ``code_assembly._unsourced_target_field_names`` reads **both** lists too, as of
  wave 3 of this fix. It used to read only ``mappings``. No ledger record
  governed that; only its own docstring, which argued the omission was safe
  because such a field "carries no rule and the model is not told to produce it
  from anything" — and the target-schema block at ``transform_generation.j2:65-67``
  falsified it, because it tells the model to produce every declared field
  unconditionally. The docstring was rewritten with the reader.

╔═══════════════════════════════════════════════════════════════════════════╗
║ WAVE 3 CONTRACT — SETTLED. THE MARKED ASSERTION WAS INVERTED.             ║
║                                                                           ║
║ ONE test here encoded the branch B defect. Wave 3 inverted it and renamed  ║
║ the marker ``BRANCH_B_DEFECT`` → ``BRANCH_B_FIXED`` (``LGR-60c4985-6`` /   ║
║ ``LGR-9e76249-10``: a deliberate hold is amended in place with its reason, ║
║ so the reversal reads as a reversal). Grep ``BRANCH_B``.                   ║
║                                                                           ║
║   test_the_two_readers_agree_about_one_record_BRANCH_B_FIXED               ║
║     (was test_the_two_readers_disagree_about_one_record_BRANCH_B_DEFECT;   ║
║      now asserts the two outputs are EQUAL)                                ║
║                                                                           ║
║ SURVIVED UNCHANGED:                                                        ║
║  * every parity assertion between the two ``_has_usable_source_field``     ║
║    copies — wave 3 changed which LISTS are read, never what "usable"       ║
║    means, so any parity failure would be a regression;                    ║
║  * every assertion about ``schema_mapping.unsourced_target_field_names``   ║
║    reading both lists (``LGR-9e76249-7``) — that side is the correct one   ║
║    and is what wave 3 made the other side match. ``schema_mapping/         ║
║    lambda_function.py`` was NOT touched, so ``determine_approval_          ║
║    requirement``'s 20% ratio rule and ``avg_confidence`` are unmoved;      ║
║  * the ``required``-first ordering assertions.                              ║
║                                                                           ║
║ ADDED: a parity test for ``_unmapped_target_field_name``, which wave 3     ║
║ copied into ``code_assembly`` — a third cross-Lambda copy, and this file   ║
║ is now the only mechanical link for it too.                                ║
╚═══════════════════════════════════════════════════════════════════════════╝

NOT DUPLICATED HERE
-------------------
CodeGraph reports "no covering tests" for both symbols. That is a false negative
for the ``schema_mapping`` side — these tests already exist and are green, and
they are deliberately not repeated:

* ``test_schema_mapping_approval.py:418-441`` — the five unusable
  ``source_field`` forms, through ``unsourced_target_field_names``.
* ``test_schema_mapping_approval.py:508-522`` — ``required``-first ordering.
* ``test_schema_mapping_approval.py:526-538`` — both ``unmapped_target_fields``
  element shapes, deduped.
* ``test_schema_mapping_approval.py:443-461`` — the reader is total on
  degenerate input.
* ``test_schema_mapping_fallback_provenance.py:441-460, 539-590, 596-624`` —
  ``_normalise_mapping_structure``'s deliberate non-moves, the non-duplication
  that protects the 20% ratio numerator, and idempotence on
  ``generate_simple_mapping``'s output.
* ``test_schema_mapping_normalisation_prop.py`` — Property 3.

What is genuinely absent, and therefore here: direct assertions on the predicate
itself, cross-copy agreement, cross-LIST de-duplication, an empty ``required``
array, and the asymmetry.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

_TESTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
_SHARED_DIR = os.path.join(_TESTS_ROOT, "shared")
_CREATE_CODE_DIR = os.path.join(_TESTS_ROOT, "lambda", "agentic_data_ingestion_create_code")
_SCHEMA_MAPPING_DIR = os.path.join(_TESTS_ROOT, "lambda", "agentic_data_ingestion_schema_mapping")

# `code_assembly` first: it is imported by its own name (not through
# `lambda_function`), owns no AWS clients and reads no environment, so it needs
# neither stubs nor to win the `sys.modules["lambda_function"]` race.
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)
import code_assembly as _ca

# `schema_mapping.lambda_function` second, via the import dance the other files
# in this directory use — the Lambda-layer stubs and the position-0 insert are
# not optional: `require_env` raises at import time for the real `env_utils`
# outside local mode, and other test files leave their own Lambda's module in
# `sys.modules["lambda_function"]`.
sys.modules.setdefault(
    "eventbridge_utils", MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock())

sys.modules.pop("lambda_function", None)
sys.path.insert(0, _SCHEMA_MAPPING_DIR)
try:
    import lambda_function as _sm
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _ensure_correct_lambda_module():
    """Keep ``sys.modules["lambda_function"]`` pointing at schema_mapping's module.

    Other Lambda dirs' test files replace it with their own; nothing in this file
    patches by module name, but the fixture matches the convention every other
    file in this directory follows so a later addition here can.
    """
    sys.modules["lambda_function"] = _sm
    yield


# ---------------------------------------------------------------------------
# The shapes. `_ABSENT` means the key is not present at all.
# `123` is TRUTHY and unusable — the shape that separates the predicate from a
# truthiness test, which is what both docstrings exist to record.
# ---------------------------------------------------------------------------

_ABSENT = object()

UNUSABLE = [
    pytest.param(_ABSENT, id="absent"),
    pytest.param(None, id="None"),
    pytest.param("", id="empty-string"),
    pytest.param("   ", id="whitespace-only"),
    pytest.param("\t\n ", id="whitespace-tab-newline"),
    pytest.param(123, id="non-string-truthy"),
    pytest.param(0, id="non-string-falsy"),
    pytest.param(["mobile"], id="non-string-list"),
    pytest.param({"name": "mobile"}, id="non-string-dict"),
    pytest.param(True, id="non-string-bool"),
]

USABLE = ["mobile", "  mobile  ", "0", "a"]

NON_DICT_ENTRIES = [None, 3, "phone", ["phone"], (), object()]

_BOTH_COPIES = [
    pytest.param(_ca._has_usable_source_field, id="create_code.code_assembly"),
    pytest.param(_sm._has_usable_source_field, id="schema_mapping.lambda_function"),
]


def _entry(target_field="phone", source_field=_ABSENT) -> dict:
    entry: dict = {"target_field": target_field, "confidence": 0.6}
    if source_field is not _ABSENT:
        entry["source_field"] = source_field
    return entry


# ===========================================================================
# `_has_usable_source_field` — pinned for BOTH copies (item 1)
# ===========================================================================


@pytest.mark.parametrize("predicate", _BOTH_COPIES)
@pytest.mark.parametrize("source_field", UNUSABLE)
def test_both_copies_reject_every_unusable_shape(predicate, source_field):
    """``None``, ``""``, ``"   "`` and a non-string are unusable — in both copies.

    Both docstrings name exactly this list and both give the same reason: a
    mapping record is parsed JSON, so any of these can arrive.
    """
    assert predicate(_entry(source_field=source_field)) is False


@pytest.mark.parametrize("predicate", _BOTH_COPIES)
@pytest.mark.parametrize("source_field", USABLE)
def test_both_copies_accept_a_named_source_column(predicate, source_field):
    """A string that survives ``.strip()`` names a column, padding included."""
    assert predicate(_entry(source_field=source_field)) is True


@pytest.mark.parametrize("predicate", _BOTH_COPIES)
@pytest.mark.parametrize("mapping", NON_DICT_ENTRIES)
def test_both_copies_are_total_on_a_non_dict_entry(predicate, mapping):
    """A ``mappings`` element can be a bare string — model output parsed as
    returned. Neither copy raises."""
    assert predicate(mapping) is False


@pytest.mark.parametrize("predicate", _BOTH_COPIES)
def test_neither_copy_is_a_truthiness_test(predicate):
    """The distinction both docstrings exist to record, isolated.

    ``123`` and ``"   "`` are truthy and unusable; ``"0"`` is falsy-looking and
    usable. A ``bool(source_field)`` implementation gets all three wrong.
    """
    assert bool(123) is True and predicate(_entry(source_field=123)) is False
    assert bool("   ") is True and predicate(_entry(source_field="   ")) is False
    assert predicate(_entry(source_field="0")) is True


# --- the only mechanical link between the two definitions -------------------


@pytest.mark.parametrize("source_field", [*[p.values[0] for p in UNUSABLE], *USABLE])
def test_the_two_copies_agree_on_every_source_field_shape(source_field):
    """The two copies answer identically for every shape.

    ⚠️ **This test is the shared definition.** ``code_assembly``'s docstring says
    so: "The two live in different Lambdas and cannot import each other; the
    shared definition is the docstring in both plus the tests that pin the four
    unusable shapes." No import, no type, and no CI step other than this couples
    them — so if one copy is edited and the other is not, this is what fails.

    Compared as answers rather than each against a literal: a change that broke
    both copies in the same direction would pass the per-copy tests above and
    fail nothing here, which is why those tests exist as well as this one.
    """
    entry = _entry(source_field=source_field)
    assert _ca._has_usable_source_field(entry) is _sm._has_usable_source_field(entry)


@pytest.mark.parametrize("mapping", NON_DICT_ENTRIES)
def test_the_two_copies_agree_on_non_dict_entries(mapping):
    """Agreement extends to the not-a-dict guard, which both open with."""
    assert _ca._has_usable_source_field(mapping) is _sm._has_usable_source_field(mapping)


# ===========================================================================
# `schema_mapping.unsourced_target_field_names` — reads BOTH lists
# (LGR-9e76249-7, template / ratified). MUST SURVIVE.
# ===========================================================================


def test_a_field_only_in_unmapped_target_fields_is_named():
    """List 1 alone is enough to be named — the post-C9 placement.

    ``_normalise_mapping_structure`` MOVES a source-less entry here, so this is
    where an unsourced field normally lives once C9 has run.
    """
    record = {
        "mappings": [_entry("email", "email_addr")],
        "unmapped_target_fields": [{"field": "phone", "default_value": None, "reason": "no source"}],
    }
    assert _sm.unsourced_target_field_names(record) == ["phone"]


def test_a_field_only_in_mappings_without_a_source_is_named():
    """List 2 alone is enough — the pre-C9 placement, and the one that stays
    reachable forever: ``LGR-9e76249-7`` records that C9 "normalises at
    generation time and cannot reach a record already written"."""
    record = {
        "mappings": [_entry("email", "email_addr"), _entry("phone", None)],
        "unmapped_target_fields": [],
    }
    assert _sm.unsourced_target_field_names(record) == ["phone"]


def test_a_name_in_both_lists_is_named_once():
    """Cross-LIST de-duplication.

    ``test_schema_mapping_approval.py:526`` covers dedup WITHIN
    ``unmapped_target_fields``. This is the other axis: the same field named by
    both lists at once — reachable on a record written before C9 and then
    re-normalised, or one an operator edited. Naming it twice would double-count
    it for any caller that measures the list's length.
    """
    record = {
        "mappings": [_entry("email", "email_addr"), _entry("phone", None)],
        "unmapped_target_fields": [{"field": "phone"}],
    }
    assert _sm.unsourced_target_field_names(record) == ["phone"]


def test_unmapped_target_fields_is_named_before_mappings():
    """The declared read order: list 1 then list 2, each order-preserving.

    Not an arbitrary detail — the output feeds an approval payload a human reads,
    so a stable order keeps two views of one record comparable.
    """
    record = {
        "mappings": [_entry("from_mappings_a", None), _entry("from_mappings_b", "")],
        "unmapped_target_fields": ["from_unmapped_a", {"field": "from_unmapped_b"}],
    }
    assert _sm.unsourced_target_field_names(record) == [
        "from_unmapped_a",
        "from_unmapped_b",
        "from_mappings_a",
        "from_mappings_b",
    ]


# --- `required`-first ordering, including the empty and absent arrays --------


def _record_with(*names) -> dict:
    return {"mappings": [], "unmapped_source_fields": [], "unmapped_target_fields": [{"field": n} for n in names]}


def test_required_fields_are_named_first_and_order_is_preserved_within_each_group():
    """``required`` first, then the rest; declared order kept inside each group.

    ``test_schema_mapping_approval.py:508`` covers the two-required case; this
    adds that the non-required tail keeps its own order rather than being sorted.
    """
    record = _record_with("nickname", "phone", "middle_name", "email")
    schema = {"type": "object", "properties": {}, "required": ["email", "phone"]}

    assert _sm.unsourced_target_field_names(record, schema) == ["phone", "email", "nickname", "middle_name"]


@pytest.mark.parametrize(
    ("label", "schema"),
    [
        ("required absent", {"type": "object", "properties": {}}),
        ("required empty list", {"type": "object", "properties": {}, "required": []}),
        ("required None", {"type": "object", "properties": {}, "required": None}),
        ("schema empty", {}),
        ("schema None", None),
    ],
)
def test_no_required_array_leaves_the_order_untouched(label, schema):
    """With nothing required, the two-group partition is a no-op.

    ``required`` is read as ``target_schema.get("required", []) or []`` — the
    ``or []`` is what makes an explicit ``None`` behave like an absent key. The
    empty-list and ``None`` cases are the ones the existing coverage misses, and
    ``LGR-9e76249-7``'s own ``revisit_when`` names them: "live target schemas
    turn out never to populate ``required``". If that revisit happens, the
    forcing clause widens and THIS ordering is what it widens over — so the
    ordering has to be pinned for the no-``required`` case too.
    """
    record = _record_with("nickname", "phone", "email")

    assert _sm.unsourced_target_field_names(record, schema) == ["nickname", "phone", "email"], label


def test_a_required_field_that_is_not_unsourced_is_not_invented():
    """``required`` reorders the names; it never adds one.

    Every unsourced field is named and only unsourced fields are named — the
    ``required`` array is a sort key, not a source of names.
    """
    record = _record_with("phone")
    schema = {"type": "object", "properties": {}, "required": ["email", "phone", "cust_id"]}

    assert _sm.unsourced_target_field_names(record, schema) == ["phone"]


# ===========================================================================
# THE ASYMMETRY — one record, two answers (item 2)
# ===========================================================================


def test_the_two_readers_agree_when_the_entry_is_in_mappings():
    """Where both readers can see the entry, they give the same answer.

    The pre-C9 ``phone`` shape. This is the half that is NOT broken, and pinning
    it is what makes the next test's disagreement attributable to the list being
    read rather than to the predicate.
    """
    mappings = [_entry("email", "email_addr"), _entry("phone", None)]
    record = {"mappings": mappings, "unmapped_target_fields": []}

    assert _sm.unsourced_target_field_names(record) == ["phone"]
    assert _ca._unsourced_target_field_names(mappings) == ["phone"]


def test_the_two_readers_agree_about_one_record_BRANCH_B_FIXED():
    """One record, one answer. INVERTED by wave 3.

    ⚠️ **WAS ``test_the_two_readers_disagree_about_one_record_BRANCH_B_DEFECT``**,
    which asserted ``from_code_assembly == []`` and
    ``set(from_code_assembly) < set(from_schema_mapping)``.

    The record below is exactly what ``_normalise_mapping_structure`` produces.
    ``schema_mapping`` named ``phone`` and forced a human at approval;
    ``code_assembly`` did not name it, so the transform prompt rendered it as a
    mandatory column with no ``— NO SOURCE`` annotation and the model invented a
    source. Same record, same concept, two answers — and the approval a human
    gave was about the first answer. They now give the same answer, as LISTS and
    not merely as sets: the two readers apply the same element rule to the same
    two lists in the same order.

    ``code_assembly``'s reader takes the two lists rather than the record, which
    is why the call below destructures it. That is the seam: it is what makes
    ``generate_code`` responsible for resolving both lists off one
    ``mapping_result`` (via ``_extract_field_mappings`` and
    ``_extract_unmapped_target_fields``), and this reader responsible only for the
    naming rule.
    """
    record = {
        "mappings": [_entry("email", "email_addr")],
        "unmapped_target_fields": [
            {"field": "phone", "default_value": None, "reason": _sm.UNSOURCED_MAPPING_REASON}
        ],
    }

    from_schema_mapping = _sm.unsourced_target_field_names(record)
    from_code_assembly = _ca._unsourced_target_field_names(record["mappings"], record["unmapped_target_fields"])

    assert from_schema_mapping == ["phone"]
    assert from_code_assembly == ["phone"]
    assert from_schema_mapping == from_code_assembly
    # The relation the property form in test_unsourced_asymmetry_prop.py states,
    # now an equality rather than a proper subset.
    assert set(from_code_assembly) == set(from_schema_mapping)


def test_the_two_readers_agree_on_the_read_order_of_the_two_lists():
    """Both name ``unmapped_target_fields`` first, then ``mappings``.

    Asserted as list equality over a record where both lists contribute, so the
    agreement covers order and not only membership. ``schema_mapping``'s
    ``required`` partition is a later, schema-gated permutation of this list —
    ``code_assembly`` takes no schema and so produces the pre-partition order,
    which is what the no-schema call below returns.
    """
    record = {
        "mappings": [_entry("from_mappings_a", None), _entry("from_mappings_b", "")],
        "unmapped_target_fields": ["from_unmapped_a", {"field": "from_unmapped_b"}],
    }

    assert _sm.unsourced_target_field_names(record) == _ca._unsourced_target_field_names(
        record["mappings"], record["unmapped_target_fields"]
    )


# --- the third cross-Lambda copy: `_unmapped_target_field_name` --------------

_BOTH_NAME_READERS = [
    pytest.param(_ca._unmapped_target_field_name, id="create_code.code_assembly"),
    pytest.param(_sm._unmapped_target_field_name, id="schema_mapping.lambda_function"),
]

UNMAPPED_ENTRIES = [
    pytest.param({"field": "phone"}, "phone", id="dict"),
    pytest.param({"field": "  phone  "}, "phone", id="dict-padded"),
    pytest.param({"field": "phone", "default_value": None, "reason": "no source"}, "phone", id="dict-full"),
    pytest.param("phone", "phone", id="bare-string"),
    pytest.param("  phone  ", "phone", id="bare-string-padded"),
    pytest.param({"field": ""}, "", id="dict-empty-field"),
    pytest.param({"field": "   "}, "", id="dict-blank-field"),
    pytest.param({"field": 3}, "", id="dict-non-string-field"),
    pytest.param({"field": None}, "", id="dict-none-field"),
    pytest.param({"no_field_key": "phone"}, "", id="dict-no-field-key"),
    pytest.param({}, "", id="empty-dict"),
    pytest.param("", "", id="empty-string"),
    pytest.param("   ", "", id="blank-string"),
    pytest.param(None, "", id="None"),
    pytest.param(7, "", id="int"),
    pytest.param(["phone"], "", id="list"),
    pytest.param((), "", id="tuple"),
]


@pytest.mark.parametrize("reader", _BOTH_NAME_READERS)
@pytest.mark.parametrize(("entry", "expected"), UNMAPPED_ENTRIES)
def test_both_element_name_readers_answer_identically(reader, expected, entry):
    """Wave 3 copied ``_unmapped_target_field_name`` into ``code_assembly``.

    Two producers write ``unmapped_target_fields`` and disagree on the element
    shape — a dict from ``generate_simple_mapping``, a bare string because a
    Bedrock response is model output parsed as returned. Both copies read both,
    strip, and answer ``""`` for anything that names nothing.

    ⚠️ Like ``test_the_two_copies_agree_on_every_source_field_shape``, this is the
    shared definition: no import and no type couples the two, so this is what
    fails if one is edited and the other is not.
    """
    assert reader(entry) == expected


@pytest.mark.parametrize(("entry", "expected"), UNMAPPED_ENTRIES)
def test_the_two_element_name_readers_agree(entry, expected):
    """The same agreement asserted as an identity between the two answers.

    Compared to each other as well as to a literal, for the reason the
    ``_has_usable_source_field`` pair records: a change breaking both copies in
    the same direction would pass the per-copy test and fail nothing here.
    """
    assert _ca._unmapped_target_field_name(entry) == _sm._unmapped_target_field_name(entry)
