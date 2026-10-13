# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 3 — convert correctly, or refuse honestly; never both wrong (P32 T9).

Spec: `.kiro/specs/p32-test-gate-truth/`. `design.md` §6, Property 3:

  _For any_ transform block containing ≥2 chained `.withColumn`,
  `otherwise(<non-literal>)`, ≥2 chained `.when`, or `.withColumn` chained with
  `.select`, `convert` SHALL either raise `ConversionError` naming the construct
  and line number, or return code that **executes without error against a
  representative frame**, is **semantically equivalent** to the input, and whose
  `constructs_converted` names **only constructs the emitted code carries**.

Bug condition: `isBugCondition_D3(X)` — the four shapes `bugfix.md` 1.7-1.10
reproduce. Statements are generated over the generation prompt's permitted
construct vocabulary (`shared/prompt_templates/transform_generation.j2:20-21`:
`.select() .withColumn() .withColumnRenamed() .cast() .filter() .drop()`, and
`col lit trim lower upper to_date to_timestamp when concat_ws regexp_replace …`)
rather than over arbitrary text — the point is shapes this system really emits.

**The disjunction is tighter than "raise or convert" for three of the four
shapes**, and that is `design.md` §1, not an embellishment:

  * **2.7** — "Any number of chained `.withColumn` calls on one statement
    **converts**." `bugfix.md` 2.7 spells out why the refusal arm is closed here:
    "for the chained-`withColumn` shape specifically it SHALL convert, since the
    prompt instructs the model to produce it."
  * **2.8** — "`otherwise(<column reference>)` **converts**."
  * **2.9** — "Two or more chained `.when(...)` calls **convert**."
  * **2.10** — `.withColumn` chained with `.select` "either emits pandas that
    executes correctly **or refuses**". Only here is refusal an acceptable
    outcome, because only here is the construct arguably beyond a single-frame
    pandas proxy.

So a `ConversionError` is a **failure** of this property for the first three
shapes and a **pass** for the fourth. Without that asymmetry the property would
be green today on three of the four bugs: they all raise, naming construct and
line, so a flat "raise or convert" disjunction is already satisfied by the
defect.

**The execution clause is load-bearing** and is the reason this file carries a
pandas stand-in at all. `bugfix.md`, Defect 3 properties: "The 1.10 case is the
reason the assertion covers execution and not just the absence of a raise: today
`convert(X)` returns successfully for that input, so any property that only
checks 'did not raise' passes on the bug."

Expectations derive from `design.md` §1, **not** from `convert()`'s current
output (`LGR-28b4d75-5`, template provenance: a test transcribed from the
producer's output cannot detect the producer drifting, because the output is its
own oracle). Current output for all four shapes *is* the defect — and in the 1.10
case it is a *passing* conversion, which is why the captured oracle in
`test_converter_equivalence_baseline.py` deliberately excludes these four.

**RED ON UNFIXED CODE — the failure is task T9's deliverable. Task T10 fixes it
by replacing the per-line regex translator with an AST converter (`design.md`
C6).** Do not "fix" the converter or this test to make it pass here.

