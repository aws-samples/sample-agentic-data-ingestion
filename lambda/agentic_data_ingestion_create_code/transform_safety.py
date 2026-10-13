# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Static safety guard for the AI-generated transform block (threat model T-01).

The transform block is model output, and the model's prompt carries text an
operator or an uploaded file controls (field names, mapping transformations,
reviewer feedback). The block is rendered verbatim into the Glue job, which
runs with the per-pipeline Glue role. A prompt-injected block that imports
``os`` or ``boto3``, reaches ``spark._jvm``, or writes the frame somewhere
else would run with that role's permissions. The prompt
(``shared/prompt_templates/transform_generation.j2``) asks for a narrow shape:
read ``df``, produce ``df_transformed`` with ``.select/.withColumn/
.withColumnRenamed/.cast/.filter/.drop`` and ``pyspark.sql.functions``, and
create no classes, helper functions or imports. A prompt is a request, not a
control, so this module enforces that shape before the block is rendered.

How it works: deny by default, on the parsed AST.

* Only the statement and expression node types listed in ``_ALLOWED_NODES``
  may appear. Imports, function and class definitions, lambdas, ``with``,
  ``try``, ``raise``, ``while``, ``global``, ``del`` and the rest are rejected
  by not being on the list.
* A bare name must be ``df``/``df_transformed``, a name the job templates bind
  for the transform block (``TRANSFORM_VOCABULARY``), a small set of harmless
  builtins, or a name the block itself assigns. ``spark``, ``glueContext``,
  ``sc``, ``args``, ``boto3``, ``json``, ``sys``, ``logger``, ``eval``,
  ``open``, ``getattr`` and ``__import__`` are all module-scope or builtin
  names in the rendered job, and none of them is on the list.
* An attribute must be one of the Column and DataFrame methods in
  ``_ALLOWED_ATTRIBUTES``. That list holds transformations only: no
  ``write``, ``rdd``, ``sparkSession``, ``toPandas``, ``foreach``, ``_jdf``
  or any other ``_``-prefixed attribute.
* Spark SQL strings are a second language inside the block. ``expr()``,
  ``filter()`` and ``where()`` accept SQL text, and Spark SQL's ``reflect``,
  ``try_reflect`` and ``java_method`` call Java static methods, while a
  ``SELECT`` subquery reads a table or path the transform was never given.
  Every string literal in the block is scanned for those words, and a sink may
  only receive text the scan has already read. So a sink is called directly
  with exactly one positional argument, and that argument is a bare string
  literal. ``filter()``/``where()`` may instead take a Column expression, which
  Spark does not parse as SQL, and which must be built only from the
  vocabulary, ``df["name"]``, Column methods, operators and literals
  (``_is_column_expression``). Anything else is rejected, whatever it would
  compute: an f-string, a concatenation, a block-local variable, or a method
  called on a string (``"j*va_method(…)".replace("*", "a")``, ``"".join``,
  ``"{}".format``). Admitting ``.replace`` as a DataFrame method is safe only
  because of this rule, since the attribute check cannot see the receiver's
  type. A sink referenced without being called (``e = expr``) is rejected too,
  because a call through the new name would not be recognised as a sink.

What this does NOT cover, stated so it is not mistaken for coverage:

* The fallback full-AI path in ``lambda_function.lambda_handler`` (a target
  type with no template) emits a whole script, not a transform block, and is
  not guarded here. ``SECURITY.md`` records it as a residual.
* The guard is a static allowlist, not a sandbox. It does not reason about
  what an allowed call does with its data; least-privilege IAM on the Glue
  role (threat model T-02) is the control that bounds the blast radius.

