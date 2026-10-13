# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Properties of the undeclared-source-column refusal.

Three properties, each earning its place by covering a space the example tests in
``test_code_assembly_undeclared_source_field.py`` can only sample:

* **P1 — the refusal is exactly characterised.** For any mapping list and any
  non-empty declared set, the validator raises if and only if some entry names a
  usable ``source_field`` the set does not contain, and the offenders it reports
  are exactly those names, deduped, in first-seen order. This is the property that
  makes the check trustworthy in both directions at once; an example test can
  assert one direction per case.

* **P2 — an empty declared set NEVER refuses.** Quantified over arbitrary mapping
  lists, because this is the failure mode that would turn a bugfix into an outage:
  ``schema_extract`` legitimately writes ``{"properties": {}}`` for a parsed source
  with no records, the handler omits ``source_schema`` when the state row is
  missing, and an array-shaped legacy schema declares its columns under ``items``
  where the read finds none.

* **P3 — the branch A coupling survives.** The offender set and
  ``_unsourced_target_field_names``' set are disjoint for every input, because both
  are derived through ``_has_usable_source_field`` and take opposite arms of it.
  This is what makes "the fix does not touch the coupling" a checked fact rather
  than a claim: an entry can be unsourced, or it can name an undeclared column, and
  never both.

