# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property: the two unsourced readers stand in a superset relation.

The asymmetry between ``schema_mapping.unsourced_target_field_names`` and
``code_assembly._unsourced_target_field_names``, stated **once** over generated
records instead of case by case. Three properties, and which of them wave 3
changes is the whole point of writing them this way.

  P1  ``set(code_assembly) == set(schema_mapping)`` — for ANY mapping record.
      ⚠️ **WAVE 3 TURNED ``⊆`` INTO ``==``.** It was a proper subset whenever
      ``unmapped_target_fields`` contributed a name the ``mappings`` list did
      not, which was the branch B defect. Grep ``BRANCH_B``.

  P2  When ``unmapped_target_fields`` contributes no names, the two agree
      **exactly** — as lists, order included. MUST SURVIVE. This is what
      localises the defect: the two readers apply the same predicate to
      ``mappings``, so ``unmapped_target_fields`` is the only source of the
      disagreement. It is also what makes P1 a statement about inputs rather
      than about implementations.

  P3  The names ``schema_mapping`` adds are exactly names
      ``unmapped_target_fields`` supplies — nothing is invented. SURVIVED
      UNCHANGED. Its gap is now empty on every drawn record, which is the fix;
      a gap containing a name neither list supplies would be a fabrication, and
      this is still what would catch it.

The relation, not the cases, is what wave 3 edited: ``⊆`` became ``==``. A change
that makes ``code_assembly`` name MORE than ``schema_mapping`` fails P1 in the
other direction and is equally wrong — which is what an equality buys over the
one-directional subset the defect permitted.

``code_assembly``'s reader takes the two lists rather than the record, so the
calls below destructure it. That is the seam: resolving both lists off one
``mapping_result`` is ``generate_code``'s job (``_extract_field_mappings`` and
``_extract_unmapped_target_fields``), and the reader's job is only the naming
rule. P1 is stated over the same record for both readers either way.

WRITTEN FROM THE RULE, NOT FROM THE HELPERS
-------------------------------------------
``_unmapped_contributed`` below reimplements the string-or-dict element rule
rather than calling ``_unmapped_target_field_name``, so a change to that helper
cannot drag this test's expectation along with it — the convention
``test_schema_mapping_normalisation_prop.py`` records for the same reason.

**Validates: p37 R3.2 / design C10, and the branch B finding of this fix.**
"""

import os
import sys
from unittest.mock import MagicMock

from hypothesis import given, settings
from hypothesis import strategies as st

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

_TESTS_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, os.path.join(_TESTS_ROOT, "shared"))
sys.path.insert(0, os.path.join(_TESTS_ROOT, "lambda", "agentic_data_ingestion_create_code"))
import code_assembly as _ca

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
sys.path.insert(0, os.path.join(_TESTS_ROOT, "lambda", "agentic_data_ingestion_schema_mapping"))
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

# ---------------------------------------------------------------------------
# Generators — constrained to the input space a real mapping record occupies.
#
# A mapping record is model output parsed as returned, round-tripped through
# DynamoDB, so every shape below is reachable: a bare-string `mappings` element,
# a dict with no `target_field`, a non-string `source_field`. The field-name pool
# is small and shared between the two lists on purpose, so cross-list collisions
# (the de-duplication case) are drawn often rather than rarely.
# ---------------------------------------------------------------------------

_FIELD_NAMES = st.sampled_from(["cust_id", "email", "phone", "fax", "nickname", "middle_name"])

_UNUSABLE_SOURCE = st.sampled_from([None, "", "   ", "\t", 123, 0, True])
_USABLE_SOURCE = st.sampled_from(["mobile", "  mobile  ", "email_addr", "0"])

# Target-field values that name NOTHING — every one answers "" in both readers.
_UNNAMED_TARGET = st.sampled_from([None, "", "   ", 7, ["phone"]])


@st.composite
def _mapping_entry(draw: st.DrawFn):
    """One ``mappings`` element, including the shapes that are not dicts."""
    kind = draw(st.sampled_from(["sourced", "unsourced", "no-target", "bare-string", "not-a-dict"]))
    if kind == "bare-string":
        return draw(_FIELD_NAMES)
    if kind == "not-a-dict":
        return draw(st.sampled_from([None, 3, (), ["phone"]]))

    entry: dict = {"confidence": draw(st.floats(0.0, 1.0))}
    if kind == "no-target":
        entry["target_field"] = draw(_UNNAMED_TARGET)
    else:
        # Padding is drawn so the stripping half of both readers is exercised.
        entry["target_field"] = draw(st.sampled_from(["", "  "])) + draw(_FIELD_NAMES)

    if kind == "sourced":
        entry["source_field"] = draw(_USABLE_SOURCE)
    elif draw(st.booleans()):
        entry["source_field"] = draw(_UNUSABLE_SOURCE)
    # else: the key is absent entirely, which is one of the four unusable shapes.
    return entry


@st.composite
def _unmapped_entry(draw: st.DrawFn):
    """One ``unmapped_target_fields`` element — both producers' shapes, plus junk."""
    kind = draw(st.sampled_from(["dict", "bare-string", "junk"]))
    if kind == "dict":
        return {"field": draw(_FIELD_NAMES), "default_value": None, "reason": "no source"}
    if kind == "bare-string":
        return draw(_FIELD_NAMES)
    return draw(st.sampled_from([None, 7, ["phone"], {"field": 3}, {"no_field_key": "phone"}, {}]))


