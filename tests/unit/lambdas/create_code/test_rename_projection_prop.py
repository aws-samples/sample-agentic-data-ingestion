# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 W4 task 6.6 — design Properties 2 and 3, over 6.5's emission seams.

# Feature: p55-format-support-and-teardown, Property 2: The emitted original-name literal round-trips
# Feature: p55-format-support-and-teardown, Property 3: Renaming preserves which column is read

**Validates: Requirements 11.1, 11.2**

WHY THESE TWO ARE PROPERTIES AND NOT MORE EXAMPLES
--------------------------------------------------
Task 6.5 already covers this sink at the example level: 13 hostile inputs, the
placement ordering, the length cap's refusal, the malformed-manifest refusals,
the allowlist sink and the render-budget growth law
(``test_rename_projection.py``, 49 tests). **This file's contribution is the
space, not more examples.**

Property 2 is the injection guard and the only executable form of **do-NOT 17**.
Before W4 an original column name reached generated code **nowhere**; after 6.5
it reaches it as an escaped Python string literal. ``identifier_utils.py``'s
module docstring states the threat verbatim — *"A hostile CSV header must never
become code."* The Identifier_Allowlist guards the **SQL** sink; nothing guards a
**string-literal** sink, because before W4 no such sink existed. So the guard has
to be quantified over strings rather than sampled from a list of the hostile
inputs somebody thought of.

SUBJECTS, AND WHY THE PROPERTIES ATTACH WHERE THEY DO
-----------------------------------------------------
``code_assembly._python_string_literal`` is pure ``str -> str``;
``_rename_projection_block`` takes ``(original, target)`` pairs and returns
Python source; ``_rename_lookup`` is ``dict(pairs)``. All three are total and
none of them touches AWS — ``LGR-1e135d2-2`` (**template / ratified**) records
that ``code_assembly`` owns no AWS client and imports no strands, and
``test_code_assembly_still_owns_no_aws_client`` is that record's fence. So
neither property needs a render, a fixture, a client or an environment.

* **Property 2 attaches to ``_python_string_literal`` first** (the literal is the
  artifact the property is about), and then to ``_rename_projection_block``, so
  the guarantee is asserted at the seam *and* at the sink that emits into the
  generated job. The second arm is what makes it a statement about the rendered
  job rather than about a helper.
* **Property 3 attaches to ``_rename_projection_block``'s output, EXECUTED.** See
  the next block — this is the one judgement call in the task.

PROPERTY 3'S EXECUTABLE FORM — A JUDGEMENT, STATED
--------------------------------------------------
Task 6.5's report suggests expressing Property 3 as: *for any source column name
list, ``_rename_lookup`` composed with the emitted projection is an injective map
whose image satisfies the allowlist and whose preimage is the original list.*
**That framing is not adopted, and the reason is that it does not check what
design Property 3 says.**

Design states Property 3 as *"applying the rename then reading a column by its
approved name yields the column the original name selected."* The suggested
framing is about the **shape of a dict**: distinctness plus conformance of the
target names. That is design **Property 1** (task 6.1, over
``derive_rename_manifest``), one domain over — and against ``_rename_lookup`` it
is close to unfalsifiable, because that function is a single ``dict(pairs)`` call,
so the property would pass for any manifest whose pairs were already distinct and
conforming, i.e. it would be re-asserting its own input. It never models a
**read**, and "which column is read" is the whole of R11.1/R11.2.

So the honest form is to **execute the emitted projection** against a model of
``withColumnRenamed`` and check column identity survives it. Each source column
carries an opaque payload; the property is that after the projection, reading the
approved name returns the payload the **original** name selected, that a column
nobody renamed still answers to its own name, and that column order, count and
payload set are unchanged. That is a statement about the code 6.5 emits, not
about a dict it also builds.

The model is deliberately faithful on one point: ``withColumnRenamed`` is a
**silent no-op** when no column carries the given name (that is exactly why 6.5's
cap refuses instead of truncating — see ``_MAX_ORIGINAL_NAME_CHARS``). The model
reproduces the no-op rather than raising, and counts them, so a mangled literal
surfaces as *"the rename silently did not happen"* — the real failure mode — with
a precise count, instead of as an incidental ``KeyError`` from the test's own
stub.

