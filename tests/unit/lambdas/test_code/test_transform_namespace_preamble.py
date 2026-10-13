# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Glue session binds the names the transform block was written against.

Spec ``real-spark-verification``, defect 3 — the third live defect on the Glue
verification path, found by a real run after defects 1 and 2 landed. Run
``run-2026-08-18-18-52-03-a8de6fca`` (account 777788889999, ``us-west-2``,
``VERIFICATION_EXECUTOR=glue``)::

    Glue session created: adi-verify-95eda138b1d9 (G.1X x 2, idle timeout 1 min)
    Glue session adi-verify-95eda138b1d9 is READY
    Glue statement 0 submitted for stage 'test data generation'      -> ok
    Glue statement 1 submitted for stage 'transform execution'
    Glue statement failed at stage 'transform execution'
    Glue transform execution failed: NameError: name 'col' is not defined

Defect 2's dedent worked — the statement parsed and Spark evaluated it, which is
how evaluation got as far as an *argument*. What was missing was the ambient
namespace: the transform block references ``col``, ``lit``, ``initcap`` and
friends as bare names because in the generated Glue job they are bound at module
scope, and this runner submits only the transform block.

**Validates: Requirement 1.4** — "execute the Transform_Block without
translating it". Reproducing the execution environment is not a translation, and
this file is where that distinction is made checkable: the tests below assert
both that the names are bound and that the transform statement's own text did
not move.

What this file covers, and what it does not
-------------------------------------------
Here: the preamble's content, that it is bound by the time the transform runs,
that it lands in the frame-build statement rather than in a fourth statement or
in the transform statement, and that the alias forms bind aliases only.