Rejection raises ``UnsafeTransformBlockError``, a ``ValueError``. The handler
turns it into ``StepFailure("create_code", ...)`` the same way it turns
"Template code validation failed", so the run fails naming the construct and
no script is stored. It does not feed the in-process regeneration on purpose:
a model that emitted an import once under an injected prompt sees the same
prompt again, so a retry buys a second chance for the injection, not a fix.
"""

import ast
import re
from collections.abc import Iterable
from typing import Final

__all__ = [
    "TRANSFORM_VOCABULARY",
    "UnsafeTransformBlockError",
    "validate_transform_block_safety",
]


class UnsafeTransformBlockError(ValueError):
    """The AI transform block uses a construct outside the permitted shape."""


#: The names the five ``pyspark_templates/glue_job_*.py.j2`` scaffolds bind at
#: module scope from ``pyspark.sql.functions`` and ``pyspark.sql.types``, which
#: is the transform block's whole expression vocabulary in the rendered job.
#: ``spark_max`` is bound only by ``glue_job_jdbc_source.py.j2``; it is
#: admitted here for every template because a block using it in another
#: template fails with ``NameError`` at run time, which is a correctness
#: problem the verification step catches, not a safety one.
#: ``tests/contracts/test_transform_safety_vocabulary_matches_templates.py``
#: derives the template side from the templates' own source, so a template that
#: adds an import fails that test instead of making this guard reject it.
TRANSFORM_VOCABULARY: Final[frozenset[str]] = frozenset(
    {
        # pyspark.sql.functions, as bound (note `spark_count`, not `count`)
        "col",
        "lit",
        "trim",
        "lower",
        "to_date",
        "to_timestamp",
        "when",
        "concat_ws",
        "spark_count",
        "regexp_replace",
        "upper",
        "initcap",
        "substring",
        "coalesce",
        "length",
        "split",
        "array_contains",
        "expr",
        "spark_max",
        # pyspark.sql.types
        "StringType",
        "IntegerType",
        "LongType",
        "DoubleType",
        "BooleanType",
        "DateType",
        "TimestampType",
    }
)

_FRAME_NAMES: Final[frozenset[str]] = frozenset({"df", "df_transformed"})

#: Builtins with no side effect and no reach outside the interpreter. Not
#: offered by the prompt; admitted so a harmless ``int(...)`` or
#: ``len(df.columns)`` does not fail a run.
_SAFE_BUILTINS: Final[frozenset[str]] = frozenset(
    {
        "str",
        "int",
        "float",
        "bool",
        "len",
        "range",
        "list",
        "tuple",
        "dict",
        "set",
        "zip",
        "enumerate",
        "sorted",
        "min",
        "max",
        "abs",
        "round",
    }
)

#: Column and DataFrame transformations, plus the five method spellings
#: ``validate_pyspark_antipatterns`` exists to catch (``.lower()``, ``.upper()``,
#: ``.trim()``, ``.substring()``, ``.length()``). Those five are not valid
#: Column methods, but rejecting them here would pre-empt the anti-pattern
#: regeneration that fixes them, turning a recoverable mistake into a failed
#: run. They are harmless: at worst they raise ``AttributeError``.
_ALLOWED_ATTRIBUTES: Final[frozenset[str]] = frozenset(
    {
        # DataFrame transformations (no actions, no I/O)
        "select",
        "withColumn",
        "withColumns",
        "withColumnRenamed",
        "withColumnsRenamed",
        "drop",
        "filter",
        "where",
        "dropDuplicates",
        "drop_duplicates",
        "distinct",
        "fillna",
        "dropna",
        "replace",
        "na",
        "fill",
        "columns",
        "dtypes",
        # Column methods
        "alias",
        "name",
        "cast",
        "astype",
        "isNull",
        "isNotNull",
        "isNaN",
        "isin",
        "otherwise",
        "when",
        "like",
        "ilike",
        "rlike",
        "startswith",
        "endswith",
        "contains",
        "between",
        "substr",
        "eqNullSafe",
        "getItem",
        "getField",
        "asc",
        "desc",
        "asc_nulls_first",
        "asc_nulls_last",
        "desc_nulls_first",
        "desc_nulls_last",
        "bitwiseAND",
        "bitwiseOR",
        "bitwiseXOR",
        # validate_pyspark_antipatterns' COLUMN_METHODS — see the docstring above
        "lower",
        "upper",
        "trim",
        "substring",
        "length",
    }
)

#: Calls whose first argument Spark parses as SQL text.
_SQL_STRING_SINKS: Final[frozenset[str]] = frozenset({"expr", "filter", "where"})

#: The sinks that also accept a Column, which Spark evaluates without parsing.
#: ``expr()`` takes only text, so for it the argument must be a literal.
_COLUMN_OR_SQL_SINKS: Final[frozenset[str]] = frozenset({"filter", "where"})

#: Spark SQL words that reach outside the frame. ``reflect``/``try_reflect``/
#: ``java_method`` invoke Java static methods; ``select`` opens a subquery that
#: can read a table or path the transform was never given. Matched as whole
#: words, case-insensitively, in every string literal of the block. A column
#: literally named one of these would be rejected too; that is the accepted
#: false-positive cost of matching words rather than parsing Spark SQL.
_FORBIDDEN_SQL_WORDS: Final[re.Pattern[str]] = re.compile(
    r"\b(?:try_reflect|reflect|java_method|select)\b", re.IGNORECASE
)

_ALLOWED_NODES: Final[tuple[type[ast.AST], ...]] = (
    ast.Module,
    # statements
    ast.Assign,
    ast.Expr,
    ast.Pass,
    ast.If,
    ast.For,
    # expressions
    ast.Name,
    ast.Attribute,
    ast.Call,
    ast.keyword,
    ast.Constant,
    ast.List,
    ast.Tuple,
    ast.Dict,
    ast.Set,
    ast.Subscript,
    ast.Slice,
    ast.Starred,
    ast.BinOp,
    ast.UnaryOp,
    ast.BoolOp,
    ast.Compare,
    ast.IfExp,
    ast.ListComp,
    ast.GeneratorExp,
    ast.comprehension,
    ast.JoinedStr,
    ast.FormattedValue,
    # contexts and operators
    ast.Load,
    ast.Store,
    ast.operator,
    ast.unaryop,
    ast.boolop,
    ast.cmpop,
)


def _assigned_names(tree: ast.AST) -> set[str]:
    """Every name the block binds: assignment, ``for`` and comprehension targets."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            names.add(node.id)
    return names


