# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.2 — the Rename_Manifest as a sub-payload of the schema-mapping
approval payload (R10.1, R10.2), and its two wired-in identity values.

**Function under test, named by file and line because two functions in this
repository carry the name:** ``create_approval_payload`` in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py``. The other is
``create_approval_payload`` in
``lambda/agentic_data_ingestion_create_code/lambda_function.py:521``, and it is
**not** the one W4 changes — risk R5.8, which is that a grep-driven edit changes
the wrong one. This module asserts on the discriminator the same way its sibling
``test_approval_payload_keys_characterization.py`` does: the five-name parameter
list, plus ``__file__``. If a later edit points either module at ``create_code``'s
function, that assertion fails before any manifest assertion can confuse the
reader with a ``KeyError``.

What task 6.1 built and what this task wires:

* ``derive_rename_manifest`` is the pure derivation. It records the
  ``manifest_id`` and ``source_schema_hash`` it is **given** and says so in its
  own docstring — "wiring it to that value is task 6.2's".
* So the two values are this task's, and they are what this module pins:
  ``manifest_id`` is the mapping record's own ``mapping_id`` (design §Data Models
  — reusing it introduces no new identifier space), and ``source_schema_hash`` is
  the same value ``store_mapping_separately`` writes onto the mapping record,
  ``"no-hash"`` default included.

**Why ``manifest_id`` is derived inside the producer rather than passed in.**
``create_approval_payload``'s parameter list is pinned by task 1.2's
characterization, by exact equality, as the R5.8 discriminator above — so a sixth
parameter is not available without editing a file this task must leave alone. The
id is therefore obtained by calling ``generate_mapping_id`` on the same three
arguments ``lambda_handler`` calls it with. That is one derivation reused, not a
second authority: that function *is* the definition of the mapping record's id.
The two agreeing is not left to inspection — ``test_manifest_id_is_the_mapping_id
_store_mapping_result_was_handed`` drives the real ``store_mapping_result`` and
compares the payload's value against the ``mapping_id`` its caller passed.

*Rejected: a placeholder id stamped by the caller afterwards.* ``LGR-b2f79ef-11``
(**template / ratified**) records what a substituted-in identity costs — "it
records a missing hash as a valid-looking identity that matches nothing, so a hash
comparison downstream silently answers 'different' for a reason unrelated to the
schemas" — and task 6.3's reuse lookup is exactly that downstream comparison.

**The degraded ``except`` arm is deliberately NOT pinned here.** It returns
``{approval_type, error, generated_by}`` and carries no ``rename_manifest``, and
task 1.2 recorded that fact *for this task* rather than asserting it: "the key it
adds lands on the happy path only … so any consumer of the new key must tolerate
its absence." Pinning the arm would record it as intended behaviour, which
do-NOT 10 and ``LGR-b2f79ef-13`` (**template / ratified**, which narrowed a
blanket ``except`` in this same module) both decline. The tolerance is asserted on
the **reader**, in
``dashboard-react/src/utils/__tests__/rename-manifest.test.ts``, which is where a
consumer can be held to it. Every helper below instead asserts ``logger.error``
was never called, so a swallow-manufactured payload fails rather than being read
as the documented shape.

**Not asserted here, and named so the gap is legible:** R10.3 (targets satisfy
the Identifier_Allowlist) and R10.4 (targets pairwise distinct) belong to task
6.1 and are covered by ``test_rename_manifest_derivation_prop.py`` over ≥100
generated inputs, plus the closing invariant check inside
``derive_rename_manifest`` itself. Re-asserting them here would be a second, weaker
copy of a property test.