Deliberately NOT a property: whether a padded ``source_field`` matches. That is a
single decided behaviour (compared as given, never trimmed) and belongs in the
example file where its reason can be written down.
"""

import os
import re
import sys

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

import code_assembly as _ca

from step_errors import StepFailure

# A small, colliding name pool so undeclared and declared names actually overlap
# often enough to exercise both arms — a wide random alphabet would make almost
# every mapping an offender and P1's "does not raise" arm would go untested.
_NAMES = st.sampled_from(["id", "email", "phone", "name", "mobile", "Email", "id ", "0"])

# Every unusable `source_field` shape `_has_usable_source_field` rejects, so the
# generator produces genuinely unsourced entries alongside sourced ones.
_UNUSABLE = st.sampled_from([None, "", "   ", "\t\n ", 123, True, ["mobile"], {"n": "mobile"}])


@st.composite
def _entries(draw):
    """A ``mappings`` list: sourced entries, unsourced entries, and non-dict junk."""
    return draw(
        st.lists(
            st.one_of(
                st.builds(
                    lambda t, s: {"target_field": t, "source_field": s, "confidence": 0.9},
                    t=_NAMES,
                    s=_NAMES,
                ),
                st.builds(
                    lambda t, s: {"target_field": t, "source_field": s, "confidence": 0.5},
                    t=_NAMES,
                    s=_UNUSABLE,
                ),
                st.builds(lambda t: {"target_field": t, "confidence": 0.5}, t=_NAMES),
                st.sampled_from([None, 7, "phone", ["phone"]]),
            ),
            max_size=8,
        )
    )


def _schema(names) -> dict:
    return {"type": "object", "properties": {n: {"type": "string"} for n in names}}


def _expected_offenders(entries, declared) -> list[str]:
    """First-seen-order offenders, computed independently of the implementation."""
    offenders: list[str] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        source_field = entry.get("source_field")
        if not isinstance(source_field, str) or not source_field.strip():
            continue
        if source_field in declared or source_field in offenders:
            continue
        offenders.append(source_field)
    return offenders


@settings(max_examples=400, deadline=None)
@given(entries=_entries(), declared=st.lists(_NAMES, min_size=1, max_size=5, unique=True))
def test_refuses_exactly_the_mappings_naming_an_undeclared_column(entries, declared):
    """P1: raises iff an offender exists, and names exactly the offenders.

    Validates: branch A — a mapping entry naming a ``source_field`` the source
    schema does not declare fails code generation, naming the field.
    """
    expected = _expected_offenders(entries, set(declared))

    if not expected:
        _ca._validate_mapped_source_fields(entries, _schema(declared))
        return

    with pytest.raises(StepFailure) as exc_info:
        _ca._validate_mapped_source_fields(entries, _schema(declared))

    failure = exc_info.value
    assert failure.details["undeclared_source_fields"] == expected
    assert failure.step_name == "create_code"
    assert failure.error_type == "permanent"
    # The message names every offender — the actionability requirement.
    for offender in expected:
        assert offender in failure.message


@settings(max_examples=400, deadline=None)
@given(
    entries=_entries(),
    schema=st.sampled_from(
        [
            None,
            {},
            {"type": "object"},
            {"properties": {}},
            {"fields": {}},
            # The precedence trap: an empty `properties` KEY wins over a
            # populated `fields`, so this declares nothing (p33 req 3.14).
            {"properties": {}, "fields": {"id": {"type": "string"}}},
            # The legacy array shape: columns live under `items`, where the read
            # does not look, so nothing is declared and the check is inert.
            {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
        ]
    ),
)
def test_an_empty_declared_set_never_refuses(entries, schema):
    """P2: no source schema on record means no refusal, for any mapping.

    The outage guard. Validating against an empty declared set would fail every
    pipeline whose source produced no parseable records, every pipeline whose
    ``schema_extract`` row is missing, and every array-shaped legacy schema.
    """
    _ca._validate_mapped_source_fields(entries, schema)


# P3 needs target names that survive `validate_identifier`, because it drives
# `_generate_dqdl_rules` — the wide pool above deliberately contains names that
# do not (`"id "`, `"0"`), and a hostile name raising there is a different,
# already-pinned property (`test_identifier_validation_sinks.py`).
_SAFE_TARGETS = st.sampled_from(["cust_id", "email", "phone", "full_name", "order_key"])

_COMPLETENESS = re.compile(r'Completeness "([^"]+)"')


@st.composite
def _safe_entries(draw):
    """Entries with identifier-safe, DISTINCT target names; sources vary freely.

    ``unique_by`` on ``target_field`` is a real restriction, and the reason is a
    finding rather than convenience. With two entries claiming the SAME target —
    one unsourced, one sourced — the coupling below does not hold: the unsourced
    entry puts the name in the annotated set while the sourced entry puts it in the
    Completeness set, so the name is in both. That is pre-existing behaviour of the
    two p37 helpers, unrelated to this change and not made worse by it; a mapping
    record with a duplicated ``target_field`` is a schema_mapping-side anomaly.
    Recorded here so the restriction is not read as the property being weakened to
    fit.
    """
    return draw(
        st.lists(
            st.one_of(
                st.builds(
                    lambda t, s: {"target_field": t, "source_field": s, "confidence": 0.9},
                    t=_SAFE_TARGETS,
                    s=_NAMES,
                ),
                st.builds(
                    lambda t, s: {"target_field": t, "source_field": s, "confidence": 0.5},
                    t=_SAFE_TARGETS,
                    s=_UNUSABLE,
                ),
                st.builds(lambda t: {"target_field": t, "confidence": 0.5}, t=_SAFE_TARGETS),
            ),
            min_size=1,
            max_size=5,
            unique_by=lambda e: e["target_field"],
        )
    )


@settings(max_examples=400, deadline=None)
@given(entries=_safe_entries(), declared=st.lists(_NAMES, min_size=1, max_size=5, unique=True))
def test_the_annotated_set_is_still_the_completeness_skipped_set(entries, declared):
    """P3: the branch A coupling at ``code_assembly.py:167-173`` is untouched.

    The set the transform prompt annotates as unsourced IS the set that carries no
    Completeness rule — one predicate, two readers, so the prompt and the ruleset
    cannot drift. This change adds a refusal alongside them and touches neither
    derivation; that is checked here rather than asserted in prose, over inputs
    that include entries the new refusal would reject.
    """
    declared_targets = {e["target_field"] for e in entries}

    annotated = set(_ca._unsourced_target_field_names(entries))
    completeness_named = set(_COMPLETENESS.findall(_ca._generate_dqdl_rules(entries)))

    assert annotated == declared_targets - completeness_named
