# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the Glue job's ``--source_path`` has exactly ONE derivation.

Spec ``p55-format-support-and-teardown``, task 4.1 — the blocking gate that
lands before task 4.3 writes a converter, and the assertion risk **R5.2** names.

WHAT R5.2 ASKS FOR, AND HOW THIS FILE ASSERTS IT
------------------------------------------------
R5.2 reads: *"a contract test asserts the rendered job's ``source_path`` derives
from ``readable_source_uri`` and never from the original key for a
Conversion_Required_Set format."*

That sentence is asserted literally, in
:class:`TestConversionRequiredFormatsDoNotReachTheJobUnconverted`. It **xfailed
strictly** between tasks 4.2 and 4.3 — 4.2 made ``NATIVE_FORMATS`` mean
*readable directly from the source object*, so the Conversion_Required_Set
became ``{"excel"}`` while ``get_source_path`` still resolved
``original_s3_uri`` unconditionally and an ``.xlsx`` upload reached the Glue job
as an ``.xlsx``. **Task 4.3 has now landed and the marker is deleted**, which is
what ``strict=True`` was for: the XPASS was the signal. The assertion itself was
not touched (``LGR-60c4985-6``), so it is now a live guard against the one
derivation being pointed back at the original key.

The ordering mattered and is worth recording: had this been written before 4.2,
the same sentence would have asserted that a **CSV** needs converting, since the
pre-p55 complement of ``{"parquet", "avro", "orc"}`` is ``{"csv", "json"}``. That
is the exact hazard R13 exists to remove, and design §4.3 (W3-D2)'s ordering —
*the set's meaning is fixed first, then a consumer is added* — is what makes the
assertion writable at all.

:class:`TestSourcePathHasOneDerivation` carries the other half, and it passes
today:

    Exactly one expression in ``create_glue_pipeline`` decides what object the
    generated Glue job reads, and the four S3-source templates read no other.

That is what makes R5.2's fix a **one-site** change. The thing that would make
R5.2 fire even after 4.3 is a *second* derivation — a consumer that keeps
reading the original URI after the first one is repointed. This file is what
fails when one appears.

HOW TASK 4.3 DISCHARGED THE OBLIGATION THIS FILE RECORDED
---------------------------------------------------------
:data:`SOLE_DERIVATION_FUNCTION` is the one site on the Glue-read path, and 4.3
changed what it reads — from ``original_s3_uri`` to the resolved
``readable_source_uri`` — and nothing else on that path, because there is
nothing else on it. It also dropped that function's dead ``source_format``
parameter and the docstring that described behaviour the body did not have
(task 4.1, finding 1).

The two facts task 4.1's enumeration established, and what 4.3 did with them:

* ``json_s3_uri`` **must not** be repointed at the converted object.
  ``LGR-5c539a6-2`` rejected exactly that, by name: *"three readers take it to
  mean the canonical source URI"*. So ``create_code``'s ``source_object_uri``
  could not ride on a repoint, and 4.3 switched it to ``readable_source_uri``
  explicitly (falling back to ``json_s3_uri`` only for a pre-p55 row, where the
  two are necessarily the same object). ``json_s3_uri`` is untouched.
* ``source_format`` must move with the URI. The templates branch
  ``{% if source_format == 'csv' %} … {% else %} spark.read…json(source_path)``,
  and ``code_assembly.py`` defaults ``source_format`` to ``"json"``. An Excel run
  whose URI is repointed but whose ``source_format`` still says ``"excel"``
  renders the ``else`` and reads a converted **CSV** as JSON. 4.3 therefore
  resolves **two** outputs, not one: ``readable_source_uri`` and
  ``readable_source_format``, written together on the ``schema_extract`` row and
  returned together on both arms. ``create_code`` prefers the row's format over
  the event's detected one. This file asserts the URI half; the format half is
  behavioural and is covered under ``tests/unit/lambdas/schema_extract/``.

THE ORACLE IS THE SOURCE, NEVER A CAPTURED VALUE
------------------------------------------------
``LGR-28b4d75-5`` (**template / ratified**): a contract test derives its
expectation from the producer's source, because "a test written from the
producer's output cannot detect the producer drifting". So the Lambda modules
are parsed with :mod:`ast` and the templates are read as text; nothing here
compares against a recorded dict.