**Validates: Requirements 2.7, 2.8, 2.9, 2.10, 2.11**
"""

import copy
import os
import sys
import types
from dataclasses import dataclass
from unittest.mock import patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

# Reused rather than reinvented: T8's detector for a `[[...]]` subscript applied
# to an expression instead of to a frame name. That is exactly the 1.10
# signature (`df["x"].str.strip()[["a"]]` — a `[["a"]]` subscript on a Series).
# T8 deliberately did not point it at the 1.10 input because that shape is this
# property's business.
from .test_select_output_column_refusal import _mangled_subscripts

# ──────────────────────────────────────────────────────────────────────
# A representative frame.
#
# Values are chosen so every generated transform is OBSERVABLE: surrounding
# whitespace and mixed case make trim/lower/upper visible, and the literals the
# `when` generator draws from overlap these values so branches actually fire.
# A frame of clean lowercase strings would let a converter that dropped a
# transform still compare equal.
# ──────────────────────────────────────────────────────────────────────

REPRESENTATIVE_FRAME: dict[str, list[str]] = {
    "name": ["  Ada  ", "Grace", "  linus"],
    "city": ["Berlin ", " OSLO", "kyoto"],
    "state": ["CA", "NY", "TX"],
    "status": ["active", "ACTIVE", " pending "],
}

SOURCE_COLUMNS = tuple(REPRESENTATIVE_FRAME)
STRING_FUNCS = ("trim", "lower", "upper")
# Drawn to overlap the frame's own values, so a `when` predicate matches some
# rows and misses others within one three-row frame.
MATCH_LITERALS = ("CA", "NY", "TX", "active", "nope")
RESULT_LITERALS = ("California", "NewYork", "Texas", "yes", "no")


# ──────────────────────────────────────────────────────────────────────
# pandas / numpy stand-ins.
#
# pandas is not installed in the test venv (the same constraint
# `test_to_date_regression.py` and `test_alias_conversion.py` work around), so
# "executes without error against a representative frame" runs against a
# purpose-built stand-in.
#
# The load-bearing fidelity requirement is NEGATIVE: `Series[["a"]]` must raise
# `KeyError`, exactly as pandas does when "a" is not in the series' index. That
# is the 1.10 runtime failure the residual scan cannot see. `TestHarnessFidelity`
# pins it, so a reader can tell a genuine counterexample from a stand-in
# artifact.
# ──────────────────────────────────────────────────────────────────────


class _StrAccessor:
    """The `.str` accessor — only the three ops the prompt's vocabulary needs."""

    def __init__(self, values: list):
        self._values = values

    def strip(self):
        return _Series([v.strip() if isinstance(v, str) else v for v in self._values])

    def lower(self):
        return _Series([v.lower() if isinstance(v, str) else v for v in self._values])

    def upper(self):
        return _Series([v.upper() if isinstance(v, str) else v for v in self._values])


class _Series:
    """A pandas.Series stand-in over a positional (RangeIndex) list of values."""

    def __init__(self, values, name=None):
        self._values = list(values)
        self.name = name

    @property
    def str(self):
        return _StrAccessor(self._values)

    def rename(self, name):
        return _Series(self._values, name=name)

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def __eq__(self, other):
        """Elementwise comparison, like pandas — a boolean mask, not a bool."""
        if isinstance(other, _Series):
            return _Series([a == b for a, b in zip(self._values, other._values, strict=True)])
        return _Series([value == other for value in self._values])

    __hash__ = None

    def __getitem__(self, key):
        """Positional lookup, and a `KeyError` for a list-of-labels subscript.

        This is the fidelity that matters. A series over a RangeIndex has no
        label "a", so `series[["a"]]` raises in real pandas — which is what turns
        1.10's mangled `df["x"].str.strip()[["a"]]` into a runtime `KeyError`
        inside the Code Interpreter instead of a conversion failure the checker
        could report.
        """
        if isinstance(key, list):
            raise KeyError(f"None of {key} are in the [index]")
        return self._values[key]


class _Frame:
    """A pandas.DataFrame stand-in: column access, assignment, copy, subset."""

    def __init__(self, data: dict):
        self._data = {name: list(values) for name, values in data.items()}

    @property
    def columns(self) -> list:
        return list(self._data)

    def copy(self):
        return _Frame(copy.deepcopy(self._data))

    def to_dict(self) -> dict:
        return {name: list(values) for name, values in self._data.items()}

    def __len__(self):
        return len(next(iter(self._data.values()), []))

    def __getitem__(self, key):
        if isinstance(key, list):
            missing = [name for name in key if name not in self._data]
            if missing:
                raise KeyError(f"None of {missing} are in the [columns]")
            return _Frame({name: self._data[name] for name in key})
        if isinstance(key, _Series):  # boolean row mask
            keep = [index for index, flag in enumerate(key) if flag]
            return _Frame({name: [values[i] for i in keep] for name, values in self._data.items()})
        return _Series(self._data[key], name=key)

    def __setitem__(self, name, value):
        if isinstance(value, _Series | list):
            self._data[name] = list(value)
        else:  # scalar broadcast, as pandas does for `df["c"] = "x"`
            self._data[name] = [value] * len(self)

    def rename(self, columns=None):
        mapping = columns or {}
        return _Frame({mapping.get(name, name): values for name, values in self._data.items()})

    def drop(self, columns=None):
        dropped = set(columns or [])
        return _Frame({name: values for name, values in self._data.items() if name not in dropped})


def _fake_pandas() -> types.ModuleType:
    module = types.ModuleType("pandas")
    module.DataFrame = _Frame
    module.Series = _Series
    return module


def _fake_numpy() -> types.ModuleType:
    module = types.ModuleType("numpy")

    def where(condition, if_true, if_false):
        def pick(value, index):
            return value[index] if isinstance(value, _Series) else value

        return _Series(
            [pick(if_true, i) if flag else pick(if_false, i) for i, flag in enumerate(condition)],
        )

    module.where = where
    return module