``exec`` is used on purpose and is the point: the property is about emitted
**source text**, and executing it is the only way to make "the rename happens"
mean anything. ``ast.parse`` alone proves the text is parseable, which Property 2
already covers.

WHAT PROPERTY 3'S GENERATOR ASSUMES, AND WHERE THAT ASSUMPTION IS EARNED
------------------------------------------------------------------------
A manifest whose target name collides with a column that is **not** being renamed
makes "reading by the approved name" ambiguous — Spark would carry two columns of
that name. The generator therefore produces manifests whose targets are pairwise
distinct and distinct from every retained column name, and the property
**asserts that precondition on its own generated data** rather than trusting it.
The warrant that production manifests satisfy it is R10.3/R10.4 and design
Property 1, checked at derivation in ``schema_mapping`` — not here. This file does
not import ``derive_rename_manifest``: it lives in
``schema_mapping/lambda_function.py``, which needs five layer modules stubbed and
takes part in the ``sys.modules["lambda_function"]`` tug-of-war 23 Lambda
directories share, and 6.5's contract is that the **stored** manifest is the
authority, consumed verbatim or not at all. A consumer test that re-derived its
input would be testing the deriver.

PROPERTY 2'S GENERATOR — WHAT IT REACHES, AND WHY EACH ARM IS THERE
-------------------------------------------------------------------
The mechanism under test has already been wrong once in exactly one region, and
6.5 pinned the counterexample: ``json.dumps(..., ensure_ascii=True)`` — which is
W4-D2's letter — escapes a non-BMP character as a UTF-16 **surrogate pair**, and
Python reads ``"\\ud83d\\ude42"`` as two lone surrogates rather than as ``"🙂"``.
6.5 therefore kept the *property* (a pure-ASCII literal, everything escaped) via
``ensure_ascii=False`` plus explicit ``\\uXXXX`` / ``\\UXXXXXXXX`` escaping. A
generator that does not reach that region would ratify the naive form. So the
generator reaches, and ``test_the_property_2_generator_reaches_each_hazard``
proves it reaches with ``hypothesis.find`` rather than claiming it in prose:

* **astral characters** (> U+FFFF) — the region the naive mechanism got wrong;
* **lone surrogates** — ``st.text()`` **cannot** produce them (measured: a ``find``
  for a surrogate over ``st.text()`` raises ``NoSuchExample``), so they are added
  explicitly from ``st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF)``;
* **control characters**, including NUL and ESC;
* the **structural** characters of the sink itself — double quote, backslash,
  newline, carriage return — and text shaped like an escape already (``\\ud83d``),
  which is the input that made post-processing an ``ensure_ascii=True`` string
  with a regex unworkable;
* **injection-shaped** text: SQL fragments, Jinja delimiters, a ``)`` that would
  close the emitted call;
* the **over-cap boundary**, in its own arm, because the cap belongs to
  ``_rename_projection_block`` and not to ``_python_string_literal`` — see below.

LONE SURROGATES, HANDLED EXPLICITLY
-----------------------------------
A Python ``str`` may contain a lone surrogate, and such a string **cannot be
encoded to UTF-8 at all** — so a rendered job containing one raw would fail to
write. The escaping is what makes the input emittable: the literal is pure ASCII,
so it encodes even when its own input does not. That asymmetry is asserted
(``test_an_unencodable_original_name_still_produces_an_encodable_literal``) rather
than assumed, and the round trip through ``ast.literal_eval`` holds for lone
surrogates too, which is why they need no special case in the emission. This is
stated because the alternative reading — "surrogates are undefined behaviour here"
— would have licensed skipping them, and they are the neighbourhood of the one
mechanism failure on record.

THE CAP, AND WHY IT SPLITS PROPERTY 2 INTO TWO ARMS
---------------------------------------------------
``_python_string_literal`` has **no** length cap: it is total on ``str``, and the
first arm quantifies over strings without a length precondition. The cap lives in
``_rename_projection_block`` (``_MAX_ORIGINAL_NAME_CHARS = 300``), which **refuses
loudly** above it rather than truncating, because ``withColumnRenamed`` is a
silent no-op on a name no column carries. So the sink arm is stated as a total
disjunction across that boundary — at or below the cap the block emits a
round-tripping call; above it, it raises ``StepFailure`` — which is the boundary
as a property, where 6.5 pinned it at the two adjacent examples (300 and 301).