``LGR-28b4d75-1`` (**template / ratified**) is the rule this file enforces: the
conversion gate is read in exactly one place and consumers take the
already-resolved value. Design §4.3 restates it — *"``create_glue_pipeline`` and
the templates consume ``readable_source_uri``, which is already resolved, so
they never re-derive the gate. One reader."*

``glue_job_jdbc_source.py.j2`` IS EXCLUDED, DELIBERATELY
-------------------------------------------------------
It declares no ``source_path`` and its frame comes from
``spark.read.format("jdbc")``. ``job_builders`` pops ``--source_path`` outright
for a JDBC source (``job_builders.py:603``). A JDBC pipeline has no source
object, so it has no source path to converge — ``LGR-819980f-1`` (**standing
invariant**) is why that must stay unreachable rather than merely unused.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

CREATE_GLUE_PIPELINE_DIR = _REPO_ROOT / "lambda" / "agentic_data_ingestion_create_glue_pipeline"
TEMPLATES_DIR = _REPO_ROOT / "pyspark_templates"

#: The four templates whose source read is an S3 object read. Named rather than
#: globbed, for the reason ``test_sample_readers_match_templates.py`` names its
#: identical list: a glob that matched nothing would make this file vacuous, and
#: a glob that picked up a sixth template would silently widen the oracle.
S3_SOURCE_TEMPLATE_NAMES = (
    "glue_job_glue_catalog.py.j2",
    "glue_job_jdbc.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_s3_tables.py.j2",
)

#: The fifth template, excluded — see the module docstring.
JDBC_SOURCE_TEMPLATE_NAME = "glue_job_jdbc_source.py.j2"

#: The Glue ``DefaultArguments`` key that carries the source object's URI, and
#: the ``getResolvedOptions`` name the templates declare for it.
SOURCE_PATH_ARGUMENT = "--source_path"
SOURCE_PATH_PARAMETER = "source_path"

#: The single function that decides which object the generated job reads.
#: ``lambda_handler`` calls it once (``lambda_function.py:397``) and passes the
#: result into ``deploy_glue_pipeline_with_mcp`` (``:528``) and
#: ``create_glue_job`` (``:571``); every ``--source_path`` assignment downstream
#: is a pass-through of that one value.
SOLE_DERIVATION_FUNCTION = "get_source_path"
SOLE_DERIVATION_MODULE = "artifacts.py"

#: The state-row attribute :data:`SOLE_DERIVATION_FUNCTION` resolves the URI
#: from, before p55 task 4.3.
PRE_CONVERSION_SOURCE_KEY = "original_s3_uri"

#: The attribute it resolves the URI from AFTER task 4.3 — the new envelope key
#: ``schema_extract`` returns on both arms (``LGR-5c539a6-2``), defaulting to the
#: original source URI for every Native_Format_Set member (R13.4).
READABLE_SOURCE_KEY = "readable_source_uri"

#: The pair. :meth:`TestSourcePathHasOneDerivation.
#: test_the_sole_derivation_function_reads_a_designated_readable_key` asserts the
#: one derivation reads one of exactly these two and nothing else, so the
#: function cannot quietly start resolving a third attribute — while 4.3's
#: transition from the first to the second needs no edit here.
DESIGNATED_SOURCE_KEYS = (PRE_CONVERSION_SOURCE_KEY, READABLE_SOURCE_KEY)

#: The documented fallback inside the same function, for a row that carries the
#: URI nested under ``output_data.s3_uris``. Part of the one derivation, not a
#: second one.
READABLE_SOURCE_KEY_FALLBACK = "source_file"

#: Attributes that name a source object's URI somewhere in this system. Any read
#: of one of these inside ``create_glue_pipeline`` is a candidate second
#: derivation, and the guard below allows them ONLY inside
#: :data:`SOLE_DERIVATION_FUNCTION`.
SOURCE_URI_ATTRIBUTES = frozenset(
    {
        "original_s3_uri",
        "json_s3_uri",
        "sample_s3_uri",
        "source_sample_uri",
        "readable_source_uri",
        "source_s3_uri",
    }
)

#: The dormant legacy resolver found by task 4.1's enumeration:
#: ``parameter_builder.py:106-108`` maps ``SOURCE_S3_PATH`` to a **constructed**
#: glob, ``s3://{processed_bucket}/{pipeline_id}/validated/*.json`` — a second
#: source-path derivation that reads no source URI at all and hardcodes
#: ``.json``. It is unreachable today because no template declares that name, and
#: this file asserts that it stays unreachable. If a template ever declares
#: ``SOURCE_S3_PATH``, an Excel pipeline would be handed a ``*.json`` glob at the
#: processed bucket regardless of anything 4.3 does.
DORMANT_LEGACY_PARAMETER = "SOURCE_S3_PATH"

