# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 14 — frame parity between executors.

Spec ``real-spark-verification`` task 5.2. **Validates: Requirement 10.3.**

    THE Synthetic_Frame SHALL be generated from a fixed seed, so that two
    executors given one source schema receive the same column names, the same
    row count and the same null positions.

Design §4.4's MECHANISM note is why this is a property and not a nicety:

    A shadow disagreement is only attributable to the executor if the input was
    identical. Random nulls at 5% with no seed would produce disagreements that
    are sampling noise, and the whole measurement would be worthless.

Every per-column ``null_counts`` difference shadow mode (task 10.1) records is
therefore either a real semantic divergence or an artifact of the frame. This
file is what rules out the second. ``test_synthetic_frame_builder.py::
test_null_mask_matches_the_pandas_builders_loop`` establishes the same equality
for one four-column schema; what follows generalises it over schemas and seeds,
which is task 5.2's explicit instruction.

Method: both frames are EXECUTED, neither is transcribed
--------------------------------------------------------

The comparison has to be against the pandas builder's **algorithm**, not against
a second copy of the Spark builder's — comparing the builder with itself would
only prove it is deterministic, which is strictly weaker than parity.

Neither ``pyspark`` nor ``pandas`` is a dependency of this repository and neither
may become one (design §3 do-NOT 7, ``LGR-4dd9b42-1``). Both builders emit a
**code string**, though, which is what makes this checkable without either
runtime: each side's emitted code is executed against a stub that records what it
was given, and the two recordings are compared.

* Spark side — ``build_synthetic_frame_code`` executed against a stubbed
  ``pyspark.sql.types`` and a recording ``spark``, the arrangement
  ``test_synthetic_frame_builder.py`` already uses.
* pandas side — ``_build_test_data_code`` executed against a stubbed ``pandas``
  whose ``DataFrame`` supports exactly the operations that emitted code performs
  and nothing else, so a change to what it emits fails here rather than being
  absorbed.

The null positions therefore come out of running *the pandas builder's own
emitted loop*, not out of a transcription of it.

**The one liberty, stated because it is the whole of R10.3's remaining work.**
The pandas builder's loop is unseeded today (``code_interpreter_runner.py:
529-531``), so the harness seeds the module-level ``random`` stream immediately
before executing it. ``random.seed(s)`` and ``random.Random(s)`` are the same
Mersenne Twister, which is why ``synthetic_null_rows`` uses the latter — so a
later change that seeds the pandas side reproduces these positions rather than
merely its own. What is proven here is: *once both sides are seeded to one value,
the frames are identical.* Seeding the pandas side is a one-line change that this
property makes safe to land.

The interesting cases, and the two deliberate asymmetries
--------------------------------------------------------

Generated: widths from 0 up (**the empty schema included** — both builders fall
back to ``{id, value}``, so they build the same useless frame rather than two
different useless ones), declaration order, the ``properties``/``fields`` shapes
p33 3.14 pins, absent and non-dict field metas, and awkward column names.

Two places where the builders do NOT agree are handled on purpose rather than
generated into a failure:

1. **Names containing ``"`` or ``\\``.** The Spark builder emits every name
   through ``json.dumps``; the pandas builder interpolates bare (``:513-518``),
   so such a name emits code there that either does not parse or parses as a
   DIFFERENT name — ``"c\\td"`` becomes ``c<TAB>d``, which is the quieter and
   worse of the two. That is a pandas-builder defect, out of scope under R3.3 and
   design §3 do-NOT 1. It is pinned as an asymmetry with the parity dimension
   that still holds: the mask is a function of column COUNT and POSITION, never
   of spelling (``test_null_positions_do_not_depend_on_column_names``), so an
   unemittable name costs no parity claim.
2. **A declared type outside the vocabulary.** Spark refuses with
   ``FrameBuildError``; pandas strings it silently via its ``else``. That
   narrowing is 5.1's deliberate design — Spark enforces the ``StructType``, so a
   mis-typed column changes what every operation on it does — and the refusal is
   raised host-side before any code is returned, so it cannot be reached with a
   session already created. Generated explicitly rather than filtered out.