LEDGER
------
* ``LGR-1e135d2-11`` (**template / ratified**) governs what happens when a property
  here fails: ``.hypothesis/examples`` is not committed, so a counterexample does
  not survive a fresh clone and must be pinned as an **example test in the tree**.
  No counterexample was found on this run — see the task report — so the pinning
  section below is empty by outcome, and that is stated rather than left implied.
* ``LGR-1e135d2-2`` — the injection-free, AWS-free seam these properties hang off.
* ``LGR-a419843-2`` — untouched here by construction: this file exercises the
  emission seam only, never ``_declared_source_fields`` /
  ``_validate_mapped_source_fields``.
* ``LGR-3e45e65-1`` — the unit layer's region is pinned by
  ``tests/unit/conftest.py``; the ``setdefault`` calls below are no-ops kept for
  parity with the sibling files in this directory.
* Neither ``validate_identifier`` nor ``shared/identifier_utils.py`` is modified
  (do-NOTs 2 and 3). This file imports nothing from it and reaches it only
  transitively, through the block under test. Its caller count has now been
  measured three ways and they disagree: the spec says **42**, task 6.5 measured
  **56**, and ``codegraph callers validate_identifier`` — the **CLI** interface,
  because no codegraph MCP tool was available in this session — reports **20**
  (15 named entries). The conclusion is the same under every figure, and it is why
  W4's sanitization happens upstream of the validator rather than inside it.
