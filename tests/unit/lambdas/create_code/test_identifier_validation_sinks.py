# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R12 — Identifier validation at create_code's injection sinks.

Two sinks live in this Lambda:

- **DQDL rule assembly** (``_generate_dqdl_rules``): mapped target field
  names are interpolated into DQDL rule strings that the template embeds in
  a triple-quoted block (``dq_ruleset = \"\"\"...\"\"\"``). A hostile CSV
  header must fail rule assembly loudly — naming the identifier — never be
  sanitized into code.
- **merge_key at template render** (``generate_code``): the configured
  merge_key is rendered into the job and read back at runtime into a
  ``spark.sql`` MERGE INTO f-string. A hostile ``target_config.merge_key``
  must fail code generation.

Hostile inputs cover quotes, semicolons, newlines, and SQL comment tokens
(R12.4).

**Validates: Requirements 12.1, 12.2, 12.4**
"""

import ast
import inspect
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Add Lambda dir and shared dir to path
_create_code_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
    )
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

from identifier_utils import IdentifierValidationError

# Stub heavy dependencies so the module imports cleanly in unit context.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "code_validator": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "parameter_extractor": MagicMock(),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


# Hostile identifiers per R12.4: quotes, semicolons, newlines, comment tokens.
HOSTILE_IDENTIFIERS = [
    pytest.param('col"; DROP TABLE x; --', id="double-quote-semicolon-comment"),
    pytest.param("col'; DROP TABLE x; --", id="single-quote-semicolon-comment"),
    pytest.param("col; DELETE FROM t", id="semicolon"),
    pytest.param("col\nDROP TABLE x", id="newline"),
    pytest.param("col--comment", id="line-comment-token"),
    pytest.param("col/*comment*/", id="block-comment-token"),
    pytest.param('a"""b', id="triple-quote-block-escape"),
]


def _mappings(*target_fields: str) -> list[dict]:
    return [
        {"source_field": f"src_{i}", "target_field": name, "confidence": 0.9} for i, name in enumerate(target_fields)
    ]


# ── Sink: DQDL rule assembly ─────────────────────────────────────────


class TestDqdlRuleAssemblyRejectsHostileFieldNames:
    """**Validates: Requirements 12.1, 12.2, 12.4**"""

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_field_name_fails_rule_assembly(self, hostile):
        with pytest.raises(IdentifierValidationError) as exc_info:
            _mod._generate_dqdl_rules(_mappings(hostile))

        # R12.2: the failure names the offending identifier and the sink.
        assert exc_info.value.name == hostile
        assert "DQDL rule field" in str(exc_info.value)

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_field_fails_even_when_other_fields_are_valid(self, hostile):
        """No partial ruleset: assembly fails outright (no sanitize-and-continue)."""
        with pytest.raises(IdentifierValidationError):
            _mod._generate_dqdl_rules(_mappings("customer_id", hostile))

    def test_valid_field_names_still_produce_rules(self):
        dqdl = _mod._generate_dqdl_rules(_mappings("customer_id", "email_address"))
        assert 'Completeness "customer_id" >= 0.95' in dqdl
        assert 'Completeness "email_address" >= 0.95' in dqdl

    def test_overlong_field_name_fails(self):
        with pytest.raises(IdentifierValidationError):
            _mod._generate_dqdl_rules(_mappings("f" * 256))


# ── Sink: merge_key at template render (generate_code) ──────────────


def _s3_tables_pipeline_metadata(merge_key: str) -> dict:
    return {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": {
            "type": "s3_tables",
            "bucket_name": "my-table-bucket",
            "namespace": "analytics",
            "table_name": "customers",
            "write_mode": "merge",
            "merge_key": merge_key,
            "source_format": "json",
        },
        "source_schema": {"properties": {}},
    }


_MAPPING_RESULT = {
    "field_mappings": [
        {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.9},
    ]
}