_GET_RESOLVED_OPTIONS = re.compile(
    r"getResolvedOptions\s*\(\s*sys\.argv\s*,\s*\[(.*?)\]\s*\)",
    re.DOTALL,
)
_QUOTED = re.compile(r"'([^']+)'")
_READER_ASSIGNMENT = re.compile(r"^\s*df\s*=\s*(\S.*?)\s*$")


def _template_source(name: str) -> str:
    path = TEMPLATES_DIR / name
    assert path.is_file(), (
        f"template {name} not found at {path} — has it been renamed or removed? "
        "S3_SOURCE_TEMPLATE_NAMES in this file is the list under contract."
    )
    return path.read_text(encoding="utf-8")


def _declared_parameters(name: str) -> list[str]:
    source = _template_source(name)
    match = _GET_RESOLVED_OPTIONS.search(source)
    assert match, f"no getResolvedOptions(...) call found in {name}"
    return _QUOTED.findall(match.group(1))


def _reader_expressions(name: str) -> list[str]:
    """Every ``df = …`` right-hand side in *name*'s source-read block.

    Line-oriented rather than ``ast.parse``d, for the reason
    ``test_sample_readers_match_templates.py`` gives for the same technique:
    these files are Jinja, not Python, so they do not parse.
    """
    return [
        m.group(1)
        for m in (_READER_ASSIGNMENT.match(line) for line in _template_source(name).splitlines())
        if m is not None and "spark.read" in m.group(1)
    ]


def _lambda_modules() -> dict[str, ast.Module]:
    modules: dict[str, ast.Module] = {}
    for path in sorted(CREATE_GLUE_PIPELINE_DIR.glob("*.py")):
        modules[path.name] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return modules


def _enclosing_function_names(tree: ast.Module) -> dict[ast.AST, str]:
    """Map every node in *tree* to the name of the function that encloses it."""
    owner: dict[ast.AST, str] = {}

    def walk(node: ast.AST, current: str) -> None:
        for child in ast.iter_child_nodes(node):
            name = current
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                name = child.name
            owner[child] = name
            walk(child, name)

    walk(tree, "<module>")
    return owner


def _string_constants(node: ast.AST) -> set[str]:
    return {n.value for n in ast.walk(node) if isinstance(n, ast.Constant) and isinstance(n.value, str)}


# ---------------------------------------------------------------------------
# The parse is non-vacuous
# ---------------------------------------------------------------------------


class TestTheParseIsNonVacuous:
    """A guard that found nothing to check would pass, so check it found things."""

    def test_the_four_s3_source_templates_all_declare_the_source_path_parameter(self) -> None:
        for name in S3_SOURCE_TEMPLATE_NAMES:
            declared = _declared_parameters(name)
            assert SOURCE_PATH_PARAMETER in declared, (
                f"{name} does not declare {SOURCE_PATH_PARAMETER!r} in getResolvedOptions — "
                f"it declares {declared}. If a template stopped taking the source path, this "
                f"file's whole subject moved and S3_SOURCE_TEMPLATE_NAMES needs revisiting."
            )

    def test_the_jdbc_source_template_declares_no_source_path(self) -> None:
        declared = _declared_parameters(JDBC_SOURCE_TEMPLATE_NAME)
        assert SOURCE_PATH_PARAMETER not in declared, (
            f"{JDBC_SOURCE_TEMPLATE_NAME} now declares {SOURCE_PATH_PARAMETER!r}. It reads from a "
            f"JDBC connection and job_builders pops --source_path for a JDBC source "
            f"(job_builders.py:603), so a source path here means the exclusion in this file's "
            f"docstring is no longer true — and LGR-819980f-1 makes that a credentials question, "
            f"not a formatting one."
        )

    def test_the_sole_derivation_function_exists_where_this_file_says_it_does(self) -> None:
        tree = _lambda_modules()[SOLE_DERIVATION_MODULE]
        names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
        assert SOLE_DERIVATION_FUNCTION in names, (
            f"{SOLE_DERIVATION_FUNCTION} is not defined in {SOLE_DERIVATION_MODULE}. "
            f"It is the one site R5.2's fix lands on; if it moved, every constant in this "
            f"file needs re-pointing rather than the assertions relaxing."
        )

    def test_every_module_under_contract_parsed(self) -> None:
        modules = _lambda_modules()
        assert len(modules) >= 5, (
            f"only {len(modules)} module(s) parsed under {CREATE_GLUE_PIPELINE_DIR} — "
            f"expected the Lambda's several modules. A near-empty parse makes the "
            f"second-derivation guard below vacuous."
        )
        assert SOLE_DERIVATION_MODULE in modules
        assert "job_arguments.py" in modules
        assert "job_builders.py" in modules