Ledger notes
------------

* ``LGR-b2f79ef-12`` (**template / ratified**) governs the builder this parity is
  measured against: one column per declared field up to
  ``_MAX_TEST_FRAME_COLUMNS = 512``, whose derivation is the DynamoDB item-size
  limit on the state row and therefore executor-independent (R10.4). The slice is
  asserted here on both sides at exactly that width.
* ``LGR-b2f79ef-9`` (**template / ratified**) leaves ``_build_test_data_code``'s
  ``{id, value}`` fallback in place "but never scored". Mirroring it is what the
  empty-schema case checks; task 5.3 is what stops it being reached.
* ``LGR-4dd9b42-1`` is why neither runtime may be installed to check this.
* ``why`` on ``code_interpreter_runner.py:496`` returns one commit (``feb6a1db18``,
  the testing overhaul) carrying **no records at all**, and ``why`` on
  ``glue_session_runner.py`` returns nothing because the file is not yet
  committed. Stated as a gap, not as an all-clear: the governing records for this
  area are the two above, found via ``decisions(concept="schema")``.
* ``decisions(concept="testing")`` returns five records, none about frame
  construction. ``LGR-3e45e65-1`` is the one touching this file's environment —
  the unit layer pins ``AWS_REGION`` in ``tests/unit/conftest.py`` by assignment,
  so the ``setdefault`` calls below are consistent no-ops.