class TestGenerateCodeRejectsHostileMergeKey:
    """**Validates: Requirements 12.1, 12.2, 12.4**"""

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_merge_key_fails_code_generation(self, hostile):
        with (
            patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
            pytest.raises(IdentifierValidationError) as exc_info,
        ):
            _mod.generate_code(
                pipeline_metadata=_s3_tables_pipeline_metadata(hostile),
                mapping_result=_MAPPING_RESULT,
            )

        assert exc_info.value.name == hostile
        assert "merge_key" in str(exc_info.value)

    def test_valid_merge_key_renders(self):
        with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
            code = _mod.generate_code(
                pipeline_metadata=_s3_tables_pipeline_metadata("customer_id"),
                mapping_result=_MAPPING_RESULT,
            )
        assert "MERGE INTO" in code

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_mapped_field_fails_code_generation(self, hostile):
        """The DQDL sink is enforced on the full generate_code path too."""
        mapping_result = {
            "field_mappings": [
                {"source_field": "src", "target_field": hostile, "confidence": 0.9},
            ]
        }
        with (
            patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
            pytest.raises(IdentifierValidationError) as exc_info,
        ):
            _mod.generate_code(
                pipeline_metadata=_s3_tables_pipeline_metadata("customer_id"),
                mapping_result=mapping_result,
            )
        assert exc_info.value.name == hostile


# ── The ordering constraint: validation runs BEFORE the p37 skip ─────


# Every unusable `source_field` shape C7 skips on. Kept in this file rather
# than imported so the two test modules stay independently runnable.
UNSOURCED_SHAPES = [
    pytest.param({}, id="source_field-absent"),
    pytest.param({"source_field": None}, id="source_field-None"),
    pytest.param({"source_field": ""}, id="source_field-empty-string"),
    pytest.param({"source_field": "   "}, id="source_field-whitespace-only"),
    pytest.param({"source_field": 123}, id="source_field-non-string-truthy"),
]


class TestUnsourcedTargetFieldIsStillValidated:
    """p37 R2.7 — C7's skip sits AFTER the injection sink, deliberately.

    ``_generate_dqdl_rules`` now emits no Completeness/Uniqueness rule for an
    Unsourced_Target_Field (p37 R2.1/R2.2). The skip is placed **after**
    ``validate_identifier(target_field, "DQDL rule field")``, not before, so a
    hostile field name still fails code generation naming the identifier even
    when nothing would have been emitted for it.

    These assertions exist to catch a future "skip first, validate later"
    refactor. Such a refactor looks harmless — the field produces no rule
    either way — but it silently retires P28 R12.1's sink for exactly the
    entries an attacker controls most cheaply: a target field nothing sources.

    **Validates: Requirements 12.1, 12.2, 12.4 (P28); 2.7 (p37)**
    """

    @pytest.mark.parametrize("unsourced", UNSOURCED_SHAPES)
    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_unsourced_field_still_fails_rule_assembly(self, hostile, unsourced):
        with pytest.raises(IdentifierValidationError) as exc_info:
            _mod._generate_dqdl_rules([{"target_field": hostile, "confidence": 0.9, **unsourced}])

        # R12.2: the failure still names the offending identifier and the sink.
        assert exc_info.value.name == hostile
        assert "DQDL rule field" in str(exc_info.value)

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_unsourced_field_fails_even_after_a_valid_sourced_field(self, hostile):
        """Still no partial ruleset: the unsourced entry is validated in place."""
        with pytest.raises(IdentifierValidationError) as exc_info:
            _mod._generate_dqdl_rules(
                [
                    {"source_field": "src", "target_field": "customer_id", "confidence": 0.9},
                    {"target_field": hostile, "confidence": 0.9},
                ]
            )
        assert exc_info.value.name == hostile

    def test_overlong_unsourced_field_name_still_fails(self):
        with pytest.raises(IdentifierValidationError):
            _mod._generate_dqdl_rules([{"target_field": "f" * 256, "source_field": None}])

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_unsourced_field_fails_the_full_generate_code_path(self, hostile):
        """The sink is enforced through generate_code for unsourced fields too."""
        mapping_result = {"field_mappings": [{"target_field": hostile, "source_field": None, "confidence": 0.9}]}
        with (
            patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"),
            pytest.raises(IdentifierValidationError) as exc_info,
        ):
            _mod.generate_code(
                pipeline_metadata=_s3_tables_pipeline_metadata("customer_id"),
                mapping_result=mapping_result,
            )
        assert exc_info.value.name == hostile

    def test_valid_unsourced_field_is_skipped_rather_than_rejected(self):
        """The negative control: C7 skips, it does not raise.

        Without this, the class above would pass for a wrong reason — an
        implementation that rejected every unsourced field outright.
        """
        dqdl = _mod._generate_dqdl_rules(
            [
                {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.9},
                {"target_field": "phone", "source_field": None, "confidence": 0.2},
            ]
        )
        assert 'Completeness "customer_id" >= 0.95' in dqdl
        assert "phone" not in dqdl