# ---------------------------------------------------------------------------
# R5.2: one derivation, and the templates read nothing else
# ---------------------------------------------------------------------------


class TestSourcePathHasOneDerivation:
    def test_only_the_sole_derivation_function_reads_a_source_object_uri(self) -> None:
        """No second place in ``create_glue_pipeline`` reads a source URI attribute.

        This is R5.2's structural precondition. ``LGR-28b4d75-1``'s derive-once
        rule says the conversion gate is read in one place and consumers take
        the resolved value; the corollary is that ``create_glue_pipeline`` reads
        the source object's URI in one place too. A second reader is precisely
        the consumer R5.2 worries about — the one that keeps pointing at the
        original workbook after 4.3 repoints the first.
        """
        offenders: list[str] = []
        for module_name, tree in _lambda_modules().items():
            owners = _enclosing_function_names(tree)
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Constant) and isinstance(node.value, str)):
                    continue
                if node.value not in SOURCE_URI_ATTRIBUTES:
                    continue
                if owners.get(node, "<module>") == SOLE_DERIVATION_FUNCTION:
                    continue
                offenders.append(
                    f"{module_name}:{node.lineno} reads {node.value!r} inside {owners.get(node, '<module>')}()"
                )
        assert not offenders, (
            "a SECOND derivation of the source object's URI exists in "
            "create_glue_pipeline:\n  " + "\n  ".join(offenders) + "\n\n"
            f"Only {SOLE_DERIVATION_FUNCTION}() in {SOLE_DERIVATION_MODULE} may resolve it. "
            "This is risk R5.2: a consumer that keeps reading the ORIGINAL URI hands an Excel "
            "workbook to Spark. If the new read is legitimate, it belongs inside "
            f"{SOLE_DERIVATION_FUNCTION}() — not beside it (LGR-28b4d75-1, derive once)."
        )

    def test_the_sole_derivation_function_reads_a_designated_readable_key(self) -> None:
        """The one derivation resolves one of :data:`DESIGNATED_SOURCE_KEYS`.

        Before task 4.3 that is ``original_s3_uri``; after it,
        ``readable_source_uri``. Both are admitted so 4.3's transition needs no
        edit here, but a **third** attribute is refused — the function's job is
        to resolve the designated key, not to acquire opinions.
        """
        tree = _lambda_modules()[SOLE_DERIVATION_MODULE]
        function = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == SOLE_DERIVATION_FUNCTION
        )
        literals = _string_constants(function)
        designated = [k for k in DESIGNATED_SOURCE_KEYS if k in literals]
        assert designated, (
            f"{SOLE_DERIVATION_FUNCTION}() reads none of {list(DESIGNATED_SOURCE_KEYS)}. It is the "
            f"one site that decides which object the generated job reads; if the designated key "
            f"changed, add it to DESIGNATED_SOURCE_KEYS with the reason stated here — do not "
            f"delete this assertion (LGR-60c4985-6)."
        )
        stray = (literals & SOURCE_URI_ATTRIBUTES) - set(DESIGNATED_SOURCE_KEYS)
        assert not stray, (
            f"{SOLE_DERIVATION_FUNCTION}() also reads {sorted(stray)}, which is not a designated "
            f"source key. A second attribute inside the one derivation is R5.2 in miniature: it "
            f"reintroduces a preference where LGR-28b4d75-1 requires an already-resolved value."
        )
        assert READABLE_SOURCE_KEY_FALLBACK in literals, (
            f"{SOLE_DERIVATION_FUNCTION}()'s documented nested fallback "
            f"({READABLE_SOURCE_KEY_FALLBACK!r} under output_data.s3_uris) is gone. It is part of "
            f"the ONE derivation; removing it silently narrows which rows resolve a source path."
        )

    def test_every_source_path_argument_is_a_passthrough_never_a_derivation(self) -> None:
        """``"--source_path": X`` always has ``X`` a bare name, never a call or literal.

        A literal or a call on the right-hand side would be a second derivation
        wearing an assignment's clothes — the shape that makes 4.3 an unbounded
        edit rather than a one-line one.
        """
        offenders: list[str] = []
        for module_name, tree in _lambda_modules().items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Dict):
                    continue
                for key, value in zip(node.keys, node.values, strict=True):
                    if not (isinstance(key, ast.Constant) and key.value == SOURCE_PATH_ARGUMENT):
                        continue
                    if isinstance(value, ast.Name):
                        continue
                    offenders.append(
                        f"{module_name}:{key.lineno} assigns {SOURCE_PATH_ARGUMENT} from a "
                        f"{type(value).__name__}, not a plain name"
                    )
        assert not offenders, (
            f"{SOURCE_PATH_ARGUMENT} is derived rather than passed through:\n  "
            + "\n  ".join(offenders)
            + f"\n\nEvery site must pass the one value {SOLE_DERIVATION_FUNCTION}() resolved. "
            "R5.2's fix is a one-line change only while that holds."
        )

    def test_the_templates_read_their_frame_only_from_the_declared_source_path(self) -> None:
        """No template re-derives a source URI or hardcodes an ``s3://`` literal.

        The templates are the last consumer on the path, so if one of them built
        its own URI, repointing ``--source_path`` would not reach it.
        """
        for name in S3_SOURCE_TEMPLATE_NAMES:
            readers = _reader_expressions(name)
            assert readers, f"no spark.read expression found in {name}"
            for expression in readers:
                assert SOURCE_PATH_PARAMETER in expression, (
                    f"{name} reads a frame from an expression that does not mention "
                    f"{SOURCE_PATH_PARAMETER!r}: {expression!r}. A template that builds its own "
                    f"URI is invisible to R5.2's fix — repointing --source_path would not reach "
                    f"it, and an Excel pipeline would still read the workbook."
                )
                assert "s3://" not in expression, f"{name} hardcodes an s3:// literal in a reader: {expression!r}."

    def test_the_dormant_legacy_source_parameter_stays_unreachable(self) -> None:
        """No template declares :data:`DORMANT_LEGACY_PARAMETER`.

        Found by task 4.1's enumeration and recorded because it is a genuine
        second derivation, merely unreachable: ``parameter_builder.py:106-108``
        resolves ``SOURCE_S3_PATH`` to
        ``s3://{processed_bucket}/{pipeline_id}/validated/*.json``, which reads
        no source URI and hardcodes ``.json``. It is emitted only if a generated
        job's ``required_params`` names it — which the four templates do not. The
        moment one does, that glob wins over anything 4.3 resolves.
        """
        for name in (*S3_SOURCE_TEMPLATE_NAMES, JDBC_SOURCE_TEMPLATE_NAME):
            declared = _declared_parameters(name)
            assert DORMANT_LEGACY_PARAMETER not in declared, (
                f"{name} declares {DORMANT_LEGACY_PARAMETER!r}. parameter_builder.py resolves it "
                f"to a constructed s3://.../validated/*.json glob that reads no source URI at "
                f"all — so declaring it hands the job a JSON glob regardless of what "
                f"{SOLE_DERIVATION_FUNCTION}() resolved. Either delete the legacy resolver or "
                f"make it read the resolved source path."
            )


