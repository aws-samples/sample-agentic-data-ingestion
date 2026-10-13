# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 12 — the validation payload's key set is executor-independent.

Spec ``real-spark-verification`` task 6.3. **Validates: Requirement 10.1.**

    THE Glue_Session_Runner SHALL emit a Validation_Payload whose key set equals
    the key set the Pandas_Proxy emits.

An equality between two executors, so both sides have to be read. Task 6.1's
``test_validation_statement.py::TestThePayloadKeySet`` establishes it at the
example level over five hand-built frames; what follows generalises it over
generated target schemas and adds the two directions an example cannot cover — a
payload that grew a tenth key, and one that dropped a declared key.

Method: the other side is PARSED, never transcribed
---------------------------------------------------

``LGR-28b4d75-5`` (**template / ratified**) is the governing reasoning, and it is
about exactly this failure mode:

    a test written from the producer's output cannot detect the producer
    drifting, because the output is its own oracle.

That record's own answer was to derive the expectation by *parsing the other side
at test time* — it parses the frontend's TypeScript interface rather than
restating its fields. The same technique applies here, and it is available for the
same reason task 6.1's file gives: **both builders return a string of code.**
``build_validation_code`` emits PySpark and ``CodeInterpreterTestRunner.
_build_validation_code`` emits pandas, and neither runtime is installed here nor
may be (design §3 do-NOT 7, ``LGR-4dd9b42-1``). So each side's emitted text is
parsed with ``ast``, the ``print("VALIDATION:" + json.dumps({…}))`` calls are
located structurally, and the dict literals' keys are read off them.

Structural rather than textual on purpose. Task 6.1's example-level version
recovers the pandas keys with a line filter — ``line.startswith('    "')`` and
ends with a comma — which is correct today and would silently return an empty set
if that builder's f-string were reindented or its dict collapsed onto one line. An
empty set on both sides compares equal, so the weaker extraction can fail open.
:func:`_emitted_validation_dicts` cannot: it asserts the shape it found before
returning anything.

The two payloads are told apart by **position in the syntax tree**, not by their
contents: the ``NameError`` arm is the one lexically inside an
``ast.ExceptHandler``. Content-based discrimination would have made
``test_the_error_arm_key_set_also_agrees`` vacuous — an extractor that asserts the
error arm is ``{"error"}`` leaves nothing for a test of that fact to falsify.

What is asserted, and what is deliberately not
----------------------------------------------

Asserted:

* the emitted key **tuple** — the Glue side's against the pandas side's, and both
  against :data:`VALIDATION_PAYLOAD_KEYS`, in both directions
* the same equality for the ``NameError`` arm's ``{"error"}`` payload, which is a
  Validation_Payload too and is the one the parser's failure discriminator reads
* that no payload key is ``error``, because ``_parse_results`` returns early
  whenever ``"error" in val`` (``code_interpreter_runner.py:618-621``) — a tenth
  key spelled that way would turn every successful validation into a reported
  failure on both executors, and the key sets would still be equal
* that the keys survive **execution**, not only emission (see below)
* that ``_VALIDATION_PAYLOAD_EXPRESSIONS`` stays total over the key set and that
  every entry is what the emitted text actually carries

**Not** asserted: any payload *value*. R10.1 is about the key set. ``dtypes`` in
particular is deliberately unpinned — design C7 predicts every entry moves, and
task 1.1 records that the pandas side is not one vocabulary either (a string
column reports ``object`` on pandas 2.2.3 and ``str`` on 3.0.5, with nothing
pinning which pandas the managed interpreter runs). Task 6.4 owns the metric
definitions and task 6.2 owns the parser round-trip; neither is duplicated here.

Emission is not enough — the runtime arm
----------------------------------------

:class:`TestTheExecutedPayloadCarriesTheSameKeys` runs the emitted statement
against task 6.1's Spark stub. Task 6.1's module docstring records why that arm is
not optional: the first version of the payload emitted ``"missing_columns":
missing_columns`` where the local is ``missing``, which parses cleanly and fails
at run time with a ``NameError``. A key set read off the AST alone would have
called that payload correct. Eight of the nine expressions are the identity
mapping and the ninth is not, so generating the expression from the key
reintroduces exactly that defect — :data:`_VALIDATION_PAYLOAD_EXPRESSIONS` is the
guard, and :class:`TestTheExpressionMapStaysTotalOverTheKeySet` is what keeps it
load-bearing rather than decorative.

