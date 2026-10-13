# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the Unsourced_Keys are the same two names on both sides of the wire.

Feature: p45-unmapped-target-field-visibility, task 21.1.
_Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_

The **producer** is ``create_approval_payload`` in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py``, whose
``unmapped_fields`` dict literal (``:1391-1397``) carries four keys, two of them
added by p37 R3.2 and consumed by p45:

- ``target_unsourced`` — every unsourced target field, required ones first.
- ``target_required`` — the subset the target schema declares ``required``.

The **consumer** is ``UnmappedFieldsPayload`` in
``dashboard-react/src/types.ts`` (``:715``), applied at its point of use rather
than by widening ``Approval.details`` — ``ApprovalArtifact.tsx:151`` and
``RunDetailPage.tsx:294`` both coerce locally before handing the payload to
``unsourcedTargetFields``. That narrowing-at-the-point-of-use shape is
``LGR-69ca78b-4`` (**template / ratified**), and the interface's doc comment
names this file as the guard a rename has to get past.

THE FAILURE THIS EXISTS FOR IS A RENAME (R6.5)
----------------------------------------------
Nothing breaks loudly when the two sides stop agreeing on a name. Every key on
``UnmappedFieldsPayload`` is optional, so a producer-side rename makes the
consumer read ``undefined``, fall to R1.4's branch and report "required-ness not
established" for every field on every run — a panel that is confidently wrong
rather than one that errors. There is no type error, no runtime error and no
empty state to notice; the screen keeps rendering a sentence it can no longer
support.

THE DIRECTION ASSERTED, AND WHY IT IS NOT SYMMETRIC
---------------------------------------------------
Every key the **interface declares** must appear in the producer's literal.
The reverse is deliberately not asserted, on
``test_not_verified_reason_labels_cross_language.py``'s stated ground: a
producer key the interface does not declare is **inert** — ``details`` is
``Record<string, unknown>`` and ``unsourcedTargetFields`` reads only the keys it
knows, so an undeclared key renders nothing — while an interface key no producer
writes **degrades a live screen**. Asserting Python ⊆ TypeScript would also fail
on a key staged in the interface ahead of its producer, and narrowing the
frontend to suit a Python reader is what ``LGR-99050e6-5``
(**template / ratified**) rejects outright.

One subset assertion catches a rename on **either** side, which is the whole of
R6.5: rename the producer's key and the interface's declaration goes unwritten;
rename the interface's key and the producer's write goes undeclared. Both land
in the same missing set.

WHERE EACH SIDE'S EXPECTATION COMES FROM (R6.1, C2, do-NOT 7)
-------------------------------------------------------------
Neither side is transcribed. The TypeScript key set is parsed out of
``types.ts`` at test time and the Python key set out of ``lambda_function.py``,
so a test that passed while both sides were wrong in the same way is not
reachable — the failure ``LGR-28b4d75-5`` (**template / ratified**) names, where
a contract test written from the producer's own output cannot detect the
producer drifting because that output is its own oracle.

The two key names **are** written down here, once each, as
``_UNSOURCED_KEYS``. That identifies the guard's *subject*, not its
*expectation*: it is what makes "this test's subject moved" a failure instead of
a silent narrowing of scope. Same move, same purpose, as
``test_not_verified_reason_labels_cross_language.py``'s
``executed_run_reason = "no_target_schema"`` and its "is no longer a declared
reason; this test's subject moved" guard.

WHY THE SHARED READER IS USED HERE AND A LOCAL COPY IS NOT (R6.2)
-----------------------------------------------------------------
``UnmappedFieldsPayload`` **is** an ``export interface`` — the one construct
``helpers.read_interface`` models — so ``load_interfaces`` reads it directly and
R6.2 is satisfied literally, with no second copy of the parser.
``LGR-99050e6-5`` (**template / ratified**) extracted that reader precisely
because per-endpoint copies "would have diverged immediately".

This is where p45 parts company with the file R6.3 names as its model.
``test_non_blocking_severities_cross_language.py`` and
``test_not_verified_reason_labels_cross_language.py`` both hand-roll a regex
(``:156``, ``:161`` in the first), and both say why in their own docstrings —
``test_non_blocking_severities_cross_language.py:73``:
*"``api_responses/helpers.py``'s frontend-type oracle is not reusable: it parses
``export interface`` declarations, and this subject is a ``const`` object
literal."* p45's subject is the other case, so the shared reader applies and the
``const``-literal technique does not transfer. What carries over from R6.3's
model is its **discipline** — the ``ast`` read below and the four anti-vacuity
guards (design §4.9a).

