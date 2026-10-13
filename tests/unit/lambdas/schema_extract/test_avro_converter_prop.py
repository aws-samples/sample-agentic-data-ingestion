# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property tests for the Avro_Converter (p46 task 3.2).

**Validates: Requirements 8.6**

Covers two of the spec's correctness properties:

* **Property 1 — totality.** Every token in the Avro type vocabulary (8
  primitives, 6 complex, 4 logical) and any novel token maps to a valid JSON
  Schema type, and nothing raises. Mirrors ``arrow_schema_to_json_schema``'s
  ``string`` fallthrough (``lambda_function.py:388-389``).
* **Property 4 — union resolution agrees across both resolvers.** The Avro
  converter's union policy and ``infer_schema_from_records``'s conflict
  resolution (``:171-181``) answer the same question, so they must give the same
  answer.

**Neither resolver is its own oracle.** ``LGR-28b4d75-5`` (template / ratified):
*"a test written from the producer's output cannot detect the producer drifting,
because the output is its own oracle."* So no expected-answer table appears here.
Instead:

* "valid JSON Schema type" is the seven type keywords the **JSON Schema
  specification** defines, restated from the spec rather than read out of
  ``_AVRO_TYPE_MAP`` — a validity check sourced from the converter's own table
  would pass for any table, including a wrong one.
* The Avro type vocabulary is restated from **requirements R8.1–R8.3**, which
  enumerate it, rather than derived from ``_AVRO_TYPE_MAP`` — deriving it would
  make a token silently dropped from the map invisible.
* Property 4 **runs both resolvers and compares them**. The existing resolver is
  reached the only way it can be, through data: a witness Python value per JSON
  Schema type, chosen from the JSON data model, fed to
  ``infer_schema_from_records``. The witnesses encode neither resolver's policy.

Exhaustive where the space is closable (the 14-token vocabulary; the 127
non-empty subsets of the seven JSON Schema types) and generative where it is not
(novel tokens, arbitrary type shapes, unions of unbounded length).

