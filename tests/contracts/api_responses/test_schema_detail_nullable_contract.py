# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test — ``nullable`` is optional on BOTH sides of the schema-detail wire.

Feature: p45-unmapped-target-field-visibility, task 22.1.
_Requirements: R6.1, R6.3, R5.3_

``GET /api/schemas/{id}`` reports a field's nullability as a **three**-state
value: ``false`` = the source declared it required, ``true`` = the source
declared it optional, **absent** = the source did not say. This file pins the
third state across the language boundary, in the only place where both sides can
be observed at once.

Two claims, one per side, and the point is that they meet:

1. **TypeScript — the key is optional.** ``RawSchemaDetailField``
   (``types.ts:435``) declares ``nullable?: boolean`` (``:465``), so ``nullable``
   is **not** in ``required_fields(...)``. A consumer that declared it required
   would make the producer's omission a type lie, and the frontend would be one
   ``?? true`` away from C4's collapse.
2. **Python — the producer omits it when nothing was stated.** ``_schema_detail``
   (``handlers/schemas.py:1542``) sets the key only when its input carries one,
   at the conditional gate ``:1606-1608`` — the same gate ``displayLabel`` uses
   twenty lines below at ``:1625-1627``. What it emits must satisfy the interface
   above: every **required** key present, no key the interface does not declare,
   and every value of the declared type.