Production code is **not** modified by this task.
"""

import ast
import contextlib
import os
import random
import sys
import types

import hypothesis.strategies as st
import pytest
from hypothesis import given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import _MAX_TEST_FRAME_COLUMNS, CodeInterpreterTestRunner
from glue_session_runner import (
    FrameBuildError,
    build_synthetic_frame_code,
    spark_type_for,
    synthetic_frame_fields,
    synthetic_null_rows,
)

_ROWS = glue_session_runner._SYNTHETIC_FRAME_ROWS
_SEED = glue_session_runner._SYNTHETIC_FRAME_SEED

#: Every declared type the Spark builder accepts — the union of the three
#: buckets, which are copied verbatim from the pandas builder's branches. Read
#: from the module rather than transcribed, so a bucket gaining a member widens
#: this generator automatically instead of leaving the new member ungenerated.
_ACCEPTED_TYPES = sorted(
    glue_session_runner._INT_DECLARED_TYPES
    | glue_session_runner._FLOAT_DECLARED_TYPES
    | glue_session_runner._STRING_DECLARED_TYPES
)

#: Column-name characters BOTH builders can emit. The pandas builder interpolates
#: the name bare into a ``"…"`` literal, so ``"`` and ``\`` are excluded here and
#: generated separately (asymmetry 1). Braces, single quotes, ``#`` and non-ASCII
#: are all safe on both sides and included deliberately — braces because the
#: marker parser task 6.2 exercises is brace-depth-scanned, the rest because a
#: schema key is arbitrary text. With ``\`` excluded no escape sequence is
#: expressible, which is what makes this alphabet genuinely safe rather than
#: merely usually safe.
_SAFE_ALPHABET = "abzAZ_09 .-{}()[]'`~!@#$%^&*+=|:;,<>?/éö漢"

#: Adds the two characters only the Spark builder survives.
_AWKWARD_ALPHABET = _SAFE_ALPHABET + '"\\'

_MISSING = object()

_PARITY_SETTINGS = settings(max_examples=250, deadline=None)

#: For the properties that build a deliberately wide frame twice per example.
_WIDE_SETTINGS = settings(max_examples=40, deadline=None)


# ── Executing what each builder emits ─────────────────────────────────


@contextlib.contextmanager
def _stubbed_modules(modules: dict):
    """Install stub modules in ``sys.modules`` and restore on exit.

    Hand-rolled rather than ``monkeypatch``: a function-scoped fixture used from a
    ``@given`` test is applied once for the whole function and never reset between
    examples, which hypothesis flags. A context manager entered inside the body is
    per-example and needs no health-check suppression. Takes a dict rather than
    ``**kwargs`` because ``pyspark.sql.types`` is not an identifier.
    """
    saved = {name: sys.modules.get(name, _MISSING) for name in modules}
    sys.modules.update(modules)
    try:
        yield
    finally:
        for name, previous in saved.items():
            if previous is _MISSING:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = previous


@contextlib.contextmanager
def _seeded_global_random(seed: int):
    """Seed the module-level ``random`` stream, then put it back.

    The pandas builder's emitted loop calls ``random.random()`` on that stream.
    State is restored so no other test in the session inherits this seed — all
    draws for a ``@given`` body happen before the body runs, so hypothesis's own
    generation is unaffected, but leaving a seeded global stream behind is the
    kind of cross-test coupling ``LGR-3e45e65-1`` was written about.
    """
    state = random.getstate()
    random.seed(seed)
    try:
        yield
    finally:
        random.setstate(state)


class _RecordingSpark:
    """Stands in for the ``spark`` a Glue interactive session provides."""

    def __init__(self) -> None:
        self.rows: list[tuple] | None = None
        self.schema: list[tuple] | None = None

    def createDataFrame(self, rows, schema):
        self.rows = rows
        self.schema = schema
        return object()


class _FakeLoc:
    """``df.loc[boolean_mask, column] = None``."""

    def __init__(self, frame: "_FakeDataFrame") -> None:
        self._frame = frame

    def __setitem__(self, key, value) -> None:
        mask, column = key
        values = self._frame.data[column]
        for row, selected in enumerate(mask):
            if selected:
                values[row] = value


class _FakeDataFrame:
    """Exactly the operations ``_build_test_data_code``'s emitted code performs.

    ``pd.DataFrame(data)``, iterating ``.columns``, ``.loc[mask, col] = None`` and
    ``len(df)`` — and nothing else. No catch-all ``__getattr__``: if that builder
    starts emitting a fifth operation this raises, instead of quietly absorbing it
    and leaving a parity claim resting on stale knowledge of the other executor.

    Real pandas would coerce a nulled float column to ``NaN`` rather than hold
    ``None``. That is design C7's predicted dtype divergence, and it is about
    VALUES; R10.3 pins POSITIONS, and a nulled cell is a nulled cell either way.
    """

    def __init__(self, data: dict) -> None:
        self.data = {name: list(values) for name, values in data.items()}
        self.columns = list(self.data)
        self.loc = _FakeLoc(self)

    def __len__(self) -> int:
        return len(next(iter(self.data.values()))) if self.data else 0

    def null_rows(self) -> dict[str, list[int]]:
        """Row indices holding ``None``, per column — this frame's null mask.

        ``is None`` and not a falsy test: the long bucket's first value is ``0``
        and the double bucket's is ``0.0``, both of which a truthiness test would
        misreport as null.
        """
        return {name: [row for row, value in enumerate(values) if value is None] for name, values in self.data.items()}


def _stub_pyspark_modules() -> dict:
    """A ``pyspark.sql.types`` whose constructors record their own names."""
    types_module = types.ModuleType("pyspark.sql.types")
    for name in ("LongType", "DoubleType", "StringType"):
        setattr(types_module, name, (lambda captured: lambda: captured)(name))
    types_module.StructField = lambda name, spark_type, nullable: (name, spark_type, nullable)
    types_module.StructType = list
    return {
        "pyspark": types.ModuleType("pyspark"),
        "pyspark.sql": types.ModuleType("pyspark.sql"),
        "pyspark.sql.types": types_module,
    }


def _spark_frame(source_schema, *, seed: int = _SEED) -> _RecordingSpark:
    """Execute the Spark builder's emitted code; return what ``spark`` saw."""
    code = build_synthetic_frame_code(source_schema, num_rows=_ROWS, seed=seed)
    spark = _RecordingSpark()
    with _stubbed_modules(_stub_pyspark_modules()):
        exec(compile(code, "<spark_frame_build>", "exec"), {"spark": spark})
    return spark