Not here — ``tests/contracts/test_transform_namespace_matches_templates.py``
owns the **drift fence**, which parses ``pyspark_templates/*.j2`` and asserts the
preamble still covers what they bind (``LGR-28b4d75-5``, template / ratified: a
test written from the producer's output cannot detect the producer drifting). A
test in this file would be written from the runner's own constants and could not
see a template adding a function.

Sibling coverage, not duplicated:

* ``test_transform_statement_indentation.py`` — defect 2, and the line-for-line
  ``strip()``-equality property that is the reason the preamble is not prepended
  to the transform statement.
* ``test_validation_statement.py`` — the validation statement's ``_F`` namespace,
  which is deliberately namespaced and therefore binds no bare ``col``.
* ``test_shadow_orchestration.py::test_the_worst_case_bound_still_covers_the_runners_own_budgets``
  — the three-statement budget fence, which hardcodes ``3 *`` and is the reason a
  fourth statement was rejected.

**No Spark.** ``pyspark`` is deliberately not a dependency (design §3 do-NOT 7,
``LGR-4dd9b42-1``), so the emitted lines are executed against stub
``pyspark.sql.functions`` / ``pyspark.sql.types`` modules — the same technique
``test_validation_statement.py`` uses. Executing rather than only string-matching
is the point: a preamble that parses but binds the wrong name is exactly the
defect class this closes.
"""

import ast
import os
import sys
import types
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(_REPO_ROOT, "shared"))

import glue_session_runner
from glue_session_runner import (
    STAGE_FRAME_BUILD,
    STAGE_TRANSFORM,
    TRANSFORM_FUNCTION_IMPORTS,
    TRANSFORM_TYPE_IMPORTS,
    FrameSource,
    GlueSessionTestRunner,
    build_synthetic_frame_code,
    build_transform_namespace_code,
    frame_build_statement,
    transform_namespace_bound_names,
    transform_statement,
)

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"
SCHEMA = {"properties": {"name": {"type": "string"}, "amount": {"type": "number"}}}
TARGET_SCHEMA = {"properties": {"name": {"type": "string"}}}

GENERATED_STDOUT = 'GENERATED:{"record_count": 100, "columns": ["name"]}'
VALIDATION_STDOUT = 'VALIDATION:{"record_count": 100, "schema_compliance": 100.0}'

#: The construct from the live failure, verbatim in spirit: a bare ``col`` and a
#: bare ``initcap`` as arguments. This is also the construct the pandas proxy
#: refuses by name, which is why it reaches Spark at all (R3.1).
LIVE_DEFECT_TRANSFORM = 'df_transformed = df.withColumn("name", initcap(col("name")))'


def _synthetic_source() -> FrameSource:
    return FrameSource(mode="synthetic", requested_mode="synthetic")


def _ok(text: str = "") -> dict:
    return {"State": "AVAILABLE", "Output": {"Status": "ok", "Data": {"TextPlain": text}}}


@pytest.fixture
def glue():
    """A Glue client wired so a full three-stage run succeeds."""
    client = MagicMock()
    client.create_session.return_value = {"Session": {"Id": "created"}}
    client.get_session.return_value = {"Session": {"Status": "READY"}}
    client.run_statement.side_effect = [{"Id": 1}, {"Id": 2}, {"Id": 3}]
    client.get_statement.side_effect = [
        {"Statement": _ok(GENERATED_STDOUT)},
        {"Statement": _ok()},
        {"Statement": _ok(VALIDATION_STDOUT)},
    ]
    return client


@pytest.fixture
def runner(glue):
    with patch("glue_session_runner.boto3.client", return_value=glue), patch("glue_session_runner.time.sleep"):
        yield GlueSessionTestRunner(REGION, role_arn=ROLE_ARN)


def _submitted(glue: MagicMock) -> list[str]:
    return [call.kwargs["Code"] for call in glue.run_statement.call_args_list]


def _stages(glue: MagicMock) -> int:
    return len(glue.run_statement.call_args_list)


@pytest.fixture
def stub_pyspark(monkeypatch: pytest.MonkeyPatch):
    """Install ``pyspark.sql.functions`` and ``pyspark.sql.types`` stubs.

    Every attribute is a distinct sentinel object carrying its own source name, so
    a binding can be traced back to the attribute it came from — which is what
    lets the alias tests assert that ``spark_count`` is ``functions.count`` and
    not something that merely exists.
    """

    class _Sentinel:
        def __init__(self, name: str):
            self.pyspark_name = name

        def __call__(self, *args, **kwargs):
            # A `MagicMock` and not `self`, so a chained call such as
            # `col("d").cast(DateType())` resolves. The identity assertions read
            # `pyspark_name` off the MODULE attribute, not off a call result, so
            # returning something else here costs them nothing.
            return MagicMock()

    functions = types.ModuleType("pyspark.sql.functions")
    dtypes = types.ModuleType("pyspark.sql.types")

    # Populate from the runner's own lists: the SOURCE name (left of ``as``), so
    # the stub provides what the import line asks for rather than what it binds.
    for entry in TRANSFORM_FUNCTION_IMPORTS:
        source_name = entry.split(" as ")[0].strip()
        setattr(functions, source_name, _Sentinel(source_name))
    for entry in TRANSFORM_TYPE_IMPORTS:
        source_name = entry.split(" as ")[0].strip()
        setattr(dtypes, source_name, _Sentinel(source_name))

    sql = types.ModuleType("pyspark.sql")
    sql.functions = functions
    sql.types = dtypes

    monkeypatch.setitem(sys.modules, "pyspark", types.ModuleType("pyspark"))
    monkeypatch.setitem(sys.modules, "pyspark.sql", sql)
    monkeypatch.setitem(sys.modules, "pyspark.sql.functions", functions)
    monkeypatch.setitem(sys.modules, "pyspark.sql.types", dtypes)
    return functions, dtypes


# ---------------------------------------------------------------------------
# The preamble itself
# ---------------------------------------------------------------------------


class TestThePreambleExecutes:
    """It is not enough that it parses — it has to bind."""

    def test_the_emitted_lines_compile_at_top_level(self):
        """A Glue statement is a fresh top-level scope, as defect 2 established."""
        compile(build_transform_namespace_code(), "<stdin>", "exec")

    def test_executing_the_preamble_binds_every_declared_name(self, stub_pyspark):
        """The claim :func:`transform_namespace_bound_names` makes, executed.

        That helper is read by the contract test as the subject of the drift
        fence, so if it over-reported the fence would pass on names nothing
        actually binds.
        """
        namespace: dict = {}
        exec(build_transform_namespace_code(), namespace)

        bound = set(namespace) - {"__builtins__"}

        assert bound == set(transform_namespace_bound_names())

    def test_the_live_defects_construct_evaluates_after_the_preamble(self, stub_pyspark):
        """``initcap(col("name"))`` — the expression that raised in production.

        Executed in a namespace built by the preamble plus a stub frame, so this
        fails with ``NameError`` if either name stops being bound. This is the
        regression test for the defect itself rather than for its fix's shape.
        """
        namespace: dict = {}
        exec(build_transform_namespace_code(), namespace)
        namespace["df"] = MagicMock()

        exec(LIVE_DEFECT_TRANSFORM, namespace)

        assert "df_transformed" in namespace

    def test_a_type_reference_in_a_cast_evaluates(self, stub_pyspark):
        """The types half, which the generated auto-cast block also uses.

        A transform is free to write ``.cast(DateType())``, and that would have
        failed with the identical ``NameError`` on a different name.
        """
        namespace: dict = {}
        exec(build_transform_namespace_code(), namespace)
        namespace["df"] = MagicMock()

        exec('df_transformed = df.withColumn("d", col("d").cast(DateType()))', namespace)

        assert "df_transformed" in namespace


class TestTheAliasesAreReproducedExactly:
    """``count as spark_count`` binds the alias and not the bare name."""

    @pytest.mark.parametrize(
        ("alias", "source_name"),
        [("spark_count", "count"), ("spark_max", "max")],
    )
    def test_an_aliased_name_binds_the_alias_to_the_right_object(self, stub_pyspark, alias, source_name):
        namespace: dict = {}
        exec(build_transform_namespace_code(), namespace)

        assert alias in namespace, f"the templates bind {alias!r}; the session does not"
        assert namespace[alias].pyspark_name == source_name

    @pytest.mark.parametrize("bare", ["count", "max"])
    def test_the_bare_form_of_an_aliased_name_is_not_bound(self, stub_pyspark, bare):
        """The false-pass direction, and it is the one that ships a broken job.

        The templates do **not** bind bare ``count`` or bare ``max`` at module
        scope. Binding them here would let a transform using ``count()`` verify
        green and then fail with ``NameError`` in production — the live defect,
        pointing the other way.
        """
        namespace: dict = {}
        exec(build_transform_namespace_code(), namespace)

        assert bare not in namespace, (
            f"the session binds bare {bare!r}, which the generated job does not — a transform "
            f"using it would verify green and fail in production"
        )


# ---------------------------------------------------------------------------
# Placement: the frame-build statement, and nowhere else
# ---------------------------------------------------------------------------


class TestPlacement:
    """Where the preamble goes, asserted with the reasons attached."""

    def test_the_frame_build_statement_carries_it(self):
        statement = frame_build_statement(_synthetic_source(), SCHEMA)

        assert build_transform_namespace_code() in statement

    def test_the_frame_code_is_still_the_statements_leading_text(self):
        """Why the preamble is appended rather than prepended.

        ``test_frame_refusal_precedes_session_prop.py:526`` and
        ``test_sample_frame_dispatch.py:617`` assert the submitted statement
        ``startswith`` the frame builder's output. Prepending would have forced
        those to be relaxed to ``in``, which is a weaker assertion bought for a
        placement that gains nothing.
        """
        statement = frame_build_statement(_synthetic_source(), SCHEMA)

        assert statement.startswith(build_synthetic_frame_code(SCHEMA))

    def test_the_whole_frame_build_statement_still_compiles(self):
        """Frame code + preamble + marker, as one statement."""
        compile(frame_build_statement(_synthetic_source(), SCHEMA), "<stdin>", "exec")

    def test_the_transform_statement_carries_no_import(self):
        """R1.4's text half — the transform statement is the block and nothing else.

        ``test_transform_statement_indentation.py`` asserts line-for-line
        ``strip()``-equality with the extracted block; this states the same
        constraint from the preamble's side, so a later "simplification" that
        prepends the imports to the transform fails in both places.
        """
        stage, code = transform_statement(LIVE_DEFECT_TRANSFORM)

        assert stage == STAGE_TRANSFORM
        assert code == LIVE_DEFECT_TRANSFORM
        assert "import" not in code

    def test_the_run_still_submits_exactly_three_statements(self, runner, glue):
        """The reason a fourth statement was rejected, pinned.

        ``SHADOW_GLUE_WORST_CASE_SECONDS`` is ``180 + 3 x 120 + 120`` and the
        fence that guards it (``test_shadow_orchestration.py:232``) hardcodes the
        same ``3 *``. A fourth statement would raise the real worst case to 780 s
        while both the constant and its fence still said 660 — silently. So the
        count is part of the contract, not an implementation detail.
        """
        runner.run_test(LIVE_DEFECT_TRANSFORM, SCHEMA, TARGET_SCHEMA)

        assert _stages(glue) == 3

    def test_the_names_are_bound_by_the_statement_before_the_transform(self, runner, glue):
        """Ordering is what makes it work: the session is stateful across statements.

        Asserted on submission order rather than on content alone — a preamble in
        a statement that ran *after* the transform would bind nothing in time,
        which is precisely the trap the validation statement's ``_F`` import
        already falls into by design.
        """
        runner.run_test(LIVE_DEFECT_TRANSFORM, SCHEMA, TARGET_SCHEMA)

        submitted = _submitted(glue)
        preamble = build_transform_namespace_code()

        assert preamble in submitted[0], "the preamble is not in the frame-build statement"
        assert preamble not in submitted[1], "the transform statement should carry no imports"
        assert "df_transformed" in submitted[1]

    def test_the_stage_label_of_the_carrying_statement_is_the_frame_build(self, runner, glue):
        """Where an import failure would be attributed, stated rather than implied.

        The accepted cost of this placement: a name missing from the session's
        PySpark is reported at the frame-build stage. That is a fact about setting
        the session up rather than about the AI's transform, so the attribution is
        defensible — but it should be visible in a test rather than only in a
        docstring.
        """
        runner.run_test(LIVE_DEFECT_TRANSFORM, SCHEMA, TARGET_SCHEMA)

        stages = [call.kwargs.get("Code") for call in glue.run_statement.call_args_list]
        assert build_transform_namespace_code() in stages[0]
        assert STAGE_FRAME_BUILD == "test data generation"


# ---------------------------------------------------------------------------
# The runner module itself stays free of pyspark
# ---------------------------------------------------------------------------


def test_the_runner_module_still_imports_no_pyspark():
    """Design §3 do-NOT 7 / ``LGR-4dd9b42-1`` — and the constraint in the brief.

    The preamble is **text emitted into a Glue statement**, not a Python import
    in the Lambda. Importing ``pyspark.sql.functions`` here would fail at module
    import and take the whole handler with it. Asserted over the AST so a string
    literal containing the words does not trip it, which is exactly the shape the
    fix uses.
    """
    with open(glue_session_runner.__file__) as handle:
        tree = ast.parse(handle.read())

    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])

    assert "pyspark" not in modules
