# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the verification session binds every name the templates do.

Spec ``real-spark-verification``, defect 3 — the fence that stops it recurring.

THE DEFECT THIS FENCES
----------------------
Run ``run-2026-08-18-18-52-03-a8de6fca`` (account 777788889999, ``us-west-2``,
``VERIFICATION_EXECUTOR=glue``) got past the previous fix's ``IndentationError``:
the transform statement parsed and Spark evaluated it. It then failed with

    Glue statement failed at stage 'transform execution'
    Glue transform execution failed: NameError: name 'col' is not defined

The transform block references ``col`` as a bare name because inside the
generated Glue job it *is* one — every ``pyspark_templates/*.j2`` binds it at
module scope. The Glue runner submits only the transform block, so the ambient
namespace had to be reproduced in the session;
``glue_session_runner.build_transform_namespace_code`` does that, folded into the
frame-build statement.

WHY THIS TEST AND NOT A CODE CONSTANT
-------------------------------------
The reproduction is a **hardcoded** preamble, and a hardcoded preamble drifts the
moment a template adds a function to its import line. The failure mode of that
drift is not subtle and not new — it is exactly the live defect above, in
production, on whichever construct the model reaches for next.

So the oracle here is the **templates' own source**, parsed at test time.
``LGR-28b4d75-5`` (**template / ratified**) is the governing record: a contract
test derives its expectation from the producer's source because "a test written
from the producer's output cannot detect the producer drifting, the output is its
own oracle". Same technique as this directory's
``test_not_verified_reason_labels_cross_language.py`` and
``test_shadow_verification_step_name_cross_lambda.py``.

Direction matters: this asserts **templates ⊆ preamble**, not equality. The
preamble is allowed to bind more than any one template does, because it emits the
union of the two template variants — four templates carry the base function list
and ``glue_job_jdbc_source.py.j2`` adds ``max as spark_max``, and the runner is
not told which template the job will be rendered from. That asymmetry is recorded
in the runner's own section comment with its revisit condition.

SCOPE: MODULE-SCOPE ``pyspark.sql.functions`` AND ``pyspark.sql.types`` ONLY
---------------------------------------------------------------------------
Two narrowings, both deliberate and both asserted below rather than described:

* **Module scope only.** ``glue_job_s3_parquet.py.j2:81`` and its three siblings
  carry ``from pyspark.sql.utils import AnalysisException as _AnalysisException``
  at an indent, inside a function — a name the transform block cannot see in the
  real job either. Column 0 is the discriminator, and
  ``test_a_function_scope_import_is_not_collected`` pins that it works.
* **Those two modules only.** The templates' other module-scope imports are job
  scaffolding rather than transform vocabulary: ``SparkContext``, ``GlueContext``
  and ``Job`` construct the job, ``DynamicFrame`` is referenced only in the write
  partials *after* the transform block
  (``_partials/_write_glue_catalog.py.j2:3``), and
  ``sys``/``logging``/``json``/``boto3``/``datetime``/``jdbc_helpers`` are not
  expression vocabulary. ``from awsglue.transforms import *`` settles it: a star
  import means "every module-scope name" is not statically enumerable, so a test
  claiming that would be false by construction.

HOW THE PARSE IS KEPT NON-VACUOUS
---------------------------------
A source-parsing test's whole risk is passing on nothing found — a regex that
stops matching yields an empty oracle and an empty oracle is a subset of
everything. Four guards, each failing with a message naming what it looked for:

1. all five templates exist and are readable
2. every template contributes at least one ``functions`` and one ``types`` import
3. the collected union contains known members (``col``, ``initcap``,
   ``DateType``, and the aliased ``spark_count``)
4. the two variants are still two — ``spark_max`` in exactly one template

The import lines are extracted as text and then handed to ``ast.parse``, so the
``as`` clauses are resolved by Python's own parser rather than by a regex.
``ast.parse`` cannot be run on a whole template: they are Jinja, so ``{% if %}``
is a syntax error.
"""

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

TEMPLATES_DIR = _REPO_ROOT / "pyspark_templates"

#: The five job templates, named rather than globbed. A glob that matched nothing
#: would make this whole file vacuous, and a glob that picked up a sixth template
#: would silently widen the oracle; naming them means a new template fails here
#: and is added deliberately.
TEMPLATE_NAMES = (
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_jdbc_source.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_s3_tables.py.j2",
)

#: The two modules whose module-scope names are the transform block's expression
#: vocabulary. See the module docstring for why the scaffolding imports are out.
VOCABULARY_MODULES = ("pyspark.sql.functions", "pyspark.sql.types")


def _module_scope_import_lines(source: str, module: str) -> list[str]:
    """Every column-0 ``from {module} import …`` statement in *source*, as text.

    Column 0 is the module-scope test and it is the point: an indented import of
    the same module binds inside a function, where the transform block cannot see
    it in the real job either.

    Consumes a parenthesised continuation, so a template that later wraps its
    import list across lines is still read in full rather than truncated at the
    open paren. No template wraps these two today; ``jdbc_helpers`` at
    ``glue_job_jdbc_source.py.j2:63`` shows the wrapped form is house style, so
    this is a shape to expect rather than a hypothetical.
    """
    prefix = f"from {module} import "
    lines = source.splitlines()
    collected: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith(prefix):
            index += 1
            continue
        statement = line
        # Balance parentheses across continuation lines. `count` on the running
        # text rather than on each line, so a nested paren cannot end it early.
        while statement.count("(") > statement.count(")"):
            index += 1
            if index >= len(lines):
                break
            statement += "\n" + lines[index]
        collected.append(statement)
        index += 1
    return collected


def _bound_names(import_statement: str) -> set[str]:
    """The names *import_statement* binds, per Python's own parser.

    ``asname or name``, which is what makes ``count as spark_count`` bind
    ``spark_count`` and **not** ``count`` — the distinction the runner's preamble
    has to reproduce exactly, since bare ``count`` is not bound in the generated
    job either.
    """
    parsed = ast.parse(import_statement)
    names: set[str] = set()
    for node in ast.walk(parsed):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _template_source(name: str) -> str:
    path = TEMPLATES_DIR / name
    assert path.is_file(), (
        f"template {name} not found at {path} — has it been renamed or removed? "
        "TEMPLATE_NAMES in this file is the list under contract."
    )
    return path.read_text(encoding="utf-8")


def _names_for(name: str, module: str) -> set[str]:
    statements = _module_scope_import_lines(_template_source(name), module)
    names: set[str] = set()
    for statement in statements:
        names |= _bound_names(statement)
    return names


def _template_vocabulary() -> set[str]:
    """The union of every module-scope vocabulary name across all five templates."""
    names: set[str] = set()
    for template in TEMPLATE_NAMES:
        for module in VOCABULARY_MODULES:
            names |= _names_for(template, module)
    return names


def _preamble_bound_names() -> frozenset[str]:
    """What the runner's preamble binds, read from the runner itself.

    Imported rather than regex-parsed, because this is the side *under test*: a
    regex over the runner's source could mis-parse and pass on a preamble that
    does not actually bind what it claims. The oracle is the templates; the
    subject is the real function's real output. ``glue_session_runner`` imports
    with no environment and no mocking — verified — so nothing is constructed
    here.
    """
    import sys

    for path in (
        _REPO_ROOT / "lambda" / "agentic_data_ingestion_test_code",
        _REPO_ROOT / "shared",
    ):
        if str(path) not in sys.path:
            sys.path.insert(0, str(path))

    import glue_session_runner

    return glue_session_runner.transform_namespace_bound_names()


class TestTemplatesAreReadable:
    """Guard 1 — a moved or renamed template fails loudly, not vacuously."""

    @pytest.mark.parametrize("template", TEMPLATE_NAMES)
    def test_template_is_readable(self, template: str):
        assert (TEMPLATES_DIR / template).is_file(), f"{template} not found in {TEMPLATES_DIR}"


class TestTheParseIsNonVacuous:
    """Guards 2-4 — an oracle that found nothing is a subset of everything."""

    @pytest.mark.parametrize("template", TEMPLATE_NAMES)
    @pytest.mark.parametrize("module", VOCABULARY_MODULES)
    def test_every_template_contributes_names(self, template: str, module: str):
        """Each template imports from each vocabulary module at module scope.

        If a template stops doing so this fails here, rather than quietly
        shrinking the set the contract below is checked against.
        """
        names = _names_for(template, module)

        assert names, (
            f"No column-0 `from {module} import …` found in {template}. Either the template "
            f"stopped importing it at module scope — in which case the transform block no "
            f"longer sees those names in the real job and the runner's preamble should shrink "
            f"to match — or the extraction in this file needs to follow a reformatting."
        )

    def test_the_collected_vocabulary_contains_known_members(self):
        """Named members, including the one whose binding is an alias.

        ``spark_count`` is in this list on purpose: it is the case a regex that
        split on commas and ignored ``as`` would get wrong, and getting it wrong
        in the *permissive* direction (collecting ``count``) would make the
        contract demand a name the generated job does not bind.
        """
        vocabulary = _template_vocabulary()

        expected_functions = {"col", "lit", "initcap", "regexp_replace", "expr"}
        expected_types = {"StringType", "DateType", "TimestampType"}

        assert expected_functions <= vocabulary, (
            f"the parse did not find {sorted(expected_functions - vocabulary)} — it matched the "
            f"import lines but not their contents, so the oracle is incomplete"
        )
        assert expected_types <= vocabulary, (
            f"the parse did not find {sorted(expected_types - vocabulary)} from pyspark.sql.types"
        )
        assert "spark_count" in vocabulary, "the `count as spark_count` alias was not resolved"
        assert "count" not in vocabulary, (
            "bare `count` was collected, but the templates bind only the `spark_count` alias — "
            "the extraction is resolving `as` clauses in the wrong direction"
        )

    def test_the_two_template_variants_are_still_two(self):
        """``spark_max`` in exactly one template, which is what the union buys.

        The runner emits the union of both variants and records that as a
        permissive imprecision. If a second template gains ``max as spark_max``,
        or the only one loses it, that reasoning changes and should be re-read
        rather than silently outgrown.
        """
        carriers = [t for t in TEMPLATE_NAMES if "spark_max" in _names_for(t, "pyspark.sql.functions")]

        assert carriers == ["glue_job_jdbc_source.py.j2"], (
            f"expected `max as spark_max` in exactly glue_job_jdbc_source.py.j2, found it in {carriers}. "
            "The runner's union note and its revisit condition need re-reading."
        )

    def test_a_function_scope_import_is_not_collected(self):
        """The module-scope discriminator, pinned on a real indented import.

        ``glue_job_s3_parquet.py.j2:81`` carries
        ``from pyspark.sql.utils import AnalysisException as _AnalysisException``
        inside a function. The transform block cannot see that name in the real
        job, so the preamble must not be obliged to bind it.
        """
        source = _template_source("glue_job_s3_parquet.py.j2")

        assert "from pyspark.sql.utils import" in source, (
            "the indented import this test discriminates against is gone from the template; "
            "the module-scope rule still holds but this case no longer exercises it"
        )
        assert _module_scope_import_lines(source, "pyspark.sql.utils") == []


class TestThePreambleCoversTheTemplates:
    """The contract itself."""

    def test_every_template_bound_name_is_bound_by_the_preamble(self):
        """R1.4's environment half — reproducing the namespace, not the code.

        A name the templates bind at module scope and the preamble does not is a
        ``NameError`` waiting at ``STAGE_TRANSFORM`` the first time a model
        reaches for it. That is not a hypothetical: it is
        ``run-2026-08-18-18-52-03-a8de6fca``.
        """
        vocabulary = _template_vocabulary()
        preamble = _preamble_bound_names()

        missing = sorted(vocabulary - preamble)

        assert not missing, (
            f"pyspark_templates bind {missing} at module scope, but the Glue verification "
            f"session's preamble does not. A transform block referencing any of them fails "
            f"with `NameError` at the transform stage — the live defect this fences. Add them "
            f"to TRANSFORM_FUNCTION_IMPORTS / TRANSFORM_TYPE_IMPORTS in glue_session_runner.py."
        )

    def test_the_preamble_binds_nothing_the_templates_do_not(self):
        """The other direction, as a *warning* about false passes.

        Asserted rather than left implicit because the failure mode is the
        inverse and is worse to debug: a name bound only in the verification
        session verifies green and then fails in production. Equality is
        achievable today — the union of the two variants is exactly the set of
        names some template binds — so it is asserted, and if a deliberate
        widening ever becomes necessary this is the test that makes it a decision
        rather than an accident.
        """
        extra = sorted(_preamble_bound_names() - _template_vocabulary())

        assert not extra, (
            f"the verification session binds {extra}, which no pyspark_templates template binds "
            f"at module scope. A transform using one of them would verify green here and fail "
            f"with `NameError` in the generated Glue job."
        )