def _pandas_code(source_schema) -> str:
    """The pandas builder's emitted code.

    ``_build_test_data_code`` reads no attribute of ``self``, so it is called
    unbound with ``None`` — the idiom ``test_synthetic_frame_builder.py`` and
    ``test_glue_result_mapping.py`` both use.
    """
    return CodeInterpreterTestRunner._build_test_data_code(None, source_schema)


def _pandas_frame(source_schema, *, seed: int = _SEED) -> _FakeDataFrame:
    """Execute the PANDAS builder's emitted code; return the frame it built."""
    pandas_module = types.ModuleType("pandas")
    pandas_module.DataFrame = _FakeDataFrame
    stubs = {"pandas": pandas_module, "numpy": types.ModuleType("numpy")}
    namespace: dict = {}
    with _stubbed_modules(stubs), _seeded_global_random(seed):
        exec(compile(_pandas_code(source_schema), "<pandas_frame_build>", "exec"), namespace)
    return namespace["df"]


def _pandas_data_dict(source_schema) -> ast.Dict:
    """The ``data = {…}`` literal, out of the emitted code's AST.

    The pandas builder's column list as that builder states it, extracted rather
    than recomputed — so a change to how it reads a schema shows up here as a
    disagreement instead of being mirrored into agreement by a shared helper.
    """
    tree = ast.parse(_pandas_code(source_schema))
    for node in ast.walk(tree):
        is_data = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "data" for target in node.targets
        )
        if is_data:
            return node.value
    raise AssertionError("the pandas builder no longer assigns a `data` dict")


def _pandas_declared_columns(source_schema) -> list[str]:
    """The ``data`` dict's keys, in order."""
    return [key.value for key in _pandas_data_dict(source_schema).keys]


def _pandas_num_rows(source_schema) -> int:
    """The ``num_rows = …`` literal the pandas builder emits."""
    tree = ast.parse(_pandas_code(source_schema))
    for node in ast.walk(tree):
        is_rows = isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "num_rows" for target in node.targets
        )
        if is_rows:
            return node.value.value
    raise AssertionError("the pandas builder no longer assigns num_rows")


def _spark_null_rows(spark: _RecordingSpark) -> dict[str, list[int]]:
    """The executed Spark frame's null mask, keyed by column name."""
    return {
        name: [row for row, values in enumerate(spark.rows) if values[index] is None]
        for index, (name, _, _) in enumerate(spark.schema)
    }


# ── Generators ────────────────────────────────────────────────────────


def _names(alphabet: str = _SAFE_ALPHABET):
    return st.text(alphabet=alphabet, min_size=0, max_size=8)


@st.composite
def _source_schemas(draw, *, min_columns: int = 0, max_columns: int = 8):
    """A source schema in a shape ``schema_extract`` or an upload produces.

    Both container keys are generated, because p33 3.14's rule — ``properties``
    wins whenever the KEY is present, even when its value is empty — is what
    decides the frame's column list, and it decides it for both builders through
    the one shared ``declared_source_fields``. Field metas vary across the three
    shapes the builders treat differently: a declared type, an empty dict
    (undeclared → string), and a bare non-dict value (also → string).
    """
    columns = draw(
        st.lists(
            st.tuples(
                _names(),
                st.sampled_from(_ACCEPTED_TYPES),
                st.sampled_from(("declared", "bare_dict", "non_dict")),
            ),
            min_size=min_columns,
            max_size=max_columns,
            unique_by=lambda triple: triple[0],
        )
    )
    fields = {}
    for name, declared, shape in columns:
        if shape == "declared":
            fields[name] = {"type": declared}
        elif shape == "bare_dict":
            fields[name] = {}
        else:
            fields[name] = declared  # a bare string where a dict was expected

    container = draw(st.sampled_from(("properties", "fields", "both")))
    if container == "properties":
        return {"type": "object", "properties": fields}
    if container == "fields":
        return {"fields": fields}
    # `properties` present and populated wins; `fields` must be ignored.
    return {"properties": fields, "fields": {"ignored_by_the_rule": {"type": "integer"}}}


