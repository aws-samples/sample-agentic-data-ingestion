# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: every format ``file_validation`` can emit has an extractor.

**This is the assertion whose absence allowed the p46 defect.** ``FORMAT_MAP``
claimed ``avro`` and ``orc``, the Extraction_Dispatch had no branch for either,
and nothing noticed. Both fell to the ``else``, which handed a binary container
file to a JSON reader that found no complete records and returned
``{"type": "object", "properties": {}}`` instead of raising — so the run
**succeeded** at ``schema_extract`` declaring zero fields and continued to
``schema_mapping`` with nothing to map. Every other task in p46 fixes that
instance. This one stops the class recurring.

Three Lambdas have to agree on one vocabulary and none can import another:

- The **producer**, ``lambda/agentic_data_ingestion_file_validation/lambda_function.py``,
  declares the Canonical_Format vocabulary as ``FORMAT_MAP``'s **values** —
  **eleven** extension keys collapsing to **six** distinct formats (p55 R6.1
  added ``xlsx → "excel"``, taking nine keys and five formats to ten and six;
  p64 R3.1 added ``txt → "csv"``, which adds a key and **no** new value, so the
  value set stayed at six). ``detect_format`` returns one of them, or
  ``FORMAT_MAP.get``'s ``"unknown"`` default.

  Both amendments are why every count here is prose and every *assertion* below
  is derived: nothing in this file names a format literal as an expectation, so a
  stale sentence in this docstring cannot make a guard pass.
- The **consumer**, ``lambda/agentic_data_ingestion_schema_extract/lambda_function.py``,
  dispatches on that value in ``lambda_handler``'s ``if``/``elif`` chain and
  selects an extractor — and, for a Conversion_Required_Set member, a converter.
- The **mirror**, ``lambda/agentic_data_ingestion_test_code/glue_session_runner.py``'s
  ``_SOURCE_FORMAT_BY_EXTENSION``, is a deliberate duplicate of ``FORMAT_MAP``
  whose own docstring says "**change both or neither**". p55 R5.1/R5.2 make that
  instruction executable here rather than leaving it as a comment — see the
  mirror guard below.

They are separate Lambda bundles with separate ``sys.path`` roots and their own
module-level boto3 clients, so none can import another's constant. There
is no shared home for the vocabulary either: ``FORMAT_MAP`` lives beside
``NATIVE_FORMATS`` in the Lambda that owns detection, and the other two know
the values only as string literals in their own source.

THE CONTRACT, AND WHY IT IS ASSERTED IN BOTH DIRECTIONS
-------------------------------------------------------
**Forward (R3.3, the required half).** Every value of ``FORMAT_MAP`` has a
branch in the dispatch chain. A format the producer can emit and the consumer
cannot route is the p46 defect exactly.