@st.composite
def _mapping_record(draw: st.DrawFn):
    return {
        "mappings": draw(st.lists(_mapping_entry(), min_size=0, max_size=6)),
        "unmapped_source_fields": [],
        "unmapped_target_fields": draw(st.lists(_unmapped_entry(), min_size=0, max_size=4)),
        "generated_by": "us.test-model",
    }


def _unmapped_contributed(record: dict) -> set[str]:
    """Names ``unmapped_target_fields`` supplies, per C10's stated element rule.

    Written from the rule ("a bare string, or a dict's ``field`` when that value
    is a string"), not by calling ``_unmapped_target_field_name``.
    """
    names: set[str] = set()
    for entry in record.get("unmapped_target_fields") or []:
        if isinstance(entry, str):
            if entry.strip():
                names.add(entry.strip())
        elif isinstance(entry, dict):
            value = entry.get("field", "")
            if isinstance(value, str) and value.strip():
                names.add(value.strip())
    return names


# ===========================================================================
# P1 — the relation. WAVE 3 INVERTED `<=` TO `==`.
# ===========================================================================


@settings(max_examples=400, deadline=None)
@given(record=_mapping_record())
def test_the_two_readers_name_the_same_set_BRANCH_B_FIXED(record):
    """The two readers name the same set, for ANY mapping record. INVERTED.

    ⚠️ **WAS ``test_schema_mapping_names_a_superset_of_code_assembly_BRANCH_B_DEFECT``**,
    which asserted ``set(from_ca) <= set(from_sm)`` and permitted a PROPER
    subset. That possibility was the branch B defect; the ``==`` here removes it
    and catches the other direction too — ``code_assembly`` naming something
    ``schema_mapping`` does not would be equally wrong.

    Stated over the same record for both readers. ``code_assembly`` is handed the
    record's two lists; before wave 3 its signature could take only the first,
    which was the defect in one line.

    Asserted as LISTS as well as sets: the two apply the same element rules to
    the same two lists in the same declared order, so a difference in order would
    be a real divergence rather than a formatting detail.
    ``schema_mapping``'s ``required`` partition is not exercised here — no schema
    is passed — and ``test_the_required_partition_reorders_but_never_changes_the_
    named_set`` covers it separately.

    **Validates: p37 R3.2 / design C10, and the branch B fix**
    """
    from_sm = _sm.unsourced_target_field_names(record)
    from_ca = _ca._unsourced_target_field_names(record.get("mappings"), record.get("unmapped_target_fields"))

    assert set(from_ca) == set(from_sm), (
        f"the two readers disagree: code_assembly-only "
        f"{sorted(set(from_ca) - set(from_sm))}, schema_mapping-only "
        f"{sorted(set(from_sm) - set(from_ca))}\nrecord={record}"
    )
    assert from_ca == from_sm, f"same set, different order\nrecord={record}"
    # Both are de-duplicated, so the list length is the set size — asserted here
    # so a regression in either reader's dedup cannot hide behind the set
    # comparison above.
    assert len(from_sm) == len(set(from_sm))
    assert len(from_ca) == len(set(from_ca))


