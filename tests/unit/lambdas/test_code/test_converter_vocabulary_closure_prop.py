# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A conversion that succeeds never emits an undefined name (p32 2.10, generalised).

Spec: `.kiro/specs/p32-test-gate-truth/`. This file exists because 2.10's general
clause — "it SHALL NOT emit code that raises at runtime" — had a hole that
`bugfix.md` 1.10 did not name and the AST rewrite did not close:

    df_transformed = df.withColumn("t", posexplode(col("tags")))

**converted successfully**, emitting ``posexplode(df["tags"])``, and then raised
``NameError`` inside the Code Interpreter. ``explode`` was in
``_UNSUPPORTED_FUNCTIONS``; ``posexplode`` was in neither frozenset, so it fell
through ``_Translator._function_call``'s final branch and was passed through
verbatim as though it were ordinary Python. Exactly the class of defect the spec
existed to remove — a conversion that succeeds and produces pandas that fails at
runtime, rather than refusing honestly — reached by a different mechanism, because
the rewrite changed *how* constructs are recognised, not *which* names are
enumerated.

The sweep that found it also found siblings: ``array_contains`` (imported by every
``glue_job_*.py.j2`` scaffold), ``spark_count`` (the alias
``transform_generation.j2`` explicitly tells the model to use) and ``spark_max``.
Enumerating those four fixes four names. **These two properties are what close the
class**, and they are deliberately generative rather than one example per name —
an example per name only ever documents the last hole someone noticed.

Property A — *closure over the offered vocabulary.* Read the function names the
generation prompt offers and the scaffold templates import out of those files, and
require every one of them to be a name the converter has an opinion about. This is
the sweep, automated: a name added to the prompt or to a template import without
being enumerated here fails, which is how ``array_contains``/``spark_count``/
``spark_max`` got in unnoticed.

Property B — *default refusal.* For an arbitrary identifier the converter has no
translation rule for, a `withColumn` calling it in function position refuses,
naming that identifier. This is the one that would have caught ``posexplode``
itself: it never mentions ``posexplode``, or any other name, and it holds over
identifiers nobody has thought of. Generated over both realistic
``pyspark.sql.functions`` names and arbitrary identifiers, so it is a statement
about the converter's default rather than about a list.

Both properties assert against `bugfix.md` 2.10, not against `convert()`'s output
(`LGR-28b4d75-5`, template provenance: a test transcribed from the producer's
output cannot detect the producer drifting).

**How "does not raise at runtime" is checked here.** Property 3 executes its
emitted code against a pandas stand-in, and this file reuses that harness where it
applies. But the stand-in models `DataFrame`/`Series` only, so a conversion that
emits `pd.to_datetime(...)` cannot be executed against it — and a harness gap must
not be reported as a converter defect. The universal assertion is therefore
**name-closure over the emitted code**: every name the emitted pandas reads must be
one the emitted pandas binds (`pd`, `np`, the input frame, an assignment target).
That is exactly the failure mode — `posexplode(df["tags"])` reads a name nothing
defines — and it holds without depending on how much pandas the stand-in models.
Execution runs on top of it wherever the stand-in is adequate.