**Inverse (this file's own judgement, not required by R3.3).** Every literal in
the dispatch chain is a value ``FORMAT_MAP`` can produce. A branch for a format
detection cannot emit is dead code that reads as capability — the same
misinformation as the forward gap, pointed the other way. ``LGR-60c4985-7``
(template / ratified) rejected adding ``.xlsx`` to ``FORMAT_MAP`` because
*"nothing in the Lambda reads Excel"*; the inverse assertion is that argument's
mirror, and it is what makes the pair an equality rather than a subset.

The inverse is scoped honestly rather than narrowly: ``jdbc`` **is** a
``source_format`` this handler receives, and ``FORMAT_MAP`` cannot produce it,
because a JDBC pipeline has no file to detect. It is dispatched by an early
guard on ``event.get("source_format")`` before the file-format chain is
reached, so it contributes no chain literal.
``test_the_jdbc_source_format_is_dispatched_outside_the_chain`` asserts that
placement, so the inverse assertion's scope is a checked fact and not an
accident of where the guard happens to sit today.

WHY THE VALUE SET IS DERIVED AND NOT RESTATED
---------------------------------------------
``LGR-28b4d75-5`` (template / ratified) is the precedent and the reason:
*"a test written from the producer's output cannot detect the producer
drifting, because the output is its own oracle."* A transcribed list of five
format names would pass forever while ``FORMAT_MAP`` grew a sixth — which is
precisely the drift that produced the defect, since the repository already
contained tests asserting ``avro`` and ``orc`` were *detected* while nothing
extracted them.

So the expectation is read out of ``FORMAT_MAP``'s own declaration. Nothing in
this file names a format literal as an expectation.
``tests/unit/lambdas/schema_extract/test_streaming_schema.py`` does restate the
format → extractor map, deliberately and with a comment saying so: it is
testing *routing behaviour* per format and needs a per-case oracle. This file
owns the derived form of the vocabulary claim (design Property 3), and the two
are complementary rather than duplicative — behaviour there, completeness here.

WHY THE DISPATCH IS READ BY ``ast`` AND NOT BY SUBSTRING
-------------------------------------------------------
``LGR-b46a5bb-4`` (template / ratified) rejected substring matching for a
repository invariant test because *"it forbids documenting the fence, and
failed for exactly that reason against handlers/agents.py, whose docstring
names threading.Thread on purpose."*

The same applies here, and not hypothetically. ``schema_extract``'s dispatch
now carries an extensive block comment at its defensive ``else`` explaining
what used to land there and why it no longer does — prose that names binary
container formats on purpose. A substring check for ``"avro"`` in that
function's source would pass on the comment alone, i.e. it would have passed
**before task 6.1 added the branches**, which makes it worse than no test.
``ast`` sees only the ``if``/``elif`` conditions, so a branch either exists as
source or it does not.

The parse is source-text, not an import, matching the rest of this directory.
Importing either module needs that Lambda's full environment and would
construct module-level boto3 clients and DynamoDB resources inside a contract
test; ``ast`` needs no stub dict and mutates no ``sys.modules``.

WHAT IS DELIBERATELY **NOT** ASSERTED
-------------------------------------
R3.2's *"or its comment"* half. The requirement says the ``else`` branch must
not name ``avro`` or ``orc`` in its comment or its behaviour, and only the
behaviour half is checked here — structurally, by both formats having their own
branch ahead of the ``else``, which makes the ``else`` unreachable for them.

Comments are absent from the AST by construction, so asserting the other half
would mean going back to text matching over a comment block, and forbidding a
future maintainer from writing "avro and orc used to land here" is the exact
failure ``LGR-b46a5bb-4`` records. ``tests/unit/lambdas/schema_extract/test_streaming_schema.py``
(task 6.2) left the same half alone for the same reason.

HOW THE PARSE IS KEPT NON-VACUOUS
---------------------------------
A source-parsing test's whole risk is passing on an empty set: an empty set of
chain literals makes the forward assertion fail loudly, but an empty
``FORMAT_MAP`` value set would make it pass vacuously. **Eight** guards (four
until p55 task 4.5, six until p55 task 6.2), each its own test so a degraded
extraction is reported as *that* test failing rather than as the contract below
passing over nothing:

1. All **four** source files must exist — a moved or renamed Lambda, or a
   relocated dashboard component, fails loudly instead of skipping.
2. Both extractions must be non-empty, with a message naming what changed.
3. ``NATIVE_FORMATS``, the sibling literal in the producer, must be a subset of
   the extracted values. It is an independent corroboration of the value
   extraction: a parse that silently recovered a subset of ``FORMAT_MAP``
   fails here. **Amended by p55 — see the section below for which of this
   guard's two roles survives and which weakens.**
4. Every chain branch must reach an ``extract_schema_from_*`` call, so a branch
   that exists and does nothing cannot satisfy the vocabulary contract — the
   claimed-but-unhandled defect in a new costume.
5. *(p55 R13.6/R13.7)* Every member of the Conversion_Required_Set —
   ``set(FORMAT_MAP.values()) - NATIVE_FORMATS`` — must reach a
   ``convert_source_to_*`` call in its dispatch branch, and that complement must
   be non-empty. Guard 4's shape pointed at the other population.
6. *(p55 R5.1/R5.2)* The Format_Vocabulary_Mirror must be extension-for-extension
   the same mapping as ``FORMAT_MAP``.
7. *(p55 R6.6)* The Upload_Modal's ``ACCEPTED_FORMATS`` must be ``FORMAT_MAP``'s
   **key** set, dot-prefixed — set equality, both directions. A superset is
   ``requirements.md``'s opening defect (*"frontend accepts what the backend
   rejects"*); a subset hides a format this system supports end to end, which is
   what ``jsonl``, ``ndjson``, ``pq``, ``avro`` and ``orc`` all were until 6.2.
   This is the first guard here to read a **TypeScript** source, by regex rather
   than ``ast`` — see :data:`UPLOAD_MODAL_SOURCE`.
8. *(p55 R6.6)* Those entries must be dot-prefixed and lower case, because both
   readers of the set — ``validateFile`` and the input's ``accept`` attribute —
   depend on that spelling, and an entry that matches nothing is a silent subset
   guard 7 cannot tell from a deliberate one.

AMENDED BY p55 TASK 4.5 — GUARD 3'S TWO ROLES, SPLIT HONESTLY
-------------------------------------------------------------
``NATIVE_FORMATS`` was ``{"parquet", "avro", "orc"}`` with no production
consumer. p55 R13 redefines it as *every Canonical_Format a Glue_Job_Template
reads directly from the source object, with no prior conversion of that object*,
which makes it ``{"csv", "json", "parquet", "avro", "orc"}`` and gives it a real
consumer in ``schema_extract``. Guard 3 played two roles and they do not fare
alike, so both are stated rather than one being quietly dropped
(``LGR-60c4985-6`` / ``LGR-9e76249-10``, both **template / ratified**: a
deliberate change to an assertion stays legible as a change and is never
deleted).

* **The extraction-corroboration role survives intact, and is slightly
  stronger.** It rests on two facts, neither of which depends on the set having
  a production consumer: the set is parsed by a *different* code path
  (:func:`_native_formats`, asserting ``ast.Set``) from the values
  (:func:`_format_map_values`, asserting ``ast.Dict``), and a degraded
  ``FORMAT_MAP`` parse that drops any member still fails. After the widening
  **five** members must be found rather than three.
* **The semantic role weakens, and that is the honest loss.** Once the set is
  ``values − {"excel"}``, "every native format is still detectable" approaches
  "every value is a value". Not vacuous — a dropped ``FORMAT_MAP`` entry still
  fails it — but close to uninformative as a *statement about formats*.

Guard 5 is the replacement for what guard 3 gives up, not a deletion of it: it
takes guard 4's shape (a claim must reach an implementation) and points it at the
**complement**, which is the population the subset assertion says nothing about.
Guard 3 is **kept**, and the loss is restated at its own assertion so a reader
of the failure sees it too.

``NATIVE_FORMATS`` must stay a hand-written ``ast.Set`` literal (p55 do-NOT 8).
``NATIVE_FORMATS = set(FORMAT_MAP.values()) - {"excel"}`` would make guard 3 a
tautology and break its ``isinstance(node, ast.Set)`` assertion outright — the
assertion is what enforces the do-NOT, so it is not relaxed here.

The terminal ``else`` is asserted present for a reason with precedent:
``LGR-60c4985-7`` (template / ratified) records that a branch with no terminal
statement fell through to a read of a local only a sibling branch assigns,
failing as ``UnboundLocalError: cannot access local variable
'file_size_bytes'``. Deleting this ``else`` on the grounds that it is
unreachable would reproduce that shape here — ``schema`` would be unbound.

Validates: p46 Requirement 3.3 (design Property 3)
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Declares the Canonical_Format vocabulary as ``FORMAT_MAP``'s values.
PRODUCER_SOURCE = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_file_validation" / "lambda_function.py"

#: Dispatches on a Canonical_Format and selects an extractor.
CONSUMER_SOURCE = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_schema_extract" / "lambda_function.py"

#: The Upload_Modal, which declares the extensions a user may offer (p55 R6.6).
#:
#: A **TypeScript** source, and it is the first non-Python file this contract
#: reads. Read as text with a regex rather than by ``ast``, because there is no
#: Python parser for TSX and adding a JS toolchain dependency to make ``make
#: check`` work would be a new prerequisite on every machine — the trade p55
#: task 2.1 already refused for bats/shunit2.
#:
#: Guard 8 is what makes R6.6's "exactly the set the Format_Registry contains"
#: enforceable rather than aspirational: the frontend cannot import a Python dict,
#: so the two declarations are held together by this file. ``LGR-28b4d75-5``
#: (**template / ratified**) is the reasoning — *"a test written from the
#: producer's output cannot detect the producer drifting, because the output is its
#: own oracle"* — so the oracle for each declaration is the other one.
UPLOAD_MODAL_SOURCE = _PROJECT_ROOT / "dashboard-react" / "src" / "components" / "FileUploadModal.tsx"

_ACCEPTED_FORMATS_NAME = "ACCEPTED_FORMATS"

#: Holds the Format_Vocabulary_Mirror — a deliberate duplicate of ``FORMAT_MAP``
#: kept in sync by this contract (p55 R5.1, R5.2). Read by ``ast`` rather than
#: imported, for the same reason the other two are: this file parses source and
#: constructs nothing.
MIRROR_SOURCE = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_test_code" / "glue_session_runner.py"

_FORMAT_MAP_NAME = "FORMAT_MAP"
_NATIVE_FORMATS_NAME = "NATIVE_FORMATS"
_MIRROR_NAME = "_SOURCE_FORMAT_BY_EXTENSION"
_HANDLER_NAME = "lambda_handler"

#: The local the dispatch chain compares against.
_DISPATCH_VARIABLE = "source_format"

#: The naming convention every extractor follows (design §Components). Required
#: of each branch so a branch that routes nowhere cannot pass as a branch.
_EXTRACTOR_PREFIX = "extract_schema_from_"

#: The naming convention every source conversion follows (p55 R13.6, design §4.3).
#: Guard 5 requires it of every Conversion_Required_Set member's branch, exactly
#: as guard 4 requires :data:`_EXTRACTOR_PREFIX` of every branch. A converter
#: named outside the convention should be renamed rather than this check relaxed
#: — ``convert_source_to_csv``'s own docstring says so at its definition.
_CONVERTER_PREFIX = "convert_source_to_"

#: A ``source_format`` that reaches ``schema_extract`` without coming from
#: ``detect_format``, so ``FORMAT_MAP`` cannot produce it and it must NOT appear
#: in the file-format chain. Asserted, not assumed — see
#: ``test_the_jdbc_source_format_is_dispatched_outside_the_chain``.
_NON_FILE_SOURCE_FORMAT = "jdbc"


def _read(path: Path, description: str) -> str:
    assert path.is_file(), f"{description} not found at {path} — has the Lambda moved or been renamed?"
    return path.read_text(encoding="utf-8")


def _module(path: Path, description: str) -> ast.Module:
    return ast.parse(_read(path, description), filename=str(path))


def _module_level_assignment(module: ast.Module, name: str, source: Path = PRODUCER_SOURCE) -> ast.expr:
    """The value expression of a top-level ``name = ...``, annotated or not.

    *source* names the file in the failure message. It defaults to
    :data:`PRODUCER_SOURCE` so the two original call sites read as they did; p55
    task 4.5 added a third caller against :data:`MIRROR_SOURCE`, and a message
    naming the wrong Lambda would send a reader to the wrong file.
    """
    for node in module.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            value: ast.expr | None = node.value
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
            value = node.value
        else:
            continue
        if name in targets and value is not None:
            return value
    raise AssertionError(
        f"no top-level `{name} = ...` found in {source.name}. "
        "This test's oracle is that declaration; if the vocabulary moved, "
        "point the extraction at its new home rather than transcribing the "
        "format names here (LGR-28b4d75-5)."
    )


def _format_map_values() -> set[str]:
    """The distinct Canonical_Formats, read out of ``FORMAT_MAP``'s own literal.

    Every key and every value must be a string constant. A computed entry, a
    ``**``-unpack or a name reference raises rather than being skipped: a value
    this scan cannot see is a format the dispatch would never be checked for,
    which is the defect this file exists to catch.
    """
    node = _module_level_assignment(_module(PRODUCER_SOURCE, "format detection producer"), _FORMAT_MAP_NAME)
    assert isinstance(node, ast.Dict), (
        f"{_FORMAT_MAP_NAME} is no longer a dict literal ({type(node).__name__}), "
        "so its values cannot be read statically. If it became a computed "
        "mapping, this extraction has to follow it."
    )

    values: set[str] = set()
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            raise AssertionError(
                f"{_FORMAT_MAP_NAME} contains a `**`-unpack, so this scan cannot "
                "see every extension it maps. The forward assertion below would "
                "be incomplete without failing."
            )
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"{_FORMAT_MAP_NAME} has a non-literal key ({ast.dump(key)}); the extension set is not statically readable."
        )
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{_FORMAT_MAP_NAME}[{key.value!r}] is not a string literal "
            f"({ast.dump(value)}). A Canonical_Format this scan cannot read is "
            "a format the dispatch is never checked for."
        )
        values.add(value.value)

    assert values, (
        f"{_FORMAT_MAP_NAME} parsed to ZERO values. Every assertion below would "
        "pass vacuously against an empty vocabulary — which is the exact shape "
        "of the defect p46 fixed, a claim nothing checked."
    )
    return values