_SEEDS = st.integers(min_value=-(2**32), max_value=2**32)


# ── Property 14: the three named dimensions ──────────────────────────


@given(source_schema=_source_schemas(), seed=_SEEDS)
@_PARITY_SETTINGS
def test_frame_parity_column_names_row_count_and_null_positions(source_schema, seed):
    """Property 14, one assertion per dimension R10.3 names.

    **Validates: Requirement 10.3**

    For any source schema and any seed, the two executors' frames agree on the
    column names, on the column ORDER (R10.3 says "the same column names", and a
    set equality would let one executor reorder the frame, which changes what
    every positional comparison downstream means), on the row count, and on the
    null positions.
    """
    spark = _spark_frame(source_schema, seed=seed)
    pandas = _pandas_frame(source_schema, seed=seed)
    spark_columns = [name for name, _, _ in spark.schema]

    # Column names, in order.
    assert spark_columns == pandas.columns
    # …and equal to what the pandas builder's own emitted code declares.
    assert spark_columns == _pandas_declared_columns(source_schema)

    # Row count.
    assert len(spark.rows) == len(pandas) == _ROWS
    assert _pandas_num_rows(source_schema) == _ROWS

    # Null positions, per column.
    assert _spark_null_rows(spark) == pandas.null_rows()


@given(source_schema=_source_schemas(), seed=_SEEDS)
@_PARITY_SETTINGS
def test_the_published_null_mask_is_the_one_both_frames_carry(source_schema, seed):
    """``synthetic_null_rows`` is the mask, not merely a description of one.

    **Validates: Requirement 10.3**

    The function is public so that shadow mode and this property can reason about
    the frame without executing it. That is only sound if the positions it
    returns are the positions BOTH frames actually hold — otherwise it could be
    perfectly deterministic while the emitted code nulled different cells.
    """
    columns = [name for name, _ in synthetic_frame_fields(source_schema)]
    published = synthetic_null_rows(columns, num_rows=_ROWS, seed=seed)

    assert _pandas_frame(source_schema, seed=seed).null_rows() == published
    assert _spark_null_rows(_spark_frame(source_schema, seed=seed)) == published


@given(source_schema=_source_schemas(min_columns=1), seed=_SEEDS)
@_PARITY_SETTINGS
def test_non_null_values_agree_too(source_schema, seed):
    """Parity beyond the three named dimensions, since both frames are in hand.

    R10.3 names column names, row count and null positions; equal values are what
    make those three worth having. The three bucket expressions are
    character-for-character the pandas builder's (``_VALUE_EXPRESSIONS``), so this
    is an equality rather than a similarity — and it is what stops a shadow
    ``completeness`` or ``uniqueness`` difference being an artifact of different
    inputs.
    """
    spark = _spark_frame(source_schema, seed=seed)
    pandas = _pandas_frame(source_schema, seed=seed)
    columns = [name for name, _, _ in spark.schema]

    for row in range(_ROWS):
        assert spark.rows[row] == tuple(pandas.data[name][row] for name in columns), row


# ── What the mask is, and is not, a function of ──────────────────────