def execute_converted(code: str) -> dict:
    """Execute *code* against a copy of the representative frame.

    Returns the resulting `df_transformed` as a plain dict of lists. Any
    exception propagates — an emitted-code runtime error is the property
    violation 2.10 is about, and must not be swallowed.
    """
    namespace: dict = {"df": _Frame(REPRESENTATIVE_FRAME)}
    with patch.dict(sys.modules, {"pandas": _fake_pandas(), "numpy": _fake_numpy()}):
        # Executing the converter's own output IS the check (2.10's execution clause).
        exec(code, namespace)
    assert "df_transformed" in namespace, "emitted code did not produce df_transformed"
    result = namespace["df_transformed"]
    assert isinstance(result, _Frame), f"df_transformed is {type(result).__name__}, not a frame"
    return result.to_dict()


# ──────────────────────────────────────────────────────────────────────
# The input model.
#
# One structure renders the PySpark AND drives the reference interpreter, so
# "semantically equivalent" is checked against an independent evaluation of the
# input rather than against the converter's own output.
# ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class StrFunc:
    """`trim(col("c"))` / `lower(...)` / `upper(...)`."""

    func: str
    column: str


@dataclass(frozen=True)
class WhenBranch:
    column: str
    match: str
    result: str


@dataclass(frozen=True)
class WhenChain:
    """`when(...)[.when(...)]*.otherwise(<literal | col(...)>)`."""

    branches: tuple[WhenBranch, ...]
    otherwise_literal: "str | None"
    otherwise_column: "str | None"


@dataclass(frozen=True)
class WithColumn:
    target: str
    expr: "StrFunc | WhenChain"


@dataclass(frozen=True)
class Block:
    """`df_transformed = df.withColumn(...)…[.select(...)]` — one statement."""

    with_columns: tuple[WithColumn, ...]
    select_columns: "tuple[str, ...] | None"


def render_expr(expr) -> str:
    if isinstance(expr, StrFunc):
        return f'{expr.func}(col("{expr.column}"))'
    parts = [f'when(col("{b.column}") == "{b.match}", "{b.result}")' for b in expr.branches]
    chain = parts[0] + "".join(f".{part}" for part in parts[1:])
    if expr.otherwise_column is not None:
        return f'{chain}.otherwise(col("{expr.otherwise_column}"))'
    return f'{chain}.otherwise("{expr.otherwise_literal}")'


def render_block(block: Block) -> str:
    """The PySpark statement — one line, as the prompt requires."""
    chain = "".join(f'.withColumn("{op.target}", {render_expr(op.expr)})' for op in block.with_columns)
    if block.select_columns is not None:
        chain += ".select(" + ", ".join(f'"{name}"' for name in block.select_columns) + ")"
    return "df_transformed = df" + chain


def eval_expr(expr, frame: dict) -> list:
    """Reference semantics — what Spark would compute for *expr* over *frame*."""
    if isinstance(expr, StrFunc):
        values = frame[expr.column]
        if expr.func == "trim":
            return [v.strip() for v in values]
        if expr.func == "lower":
            return [v.lower() for v in values]
        return [v.upper() for v in values]

    rows = len(next(iter(frame.values())))
    out: list = []
    for index in range(rows):
        for branch in expr.branches:
            if frame[branch.column][index] == branch.match:
                out.append(branch.result)
                break
        else:
            if expr.otherwise_column is not None:
                out.append(frame[expr.otherwise_column][index])
            else:
                out.append(expr.otherwise_literal)
    return out


def reference_result(block: Block) -> dict:
    """The frame Spark would produce — the semantic-equivalence oracle.

    `withColumn` adds (or replaces) a column on a new frame and `select`
    restricts it, so the whole statement is a fold over the running frame. The
    generators keep every read column in `SOURCE_COLUMNS` and every fresh target
    outside it, so no op in a chain reads a column an earlier op in the same
    chain wrote — read-after-write inside one statement is a separate question
    from the four shapes under test and is deliberately left out of scope here.
    """
    frame = {name: list(values) for name, values in REPRESENTATIVE_FRAME.items()}
    for op in block.with_columns:
        frame[op.target] = eval_expr(op.expr, frame)
    if block.select_columns is not None:
        frame = {name: frame[name] for name in block.select_columns}
    return frame