# ---------------------------------------------------------------------------
# R5.2, per-format: a Conversion_Required_Set format must not reach the job raw
# ---------------------------------------------------------------------------


def _format_vocabulary() -> tuple[set[str], set[str]]:
    """``(FORMAT_MAP values, NATIVE_FORMATS)``, read as literals from source.

    Both are hand-written literals in ``file_validation/lambda_function.py``,
    which is what makes them independently readable — design §4.3 (W3-D3) and
    do-NOT 8 require ``NATIVE_FORMATS`` to STAY a literal for exactly this
    reason, and the ``ast.Set`` assertion below is that requirement's guard.
    """
    path = _REPO_ROOT / "lambda" / "agentic_data_ingestion_file_validation" / "lambda_function.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    values: set[str] | None = None
    native: set[str] | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if "FORMAT_MAP" in targets:
            assert isinstance(node.value, ast.Dict), "FORMAT_MAP is no longer a dict literal"
            values = {v.value for v in node.value.values if isinstance(v, ast.Constant) and isinstance(v.value, str)}
        if "NATIVE_FORMATS" in targets:
            assert isinstance(node.value, ast.Set), (
                "NATIVE_FORMATS is no longer a hand-written set literal. do-NOT 8 and design "
                "§4.3 (W3-D3) require it to stay one — a derived set makes F5 guard 3's subset "
                "assertion a tautology and breaks its isinstance(node, ast.Set) check outright."
            )
            native = _string_constants(node.value)
    assert values, "FORMAT_MAP not found or empty"
    assert native, "NATIVE_FORMATS not found or empty"
    return values, native