def _native_formats() -> set[str]:
    """``NATIVE_FORMATS``' members — the corroborating derivation (guard 3)."""
    node = _module_level_assignment(_module(PRODUCER_SOURCE, "format detection producer"), _NATIVE_FORMATS_NAME)
    assert isinstance(node, ast.Set), (
        f"{_NATIVE_FORMATS_NAME} is no longer a set literal ({type(node).__name__}); "
        "the corroboration of the FORMAT_MAP value extraction has to follow it."
    )
    members = {e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)}
    assert len(members) == len(node.elts), (
        f"{_NATIVE_FORMATS_NAME} has a non-literal member; it is not statically readable."
    )
    assert members, f"{_NATIVE_FORMATS_NAME} parsed to zero members."
    return members


def _string_dict_literal(source: Path, description: str, name: str) -> dict[str, str]:
    """A top-level ``name = {"k": "v", ...}`` in *source*, as a ``dict[str, str]``.

    The same **raise rather than skip** posture :func:`_format_map_values` takes,
    and for the same stated reason one level along: an entry this scan cannot read
    is an entry the sync check would never be made about, so a silently partial
    parse would let the two literals diverge on exactly the key it could not see.
    """
    node = _module_level_assignment(_module(source, description), name, source)
    assert isinstance(node, ast.Dict), (
        f"{name} in {source.name} is no longer a dict literal ({type(node).__name__}), "
        "so its entries cannot be read statically. If it became a computed "
        "mapping, this extraction has to follow it."
    )

    entries: dict[str, str] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if key is None:
            raise AssertionError(
                f"{name} in {source.name} contains a `**`-unpack, so this scan cannot "
                "see every entry it declares. The sync assertion below would be "
                "incomplete without failing."
            )
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"{name} in {source.name} has a non-literal key ({ast.dump(key)}); it is not statically readable."
        )
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), (
            f"{name}[{key.value!r}] in {source.name} is not a string literal ({ast.dump(value)}); "
            "an entry this scan cannot read is an entry the sync check is never made about."
        )
        entries[key.value] = value.value

    assert entries, f"{name} in {source.name} parsed to ZERO entries; every assertion over it would pass vacuously."
    return entries