**Validates: Requirements 10.1, 10.2**
"""

import inspect
import os
import sys
from unittest.mock import MagicMock, patch

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in the sibling files here).
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

# sys.modules.pop + sys.path.insert: `lambda_function` is a name 23 Lambda dirs
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)`, so nothing here depends on sys.modules still pointing
# at this module at call time.
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

#: The payload key. One spelling in this module, so a rename fails in one place.
_MANIFEST_KEY = "rename_manifest"

#: design §Data Models fixes exactly these four, and no more. Equality is asserted
#: rather than a subset, because the manifest is **persisted and read back
#: verbatim at code-generation time** (risk R5.7) — an extra key would be a field
#: some later reader might start depending on without a decision, and a missing
#: one is a hole in what task 6.3 or 6.5 reads.
_MANIFEST_KEYS = frozenset({"manifest_id", "source_schema_hash", "approval_id", "renames"})

#: The two keys of one Proposed_Rename (R10.2 — **both** names).
_RENAME_KEYS = frozenset({"original_name", "target_name"})


# --- Fixtures -----------------------------------------------------------------


def _target_schema() -> dict:
    return {
        "type": "object",
        "properties": {"customer_id": {"type": "string"}, "full_name": {"type": "string"}},
        "required": ["customer_id"],
    }


def _mapping_result() -> dict:
    """Two mappings, both carrying `source_field` and `target_field`.

    Both keys on every entry is what keeps execution out of the degraded arm —
    `confidence_scores` indexes `m["target_field"]` and `field_mapping_details`
    indexes `m["source_field"]` directly.
    """
    return {
        "mappings": [
            {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.97, "transformation": "direct"},
            {"source_field": "first name", "target_field": "full_name", "confidence": 0.9},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "Two mappings.",
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _source_schema(*column_names: str) -> dict:
    """A source schema declaring `column_names`, **in that order**.

    The order is load-bearing and it survives: `properties` is an ordinary dict,
    so it preserves insertion order, and in production the same order arrives
    from `get_source_schema` (`:193`) `json.loads`-ing the `extracted_schema`
    STRING off the `schema_extract` row — `json.loads` preserves object key
    order. R10.4's collision suffix is defined in source-column order, so a
    fixture that reordered here would be testing a different rule.
    """
    return {"type": "object", "properties": {name: {"type": "string"} for name in column_names}}


def _build(
    *,
    source_schema: dict | None = None,
    target_schema: dict | None = None,
    mapping_result: dict | None = None,
    source_schema_hash: str = "src-hash-p55-6-2",
    target_schema_hash: str = "tgt-hash-p55-6-2",
) -> dict:
    """Call the producer and refuse a payload it did not build on the success arm."""
    with patch.object(_mod, "logger") as mock_logger:
        payload = _mod.create_approval_payload(
            _source_schema("cust_id", "first name") if source_schema is None else source_schema,
            _target_schema() if target_schema is None else target_schema,
            _mapping_result() if mapping_result is None else mapping_result,
            source_schema_hash,
            target_schema_hash,
        )
        mock_logger.error.assert_not_called()

    assert "error" not in payload, "the degraded except arm ran — this is not the documented payload"
    return payload


# --- R5.8: this is the right function of the two --------------------------------


def test_this_is_the_schema_mapping_function_and_not_create_codes():
    """R5.8, asserted rather than trusted to the module docstring.

    Deliberately a duplicate of the sibling characterization file's first test.
    That file must not be edited by this task, so it cannot be the place a *new*
    module records which function it is pointed at; and the two same-named
    functions are distinguishable by signature and by nothing a grep can see.
    """
    parameters = list(inspect.signature(_mod.create_approval_payload).parameters)

    assert parameters == [
        "source_schema",
        "target_schema",
        "mapping_result",
        "source_schema_hash",
        "target_schema_hash",
    ], f"this is not schema_mapping's create_approval_payload; create_code's (:521) takes 8: {parameters}"
    assert _mod.__file__.endswith("agentic_data_ingestion_schema_mapping/lambda_function.py"), _mod.__file__


# --- R10.1: the manifest is on the existing artifact ---------------------------


def test_the_payload_carries_the_manifest_as_a_sub_payload_of_the_existing_artifact():
    """R10.1 — the **existing** Schema_Mapping_Approval presents the manifest.

    Two things are asserted together because they are one decision (W4-D3): the
    manifest is a key on this payload, and the payload is still a
    ``schema_mapping`` payload. A new top-level approval kind was rejected — it
    would mean a new ``waitForTaskToken`` state and a new Choice under
    ``LGR-69ca78b-1`` (standing invariant) for information that belongs on an
    approval the approver is already looking at — so ``approval_type`` staying
    ``schema_mapping`` is part of the requirement rather than incidental.
    """
    payload = _build()

    assert payload["approval_type"] == "schema_mapping", (
        "the manifest rides the EXISTING approval kind (R10.1, W4-D3); a new kind was rejected"
    )
    assert _MANIFEST_KEY in payload
    assert set(payload[_MANIFEST_KEY]) == _MANIFEST_KEYS


def test_the_manifest_is_appended_so_no_existing_position_moves():
    """The key order is part of the serialized bytes stored at ``output_data.approval``.

    ``LGR-e5e9edd-6`` (**template / ratified**) records the append-never-insert
    posture for the sibling projection in ``approval_handler`` and the reason: the
    order *is* the bytes. Asserting last position here is what makes the sibling
    tuple in ``tests/fixtures/conftest.py`` predictable rather than coincidental.
    """
    payload = _build()

    assert list(payload)[-1] == _MANIFEST_KEY, (
        f"the manifest must be the LAST key so no existing position moves; got {list(payload)}"
    )


def test_a_fully_conforming_source_schema_yields_an_empty_list_not_an_absent_key():
    """ "No renames proposed" and "this payload says nothing about renames" differ.

    R10.1 only *requires* presentation when a Non_Conforming_Column_Name exists,
    so an absent key would satisfy the letter of it. design §Architecture rules
    that out in as many words — "nothing becomes a conditionally present key" —
    and the reason is the three-state discipline this repository applies
    everywhere else: ``LGR-4854b3d-1`` (**template / ratified**) on ``field_count``
    and p50's ``mapping_count`` both refuse to let absence and zero share a
    rendering. An empty list is "read, and nothing proposed"; an absent key is
    "not established", and only the degraded arm may say that.
    """
    payload = _build(source_schema=_source_schema("cust_id", "full_name"))

    assert payload[_MANIFEST_KEY]["renames"] == []
    assert _MANIFEST_KEY in payload


# --- R10.2: both names of every Proposed_Rename --------------------------------


def test_every_rename_carries_both_the_original_and_the_proposed_name():
    """R10.2, and the original is carried **verbatim** (do-NOT 1).

    Nothing is sanitized in place. The manifest is a *proposal* reaching a human,
    so the approver has to be able to see what the column is called today beside
    what it would be called — which is only possible if the original survives the
    trip unedited.
    """
    payload = _build(source_schema=_source_schema("cust_id", "first name", "2024-total"))
    renames = payload[_MANIFEST_KEY]["renames"]

    assert [entry["original_name"] for entry in renames] == ["first name", "2024-total"], (
        "one entry per DISTINCT non-conforming column, in source-column order, and a conforming "
        "column (`cust_id`) is not an entry"
    )
    for entry in renames:
        assert set(entry) == _RENAME_KEYS
        assert isinstance(entry["original_name"], str) and isinstance(entry["target_name"], str)
        assert entry["target_name"], "a Proposed_Rename with no proposed name is not a proposal"


def test_the_collision_suffix_lands_in_the_source_columns_own_order():
    """R10.4's "second and subsequent **in source-column order**", at this call site.

    Task 6.1 owns the rule and property-tests it. What is this task's is that the
    order reaching it is the source schema's own ``properties`` order and not
    something re-derived — so the same two originals in the opposite order must
    swap which one keeps the bare name. Sanitization is not injective:
    ``first name`` and ``first-name`` both yield ``first_name``.
    """
    forward = _build(source_schema=_source_schema("first name", "first-name"))[_MANIFEST_KEY]["renames"]
    reverse = _build(source_schema=_source_schema("first-name", "first name"))[_MANIFEST_KEY]["renames"]

    assert [(e["original_name"], e["target_name"]) for e in forward] == [
        ("first name", "first_name"),
        ("first-name", "first_name_2"),
    ]
    assert [(e["original_name"], e["target_name"]) for e in reverse] == [
        ("first-name", "first_name"),
        ("first name", "first_name_2"),
    ]


# --- The two values this task wires ------------------------------------------


def test_approval_id_is_none_until_an_approver_decides():
    """design §Data Models: ``None`` until an approver decides; task 6.4 records it.

    Present-and-``None``, never absent: task 6.3's reuse condition requires a
    non-``None`` value by construction (R10.8 also requires approved status), and a
    key that is sometimes missing makes that a two-shape read instead of one.
    """
    payload = _build()

    assert payload[_MANIFEST_KEY]["approval_id"] is None
    assert "approval_id" in payload[_MANIFEST_KEY]


def test_source_schema_hash_is_carried_through_verbatim():
    """R10.8 — the Schema_Hash this manifest was recorded against.

    Expectation from the parameter name, not from output: a value named
    ``source_schema_hash`` arriving at a key named ``source_schema_hash`` has been
    transformed by nothing.
    """
    payload = _build(source_schema_hash="src-abc123")

    assert payload[_MANIFEST_KEY]["source_schema_hash"] == "src-abc123"


def test_an_undeterminable_hash_arrives_as_the_same_sentinel_the_mapping_record_carries():
    """One sentinel, so task 6.3's filter has one value to be inert on (R10.9).

    ``store_mapping_separately`` writes ``source_schema_hash if source_schema_hash
    else "no-hash"`` onto the mapping record, so an undeterminable hash is a
    **value** and never a missing key. R10.8 keys reuse on equality between the
    manifest's hash and that attribute, so the manifest applying a *different*
    default — ``""`` here against ``"no-hash"`` there — would make every such
    comparison answer "different" for a reason unrelated to the schemas. That is
    ``LGR-b2f79ef-11``'s failure mode with the sides swapped, and it is why this is
    a test rather than a comment.
    """
    payload = _build(source_schema_hash="")

    assert payload[_MANIFEST_KEY]["source_schema_hash"] == "no-hash"

    # And it really is the mapping record's spelling, read off the writer rather
    # than transcribed from memory.
    source = inspect.getsource(_mod.store_mapping_separately)
    assert '"source_schema_hash": source_schema_hash if source_schema_hash else "no-hash"' in source, (
        "store_mapping_separately's default changed. The manifest mirrors it deliberately (R10.8 "
        "compares the two for equality), so this test is the place the two literals are held together."
    )


def test_manifest_id_is_the_mapping_id_store_mapping_result_was_handed():
    """design §Data Models: ``manifest_id`` **is** the mapping record's ``mapping_id``.

    Driven through the real ``store_mapping_result`` rather than asserted against
    a recomputation, because the claim is about the *wiring*: the id the payload
    carries has to be the id the caller is storing the mapping under. Two
    authorities that agree by inspection are what ``LGR-28b4d75-1`` and
    ``LGR-9e76249-2`` (both **template / ratified**) reject; one derivation reused
    is what this asserts.

    ``mapping_id`` here is computed exactly as ``lambda_handler`` computes it —
    ``generate_mapping_id(source_schema_hash, target_schema_hash, mapping_result)``
    — because that is the production call, and pinning the wiring means driving the
    production path rather than a convenient stand-in.
    """
    source_schema = _source_schema("cust_id", "first name")
    target_schema = _target_schema()
    mapping_result = _mapping_result()
    source_hash, target_hash = "src-wiring", "tgt-wiring"
    mapping_id = _mod.generate_mapping_id(source_hash, target_hash, mapping_result)

    stored: dict = {}

    def _capture(Item):  # boto3's own parameter name, hence the capital
        stored.update(Item)

    with (
        patch.object(_mod, "logger"),
        patch.object(_mod, "table") as mock_table,
        patch.object(_mod, "create_output_data", side_effect=_output_data),
        patch.object(_mod, "convert_floats_to_decimal", side_effect=lambda value: value),
    ):
        mock_table.put_item.side_effect = _capture
        _mod.store_mapping_result(
            "run-wiring",
            "pipe-wiring",
            mapping_id,
            mapping_result,
            source_hash,
            source_schema,
            target_schema,
            target_hash,
            None,
            True,
        )

    manifest = stored["output_data"]["approval"][_MANIFEST_KEY]
    assert manifest["manifest_id"] == mapping_id, (
        f"the manifest must be identified by the mapping record's own mapping_id (design §Data "
        f"Models — no new identifier space). stored under {mapping_id!r}, manifest says "
        f"{manifest['manifest_id']!r}"
    )
    assert manifest["manifest_id"] == stored["mapping_id"], (
        "and it must equal the mapping_id on the state row itself, which is the value task 6.3's "
        "reuse lookup selects candidate records by"
    )


def test_the_manifest_reaches_the_client_through_output_data_approval():
    """The path W4-D3 names: ``output_data["approval"]``, whole, as one dict.

    Not a second attribute beside it and not a JSON string: ``store_mapping_result``
    already stores this payload as a native dict at that address, and
    ``approval_handler._schema_mapping_payload`` projects the client's view out of
    it. Asserting the address here is what makes the projection's new eleventh key
    point at something.
    """
    stored: dict = {}

    with (
        patch.object(_mod, "logger"),
        patch.object(_mod, "table") as mock_table,
        patch.object(_mod, "create_output_data", side_effect=_output_data),
        patch.object(_mod, "convert_floats_to_decimal", side_effect=lambda value: value),
    ):
        mock_table.put_item.side_effect = lambda Item: stored.update(Item)
        _mod.store_mapping_result(
            "run-address",
            "pipe-address",
            "map-address",
            _mapping_result(),
            "src-address",
            _source_schema("cust_id", "first name"),
            _target_schema(),
            "tgt-address",
        )

    manifest = stored["output_data"]["approval"][_MANIFEST_KEY]
    assert [entry["original_name"] for entry in manifest["renames"]] == ["first name"]
    assert isinstance(stored["output_data"]["approval"], dict), (
        "the approval payload is stored as a NATIVE dict, not a JSON string (store_mapping_result's "
        "own comment says so); a string here would put a second parse on every reader"
    )


def _output_data(*, artifact_type, primary_id, status, summary, s3_uris):
    """A real dict in place of the mocked `artifact_schema.create_output_data`.

    `artifact_schema` is a `MagicMock` in this directory's import shim, so the
    production function is unavailable and its return value would be a mock that
    silently accepts the `["approval"]` assignment and tells us nothing. The keys
    mirror the producer's own call.
    """
    return {
        "artifact_type": artifact_type,
        "primary_id": primary_id,
        "status": status,
        "summary": summary,
        "s3_uris": s3_uris,
    }