def input_constructs(block: Block) -> set:
    """Construct names the INPUT actually contains, in the converter's own
    vocabulary (`_CONSTRUCT_DETECTORS`, `pyspark_to_pandas.py:381-400`)."""
    names = {"withColumn", "col"}
    if block.select_columns is not None:
        names.add("select")
    for op in block.with_columns:
        if isinstance(op.expr, StrFunc):
            names.add(op.expr.func)
        else:
            names.add("when_otherwise")
    return names


# ──────────────────────────────────────────────────────────────────────
# Generators — one per disjunct of isBugCondition_D3.
# ──────────────────────────────────────────────────────────────────────

columns = st.sampled_from(SOURCE_COLUMNS)
str_funcs = st.builds(StrFunc, func=st.sampled_from(STRING_FUNCS), column=columns)


@st.composite
def when_chains(draw, *, branch_count: int, otherwise_is_column: bool):
    branches = tuple(
        WhenBranch(
            column=draw(columns),
            match=draw(st.sampled_from(MATCH_LITERALS)),
            result=draw(st.sampled_from(RESULT_LITERALS)),
        )
        for _ in range(branch_count)
    )
    if otherwise_is_column:
        return WhenChain(branches=branches, otherwise_literal=None, otherwise_column=draw(columns))
    return WhenChain(branches=branches, otherwise_literal=draw(st.sampled_from(RESULT_LITERALS)), otherwise_column=None)


@st.composite
def chained_with_column_blocks(draw):
    """1.7 — ≥2 `.withColumn` chained on ONE statement.

    The live failure chained seven; two is the minimum that triggers it. Targets
    are fresh names so the chain has no read-after-write.
    """
    count = draw(st.integers(min_value=2, max_value=7))
    ops = tuple(WithColumn(target=f"out_{i}", expr=draw(str_funcs)) for i in range(count))
    return Block(with_columns=ops, select_columns=None)


@st.composite
def otherwise_non_literal_blocks(draw):
    """1.8 — `otherwise(...)` receiving a column reference, not a quoted literal."""
    expr = draw(when_chains(branch_count=1, otherwise_is_column=True))
    target = draw(st.sampled_from([*SOURCE_COLUMNS, "out_0"]))
    return Block(with_columns=(WithColumn(target=target, expr=expr),), select_columns=None)


@st.composite
def chained_when_blocks(draw):
    """1.9 — ≥2 chained `.when(...)` in one expression."""
    expr = draw(when_chains(branch_count=draw(st.integers(min_value=2, max_value=4)), otherwise_is_column=False))
    target = draw(st.sampled_from([*SOURCE_COLUMNS, "out_0"]))
    return Block(with_columns=(WithColumn(target=target, expr=expr),), select_columns=None)


@st.composite
def with_column_then_select_blocks(draw):
    """1.10 — `.withColumn` chained with a trailing `.select` on one statement."""
    count = draw(st.integers(min_value=1, max_value=3))
    ops = tuple(WithColumn(target=f"out_{i}", expr=draw(str_funcs)) for i in range(count))
    targets = [op.target for op in ops]
    extra = draw(st.lists(columns, max_size=2, unique=True))
    selected = draw(st.permutations([*targets, *extra]).filter(bool))
    return Block(with_columns=ops, select_columns=tuple(selected))


# ──────────────────────────────────────────────────────────────────────
# Property 3.
# ──────────────────────────────────────────────────────────────────────