WHY THE PYTHON SIDE IS READ WITH ``ast`` — NOT REGEX, NOT IMPORT
----------------------------------------------------------------
``LGR-b46a5bb-4`` (**template / ratified**) rejected substring matching for a
repository-wide invariant test, and its recorded reason is not abstract: the
scan "forbids documenting the fence, and failed for exactly that reason against
handlers/agents.py, whose docstring names threading.Thread on purpose". The same
hazard is present here, and measurably: ``target_unsourced`` occurs 319 times
across this repository, and **inside the producer file it occurs exactly twice**
— once in the literal at ``:1395`` and once in the function's own docstring at
``:1336``. A substring scan of that file cannot tell those two apart, so it would
keep reporting the key as written after a rename moved only the source.

Importing the module and reading the returned dict is rejected for two separate
reasons. It would make the producer its own oracle (C2, do-NOT 7) — the dict
would then agree with itself by construction. And it would execute producer code
as pytest collects this file: ``lambda_function`` resolves four table names
through ``require_env`` at module scope (``:78-83``) and builds three boto3
clients (``:105-107``), which is why the sibling contract tests that do import a
handler carry a path-and-environment bootstrap block —
``test_metrics_stage_keys_contract.py:119-148``, ahead of its handler import at
``:150``. ``ast`` needs none of that, mutates no ``sys.modules``, and cannot be
fooled by prose.

HOW THE PARSE IS KEPT NON-VACUOUS — FOUR GUARDS
-----------------------------------------------
A source-parsing test's whole risk is passing on an empty set. An empty
``ts_keys`` makes ``ts_keys <= python_keys`` vacuously **true**, so this is the
live hazard rather than a theoretical one. Four guards, following
``test_not_verified_reason_labels_cross_language.py``'s model:

1. Both source files must exist (``test_both_contract_sources_are_readable``) —
   a moved or renamed file fails loudly instead of skipping.
2. Both parses must be non-empty and must still contain the two subject keys,
   each with a message naming what changed
   (``test_parsed_key_sets_are_neither_empty_nor_missing_the_subject``).
3. The ``ast`` walk must find the dict literal or **raise**, and the interface
   reader likewise — exercised against deliberately mutated copies of the real
   sources (``TestTheOraclesThemselves``), because an oracle that quietly
   returns an empty set makes every assertion built on it pass.
4. The interface must still declare both keys **optional**
   (``test_both_unsourced_keys_are_still_declared_optional``). That is the guard
   ``LGR-99050e6-5`` describes as asserting "the interface still declares that
   field required and nullable so a future narrowing fails loudly", applied to
   the spelling optionality actually has here: these keys are optional by ``?``,
   not by a ``| null`` union, so ``required_fields`` is the instrument and
   ``declares_null`` — which tests for the union — would pass on any of them
   vacuously and pin nothing.

NOT ASSERTED HERE
-----------------
**The values of ``target_unsourced`` for a given mapping** (R6.6). That is
``unsourced_target_field_names``' own behaviour, and
``tests/unit/lambdas/schema_mapping/test_schema_mapping_approval.py`` already
covers it — four tests at ``:561``, ``:575``, ``:595`` and ``:636``, running to
the end of that file at ``:648``, over the payload's naming, its required-first
ordering and its ``requires_review`` term (R7.2). This file is about the
language seam only: two names, and whether both sides still spell them the same
way.

**The second TypeScript declaration of these key names.**
``utils/unsourced-target-fields.ts:116-120`` declares
``UnsourcedTargetFieldsInput`` with the same two keys. It is deliberately local,
unexported and reduced to the keys that function reads, on its own stated ground
that "declaring a second exported interface of the same shape here would be the
second declaration that guard exists to prevent". So ``UnmappedFieldsPayload``
is the named contract and the only subject here — do not widen this file to pin
the structural one as well, which would create the very second authority both
sides went out of their way to avoid.

**Validates: Requirements 6.1, 6.2, 6.3, 6.4, 6.5, 6.6**
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from .helpers import load_interfaces, read_interface, required_fields