class TestConversionRequiredFormatsDoNotReachTheJobUnconverted:
    def test_the_conversion_required_set_is_non_empty(self) -> None:
        """R13.7's anti-vacuity floor, and this class's own.

        An empty complement would make the assertion below pass over nothing —
        the failure mode ``test_format_vocabulary_cross_lambda.py``'s four
        existing guards were written to prevent.
        """
        values, native = _format_vocabulary()
        conversion_required = values - native
        assert conversion_required, (
            f"the Conversion_Required_Set is empty: FORMAT_MAP values {sorted(values)} minus "
            f"NATIVE_FORMATS {sorted(native)}. R13.7 requires at least one member, because an "
            f"empty set makes every conversion guard vacuous."
        )
        assert native <= values, (
            f"NATIVE_FORMATS {sorted(native)} is not a subset of FORMAT_MAP's values {sorted(values)} (R13.5)."
        )

    # The ``xfail(strict=True)`` marker that stood here was DELETED by p55 task
    # 4.3, which is the action it existed to trigger. It read: "p55 task 4.3 has
    # not landed. This IS the assertion risk R5.2 names, and it fails today
    # because the defect is real: get_source_path resolves original_s3_uri
    # unconditionally, so an .xlsx upload reaches the Glue job as an .xlsx and
    # Spark reads a workbook. When 4.3 adds readable_source_uri to
    # schema_extract's result on both arms and points get_source_path at it, this
    # passes — and strict=True then turns the XPASS into a failure, which is the
    # signal to DELETE this marker rather than to rediscover why it was here."
    #
    # 4.3 landed: ``schema_extract`` returns ``readable_source_uri`` on both arms
    # and stores it on its own row, and ``get_source_path`` resolves that key and
    # no longer reads ``original_s3_uri`` at all. **The assertion below is
    # unchanged** — only the marker is gone (``LGR-60c4985-6`` /
    # ``LGR-9e76249-10``: an assertion is amended in place with its reason, never
    # deleted). It is now a live guard rather than a recorded defect: it fails if
    # a later change points the one derivation back at the original key.
    def test_the_source_path_derives_from_the_readable_uri_not_the_original_key(self) -> None:
        """R5.2, stated as the obligation on task 4.3.

        A Conversion_Required_Set format exists (``{"excel"}`` after task 4.2),
        and the one derivation of ``--source_path`` must resolve
        ``readable_source_uri`` — the value ``schema_extract`` returns on both
        arms, already resolved — rather than the original upload's key. Design
        §4.2 (W3-D1) and ``LGR-28b4d75-1`` between them make this the ONLY read
        that has to change; ``TestSourcePathHasOneDerivation`` above is what
        keeps that true.

        Note the sibling obligation this cannot see, recorded in the module
        docstring: ``source_format`` must move with the URI, or the templates'
        ``{% else %}`` reads a converted CSV as JSON.
        """
        tree = _lambda_modules()[SOLE_DERIVATION_MODULE]
        function = next(
            n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == SOLE_DERIVATION_FUNCTION
        )
        literals = _string_constants(function)
        assert READABLE_SOURCE_KEY in literals, (
            f"{SOLE_DERIVATION_FUNCTION}() does not read {READABLE_SOURCE_KEY!r}, so the Glue "
            f"job's --source_path still points at the ORIGINAL object. With a non-empty "
            f"Conversion_Required_Set that means a workbook handed to Spark (risk R5.2)."
        )
        assert PRE_CONVERSION_SOURCE_KEY not in literals, (
            f"{SOLE_DERIVATION_FUNCTION}() reads BOTH {READABLE_SOURCE_KEY!r} and "
            f"{PRE_CONVERSION_SOURCE_KEY!r}. A preference between them here is the thing "
            f"LGR-28b4d75-1 forbids — schema_extract resolves it once and this consumer takes "
            f"the resolved value. The default-to-original behaviour belongs in schema_extract "
            f"(R13.4), not in a fallback here."
        )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