def assert_property_3(block: Block, *, refusal_permitted: bool) -> None:
    """Property 3 for one generated block.

    `refusal_permitted` is False for 1.7-1.9, where `design.md` §1 2.7-2.9 say
    the converter SHALL convert, and True for 1.10, where 2.10 allows either.
    """
    source = render_block(block)
    try:
        result = PySpark2PandasConverter.convert(source)
    except ConversionError as exc:
        if not refusal_permitted:
            pytest.fail(
                f"refused a shape design.md §1 requires it to CONVERT: {exc}\n"
                f"  input:     {source}\n"
                f"  construct: {exc.construct!r} on line {exc.line_number}"
            )
        # The refusal arm of 2.10 — honest means it names the construct and the
        # line, so `code_interpreter_runner.py:84` can put both in front of a
        # human rather than reporting an opaque failure.
        assert exc.construct, "ConversionError carries no construct name"
        assert exc.line_number == 1, f"refusal reported line {exc.line_number} for a one-line block"
        assert exc.construct in str(exc)
        assert f"line {exc.line_number}" in str(exc)
        return

    code = result["code"]
    report = result["conversion_report"]

    # 2.10 — never mangled output. A `[[...]]` subscript applied to an
    # expression rather than to a frame name is 1.10's signature, and it is the
    # thing a post-hoc residual scan over already-rewritten text cannot see
    # (2.11).
    assert _mangled_subscripts(code) == [], (
        f"emitted a [[...]] subscript applied to an expression, not a frame\n  input: {source}\n  code:  {code}"
    )

    # "executes without error against a representative frame" — the clause that
    # makes this property able to see 1.10 at all.
    try:
        produced = execute_converted(code)
    # Broad on purpose: 2.10's failure mode is "the emitted code raises", whatever
    # the exception type. Today it is KeyError; a different mangling would be a
    # different type and must still fail the property.
    except Exception as exc:
        pytest.fail(
            f"converted successfully but the emitted code raised {type(exc).__name__}: {exc}\n"
            f"  input: {source}\n  code:  {code}"
        )

    # "semantically equivalent to the input" — compared against an independent
    # evaluation of the PySpark, not against the converter's own output.
    expected = reference_result(block)
    assert produced == expected, f"converted code is not semantically equivalent\n  input: {source}\n  code:  {code}"

    # "constructs_converted names only constructs the emitted code carries".
    claimed = set(report["constructs_converted"])
    assert claimed <= input_constructs(block), (
        f"report claims constructs the input never contained: {sorted(claimed - input_constructs(block))}"
    )
    if "select" in claimed:
        assert block.select_columns is not None, "report claims `select` for a block with no select"
        assert list(produced) == list(block.select_columns), (
            f"report claims `select` but the emitted code does not carry select semantics: "
            f"produced columns {list(produced)}, selected {list(block.select_columns)}\n  code: {code}"
        )
    if "withColumn" in claimed:
        for op in block.with_columns:
            assert op.target in produced, f"report claims `withColumn` but {op.target!r} is absent from the output"


PROP_SETTINGS = settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROP_SETTINGS
@given(chained_with_column_blocks())
def test_chained_with_column_converts_or_refuses_honestly(block: Block):
    """Property 3 over `bugfix.md` 1.7. RED on unfixed code — the deliverable.

    Validates: Requirements 2.7, 2.11
    """
    assert_property_3(block, refusal_permitted=False)


@PROP_SETTINGS
@given(otherwise_non_literal_blocks())
def test_otherwise_with_a_column_reference_converts_or_refuses_honestly(block: Block):
    """Property 3 over `bugfix.md` 1.8. RED on unfixed code — the deliverable.

    Validates: Requirements 2.8, 2.11
    """
    assert_property_3(block, refusal_permitted=False)


@PROP_SETTINGS
@given(chained_when_blocks())
def test_chained_when_converts_or_refuses_honestly(block: Block):
    """Property 3 over `bugfix.md` 1.9. RED on unfixed code — the deliverable.

    Validates: Requirements 2.9, 2.11
    """
    assert_property_3(block, refusal_permitted=False)


@PROP_SETTINGS
@given(with_column_then_select_blocks())
def test_with_column_chained_with_select_converts_or_refuses_honestly(block: Block):
    """Property 3 over `bugfix.md` 1.10. RED on unfixed code — the deliverable.

    The one shape where refusing is acceptable (2.10), and the one shape that
    *succeeds* today — emitting `df_transformed["a"] = df["x"].str.strip()[["a"]]`
    with `constructs_converted` falsely claiming `select`. A "did not raise"
    property passes on this bug; the execution and report clauses are what catch
    it.

    Validates: Requirements 2.10, 2.11
    """
    assert_property_3(block, refusal_permitted=True)


# ──────────────────────────────────────────────────────────────────────
# Harness fidelity — so a counterexample above cannot be blamed on the
# pandas stand-in. These pass on unfixed code.
# ──────────────────────────────────────────────────────────────────────