# ── p64 2d.4 — the ordering rule, guarded against a *future* skip ────
#
# do-NOT 7 / R8.5 says `validate_identifier` stays before **any** skip. The
# class above pins that behaviourally, but only for the ONE skip that exists
# today (`_has_usable_source_field`, p37 C7): every test in it drives an
# unsourced mapping. A *second* skip added ahead of the sink — which is
# exactly what R8's per-field configurability invites — would leave all of
# those tests green, because none of them can reach a skip that does not yet
# exist. That is the hole this section closes.
#
# So the assertion is structural rather than behavioural: read the loop and
# require the sink to precede every early exit in it. A behavioural test can
# only cover skips someone thought to write a case for; the shape of the loop
# covers the ones they did not.
#
# WHY ONE PRE-SINK GUARD IS LEGITIMATE. `if not target_field: continue` sits
# BEFORE the sink and must stay allowed. R8.5 governs "field names reaching a
# generated-code sink"; a falsy `target_field` is not a name and reaches
# nothing, and `validate_identifier("")` would raise — so hoisting the sink
# above that guard would fail every mapping list carrying an incomplete entry.
# The exemption is therefore expressed as a rule about what the guard READS,
# not as a match on its source text: a guard that consults ONLY the identifier
# variable is deciding "there is no name here", while a guard that consults
# anything else (a source_field, a flag, a set) is deciding "there is a name
# here, but skip it" — and that second kind is precisely what do-NOT 7 forbids
# ahead of the sink.

_DQDL_SINK_CALL = "validate_identifier"
_DQDL_SINK_ARG = "target_field"


def _function_def(name: str) -> ast.FunctionDef:
    """Return the AST node for ``name`` as defined in ``code_assembly``.

    The file is resolved from ``_generate_dqdl_rules``' own function object
    rather than from a hardcoded filename or from ``_mod.__file__``. Two traps
    make that necessary: ``_mod`` here is ``lambda_function``, which RE-EXPORTS
    the assembly helpers (P28 R19.1's decomposition) — and it *also* defines its
    own ``generate_code``, a different function from the one holding these two
    sinks. Anchoring on a symbol that exists only in ``code_assembly`` picks the
    right file, and keeps working if the module is renamed again.
    """
    anchor = inspect.getsourcefile(_mod._generate_dqdl_rules)
    assert anchor, "could not resolve the source file for _generate_dqdl_rules"
    with open(anchor, encoding="utf-8") as handle:
        module = ast.parse(handle.read())

    for node in module.body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} is not a module-level function in {anchor}")


def _per_mapping_loop(func: ast.FunctionDef) -> ast.For:
    """The loop that walks field_mappings — the one carrying the sink."""
    for node in ast.walk(func):
        if isinstance(node, ast.For) and isinstance(node.iter, ast.Name) and node.iter.id == "field_mappings":
            return node
    raise AssertionError(f"no `for ... in field_mappings` loop in {func.name}")


def _sink_lineno(loop: ast.For) -> int:
    """Line of `validate_identifier(target_field, ...)` inside the loop."""
    for node in ast.walk(loop):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _DQDL_SINK_CALL
            and node.args
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id == _DQDL_SINK_ARG
        ):
            return node.lineno
    raise AssertionError(
        f"no `{_DQDL_SINK_CALL}({_DQDL_SINK_ARG}, ...)` call inside _generate_dqdl_rules' "
        "per-mapping loop. P28 R12.1's injection sink was removed or renamed — the DQDL "
        "field name is interpolated into a triple-quoted block in the rendered job, so "
        "this call is what stops a hostile CSV header becoming code."
    )


def _names_read(node: ast.AST) -> set[str]:
    return {inner.id for inner in ast.walk(node) if isinstance(inner, ast.Name)}


def _early_exits(loop: ast.For) -> list[tuple[ast.stmt, ast.expr | None]]:
    """Every continue/break/return in the loop, with the `if` test governing it.

    Recurses through nested blocks so a skip buried one level down is still
    seen; ``guard`` is ``None`` for an unconditional exit.
    """
    found: list[tuple[ast.stmt, ast.expr | None]] = []

    def walk(stmts: list[ast.stmt], guard: ast.expr | None) -> None:
        for stmt in stmts:
            if isinstance(stmt, (ast.Continue, ast.Break, ast.Return)):
                found.append((stmt, guard))
            elif isinstance(stmt, ast.If):
                walk(stmt.body, stmt.test)
                walk(stmt.orelse, stmt.test)
            else:
                for attr in ("body", "orelse", "finalbody"):
                    nested = getattr(stmt, attr, None)
                    if isinstance(nested, list):
                        walk([s for s in nested if isinstance(s, ast.stmt)], guard)

    walk(loop.body, None)
    return found