Key ORDER is pinned, and nothing depends on it — stated so
----------------------------------------------------------

``_parse_results`` reads the payload by name (``val.get("completeness", 0)`` and
its siblings, ``code_interpreter_runner.py:604-630``), so neither the parser nor
any consumer downstream of it depends on emission order. Order is pinned anyway,
as drift detection: two builders emitting the same nine keys in different orders
is a signal that one was edited without the other, which is the condition R10.1
exists to prevent. Recorded here explicitly so a later reader does not mistake the
tuple comparison for a contract on order.

The generated shapes
--------------------

Target schemas across every shape ``declared_target_columns`` distinguishes:
``properties``, ``fields``, both, an **empty** ``properties`` beside a populated
``fields`` (``properties`` wins whenever the KEY is present — p33 3.14), neither,
unrelated keys only, and ``None``. Widths from zero declared targets up. Declared
names include the awkward spellings :data:`_AWKWARD_NAMES` lists: braces both
balanced and not, dots, backticks, quotes, backslashes, newlines, the empty
string, non-ASCII, and — the group that earns its place — names colliding with the
payload's own keys (``record_count``, ``error``) and with the emitted statement's
locals (``_df``, ``_agg``, ``_cols``, ``missing``). A declared name reaches the
emitted source through ``json.dumps``, as data inside ``expected = [...]``, so
none of those can add a payload key or shadow a local; generating them is what
turns that from a reading of the builder into a check on it.

Coverage of those shapes is **pinned, not merely likely**. Measured over 400
draws, ``_target_schemas`` reaches all eight shapes, declared widths zero through
five, and every entry of :data:`_AWKWARD_NAMES` — but that is a probabilistic
claim about a seed, and these are the shapes the task names as load-bearing. So
:data:`_CANONICAL_SCHEMAS` carries one concrete schema per shape plus the whole
awkward alphabet in a single declaration, applied as ``@example`` to the three
cross-executor properties by :func:`_over_canonical_shapes`.

Two asymmetries, handled rather than generated into a failure
------------------------------------------------------------

1. **``None``.** ``declared_target_columns(None)`` returns ``[]`` because a target
   schema is optional on the Glue path; ``_build_validation_code(None)`` raises
   ``AttributeError`` on ``None.get``. The pandas key set is invariant in its
   argument — ``test_the_pandas_key_set_does_not_vary_with_the_target_schema``
   establishes that rather than assuming it — so ``None`` is compared against the
   pandas keys for ``{}``, the same "nothing declared" reading. Task 6.1's
   example-level test skips ``None`` with a bare ``if``; this names why.
2. **A list-shaped ``fields``.** Both sides refuse, by different routes, so
   neither emits a payload and the key sets are vacuously equal.
   :class:`TestAListShapedFieldsYieldsNoPayloadOnEitherSide` pins the mutual
   refusal, because a one-sided refusal would be a divergence the equality
   properties cannot see — a raising builder emits no keys to compare.

The brace-scanning parser limitation is NOT this property's
----------------------------------------------------------

Task 6.1 records a pre-existing limitation: ``_parse_results``' ``_extract_json``
counts braces without tracking string literals, so a column name carrying an
unbalanced ``}`` truncates the slice fed to ``json.loads``. It is
executor-independent — both builders print ``"columns": list(df.columns)`` and
both key ``null_counts`` by column name — and design §3 do-NOT 8 forbids touching
the parser, so it is pinned there as behaviour.

The runtime arm here therefore reads the marker **by line** rather than through
the brace scanner (:func:`_printed_payload`). ``json.dumps`` escapes newlines, so
the payload occupies exactly one line and a line-based read is exact for every
name this file generates. That keeps a failure here attributable to the KEY SET: a
frame column named ``c}d`` defeats the parser and does not perturb the payload
keys, and this property says the second part without inheriting the first.

Ledger notes — where the ledger is thin, and where it is silent
--------------------------------------------------------------

* ``LGR-28b4d75-5`` (**template / ratified**) is the one record that changed the
  approach here: it is why the pandas side is parsed rather than transcribed, and
  why the extraction asserts its own shape instead of returning a possibly-empty
  set.
* ``why`` on ``glue_session_runner.py`` returns **nothing** — the file is not yet
  committed to main, so the ledger holds no record of any decision in it,
  including 6.1's. Stated as a gap, not as an all-clear.