# tests/contracts/api_responses/<this file> -> tests -> agentic_data_ingestion.
# Same idiom as the sibling test_metrics_stage_keys_contract.py:120-121.
_REPO_TESTS = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_TESTS.parent

_TYPES_TS = _PROJECT_ROOT / "dashboard-react" / "src" / "types.ts"
_PRODUCER = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_schema_mapping" / "lambda_function.py"

#: The consumer interface. Renaming it fails `read_interface`'s own "not found"
#: assertion, which is guard 1 for the TypeScript half.
_INTERFACE_NAME = "UnmappedFieldsPayload"

#: The producer function and the key its `unmapped_fields` literal hangs from.
#: The walk is scoped to the function so `store_mapping_result`'s unrelated
#: `"unmapped_fields": unmapped_count` (a COUNT, not this payload) cannot be
#: mistaken for it.
_PRODUCER_FUNCTION = "create_approval_payload"
_PAYLOAD_KEY = "unmapped_fields"

#: The guard's SUBJECT, not its expectation — see the docstring. Both sides are
#: parsed; these two names only assert that the thing being guarded still exists
#: to be guarded.
_UNSOURCED_KEYS = frozenset({"target_unsourced", "target_required"})


def _interface_fields():
    """``{field: (required, ts_type)}`` for ``UnmappedFieldsPayload``.

    ``load_interfaces`` already asserts the file exists and that the interface
    parsed to a non-empty field set, so a moved ``types.ts`` or a renamed
    interface fails here rather than quietly reducing this test to a no-op.
    """
    return load_interfaces(_TYPES_TS, _INTERFACE_NAME)[_INTERFACE_NAME]


def _producer_keys_from_source(source: str, source_path: Path | str = "<source>") -> set[str]:
    """The string keys of ``create_approval_payload``'s ``unmapped_fields`` literal.

    Takes the source as text rather than reading the file, so the oracle can be
    exercised against mutated copies of the real thing (``TestTheOraclesThemselves``).

    Raises:
        AssertionError: if the function is absent, defined more than once, or
            has no ``unmapped_fields`` dict literal — or if that literal has a
            key this reader cannot resolve to a string. Every one of those is a
            failure of the oracle itself and must be loud: returning an empty
            set would make the subset assertion below vacuously true.
    """
    module = ast.parse(source, filename=str(source_path))

    functions = [
        node
        for node in ast.walk(module)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == _PRODUCER_FUNCTION
    ]
    assert functions, (
        f"{_PRODUCER_FUNCTION} not found in {source_path} — the approval payload's producer was renamed or "
        f"moved, so the Unsourced_Keys the frontend reads can no longer be checked against what it writes. "
        f"Repoint this reader rather than deleting the assertion: an unchecked rename renders "
        f"'required-ness not established' for every field on every run."
    )
    assert len(functions) == 1, (
        f"{_PRODUCER_FUNCTION} is defined {len(functions)} times in {source_path}. Only the last definition "
        f"is live at runtime, so this reader cannot tell which literal the approver actually sees."
    )

    literals: list[ast.Dict] = []
    for node in ast.walk(functions[0]):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if isinstance(key, ast.Constant) and key.value == _PAYLOAD_KEY and isinstance(value, ast.Dict):
                literals.append(value)

    assert literals, (
        f"{_PRODUCER_FUNCTION} in {source_path} has no {_PAYLOAD_KEY!r} dict literal. Either the key was "
        f"renamed, or the payload is now assembled somewhere this reader cannot see — a helper call, a "
        f"comprehension, a merge. Both cases mean the frontend's declared keys are no longer checked "
        f"against anything, so fix the reader instead of relaxing it."
    )
    assert len(literals) == 1, (
        f"{_PRODUCER_FUNCTION} in {source_path} has {len(literals)} {_PAYLOAD_KEY!r} dict literals. This "
        f"reader cannot tell which one reaches the approver, and picking either would be a guess."
    )

    keys: set[str] = set()
    for key in literals[0].keys:
        if key is None:
            raise AssertionError(
                f"{_PAYLOAD_KEY!r} in {source_path} contains a `**` unpacking, so some of its keys come "
                f"from a value this reader cannot see statically. The subset assertion would be incomplete "
                f"without failing — resolve the spread here, or write the keys out in the literal."
            )
        if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
            raise AssertionError(
                f"{_PAYLOAD_KEY!r} in {source_path} has a computed key ({ast.dump(key)}) that this reader "
                f"cannot resolve to a string. A key the wire carries but this test cannot name is exactly "
                f"the drift it exists to catch."
            )
        keys.add(key.value)

    assert keys, (
        f"{_PAYLOAD_KEY!r} in {source_path} parsed to ZERO keys. The literal was found but is empty, so "
        f"every assertion below would pass against nothing."
    )
    return keys