def _format_map_entries() -> dict[str, str]:
    """``FORMAT_MAP`` as extension → Canonical_Format (guard 6's expectation).

    Parses the same literal :func:`_format_map_values` does, deliberately without
    reusing it: that function's assertion messages are a fence in their own right
    — p55 do-NOT 15 quotes the value-literal one verbatim — and rewriting it to
    return entries would have edited a fenced message to add a caller. Two
    readers of one literal is the cheaper of the two costs, and any divergence
    between them is a failure in whichever guard reads it.
    """
    return _string_dict_literal(PRODUCER_SOURCE, "format detection producer", _FORMAT_MAP_NAME)


def _upload_modal_accepted_extensions() -> list[str]:
    """The Upload_Modal's ``ACCEPTED_FORMATS`` entries, in declared order (guard 8).

    **Raise rather than skip**, the same posture :func:`_format_map_values` takes
    and for the same reason: an ``ACCEPTED_FORMATS`` this scan cannot read is a set
    R6.6 is never checked for, and a silent skip would report "one fact" while the
    two declarations drifted.

    Deliberately narrow about what it accepts as the literal. Only a
    ``const ACCEPTED_FORMATS = [ ... ]`` of single-quoted string entries parses; a
    computed value, a spread, or a value read from configuration raises. That is
    not a lint about style — a derived value on the frontend would mean this
    contract's oracle had become the same expression on both sides, which is
    exactly what ``LGR-28b4d75-5`` says cannot detect drift.
    """
    source = _read(UPLOAD_MODAL_SOURCE, "upload modal")
    match = re.search(
        rf"^const\s+{_ACCEPTED_FORMATS_NAME}\s*=\s*\[(?P<body>[^\]]*)\]\s*;",
        source,
        re.MULTILINE | re.DOTALL,
    )
    assert match is not None, (
        f"no top-level `const {_ACCEPTED_FORMATS_NAME} = [ ... ];` found in "
        f"{UPLOAD_MODAL_SOURCE.name}. This guard's oracle is that declaration; if the accepted set "
        f"moved, point the extraction at its new home rather than transcribing the extensions here "
        f"(LGR-28b4d75-5). If it stopped being a literal, that is a change this contract must fail on: "
        f"a derived set makes the two sides one expression, which cannot detect drift."
    )

    body = match.group("body")
    entries = re.findall(r"'([^']*)'", body)
    assert entries, f"{_ACCEPTED_FORMATS_NAME} in {UPLOAD_MODAL_SOURCE.name} parsed as empty: {body!r}"

    # Every non-comment, non-whitespace token must have been one of the strings —
    # otherwise something in the literal was not read and the set is larger than
    # this guard can see.
    residue = re.sub(r"'[^']*'|//[^\n]*|/\*.*?\*/|[\s,]", "", body, flags=re.DOTALL)
    assert residue == "", (
        f"{_ACCEPTED_FORMATS_NAME} carries an entry this guard cannot read: {residue!r}. "
        f"An extension the scan cannot see is an extension R6.6 is never checked for."
    )
    return entries


def _mirror_extension_map() -> dict[str, str]:
    """The Format_Vocabulary_Mirror as extension → Canonical_Format (guard 6).

    ``glue_session_runner`` imports cleanly with no AWS call, so this one *could*
    be imported rather than parsed — ``test_sample_readers_match_templates.py``
    does exactly that. It is parsed here anyway, so that all three sources in this
    file are read the same way and a reader does not have to hold two mechanisms
    in mind to know what the failure means.
    """
    return _string_dict_literal(MIRROR_SOURCE, "format vocabulary mirror (test_code)", _MIRROR_NAME)


def _handler(module: ast.Module) -> ast.FunctionDef:
    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == _HANDLER_NAME:
            return node
    raise AssertionError(
        f"no top-level `def {_HANDLER_NAME}` in {CONSUMER_SOURCE.name} — the "
        "Extraction_Dispatch's home moved and this test cannot find the chain."
    )


def _chain_literal(node: ast.stmt) -> str | None:
    """The format literal of ``if source_format == "<x>":``, else ``None``.

    Deliberately matches only a comparison whose left operand is the bare local
    ``source_format``. The early ``event.get("source_format") == "jdbc"`` guard
    compares a *call*, so it is excluded structurally rather than by an
    exemption list.
    """
    if not isinstance(node, ast.If):
        return None
    test = node.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or not isinstance(test.ops[0], ast.Eq):
        return None
    if not (isinstance(test.left, ast.Name) and test.left.id == _DISPATCH_VARIABLE):
        return None
    if len(test.comparators) != 1:
        return None
    right = test.comparators[0]
    if isinstance(right, ast.Constant) and isinstance(right.value, str):
        return right.value
    return None


def _called_names(body: list[ast.stmt]) -> set[str]:
    """Every function name called anywhere in *body*, by AST not by substring."""
    names: set[str] = set()
    for stmt in body:
        for node in ast.walk(stmt):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    names.add(node.func.attr)
    return names