* ``why`` on ``code_interpreter_runner.py`` returns four commits and no record
  naming the validation payload or its keys. ``LGR-4dd9b42-1`` is the relevant one
  for this file's constraints (neither pandas nor PySpark may be installed);
  ``LGR-f6519e1-1`` governs literal fences in the converter, not the payload. The
  two broad snapshots (``LGR-0176ccc-1``, commit ``2b45b72912``) carry no records
  and match everything — the documented snapshot trap.
* ``decisions(concept="testing")`` returns five records, none about the payload.
  ``LGR-3e45e65-1`` is the one touching this file's environment: the unit layer
  pins ``AWS_REGION`` in ``tests/unit/conftest.py`` by assignment, so the
  ``setdefault`` calls below are consistent no-ops kept for module-import parity
  with every sibling in this directory.

Codegraph, for the symbols this reads
-------------------------------------

* ``build_validation_code`` (``glue_session_runner.py:2088``) — 17 callers, all
  within its own module and its covering test; covered by
  ``test_validation_statement.py``.
* ``declared_target_columns`` (``:2006``) — 5 callers, same module; same coverage.
* ``VALIDATION_PAYLOAD_KEYS`` (``:1968``) — 2 callers; same coverage.
* ``_build_validation_code`` is a method on ``CodeInterpreterTestRunner``, read
  here and never called by the Glue runner in production.