@given(
    original=st.lists(_names(), min_size=1, max_size=8, unique=True),
    # `min_size=1` on each name, not on the list: an 8-name unique list drawn from
    # an alphabet that admits the empty string spends most of its draws colliding.
    replacements=st.lists(
        st.text(alphabet=_AWKWARD_ALPHABET, min_size=1, max_size=10), min_size=8, max_size=8, unique=True
    ),
    seed=_SEEDS,
)
@_PARITY_SETTINGS
def test_null_positions_do_not_depend_on_column_names(original, replacements, seed):
    """The mask is a function of column COUNT and POSITION, never of spelling.

    **Validates: Requirement 10.3**

    Load-bearing twice over. It is what makes the mask comparable across a
    rename, and it is what makes asymmetry 1 cost no parity claim: a name the
    pandas builder cannot emit is still a name whose null positions are pinned,
    because those positions were never a function of the name. The replacements
    are drawn from the AWKWARD alphabet on purpose — quotes and backslashes
    included — since that is the case this property exists to cover.
    """
    renamed = replacements[: len(original)]

    before = synthetic_null_rows(original, num_rows=_ROWS, seed=seed)
    after = synthetic_null_rows(renamed, num_rows=_ROWS, seed=seed)

    assert [before[name] for name in original] == [after[name] for name in renamed]


@given(
    schema_columns=st.lists(
        st.tuples(_names(), st.sampled_from(_ACCEPTED_TYPES)),
        min_size=2,
        max_size=6,
        unique_by=lambda pair: pair[0],
    ),
    seed=_SEEDS,
)
@_WIDE_SETTINGS
def test_declaration_order_reaches_both_frames_identically(schema_columns, seed):
    """Reversing the declaration order reverses BOTH frames' column order.

    Neither builder may sort, group by type, or otherwise normalise: the columns
    are compared positionally by shadow mode and the null mask is drawn per
    position, so an order differing between executors would move every null and
    make the whole comparison noise.
    """
    forward = {"properties": {name: {"type": declared} for name, declared in schema_columns}}
    reverse = {"properties": {name: {"type": declared} for name, declared in reversed(schema_columns)}}

    spark_forward = [name for name, _, _ in _spark_frame(forward, seed=seed).schema]
    spark_reverse = [name for name, _, _ in _spark_frame(reverse, seed=seed).schema]

    assert spark_forward == [name for name, _ in schema_columns]
    assert spark_reverse == list(reversed(spark_forward))
    assert spark_forward == _pandas_frame(forward, seed=seed).columns
    assert spark_reverse == _pandas_frame(reverse, seed=seed).columns


@given(seed=_SEEDS)
@_WIDE_SETTINGS
def test_a_different_seed_moves_the_null_positions_on_both_executors(seed):
    """The seed is what the agreement is ON, not an ignored argument.

    The complement of the parity properties above: without this, "the two agree"
    could be satisfied by two builders that both ignore the seed and null nothing,
    or null everything. Both frames must MOVE together when the seed moves. Twelve
    columns × 100 cells at ~5% makes an accidental collision between neighbouring
    seeds vanishingly unlikely while keeping the example cheap.
    """
    schema = {"properties": {f"c{index}": {"type": "integer"} for index in range(12)}}

    assert _pandas_frame(schema, seed=seed).null_rows() != _pandas_frame(schema, seed=seed + 1).null_rows()
    assert _spark_frame(schema, seed=seed).rows != _spark_frame(schema, seed=seed + 1).rows


# ── Asymmetry 1: names the pandas builder cannot emit ────────────────


@given(
    prefix=_names(),
    awkward=st.text(alphabet='"\\', min_size=1, max_size=3),
    declared=st.sampled_from(_ACCEPTED_TYPES),
)
@_PARITY_SETTINGS
def test_the_spark_builder_emits_a_quote_or_backslash_name_intact(prefix, awkward, declared):
    """Asymmetry 1, the half that holds: ``json.dumps`` survives any name.

    JSON's escapes are valid Python string escapes, so this changes no name that
    was already safe — which is why escaping here costs no parity on the names
    both builders can emit.
    """
    name = f"{prefix}{awkward}"
    spark = _spark_frame({"properties": {name: {"type": declared}}})

    assert [column for column, _, _ in spark.schema] == [name]
    assert len(spark.rows) == _ROWS