def _dispatch_chain() -> tuple[dict[str, set[str]], bool]:
    """``({format: names called in its branch}, has_terminal_else)``.

    Walks ``lambda_handler`` for every ``source_format == "<literal>"``
    comparison, groups them into ``if``/``elif`` chains by following ``orelse``,
    and requires exactly one chain — a second chain on the same variable is
    itself a drift signal, since two places would then decide routing.
    """
    handler = _handler(_module(CONSUMER_SOURCE, "schema extraction consumer"))

    matching = [node for node in ast.walk(handler) if _chain_literal(node) is not None]
    assert matching, (
        f'no `{_DISPATCH_VARIABLE} == "<format>"` comparison found in '
        f"{CONSUMER_SOURCE.name}::{_HANDLER_NAME}. The Extraction_Dispatch was "
        "reshaped — a dict lookup, a registry, a match statement — and this "
        "extraction has to follow it. Do NOT delete this test: an unchecked "
        "dispatch is how two claimed formats reached a JSON reader unnoticed."
    )

    # A node reached as the sole element of another matching node's ``orelse``
    # is an ``elif``, not a chain head.
    nested = {id(n.orelse[0]) for n in matching if len(n.orelse) == 1 and id(n.orelse[0]) in {id(m) for m in matching}}
    heads = [n for n in matching if id(n) not in nested]
    assert len(heads) == 1, (
        f"expected exactly one {_DISPATCH_VARIABLE} if/elif chain, found "
        f"{len(heads)} at lines {sorted(n.lineno for n in heads)}. Two chains "
        "means two places decide which extractor runs, and this contract can "
        "only speak for one of them."
    )

    branches: dict[str, set[str]] = {}
    current: ast.If = heads[0]
    while True:
        literal = _chain_literal(current)
        assert literal is not None  # loop invariant: only matching nodes enter
        assert literal not in branches, (
            f"{_DISPATCH_VARIABLE} == {literal!r} appears twice in one chain; the second branch is unreachable."
        )
        branches[literal] = _called_names(current.body)

        if len(current.orelse) == 1 and _chain_literal(current.orelse[0]) is not None:
            next_node = current.orelse[0]
            assert isinstance(next_node, ast.If)
            current = next_node
            continue
        break

    return branches, bool(current.orelse)


# ── Guard 1: the sources are readable ────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "description"),
    [
        (PRODUCER_SOURCE, "format detection producer (file_validation)"),
        (CONSUMER_SOURCE, "schema extraction consumer (schema_extract)"),
        # p55 R5.1/R5.2 — a third source this contract now depends on. Added to
        # this guard rather than left implicit so a moved `glue_session_runner`
        # fails as "the mirror is not where this contract looks" instead of
        # inside guard 6's extraction.
        (MIRROR_SOURCE, "format vocabulary mirror (test_code)"),
        # p55 R6.6 — the fourth, and the first outside `lambda/`. Same reason: a
        # renamed or relocated modal must fail as "not where this contract looks"
        # rather than inside guard 8's regex.
        (UPLOAD_MODAL_SOURCE, "upload modal (dashboard-react)"),
    ],
)
def test_source_is_readable(path: Path, description: str):
    """A moved or renamed Lambda must fail loudly, not skip."""
    assert path.is_file(), f"{description} not found at {path}"


# ── Guard 2/3: neither extraction may be empty or degraded ───────────────


def test_format_map_values_are_extractable_and_corroborated():
    """Anti-vacuity: the producer's vocabulary parses to a real, plural set.

    ``NATIVE_FORMATS`` is the second derivation. It is a sibling literal naming
    formats Glue reads without conversion — orthogonal to whether an extractor
    exists, which is what makes it usable as a check on the value extraction
    rather than a restatement of it.

    **AMENDED by p55 task 4.5 — kept, with one of its two roles weakened and the
    weakening named rather than dropped** (``LGR-60c4985-6`` / ``LGR-9e76249-10``,
    both **template / ratified**). ``NATIVE_FORMATS`` was
    ``{"parquet", "avro", "orc"}``; p55 R13 redefines it as *readable directly
    from the source object, with no prior conversion* and widens it to
    ``{"csv", "json", "parquet", "avro", "orc"}``.

    * **Extraction corroboration — intact, and stronger.** It never rested on the
      set having a production consumer. It rests on the set being parsed by a
      *different* code path (:func:`_native_formats`, ``ast.Set``) from the values
      (:func:`_format_map_values`, ``ast.Dict``), so a degraded ``FORMAT_MAP``
      parse that drops any member fails here. **Five** members must now be found
      rather than three, which is more of the literal that has to survive the
      parse, not less.
    * **Semantic content — weakened, and this is the honest loss.** Once the set
      is ``values − {"excel"}``, "every native format is still detectable" is
      close to "every value is a value". Guard 5
      (:func:`test_every_conversion_required_format_reaches_a_converter`) is the
      replacement: it takes guard 4's shape and points it at the complement, which
      is the population this subset assertion cannot speak about at all.

    The ``ast.Set`` assertion inside :func:`_native_formats` is **not** relaxed —
    it is what enforces p55 do-NOT 8 (the set stays a hand-written literal and is
    never derived as ``set(FORMAT_MAP.values()) - {"excel"}``, which would make
    the assertion below a tautology).
    """
    values = _format_map_values()
    assert len(values) > 1, (
        f"{_FORMAT_MAP_NAME} parsed to {len(values)} value(s) — too few to be "
        "the real vocabulary; the extraction is matching part of the literal "
        "and missing the rest."
    )

    native = _native_formats()
    assert native <= values, (
        f"{_NATIVE_FORMATS_NAME} names {sorted(native - values)}, which is not "
        f"among the {_FORMAT_MAP_NAME} values this scan found ({sorted(values)}). "
        "Either the value extraction is degraded, or a format Glue reads "
        "natively is no longer detectable at all — both are real failures.\n"
        "\n"
        "p55 R13.5, and read this assertion for what it now is: since the "
        "Native_Format_Set is the full value set minus the Conversion_Required_Set, "
        "it is primarily a CORROBORATION OF THE PARSE (two code paths, ast.Set vs "
        "ast.Dict, five members that must all be found) and only weakly a "
        "statement about formats. The semantic half moved to "
        "test_every_conversion_required_format_reaches_a_converter, which asserts "
        "the complement reaches a real conversion. Do NOT delete this assertion on "
        "the grounds that it has become obvious: it is the only thing standing "
        "between do-NOT 8 and a NATIVE_FORMATS derived from FORMAT_MAP."
    )


def test_dispatch_chain_is_extractable():
    """Anti-vacuity: the consumer's chain parses to a real, plural set."""
    branches, _ = _dispatch_chain()
    assert len(branches) > 1, (
        f"the Extraction_Dispatch parsed to {len(branches)} branch(es) "
        f"({sorted(branches)}) — too few to be the real chain."
    )