Nothing under ``lambda/`` or ``shared/`` is modified by this task: it adds one
test file. ``glue_session_runner.py`` is concurrently owned by task 8.1 and is
deliberately left alone.
"""

import ast
import json
import os
import sys
from unittest.mock import MagicMock, patch

import hypothesis.strategies as st
import pytest
from hypothesis import example, given, settings

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import glue_session_runner
from code_interpreter_runner import CodeInterpreterTestRunner
from glue_session_runner import (
    VALIDATION_PAYLOAD_KEYS,
    FrameBuildError,
    build_validation_code,
    declared_target_columns,
)

# Task 6.1's Spark stub and task 1.1's frame type, imported rather than rebuilt.
# `_execute` installs a stubbed `pyspark.sql.functions` implementing the pieces of
# documented Spark semantics the emitted arithmetic depends on; a second copy here
# would be a second thing to keep in step for no gain. The runtime arm below
# asserts only KEYS, so none of that semantics is load-bearing for this property —
# it is what makes the emitted statement runnable at all.
from tests.unit.lambdas.test_code.test_dq_definitions_characterization import NAN, Frame
from tests.unit.lambdas.test_code.test_validation_statement import _execute, _stub_frame

_SETTINGS = settings(max_examples=200, deadline=None)

_VALIDATION_MARKER_PREFIX = "VALIDATION:"

#: Declared-column spellings that are awkward to emit, grouped by why.
#:
#: The last group earns its place: a declared name reaches the emitted source
#: through `json.dumps`, as DATA in `expected = [...]`, so it can neither add a
#: payload key nor shadow one of the statement's own locals. Generating names that
#: would do both if they were interpolated is what turns that from a reading of
#: the builder into a check on it.
_AWKWARD_NAMES = (
    # Braces — balanced and not. Both are fine here; only the parser cares.
    "a{b}",
    "c}d",
    "{",
    "}",
    # Spellings needing quoting or escaping somewhere on one side or the other.
    "a.b",
    "g`h",
    'q"r',
    "s\\t",
    "u\nv",
    "",
    " ",
    "é",
    "日本",
    # Collisions with the payload's own keys and with the statement's own locals.
    "record_count",
    "null_counts",
    "missing_columns",
    "error",
    "_df",
    "_agg",
    "_cols",
    "missing",
    "expected",
)

_column_names = st.one_of(
    st.sampled_from(_AWKWARD_NAMES),
    st.text(alphabet=st.characters(exclude_categories=("Cs",)), max_size=6),
)

#: Field metadata shapes. `declared_target_columns` and the pandas builder both
#: read only `.keys()`, so the value is irrelevant to either — varied so that
#: staying irrelevant is checked rather than assumed.
_field_metas = st.sampled_from(({"type": "string"}, {"type": "integer"}, {}, None, "string", 7))

#: Every shape `declared_target_columns` distinguishes, named so a failure says
#: which one broke. `EMPTY_PROPERTIES_WITH_FIELDS` is p33 3.14's: `properties`
#: wins whenever the KEY is present, even when its value is empty.
_SCHEMA_SHAPES = (
    "PROPERTIES_ONLY",
    "FIELDS_ONLY",
    "BOTH_POPULATED",
    "EMPTY_PROPERTIES_WITH_FIELDS",
    "EMPTY_FIELDS_ONLY",
    "NEITHER",
    "UNRELATED_KEYS_ONLY",
    "NONE",
)


@st.composite
def _target_schemas(draw, *, max_columns: int = 5):
    """A target schema in one of the shapes ``declared_target_columns`` reads.

    Returns ``(schema, shape_label)``. The label is carried so an assertion names
    the shape that broke rather than only dumping the dict.
    """
    shape = draw(st.sampled_from(_SCHEMA_SHAPES))
    primary = draw(st.lists(_column_names, max_size=max_columns, unique=True))
    secondary = draw(st.lists(_column_names, max_size=max_columns, unique=True))

    def _fields(names: list[str]) -> dict:
        return {name: draw(_field_metas) for name in names}

    if shape == "PROPERTIES_ONLY":
        return {"properties": _fields(primary)}, shape
    if shape == "FIELDS_ONLY":
        return {"fields": _fields(primary)}, shape
    if shape == "BOTH_POPULATED":
        return {"properties": _fields(primary), "fields": _fields(secondary)}, shape
    if shape == "EMPTY_PROPERTIES_WITH_FIELDS":
        return {"properties": {}, "fields": _fields(primary)}, shape
    if shape == "EMPTY_FIELDS_ONLY":
        return {"fields": {}}, shape
    if shape == "NEITHER":
        return {}, shape
    if shape == "UNRELATED_KEYS_ONLY":
        return {"title": "t", "required": list(primary)}, shape
    return None, shape


#: One concrete schema per shape, plus the two width extremes and the whole
#: awkward-name alphabet in a single declaration.
#:
#: Pinned as ``@example`` rather than left to generation. Measured over 400 draws,
#: :func:`_target_schemas` reaches all eight shapes, declared widths zero through
#: five, and every entry of :data:`_AWKWARD_NAMES` — but "reaches with
#: overwhelming probability" is not "always reaches", and these are exactly the
#: shapes task 6.3 enumerates as the ones that matter. Making them explicit costs
#: nine examples and removes the RNG from the coverage claim.
_CANONICAL_SCHEMAS = (
    ({"properties": {"a": {"type": "string"}, "b": {}}}, "PROPERTIES_ONLY"),
    ({"fields": {"a": {"type": "integer"}}}, "FIELDS_ONLY"),
    ({"properties": {"a": {}}, "fields": {"b": {}}}, "BOTH_POPULATED"),
    # p33 3.14: `properties` wins because the KEY is present, though it is empty.
    ({"properties": {}, "fields": {"a": {}}}, "EMPTY_PROPERTIES_WITH_FIELDS"),
    ({"fields": {}}, "EMPTY_FIELDS_ONLY"),
    ({}, "NEITHER"),
    ({"title": "t", "required": ["a"]}, "UNRELATED_KEYS_ONLY"),
    (None, "NONE"),
    # Every awkward spelling at once, including the payload-key and local-name
    # collisions. Neither builder mutates its argument, so sharing this dict
    # across examples is safe.
    ({"properties": {name: {"type": "string"} for name in _AWKWARD_NAMES}}, "PROPERTIES_ONLY"),
)


def _over_canonical_shapes(test):
    """Stack one ``@example`` per entry of :data:`_CANONICAL_SCHEMAS`."""
    for schema_and_shape in reversed(_CANONICAL_SCHEMAS):
        test = example(schema_and_shape=schema_and_shape)(test)
    return test


@st.composite
def _frames(draw, *, max_columns: int = 4, max_rows: int = 4):
    """A stub-able frame: unique column names, cells the stub can hold.

    Column names are unique because :func:`_stub_frame` keys its rows by name, so
    a repeated name would collapse two columns and make the frame describe
    something other than what was generated. ``NAN`` is in the cell alphabet
    because it is the one value where the two executors' arithmetic diverges
    (design C7); it changes no KEY, which is the point of including it. Zero
    columns and zero rows are both reachable — the emitted statement's
    ``max(…, 1)`` guards mean neither divides by zero.
    """
    columns = tuple(draw(st.lists(_column_names, max_size=max_columns, unique=True)))
    cell = st.one_of(
        st.none(),
        st.integers(min_value=-3, max_value=3),
        st.sampled_from((NAN, 0.0, 1.5)),
        st.text(max_size=3),
    )
    row_count = draw(st.integers(min_value=0, max_value=max_rows))
    rows = tuple(tuple(draw(cell) for _ in columns) for _ in range(row_count))
    return Frame(name="generated", columns=columns, rows=rows, declared_targets=())


# ── Reading the emitted payloads off each builder's text ───────────────


def _emitted_validation_dicts(code: str) -> tuple[ast.Dict, ast.Dict]:
    """The ``(error_arm, payload)`` dict literals *code* prints after the marker.

    Located structurally — a ``print`` whose single argument is
    ``Constant("VALIDATION:") + json.dumps(<dict literal>)`` — so a reindentation
    or a collapsed dict cannot quietly change the answer.

    The two are told apart by **position in the tree**: the error arm is the print
    lexically inside an ``ast.ExceptHandler`` (both builders put it in
    ``except NameError:``), the payload is the one outside every handler. Not by
    contents, because a content-based split would make an assertion about those
    contents unfalsifiable.

    Asserts the shape it found rather than returning whatever it collected. That is
    the whole point (``LGR-28b4d75-5``): an extraction that can return nothing
    fails OPEN, since two empty key sets compare equal.
    """
    tree = ast.parse(code)
    handled = {
        id(inner) for handler in ast.walk(tree) if isinstance(handler, ast.ExceptHandler) for inner in ast.walk(handler)
    }

    in_handler: list[ast.Dict] = []
    at_top_level: list[ast.Dict] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "print"):
            continue
        if len(node.args) != 1 or node.keywords:
            continue
        argument = node.args[0]
        if not (isinstance(argument, ast.BinOp) and isinstance(argument.op, ast.Add)):
            continue
        marker, dumps = argument.left, argument.right
        if not (isinstance(marker, ast.Constant) and marker.value == _VALIDATION_MARKER_PREFIX):
            continue

        assert isinstance(dumps, ast.Call) and isinstance(dumps.func, ast.Attribute) and dumps.func.attr == "dumps", (
            "a VALIDATION marker is printed by something other than json.dumps; the payload "
            "shape both executors share has changed and this extraction no longer describes it"
        )
        assert len(dumps.args) == 1 and isinstance(dumps.args[0], ast.Dict), (
            "json.dumps is not called on a dict literal, so the payload keys are not readable "
            "statically and R10.1's key-set equality cannot be checked against this shape"
        )
        (in_handler if id(node) in handled else at_top_level).append(dumps.args[0])

    assert len(in_handler) == 1 and len(at_top_level) == 1, (
        f"expected one VALIDATION print inside an except handler and one outside it; found "
        f"{len(in_handler)} and {len(at_top_level)}"
    )
    return in_handler[0], at_top_level[0]


def _dict_keys(node: ast.Dict) -> tuple[str, ...]:
    """The key names of a payload dict literal, in emitted order.

    Every key must be a string literal: a computed key would make the emitted key
    set unknowable without executing the statement, which is precisely the
    property being checked.
    """
    keys = []
    for key in node.keys:
        assert isinstance(key, ast.Constant) and isinstance(key.value, str), (
            f"a payload key is not a string literal: {ast.dump(key) if key is not None else '**unpacking'}"
        )
        keys.append(key.value)
    return tuple(keys)


def _emitted_payload_keys(code: str) -> tuple[str, ...]:
    """The success payload's key tuple, as emitted."""
    return _dict_keys(_emitted_validation_dicts(code)[1])