class TestHarnessFidelity:
    """The stand-in reproduces the two behaviours the property depends on."""

    def test_a_list_subscript_on_a_series_raises_keyerror(self):
        """The 1.10 runtime failure. A series over a positional index has no
        label "a", so pandas raises — which is why the mangled output reaches
        the Code Interpreter and fails there rather than at conversion time."""
        with pytest.raises(KeyError):
            _Series(["x", "y"])[["a"]]

    def test_a_list_subscript_on_a_frame_selects_columns(self):
        """The legitimate multi-column subscript still works, so the property is
        not simply rejecting every `[[...]]`."""
        frame = _Frame({"a": [1, 2], "b": [3, 4]})
        assert frame[["a"]].to_dict() == {"a": [1, 2]}

    def test_a_frame_copy_is_independent(self):
        """`_rewrite_with_column`'s new-frame semantics (`:101-103`) depend on
        `.copy()` really copying, or a converter that mutated the source frame
        would compare equal."""
        frame = _Frame({"a": ["x"]})
        clone = frame.copy()
        clone["a"] = ["y"]
        assert frame.to_dict() == {"a": ["x"]}

    def test_series_equality_is_an_elementwise_mask(self):
        assert list(_Series(["CA", "NY"]) == "CA") == [True, False]

    def test_np_where_picks_elementwise_across_scalars_and_series(self):
        result = _fake_numpy().where(_Series([True, False]), "yes", _Series(["a", "b"]))
        assert list(result) == ["yes", "b"]

    def test_the_representative_frame_makes_every_string_func_observable(self):
        """A frame of clean values would let a dropped transform compare equal."""
        for column, values in REPRESENTATIVE_FRAME.items():
            assert any(v != v.strip() for v in values) or any(v != v.lower() for v in values), (
                f"column {column!r} is invariant under trim and lower — a dropped transform would be invisible"
            )


class TestReferenceInterpreter:
    """The equivalence oracle is checked against `bugfix.md`'s own worked shapes,
    so a bug in the reference model cannot masquerade as a converter defect."""

    def test_renders_the_1_7_shape_bugfix_reproduces(self):
        block = Block(
            with_columns=(
                WithColumn("a", StrFunc("trim", "name")),
                WithColumn("b", StrFunc("lower", "city")),
            ),
            select_columns=None,
        )
        assert render_block(block) == (
            'df_transformed = df.withColumn("a", trim(col("name"))).withColumn("b", lower(col("city")))'
        )

    def test_renders_the_1_8_shape_bugfix_reproduces(self):
        block = Block(
            with_columns=(WithColumn("state", WhenChain((WhenBranch("state", "CA", "California"),), None, "state")),),
            select_columns=None,
        )
        assert render_block(block) == (
            'df_transformed = df.withColumn("state", when(col("state") == "CA", "California").otherwise(col("state")))'
        )

    def test_renders_the_1_9_shape_bugfix_reproduces(self):
        block = Block(
            with_columns=(
                WithColumn(
                    "state",
                    WhenChain(
                        (WhenBranch("state", "CA", "California"), WhenBranch("state", "NY", "NewYork")),
                        "no",
                        None,
                    ),
                ),
            ),
            select_columns=None,
        )
        assert render_block(block) == (
            'df_transformed = df.withColumn("state", when(col("state") == "CA", "California")'
            '.when(col("state") == "NY", "NewYork").otherwise("no"))'
        )

    def test_renders_the_1_10_shape_bugfix_reproduces(self):
        block = Block(with_columns=(WithColumn("a", StrFunc("trim", "name")),), select_columns=("a",))
        assert render_block(block) == 'df_transformed = df.withColumn("a", trim(col("name"))).select("a")'

    def test_chained_with_column_adds_every_target_and_keeps_source_columns(self):
        block = Block(
            with_columns=(WithColumn("a", StrFunc("trim", "name")), WithColumn("b", StrFunc("upper", "city"))),
            select_columns=None,
        )
        result = reference_result(block)
        assert result["a"] == ["Ada", "Grace", "linus"]
        assert result["b"] == ["BERLIN ", " OSLO", "KYOTO"]
        assert result["name"] == REPRESENTATIVE_FRAME["name"]  # source frame stays pristine

    def test_when_chain_falls_through_branches_then_to_otherwise(self):
        block = Block(
            with_columns=(
                WithColumn(
                    "label",
                    WhenChain(
                        (WhenBranch("state", "CA", "California"), WhenBranch("state", "NY", "NewYork")),
                        "no",
                        None,
                    ),
                ),
            ),
            select_columns=None,
        )
        assert reference_result(block)["label"] == ["California", "NewYork", "no"]

    def test_otherwise_with_a_column_reference_passes_the_row_through(self):
        block = Block(
            with_columns=(WithColumn("label", WhenChain((WhenBranch("state", "CA", "California"),), None, "state")),),
            select_columns=None,
        )
        assert reference_result(block)["label"] == ["California", "NY", "TX"]

    def test_select_restricts_the_frame_to_the_named_columns_in_order(self):
        block = Block(with_columns=(WithColumn("a", StrFunc("trim", "name")),), select_columns=("a", "state"))
        result = reference_result(block)
        assert list(result) == ["a", "state"]