def _pandas_round_trips(name: str) -> bool:
    """Whether the pandas builder emits *name* as the column it was given.

    Two distinct failure modes, and the second is the dangerous one: the emitted
    code may not parse, or it may parse and declare a DIFFERENT name because an
    escape sequence was interpreted.
    """
    schema = {"properties": {name: {"type": "string"}}}
    try:
        return _pandas_declared_columns(schema) == [name]
    except SyntaxError:
        return False


@pytest.mark.parametrize(
    ("name", "mode"),
    [
        ('a"b', "unparseable"),  # the quote closes the literal early
        ('"', "unparseable"),
        ('both"and\\', "unparseable"),
        ("trailing\\", "unparseable"),  # the backslash escapes the closing quote
        ("c\\td", "different name"),  # parses, and declares `c<TAB>d`
        ("c\\nd", "different name"),  # parses, and declares a name with a newline
    ],
)
def test_the_pandas_builder_cannot_emit_a_quote_or_backslash_name(name, mode):
    """Asymmetry 1, the half that does not — recorded, not silently tolerated.

    ``_build_test_data_code`` interpolates the name bare into a ``"…"`` literal
    (``:513-518``). R3.3 and design §3 do-NOT 1 keep that builder out of scope
    here, so this is pinned as an observation: if it is ever fixed, this test
    fails and points at the fix.

    ``mode`` records which failure each name produces, because the two are not
    equally bad. "different name" is the quiet one — the emitted code runs and
    builds a frame whose column is not the column the schema declared, which on
    the live path would surface as a ``schema_compliance`` shortfall attributed to
    the transform. It is named here so a future fix knows which case it closed.

    Parity is unaffected. The mask never depended on spelling
    (``test_null_positions_do_not_depend_on_column_names``) and the Spark side
    emits the name intact, so no dimension R10.3 names is reachable through this
    hole — only the pandas executor's ability to build the frame at all.
    """
    assert not _pandas_round_trips(name), f"the pandas builder now emits {name!r} intact; asymmetry 1 is fixed"
    assert _spark_frame({"properties": {name: {"type": "string"}}}).schema[0][0] == name
    assert mode in ("unparseable", "different name")


@given(name=_names())
@_PARITY_SETTINGS
def test_every_safe_name_round_trips_on_both_builders(name):
    """The complement: on the shared alphabet, both builders emit the name given.

    Which is what makes asymmetry 1 an asymmetry about two characters rather than
    an open question about names generally.
    """
    schema = {"properties": {name: {"type": "string"}}}

    assert _pandas_round_trips(name)
    assert _spark_frame(schema).schema[0][0] == name


# ── Asymmetry 2: a type outside the vocabulary ───────────────────────


@given(
    unknown=st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
        lambda candidate: candidate not in _ACCEPTED_TYPES
    ),
    name=_names(),
)
@_PARITY_SETTINGS
def test_an_out_of_vocabulary_type_refuses_on_spark_and_is_strung_on_pandas(unknown, name):
    """Asymmetry 2, generated deliberately rather than filtered away.

    The Spark builder's exhaustive mapping has an explicit default that RAISES
    (task 5.1): Spark enforces the ``StructType``, so a mis-typed column changes
    what every operation on it does, and the symptom would be a transform error or
    a wrong data-quality number attributed to the transform. The pandas builder
    has an ``else`` and strings it.

    The narrowing costs no verification, because the refusal is raised host-side
    out of ``build_synthetic_frame_code`` — before any code is returned and
    therefore before any session could exist. It is a refusal to build, not a
    frame that disagrees.
    """
    schema = {"properties": {name: {"type": unknown}}}

    with pytest.raises(FrameBuildError) as excinfo:
        synthetic_frame_fields(schema)
    assert unknown in str(excinfo.value)

    with pytest.raises(FrameBuildError):
        build_synthetic_frame_code(schema)

    # The pandas builder does not refuse — that is the asymmetry, stated.
    assert _pandas_declared_columns(schema) == [name]