def _emitted_error_arm_keys(code: str) -> tuple[str, ...]:
    """The ``NameError`` arm payload's key tuple, as emitted."""
    return _dict_keys(_emitted_validation_dicts(code)[0])


def _printed_payload(stdout: str) -> dict:
    """The payload the executed statement actually printed, read BY LINE.

    Not through ``_parse_results``' brace-depth scan, and not through task 6.1's
    ``_payload`` helper which reproduces it. That scanner does not track string
    literals, so a column name carrying an unbalanced ``}`` truncates the slice — a
    pre-existing, executor-independent limitation task 6.1 pins as behaviour and
    design §3 do-NOT 8 forbids repairing.

    ``json.dumps`` escapes newlines, so the payload occupies exactly one line and a
    line-based read is exact for every name generated here. That keeps a failure
    attributable to the KEY SET rather than to the parser, which is task 6.2's
    subject.
    """
    lines = [line for line in stdout.splitlines() if line.startswith(_VALIDATION_MARKER_PREFIX)]
    assert len(lines) == 1, f"expected one VALIDATION line, got {len(lines)} in {stdout!r}"
    return json.loads(lines[0][len(_VALIDATION_MARKER_PREFIX) :])


@pytest.fixture(scope="module")
def pandas_validation_builder():
    """``CodeInterpreterTestRunner._build_validation_code``, bound.

    Module-scoped: the builder is pure — it reads its argument and returns a
    string — and a function-scoped fixture used from a ``@given`` test is applied
    once for the whole function rather than per example, which hypothesis flags.
    The boto client is mocked because the constructor builds one; no builder
    touches it.
    """
    with patch("code_interpreter_runner.boto3") as mock_boto:
        mock_boto.client.return_value = MagicMock()
        return CodeInterpreterTestRunner(region="us-west-2")._build_validation_code