# ── Guard 4: a branch must actually route somewhere ──────────────────────


def test_every_dispatch_branch_reaches_an_extractor():
    """A branch that exists and extracts nothing does not satisfy the contract.

    Without this, ``elif source_format == "avro": pass`` would pass the
    vocabulary assertion below while reproducing the defect — a format claimed
    by the dispatch and handled by nothing.
    """
    branches, _ = _dispatch_chain()

    routeless = {
        fmt: sorted(names) for fmt, names in branches.items() if not any(n.startswith(_EXTRACTOR_PREFIX) for n in names)
    }
    assert not routeless, (
        f"these dispatch branches call no `{_EXTRACTOR_PREFIX}*` function: {routeless}. "
        "A branch that routes nowhere is the claimed-but-unhandled defect in a "
        "new costume. If an extractor was legitimately named outside that "
        "convention, rename it to follow the convention (design §Components) "
        "rather than relaxing this check."
    )


# ── Guard 5: a Conversion_Required_Set member must reach a converter ─────


def test_every_conversion_required_format_reaches_a_converter():
    """p55 R13.6 and R13.7 — guard 4's shape, pointed at the complement.

    The Conversion_Required_Set is ``set(FORMAT_MAP.values()) - NATIVE_FORMATS``:
    the Canonical_Formats no Glue_Job_Template can read from the source object, so
    the object must be converted before the Glue job reads it (R13.3). "There is a
    branch, and it extracts a schema" is not enough for one of those — a run whose
    schema was extracted from a workbook and whose Glue job was then handed the
    workbook would fail at the Glue read, or worse, read it as something else.

    So the claim *this format needs converting* must reach an implementation, which
    is exactly guard 4's argument one population along. Both are AST-read the same
    way, off the same :func:`_dispatch_chain` extraction, differing only in the
    name prefix required of the branch.

    **R13.7 is the anti-vacuity floor.** An empty complement would make this guard
    pass over nothing, which is the failure mode all four original guards were
    written to prevent. It is asserted first, so "no format needs converting" is
    reported as *that* rather than as this contract holding.

    **Raise rather than skip, per :func:`_format_map_values`' posture.** A
    Conversion_Required_Set member with no dispatch branch at all fails here
    naming the missing branch, instead of being silently absent from the
    converter check — a format this guard cannot find is a format the conversion
    is never checked for.
    """
    values = _format_map_values()
    native = _native_formats()
    conversion_required = values - native

    assert conversion_required, (
        f"the Conversion_Required_Set is EMPTY: {_FORMAT_MAP_NAME}'s values "
        f"({sorted(values)}) and {_NATIVE_FORMATS_NAME} ({sorted(native)}) are the "
        "same set, so the assertion below would pass over nothing (p55 R13.7).\n"
        "\n"
        "If a format genuinely stopped needing conversion, that is a real change "
        "and this floor is where it has to be argued: an empty complement means "
        "this guard has no subject, and a guard with no subject is the vacuous "
        "pass the four older guards in this file exist to prevent. It also means "
        "NATIVE_FORMATS has stopped being a claim about anything — every value "
        "would be native — which is the point at which do-NOT 8's hand-written "
        "literal should be reconsidered deliberately rather than by drift."
    )

    branches, _ = _dispatch_chain()

    unbranched = sorted(conversion_required - set(branches))
    assert not unbranched, (
        f"{sorted(conversion_required)} need conversion before a Glue job can read "
        f"them, and the Extraction_Dispatch has no branch for {unbranched}. It has "
        f"branches for {sorted(branches)}. This guard raises rather than skipping "
        "the missing format: a Canonical_Format this scan cannot find a branch for "
        "is a format the conversion is never checked for."
    )

    unconverted = {
        fmt: sorted(branches[fmt])
        for fmt in sorted(conversion_required)
        if not any(name.startswith(_CONVERTER_PREFIX) for name in branches[fmt])
    }
    assert not unconverted, (
        f"these Conversion_Required_Set branches call no `{_CONVERTER_PREFIX}*` "
        f"function: {unconverted}.\n"
        "\n"
        f"A member of {sorted(conversion_required)} cannot be read from the source "
        "object by any Glue_Job_Template (p55 R13.1, R13.2), so a branch that "
        "extracts a schema and converts nothing leaves the Glue job pointed at an "
        "object it cannot read — the schema would be right and the run would fail "
        "at the read, or read the bytes as something else. R13.3 requires the "
        "conversion; this asserts it exists.\n"
        "\n"
        "This is guard 4's argument for the other population: guard 4 says every "
        "branch reaches an `extract_schema_from_*`, and it is satisfied by an "
        "Excel branch that extracts and does not convert. If a converter was "
        f"legitimately named outside the `{_CONVERTER_PREFIX}*` convention, rename "
        "it to follow the convention rather than relaxing this check — "
        "`convert_source_to_csv`'s own docstring says so at its definition."
    )


def test_the_dispatch_keeps_its_defensive_else():
    """The fallthrough path stays, even though no live input reaches it.

    ``LGR-60c4985-7`` (template / ratified) is why this is asserted rather than
    assumed: in ``file_validation`` a branch with no terminal statement fell
    through to a read of a local only its sibling assigns, and the run failed as
    ``UnboundLocalError: cannot access local variable 'file_size_bytes'``.
    Deleting this ``else`` as unreachable reproduces that shape — ``schema``
    would be unbound for any value the chain does not name, and the failure
    would name a Python local instead of the format nothing could read.
    """
    _, has_else = _dispatch_chain()
    assert has_else, (
        "the Extraction_Dispatch chain has no terminal `else`. It is defensive, "
        "not dead: it is what makes an unrecognised source_format say so in the "
        "log instead of raising UnboundLocalError on `schema` (LGR-60c4985-7)."
    )


# ── Guard 6: the Format_Vocabulary_Mirror is FORMAT_MAP ──────────────────