Imports the Lambda module by ``sys.path`` insert as ``lf``, the pattern
``test_streaming_schema.py`` uses — at module level rather than through a
fixture, because hypothesis rejects function-scoped fixtures. ``shared/`` is put
on the path too so ``step_errors`` / ``step_names`` resolve: this file collects
first in the directory, so it cannot rely on a sibling having done it. Region is
left to ``tests/unit/conftest.py``, which pins it by assignment
(``LGR-3e45e65-1``).
"""

import os
import sys
from decimal import Decimal

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

_TESTS_TO_PROJECT = (os.path.dirname(__file__), "..", "..", "..", "..")

# ``shared/`` first, then the Lambda directory ahead of it — same two inserts
# test_streaming_schema.py makes.
sys.path.insert(0, os.path.join(*_TESTS_TO_PROJECT, "shared"))
sys.path.insert(0, os.path.join(*_TESTS_TO_PROJECT, "lambda", "agentic_data_ingestion_schema_extract"))

# No-op EventBridge publishers instead of a MagicMock in sys.modules: the real
# eventbridge_utils honours this flag (shared/eventbridge_utils.py:23), so the
# module imports for real and leaves no stub behind for the sibling test files
# that collect after this one.
os.environ.setdefault("EVENTBRIDGE_ENABLED", "false")
os.environ.setdefault("STATE_TABLE", "test_state_table")

sys.modules.pop("lambda_function", None)
import lambda_function as lf  # the path inserts above must run first

# ── Oracles, sourced from the specifications rather than from the code ──

#: The seven type keywords JSON Schema defines for ``type``. From the JSON Schema
#: specification, NOT from _AVRO_TYPE_MAP / _AVRO_LOGICAL_TYPE_MAP.
VALID_JSON_SCHEMA_TYPES = frozenset({"null", "boolean", "object", "array", "number", "string", "integer"})

#: Avro primitive type tokens, from R8.1.
AVRO_PRIMITIVES = ("null", "boolean", "int", "long", "float", "double", "bytes", "string")

#: Avro complex type tokens, from R8.2.
AVRO_COMPLEX = ("record", "enum", "array", "map", "union", "fixed")

#: Avro logical type annotations, from R8.3.
AVRO_LOGICAL = ("date", "timestamp-millis", "timestamp-micros", "decimal")

KNOWN_TOKENS = AVRO_PRIMITIVES + AVRO_COMPLEX

#: A Python value whose JSON Schema type is unambiguous under the JSON data
#: model. Used to drive ``infer_schema_from_records``, which resolves types from
#: observed data and so cannot be handed a type name directly. Chosen from the
#: data model, not from either resolver's policy.
JSON_TYPE_WITNESS: dict[str, object] = {
    "null": None,
    "boolean": True,
    "integer": 1,
    "number": 1.5,
    "string": "s",
    "array": [1],
    "object": {"k": 1},
}

#: A Python value a decoder produces for a datum of each Avro type — Avro's
#: ``bytes``/``fixed`` decode to ``bytes``, ``enum`` to its symbol string,
#: ``record``/``map`` to a dict, ``array`` to a list. From the Avro
#: specification's type model, not from ``_AVRO_TYPE_MAP``.
#:
#: ``union`` is absent deliberately: it is in R8.2's vocabulary but has no datum
#: of its own — a union value *is* a value of one of its branches — so there is
#: nothing to observe. Totality still covers the bare token.
AVRO_TOKEN_WITNESS: dict[str, object] = {
    "null": None,
    "boolean": True,
    "int": 1,
    "long": 2,
    "float": 1.5,
    "double": 2.5,
    "bytes": b"\x00",
    "string": "s",
    "record": {"nested": 1},
    "enum": "SYMBOL",
    "array": [1],
    "map": {"k": 1},
    "fixed": b"\x00\x01",
}

WITNESSABLE_TOKENS = tuple(AVRO_TOKEN_WITNESS)

_FIELD = "f"


def _resolved_by_infer_schema(json_types: tuple[str, ...]) -> str:
    """What ``infer_schema_from_records`` resolves one field to, for this type set.

    Reaches the existing resolver through the only door it has — observed data —
    by giving it one record per type, each carrying a witness value of that type.
    """
    records = [{_FIELD: JSON_TYPE_WITNESS[t]} for t in json_types]
    return lf.infer_schema_from_records(records)["properties"][_FIELD]["type"]


def _non_empty_subsets(items: tuple[str, ...]) -> list[tuple[str, ...]]:
    """Every non-empty subset of ``items``, as sorted tuples."""
    subsets = []
    for mask in range(1, 1 << len(items)):
        subsets.append(tuple(sorted(item for i, item in enumerate(items) if mask & (1 << i))))
    return subsets


# ── Hypothesis strategies ──────────────────────────────────────────────

novel_tokens = st.text(max_size=16).filter(lambda s: s not in KNOWN_TOKENS)

#: Arbitrary values in the three shapes an Avro type can take (token, union list,
#: parameterised mapping) plus junk in any position.
avro_shapes = st.recursive(
    st.one_of(
        st.sampled_from(KNOWN_TOKENS),
        st.sampled_from(AVRO_LOGICAL),
        st.text(max_size=12),
        st.none(),
        st.booleans(),
        st.integers(min_value=-1000, max_value=1000),
        st.floats(allow_nan=False, allow_infinity=False),
    ),
    lambda children: st.one_of(
        st.lists(children, max_size=4),
        st.dictionaries(st.sampled_from(["type", "logicalType", "items", "values", "name"]), children, max_size=5),
    ),
    max_leaves=12,
)

avro_field_schemas = st.fixed_dictionaries({"name": st.text(min_size=1, max_size=12), "type": avro_shapes})

avro_record_schemas = st.fixed_dictionaries(
    {
        "type": st.just("record"),
        "name": st.just("Fixture"),
        "fields": st.lists(avro_field_schemas, max_size=5),
    }
)

#: Avro unions, spelled as Avro spells them: a JSON array of branches.
witnessable_unions = st.lists(st.sampled_from(WITNESSABLE_TOKENS), min_size=1, max_size=5, unique=True)


# ── Property 1: totality ───────────────────────────────────────────────


def test_the_vocabulary_constants_match_the_requirement_counts():
    """R8.1-R8.3 enumerate 8 primitives, 6 complex types and 4 logical types.

    Pinned so the vocabulary this file sweeps cannot quietly shrink, which would
    make the totality tests below pass while covering less.
    """
    assert len(AVRO_PRIMITIVES) == 8
    assert len(AVRO_COMPLEX) == 6
    assert len(AVRO_LOGICAL) == 4


@pytest.mark.parametrize("token", KNOWN_TOKENS)
def test_every_known_token_maps_to_a_valid_json_schema_type(token: str):
    """**Validates: Requirements 8.6** (Property 1, primitives and complex types).

    Exhaustive rather than sampled — the vocabulary is 14 items, so there is no
    input space left over for a generator to explore.
    """
    assert lf._avro_type_to_json_type(token) in VALID_JSON_SCHEMA_TYPES


@pytest.mark.parametrize("logical", AVRO_LOGICAL)
@pytest.mark.parametrize("underlying", AVRO_PRIMITIVES)
def test_every_logical_annotation_maps_to_a_valid_json_schema_type(logical: str, underlying: str):
    """**Validates: Requirements 8.6** (Property 1, logical types).

    A logical type is an annotation on an underlying primitive, so it is swept
    against every primitive rather than only its canonical carrier.
    """
    assert lf._avro_type_to_json_type({"type": underlying, "logicalType": logical}) in VALID_JSON_SCHEMA_TYPES


@given(novel_tokens)
@settings(max_examples=200)
def test_novel_token_maps_to_a_valid_json_schema_type(token: str):
    """**Validates: Requirements 8.6** (Property 1, the novel token).

    The unbounded half of the property: an unrecognised token returns a type
    rather than raising (R8.5).
    """
    assert lf._avro_type_to_json_type(token) in VALID_JSON_SCHEMA_TYPES


@given(avro_shapes)
@settings(max_examples=300)
def test_arbitrary_avro_shape_maps_to_a_valid_json_schema_type(shape: object):
    """**Validates: Requirements 8.6** (Property 1, over all three type shapes).

    Tokens, unions and parameterised mappings, with junk reachable in every
    position — a missing ``type`` key, a non-string token, a union of unions.
    """
    assert lf._avro_type_to_json_type(shape) in VALID_JSON_SCHEMA_TYPES


@given(avro_shapes)
@settings(max_examples=300)
def test_field_level_resolution_is_valid_and_never_null(shape: object):
    """**Validates: Requirements 8.6** (Property 1 at the field level).

    Two entry points give two answers for the ``null`` token, both deliberate:
    ``_avro_type_to_json_type("null")`` is ``"null"``, matching ``_get_type``
    for ``None`` (``:129-130``), while ``_avro_field_json_type("null")`` is
    ``"string"`` because a field discards ``null`` (R8.4). So the field level
    never answers ``"null"`` for any input — the consequence asserted here.
    """
    resolved = lf._avro_field_json_type(shape)
    assert resolved in VALID_JSON_SCHEMA_TYPES
    assert resolved != "null"


def test_the_two_entry_points_answer_the_null_token_differently():
    """The type level keeps ``null``; the field level discards it (R8.1, R8.4).

    Pinned as an example because it is the one place the two levels visibly
    disagree, and conflating them is the available mistake.
    """
    assert lf._avro_type_to_json_type("null") == "null"
    assert lf._avro_field_json_type("null") == "string"


def test_deeply_nested_type_returns_a_valid_type_without_raising():
    """**Validates: Requirements 8.6** (Property 1's "raises nothing", at depth).

    ``_AVRO_MAX_TYPE_DEPTH`` exists because R8.5 forbids *any* raise and
    ``RecursionError`` is a raise. 200 levels is an order of magnitude past the
    bound, so this exercises the guard rather than Python's own limit.
    """
    nested: object = "int"
    for _ in range(200):
        nested = {"type": nested}
    assert lf._avro_type_to_json_type(nested) in VALID_JSON_SCHEMA_TYPES


@given(avro_record_schemas)
@settings(max_examples=200)
def test_whole_schema_conversion_is_total(writer_schema: dict):
    """**Validates: Requirements 8.6** (Property 1 through the public converter).

    Totality has to hold at the entry point the extractor actually calls, not
    only at the helper. Every emitted property carries a valid type and nothing
    raises, whatever the field types look like.
    """
    schema = lf.avro_schema_to_json_schema(writer_schema)
    assert schema["type"] == "object"
    for name, prop in schema["properties"].items():
        assert isinstance(name, str) and name
        assert prop["type"] in VALID_JSON_SCHEMA_TYPES


# ── Property 4: the two resolvers agree ────────────────────────────────


@pytest.mark.parametrize("json_types", _non_empty_subsets(tuple(sorted(JSON_TYPE_WITNESS))))
def test_union_resolution_agrees_with_infer_schema_from_records(json_types: tuple[str, ...]):
    """**Validates: Requirements 8.6** (Property 4, exhaustively).

    Both resolvers are run and compared; no expected answer is written down. The
    existing resolver is driven through data — one record per type, carrying a
    witness value of that type — because that is its only interface. All 127
    non-empty subsets of the seven JSON Schema types, so ``null`` discarding,
    ``{number, integer}`` widening and every ambiguous combination are covered
    by enumeration rather than by sampling.
    """
    assert lf._resolve_json_type_union(json_types) == _resolved_by_infer_schema(json_types)


@given(witnessable_unions)
@settings(max_examples=300)
def test_avro_union_spelling_agrees_with_infer_schema_from_records(tokens: list[str]):
    """**Validates: Requirements 8.6** (Property 4, reached through Avro's spelling).

    The previous test compares the resolvers on a JSON type set. This one starts
    from Avro instead: a union spelled as Avro spells it goes through
    ``_avro_field_json_type``, while the same branches' witness *datums* go
    through ``infer_schema_from_records``. Agreement then covers the token
    mapping and the union policy together.

    Logical types are out of scope here, and the reason is worth naming: a
    ``decimal``'s decoded datum is a ``Decimal``, which ``infer_schema_from_records``
    observes as ``string`` while R8.3 mandates ``number``. R8.3 fixes what a
    *declared annotation* means; R8.4's agreement requirement is about union
    resolution. The two do not overlap, and the divergence is asserted below
    rather than left implicit.
    """
    records = [{_FIELD: AVRO_TOKEN_WITNESS[t]} for t in tokens]
    expected = lf.infer_schema_from_records(records)["properties"][_FIELD]["type"]
    assert lf._avro_field_json_type(tokens) == expected


@pytest.mark.parametrize("token", WITNESSABLE_TOKENS)
def test_token_mapping_agrees_with_get_type_on_a_witness_datum(token: str):
    """**Validates: Requirements 8.6** (Property 4's premise: one type vocabulary).

    ``_avro_type_to_json_type`` maps a *declared* Avro type; ``_get_type`` maps an
    *observed* Python value. For a datum of the declared type they must land on
    the same JSON Schema type, or the union agreement above holds only by
    coincidence. This is the assertion that would fail if a token were dropped
    from ``_AVRO_TYPE_MAP`` — totality alone would not notice, because the
    fallthrough answers ``string``.
    """
    assert lf._avro_type_to_json_type(token) == lf._get_type(AVRO_TOKEN_WITNESS[token])


def test_decimal_declared_and_observed_types_diverge_by_design():
    """R8.3's logical mapping is a declared-type rule, not an observed-data rule.

    Documents the one boundary of Property 4's agreement claim so a reader does
    not mistake it for an oversight: a declared ``decimal`` is ``number`` (R8.3),
    while the same value observed as a ``Decimal`` reads as ``string``. Both are
    correct for what they describe.
    """
    assert lf._avro_type_to_json_type({"type": "bytes", "logicalType": "decimal"}) == "number"
    assert lf._get_type(Decimal("1.5")) == "string"