"""

import ast
import os
import sys

import pytest
from hypothesis import find, given, settings
from hypothesis import strategies as st

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

# Imported directly, for the reason the sibling files give: `code_assembly` owns
# no AWS client and reads no environment (`LGR-1e135d2-2`), so importing the
# handler module would join the `sys.modules["lambda_function"]` contest for
# nothing.
import code_assembly as _ca

from step_errors import StepFailure

_MAX_CHARS = _ca._MAX_ORIGINAL_NAME_CHARS

# ---------------------------------------------------------------------------
# Property 2's generator
# ---------------------------------------------------------------------------

# The structural characters of the sink, the ones that have historically been got
# wrong, and text shaped like something the emission must NOT interpret.
_HOSTILE_FRAGMENTS = st.sampled_from(
    [
        '"',
        "'",
        "\\",
        '\\"',
        "\\\\",
        "\n",
        "\r",
        "\r\n",
        "\t",
        "\x00",
        "\x1b",
        "\x7f",
        "\u0085",  # NEL — a line break to str.splitlines(), not to Python source
        "\u2028",  # LINE SEPARATOR — likewise
        "\ufeff",
        "\u200b",
        "café",
        "日本語",
        "🙂",  # astral: the region `ensure_ascii=True` got wrong
        "\U0010ffff",  # the last code point
        "\ud83d",  # a lone HIGH surrogate — half of the emoji above
        "\ude42",  # a lone LOW surrogate
        "\\ud83d",  # ESCAPE-SHAPED TEXT, not an escape
        '") or 1=1 --',
        "'; DROP TABLE t; --",
        '", "injected_name")',  # would close the emitted call, if interpolated
        "{{ jinja }}",
        "{% raw %}",
        "#comment",
        "first name",
        "2024-total",
    ]
)

# Lone surrogates are NOT reachable through `st.text()` — measured, not assumed:
# `find(st.text(min_size=1), <is a surrogate>)` raises `NoSuchExample`. They are
# in the input space of a Python `str`, so they are added on purpose.
_SURROGATES = st.characters(min_codepoint=0xD800, max_codepoint=0xDFFF)

_TEXT_PIECES = st.one_of(
    _HOSTILE_FRAGMENTS,
    _SURROGATES,
    st.text(max_size=4),
    st.characters(),
)

# Any string, with the hazards at a density a uniform generator would never
# reach. `st.text()` stays in the union so the ordinary case is not lost.
_ORIGINAL_NAMES = st.one_of(
    st.text(max_size=40),
    st.lists(_TEXT_PIECES, max_size=10).map("".join),
)

# The over-cap arm. Built by repetition so the length is reached without
# generating 300 characters one draw at a time, and it carries hostile content so
# the refusal is not only exercised on benign text.
_OVER_CAP_NAMES = st.builds(
    lambda piece, extra: (piece * (_MAX_CHARS // max(len(piece), 1) + 2)) + extra,
    _HOSTILE_FRAGMENTS,
    st.text(max_size=8),
).filter(lambda name: len(name) > _MAX_CHARS)

# A target that satisfies the Identifier_Allowlist, so the sink arm exercises the
# ORIGINAL name's escaping rather than tripping `validate_identifier` first.
_SAFE_TARGET = "safe_name"


def _sole_projection_line(block: str) -> str:
    (line,) = [line for line in block.splitlines() if line.startswith("df = df.withColumnRenamed(")]
    return line


# ===========================================================================
# Property 2: The emitted original-name literal round-trips
# ===========================================================================


@given(original=_ORIGINAL_NAMES)
@settings(max_examples=400, deadline=None)
def test_the_emitted_original_name_literal_round_trips(original: str) -> None:
    """Feature: p55-format-support-and-teardown, Property 2: The emitted
    original-name literal round-trips.

    *For any* string, the emitted original-name literal parses as a Python string
    equal to the input.

    **Validates: Requirements 11.1**

    Four things are asserted, and the third is the injection guard proper:

    1. the literal round-trips through ``ast.literal_eval``;
    2. it is pure ASCII, which is the property W4-D2 asked ``ensure_ascii=True``
       for and which 6.5 kept by another mechanism;
    3. it parses as **exactly one** ``ast.Constant`` holding a ``str`` — so
       whatever the input, the emitted text is a single string literal and cannot
       introduce a token. That is do-NOT 17 made executable: an original name
       reaches the rendered job only as an escaped literal, never as a bare
       interpolation;
    4. it is double-quoted, which is why ``repr()`` was rejected.
    """
    literal = _ca._python_string_literal(original)

    assert ast.literal_eval(literal) == original
    assert literal.isascii(), "the emitted literal must be pure ASCII for any input (W4-D2)"

    expression = ast.parse(literal, mode="eval").body
    assert isinstance(expression, ast.Constant), f"emitted {literal!r} is not a single literal"
    assert isinstance(expression.value, str)
    assert expression.value == original

    assert literal.startswith('"') and literal.endswith('"')


@given(original=st.one_of(_ORIGINAL_NAMES, _OVER_CAP_NAMES))
@settings(max_examples=400, deadline=None)
def test_the_emitted_projection_line_round_trips_or_refuses_over_the_cap(original: str) -> None:
    """Feature: p55-format-support-and-teardown, Property 2: The emitted
    original-name literal round-trips — at the sink that emits it.

    *For any* string, ``_rename_projection_block`` either emits one
    ``withColumnRenamed`` call whose first argument is a string literal equal to
    the input, or — above ``_MAX_ORIGINAL_NAME_CHARS`` — refuses with a
    ``StepFailure``. Total across that boundary, which 6.5 pinned at the two
    adjacent examples (300 and 301).

    **Validates: Requirements 11.1**

    The AST shape is asserted rather than the text: the emitted statement must be
    exactly one call to ``withColumnRenamed`` with two ``str`` constants. A name
    that injected anything would show up as a second statement, a different call,
    or a non-constant argument.
    """
    if len(original) > _MAX_CHARS:
        with pytest.raises(StepFailure):
            _ca._rename_projection_block([(original, _SAFE_TARGET)])
        return

    block = _ca._rename_projection_block([(original, _SAFE_TARGET)])
    line = _sole_projection_line(block)
    assert line.isascii()

    module = ast.parse(line)
    (statement,) = module.body
    assert isinstance(statement, ast.Assign)
    call = statement.value
    assert isinstance(call, ast.Call)
    assert isinstance(call.func, ast.Attribute)
    assert call.func.attr == "withColumnRenamed"
    assert not call.keywords
    emitted_original, emitted_target = call.args
    assert isinstance(emitted_original, ast.Constant) and isinstance(emitted_original.value, str)
    assert isinstance(emitted_target, ast.Constant) and isinstance(emitted_target.value, str)
    assert emitted_original.value == original
    assert emitted_target.value == _SAFE_TARGET


@given(original=_ORIGINAL_NAMES)
@settings(max_examples=200, deadline=None)
def test_an_unencodable_original_name_still_produces_an_encodable_literal(original: str) -> None:
    """Feature: p55-format-support-and-teardown, Property 2: The emitted
    original-name literal round-trips — including for input that cannot be
    written at all.

    A Python ``str`` carrying a lone surrogate **cannot be encoded to UTF-8**, so
    a rendered job containing one raw could not be written to S3. The escaping is
    what makes such a name emittable: the literal is pure ASCII, so it encodes
    even when its own input does not. Asserted rather than assumed, because it is
    the reason lone surrogates need no special case in the emission.

    **Validates: Requirements 11.1**
    """
    literal = _ca._python_string_literal(original)
    literal.encode("utf-8")  # must not raise, for ANY original

    try:
        original.encode("utf-8")
    except UnicodeEncodeError:
        # The interesting half: unencodable input, encodable literal, and the
        # round trip still holds through `ast.literal_eval`.
        assert ast.literal_eval(literal) == original


def test_the_property_2_generator_reaches_each_hazard() -> None:
    """The generator's reach is asserted, not asserted-in-prose.

    ``hypothesis.find`` raises ``NoSuchExample`` if the strategy cannot produce a
    matching value, so a generator that quietly stops exercising the region where
    the mechanism was wrong once fails the suite instead of passing it vacuously.
    Follows the shape task 6.1's property file established.
    """
    hazards = {
        "astral (> U+FFFF)": lambda s: any(ord(c) > 0xFFFF for c in s),
        "lone surrogate": lambda s: any(0xD800 <= ord(c) <= 0xDFFF for c in s),
        "control character": lambda s: any(ord(c) < 0x20 for c in s),
        "NUL": lambda s: "\x00" in s,
        "double quote": lambda s: '"' in s,
        "backslash": lambda s: "\\" in s,
        "newline": lambda s: "\n" in s,
        "escape-shaped text": lambda s: "\\u" in s,
        "call-closing shape": lambda s: '")' in s,
    }
    for name, predicate in hazards.items():
        found = find(_ORIGINAL_NAMES, predicate)
        assert predicate(found), name

    over_cap = find(_OVER_CAP_NAMES, lambda s: len(s) > _MAX_CHARS)
    assert len(over_cap) > _MAX_CHARS


# ===========================================================================
# Property 3: Renaming preserves which column is read
# ===========================================================================

# Deliberately overlapping pools: the point is columns that fail the
# Identifier_Allowlist for the reasons real CSV headers fail it, beside
# conforming columns that must pass through untouched.
_NON_CONFORMING = st.sampled_from(
    [
        "first name",
        "2024-total",
        "e-mail",
        "a.b",
        "col#1",
        "  spaced  ",
        'quote"d',
        "back\\slash",
        "tab\there",
        "new\nline",
        "café",
        "emoji 🙂",
        "\ud83d",
        "nul\x00byte",
        "日本語",
        "",  # a header cell that is empty — non-conforming, and a legal `str`
    ]
)

_CONFORMING = st.sampled_from(["id", "email", "first_name", "_2024_total", "amount", "col_1", "total"])

_TARGET_STEM_VALUES = ("first_name", "total", "email", "col", "value", "_2024_total")
_TARGET_STEMS = st.sampled_from(_TARGET_STEM_VALUES)


@st.composite
def _rename_scenario(draw) -> tuple[list[str], list[tuple[str, str]]]:
    """A distinct source column list plus a VALID Rename_Manifest over it.

    Valid in the sense R10.3/R10.4 and design Property 1 guarantee at derivation:
    the target names are pairwise distinct, satisfy the Identifier_Allowlist, and
    collide with no retained (unrenamed) column name. The property asserts that
    precondition on the generated data rather than trusting this docstring.

    Not derived by calling ``derive_rename_manifest`` — see the module docstring
    for why a consumer test must not re-derive its own input.
    """
    # Drawn as two unique lists and then permuted, rather than name-by-name under
    # a not-yet-seen filter: `unique=True` is native, so the filter-rejection rate
    # stays low and a renamed column can land anywhere in the order — which is
    # what makes the interleaving hazard below reachable.
    non_conforming = draw(st.lists(_NON_CONFORMING, unique=True, max_size=4))
    conforming = draw(st.lists(_CONFORMING, unique=True, max_size=3))
    columns = draw(st.permutations(non_conforming + conforming))

    renamed_originals = set(non_conforming)
    retained = set(conforming)
    used: set[str] = set(retained)
    pairs: list[tuple[str, str]] = []
    for index, name in enumerate(columns):
        if name not in renamed_originals:
            continue
        stem = draw(_TARGET_STEMS)
        target = stem if stem not in used else f"{stem}_{index}"
        while target in used:  # pragma: no cover - the index suffix already settles it
            target = f"{target}_x"
        used.add(target)
        pairs.append((name, target))

    return columns, pairs


class _ModelFrame:
    """A name-keyed column collection modelling ``withColumnRenamed``'s contract.

    Each column carries an opaque payload, so "which column is read" is a
    checkable identity rather than a name comparison.

    ``withColumnRenamed`` is a **silent no-op** when no column carries the given
    name — that is Spark's documented behaviour, and it is exactly why 6.5's
    length cap refuses instead of truncating (a truncated literal would match no
    column and the rename would quietly not happen). The no-op is reproduced and
    **counted**, so a mangled literal surfaces as that failure rather than as a
    ``KeyError`` from this stub.
    """

    def __init__(self, columns: list[tuple[str, object]], no_ops: int = 0) -> None:
        self.columns = list(columns)
        self.no_ops = no_ops

    # Spark's own camelCase name, deliberately: the emitted line calls it.
    def withColumnRenamed(self, existing: str, new: str) -> "_ModelFrame":
        if all(name != existing for name, _ in self.columns):
            return _ModelFrame(self.columns, self.no_ops + 1)
        renamed = [(new if name == existing else name, payload) for name, payload in self.columns]
        return _ModelFrame(renamed, self.no_ops)

    def read(self, name: str) -> object:
        matches = [payload for column_name, payload in self.columns if column_name == name]
        if not matches:
            raise KeyError(name)
        assert len(matches) == 1, f"column {name!r} is ambiguous — {len(matches)} columns carry it"
        return matches[0]

    def has(self, name: str) -> bool:
        return any(column_name == name for column_name, _ in self.columns)


def _apply_projection(block: str, columns: list[str]) -> tuple[_ModelFrame, _ModelFrame]:
    """Execute the emitted projection against a model frame; return (before, after).

    ``exec`` is the point rather than a shortcut: Property 3 is a statement about
    emitted **source text**, and executing it is what makes "the rename happens"
    mean anything. Property 2 already covers "the text parses".
    """
    before = _ModelFrame([(name, f"payload::{index}::{name}") for index, name in enumerate(columns)])
    namespace: dict[str, object] = {"df": before}
    # `exec` on purpose: the artifact under test IS emitted source text. Scoped to
    # a namespace holding one model frame, over a block this test generated.
    exec(compile(block, "<rename-projection>", "exec"), namespace)
    after = namespace["df"]
    assert isinstance(after, _ModelFrame)
    return before, after


@given(scenario=_rename_scenario())
@settings(max_examples=300, deadline=None)
def test_renaming_preserves_which_column_is_read(scenario: tuple[list[str], list[tuple[str, str]]]) -> None:
    """Feature: p55-format-support-and-teardown, Property 3: Renaming preserves
    which column is read.

    *For any* source column name list, applying the rename then reading a column
    by its approved name yields the column the original name selected.

    **Validates: Requirements 11.1, 11.2**

    Executed against a model of ``withColumnRenamed`` — see the module docstring
    for why this rather than a claim about ``_rename_lookup``'s shape.
    """
    columns, pairs = scenario

    # The generator's own precondition, asserted rather than trusted. Production
    # manifests earn it at derivation (R10.3/R10.4, design Property 1).
    targets = [target for _, target in pairs]
    assert len(set(targets)) == len(targets)
    retained = set(columns) - {original for original, _ in pairs}
    assert not (set(targets) & retained)

    block = _ca._rename_projection_block(pairs)
    before, after = _apply_projection(block, columns)

    assert after.no_ops == 0, "a rename that matched no column is a silent no-op — the literal is wrong"

    for original, target in pairs:
        assert after.read(target) is before.read(original), (
            f"reading {target!r} after the rename must yield the column {original!r} selected"
        )

    for name in retained:
        assert after.read(name) is before.read(name), f"a column nobody renamed must still answer to {name!r}"

    # Nothing was added, dropped, reordered or duplicated: the projection renames
    # and does nothing else.
    lookup = _ca._rename_lookup(pairs)
    assert [name for name, _ in after.columns] == [lookup.get(name, name) for name in columns]
    assert [payload for _, payload in after.columns] == [payload for _, payload in before.columns]


@given(scenario=_rename_scenario())
@settings(max_examples=300, deadline=None)
def test_the_original_name_no_longer_selects_after_the_rename(
    scenario: tuple[list[str], list[tuple[str, str]]],
) -> None:
    """Feature: p55-format-support-and-teardown, Property 3: Renaming preserves
    which column is read — the write side of the asymmetry.

    R11.2 requires the target to be written under the **approved** name, so after
    the projection the original name must select nothing: everything downstream —
    the transform, the DQDL rules, the ``MERGE INTO`` — sees approved names only.
    Stated as the complement of the property above so the asymmetry is checked in
    both directions rather than only in the direction that is easy to satisfy.

    A renamed original that is *also* someone else's approved target would break
    this, and the generator excludes that by the same precondition R10.3/R10.4
    guarantee upstream — asserted here.

    **Validates: Requirements 11.1, 11.2**
    """
    columns, pairs = scenario
    block = _ca._rename_projection_block(pairs)
    _, after = _apply_projection(block, columns)

    targets = {target for _, target in pairs}
    for original, _ in pairs:
        assert original not in targets  # the generator's precondition, restated where it bites
        assert not after.has(original), f"{original!r} must not select a column after the rename (R11.2)"


def test_the_property_3_generator_reaches_each_hazard() -> None:
    """The scenarios that make Property 3 non-vacuous are reachable.

    Without these, the property could pass on nothing but single-column
    all-conforming lists — where the block is empty and every assertion is
    trivially true.
    """
    hazards = {
        "at least two renames": lambda scenario: len(scenario[1]) >= 2,
        "a conforming column between two renamed ones": lambda scenario: any(
            not _is_renamed(scenario, index)
            and any(_is_renamed(scenario, before) for before in range(index))
            and any(_is_renamed(scenario, after) for after in range(index + 1, len(scenario[0])))
            for index in range(len(scenario[0]))
        ),
        "a non-ASCII original": lambda scenario: any(not original.isascii() for original, _ in scenario[1]),
        "a target that had to be de-collided": lambda scenario: any(
            target not in _TARGET_STEM_VALUES for _, target in scenario[1]
        ),
        "columns but no rename at all (the inert case)": lambda scenario: bool(scenario[0]) and not scenario[1],
    }
    for name, predicate in hazards.items():
        found = find(_rename_scenario(), predicate)
        assert predicate(found), name


def _is_renamed(scenario: tuple[list[str], list[tuple[str, str]]], index: int) -> bool:
    originals = {original for original, _ in scenario[1]}
    return scenario[0][index] in originals


# ===========================================================================
# Pinned counterexamples (LGR-1e135d2-11)
# ===========================================================================
#
# `.hypothesis/examples` is not committed, so a counterexample does not survive a
# fresh clone and belongs in the tree as an example test. **Neither property
# produced one on this task's run** — recorded as an outcome, not as an absence of
# looking — so this section is deliberately empty. A future failure's shrunk input
# goes here, beside the property it falsified.