def test_the_format_vocabulary_mirror_matches_the_format_registry():
    """p55 R5.1 and R5.2 — the mirror's "change both or neither", made executable.

    ``glue_session_runner._SOURCE_FORMAT_BY_EXTENSION`` is a deliberate duplicate
    of ``FORMAT_MAP``, and its own docstring says why it is a duplicate rather than
    an import: ``file_validation`` is a different Lambda package, not in
    ``shared/``, so importing it "would work in the test suite and fail in the
    container". The docstring then states the obligation the duplication creates —
    "**change both or neither**" — and until p55 task 4.5 nothing checked it.
    R5.2 requires this contract to fail until the mirror carries a newly added
    extension, which is what the first assertion below does.

    Read as equality, not as a subset in the R5.1 direction only. The mirror
    consumer resolves an extension to the format it will *read a sample as*, and
    the docstring's own reason for reproducing ``FORMAT_MAP`` is that "the format
    this runner reads a sample as is the format ``file_validation`` recorded" — so
    an extension the mirror knows and the registry does not is the same defect
    pointed the other way, exactly as the inverse dispatch assertion is to the
    forward one.

    The expectation is derived from ``FORMAT_MAP``, never restated here
    (``LGR-28b4d75-5``, **template / ratified**): a transcribed pair list would
    pass forever while both sides drifted together.
    """
    registry = _format_map_entries()
    mirror = _mirror_extension_map()

    missing = sorted(set(registry) - set(mirror))
    assert not missing, (
        f"{_FORMAT_MAP_NAME} declares the extension(s) {missing} and the "
        f"Format_Vocabulary_Mirror ({_MIRROR_NAME} in {MIRROR_SOURCE.name}) does "
        f"not. It carries {sorted(mirror)}.\n"
        "\n"
        "p55 R5.1/R5.2: adding an extension to FORMAT_MAP obliges the matching "
        "mirror entry, and this contract fails until it is there. The mirror is "
        "what `sample_read_format` reads to decide how the verification session "
        "reads the run's real source object, so an extension the producer accepts "
        "and the mirror does not know resolves to None — the run silently falls "
        "back to a synthetic frame and the transform is verified against generated "
        f"data. Add the entry to {_MIRROR_NAME} with the SAME Canonical_Format "
        "FORMAT_MAP gives it; do not invent a second spelling."
    )

    disagreeing = {
        extension: (registry[extension], mirror[extension])
        for extension in sorted(set(registry) & set(mirror))
        if registry[extension] != mirror[extension]
    }
    assert not disagreeing, (
        f"the Format_Vocabulary_Mirror disagrees with {_FORMAT_MAP_NAME} about "
        f"{disagreeing} (registry value, mirror value).\n"
        "\n"
        "Two opinions about what an extension means is worse than one: "
        "`file_validation` would record one Canonical_Format, the generated Glue "
        "job would be templated for it, and the verification would read the sample "
        "as the other — so the transform would be verified against a frame the job "
        "never produces."
    )

    extra = sorted(set(mirror) - set(registry))
    assert not extra, (
        f"the Format_Vocabulary_Mirror knows the extension(s) {extra}, which "
        f"{_FORMAT_MAP_NAME} cannot produce — no upload carrying them is ever "
        "accepted, so the entry reads as support that does not exist. This is the "
        "inverse direction, asserted for the same reason "
        "test_no_dispatch_branch_names_a_format_detection_cannot_emit is: a claim "
        "no input can reach is misinformation. If the extension should be "
        f"supported, add it to {_FORMAT_MAP_NAME} — which obliges an extractor, a "
        "dispatch branch, and either native readability or a conversion."
    )


# ── The contract ─────────────────────────────────────────────────────────


def test_every_format_map_value_has_a_dispatch_branch():
    """``FORMAT_MAP`` values ⊆ dispatch chain literals — R3.3, design Property 3.

    The assertion whose absence allowed the p46 defect. The expected set is
    derived from ``FORMAT_MAP`` and never written down here, per
    ``LGR-28b4d75-5``: a restated list is the drift this exists to prevent.

    Validates: p46 Requirement 3.3
    """
    values = _format_map_values()
    branches, _ = _dispatch_chain()

    unhandled = sorted(values - set(branches))
    assert not unhandled, (
        f"{_FORMAT_MAP_NAME} can emit {unhandled}, and the Extraction_Dispatch "
        f"in {CONSUMER_SOURCE.name}::{_HANDLER_NAME} has no branch for it. It has "
        f"branches for {sorted(branches)}.\n"
        "\n"
        "This is the p46 defect: a format claimed at validation and unhandled at "
        "extraction does NOT fail. It falls to the defensive `else`, gets read as "
        "JSON, and a binary container yields `{'type': 'object', 'properties': "
        "{}}` — so the run SUCCEEDS declaring zero fields and continues to "
        "schema_mapping with nothing to map.\n"
        "\n"
        "Adding a format to FORMAT_MAP therefore obliges four more things, none "
        "of which this failure can do for you:\n"
        f"  1. an `extract_schema_from_<format>` in {CONSUMER_SOURCE.name} that "
        "RAISES on a parse failure and never returns an empty `properties` "
        "(p46 R4, design Property 2);\n"
        f"  2. an `elif {_DISPATCH_VARIABLE} == '<format>'` branch calling it, "
        "ahead of the `else`;\n"
        "  3. a fixture under tests/fixtures/data/ generated by "
        "generate_binary_fixtures.py, plus routing and malformed-input tests in "
        "tests/unit/lambdas/schema_extract/;\n"
        "  4. a check that the format is reachable at all — NATIVE_FORMATS and "
        "the docs' format list both make claims about it.\n"
        "\n"
        "Do NOT satisfy this test by removing the format from FORMAT_MAP unless "
        "rejecting it at the gate is the actual intent: that is the alternative "
        "p46's requirements considered and rejected, because it withdraws a "
        "capability rather than delivering one."
    )


def test_no_dispatch_branch_names_a_format_detection_cannot_emit():
    """Dispatch chain literals ⊆ ``FORMAT_MAP`` values — the inverse direction.

    Not required by R3.3; asserted because a branch for a format ``detect_format``
    cannot return is dead code that reads as capability. Together with the
    forward assertion this makes the two vocabularies equal, which is the
    strongest honest statement of design Property 3.
    """
    values = _format_map_values()
    branches, _ = _dispatch_chain()

    orphaned = sorted(set(branches) - values)
    assert not orphaned, (
        f"the Extraction_Dispatch branches on {orphaned}, which {_FORMAT_MAP_NAME} "
        "cannot produce — so no uploaded file can ever reach that branch and it "
        "reads as support that does not exist.\n"
        "\n"
        f"If the value arrives from somewhere other than `detect_format` (as "
        f"{_NON_FILE_SOURCE_FORMAT!r} does, from a JDBC pipeline with no file to "
        "detect), it belongs in an early guard before the file-format chain, "
        "where it does not claim to be a detectable file format. If it is a new "
        f"detectable format, add its extension to {_FORMAT_MAP_NAME} — and note "
        "that widening the nine-key set equality at "
        "tests/unit/lambdas/file_validation/test_format_detection.py:133 is a "
        "ratified fence (LGR-60c4985-7), retired deliberately or not at all."
    )