def _pandas_equivalent(schema):
    """The schema to hand the pandas builder for *schema*.

    ``None`` becomes ``{}``: both mean "nothing declared" and the pandas builder
    raises ``AttributeError`` on ``None.get`` rather than reading it. Licensed by
    ``test_the_pandas_key_set_does_not_vary_with_the_target_schema``.
    """
    return {} if schema is None else schema


# ══════════════════════════════════════════════════════════════════════
# Property 12 — the key set is the same on both sides
# ══════════════════════════════════════════════════════════════════════


class TestTheKeySetIsExecutorIndependent:
    """**Validates: Requirement 10.1**"""

    @_over_canonical_shapes
    @given(schema_and_shape=_target_schemas())
    @_SETTINGS
    def test_the_glue_key_set_equals_the_pandas_paths(self, schema_and_shape, pandas_validation_builder):
        """R10.1 directly: the two emitted key sets are equal.

        Read off the pandas builder's actual output, so either side drifting fails
        here rather than agreeing with a stale copy of itself.
        """
        schema, shape = schema_and_shape
        glue_keys = _emitted_payload_keys(build_validation_code(schema))
        pandas_keys = _emitted_payload_keys(pandas_validation_builder(_pandas_equivalent(schema)))

        assert glue_keys == pandas_keys, f"{shape}: Glue emits {glue_keys}, pandas emits {pandas_keys}"

    @_over_canonical_shapes
    @given(schema_and_shape=_target_schemas())
    @_SETTINGS
    def test_the_error_arm_key_set_also_agrees(self, schema_and_shape, pandas_validation_builder):
        """The ``NameError`` arm is a Validation_Payload too, and R10.1 covers it.

        It is also the payload the parser's failure discriminator reads
        (``"error" in val``), so the two executors agreeing on ``("error",)`` is
        what makes a Spark ``df_transformed`` miss report identically to a pandas
        one.
        """
        schema, shape = schema_and_shape
        glue_error = _emitted_error_arm_keys(build_validation_code(schema))
        pandas_error = _emitted_error_arm_keys(pandas_validation_builder(_pandas_equivalent(schema)))

        assert glue_error == pandas_error == ("error",), f"{shape}: {glue_error} against {pandas_error}"

    @_over_canonical_shapes
    @given(schema_and_shape=_target_schemas())
    @_SETTINGS
    def test_the_glue_key_set_equals_the_declared_tuple_in_both_directions(self, schema_and_shape):
        """No key dropped and no tenth key added.

        The direction an example-level test over five frames cannot give: an extra
        key fails on the extra, a missing one fails on the omission, and each
        assertion names which happened.
        """
        schema, shape = schema_and_shape
        glue_keys = _emitted_payload_keys(build_validation_code(schema))

        assert set(glue_keys) - set(VALIDATION_PAYLOAD_KEYS) == set(), f"{shape}: emitted a key R10.1 does not declare"
        assert set(VALIDATION_PAYLOAD_KEYS) - set(glue_keys) == set(), f"{shape}: dropped a declared key"
        assert len(glue_keys) == len(set(glue_keys)), f"{shape}: a key is emitted twice"
        assert glue_keys == VALIDATION_PAYLOAD_KEYS, f"{shape}: emission order moved"

    @given(first=_target_schemas(), second=_target_schemas())
    @_SETTINGS
    def test_the_pandas_key_set_does_not_vary_with_the_target_schema(self, first, second, pandas_validation_builder):
        """What licenses comparing ``None`` against the pandas keys for ``{}``.

        Established rather than assumed: the pandas payload is a fixed literal in
        an f-string today, and if it ever became conditional on the schema then the
        substitution :func:`_pandas_equivalent` makes would stop being sound.
        """
        first_schema, first_shape = first
        second_schema, second_shape = second
        first_keys = _emitted_payload_keys(pandas_validation_builder(_pandas_equivalent(first_schema)))
        second_keys = _emitted_payload_keys(pandas_validation_builder(_pandas_equivalent(second_schema)))

        assert first_keys == second_keys, f"{first_shape} against {second_shape}"

    @given(schema_and_shape=_target_schemas())
    @_SETTINGS
    def test_the_glue_key_set_does_not_vary_with_the_target_schema(self, schema_and_shape):
        """The same invariance on the side this spec adds.

        ``declared_target_columns`` changes the ``expected = [...]`` data the
        statement carries; it must not change which keys the payload has. A
        declared name colliding with a payload key (``record_count``) or with one
        of the statement's locals (``_agg``) is generated for this reason.
        """
        schema, shape = schema_and_shape
        assert _emitted_payload_keys(build_validation_code(schema)) == _emitted_payload_keys(
            build_validation_code(None)
        ), shape

    def test_no_payload_key_is_the_parsers_failure_discriminator(self):
        """``"error" in val`` means a payload key named ``error`` reports failure.

        ``_parse_results`` returns early with ``execution_successful`` left False
        whenever the payload carries an ``error`` key
        (``code_interpreter_runner.py:618-621``). A tenth key spelled that way
        would turn every successful validation into a reported failure, on both
        executors, and nothing else in this file would catch it — the two key sets
        would still be equal.
        """
        assert "error" not in VALIDATION_PAYLOAD_KEYS
        assert "error" not in _emitted_payload_keys(build_validation_code(None))

    def test_the_declared_tuple_is_the_nine_keys_r10_1_pins(self):
        """The anchor the rest of the file compares against, spelled out once."""
        assert VALIDATION_PAYLOAD_KEYS == (
            "record_count",
            "null_counts",
            "dtypes",
            "duplicates",
            "schema_compliance",
            "completeness",
            "uniqueness",
            "overall_quality",
            "missing_columns",
        )