**Validates: Requirements 2.10, 2.11**
"""

import ast
import keyword
import os
import re
import sys

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

import pyspark_to_pandas
from pyspark_to_pandas import ConversionError, PySpark2PandasConverter

# The execution harness is Property 3's, unchanged: 2.10's "does not raise at
# runtime" is the same clause here, so it is checked the same way rather than with
# a second stand-in that could disagree with the first.
from .test_pyspark_to_pandas_prop import SOURCE_COLUMNS, execute_converted

_REPO = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
PROMPT_PATH = os.path.join(_REPO, "shared", "prompt_templates", "transform_generation.j2")
TEMPLATES_DIR = os.path.join(_REPO, "pyspark_templates")

ENUMERATED_FUNCTIONS = (
    set(pyspark_to_pandas._SUPPORTED_FUNCTIONS)
    | set(pyspark_to_pandas._UNSUPPORTED_FUNCTIONS)
    | set(pyspark_to_pandas._UNSUPPORTED_NAMES)
)

#: Names the emitted block legitimately reads without binding them itself: the two
#: modules `convert()` prepends, and the input frame the Code Interpreter defines.
EMITTED_PRELUDE = frozenset({"pd", "np", "df"})


def unbound_names(emitted: str) -> set[str]:
    """Names *emitted* reads but never binds.

    A non-empty result is 2.10's failure mode: the Code Interpreter raises
    ``NameError`` on the first one. Parsed rather than string-matched, so a name
    inside a string literal or a comment is not a reference.
    """
    tree = ast.parse(emitted)
    bound = set(EMITTED_PRELUDE)
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            bound.add(node.asname or node.name.split(".")[0])
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
            bound.add(node.id)
    read = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    return read - bound


# ──────────────────────────────────────────────────────────────────────
# Reading the offered vocabulary out of the files that offer it.
#
# Structural extraction only. Nothing here interprets prose — the point is that
# these lists come from the artefacts the model is actually given, so they cannot
# be kept in step by hand.
# ──────────────────────────────────────────────────────────────────────


def prompt_offered_functions() -> set[str]:
    """Function names `transform_generation.j2` offers the model.

    Three places offer one: the parenthesised ``pyspark.sql.functions (...)``
    list, an ``X is imported as Y`` instruction (which is how ``spark_count``
    reaches the model), and the ``f(col("..."))`` illustrations in CRITICAL RULES.
    """
    with open(PROMPT_PATH, encoding="utf-8") as handle:
        prompt = handle.read()
    names: set[str] = set()
    listed = re.search(r"pyspark\.sql\.functions \(([^)]*)\)", prompt)
    assert listed, "the prompt no longer carries a `pyspark.sql.functions (...)` list"
    names |= {part.strip() for part in listed.group(1).split(",") if part.strip()}
    names |= set(re.findall(r"is imported as (\w+)", prompt))
    names |= set(re.findall(r"\b([a-z_]\w*)\(col\(", prompt))
    return names


def template_imported_functions() -> set[str]:
    """Names every ``glue_job_*.py.j2`` imports from ``pyspark.sql.functions``.

    Both sides of an ``as`` alias count: the scaffold binds the alias, so the
    alias is the name a transform block would call.
    """
    names: set[str] = set()
    for entry in sorted(os.listdir(TEMPLATES_DIR)):
        if not entry.endswith(".py.j2"):
            continue
        with open(os.path.join(TEMPLATES_DIR, entry), encoding="utf-8") as handle:
            for line in handle:
                if not line.startswith("from pyspark.sql.functions import"):
                    continue
                for part in line.split("import", 1)[1].split(","):
                    part = part.strip()
                    if " as " in part:
                        names.update(fragment.strip() for fragment in part.split(" as "))
                    elif part:
                        names.add(part)
    assert names, "no scaffold imports anything from pyspark.sql.functions any more"
    return names


OFFERED_FUNCTIONS = sorted(prompt_offered_functions() | template_imported_functions())


# ──────────────────────────────────────────────────────────────────────
# Property A — closure over the offered vocabulary.
# ──────────────────────────────────────────────────────────────────────


class TestOfferedVocabularyIsClosed:
    """Every function the surrounding surfaces offer is one the converter knows.

    **Validates: Requirements 2.10**
    """

    def test_the_offered_list_is_populated(self):
        """Guard: an extraction bug must not make this class vacuous."""
        assert len(OFFERED_FUNCTIONS) >= 15
        # The two the sweep found unenumerated, kept explicit so a future
        # extraction regression that silently drops them is visible.
        assert "spark_count" in OFFERED_FUNCTIONS
        assert "array_contains" in OFFERED_FUNCTIONS

    @pytest.mark.parametrize("name", OFFERED_FUNCTIONS)
    def test_offered_function_is_enumerated(self, name):
        """A name the prompt or a scaffold offers is in one of the frozensets.

        Membership is not the guarantee — Property B is — but an offered name that
        nothing enumerates cannot be documented, cannot name the leftmost
        construct of a statement carrying several, and is invisible to
        `pyspark_templates/README.md`'s construct lists.
        """
        assert name in ENUMERATED_FUNCTIONS or pyspark_to_pandas._TYPE_NAME.match(name), (
            f"{name!r} is offered to the model but appears in no frozenset"
        )

    @pytest.mark.parametrize("name", OFFERED_FUNCTIONS)
    def test_offered_function_converts_or_refuses_naming_itself(self, name):
        """Run through the real converter, not inferred from set membership.

        A supported name has to convert to code with every name bound; an
        unsupported one has to refuse under its own name, because that name is what
        reaches an operator through
        `code_interpreter_runner.classify_conversion_refusal`.
        """
        if pyspark_to_pandas._TYPE_NAME.match(name):
            pytest.skip("a type constructor is consumed by .cast(), not called on its own")
        statement = f'df_transformed = df.withColumn("out", {name}(col("{SOURCE_COLUMNS[0]}")))'
        try:
            result = PySpark2PandasConverter.convert(statement)
        except ConversionError as exc:
            assert exc.construct == name, f"refused {name!r} but named {exc.construct!r}"
            return
        emitted = result["code"]
        assert unbound_names(emitted) == set(), f"{name!r} converted to code reading undefined names:\n{emitted}"
        if "pd." not in emitted:
            # The stand-in models DataFrame/Series only, so it can execute the
            # string-function shapes but not a `pd.to_datetime` one. Where it can,
            # 2.10's execution clause is checked outright.
            execute_converted(emitted)


# ──────────────────────────────────────────────────────────────────────
# Property B — default refusal for an unrecognised bare name.
# ──────────────────────────────────────────────────────────────────────

#: Real `pyspark.sql.functions` names outside `_SUPPORTED_FUNCTIONS`, none of them
#: mentioned anywhere in the converter, the prompt, the templates or the docs.
#: `posexplode` is deliberately NOT in this list — the property must hold for
#: names nobody wrote down, which is the whole point.
UNSEEN_PYSPARK_FUNCTIONS = (
    "explode_outer",
    "posexplode_outer",
    "inline",
    "flatten",
    "element_at",
    "from_json",
    "to_json",
    "get_json_object",
    "sha2",
    "md5",
    "monotonically_increasing_id",
    "input_file_name",
    "lpad",
    "rpad",
    "translate",
    "instr",
    "locate",
    "format_string",
    "unix_timestamp",
    "from_unixtime",
    "date_add",
    "months_between",
    "weekofyear",
    "approx_count_distinct",
    "stddev",
    "percent_rank",
    "ntile",
    "nanvl",
    "greatest",
    "least",
)

#: Arbitrary identifiers, so the property is about the default rather than about a
#: curated list. Constrained to names that are actually *identifiers the converter
#: has no rule for*: a Python keyword would make the rendered statement a
#: SyntaxError, which refuses for an unrelated reason and says nothing about the
#: default. `df` is excluded for the same kind of reason — it is the input frame,
#: so `df(...)` is a different shape, not an unrecognised function.
arbitrary_identifiers = (
    st.from_regex(r"\A[a-z_][a-z_0-9]{0,15}\Z")
    .filter(lambda name: name not in ENUMERATED_FUNCTIONS)
    .filter(lambda name: name not in EMITTED_PRELUDE)
    .filter(lambda name: not keyword.iskeyword(name) and not keyword.issoftkeyword(name))
    .filter(lambda name: not pyspark_to_pandas._TYPE_NAME.match(name))
)

unrecognised_functions = st.one_of(st.sampled_from(UNSEEN_PYSPARK_FUNCTIONS), arbitrary_identifiers)

PROP_SETTINGS = settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@PROP_SETTINGS
@given(name=unrecognised_functions, column=st.sampled_from(SOURCE_COLUMNS))
def test_an_unrecognised_bare_name_refuses_naming_itself(name: str, column: str):
    """2.10 generalised: no unrecognised bare name is ever emitted.

    The refusal has to carry the name, not a generic marker — that string is what
    `classify_conversion_refusal` reads and what an operator is told to look for in
    `docs/TROUBLESHOOTING.md`.

    Validates: Requirements 2.10, 2.11
    """
    statement = f'df_transformed = df.withColumn("out", {name}(col("{column}")))'
    with pytest.raises(ConversionError) as raised:
        PySpark2PandasConverter.convert(statement)
    assert raised.value.construct == name, f"refused but named {raised.value.construct!r} instead of {name!r}"
    assert raised.value.line_number == 1
    assert name in str(raised.value)


@PROP_SETTINGS
@given(name=unrecognised_functions, column=st.sampled_from(SOURCE_COLUMNS))
def test_no_conversion_ever_emits_an_undefined_name(name: str, column: str):
    """The runtime consequence, stated directly.

    Whatever `convert` does with an unrecognised name, the one outcome 2.10
    forbids is emitting it: a name nothing defines is a `NameError` in the Code
    Interpreter, reported as a failed transform rather than as the converter gap
    it is. Asserted over the emitted text AND by parsing it, so an occurrence
    inside a string literal is not mistaken for a live reference.

    Validates: Requirements 2.10
    """
    statement = f'df_transformed = df.withColumn("out", {name}(col("{column}")))'
    try:
        emitted = PySpark2PandasConverter.convert(statement)["code"]
    except ConversionError:
        return  # refused: nothing was emitted, so nothing can be undefined
    assert unbound_names(emitted) == set(), f"emitted code reads undefined names:\n{emitted}"


# ──────────────────────────────────────────────────────────────────────
# The four names the sweep found, pinned as examples.
#
# The properties above are the guarantee; these fix the specific inputs in place
# so a reader can see what was broken, and so a regression names the same
# construct a report would have named.
# ──────────────────────────────────────────────────────────────────────


class TestTheSweptNames:
    """**Validates: Requirements 2.10**"""

    @pytest.mark.parametrize(
        ("construct", "statement"),
        [
            # The reported defect. Converted, emitting `posexplode(df["tags"])`.
            ("posexplode", 'df_transformed = df.withColumn("t", posexplode(col("tags")))'),
            # Imported by every glue_job_*.py.j2 scaffold.
            ("array_contains", 'df_transformed = df.withColumn("t", array_contains(col("tags"), "a"))'),
            # transform_generation.j2: "count is imported as spark_count".
            ("spark_count", 'df_transformed = df.withColumn("n", spark_count(col("name"))) '.strip()),
            # glue_job_jdbc_source.py.j2 imports `max as spark_max` for watermarks.
            ("spark_max", 'df_transformed = df.withColumn("m", spark_max(col("name")))'),
        ],
    )
    def test_function_position_refuses_naming_the_construct(self, construct, statement):
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(statement)
        assert raised.value.construct == construct
        assert raised.value.line_number == 1

    @pytest.mark.parametrize(
        "construct",
        ["count", "collect", "writeTo", "cache", "unpersist", "createOrReplaceTempView"],
    )
    def test_scaffold_frame_methods_refuse_in_method_position(self, construct):
        """The method-position siblings.

        `count` is the one that was not a `NameError`: it passed through to pandas'
        `DataFrame.count()`, which counts non-null values per column rather than
        rows — a silent wrong answer. The rest reached a pandas frame that has no
        such attribute.
        """
        with pytest.raises(ConversionError) as raised:
            PySpark2PandasConverter.convert(f"df_transformed = df.{construct}()")
        assert raised.value.construct == construct

    def test_the_shapes_that_convert_today_still_convert(self):
        """The default refusal must not have swallowed the supported vocabulary.

        Includes `LGR-5c33c04-1`'s nested string composition, which p32 3.4 forbids
        regressing — a blanket refusal that caught the inner call would have broken
        exactly that.
        """
        for statement in (
            'df_transformed = df.withColumn("s", lower(trim(col("name"))))',
            'df_transformed = df.withColumn("d", to_date(col("name"), "dd-MM-yyyy"))',
            'df_transformed = df.withColumn("j", concat_ws(" ", col("name"), col("city")))',
            'df_transformed = df.withColumn("r", regexp_replace(col("name"), "a", "b"))',
            'df_transformed = df.withColumn("l", lit("x"))',
            'df_transformed = df.withColumn("c", col("name").cast(StringType()))',
        ):
            PySpark2PandasConverter.convert(statement)