@settings(max_examples=400, deadline=None)
@given(record=_mapping_record())
def test_the_gap_is_exactly_what_unmapped_target_fields_supplies(record):
    """P3: every name ``schema_mapping`` adds comes from ``unmapped_target_fields``.

    MUST SURVIVE WAVE 3. The gap shrinking to empty is the fix; the gap
    containing a name neither list supplies would be a fabrication, and this is
    what would catch it.

    **Validates: p37 R3.2 / design C10**
    """
    from_sm = set(_sm.unsourced_target_field_names(record))
    from_ca = set(_ca._unsourced_target_field_names(record.get("mappings")))

    assert from_sm - from_ca <= _unmapped_contributed(record), (
        f"schema_mapping named something no list supplies: "
        f"{sorted((from_sm - from_ca) - _unmapped_contributed(record))}\nrecord={record}"
    )


@settings(max_examples=400, deadline=None)
@given(record=_mapping_record())
def test_the_widened_reader_closes_the_gap_without_widening_the_set(record):
    """P3, the other half: the gap is EMPTY once ``code_assembly`` reads both lists.

    P3 above is deliberately left calling ``code_assembly`` with ``mappings``
    alone — that projection is what makes it a live guard, because it measures a
    non-empty gap and checks every name in it against the rule. This states the
    complement: over the record's BOTH lists the gap closes entirely, and closes
    without either reader inventing a name.

    Both directions are asserted, so a fix that closed the gap by naming
    something extra — a ``default_value``, a ``reason`` string, an empty name from
    a junk element — fails here rather than passing as "no gap".

    **Validates: p37 R3.2 / design C10, and the branch B fix**
    """
    from_sm = set(_sm.unsourced_target_field_names(record))
    from_ca = set(_ca._unsourced_target_field_names(record.get("mappings"), record.get("unmapped_target_fields")))

    assert from_sm - from_ca == set(), f"schema_mapping still names more\nrecord={record}"
    assert from_ca - from_sm == set(), f"code_assembly names something extra\nrecord={record}"

    # And every name either reader produced is one of the two lists' own — no
    # name is manufactured from an element that names nothing.
    mappings_only = set(_ca._unsourced_target_field_names(record.get("mappings")))
    assert from_ca <= mappings_only | _unmapped_contributed(record), (
        f"a name neither list supplies: "
        f"{sorted(from_ca - (mappings_only | _unmapped_contributed(record)))}\nrecord={record}"
    )


# ===========================================================================
# P2 — where the two agree exactly. MUST SURVIVE.
# ===========================================================================


@settings(max_examples=400, deadline=None)
@given(record=_mapping_record())
def test_with_no_names_from_the_unmapped_list_the_two_readers_are_identical(record):
    """P2: strip ``unmapped_target_fields`` of names and the two agree as LISTS.

    MUST SURVIVE WAVE 3 — order included, not just membership.

    This is what makes P1 a statement about the INPUT rather than about two
    implementations: over ``mappings`` alone the two readers are the same
    function, applying the same ``_has_usable_source_field`` and the same
    target-field rule in the same order. So the disagreement P1 permits comes
    from exactly one place.

    Asserted by emptying the list rather than by filtering the drawn record, so
    the ``mappings`` half is the full generated variety.

    **Validates: p37 R3.2 / design C10**
    """
    mappings_only = {**record, "unmapped_target_fields": []}

    from_sm = _sm.unsourced_target_field_names(mappings_only)
    from_ca = _ca._unsourced_target_field_names(mappings_only["mappings"])

    assert from_sm == from_ca, f"the readers disagree over `mappings` alone\nrecord={mappings_only}"


@settings(max_examples=200, deadline=None)
@given(record=_mapping_record(), required=st.lists(_FIELD_NAMES, max_size=3, unique=True))
def test_the_required_partition_reorders_but_never_changes_the_named_set(record, required):
    """``required`` is a sort key, never a source or a filter of names.

    ``LGR-9e76249-7`` (**template / ratified**): "All unsourced fields are named;
    only the *forcing* clause in ``determine_approval_requirement`` is
    ``required``-gated." So passing a schema must permute the list and nothing
    else, and every ``required`` name in the output must have been there without
    the schema.

    MUST SURVIVE. Its ``revisit_when`` names widening the forcing clause to this
    whole list, which makes the set's stability under ``required`` the property
    that widening would rest on.

    **Validates: p37 R3.2 / design C10**
    """
    schema = {"type": "object", "properties": {}, "required": required}

    without = _sm.unsourced_target_field_names(record)
    with_schema = _sm.unsourced_target_field_names(record, schema)

    assert set(with_schema) == set(without)
    assert len(with_schema) == len(without)
    # And the partition really is required-first.
    heads = [n for n in with_schema if n in required]
    tails = [n for n in with_schema if n not in required]
    assert with_schema == heads + tails