# ══════════════════════════════════════════════════════════════════════
# The key set survives EXECUTION, not only emission
# ══════════════════════════════════════════════════════════════════════


class TestTheExecutedPayloadCarriesTheSameKeys:
    """**Validates: Requirement 10.1**

    Task 6.1's recorded defect is why this class exists: an emitted
    ``"missing_columns": missing_columns`` parses cleanly and raises ``NameError``
    at run time. A key set read off the AST alone would have passed it.
    """

    @given(schema_and_shape=_target_schemas(), frame=_frames())
    @_SETTINGS
    def test_the_executed_payload_carries_exactly_the_declared_keys(self, schema_and_shape, frame):
        """Emitted, executed, and the printed keys read back.

        Keys only. Values are task 6.4's, and ``dtypes``' values are pinned by
        nobody on purpose.
        """
        schema, shape = schema_and_shape
        with pytest.MonkeyPatch.context() as mp:
            stdout = _execute(build_validation_code(schema), {"df_transformed": _stub_frame(frame)}, mp)

        assert tuple(_printed_payload(stdout)) == VALIDATION_PAYLOAD_KEYS, f"{shape}, columns={frame.columns}"

    @given(schema_and_shape=_target_schemas(), frame=_frames())
    @_SETTINGS
    def test_the_executed_key_set_equals_the_emitted_one(self, schema_and_shape, frame):
        """Nothing is added or dropped between emission and execution.

        The two halves of this file joined: what the AST says the statement emits
        is what the statement prints.
        """
        schema, shape = schema_and_shape
        code = build_validation_code(schema)
        with pytest.MonkeyPatch.context() as mp:
            stdout = _execute(code, {"df_transformed": _stub_frame(frame)}, mp)

        assert tuple(_printed_payload(stdout)) == _emitted_payload_keys(code), shape

    @pytest.mark.parametrize("collision", VALIDATION_PAYLOAD_KEYS)
    def test_a_frame_column_named_like_a_payload_key_does_not_perturb_the_keys(self, collision):
        """A column name reaches the payload only as a ``null_counts`` sub-key.

        ``null_counts`` and ``dtypes`` are keyed BY column name, so a column called
        ``record_count`` does put that string in the payload — one level down. It
        must not appear at the top level. Parametrized over all nine key spellings
        rather than generated, because the interesting inputs are exactly those
        nine.
        """
        frame = Frame(
            name=f"column_named_{collision}",
            columns=(collision, "other"),
            rows=((1, "a"), (None, "a")),
            declared_targets=(),
        )
        with pytest.MonkeyPatch.context() as mp:
            stdout = _execute(build_validation_code(None), {"df_transformed": _stub_frame(frame)}, mp)

        payload = _printed_payload(stdout)
        assert tuple(payload) == VALIDATION_PAYLOAD_KEYS
        assert collision in payload["null_counts"], "the colliding name belongs one level down, not at the top"