class TestValidateIdentifierPrecedesEverySkip:
    """do-NOT 7 / R8.5 as a property of the loop, not of one input shape.

    ``_generate_dqdl_rules``' per-mapping loop must run P28 R12.1's identifier
    sink before it can ``continue`` past a mapping — with the single documented
    exemption for the guard that only asks whether a name is present at all.

    Derived from R8.5's wording ("field names reaching a generated-code sink
    MUST pass ``validate_identifier`` **before** any skip logic"), not from the
    loop's current arrangement: the assertion below would have to change if the
    requirement changed, and does not have to change when a legitimate new skip
    is added *after* the sink.

    **Validates: Requirements 8.5 (p64); 2.7 (p37); 12.1 (P28)**
    """

    def test_no_skip_that_inspects_a_mapping_runs_before_the_sink(self):
        loop = _per_mapping_loop(_function_def("_generate_dqdl_rules"))
        sink = _sink_lineno(loop)

        offenders = []
        for stmt, guard in _early_exits(loop):
            if stmt.lineno > sink:
                continue
            reads = _names_read(guard) if guard is not None else set()
            if guard is not None and reads <= {_DQDL_SINK_ARG}:
                # The documented exemption: this guard only asks whether a name
                # is present. A falsy target_field reaches no sink.
                continue
            offenders.append((stmt.lineno, ast.unparse(guard) if guard is not None else "<unconditional>"))

        assert not offenders, (
            "a skip that inspects the mapping runs BEFORE "
            f"{_DQDL_SINK_CALL}({_DQDL_SINK_ARG}, ...) (line {sink}) in _generate_dqdl_rules: "
            f"{offenders}.\n"
            "do-NOT 7 / R8.5 forbids 'skip first, validate later'. It looks harmless — the "
            "field produces no rule either way — but it retires P28 R12.1's injection sink "
            "for exactly the entries an attacker controls most cheaply: a target field "
            "nothing sources. Move the skip below the validate_identifier call.\n"
            "The one guard allowed above the sink is the one that reads only "
            f"`{_DQDL_SINK_ARG}` — a falsy name is not a name and reaches nothing."
        )

    def test_at_least_one_skip_still_sits_after_the_sink(self):
        """Negative control: the test above must not be able to pass vacuously.

        Delete the p37 skip, or the loop, or the sink, and the assertion above
        goes quiet for the wrong reason. This one fails instead.
        """
        loop = _per_mapping_loop(_function_def("_generate_dqdl_rules"))
        sink = _sink_lineno(loop)

        after = [stmt.lineno for stmt, _ in _early_exits(loop) if stmt.lineno > sink]

        assert after, (
            "no early exit remains after the identifier sink in _generate_dqdl_rules. "
            "p37 R2.1/R2.2's Unsourced_Target_Field skip is what makes the ordering rule "
            "mean anything; without it the guard above asserts nothing."
        )


class TestDedupSinkRunsAfterTheDqdlSink:
    """The half of 2c.3's ordering claim its own test left unasserted.

    ``test_the_dedup_validation_is_the_last_identifier_sink`` documents that
    "``merge_key`` **and** ``_generate_dqdl_rules``' per-field call run first"
    but only asserts ``merge_key < dedup``. Both call sites live in
    ``generate_code``, so "runs first" reduces to statement order there.

    **Validates: Requirements 8.5 (p64); 2.7 (p37)**
    """

    def test_generate_code_builds_the_ruleset_before_resolving_quality_config(self):
        generate_code = _function_def("generate_code")

        def call_lineno(name: str) -> int:
            linenos = [
                node.lineno
                for node in ast.walk(generate_code)
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name
            ]
            assert linenos, f"generate_code does not call {name}()"
            return min(linenos)

        dqdl = call_lineno("_generate_dqdl_rules")
        dedup = call_lineno("resolve_quality_config")

        assert dqdl < dedup, (
            f"resolve_quality_config (line {dedup}) was hoisted ahead of "
            f"_generate_dqdl_rules (line {dqdl}). p37 R2.7: an input that trips an "
            "existing sink must still fail with THAT sink's IdentifierValidationError "
            "naming THAT identifier. A hostile mapped target field and a hostile dedup "
            "column in the same request must report the mapped field."
        )