def test_the_jdbc_source_format_is_dispatched_outside_the_chain():
    """``jdbc`` reaches this handler and is deliberately not a chain literal.

    This is what makes the inverse assertion's scope a checked fact rather than
    an accident. ``jdbc`` is a real ``source_format`` — its schema comes from a
    per-pipeline runtime crawler, not from a file — so it is handled by an early
    guard on ``event.get("source_format")`` and never enters the file-format
    chain. If that guard were folded into the chain, the inverse assertion above
    would fail, and this test says why.
    """
    handler = _handler(_module(CONSUMER_SOURCE, "schema extraction consumer"))

    guarded = any(
        isinstance(node, ast.Compare)
        and any(isinstance(c, ast.Constant) and c.value == _NON_FILE_SOURCE_FORMAT for c in node.comparators)
        and isinstance(node.left, ast.Call)
        and any(isinstance(a, ast.Constant) and a.value == _DISPATCH_VARIABLE for a in node.left.args)
        for node in ast.walk(handler)
    )
    assert guarded, (
        f"no early `event.get({_DISPATCH_VARIABLE!r}) == {_NON_FILE_SOURCE_FORMAT!r}` "
        f"guard found in {CONSUMER_SOURCE.name}::{_HANDLER_NAME}. Either the JDBC "
        "path moved, or it was folded into the file-format chain — where it would "
        "claim to be a detectable file format. The inverse assertion above depends "
        "on this placement."
    )

    branches, _ = _dispatch_chain()
    assert _NON_FILE_SOURCE_FORMAT not in branches, (
        f"{_NON_FILE_SOURCE_FORMAT!r} is now a branch in the file-format chain. It "
        f"is not a {_FORMAT_MAP_NAME} value and never can be — a JDBC pipeline has "
        "no file to detect — so it belongs in the early guard, not here."
    )


# ── Guard 8: the Upload_Modal accepts exactly the Format_Registry's set ──


def test_the_upload_modal_accepts_exactly_the_format_registry_extensions():
    """p55 R6.6 — ``ACCEPTED_FORMATS`` is ``FORMAT_MAP``'s key set, dot-prefixed.

    **Set equality, both directions, and each direction is a different defect.**

    * A modal **superset** is the defect ``requirements.md`` opens with —
      *"frontend accepts what the backend rejects"*. Its concrete instance was
      ``.xlsx``: the modal listed it while ``detect_format`` returned ``"unknown"``
      and ``file_validation`` raised. W3 made that entry true, so the line became
      correct **by accident**. This guard is what makes it correct by construction,
      which is the whole difference between the two.
    * A modal **subset** is the mirror image and was never anyone's defect to fix:
      ``jsonl``, ``ndjson``, ``pq``, ``avro`` and ``orc`` were all in
      ``FORMAT_MAP``, all supported end to end, and all hidden. A user with an Avro
      file was told the format was unsupported by a picker rather than by the
      system.

    Neither direction is detectable from one side alone, which is why this reads
    both declarations rather than either one's output (``LGR-28b4d75-5``).

    **The comparison is on extensions, not on Canonical_Formats.** ``FORMAT_MAP``'s
    *keys* are extensions and its *values* are format families — ``tsv → "csv"``,
    ``xlsx → "excel"`` — and a user picks a file by extension. Comparing values
    would let ``.tsv`` disappear from the picker without failing, because ``csv``
    would still be present.

    Validates: p55 Requirement 6.6
    """
    registry = {f".{extension}" for extension in _format_map_entries()}
    offered = _upload_modal_accepted_extensions()

    assert len(set(offered)) == len(offered), (
        f"{_ACCEPTED_FORMATS_NAME} lists an extension twice: {offered}. A duplicate is harmless to "
        f"`includes` and to `accept`, and it means the two declarations were edited by hand into "
        f"agreement rather than kept in agreement."
    )

    missing = sorted(registry - set(offered))
    extra = sorted(set(offered) - registry)
    assert not missing, (
        f"the Format_Registry contains {missing} and the Upload_Modal does not offer them, so a "
        f"user with one of those files is refused by the picker for a format this system supports "
        f"end to end (R6.6). Add them to {_ACCEPTED_FORMATS_NAME} in {UPLOAD_MODAL_SOURCE.name}."
    )
    assert not extra, (
        f"the Upload_Modal offers {extra} and the Format_Registry does not contain them — this is "
        f"`requirements.md`'s opening defect, 'frontend accepts what the backend rejects', with a "
        f"different extension. Either add the key to {_FORMAT_MAP_NAME} with a Canonical_Format and "
        f"a dispatch branch, or remove it from {_ACCEPTED_FORMATS_NAME}."
    )


def test_the_upload_modals_extensions_are_dot_prefixed_and_lower_case():
    """The two readers of the set both depend on this shape, so it is asserted.

    ``validateFile`` compares against ``getFileExtension``'s output, which is
    ``name.slice(name.lastIndexOf('.')).toLowerCase()`` — dot included, lower-cased
    — and the input's ``accept`` attribute wants the same spelling. An entry without
    the dot, or with a capital, would match nothing and would hide a supported
    format while looking present in the literal. That is a silent subset, which the
    guard above cannot distinguish from a deliberate one.
    """
    for extension in _upload_modal_accepted_extensions():
        assert extension.startswith("."), (
            f"{extension!r} has no leading dot, so `validateFile` can never match it — "
            f"`getFileExtension` returns the dot as part of the extension."
        )
        assert extension == extension.lower(), (
            f"{extension!r} is not lower case, so `validateFile` can never match it — "
            f"`getFileExtension` lower-cases before comparing."
        )
        assert extension.count(".") == 1, f"{extension!r} carries more than one dot"