def _call_name(func: ast.expr) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _is_column_operand(node: ast.expr) -> bool:
    """A value a Column expression may take as an argument or comparand.

    Any literal, including a string: inside a Column expression a string is a
    column name, a data value, a pattern or a type name, never SQL that Spark
    parses for function calls. ``expr()`` nested inside is still a sink and is
    checked on its own.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_is_column_operand(element) for element in node.elts)
    return _is_column_expression(node)


def _is_column_expression(node: ast.expr) -> bool:
    """Whether *node* builds a Column from parts that compute no string.

    An allowlist over syntax, not type inference. It admits a call to a
    vocabulary function (``col("a")``, ``when(...)``), ``df["a"]``, an item of
    a Column (``split(...)[0]``), a method called on a Column
    (``col("a").isNotNull()``), and comparisons, arithmetic, ``~``/``&``/``|``
    and ``and``/``or`` over those. A method called on a string literal, an
    f-string, a variable or ``df.columns`` matches none of these, so the
    expression is not a Column expression and the sink refuses it.
    """
    if isinstance(node, ast.Subscript):
        if not isinstance(node.slice, ast.Constant):
            return False
        if isinstance(node.value, ast.Name):
            return node.value.id in _FRAME_NAMES and isinstance(node.slice.value, str)
        return _is_column_expression(node.value)
    if isinstance(node, ast.Call):
        if not all(_is_column_operand(arg) for arg in node.args):
            return False
        if not all(keyword.arg is not None and _is_column_operand(keyword.value) for keyword in node.keywords):
            return False
        if isinstance(node.func, ast.Name):
            return node.func.id in TRANSFORM_VOCABULARY
        if isinstance(node.func, ast.Attribute):
            return _is_column_expression(node.func.value)
        return False
    if isinstance(node, ast.Compare):
        operands = [node.left, *node.comparators]
        return all(_is_column_operand(operand) for operand in operands) and any(
            _is_column_expression(operand) for operand in operands
        )
    if isinstance(node, ast.BinOp):
        sides = (node.left, node.right)
        return all(
            _is_column_expression(side) or (isinstance(side, ast.Constant) and not isinstance(side.value, str))
            for side in sides
        ) and any(_is_column_expression(side) for side in sides)
    if isinstance(node, ast.UnaryOp):
        return _is_column_expression(node.operand)
    if isinstance(node, ast.BoolOp):
        return all(_is_column_expression(value) for value in node.values)
    return False


def _check_sql_sink_call(call: ast.Call, sink: str, local_names: set[str]) -> None:
    """A SQL-string sink may receive only text the literal scan has read.

    The argument is a bare string literal, or for ``filter``/``where`` a Column
    expression. Exactly one positional argument and no keywords, so the text
    cannot arrive as ``expr(str=...)`` or ``expr(*parts)`` beside a harmless
    first argument.
    """
    lineno = call.lineno
    if call.keywords or len(call.args) != 1 or isinstance(call.args[0], ast.Starred):
        raise UnsafeTransformBlockError(
            f"Line {lineno}: {sink}() must be called with exactly one positional argument and no keywords"
        )
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return
    if sink in _COLUMN_OR_SQL_SINKS and _is_column_expression(arg):
        return
    # Name the familiar shapes precisely, then refuse everything else.
    _raise_for_assembled_sql_text(arg, local_names, lineno)
    accepted = "a string literal or a Column expression" if sink in _COLUMN_OR_SQL_SINKS else "a string literal"
    raise UnsafeTransformBlockError(
        f"Line {lineno}: Spark SQL text must be a literal; the argument of {sink}() must be {accepted}, "
        "not text computed from other values"
    )


def _raise_for_assembled_sql_text(arg: ast.expr, local_names: set[str], lineno: int) -> None:
    """Raise a specific message for the common ways of assembling SQL text.

    ``df.columns`` and ``df.dtypes`` are refused here as well: their strings are
    the uploaded file's own header names, so ``expr(df.columns[0])`` would run
    a header an uploader chose as Spark SQL. Returns without raising for any
    other shape; the caller then refuses it generically.
    """
    for node in ast.walk(arg):
        if isinstance(node, ast.Attribute) and node.attr in {"columns", "dtypes"}:
            raise UnsafeTransformBlockError(
                f"Line {lineno}: column names from the data are not permitted as Spark SQL text"
            )
        if isinstance(node, ast.JoinedStr):
            raise UnsafeTransformBlockError(f"Line {lineno}: an f-string is not permitted as Spark SQL text")
        if isinstance(node, ast.Name) and node.id in local_names and node.id not in _FRAME_NAMES:
            raise UnsafeTransformBlockError(
                f"Line {lineno}: Spark SQL text must be a literal, not the variable '{node.id}'"
            )
        if isinstance(node, ast.BinOp) and any(
            isinstance(side, ast.Constant) and isinstance(side.value, str) for side in (node.left, node.right)
        ):
            raise UnsafeTransformBlockError(f"Line {lineno}: Spark SQL text must be a literal, not a concatenation")
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            raise UnsafeTransformBlockError(
                f"Line {lineno}: Spark SQL text must be a literal, not the result of the string method '.{node.attr}'"
            )


def validate_transform_block_safety(transform_block: str) -> None:
    """Raise ``UnsafeTransformBlockError`` unless *transform_block* stays inside the permitted shape.

    Returns ``None`` for an accepted block. An empty or whitespace-only block is
    accepted: it renders an empty transform section, which the assembled
    script's own validation and the verification step judge, not this guard.
    An unparseable block is rejected, because a block whose safety cannot be
    checked is not known to be safe.
    """
    if not transform_block or not transform_block.strip():
        return

    try:
        tree = ast.parse(transform_block)
    except SyntaxError as exc:
        raise UnsafeTransformBlockError(
            f"Transform block is not parseable Python: {exc.msg} (line {exc.lineno})"
        ) from exc

    nodes = list(ast.walk(tree))

    # Pass 1 — node types. Breadth-first order, so the outermost disallowed
    # construct is the one named (a `def`, not the `arguments` inside it).
    for node in nodes:
        if not isinstance(node, _ALLOWED_NODES):
            raise UnsafeTransformBlockError(
                f"Line {getattr(node, 'lineno', 0)}: {type(node).__name__} is not permitted in a transform block"
            )

    local_names = _assigned_names(tree)
    for name in sorted(local_names):
        if name.startswith("_"):
            raise UnsafeTransformBlockError(f"Assigning the name '{name}' is not permitted")

    # Pass 2 — names, before attributes, so `spark.sql(...)` is refused for
    # `spark` rather than for `.sql`: the root object is what an operator needs
    # to read. Within a pass the leftmost violation is reported.
    permitted_names = _FRAME_NAMES | TRANSFORM_VOCABULARY | _SAFE_BUILTINS | local_names
    _raise_first(
        (node, f"the name '{node.id}' is not available to a transform block")
        for node in nodes
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load) and node.id not in permitted_names
    )

    # Pass 3 — attributes and mutating assignment targets. Ordered by where the
    # attribute ENDS, so `df.write.parquet(...)` names `.write`, not `.parquet`.
    violations: list[tuple[ast.AST, str]] = []
    for node in nodes:
        if isinstance(node, ast.Attribute):
            if isinstance(node.ctx, ast.Store):
                violations.append((node, "assigning to an attribute is not permitted"))
            elif node.attr not in _ALLOWED_ATTRIBUTES:
                violations.append((node, f"the attribute '.{node.attr}' is not permitted"))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            violations.append((node, "assigning to a subscript is not permitted"))
    _raise_first(violations)

    # Pass 4 — Spark SQL text: forbidden words in any literal, then sinks
    # referenced without being called, then sinks whose text is assembled
    # rather than written out.
    for node in nodes:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            match = _FORBIDDEN_SQL_WORDS.search(node.value)
            if match:
                raise UnsafeTransformBlockError(
                    f"Line {node.lineno}: the Spark SQL word '{match.group(0)}' is not permitted in a transform block"
                )
    callees = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
    for node in nodes:
        if isinstance(node, ast.Name) and node.id in _SQL_STRING_SINKS:
            name, ctx, lineno = node.id, node.ctx, node.lineno
        elif isinstance(node, ast.Attribute) and node.attr in _SQL_STRING_SINKS:
            name, ctx, lineno = node.attr, node.ctx, node.lineno
        else:
            continue
        if isinstance(ctx, ast.Load) and id(node) not in callees:
            raise UnsafeTransformBlockError(
                f"Line {lineno}: '{name}' must be called directly, not passed around or bound to another name"
            )
    for node in nodes:
        if isinstance(node, ast.Call):
            sink = _call_name(node.func)
            if sink in _SQL_STRING_SINKS:
                _check_sql_sink_call(node, sink, local_names)


def _raise_first(violations: Iterable[tuple[ast.AST, str]]) -> None:
    """Raise for the violation that ends first in the source, if there is one."""
    ordered = sorted(
        violations,
        key=lambda item: (getattr(item[0], "end_lineno", 0) or 0, getattr(item[0], "end_col_offset", 0) or 0),
    )
    if ordered:
        node, message = ordered[0]
        raise UnsafeTransformBlockError(f"Line {getattr(node, 'lineno', 0)}: {message}")