WHERE EACH SIDE'S EXPECTATION COMES FROM
----------------------------------------
Neither side is transcribed from the other, and neither is transcribed from the
side under test (C2, do-NOT 7, ``LGR-28b4d75-5`` — **template / ratified**: *"a
test written from the producer's output cannot detect the producer drifting,
because the output is its own oracle"*).

* The **TypeScript** expectation is *read out of* ``types.ts`` at test time with
  the shared reader in ``helpers.py`` (R6.2, ``LGR-99050e6-5`` — **template /
  ratified**), never restated here. A rename or a re-declaration changes what the
  parse returns.
* The **Python** expectation comes from **R5.3** — *"WHERE the source did not
  state nullability, THE Schema_Detail_View SHALL render Not_Established … and
  SHALL NOT render 'optional'"* — applied to the producers' own input shapes: the
  Glue branch of ``_collect_schema_rows`` writes four keys and no ``nullable``
  (the comprehension at ``:492-526``, with the omission's reason commented at
  ``:496-507``), because ``StorageDescriptor.Columns[]`` carries no nullability
  at all; the three producers that state one derive it from the body's
  ``required`` (``_uploaded_schema_fields``' two shapes at ``:1376`` and ``:1405``,
  ``_s3_tables_schema_fields`` at ``:1469``). Rows are **built** from those
  shapes; no producer is run to discover them.

``_schema_detail`` is imported and called because it is the **subject** here, not
the oracle. Its oracle is the interface, read from the other side of the wire.

WHY BOTH THIS FILE AND ``test_schema_detail_nullable_omission.py``
-----------------------------------------------------------------
Task 11.1's file, in ``tests/unit/lambdas/dashboard_api/``, pins the same
omission against **Python-side** constants: exact key sets, the pass-through of a
stated boolean, an explicit ``None`` being dropped, and the Glue branch's own
output on the listing. It owns the *branch*.

This file owns the *language boundary*, which none of that reaches: that the
consumer's declared shape still permits the omission, and that what the producer
emits still satisfies the consumer's declared shape. A narrowing of
``RawSchemaDetailField`` fails here and nowhere in ``tests/unit/``; a new
per-field key the interface does not declare fails here too.

CORRECTING design §4.9b
-----------------------
§4.9b says ``nullable`` is pinned *"nowhere on either side"*. On the **Python**
side that is wrong as written — it is pinned six times, all reaching
``_schema_detail`` through ``get_schema``:
``tests/unit/lambdas/dashboard_api/test_schemas_listing_sources.py:686``, ``:687``,
``:691``, ``:692`` (both uploaded body shapes) and ``:727``, ``:728``
(s3_tables), each asserting ``is False`` / ``is True``. Every one of them is a
**stated**-nullability case, which is why task 9.1's change to the unstated gate
moved no test. §4.9b is therefore right about the case that matters — the
unstated one was pinned nowhere — and wrong about the count.

On the **TypeScript** side §4.9b holds as written, verified rather than assumed:
``RawSchemaDetailField`` appears only in its own declaration (``:435``) and in
``RawSchemaDetail.fields`` (``:486``). Nothing destructures it, so before this
file the optionality was declared and unobserved.

DELIBERATELY NOT ASSERTED HERE
------------------------------
* **The producers' ``required``-to-``nullable`` derivation.** Pinned by the six
  assertions above; re-asserting it here would make this file a second authority
  on what ``required`` means.
* **The Glue branch's own output**, on the listing or through the endpoint. Task
  11.1 owns it, with the moto seeding that needs.
* **``normalizeField`` / ``fieldRequirementFromNullable``** — the frontend
  readers. Their own vitest suites own the display text; this file stops at the
  wire.
* **The JDBC detect path, which is do-NOT 18 territory and stays as it is.**
  ``handlers/jdbc_resources.py:660`` emits a non-optional ``nullable`` and
  ``test_jdbc_target_detect_schema_contract.py:343-352`` pins it — ``"nullable"``
  must be **in** every column and must be a ``bool``, per that endpoint's own
  documented ``{name, type, nullable, description}`` shape (Req 4.7 of its spec).
  A different endpoint, a different producer, a different contract. Nothing here
  extends to it and nothing here should be "harmonised" with it.

FIXTURES AND AWS
----------------
None needed, and that is a property of the subject rather than a shortcut:
``_schema_detail``'s only I/O is the Context_Sidecar read at ``:1586``, gated on
``row["source"] == "uploaded"``. Rows sourced ``glue_catalog`` or
``s3_table_bucket`` therefore exercise the gate as a **pure function** — no moto,
no ``mock_aws()``, no boto3 client mock (``tests/unit/test_no_duplicate_mocking_prop.py``),
and no module-level client to construct: every ``boto3.client`` call in
``schemas.py`` is inside a function.

``AWS_REGION`` is deliberately **not** set (``LGR-3e45e65-1`` — **template /
ratified**): nothing here resolves a region, so pinning one from a test would
assert a fact this file does not depend on, in a layer whose sibling files may be
imported first.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path
from typing import Any

import pytest

from .helpers import (
    Interface,
    declares_null,
    load_interfaces,
    matches_json_type,
    read_interface,
    required_fields,
)

# ---------------------------------------------------------------------------
# Path / env bootstrap, matching the sibling contract tests that import a
# dashboard-API handler (test_metrics_stage_keys_contract.py:108-148). Repeated
# per file rather than hoisted, because under xdist --dist=loadfile any file in
# this directory may be imported first.
# ---------------------------------------------------------------------------
_REPO_TESTS = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_TESTS.parent
_LAMBDA_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api"
_SHARED_DIR = _PROJECT_ROOT / "shared"
for _p in (str(_LAMBDA_DIR), str(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# `handlers.common` resolves nine table names through `require_env` at import
# time (`common.py:35-43`), so the names must exist before the import itself.
# setdefault, so a sibling file that got here first is never overridden. No
# region: see the module docstring.
for _name, _value in (
    ("AGENTIC_DATA_INGESTION_ENV", "local"),
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
):
    os.environ.setdefault(_name, _value)

from handlers.schemas import _schema_detail

# ---------------------------------------------------------------------------
# The frontend source this contract is pinned against
# ---------------------------------------------------------------------------
_FRONTEND_TYPES = _PROJECT_ROOT / "dashboard-react" / "src" / "types.ts"

#: The consumer interface for one field of DD9's per-id response (`types.ts:435`).
_FIELD_INTERFACE = "RawSchemaDetailField"

#: The key whose optionality is this file's subject.
_SUBJECT = "nullable"


def _types_source() -> str:
    assert _FRONTEND_TYPES.is_file(), (
        f"{_FRONTEND_TYPES} is missing. This contract derives its TypeScript expectations from that "
        f"file; if it moved, repoint the constant rather than deleting the assertion."
    )
    return _FRONTEND_TYPES.read_text(encoding="utf-8")


def _field_interface() -> Interface:
    """``RawSchemaDetailField`` as ``{field: (required, ts_type)}``, read at test time.

    ``load_interfaces`` already asserts the file exists and that the parse is
    non-empty, so a renamed interface or a moved file fails here rather than
    quietly reducing every assertion below to a no-op.
    """
    return load_interfaces(_FRONTEND_TYPES, _FIELD_INTERFACE)[_FIELD_INTERFACE]


# ---------------------------------------------------------------------------
# The producers' input shapes, built from R5.3 and the producers as SOURCE
# ---------------------------------------------------------------------------
#: A field as the Glue branch of `_collect_schema_rows` writes it (`:492-526` —
#: `name` at `:494`, `type` at `:495`, `description` at `:518`, `context` at
#: `:523`): four keys and no `nullable`, because `StorageDescriptor.Columns[]`
#: carries no nullability at all. R5.3's named live instance of "the source did
#: not say".
_UNSTATED_FIELD: dict[str, Any] = {
    "name": "customer_id",
    "type": "bigint",
    "description": "Unique customer key",
    "context": "",
}


def _stating_field(nullable: bool) -> dict[str, Any]:
    """A field as the three stating producers write it (`:1376`, `:1405`, `:1469`)."""
    return {
        "name": "email",
        "type": "string",
        "nullable": nullable,
        "description": "Primary contact address",
        "context": "",
    }


def _row(source: str, field: dict[str, Any]) -> dict[str, Any]:
    """One Schema_Listing row as ``get_schema:1701-1702`` hands it to ``_schema_detail``.

    ``source`` is never ``"uploaded"``: the sidecar read at ``:1586`` is gated on
    that value, so ``glue_catalog`` and ``s3_table_bucket`` keep this a pure
    function of its argument.
    """
    return {
        "schemaId": f"{source}_row",
        "name": f"{source} row",
        "source": source,
        "targetType": "S3 Tables (Iceberg)",
        "lastModified": "2026-08-21T00:00:00",
        "fields": [field],
    }


def _emitted_field(source: str, field: dict[str, Any]) -> dict[str, Any]:
    """The single response field ``_schema_detail`` builds for one input field."""
    fields = _schema_detail(_row(source, field))["fields"]
    assert len(fields) == 1, f"the fixture row declares one field; got {fields}"
    return fields[0]


# ---------------------------------------------------------------------------
# Mutation support for the oracle guards
# ---------------------------------------------------------------------------
_DECLARATION = re.compile(
    r"export\s+interface\s+" + re.escape(_FIELD_INTERFACE) + r"\s*\{.*?^\}",
    re.DOTALL | re.MULTILINE,
)


def _mutate_declaration(old: str, new: str) -> str:
    """``types.ts`` source with ``old`` → ``new`` **inside ``RawSchemaDetailField`` only**.

    Scoped to the one declaration on purpose: ``nullable?: boolean;`` also appears
    in ``SchemaField`` (``types.ts:404``), the normalized frontend type, and a
    whole-file replace would mutate that instead and prove nothing about this
    contract. Asserts the mutation is real, so a guard can never pass because its
    edit was a no-op.
    """
    source = _types_source()
    match = _DECLARATION.search(source)
    assert match, f"{_FIELD_INTERFACE} declaration not found in {_FRONTEND_TYPES}"
    block = match.group(0)
    assert old in block, (
        f"{old!r} is not in the {_FIELD_INTERFACE} declaration, so this mutation would be a no-op — "
        f"rewrite the guard against the current declaration, do not weaken it"
    )
    return source[: match.start()] + block.replace(old, new, 1) + source[match.end() :]


#: The declaration this file's guards mutate, as it stands today (`types.ts:465`).
_OPTIONAL_DECL = f"{_SUBJECT}?: boolean;"

#: Distinguishes "the key is absent" from "the key is present and False". A bare
#: `.get(key, False)` default cannot: `False` is a legitimate value here, and it
#: is the one an omission-by-default would have overwritten.
_ABSENT = object()


# ═══════════════════════════════════════════════════════════════════════════
# The oracle itself — exercised before it is trusted
# ═══════════════════════════════════════════════════════════════════════════


class TestTheOracleItself:
    """Anti-vacuity, on ``LGR-99050e6-5``'s four-guard model.

    Every assertion in this file rests on the parse of one interface. A reader
    that returned an empty set, or the same set on every input, would make each
    of them trivially true — so the parse is shown to be non-empty, to see the
    subject, and to *change* when the declaration changes.
    """

    def test_the_interface_parses_and_declares_the_subject(self):
        """A non-empty parse that actually contains ``nullable``.

        **Validates: Requirement 6.1**
        """
        interface = _field_interface()
        assert _SUBJECT in interface, (
            f"{_FIELD_INTERFACE} no longer declares {_SUBJECT!r} — this contract is pinned against that "
            f"key. Read {_FRONTEND_TYPES}:435 before repointing it."
        )
        assert len(interface) > 1, f"a one-key parse is a parse failure in disguise: {interface!r}"

    def test_the_interface_has_required_keys_too(self):
        """ "Optional" is a real distinction in this interface, not a file-wide default.

        If every key were optional, asserting ``nullable`` is optional would say
        nothing about ``nullable``. The other four keys are required, so the
        subject's ``?`` is a deliberate difference (``types.ts:435-470``).

        **Validates: Requirement 6.1**
        """
        interface = _field_interface()
        required = required_fields(interface)
        assert required, f"{_FIELD_INTERFACE} declares no required key at all — the parse is suspect"
        assert required == set(interface) - {_SUBJECT}, (
            f"exactly one key of {_FIELD_INTERFACE} is optional and it is {_SUBJECT!r}; "
            f"required={sorted(required)}, all={sorted(interface)}"
        )

    def test_making_the_subject_required_is_visible_to_the_reader(self):
        """The detector is not vacuous: dropping the ``?`` flips the R6.1 assertion.

        This is the narrowing ``LGR-99050e6-5`` (**template / ratified**) built
        its guard for — *"a guard now asserts the interface still declares that
        field required and nullable so a future narrowing fails loudly"* — in the
        mirror-image direction. Here the wire shape is *wider* than a required
        boolean, so the narrowing to guard against is the removal of the ``?``.

        **Validates: Requirement 6.1**
        """
        mutated = read_interface(
            _mutate_declaration(_OPTIONAL_DECL, f"{_SUBJECT}: boolean;"),
            _FIELD_INTERFACE,
            "<mutated types.ts>",
        )
        assert _SUBJECT in required_fields(mutated), (
            "the mutation did not reach the reader — rewrite it, do not weaken the assertion it guards"
        )

    def test_a_renamed_interface_raises_rather_than_reading_as_empty(self):
        """An absent interface must be loud: an empty parse passes everything."""
        renamed = _types_source().replace(
            f"export interface {_FIELD_INTERFACE} {{",
            f"export interface Renamed{_FIELD_INTERFACE} {{",
            1,
        )
        with pytest.raises(AssertionError, match=f"{_FIELD_INTERFACE} not found"):
            read_interface(renamed, _FIELD_INTERFACE, "<mutated types.ts>")

    def test_a_declaration_the_reader_does_not_understand_raises(self):
        """A shape the reader cannot parse fails loudly rather than dropping the key."""
        with pytest.raises(AssertionError, match=f"Unparsed line in {_FIELD_INTERFACE}"):
            read_interface(
                _mutate_declaration(_OPTIONAL_DECL, f"{_SUBJECT}?: boolean"),  # no `;`
                _FIELD_INTERFACE,
                "<mutated types.ts>",
            )


# ═══════════════════════════════════════════════════════════════════════════
# R6.1 / R6.3 — the TypeScript side declares the key optional
# ═══════════════════════════════════════════════════════════════════════════


class TestTheConsumerDeclaresItOptional:
    def test_nullable_is_not_a_required_field(self):
        """R6.1: the consumer permits the omission R5.3 requires.

        ``RawSchemaDetailField.nullable`` is declared with ``?`` (``types.ts:465``),
        so a response that omits the key still satisfies the type the frontend
        reads DD9's per-id response through. Were it required, the producer's
        omission at ``handlers/schemas.py:1606-1608`` would be a type lie and the
        nearest repair would be a ``?? true`` default — C4's collapse
        (``LGR-4854b3d-1``, **template / ratified**: *"three-state … and null is
        never collapsed"*, scope naming ``handlers/schemas.py``) and do-NOT 5.

        **Validates: Requirements 6.1, 6.3, 5.3**
        """
        interface = _field_interface()
        assert _SUBJECT not in required_fields(interface), (
            f"{_FIELD_INTERFACE}.{_SUBJECT} is declared REQUIRED in {_FRONTEND_TYPES}. R5.3 needs three "
            f"states and absence is the third one, so the consumer must permit the key to be missing. "
            f"Restore the `?` rather than reinstating a producer-side default (`LGR-4854b3d-1`, do-NOT 5)."
        )

    def test_the_declared_type_admits_no_null(self):
        """do-NOT 17, on the TypeScript side: ``null`` is not a third spelling.

        The declared type is a bare ``boolean``, not ``boolean | null``, so the
        consumer has exactly two values plus absence — which is what makes
        absence carry "not established" unambiguously. ``LGR-44d601e-1``
        (**template / ratified**) clears a stored value by *removing* the key for
        this reason: *"a stored `""` would have to be distinguished from absence
        at every reader."*

        Read with ``declares_null``, the helper ``LGR-99050e6-5`` added for the
        opposite case — a field that must keep admitting ``null``. Same guard,
        inverted, because here admitting ``null`` is the defect.

        **Validates: Requirements 6.3, 5.3**
        """
        _, declared = _field_interface()[_SUBJECT]
        assert not declares_null(declared), (
            f"{_FIELD_INTERFACE}.{_SUBJECT} is declared {declared!r}, which admits null. Two facts have "
            f"two spellings — `false`/`true` and absence; a third would be one too many (do-NOT 17, "
            f"`LGR-44d601e-1`)."
        )


# ═══════════════════════════════════════════════════════════════════════════
# R5.3 — the producer's emission satisfies that declaration
# ═══════════════════════════════════════════════════════════════════════════


def _assert_conforms(field: dict[str, Any], interface: Interface) -> None:
    """The emitted field satisfies ``RawSchemaDetailField`` as declared.

    Three ways a producer can break the consumer, all checked against the parse
    rather than against a list in this file: a required key missing, a key the
    interface does not declare, and a value of the wrong type.
    """
    missing = required_fields(interface) - set(field)
    assert not missing, (
        f"{sorted(missing)} are required by {_FIELD_INTERFACE} and absent from the response field "
        f"{field!r} — the consumer reads them unconditionally"
    )
    undeclared = set(field) - set(interface)
    assert not undeclared, (
        f"the response carries {sorted(undeclared)}, which {_FIELD_INTERFACE} does not declare — a key "
        f"the consumer cannot see is a key nothing renders (R6.1)"
    )
    for key, value in field.items():
        declared = interface[key][1]
        assert matches_json_type(value, declared), (
            f"{key}={value!r} ({type(value).__name__}) does not satisfy {declared!r} as {_FIELD_INTERFACE} declares it"
        )


class TestTheProducerHonoursThatDeclaration:
    def test_an_unstated_field_omits_the_key_and_still_conforms(self):
        """R5.3: nothing stated means **no key** — and the rest of the field still fits.

        The input is the Glue branch's own field shape (``:492-526``), built from
        that shape rather than read back from it. ``_schema_detail``'s gate at
        ``:1606-1608`` reads ``field.get("nullable")`` and sets the key only when
        it is not ``None``; before task 9.1 it was ``field.get("nullable", True)``,
        which answered *"the source declared it optional"* for a producer that had
        said nothing.

        Asserted as **absence of the key**, not as ``field["nullable"] is None``:
        the latter would pass on the very shape do-NOT 17 forbids.

        **Validates: Requirements 5.3, 6.1**
        """
        field = _emitted_field("glue_catalog", dict(_UNSTATED_FIELD))

        assert _SUBJECT not in field, (
            f"R5.3: a producer that stated no nullability must reach the consumer with no {_SUBJECT!r} "
            f"key — got {field.get(_SUBJECT)!r}. Do NOT reinstate the `.get(..., True)` default "
            f"(`LGR-4854b3d-1`, do-NOT 5)."
        )
        _assert_conforms(field, _field_interface())

    @pytest.mark.parametrize("stated", [True, False])
    def test_a_stated_field_carries_the_key_and_still_conforms(self, stated: bool):
        """R5.2/R5.3: a stated value arrives, as the ``boolean`` the interface declares.

        Both booleans, because ``false`` is the state that matters most: it is the
        one an omission-by-default would have overwritten, and the one that makes
        a field read as required. ``is`` rather than ``==``, so a truthy stand-in
        fails — and ``matches_json_type`` inside ``_assert_conforms`` refuses a
        non-``bool`` for a ``boolean`` field.

        **Validates: Requirements 5.3, 6.1**
        """
        field = _emitted_field("s3_table_bucket", _stating_field(stated))

        assert field[_SUBJECT] is stated, (
            f"a stated {_SUBJECT!r} must cross the wire unchanged — stated {stated!r}, got {field.get(_SUBJECT)!r}"
        )
        _assert_conforms(field, _field_interface())

    def test_neither_case_emits_null_for_the_key(self):
        """do-NOT 17 across the wire: the consumer refuses ``null`` and the producer sends none.

        The pairing is the claim. ``RawSchemaDetailField.nullable`` is a bare
        ``boolean``, so a ``null`` would be a value the consumer's type does not
        admit; the producer emits none on either branch, including for an input
        that carries an explicit ``None``. Absence is the spelling for "not
        established" (``LGR-44d601e-1``); three spellings of two facts is the
        defect, not the fix.

        **Validates: Requirements 5.3, 6.3**
        """
        _, declared = _field_interface()[_SUBJECT]
        assert not declares_null(declared), (
            f"guard: this claim is only meaningful while {_FIELD_INTERFACE}.{_SUBJECT} refuses null; "
            f"it is declared {declared!r}"
        )

        cases = {
            "stated nothing": ("glue_catalog", dict(_UNSTATED_FIELD)),
            "stated true": ("s3_table_bucket", _stating_field(True)),
            "stated false": ("s3_table_bucket", _stating_field(False)),
            "carried an explicit None": ("glue_catalog", {**_UNSTATED_FIELD, _SUBJECT: None}),
        }
        for label, (source, input_field) in cases.items():
            emitted = _emitted_field(source, input_field).get(_SUBJECT, _ABSENT)
            assert emitted is not None, (
                f"a field that {label} emitted {_SUBJECT}=None — a `null` the consumer's `boolean` "
                f"cannot hold, and a third spelling of two facts (do-NOT 17, `LGR-44d601e-1`)"
            )
            assert emitted is _ABSENT or isinstance(emitted, bool), (
                f"a field that {label} emitted {_SUBJECT}={emitted!r}; the only values this key may "
                f"carry are the two booleans, and its only other state is absence"
            )

    def test_the_two_emitted_shapes_differ_by_the_subject_alone(self):
        """R6.1: the optional key is the *only* optionality the two sides negotiate.

        A stated field and an unstated one, both through ``_schema_detail``,
        differ by exactly ``{"nullable"}``. That is what makes the interface's
        single ``?`` an accurate description of the wire rather than an
        approximation of it: a second conditional per-field key would mean the
        consumer's type understates what may be missing, and the frontend would
        read ``undefined`` somewhere it declared a value.

        Derived from the two **observed** key sets, so it fails if either branch
        grows a key — rather than restating how the fixtures were written.

        **Validates: Requirements 6.1, 6.3**
        """
        unstated = set(_emitted_field("glue_catalog", dict(_UNSTATED_FIELD)))
        stated = set(_emitted_field("s3_table_bucket", _stating_field(True)))

        assert stated ^ unstated == {_SUBJECT}, (
            f"the two emitted shapes must differ by {_SUBJECT!r} alone — they differ by "
            f"{sorted(stated ^ unstated)}. Every other key of {_FIELD_INTERFACE} is declared required, "
            f"so a second conditional key breaks the consumer's type rather than the producer's."
        )
        interface = _field_interface()
        assert unstated <= set(interface) and stated <= set(interface), (
            f"both shapes must be subsets of {_FIELD_INTERFACE}'s declared keys "
            f"({sorted(interface)}); got {sorted(unstated)} and {sorted(stated)}"
        )