def _producer_keys() -> set[str]:
    """The producer's ``unmapped_fields`` keys, read off disk."""
    return _producer_keys_from_source(_PRODUCER.read_text(encoding="utf-8"), _PRODUCER)


# ═══════════════════════════════════════════════════════════════════════════
# Guard 1 — the sources exist
# ═══════════════════════════════════════════════════════════════════════════


def test_both_contract_sources_are_readable():
    """A moved or renamed source must fail loudly, not skip.

    Both halves of the seam are named, so a failure says which side moved
    without the reader having to be run first.
    """
    assert _TYPES_TS.is_file(), (
        f"the consumer types are not at {_TYPES_TS}. This test derives its expectation from "
        f"{_INTERFACE_NAME} there; repoint the constant rather than deleting the assertion."
    )
    assert _PRODUCER.is_file(), (
        f"the approval payload's producer is not at {_PRODUCER}. Repoint the constant rather than deleting "
        f"the assertion — an unchecked producer is how a rename reaches the approver's screen."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Guard 2 — neither parse is empty, and both still carry the subject
# ═══════════════════════════════════════════════════════════════════════════


def test_parsed_key_sets_are_neither_empty_nor_missing_the_subject():
    """Anti-vacuity: both parses recover real keys, including the two guarded ones.

    Every guard inside the two readers runs here on its own, so a degraded
    extraction is reported as *this* test failing rather than as the contract
    test below passing over an empty set. An empty ``ts_keys`` in particular
    would make the subset assertion vacuously true.
    """
    ts_keys = set(_interface_fields())
    python_keys = _producer_keys()

    assert len(ts_keys) > 1, (
        f"{_INTERFACE_NAME} parsed to {sorted(ts_keys)} — too few fields to be the real interface; the "
        f"reader is matching one line and missing the rest."
    )
    assert len(python_keys) > 1, (
        f"{_PAYLOAD_KEY!r} parsed to {sorted(python_keys)} — too few keys to be the real payload; the ast "
        f"walk found a literal but not the one the approver sees."
    )

    missing_from_ts = _UNSOURCED_KEYS - ts_keys
    assert not missing_from_ts, (
        f"{_INTERFACE_NAME} no longer declares {sorted(missing_from_ts)}; this test's subject moved. Those "
        f"two keys ARE the guard — with them gone the contract test below still passes and pins nothing. "
        f"If the frontend genuinely stopped consuming them, delete this file deliberately and say so."
    )
    missing_from_python = _UNSOURCED_KEYS - python_keys
    assert not missing_from_python, (
        f"{_PRODUCER_FUNCTION} no longer writes {sorted(missing_from_python)}; this test's subject moved. "
        f"Those keys are p37 R3.2's derived names, and the approval panel has nothing to read without them."
    )


# ═══════════════════════════════════════════════════════════════════════════
# Guard 3 — both oracles raise rather than returning an empty set
# ═══════════════════════════════════════════════════════════════════════════


class TestTheOraclesThemselves:
    """Each reader is exercised against mutated copies of the real source.

    An oracle that silently returns an empty set makes every assertion built on
    it pass, so neither reader is trusted until it has been shown to fail on a
    source it cannot read. The mutations assert their own arithmetic first: a
    substitution that stopped matching would make these tests pass for the wrong
    reason, which is the same vacuity one level up.
    """

    def test_the_ast_walk_raises_when_the_payload_key_is_renamed(self):
        source = _PRODUCER.read_text(encoding="utf-8")
        needle = f'"{_PAYLOAD_KEY}": {{'
        assert source.count(needle) == 1, (
            f"expected exactly one {needle!r} in the producer, found {source.count(needle)} — this "
            f"mutation no longer targets the literal, so the test below proves nothing."
        )
        mutated = source.replace(needle, '"unmapped_fieldz": {')

        with pytest.raises(AssertionError, match=f"no {_PAYLOAD_KEY!r} dict literal"):
            _producer_keys_from_source(mutated, "<mutated producer>")

    def test_the_ast_walk_raises_when_the_producer_function_is_renamed(self):
        source = _PRODUCER.read_text(encoding="utf-8")
        needle = f"def {_PRODUCER_FUNCTION}("
        assert source.count(needle) == 1, (
            f"expected exactly one {needle!r} in the producer, found {source.count(needle)}."
        )
        mutated = source.replace(needle, "def build_the_approval_payload(")

        with pytest.raises(AssertionError, match=f"{_PRODUCER_FUNCTION} not found"):
            _producer_keys_from_source(mutated, "<mutated producer>")

    def test_the_ast_walk_reports_a_renamed_inner_key_as_absent(self):
        """The rename R6.5 is about, seen from the producer side."""
        source = _PRODUCER.read_text(encoding="utf-8")
        needle = '"target_unsourced": '
        assert source.count(needle) == 1, (
            f"expected exactly one {needle!r} in the producer, found {source.count(needle)}."
        )
        mutated = source.replace(needle, '"target_unsourced_fields": ')

        keys = _producer_keys_from_source(mutated, "<mutated producer>")
        assert "target_unsourced" not in keys, (
            "the ast reader still reports `target_unsourced` after the producer renamed it — it is reading "
            "the name from somewhere other than the literal, most likely from prose."
        )
        assert "target_unsourced_fields" in keys, "the ast reader did not pick up the renamed key at all."

    def test_the_interface_reader_raises_when_the_interface_is_renamed(self):
        source = _TYPES_TS.read_text(encoding="utf-8")
        needle = f"export interface {_INTERFACE_NAME} {{"
        assert source.count(needle) == 1, f"expected exactly one {needle!r} in types.ts, found {source.count(needle)}."
        mutated = source.replace(needle, f"export interface {_INTERFACE_NAME}Renamed {{")

        with pytest.raises(AssertionError, match=f"{_INTERFACE_NAME} not found"):
            read_interface(mutated, _INTERFACE_NAME, "<mutated types.ts>")

    def test_the_interface_reader_reports_a_renamed_field_as_absent(self):
        """The same rename, seen from the consumer side."""
        source = _TYPES_TS.read_text(encoding="utf-8")
        needle = "  target_unsourced?: string[];"
        assert source.count(needle) == 1, (
            f"expected exactly one {needle!r} in types.ts, found {source.count(needle)} — "
            f"{_INTERFACE_NAME}'s field shape changed and this mutation no longer targets it."
        )
        mutated = source.replace(needle, "  targetUnsourced?: string[];")

        fields = read_interface(mutated, _INTERFACE_NAME, "<mutated types.ts>")
        assert "target_unsourced" not in fields, (
            "the interface reader still reports `target_unsourced` after the declaration was renamed — it "
            "is reading the name from prose rather than from the declaration. That trap is live, not "
            "hypothetical: the doc comment at types.ts:679 names all four keys, so a reader that scanned "
            "the file instead of parsing the interface body would pass this rename unnoticed."
        )
        assert "targetUnsourced" in fields, "the interface reader did not pick up the renamed field at all."


# ═══════════════════════════════════════════════════════════════════════════
# Guard 4 — the keys are still optional on the interface
# ═══════════════════════════════════════════════════════════════════════════


def test_both_unsourced_keys_are_still_declared_optional():
    """Optional by ``?``, so a future narrowing fails loudly.

    ``LGR-99050e6-5`` (**template / ratified**) built exactly this guard for the
    field it refused to narrow — "a guard now asserts the interface still
    declares that field required and nullable so a future narrowing fails
    loudly". The property is the same and only its spelling differs: these keys
    are optional by ``?`` rather than by a ``| null`` union, so
    ``required_fields`` is the instrument and ``declares_null`` would pass on
    every one of them without pinning anything. The interface's own doc comment
    asks for it in exactly those terms — "the ``declares_null``-shaped
    anti-vacuity guard ``LGR-99050e6-5`` built" — so this is the guard task 7.1
    specified, not a substitution invented here.

    Optionality is the wire truth, not a convenience, and ``types.ts`` names
    three live causes of a payload arriving here without the keys: p37 is not
    deployed (C15), mapping records predate p37, and
    ``create_approval_payload``'s degraded return carries no ``unmapped_fields``
    at all — measured at ``lambda_function.py:1433-1437``, three keys only
    (``approval_type``, ``error``, ``generated_by``); the doc comment in
    ``types.ts`` cites ``:1435-1439``, which is two lines off. A consumer that
    declared either key required
    would be asserting a guarantee the wire does not give. That degraded return
    is explicitly NOT pinned as expected behaviour here (do-NOT 10, C9).
    """
    fields = _interface_fields()
    declared_required = required_fields(fields) & _UNSOURCED_KEYS

    assert not declared_required, (
        f"{_INTERFACE_NAME} now declares {sorted(declared_required)} as required (no `?`). The wire does "
        f"not guarantee either key: {_PRODUCER_FUNCTION} returns a degraded payload when its assembly "
        f"raises, and approval records written before p37 carry neither. Restore the `?` rather than "
        f"loosening this test — do-NOT 6 and LGR-99050e6-5 forbid narrowing a frontend type to suit a "
        f"reader, and this direction of the same edit makes the consumer claim a guarantee it lacks."
    )


# ═══════════════════════════════════════════════════════════════════════════
# The contract (R6.5)
# ═══════════════════════════════════════════════════════════════════════════


def test_every_interface_declared_key_is_written_by_the_producer():
    """``UnmappedFieldsPayload``'s fields ⊆ ``unmapped_fields``' keys.

    **Validates: Requirements 6.1, 6.5**
    """
    ts_keys = set(_interface_fields())
    python_keys = _producer_keys()

    unwritten = ts_keys - python_keys
    assert not unwritten, (
        f"{_INTERFACE_NAME} declares {sorted(unwritten)}, which {_PRODUCER_FUNCTION} does not write. The "
        f"consumer reads `undefined` for each — silently: every field on the interface is optional, so "
        f"there is no type error and no runtime error. The approval panel falls to R1.4's branch and "
        f"reports 'required-ness not established' for every field on every run, which is worse than an "
        f"empty panel because it is a confident claim. Rename the key back on whichever side moved, or "
        f"change BOTH sides in one commit. Do NOT delete the interface field to make this pass: that is "
        f"the narrowing LGR-99050e6-5 rejects, and it would hide the drift instead of fixing it."
    )


# The expected unwritten key differs by side, and the difference is the point.
# Rename the PRODUCER's key and the interface's original name goes unwritten.
# Rename the INTERFACE's field and its NEW name goes unwritten — the producer
# never knew it. Either way the key lands in `ts_keys - python_keys`, which is
# why one subset assertion covers both directions.
@pytest.mark.parametrize(
    ("side", "needle", "replacement", "unwritten"),
    [
        ("producer", '"target_unsourced": ', '"target_unsourced_fields": ', "target_unsourced"),
        ("consumer", "  target_unsourced?: string[];", "  targetUnsourced?: string[];", "targetUnsourced"),
    ],
)
def test_a_rename_on_either_side_breaks_the_contract(side, needle, replacement, unwritten):
    """R6.5 made a demonstration rather than an argument.

    The two oracle tests above prove each reader *tracks* a rename; this proves
    the subset assertion above *fails* because of it, from either direction. It
    matters that this is checked rather than reasoned about: a guard that cannot
    be shown to bite is indistinguishable from one that has quietly stopped
    biting.

    **Validates: Requirements 6.5**
    """
    path = _PRODUCER if side == "producer" else _TYPES_TS
    source = path.read_text(encoding="utf-8")
    assert source.count(needle) == 1, (
        f"expected exactly one {needle!r} in {path.name}, found {source.count(needle)} — this mutation no "
        f"longer targets the declaration, so the demonstration below proves nothing."
    )
    mutated = source.replace(needle, replacement)

    if side == "producer":
        ts_keys = set(_interface_fields())
        python_keys = _producer_keys_from_source(mutated, "<mutated producer>")
    else:
        ts_keys = set(read_interface(mutated, _INTERFACE_NAME, "<mutated types.ts>"))
        python_keys = _producer_keys()

    assert ts_keys - python_keys == {unwritten}, (
        f"a {side}-side rename of `target_unsourced` did NOT leave {unwritten!r} in the unwritten set "
        f"({sorted(ts_keys - python_keys)}). The contract test above would still pass over a payload the "
        f"consumer cannot read, which is the whole failure R6.5 exists to catch."
    )