@given(declared=st.sampled_from(_ACCEPTED_TYPES), name=_names())
@_PARITY_SETTINGS
def test_every_accepted_type_lands_in_the_bucket_the_pandas_builder_uses(declared, name):
    """The converse of asymmetry 2: inside the vocabulary the buckets agree.

    **Validates: Requirement 10.3**

    Generalises ``test_synthetic_frame_builder.py::
    test_type_buckets_agree_with_the_pandas_builder`` over names as well as types.
    The check is on the emitted VALUE EXPRESSION, read out of the pandas builder's
    own code, so a bucket edited on one side only fails here — which matters more
    than the Spark type name, since it is the values a later ``completeness`` or
    ``dtypes`` comparison reads.
    """
    schema = {"properties": {name: {"type": declared}}}
    value_expression = _pandas_data_dict(schema).values[0]
    spark_expression = glue_session_runner._VALUE_EXPRESSIONS[spark_type_for(declared)]
    expected = ast.parse(f"[{spark_expression} for i in range(num_rows)]", mode="eval").body

    assert ast.unparse(value_expression) == ast.unparse(expected)


# ── Widths, including the ceiling and the empty schema ───────────────


@pytest.mark.parametrize("width", [0, 1, 2, 3, 17, 64])
def test_parity_holds_across_widths(width):
    """Varying widths as worked examples, the empty schema included.

    ``width=0`` is the interesting one: both builders fall back to
    ``{id, value}`` (``LGR-b2f79ef-9``), so they build the same two-column frame
    rather than two different ones. Task 5.3 is what stops that frame being
    reached on the live path; until then, mirroring it is what keeps a shadow
    comparison on such a run interpretable.
    """
    fields = {f"col_{index}": {"type": _ACCEPTED_TYPES[index % len(_ACCEPTED_TYPES)]} for index in range(width)}
    schema = {"properties": fields}

    spark = _spark_frame(schema)
    pandas = _pandas_frame(schema)

    assert [name for name, _, _ in spark.schema] == pandas.columns
    assert len(pandas.columns) == (width or 2)  # `or 2` is the `{id, value}` fallback
    assert len(spark.rows) == len(pandas) == _ROWS
    assert _spark_null_rows(spark) == pandas.null_rows()


def test_parity_holds_at_the_frame_width_ceiling():
    """Both builders slice at ``_MAX_TEST_FRAME_COLUMNS`` and agree there.

    R10.4 and ``LGR-b2f79ef-12``: the 512 is derived from the DynamoDB item-size
    limit on the state row, not from the executor, so real Spark does not change
    it — and the slice must therefore bind identically on both sides or the wider
    executor would report columns the other never had. A separate example rather
    than a generated width: 512 columns × 100 rows is slow enough that running it
    250 times would dominate this file's cost.
    """
    fields = {f"c{index}": {"type": "integer"} for index in range(_MAX_TEST_FRAME_COLUMNS + 40)}
    schema = {"properties": fields}

    spark = _spark_frame(schema)
    pandas = _pandas_frame(schema)

    assert len(spark.schema) == len(pandas.columns) == _MAX_TEST_FRAME_COLUMNS
    assert [name for name, _, _ in spark.schema] == pandas.columns
    assert _spark_null_rows(spark) == pandas.null_rows()


def test_the_default_seed_is_the_one_both_frames_use():
    """Parity at the DEFAULT arguments, which is what the live path uses.

    Every property above passes a generated seed. This is the one that pins the
    pair a run actually gets: ``_SYNTHETIC_FRAME_SEED`` with no argument at all.
    """
    fields = {"id": {"type": "integer"}, "amount": {"type": "number"}, "name": {"type": "string"}}
    schema = {"properties": fields}

    spark = _spark_frame(schema)
    pandas = _pandas_frame(schema)

    assert pandas.columns == [name for name, _, _ in spark.schema]
    assert pandas.null_rows() == synthetic_null_rows(pandas.columns)
    assert _spark_null_rows(spark) == pandas.null_rows()