# ══════════════════════════════════════════════════════════════════════
# The expression map is what keeps the ninth key honest
# ══════════════════════════════════════════════════════════════════════


class TestTheExpressionMapStaysTotalOverTheKeySet:
    """**Validates: Requirement 10.1**

    Eight of nine entries are the identity mapping and the ninth is not
    (``missing_columns`` is supplied by ``missing``), so generating the expression
    from the key reintroduces task 6.1's ``NameError``. These are the assertions
    that stop the map being quietly reduced back to a comprehension over keys.
    """

    def test_the_map_is_total_and_has_no_extra_entries(self):
        assert set(glue_session_runner._VALIDATION_PAYLOAD_EXPRESSIONS) == set(VALIDATION_PAYLOAD_KEYS)

    def test_exactly_one_entry_is_not_the_identity(self):
        """Named explicitly, because it is the whole reason the map exists."""
        expressions = glue_session_runner._VALIDATION_PAYLOAD_EXPRESSIONS
        assert expressions["missing_columns"] == "missing"
        assert [key for key, value in expressions.items() if key != value] == ["missing_columns"]

    @given(schema_and_shape=_target_schemas())
    @_SETTINGS
    def test_every_declared_expression_reaches_the_emitted_text(self, schema_and_shape):
        """The map is consulted, not merely present.

        Read off the AST rather than by substring: each payload value must be a
        bare ``Name`` and must be the one the map declares, so a builder that
        started interpolating the key instead of the expression fails here — which
        is exactly the ``NameError`` task 6.1 caught by executing.
        """
        schema, shape = schema_and_shape
        payload = _emitted_validation_dicts(build_validation_code(schema))[1]

        emitted = {}
        for key, value in zip(payload.keys, payload.values, strict=True):
            assert isinstance(value, ast.Name), (
                f"{shape}: {key.value!r} is supplied by {type(value).__name__}, not a bare name"
            )
            emitted[key.value] = value.id

        assert emitted == glue_session_runner._VALIDATION_PAYLOAD_EXPRESSIONS, shape


# ══════════════════════════════════════════════════════════════════════
# The one shape where neither side emits a payload
# ══════════════════════════════════════════════════════════════════════


class TestAListShapedFieldsYieldsNoPayloadOnEitherSide:
    """Both refuse, so the key sets are vacuously equal — pinned as mutual.

    A one-sided refusal would be a real divergence the equality properties cannot
    see, because a raising builder emits no keys to compare.
    ``shared/target_schema_resolution.py`` accepts a list-shaped ``fields``
    elsewhere, so this is a reachable shape rather than a hypothetical.

    Both refusals happen host-side, before any code string exists, so neither can
    be reached with a session already created.
    """

    _list_shaped_fields = st.lists(st.fixed_dictionaries({"name": _column_names}), max_size=4)

    @given(fields=_list_shaped_fields)
    @_SETTINGS
    def test_the_glue_builder_refuses_naming_the_vacuous_score_it_avoids(self, fields):
        with pytest.raises(FrameBuildError, match="vacuous"):
            declared_target_columns({"fields": fields})
        with pytest.raises(FrameBuildError):
            build_validation_code({"fields": fields})

    @given(fields=_list_shaped_fields)
    @_SETTINGS
    def test_the_pandas_builder_also_refuses(self, fields, pandas_validation_builder):
        """By a different route — ``AttributeError`` on ``list.keys`` — and that
        difference is recorded rather than aligned: R3.3 and design §3 do-NOT 1
        require the pandas module to stay unmodified.
        """
        with pytest.raises(AttributeError):
            pandas_validation_builder({"fields": fields})
