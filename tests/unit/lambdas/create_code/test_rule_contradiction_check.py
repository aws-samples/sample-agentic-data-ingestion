# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for C8 — the generation-time rule/transform contradiction check.

p37 task 8.5, requirements 2.3 / 2.4 / 2.5. The subject is the machinery task
8.4 added to `lambda/agentic_data_ingestion_create_code/`:

- `code_assembly._expression_reads_a_column` (`:249`) — the classifier;
- `code_assembly._constant_null_columns` (`:281`) — the `withColumn` /
  `.alias` walk, the only function here that raises (`SyntaxError`);
- `code_assembly._dqdl_column_rules` (`:318`) — the bracket scan, mirroring
  `shared/quality_verdict.py::count_dqdl_rules`;
- `code_assembly.detect_rule_contradictions` (`:343`) — never raises;
- `code_assembly.generate_code`'s keyword-only OUT parameter `contradictions`
  (`:388`, check at `:525-532`);
- `lambda_function._feedback_with_contradictions` (`:246`) — the R2.5 merge;
- the handler's record: `output_data.code_contradictions` (`:924`) and
  `summary.contradiction_count` (`:915`).

**Why an AST classifier rather than a substring match, asserted rather than
described.** Attempt 2 of the live run (pipeline `newtest-ab2a08a0`, run
`run-2026-08-09-15-38-19-5c5f5f8a`) produced
`coalesce(lit(None).cast('string'), lit('unknown'))` — an expression that
contains no `lit(None)` token a naive matcher would accept and still reads no
source column. That expression appears **verbatim** in
`_ATTEMPT_2_TRANSFORM` below, which is the anti-substring-matching case.
`LGR-b46a5bb-4` (**template / ratified**) records the same lesson for the
thread-antipattern detector: substring matching forbids documenting the fence
and fails on legitimate text.

**Why the SyntaxError arm is asserted rather than assumed.** `LGR-78ce0b7-13`
/ `LGR-b2f79ef-13` (**template / ratified**, the same decision under two
commit hashes — `decisions(concept="error-handling")` returns nothing else)
forbid re-labelling a classified exception. The check catches `SyntaxError`
narrowly and skips, the arm `validate_pyspark_antipatterns:242` uses; an
unparseable block is `validate_generated_code`'s failure to report, not this
annotation's.

**Why the out parameter is exercised through the shim.** `LGR-b2f79ef-10` /
`LGR-78ce0b7-10` (**template / ratified**): a parameter is threaded *through*
`lambda_function.generate_code`, not around it, because the shim is the only
seam the tests patch. So the render-path assertions here call
`_mod.generate_code`, not `code_assembly.generate_code`.

**Module identity.** The `patch.dict("sys.modules", _mods)` import block below
evicts `code_assembly` and `mapping_resolution` from `sys.modules` on exit, so
a later plain `import code_assembly` in the same pytest process binds a
*second, distinct* module object whose globals the handler never reads. Every
reference here goes through `_mod.code_assembly` for that reason. Related:
`LGR-b7d5490-1` (**backfilled**) records the `sys.modules.pop` +
`sys.path.insert` pattern this directory shares, and why (`lambda_function` is
a name 23 Lambda dirs share).

Nothing in this file changes production code, and nothing in it amends a
pre-existing assertion.

**Validates: Requirements 2.3, 2.4, 2.5**

── p49: the date-format check shares this file, deliberately ───────────────

The final section below belongs to **p49** (requirements R1.1/R1.2/R1.3/R6.4),
not p37, and its assertions are annotated with p49's numbering. It lives here
rather than in a file of its own because p49's C1 **extends this same AST
pass** — `code_assembly.generate_code`'s `:848` check block — and reuses this
file's two framing arguments verbatim: the anti-substring-matching case
(`LGR-9e76249-5`) and the never-blocking contract (`LGR-9e76249-4`). Splitting
it out would have duplicated both.

The subject there is:

- `code_assembly._strptime_format` — the Spark→`strptime` pattern translation,
  deliberately partial;
- `code_assembly._direct_column_read` — the false-positive bias;
- `code_assembly.detect_date_format_mismatches` — never raises;
- `code_assembly.generate_code`'s two new keyword-only parameters,
  `sample_reader` (injected, p49 do-NOT 10) and the OUT list
  `date_format_findings`;
- `lambda_function.read_source_samples` / `_sample_reader_for` — the seam, and
  the JDBC inertness asserted rather than assumed.
- the p49 record and the regeneration wiring (task 2.2):
  `output_data.date_format_findings` + `summary.date_format_finding_count`,
  `_feedback_with_contradictions`' third parameter, and the date term in the
  one-regeneration trigger — the same two-place shape `LGR-9e76249-4`
  (**template / ratified**) describes for the contradiction check.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1, template /
# ratified); these are no-ops kept for parity with the sibling files here.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

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
_shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, _shared_dir)

# `artifact_schema` is deliberately NOT stubbed: `create_output_data` is a pure
# five-key validator (`shared/artifact_schema.py:77`) and the R2.4 assertions
# below are about what it produces, so a MagicMock returning `{}` would make
# `summary.contradiction_count` unobservable. Same for `code_validator` — the
# handler's own `validate_generated_code` binding is patched per-test instead.
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

# Reached through the handler module, never re-imported. See the module
# docstring's "Module identity" note.
_ca = _mod.code_assembly


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


def test_this_file_and_the_handler_share_one_code_assembly_module():
    """Precondition for every assertion below.

    A second `code_assembly` object would make the render-path and handler
    assertions test different code from the pure-function ones, silently.
    """
    assert _ca is sys.modules["lambda_function"].code_assembly
    assert _ca.detect_rule_contradictions.__module__ == "code_assembly"


# ── Transform blocks ────────────────────────────────────────────────────────

# The observed defect: attempt 1's transform. `phone` has no source column, so
# every row carries a constant null and `Completeness "phone" >= 0.95` can
# never pass.
_LIT_NONE_TRANSFORM = 'df_transformed = df.withColumn("phone", lit(None))'

# **Attempt 2's output, VERBATIM.** This is the anti-substring-matching case:
# no `lit(None)` token a naive `in` check would find, and still no column read
# anywhere in the subtree. Do not reformat, re-quote, or "tidy" this string —
# its exact shape is the point of the assertion.
_ATTEMPT_2_TRANSFORM = "df_transformed = df.withColumn(\"phone\", coalesce(lit(None).cast('string'), lit('unknown')))"

# Legitimate null handling. It reads `x`, so flagging it would make the check
# cry wolf on correct code — the false positive that matters most, because the
# finding is recorded where approvers read.
_NULL_HANDLING_TRANSFORM = 'df_transformed = df.withColumn("x", when(col("x").isNull(), lit(None)).otherwise(col("x")))'

_PHONE_COMPLETENESS = 'Rules = [\n    RowCount > 0,\n    Completeness "phone" >= 0.95\n]'


def _rules(*entries: str) -> str:
    return "Rules = [\n    " + ",\n    ".join(("RowCount > 0", *entries)) + "\n]"


# ── R2.3: the three classification cases the task names ─────────────────────


def test_lit_none_is_flagged():
    """A column assigned `lit(None)` contradicts its own Completeness rule.

    Assertion 1 of task 8.5.
    **Validates: Requirements 2.3**
    """
    findings = _ca.detect_rule_contradictions(_LIT_NONE_TRANSFORM, _PHONE_COMPLETENESS)

    assert [f["column"] for f in findings] == ["phone"]
    assert findings[0]["rule"] == 'Completeness "phone"'
    assert "reads no source column" in findings[0]["reason"]


def test_attempt_2s_coalesce_expression_is_flagged_verbatim():
    """`coalesce(lit(None).cast('string'), lit('unknown'))` is flagged.

    Assertion 2 of task 8.5, and the reason C8 is an AST walk rather than a
    match on `lit(None)`: `requirements.md` §Adjacent findings records that
    this expression "would pass a naive 'is it `lit(None)`' check", because the
    assigned expression *is not* `lit(None)` — it is a `coalesce` call — while
    still reading no source column anywhere in its subtree. Guarded here as an
    *executed* expression, not a described one (`LGR-b46a5bb-4`, template /
    ratified).

    **Validates: Requirements 2.3**
    """
    # The premise, asserted so a future reformat of the constant cannot
    # silently turn this into a test of some other expression: the assigned
    # expression is a `coalesce` call, not `lit(None)`, so a matcher keyed on
    # the assigned expression *being* `lit(None)` misses it.
    assert "coalesce(lit(None).cast('string'), lit('unknown'))" in _ATTEMPT_2_TRANSFORM
    assert 'withColumn("phone", lit(None))' not in _ATTEMPT_2_TRANSFORM

    findings = _ca.detect_rule_contradictions(_ATTEMPT_2_TRANSFORM, _PHONE_COMPLETENESS)

    assert [f["column"] for f in findings] == ["phone"]


def test_when_isnull_otherwise_col_is_not_flagged():
    """A legitimate null-handling expression that reads a column is NOT flagged.

    Assertion 3 of task 8.5. `when(col("x").isNull(), lit(None)).otherwise(col("x"))`
    reads `x`, so the column is not constant and the rule is satisfiable.

    This is the other half of the naive-matcher failure, and the half a
    substring search gets wrong in the costly direction: the expression
    **does** contain the literal text `lit(None)`, so `"lit(None)" in block`
    would flag correct null-handling code and record a false positive where
    approvers read. Asserted as a premise below rather than described.
    **Validates: Requirements 2.3**
    """
    assert "lit(None)" in _NULL_HANDLING_TRANSFORM

    findings = _ca.detect_rule_contradictions(_NULL_HANDLING_TRANSFORM, _rules('Completeness "x" >= 0.95'))

    assert findings == []


def test_unparseable_transform_block_skips_without_raising():
    """A block that is not parseable Python yields no findings and no exception.

    Assertion 4 of task 8.5. `detect_rule_contradictions` catches `SyntaxError`
    narrowly and skips (`code_assembly.py:361-367`) — the same arm
    `validate_pyspark_antipatterns:242` takes. An unparseable block is
    `validate_generated_code`'s failure to report; re-raising it here as a
    second, differently-classified error is what do-NOT 9 forbids
    (`LGR-78ce0b7-13`, template / ratified).
    **Validates: Requirements 2.3**
    """
    broken = 'df_transformed = df.withColumn("phone", lit(None)'  # unbalanced

    # The premise: this really is unparseable, and the ONLY function in the
    # chain that raises does raise on it.
    with pytest.raises(SyntaxError):
        _ca._constant_null_columns(broken)

    assert _ca.detect_rule_contradictions(broken, _PHONE_COMPLETENESS) == []


# ── The deliberate false-positive bias, pinned ──────────────────────────────


@pytest.mark.parametrize(
    "expression",
    [
        'col("mobile")',
        'column("mobile")',
        'expr("mobile")',
        'df["mobile"]',
        'coalesce(col("mobile"), lit("unknown"))',
    ],
)
def test_column_reading_calls_and_subscripts_are_not_flagged(expression):
    """`col`, `column`, `expr` and a `df["x"]` subscript all count as reads.

    `expr`'s argument is a SQL string this check does not parse, so
    `expr("mobile")` reads a column no AST edge expresses — counting it as
    constant-null would be a FALSE POSITIVE recorded where approvers read,
    which for a non-blocking heuristic is worse than the false negative of
    missing `expr("'unknown'")`. Task 8.4 chose that bias deliberately; this
    pins it.
    **Validates: Requirements 2.3**
    """
    transform = f'df_transformed = df.withColumn("phone", {expression})'

    assert _ca.detect_rule_contradictions(transform, _PHONE_COMPLETENESS) == []


def test_a_column_assigned_twice_from_a_source_and_a_constant_is_not_flagged():
    """A column is reported only when NONE of its assignments read a column.

    Same bias against false positives: the last write may well be the sourced
    one, and this finding feeds a prompt and a record an approver reads.
    **Validates: Requirements 2.3**
    """
    transform = (
        'df_transformed = df.withColumn("phone", lit(None))\n'
        'df_transformed = df_transformed.withColumn("phone", col("mobile"))'
    )

    assert _ca._constant_null_columns(transform) == set()
    assert _ca.detect_rule_contradictions(transform, _PHONE_COMPLETENESS) == []


def test_a_non_literal_column_name_is_ignored():
    """`withColumn(field.name, ...)` names no literal column, so it is skipped.

    The PySpark templates' own cast loops take a non-literal column name and
    are not the AI's improvisation — `generate_code` parses only the transform
    block for that reason (`code_assembly.py:519-524`).
    **Validates: Requirements 2.3**
    """
    transform = "for field in schema.fields:\n    df_transformed = df.withColumn(field.name, lit(None))"

    assert _ca._constant_null_columns(transform) == set()


def test_the_alias_shape_is_recognised():
    """`<expr>.alias("<name>")` names a target column too, and is classified."""
    constant = "df_transformed = df.select(lit(None).alias(\"phone\"), col('mobile').alias('mobile'))"

    assert _ca._constant_null_columns(constant) == {"phone"}


def test_uniqueness_rules_are_flagged_as_well_as_completeness():
    """Both column-naming rule kinds `_generate_dqdl_rules` emits are checked.

    `RowCount` names no column, so it cannot contradict a per-column
    transform and is not reported.
    **Validates: Requirements 2.3**
    """
    transform = 'df_transformed = df.withColumn("customer_id", lit(None))'
    rules = _rules('Completeness "customer_id" >= 0.95', 'Uniqueness "customer_id" >= 0.95')

    findings = _ca.detect_rule_contradictions(transform, rules)

    assert sorted(f["rule"] for f in findings) == ['Completeness "customer_id"', 'Uniqueness "customer_id"']


@pytest.mark.parametrize("ruleset", [None, "", "Rules = [\n    RowCount > 0\n]", "not a ruleset"])
def test_no_column_rule_means_no_finding(ruleset):
    """With no column rule there is nothing to contradict, whatever the block.

    Includes the shapes `_dqdl_column_rules` must absorb rather than raise on:
    `None`, empty, a RowCount-only ruleset, and a string with no brackets.
    **Validates: Requirements 2.3**
    """
    assert _ca.detect_rule_contradictions(_LIT_NONE_TRANSFORM, ruleset) == []


def test_a_rule_on_a_column_the_transform_never_names_is_not_flagged():
    """Only the intersection is reported — no rule, no column, no guessing."""
    findings = _ca.detect_rule_contradictions(_LIT_NONE_TRANSFORM, _rules('Completeness "email" >= 0.95'))

    assert findings == []


# ── R2.3 at the render path: the OUT parameter, through the shim ────────────

_TEMPLATE_TARGET_CONFIG = {
    "type": "s3_tables",
    "bucket_name": "b",
    "namespace_name": "n",
    "table_name": "t",
}

# A mapping record in the shape `store_mapping_separately` writes — the
# `mapping_result` JSON string variant `_extract_field_mappings` documents as
# the current schema_mapping output (`mapping_resolution.py:48-49`). `phone`
# carries a usable `source_field`, so C7 does NOT skip it and the Completeness
# rule IS emitted: that is precisely the case C8 exists for, and the one C7
# alone cannot reach (design C8, "the AI transform independently nulls a
# column that DOES have a source").
_SOURCED_PHONE_MAPPING = {
    "mapping_id": "map-c8",
    "mapping_result": '{"mappings": [{"target_field": "phone", "source_field": "mobile", "confidence": 0.4}]}',
    "source_schema_hash": "src-hash-c8",
    "target_schema_hash": "tgt-hash-c8",
}


def _pipeline_metadata() -> dict:
    return {
        "target_config": _TEMPLATE_TARGET_CONFIG,
        "source_config": {},
        "source_type": "S3_UPLOAD",
        "source_schema": {"properties": {}},
        "target_schema": {},
    }


def test_generate_code_fills_the_out_list_through_the_shim():
    """The check runs inside `generate_code` and reports via the out parameter.

    Driven through `lambda_function.generate_code` — the shim — rather than
    `code_assembly.generate_code`, because `LGR-b2f79ef-10` (template /
    ratified) is why the parameter was threaded through it: the shim is the
    only seam the tests patch, so shortcutting past it would look harmless and
    would not be.
    **Validates: Requirements 2.3**
    """
    contradictions: list[dict[str, str]] = []
    with patch.object(_mod, "generate_transform_with_ai", return_value=_ATTEMPT_2_TRANSFORM):
        rendered = _mod.generate_code(
            _pipeline_metadata(),
            _SOURCED_PHONE_MAPPING,
            contradictions=contradictions,
        )

    # Non-vacuity: the rule really is in the job the generator produced, so
    # the finding describes the ruleset the Glue job would enforce.
    assert 'Completeness "phone" >= 0.95' in rendered
    assert [f["column"] for f in contradictions] == ["phone"]


def test_omitting_the_out_parameter_skips_the_check_entirely():
    """No out list, no check — what makes C8 free for every other caller.

    `generate_code`'s docstring states this; the return stays a `str` that two
    callers and roughly a dozen tests assert directly.
    **Validates: Requirements 2.3**
    """
    with patch.object(_mod, "generate_transform_with_ai", return_value=_ATTEMPT_2_TRANSFORM):
        rendered = _mod.generate_code(_pipeline_metadata(), _SOURCED_PHONE_MAPPING)

    assert isinstance(rendered, str)
    assert 'Completeness "phone" >= 0.95' in rendered


def test_a_clean_transform_leaves_the_out_list_empty():
    """0 findings is a real value — "checked, no contradiction"."""
    contradictions: list[dict[str, str]] = []
    with patch.object(
        _mod, "generate_transform_with_ai", return_value='df_transformed = df.withColumn("phone", col("mobile"))'
    ):
        _mod.generate_code(_pipeline_metadata(), _SOURCED_PHONE_MAPPING, contradictions=contradictions)

    assert contradictions == []


# ── R2.5: the feedback merge ────────────────────────────────────────────────


def test_feedback_returns_its_input_unchanged_when_there_is_nothing_to_merge():
    """The anti-pattern-only retry keeps its exact pre-p37 prompt.

    Identity, not equality: returning a copy would be a different dict for the
    `_build_feedback_section` renderer to read, and the point of the arm is
    that nothing changed.
    **Validates: Requirements 2.5**
    """
    original = {"reviewer_issues": ["something the reviewer said"]}

    assert _mod._feedback_with_contradictions(original, []) is original
    assert _mod._feedback_with_contradictions(None, []) is None


def test_feedback_never_mutates_the_callers_dict():
    """The same dict is read again on the fallback path, so it must survive.

    **Validates: Requirements 2.5**
    """
    original = {"issues_found": ["reviewer said this"]}
    snapshot = {"issues_found": ["reviewer said this"]}

    merged = _mod._feedback_with_contradictions(original, [{"column": "phone", "rule": "r", "reason": "because"}])

    assert original == snapshot
    assert merged is not original
    assert merged["issues_found"] is not original["issues_found"]


def test_feedback_appends_each_finding_reason_to_issues_found():
    """`issues_found` is the channel `_build_feedback_section` renders (P28 R13.3).

    A generation-time finding travels the channel a reviewer finding already
    travels, which is what makes the regeneration prompt differ instead of
    being byte-identical.
    **Validates: Requirements 2.5**
    """
    merged = _mod._feedback_with_contradictions(
        {"issues_found": ["reviewer said this"]},
        [
            {"column": "phone", "rule": 'Completeness "phone"', "reason": "phone is constant"},
            {"column": "email", "rule": 'Completeness "email"', "reason": "email is constant"},
        ],
    )

    assert merged["issues_found"] == ["reviewer said this", "phone is constant", "email is constant"]


def test_feedback_starts_a_fresh_issues_list_when_there_was_no_feedback():
    """A first-attempt generation has no feedback dict at all.

    **Validates: Requirements 2.5**
    """
    merged = _mod._feedback_with_contradictions(None, [{"column": "phone", "rule": "r", "reason": "phone is constant"}])

    assert merged == {"issues_found": ["phone is constant"]}


# ── R2.4 / R2.5 at the handler: the record, and the non-blocking guarantee ──


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    Same shape as `test_target_schema_and_hash_reads.py`'s helper, with one
    addition: `__enter__` returns the dict of values the managers *yielded*
    (the mocks), keyed by the same names. `patch.object(...)` is a `_patch`
    object, not a mock — asserting on the manager instead of on what it yields
    fails with `AttributeError: '_patch' object has no attribute ...`, so the
    two are kept distinct here rather than by convention.
    """

    def __init__(self, managers: dict):
        self._managers = managers

    def __enter__(self) -> dict:
        self._entered = []
        values = {}
        for name, manager in self._managers.items():
            values[name] = manager.__enter__()
            self._entered.append(manager)
        return values

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


RUN_ID = "run-c8"
PIPELINE_ID = "pipe-c8"


def _handler_patches(state_table, transform_block):
    """Everything `lambda_handler` touches, with the AI + AWS boundaries stubbed.

    `generate_code` is deliberately NOT patched — the real one must run for the
    contradiction to be produced at all. Only the AI transform generator is
    stubbed, at the `generate_transform_with_ai` seam the shim resolves.
    """
    return {
        "get_pipeline_metadata": patch.object(
            _mod, "get_pipeline_metadata", return_value={"target_config": _TEMPLATE_TARGET_CONFIG}
        ),
        "pipeline_runs_table": patch.object(_mod, "pipeline_runs_table", state_table),
        "mappings_table": patch.object(
            _mod,
            "mappings_table",
            MagicMock(get_item=MagicMock(return_value={"Item": dict(_SOURCED_PHONE_MAPPING)})),
        ),
        "generated_code_table": patch.object(_mod, "generated_code_table", MagicMock()),
        "s3_client": patch.object(_mod, "s3_client", MagicMock()),
        "publish_pipeline_state_change": patch.object(_mod, "publish_pipeline_state_change"),
        "publish_approval_status_change": patch.object(_mod, "publish_approval_status_change"),
        "generate_transform_with_ai": patch.object(_mod, "generate_transform_with_ai", return_value=transform_block),
        "validate_generated_code": patch.object(
            _mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}
        ),
        "validate_pyspark_antipatterns": patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    }


def _state_table():
    return MagicMock(
        get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )


def _run_handler(transform_block):
    state_table = _state_table()
    with _nested(_handler_patches(state_table, transform_block)):
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )
    state_row = state_table.put_item.call_args.kwargs["Item"]
    return result, state_row


def test_the_finding_lands_on_output_data_and_increments_the_summary_count():
    """R2.4: the named finding is on the `create_code` state row.

    Assertion 5 of task 8.5. `output_data.code_contradictions` carries
    `[{column, rule, reason}]` and `summary.contradiction_count` carries its
    length — persisted where the approval surfaces already read from.
    **Validates: Requirements 2.4**
    """
    result, state_row = _run_handler(_ATTEMPT_2_TRANSFORM)

    assert result["statusCode"] == 200
    output_data = state_row["output_data"]
    findings = output_data["code_contradictions"]
    assert [f["column"] for f in findings] == ["phone"]
    assert findings[0]["rule"] == 'Completeness "phone"'
    assert set(findings[0]) == {"column", "rule", "reason"}
    assert output_data["summary"]["contradiction_count"] == len(findings) == 1


def test_a_contradiction_does_not_fail_the_step():
    """The non-blocking guarantee, asserted rather than assumed.

    Assertion 6 of task 8.5. Design C8's do-NOT-block and risk 4: this is a
    conservative static analysis of AI output, and the arm at
    `create_code/lambda_function.py:731-740` already takes the same
    "proceeding as-is" route the anti-pattern check takes. R2.1/R2.2 are the
    deterministic half; the requirements deliberately say *record and feed
    back*, not *block*.

    So the handler returns 200, writes its state row with `status: "success"`,
    and publishes its pipeline-state event — with a contradiction present and
    persisted on the very same row.
    **Validates: Requirements 2.3, 2.4**
    """
    state_table = _state_table()
    with _nested(_handler_patches(state_table, _ATTEMPT_2_TRANSFORM)) as mocks:
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )
        publish_state = mocks["publish_pipeline_state_change"]

    state_row = state_table.put_item.call_args.kwargs["Item"]

    # The contradiction really is present on this run — otherwise the 200 below
    # would prove nothing.
    assert state_row["output_data"]["code_contradictions"] != []
    assert result["statusCode"] == 200
    assert state_row["status"] == "success"
    assert state_row["output_data"]["status"] == "success"
    publish_state.assert_called_once()


def test_a_clean_run_records_a_zero_count_and_an_empty_list():
    """0 is a real value: "checked, no contradiction", not an absent key.

    The counterpart to the two assertions above — without it, a handler that
    dropped the key entirely would pass them both.
    **Validates: Requirements 2.4**
    """
    result, state_row = _run_handler('df_transformed = df.withColumn("phone", col("mobile"))')

    assert result["statusCode"] == 200
    assert state_row["output_data"]["code_contradictions"] == []
    assert state_row["output_data"]["summary"]["contradiction_count"] == 0


def test_a_contradiction_triggers_one_regeneration_carrying_the_finding():
    """R2.5: the finding reaches the next prompt on attempt 1, not attempt 3.

    The live run exhausted `regeneration_max_attempts: 2` before the reviewer's
    finding could act, so C8 uses the in-process re-call that already exists
    for anti-patterns (`:696-740`) and merges its findings into the
    `regeneration_feedback` dict `_build_feedback_section` renders.

    Exactly ONE re-call: `generate_transform_with_ai` is called twice, and the
    second call carries the finding's reason in `issues_found`.
    **Validates: Requirements 2.5**
    """
    state_table = _state_table()
    with _nested(_handler_patches(state_table, _ATTEMPT_2_TRANSFORM)) as mocks:
        _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )
        generate_transform = mocks["generate_transform_with_ai"]

    assert generate_transform.call_count == 2
    first_feedback = generate_transform.call_args_list[0].kwargs["regeneration_feedback"]
    retry_feedback = generate_transform.call_args_list[1].kwargs["regeneration_feedback"]

    assert not first_feedback
    issues = retry_feedback["issues_found"]
    assert len(issues) == 1
    assert "phone" in issues[0]


# ════════════════════════════════════════════════════════════════════════════
# p49 — the date-format check (design C1). See the module docstring for why
# these live in this file rather than one of their own.
# ════════════════════════════════════════════════════════════════════════════

# **The observed run's source values, VERBATIM.** Pipeline
# `latesttesting-19ca826a`, run `run-2026-08-24-13-48-26-4fccdf03`, file
# `customer_data_6.csv`, column `signup_date`. Do not reformat, re-order or
# "tidy" these — the regression this spec exists for is exactly these five
# strings against exactly the format below.
_OBSERVED_SIGNUP_DATES = ["01/15/2024", "02/20/2024", "03/10/2024", "04/05/2024", "05/12/2024"]

# **The format the generated code actually emitted**, taken from line 132 of
# the observed run's script: it came from the mapping hint's parenthetical
# example (`TO_DATE(signup_date, 'YYYY-MM-DD')`) and matches none of the data.
_OBSERVED_EMITTED_FORMAT = "yyyy-MM-dd"

_OBSERVED_TRANSFORM = 'df_transformed = df.withColumn("registration_date", to_date(col("signup_date"), "yyyy-MM-dd"))'

# The sentinel `schema_extract:1735` writes for a JDBC source:
# `original_s3_uri or f"jdbc://pipeline/{pipeline_id}"`, and a JDBC source has
# no uploaded object, so it always lands on this form.
_JDBC_SENTINEL_URI = "jdbc://pipeline/latesttesting-19ca826a"


def _reader(samples):
    """The injected zero-argument sample reader, as `_sample_reader_for` returns."""

    def _read():
        return samples

    return _read


# ── R1.1: the regression. Derived from the requirement, not from output ─────


def test_the_observed_runs_values_and_format_produce_a_finding():
    """THE regression test: five MM/dd/yyyy values against `yyyy-MM-dd`.

    R1.1's minimum bar, and the assertion design §Testing Strategy requires to
    exist. The expectation comes from the requirement — "a date transform must
    parse the data it will receive" — not from current behaviour, which is the
    defect.

    Non-vacuity is asserted first: none of the five values parses under the
    emitted format, which is *why* Spark nulled all five and the
    `Completeness "registration_date"` rule reported actual 0 against 1 on
    every other field.
    **Validates: Requirements 1.1, 1.2, 1.3**
    """
    strptime_format = _ca._strptime_format(_OBSERVED_EMITTED_FORMAT)
    assert strptime_format == "%Y-%m-%d"
    assert not any(_ca._value_parses(value, strptime_format) for value in _OBSERVED_SIGNUP_DATES)

    findings = _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}))

    assert len(findings) == 1
    assert findings[0]["column"] == "signup_date"
    assert findings[0]["function"] == "to_date"
    assert findings[0]["format"] == "yyyy-MM-dd"
    # The reason names the sampled values, so the finding says what the data
    # looks like rather than only that something is wrong. It must NOT assert a
    # single correct format — `05/12/2024` is valid under MM/dd/yyyy AND
    # dd/MM/yyyy, and settling that is C3's job (do-NOT 8, R5.1).
    assert "01/15/2024" in findings[0]["reason"]
    assert "MM/dd/yyyy" not in findings[0]["reason"]


def test_the_correct_format_for_the_observed_values_produces_no_finding():
    """The other half of the regression: `MM/dd/yyyy` over the same data is clean.

    Without this, a checker that flagged every `to_date` would pass the test
    above.
    **Validates: Requirements 1.1**
    """
    transform = 'df_transformed = df.withColumn("registration_date", to_date(col("signup_date"), "MM/dd/yyyy"))'

    assert _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES})) == []


def test_iso_values_with_the_iso_format_produce_no_finding():
    """ISO data parsed as ISO is the case the observed hint assumed existed.

    **Validates: Requirements 1.1**
    """
    iso_values = ["2024-01-15", "2024-02-20", "2024-03-10"]

    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"signup_date": iso_values})) == []


def test_to_timestamp_is_checked_as_well_as_to_date():
    """Both formatted-parse functions are covered (design O4).

    Only `to_date` has an observed failure behind it; `to_timestamp` is by
    analogy, and both are imported by every scaffold.
    **Validates: Requirements 1.1**
    """
    transform = 'df_transformed = df.withColumn("t", to_timestamp(col("ts"), "yyyy-MM-dd HH:mm:ss"))'

    findings = _ca.detect_date_format_mismatches(transform, _reader({"ts": ["15/01/2024 10:30:00"]}))

    assert [f["function"] for f in findings] == ["to_timestamp"]


def test_a_column_read_via_subscript_is_checked():
    """`df["x"]` names a column with no `col()` call — the sibling check's rule."""
    transform = 'df_transformed = df.withColumn("d", to_date(df["signup_date"], "yyyy-MM-dd"))'

    findings = _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}))

    assert [f["column"] for f in findings] == ["signup_date"]


def test_one_finding_per_column_and_format_pair():
    """The same column parsed the same way twice is one finding, not two."""
    transform = (
        'df_a = df.withColumn("d", to_date(col("signup_date"), "yyyy-MM-dd"))\n'
        'df_transformed = df_a.withColumn("e", to_date(col("signup_date"), "yyyy-MM-dd"))'
    )

    findings = _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}))

    assert len(findings) == 1


# ── The false-positive bias, pinned where it costs something ────────────────


def test_a_wrapped_column_read_is_not_checked():
    """A wrapper may rewrite the layout before the parse, so it is skipped.

    `to_date(regexp_replace(col("d"), "/", "-"), "yyyy-MM-dd")` is CORRECT code
    over MM/dd/yyyy data. Testing the raw sampled values against the emitted
    format would report a mismatch that does not exist — recorded where
    approvers read, and fed into a regeneration prompt. The checker cannot know
    which wrappers preserve the layout, so it checks none of them.
    **Validates: Requirements 1.3**
    """
    transform = (
        'df_transformed = df.withColumn("d", to_date(regexp_replace(col("signup_date"), "/", "-"), "yyyy-MM-dd"))'
    )

    assert _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES})) == []


def test_a_format_the_checker_does_not_understand_produces_no_finding():
    """An unknown pattern is not evidence of a mismatch (design §Error Handling).

    Timezone letters are absent from the translation table on purpose, so a
    pattern using one is inert rather than guessed at.
    **Validates: Requirements 6.4**
    """
    transform = 'df_transformed = df.withColumn("t", to_timestamp(col("ts"), "yyyy-MM-dd\'T\'HH:mm:ssXXX"))'

    assert _ca._strptime_format("yyyy-MM-dd'T'HH:mm:ssXXX") is None
    assert _ca.detect_date_format_mismatches(transform, _reader({"ts": ["01/15/2024"]})) == []


def test_a_non_literal_format_produces_no_finding():
    """A format read from a variable is not a literal this checker can test."""
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), fmt))'

    assert _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES})) == []


def test_a_format_less_to_date_produces_no_finding():
    """`to_date(col("x"))` names no format, so there is nothing to test."""
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date")))'

    assert _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES})) == []


@pytest.mark.parametrize(
    ("spark_format", "expected"),
    [
        ("yyyy-MM-dd", "%Y-%m-%d"),
        ("MM/dd/yyyy", "%m/%d/%Y"),
        ("dd-MM-yyyy", "%d-%m-%Y"),
        ("yyyy-MM-dd HH:mm:ss", "%Y-%m-%d %H:%M:%S"),
        ("yyyy-MM-dd HH:mm:ss.SSS", "%Y-%m-%d %H:%M:%S.%f"),
        ("yyyy-MM-dd'T'HH:mm:ss", "%Y-%m-%dT%H:%M:%S"),
        ("dd MMM yyyy", "%d %b %Y"),
        ("MMMM d, yyyy", "%B %d, %Y"),
        ("yy/MM/dd", "%y/%m/%d"),
        ("yyyy-MM-dd hh:mm a", "%Y-%m-%d %I:%M %p"),
    ],
)
def test_understood_patterns_translate(spark_format, expected):
    """The patterns the translation table claims to handle, asserted one by one."""
    assert _ca._strptime_format(spark_format) == expected


@pytest.mark.parametrize(
    "spark_format",
    [
        "yyyy-MM-ddXXX",  # timezone offset — absent from the table on purpose
        "yyyy-MM-dd Z",  # RFC 822 timezone
        "GGGG yyyy",  # era
        "QQ yyyy",  # quarter
        "yyyy-MM-dd 'unterminated",  # unterminated quote
        "yyyy-MM-%d",  # a literal % would collide with the directives
        "yyyy-MM-dd1",  # a bare digit no emitted pattern carries
        "MM/dd",  # no year — see the forward-compat note below
        "HH:mm:ss",  # time only
        "",
        "   ",
        None,
    ],
)
def test_patterns_the_checker_refuses(spark_format):
    """Everything the translator declines, so the decline is deliberate not luck.

    Declining is always safe — the caller records no finding. A wrong
    translation is not safe, because it makes correct code look broken.

    The two yearless entries are refused for a forward-compatibility reason
    rather than an ambiguity one: CPython deprecated parsing a day-of-month
    without a year (cpython#70647) and may RAISE from 3.15. `_value_parses`
    catches that as "did not parse", so a yearless format would silently become
    zero parses and a FALSE finding on correct code.
    **Validates: Requirements 6.4**
    """
    assert _ca._strptime_format(spark_format) is None


def test_a_format_repeating_a_directive_does_not_raise():
    """A CP2 hole the property found, pinned as an example so it stays closed.

    `strptime` compiles the format into a regex with one NAMED group per
    directive, so `y:y` → `%Y:%Y` raises
    `re.PatternError("redefinition of group name 'Y'")` — a class deriving from
    `Exception`, NOT `ValueError`, which is how it escaped `_value_parses`'
    clause and propagated out of `generate_code`. Found by
    `test_the_pattern_translation_is_total`'s generator; pinned here because
    the hypothesis example database is not committed, so the property alone
    does not guarantee the case is re-drawn on a fresh checkout.

    The translation still succeeds — refusing it there would need the
    translator to reason about directive uniqueness — so what is asserted is
    that the parse ATTEMPT is total, and that a whole-check run over such a
    format returns a list rather than raising.
    **Validates: Requirements 6.4**
    """
    translated = _ca._strptime_format("y:y")

    assert translated == "%Y:%Y"
    assert _ca._value_parses("01/15/2024", translated) is False

    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "y:y"))'
    findings = _ca.detect_date_format_mismatches(transform, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}))

    assert [f["format"] for f in findings] == ["y:y"]


# ── R6.4 / CP2: every inert case, one assertion each ───────────────────────


def test_no_sample_for_the_parsed_column_is_inert():
    """A column absent from the sample is no evidence of anything."""
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"other_column": ["x"]})) == []


def test_an_empty_sample_for_the_parsed_column_is_inert():
    """Sampled to zero non-empty values — the same as never sampled."""
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"signup_date": []})) == []
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"signup_date": ["", "   "]})) == []


def test_a_reader_returning_nothing_is_inert():
    """The fetch returned nothing — an empty object, or `json_s3_uri` absent."""
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({})) == []


def test_no_reader_at_all_is_inert():
    """`sample_reader=None` — the case every caller that does not opt in hits."""
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, None) == []


def test_a_reader_that_raises_is_caught_and_inert():
    """`AccessDenied`, `NoSuchKey`, throttling, a decode error — all inert.

    A telemetry read must never fail code generation (design §Error Handling,
    final row; `LGR-9e76249-4`).
    **Validates: Requirements 6.4**
    """

    def _raising():
        raise RuntimeError("AccessDenied")

    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _raising) == []


def test_an_unparseable_transform_block_is_inert_for_the_date_check():
    """The same skip the sibling check takes on a `SyntaxError`.

    `validate_generated_code` owns that failure; re-raising it here as a
    second, differently-classified error is what `LGR-78ce0b7-13` forbids.
    **Validates: Requirements 6.4**
    """
    broken = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "yyyy-MM-dd")'  # unbalanced

    with pytest.raises(SyntaxError):
        _ca._date_format_calls(broken)

    assert _ca.detect_date_format_mismatches(broken, _reader({"signup_date": _OBSERVED_SIGNUP_DATES})) == []


def test_an_empty_transform_block_is_inert():
    assert _ca.detect_date_format_mismatches("", _reader({"signup_date": _OBSERVED_SIGNUP_DATES})) == []


def test_the_reader_is_not_called_when_there_is_no_date_parse_to_check():
    """The AST scan runs FIRST, so a pipeline with no dates spends no S3 request.

    This is why `sample_reader` is a callable rather than a dict of samples: the
    read cost is paid exactly where a finding is possible.
    """
    calls = []

    def _counting_reader():
        calls.append(1)
        return {"signup_date": _OBSERVED_SIGNUP_DATES}

    findings = _ca.detect_date_format_mismatches('df_transformed = df.withColumn("x", col("y"))', _counting_reader)

    assert findings == []
    assert calls == []


def test_the_reader_is_called_once_when_there_is_something_to_check():
    """The counterpart: one read per generation that has a date parse in it."""
    calls = []

    def _counting_reader():
        calls.append(1)
        return {"signup_date": _OBSERVED_SIGNUP_DATES}

    findings = _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _counting_reader)

    assert len(findings) == 1
    assert calls == [1]


# ── R1.2 / do-NOT 10: the seam, through the shim ───────────────────────────


def test_the_finding_is_produced_at_code_assembly_through_the_shim():
    """R1.2: the mismatch is detected at code generation, before any Glue run.

    Driven through `lambda_function.generate_code` — the shim — for the reason
    `LGR-b2f79ef-10` (template / ratified) gives: the shim is the only seam the
    tests patch, so shortcutting past it would look harmless and would not be.
    **Validates: Requirements 1.2**
    """
    findings: list[dict[str, str]] = []
    with patch.object(_mod, "generate_transform_with_ai", return_value=_OBSERVED_TRANSFORM):
        rendered = _mod.generate_code(
            _pipeline_metadata(),
            _SOURCED_PHONE_MAPPING,
            sample_reader=_reader({"signup_date": _OBSERVED_SIGNUP_DATES}),
            date_format_findings=findings,
        )

    # Non-vacuity: the emitted parse really is in the script that would run.
    assert 'to_date(col("signup_date"), "yyyy-MM-dd")' in rendered
    assert [f["column"] for f in findings] == ["signup_date"]


def test_omitting_the_out_parameter_skips_the_date_check_entirely():
    """No out list, no check — and no read. What keeps C1 free for other callers."""
    calls = []

    def _counting_reader():
        calls.append(1)
        return {"signup_date": _OBSERVED_SIGNUP_DATES}

    with patch.object(_mod, "generate_transform_with_ai", return_value=_OBSERVED_TRANSFORM):
        rendered = _mod.generate_code(_pipeline_metadata(), _SOURCED_PHONE_MAPPING, sample_reader=_counting_reader)

    assert isinstance(rendered, str)
    assert calls == []


def test_a_clean_generation_leaves_the_date_findings_empty():
    """0 findings is a real value — "checked, no mismatch"."""
    findings: list[dict[str, str]] = []
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "MM/dd/yyyy"))'
    with patch.object(_mod, "generate_transform_with_ai", return_value=transform):
        _mod.generate_code(
            _pipeline_metadata(),
            _SOURCED_PHONE_MAPPING,
            sample_reader=_reader({"signup_date": _OBSERVED_SIGNUP_DATES}),
            date_format_findings=findings,
        )

    assert findings == []


def test_code_assembly_still_owns_no_aws_client():
    """do-NOT 10, asserted rather than left as a comment.

    `code_assembly.py:15-18` states the module "owns NO AWS clients and does
    NOT import strands". That fence had no ledger record and no test, which is
    exactly why p49's sample read is INJECTED rather than imported — the module
    already ships the pattern (`generate_transform` injected,
    `quarantine_bucket` passed in). This is the first assertion of it.

    **AST, not a substring search** — and this is not stylistic preference: the
    first draft of this test searched the source text for `"import strands"`
    and failed, because the module's own docstring says "does NOT import
    strands". That is `LGR-b46a5bb-4`'s recorded lesson verbatim — substring
    matching "forbids documenting the fence" — the same reason `LGR-9e76249-5`
    made the sibling check an AST walk.
    """
    import ast as _ast
    import inspect as _inspect

    tree = _ast.parse(open(_ca.__file__, encoding="utf-8").read())  # noqa: SIM115
    imported: set[str] = set()
    for node in _ast.walk(tree):
        if isinstance(node, _ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, _ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "boto3" not in imported
    assert "strands" not in imported
    assert not hasattr(_ca, "s3_client")
    # And the read really does arrive as a parameter instead.
    assert "sample_reader" in _inspect.signature(_ca.generate_code).parameters


# ── R1.3: the seam's reader, and JDBC inertness asserted not assumed ───────


def test_the_reader_samples_a_csv_source():
    """`read_source_samples` over a CSV object — the observed run's shape."""
    body = "customer_id,signup_date\n1,01/15/2024\n2,02/20/2024\n3,02/20/2024\n"
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": MagicMock(read=MagicMock(return_value=body.encode()))}))

    with patch.object(_mod, "s3_client", s3):
        samples = _mod.read_source_samples("s3://adi-source-files-123/uploads/customer_data_6.csv")

    # Unique non-empty values, in first-seen order — `read_sample_data`'s shape.
    assert samples == {"customer_id": ["1", "2", "3"], "signup_date": ["01/15/2024", "02/20/2024"]}
    assert s3.get_object.call_count == 1


def test_the_reader_samples_a_json_source():
    """JSON array and JSON Lines both parse, keyed off the object's extension."""
    body = '[{"signup_date": "01/15/2024"}, {"signup_date": "02/20/2024"}]'
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": MagicMock(read=MagicMock(return_value=body.encode()))}))

    with patch.object(_mod, "s3_client", s3):
        samples = _mod.read_source_samples("s3://adi-source-files-123/uploads/data.json")

    assert samples == {"signup_date": ["01/15/2024", "02/20/2024"]}


def test_the_reader_declines_a_jdbc_uri_before_any_aws_call():
    """R1.3: C1 is inert for a JDBC source, asserted rather than assumed.

    `schema_extract:1735` sets `json_s3_uri` to
    `original_s3_uri or f"jdbc://pipeline/{pipeline_id}"`, and a JDBC source has
    no uploaded object, so it lands on the sentinel. The reader must decline
    **before** touching S3, as `read_sample_data:92-94` declines.

    **One of the two source types is uncovered by this arm, and that is
    accepted, not hidden** — sampling a JDBC source means a `LIMIT`ed query
    against the customer database, with its own credential obligations under
    `LGR-819980f-1` (**invariant**). This test is where that gap is visible.
    **Validates: Requirements 1.3**
    """
    s3 = MagicMock()

    with patch.object(_mod, "s3_client", s3):
        samples = _mod.read_source_samples(_JDBC_SENTINEL_URI)

    assert samples == {}
    s3.get_object.assert_not_called()


def test_the_jdbc_decline_logs_at_info_not_warning():
    """For a JDBC pipeline the sentinel is the NORMAL state, not a fault.

    A warning per JDBC run would be noise that trains readers to ignore the
    channel.
    **Validates: Requirements 1.3**
    """
    with patch.object(_mod, "s3_client", MagicMock()), patch.object(_mod, "logger") as log:
        _mod.read_source_samples(_JDBC_SENTINEL_URI)

    log.warning.assert_not_called()
    log.info.assert_called_once()


def test_an_absent_uri_is_inert_and_makes_no_aws_call():
    """An older run predating `json_s3_uri` is not evidence of a mismatch."""
    s3 = MagicMock()

    with patch.object(_mod, "s3_client", s3), patch.object(_mod, "logger") as log:
        assert _mod.read_source_samples("") == {}

    s3.get_object.assert_not_called()
    log.warning.assert_not_called()


def test_an_unexpected_uri_shape_warns_and_makes_no_aws_call():
    """A non-`s3://`, non-`jdbc://` URI IS unexpected, so it warns."""
    s3 = MagicMock()

    with patch.object(_mod, "s3_client", s3), patch.object(_mod, "logger") as log:
        assert _mod.read_source_samples("https://example.com/data.csv") == {}

    s3.get_object.assert_not_called()
    log.warning.assert_called_once()


def test_the_reader_returns_empty_when_the_read_raises():
    """`read_sample_data:135-137`'s shape: one broad clause, warn, `{}`.

    **Validates: Requirements 6.4**
    """
    s3 = MagicMock(get_object=MagicMock(side_effect=RuntimeError("AccessDenied")))

    with patch.object(_mod, "s3_client", s3):
        assert _mod.read_source_samples("s3://adi-source-files-123/uploads/x.csv") == {}


def test_the_body_read_is_byte_bounded():
    """The one deliberate departure from the reference reader, asserted.

    `complex_mapping_agent.read_sample_data` reads the whole object; it runs on
    AgentCore Runtime. This runs in a 512 MiB Lambda
    (`compute-stack.ts:387`), where an unbounded read of a large source file is
    an OOM — and an OOM in code generation is precisely the failure p49 CP2 and
    `LGR-9e76249-4` forbid. So `read()` is passed a ceiling.
    **Validates: Requirements 6.4**
    """
    body_stream = MagicMock(read=MagicMock(return_value=b"signup_date\n01/15/2024\n"))
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": body_stream}))

    with patch.object(_mod, "s3_client", s3):
        _mod.read_source_samples("s3://adi-source-files-123/uploads/x.csv")

    (amount,) = body_stream.read.call_args.args
    assert amount == _mod._MAX_SAMPLE_BYTES
    assert amount > 0


def test_the_bound_reader_resolves_the_read_at_call_time():
    """`_sample_reader_for` binds the URI, not the function object.

    Resolving `read_source_samples` from module globals at call time is what
    keeps `patch.object(lambda_function, "read_source_samples", ...)`
    intercepting through the bound reader — the property every other wrapper in
    that module relies on. A `functools.partial` would not have it.
    """
    reader = _mod._sample_reader_for("s3://bucket/key.csv")

    with patch.object(_mod, "read_source_samples", return_value={"signup_date": ["01/15/2024"]}) as read:
        assert reader() == {"signup_date": ["01/15/2024"]}

    read.assert_called_once_with("s3://bucket/key.csv")


# ── R1.2 at the handler: the seam is wired to the row already read ──────────


def test_the_handler_binds_the_reader_to_the_schema_extract_rows_json_s3_uri():
    """C1's seam: one more attribute off a `get_item` that already happens.

    `json_s3_uri` is taken off the SAME `schema_extract` Item that already
    supplies `extracted_schema` and `schema_hash`, which is why C1 needs no new
    persisted attribute and did not need `schema_extract` to start storing
    values (`LGR-0739ab8-1`, template / ratified — 247.86 MiB against a 250 MiB
    limit, do-NOT 9).
    **Validates: Requirements 1.2**
    """
    state_table = MagicMock(
        get_item=MagicMock(
            return_value={
                "Item": {
                    # Declares BOTH columns the mapping names, so
                    # `_validate_mapped_source_fields` (`LGR-a419843-2`) does not
                    # refuse this mapping before the date check is reached.
                    "extracted_schema": (
                        '{"properties": {"signup_date": {"type": "string"}, "mobile": {"type": "string"}}}'
                    ),
                    "schema_hash": "src-hash-c8",
                    "json_s3_uri": "s3://adi-source-files-123/uploads/customer_data_6.csv",
                }
            }
        ),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )
    patches = _handler_patches(state_table, _OBSERVED_TRANSFORM)
    patches["read_source_samples"] = patch.object(
        _mod, "read_source_samples", return_value={"signup_date": _OBSERVED_SIGNUP_DATES}
    )

    with _nested(patches) as mocks:
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )
        read = mocks["read_source_samples"]

    assert result["statusCode"] == 200
    read.assert_called_with("s3://adi-source-files-123/uploads/customer_data_6.csv")


# ── p53 R2.4/R2.5: the sample URI is a PREFERENCE, not a replacement ────────
#
# The test above is the third case of three and is deliberately left
# unmodified: its Item carries no `sample_s3_uri` at all, which is the shape
# `store_schema_in_dynamodb` writes when no Sample_Artifact exists (it omits
# the key rather than storing `""` — `schema_extract:1364-1368`). The two tests
# below cover the other two shapes.


def _schema_extract_state_table(**extra_attributes):
    """A `schema_extract` Item carrying whatever URI attributes a case needs."""
    item = {
        "extracted_schema": '{"properties": {"signup_date": {"type": "string"}, "mobile": {"type": "string"}}}',
        "schema_hash": "src-hash-c8",
        "json_s3_uri": "s3://adi-source-files-123/uploads/customer_data_6.csv",
    }
    item.update(extra_attributes)
    return MagicMock(
        get_item=MagicMock(return_value={"Item": item}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )


def _uri_the_reader_was_given(state_table):
    patches = _handler_patches(state_table, _OBSERVED_TRANSFORM)
    patches["read_source_samples"] = patch.object(
        _mod, "read_source_samples", return_value={"signup_date": _OBSERVED_SIGNUP_DATES}
    )
    with _nested(patches) as mocks:
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )
        read = mocks["read_source_samples"]
    assert result["statusCode"] == 200
    return read.call_args.args[0]


def test_the_handler_prefers_the_sample_uri_when_the_row_records_one():
    """p53 R2.4: a recorded `sample_s3_uri` wins over `json_s3_uri`.

    This is the whole of consumer 1's change. The reader is untouched — it is
    simply handed the Sample_Artifact's `s3://` URI instead of the `jdbc://`
    sentinel a JDBC pipeline would otherwise carry, so the date-format check
    goes from permanently inert to armed from run 2 (p53 C4).
    **Validates: Requirements 2.4**
    """
    state_table = _schema_extract_state_table(
        sample_s3_uri="s3://adi-processed-files-1-r/samples/p1/source-sample.json"
    )

    assert _uri_the_reader_was_given(state_table) == "s3://adi-processed-files-1-r/samples/p1/source-sample.json"


def test_an_empty_sample_uri_falls_back_to_json_s3_uri():
    """p53 R2.5: present-but-empty is treated as absent.

    The DynamoDB item omits the key when there is no artifact, but the
    `schema_extract` *return envelope* always carries it, as `""` when absent
    (`schema_extract:1791-1804`). A row written from that envelope by any other
    path would therefore carry `""`, so the fallback must be keyed on
    truthiness rather than on key presence.
    **Validates: Requirements 2.5**
    """
    state_table = _schema_extract_state_table(sample_s3_uri="")

    assert _uri_the_reader_was_given(state_table) == "s3://adi-source-files-123/uploads/customer_data_6.csv"


def test_a_date_format_mismatch_does_not_fail_the_step():
    """The non-blocking guarantee at the handler, asserted rather than assumed.

    `LGR-9e76249-4` (template / ratified) rejected making the sibling check
    blocking: "a false gate on code generation is worse than a missed finding."
    C1 inherits that. So the handler returns 200 and writes `status: "success"`
    with a real mismatch present in the generated transform.
    **Validates: Requirements 6.4**
    """
    state_table = MagicMock(
        get_item=MagicMock(
            return_value={
                "Item": {
                    "extracted_schema": '{"properties": {}}',
                    "json_s3_uri": "s3://adi-source-files-123/uploads/customer_data_6.csv",
                }
            }
        ),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )
    patches = _handler_patches(state_table, _OBSERVED_TRANSFORM)
    patches["read_source_samples"] = patch.object(
        _mod, "read_source_samples", return_value={"signup_date": _OBSERVED_SIGNUP_DATES}
    )

    with _nested(patches):
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )

    state_row = state_table.put_item.call_args.kwargs["Item"]

    # The mismatch really is in the transform this run generated — otherwise
    # the 200 below would prove nothing.
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}))
    assert result["statusCode"] == 200
    assert state_row["status"] == "success"
    assert state_row["output_data"]["status"] == "success"


def test_the_handler_is_inert_when_the_row_carries_no_json_s3_uri():
    """An older run with no `json_s3_uri` generates code exactly as before.

    **Validates: Requirements 6.4**
    """
    state_table = _state_table()  # no `json_s3_uri` on the Item
    s3 = MagicMock()
    patches = _handler_patches(state_table, _OBSERVED_TRANSFORM)
    patches["s3_client"] = patch.object(_mod, "s3_client", s3)

    with _nested(patches):
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )

    assert result["statusCode"] == 200
    # No object read was attempted for sampling.
    s3.get_object.assert_not_called()


# ── R6.4 (task 2.2): the record, and the ONE regeneration ──────────────────
#
# `LGR-9e76249-4` (**template / ratified**) is the governing shape and names
# both places in as many words: the finding is persisted "where approvers
# already read from (`output_data.code_contradictions` plus
# `output_data.summary.contradiction_count` on the `create_code` state row)"
# and "merged into the `regeneration_feedback` dict for a single in-process
# re-call, so it reaches attempt 1 rather than attempt 3". p49 mirrors both
# under its own keys and adds nothing blocking — the non-blocking half is
# asserted by `test_a_date_format_mismatch_does_not_fail_the_step` above,
# which now runs THROUGH the regeneration because the trigger includes date
# findings.

# The corrected transform: the same parse with the format the sampled values
# are actually in. Used as the regeneration's output, so "the record describes
# the code that was stored" has something to distinguish it from the first
# attempt's.
_CORRECTED_DATE_TRANSFORM = (
    'df_transformed = df.withColumn("registration_date", to_date(col("signup_date"), "MM/dd/yyyy"))'
)

# Both finding kinds from one generation: `phone` is assigned a constant null
# while carrying a Completeness rule (p37's contradiction), and `signup_date`
# is parsed with a format none of its values match (p49's mismatch).
_BOTH_KINDS_TRANSFORM = (
    'df_transformed = df.withColumn("phone", lit(None))'
    '.withColumn("registration_date", to_date(col("signup_date"), "yyyy-MM-dd"))'
)


def _date_state_table():
    """A `schema_extract` row carrying `json_s3_uri`, so the reader is bound.

    `properties: {}` leaves `_validate_mapped_source_fields` inert
    (`LGR-a419843-2` records that it must be, for an empty declared set), so
    these tests exercise the date wiring rather than the mapping refusal.
    """
    return MagicMock(
        get_item=MagicMock(
            return_value={
                "Item": {
                    "extracted_schema": '{"properties": {}}',
                    "json_s3_uri": "s3://adi-source-files-123/uploads/customer_data_6.csv",
                }
            }
        ),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )


def _run_date_handler(transform_block, samples=None):
    """Run the handler over `transform_block` with the observed samples injected.

    `transform_block` may be a list, in which case it is the per-call sequence
    the AI generator returns — attempt 1 then the regeneration.
    """
    state_table = _date_state_table()
    patches = _handler_patches(state_table, None)
    if isinstance(transform_block, list):
        patches["generate_transform_with_ai"] = patch.object(
            _mod, "generate_transform_with_ai", side_effect=transform_block
        )
    else:
        patches["generate_transform_with_ai"] = patch.object(
            _mod, "generate_transform_with_ai", return_value=transform_block
        )
    patches["read_source_samples"] = patch.object(
        _mod,
        "read_source_samples",
        return_value=samples if samples is not None else {"signup_date": _OBSERVED_SIGNUP_DATES},
    )

    with _nested(patches) as mocks:
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": "map-c8"},
            None,
        )
        generate_transform = mocks["generate_transform_with_ai"]

    return result, state_table.put_item.call_args.kwargs["Item"], generate_transform


def test_the_date_finding_lands_on_output_data_and_increments_the_summary_count():
    """R6.4, place 1: the named finding on the row approvers already read.

    `output_data.date_format_findings` carries the full
    `{column, function, format, samples, reason}` record and
    `summary.date_format_finding_count` carries its length. Recording it is
    what makes "non-blocking" mean something rather than nothing
    (`LGR-9e76249-4`).
    **Validates: Requirements 6.4**
    """
    result, state_row, _ = _run_date_handler(_OBSERVED_TRANSFORM)

    assert result["statusCode"] == 200
    output_data = state_row["output_data"]
    findings = output_data["date_format_findings"]
    assert len(findings) == 1
    assert findings[0]["column"] == "signup_date"
    assert findings[0]["format"] == "yyyy-MM-dd"
    assert set(findings[0]) == {"column", "function", "format", "samples", "reason"}
    assert output_data["summary"]["date_format_finding_count"] == len(findings) == 1


def test_the_date_findings_stay_separate_from_the_contradictions():
    """The two kinds are recorded under their own keys and their own counts.

    They say different things — one that a rule can never pass, the other that
    a parse will null a column — and they carry different field sets, so a
    merged list would be one no consumer could read without discriminating on
    shape. This is the assertion that would fail if a later change "tidied"
    them into one list.
    **Validates: Requirements 6.4**
    """
    result, state_row, _ = _run_date_handler(_BOTH_KINDS_TRANSFORM)

    assert result["statusCode"] == 200
    output_data = state_row["output_data"]

    # Non-vacuity: BOTH kinds really were produced by this one generation.
    assert [f["column"] for f in output_data["code_contradictions"]] == ["phone"]
    assert [f["column"] for f in output_data["date_format_findings"]] == ["signup_date"]

    assert output_data["summary"]["contradiction_count"] == 1
    assert output_data["summary"]["date_format_finding_count"] == 1
    # Neither list absorbed the other.
    assert set(output_data["code_contradictions"][0]) == {"column", "rule", "reason"}
    assert "function" in output_data["date_format_findings"][0]


def test_a_clean_run_records_a_zero_date_count_and_an_empty_list():
    """0 is a real value: "checked, no mismatch", not an absent key.

    Without this, a handler that dropped the key whenever there was nothing to
    say would pass the two assertions above.
    **Validates: Requirements 6.4**
    """
    result, state_row, _ = _run_date_handler(_CORRECTED_DATE_TRANSFORM)

    assert result["statusCode"] == 200
    assert state_row["output_data"]["date_format_findings"] == []
    assert state_row["output_data"]["summary"]["date_format_finding_count"] == 0


def test_a_run_with_no_samples_records_zero_rather_than_omitting_the_key():
    """An inert check still records that it ran and found nothing.

    A JDBC pipeline, an older run with no `json_s3_uri`, or a failed sample
    read all land here, and all of them must produce the same honest 0 as a
    clean check — the reason the out list is initialised for both generation
    paths.
    **Validates: Requirements 6.4**
    """
    result, state_row, _ = _run_date_handler(_OBSERVED_TRANSFORM, samples={})

    assert result["statusCode"] == 200
    assert state_row["output_data"]["date_format_findings"] == []
    assert state_row["output_data"]["summary"]["date_format_finding_count"] == 0


def test_a_date_mismatch_triggers_one_regeneration_carrying_the_finding():
    """R6.4, place 2: ONE in-process re-call, with the finding in the prompt.

    Exactly one: `generate_transform_with_ai` is called twice, never three
    times. The second call's `issues_found` is the channel
    `_build_feedback_section` renders (P28 R13.3), which is what makes the
    regeneration prompt differ from the original instead of being
    byte-identical.

    **What the feedback names.** The emitted format and the sampled values —
    the evidence. NOT a derived source layout: `05/12/2024` is valid under
    MM/dd/yyyy and dd/MM/yyyy alike, so naming one would be the silent pick
    do-NOT 8 / R5.1 forbid, and surfacing that ambiguity is C3's job. The
    last assertion pins that the feedback stops where the finding stops.
    **Validates: Requirements 6.4**
    """
    _, _, generate_transform = _run_date_handler([_OBSERVED_TRANSFORM, _CORRECTED_DATE_TRANSFORM])

    assert generate_transform.call_count == 2
    first_feedback = generate_transform.call_args_list[0].kwargs["regeneration_feedback"]
    retry_feedback = generate_transform.call_args_list[1].kwargs["regeneration_feedback"]

    assert not first_feedback
    issues = retry_feedback["issues_found"]
    assert len(issues) == 1
    assert "signup_date" in issues[0]
    assert "yyyy-MM-dd" in issues[0]
    assert "01/15/2024" in issues[0]
    assert "MM/dd/yyyy" not in issues[0]


def test_the_recorded_findings_describe_the_transform_that_was_stored():
    """The re-check's findings replace the first attempt's, not add to them.

    A finding about a transform block that was thrown away describes code
    nobody will run. So a run whose regeneration fixed the format records
    zero — the mismatch is in the logs and the attempt-1 prompt, not on the
    row an approver reads about the code that shipped.
    **Validates: Requirements 6.4**
    """
    result, state_row, generate_transform = _run_date_handler([_OBSERVED_TRANSFORM, _CORRECTED_DATE_TRANSFORM])

    # Non-vacuity: attempt 1 really did mismatch, which is why there was a
    # regeneration at all.
    assert generate_transform.call_count == 2
    assert _ca.detect_date_format_mismatches(_OBSERVED_TRANSFORM, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}))

    assert result["statusCode"] == 200
    assert state_row["output_data"]["date_format_findings"] == []
    assert state_row["output_data"]["summary"]["date_format_finding_count"] == 0


def test_a_date_finding_alone_is_enough_to_trigger_the_regeneration():
    """The date term in the trigger is load-bearing, and is asserted as such.

    Neither anti-patterns nor contradictions are present in this run — only a
    date-format mismatch — so a trigger reading
    `if antipattern_issues or code_contradictions:` would record the finding
    and never act on it.
    **Validates: Requirements 6.4**
    """
    _, state_row, generate_transform = _run_date_handler(_OBSERVED_TRANSFORM)

    assert state_row["output_data"]["code_contradictions"] == []
    assert generate_transform.call_count == 2


# ── R6.4: the merge helper, as a pure function ──────────────────────────────


def test_feedback_appends_a_date_finding_reason_to_issues_found():
    """A date finding travels the same `issues_found` channel as everything else.

    **Validates: Requirements 6.4**
    """
    merged = _mod._feedback_with_contradictions(
        None,
        [],
        [{"column": "signup_date", "format": "yyyy-MM-dd", "reason": "the format matches no sampled value"}],
    )

    assert merged == {"issues_found": ["the format matches no sampled value"]}


def test_feedback_appends_contradictions_before_date_findings():
    """Order is fixed so the contradiction-only merge is byte-identical to p37's.

    Both kinds in one dict, contradictions first, reviewer issues still ahead
    of both.
    **Validates: Requirements 6.4**
    """
    merged = _mod._feedback_with_contradictions(
        {"issues_found": ["reviewer said this"]},
        [{"column": "phone", "rule": 'Completeness "phone"', "reason": "phone is constant"}],
        [{"column": "signup_date", "format": "yyyy-MM-dd", "reason": "signup_date will not parse"}],
    )

    assert merged["issues_found"] == ["reviewer said this", "phone is constant", "signup_date will not parse"]


def test_feedback_never_mutates_the_callers_dict_for_a_date_finding():
    """The same dict is read again on the fallback path, so it must survive.

    The p37 assertion one section up covers the contradiction argument; this
    one covers the new argument, because the guard is a single `if` that a
    change to either could bypass.
    **Validates: Requirements 6.4**
    """
    original = {"issues_found": ["reviewer said this"]}
    snapshot = {"issues_found": ["reviewer said this"]}

    merged = _mod._feedback_with_contradictions(original, [], [{"column": "d", "reason": "because"}])

    assert original == snapshot
    assert merged is not original
    assert merged["issues_found"] is not original["issues_found"]


def test_feedback_returns_its_input_unchanged_when_neither_kind_is_present():
    """Identity, not equality — the arm that keeps a prompt byte-identical.

    A regeneration driven by anti-patterns alone must still see the exact dict
    it would have seen before p37 and before p49.
    **Validates: Requirements 6.4**
    """
    original = {"reviewer_issues": ["something the reviewer said"]}

    assert _mod._feedback_with_contradictions(original, [], []) is original
    assert _mod._feedback_with_contradictions(original, [], None) is original
    assert _mod._feedback_with_contradictions(None, [], []) is None


# ════════════════════════════════════════════════════════════════════════════
# p49 task 5.1 — the ambiguity arm (design C3, requirements R5.1/R5.2/R5.3).
#
# The same AST pass, the same sample read, a different question about the same
# node: not "is the emitted format refuted" but "do the samples SETTLE it". The
# mismatch arm above answers the first; a format it leaves alone is not
# therefore correct, and treating "not refuted" as "settled" is the silent pick
# do-NOT 8 and R5.1 forbid.
#
# **This arm RECORDS and does not force a human**, per design O3: ambiguity
# frequency is unmeasured, `05/12/2024`-shaped values are common, and a forcing
# clause resting on an unmeasured frequency risks approval fatigue rather than
# safety. `LGR-9e76249-7`'s forcing clause is cited by C3 as the *precedent*
# that a mapping condition can require a human — precedent, not instruction.
# The tests that pin the non-forcing choice are
# `test_an_ambiguity_alone_does_not_trigger_a_regeneration` and
# `test_an_ambiguity_does_not_fail_the_step`; the follow-up is backlog item 21.
# ════════════════════════════════════════════════════════════════════════════

# `05/12/2024` is the observed run's FIFTH value and the one R5.1 names: valid
# under MM/dd/yyyy (12 May) and dd/MM/yyyy (5 December) alike.
_AMBIGUOUS_VALUE = "05/12/2024"

# The corrected format over an ambiguous-only sample. Attempt 1's `yyyy-MM-dd`
# is refuted by this value, so a regeneration to `MM/dd/yyyy` fixes the
# mismatch and lands on an assumption — which is what makes it possible for the
# stored row to carry an ambiguity the first attempt never had.
_AMBIGUOUS_TRANSFORM = 'df_transformed = df.withColumn("registration_date", to_date(col("signup_date"), "MM/dd/yyyy"))'


def test_an_ambiguous_only_sample_yields_an_ambiguity_not_a_settled_format():
    """R5.1: `05/12/2024` alone cannot settle MM/dd/yyyy against dd/MM/yyyy.

    Non-vacuity first: both readings really do parse the value, and they
    disagree about which date it is — 12 May against 5 December. That
    disagreement is the whole content of the finding, and without it there
    would be nothing for an approver to act on.

    **Validates: Requirements 5.1, 5.2, 5.3**
    """
    assert _ca._value_parses(_AMBIGUOUS_VALUE, "%m/%d/%Y")
    assert _ca._value_parses(_AMBIGUOUS_VALUE, "%d/%m/%Y")
    assert _ca._parsed_datetime(_AMBIGUOUS_VALUE, "%m/%d/%Y") != _ca._parsed_datetime(_AMBIGUOUS_VALUE, "%d/%m/%Y")

    ambiguities: list[dict[str, str]] = []
    findings = _ca.detect_date_format_mismatches(
        _AMBIGUOUS_TRANSFORM, _reader({"signup_date": [_AMBIGUOUS_VALUE]}), ambiguities
    )

    # Not a mismatch — the format is not refuted. The two outcomes are mutually
    # exclusive per node and this is the case that shows why both are needed.
    assert findings == []
    assert len(ambiguities) == 1
    assert ambiguities[0]["column"] == "signup_date"
    assert ambiguities[0]["function"] == "to_date"
    assert ambiguities[0]["assumed_format"] == "MM/dd/yyyy"
    assert ambiguities[0]["alternative_format"] == "dd/MM/yyyy"
    assert set(ambiguities[0]) == {
        "column",
        "function",
        "assumed_format",
        "alternative_format",
        "samples",
        "reason",
    }


def test_the_ambiguity_names_both_readings_and_marks_the_assumed_one():
    """do-NOT 8 at the text an approver actually reads.

    The record must name the ambiguity AND the format that was assumed. A
    finding that named only one format would be a silent pick with extra steps,
    and a finding that named two without saying which the code took would leave
    the approver unable to check the generated script against it.

    R5.2 is why the wording carries the reason as well: a wrong date passes
    every rule in place, while the observed null was caught by a Completeness
    rule — so this finding has to justify its own existence to a reader who has
    just seen a green run.

    **Validates: Requirements 5.1, 5.2**
    """
    ambiguities: list[dict[str, str]] = []
    _ca.detect_date_format_mismatches(_AMBIGUOUS_TRANSFORM, _reader({"signup_date": [_AMBIGUOUS_VALUE]}), ambiguities)

    reason = ambiguities[0]["reason"]

    assert "MM/dd/yyyy" in reason
    assert "dd/MM/yyyy" in reason
    assert "ASSUMPTION" in reason
    # The divergent readings are shown, not merely alleged.
    assert "2024-05-12" in reason
    assert "2024-12-05" in reason
    # And the value they disagree about.
    assert _AMBIGUOUS_VALUE in reason
    assert ambiguities[0]["samples"] == _AMBIGUOUS_VALUE


def test_the_observed_runs_full_sample_settles_mm_dd_yyyy():
    """The observed sample DOES settle the format, and that is the point.

    `01/15/2024` cannot be read as dd/MM/yyyy — there is no month 15 — so the
    exchange is refuted by the data and MM/dd/yyyy is the only reading left.
    Recording an ambiguity here would be crying wolf on the very sample the
    spec was written from, which is design O3's approval-fatigue failure.

    **Validates: Requirements 5.1**
    """
    ambiguities: list[dict[str, str]] = []
    findings = _ca.detect_date_format_mismatches(
        _AMBIGUOUS_TRANSFORM, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}), ambiguities
    )

    # Non-vacuity: the refuting value is in the sample and really is unreadable
    # the other way round.
    assert "01/15/2024" in _OBSERVED_SIGNUP_DATES
    assert not _ca._value_parses("01/15/2024", "%d/%m/%Y")

    assert findings == []
    assert ambiguities == []


def test_an_iso_sample_can_be_ambiguous_too():
    """The family is day/month ORDER, not slash-separated formats.

    `2024-01-05` is a legal `yyyy-MM-dd` and a legal `yyyy-dd-MM`, and the two
    disagree. ISO-looking data is not self-evidently unambiguous, which is why
    the check reasons about the pattern rather than about its separators.

    **Validates: Requirements 5.1**
    """
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "yyyy-MM-dd"))'
    ambiguities: list[dict[str, str]] = []

    findings = _ca.detect_date_format_mismatches(transform, _reader({"signup_date": ["2024-01-05"]}), ambiguities)

    assert findings == []
    assert len(ambiguities) == 1
    assert ambiguities[0]["assumed_format"] == "yyyy-MM-dd"
    assert ambiguities[0]["alternative_format"] == "yyyy-dd-MM"


def test_an_iso_sample_with_a_day_above_twelve_is_settled():
    """The other half of the pair above, so the check is not simply permissive."""
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "yyyy-MM-dd"))'
    ambiguities: list[dict[str, str]] = []

    findings = _ca.detect_date_format_mismatches(transform, _reader({"signup_date": ["2024-01-15"]}), ambiguities)

    assert findings == []
    assert ambiguities == []


def test_a_mismatch_is_never_also_an_ambiguity():
    """The observed defect records a mismatch and nothing else.

    A format that parses none of the values is refuted, not unsettled, and an
    approver reading two findings about one column would have to work out which
    one to believe. Mutual exclusion is structural — the ambiguity arm runs in
    the branch that already declined to record a mismatch — and asserted here
    on the spec's own regression input.

    **Validates: Requirements 5.1**
    """
    ambiguities: list[dict[str, str]] = []
    findings = _ca.detect_date_format_mismatches(
        _OBSERVED_TRANSFORM, _reader({"signup_date": _OBSERVED_SIGNUP_DATES}), ambiguities
    )

    assert len(findings) == 1
    assert ambiguities == []


def test_an_ambiguity_that_cannot_change_a_date_is_not_recorded():
    """`2024-01-01` reads the same either way round, so nothing is at stake.

    The third condition in `_format_ambiguity`, and the one that answers design
    O3's fatigue concern with a measurement rather than an argument: an
    ambiguity that cannot alter a loaded value is not worth an approver's
    attention, so it is not recorded.

    **Validates: Requirements 5.1**
    """
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "yyyy-MM-dd"))'
    ambiguities: list[dict[str, str]] = []

    findings = _ca.detect_date_format_mismatches(
        transform, _reader({"signup_date": ["2024-01-01", "2024-07-07"]}), ambiguities
    )

    # Non-vacuity: the exchange DOES parse both values — it is only the
    # resolved dates that are identical.
    assert all(_ca._value_parses(value, "%Y-%d-%m") for value in ["2024-01-01", "2024-07-07"])
    assert findings == []
    assert ambiguities == []


def test_a_month_name_format_is_never_ambiguous():
    """`15 Jan 2024` has no second reading, so there is nothing to settle."""
    transform = 'df_transformed = df.withColumn("d", to_date(col("signup_date"), "dd MMM yyyy"))'
    ambiguities: list[dict[str, str]] = []

    findings = _ca.detect_date_format_mismatches(transform, _reader({"signup_date": ["05 Dec 2024"]}), ambiguities)

    assert findings == []
    assert ambiguities == []
    assert _ca._day_month_swapped("dd MMM yyyy") is None


def test_a_quoted_literal_format_is_declined_rather_than_exchanged():
    """A literal can carry `M` or `d`, and a textual exchange would corrupt it.

    Declining costs nothing: a quoted literal is a timestamp separator, not
    part of a day/month ordering question. Inert, per design §Error Handling's
    standing rule that an unsettleable input is never an error.

    **Validates: Requirements 6.4**
    """
    transform = 'df_transformed = df.withColumn("d", to_timestamp(col("ts"), "yyyy-MM-dd\'T\'HH:mm:ss"))'
    ambiguities: list[dict[str, str]] = []

    findings = _ca.detect_date_format_mismatches(transform, _reader({"ts": ["2024-01-05T10:30:00"]}), ambiguities)

    assert findings == []
    assert ambiguities == []
    assert _ca._day_month_swapped("yyyy-MM-dd'T'HH:mm:ss") is None


def test_a_timestamp_format_without_quotes_is_still_checked():
    """The decline above is about quotes, not about `to_timestamp`.

    Otherwise the previous test would be indistinguishable from the arm
    silently not covering timestamps at all (design O4).
    """
    transform = 'df_transformed = df.withColumn("d", to_timestamp(col("ts"), "MM/dd/yyyy HH:mm:ss"))'
    ambiguities: list[dict[str, str]] = []

    findings = _ca.detect_date_format_mismatches(transform, _reader({"ts": ["05/12/2024 10:30:00"]}), ambiguities)

    assert findings == []
    assert len(ambiguities) == 1
    assert ambiguities[0]["function"] == "to_timestamp"
    assert ambiguities[0]["alternative_format"] == "dd/MM/yyyy HH:mm:ss"
    # The rendered readings carry the time, since the parse does.
    assert "2024-05-12 10:30:00" in ambiguities[0]["reason"]


def test_omitting_the_ambiguity_out_list_records_nothing():
    """Opt-in, exactly like the two out lists before it.

    A caller that wants only mismatches passes only that list and pays nothing
    for C3 — the same property that makes the whole check free for callers that
    want none of it.
    """
    findings = _ca.detect_date_format_mismatches(_AMBIGUOUS_TRANSFORM, _reader({"signup_date": [_AMBIGUOUS_VALUE]}))

    assert findings == []


def test_the_ambiguity_is_produced_at_code_assembly_through_the_shim():
    """C3 rides C1's seam: one scan, one read, both questions.

    Driven through `lambda_function.generate_code` for the `LGR-b2f79ef-10`
    reason the sibling tests give — the shim is the only seam the tests patch.
    The reader is asserted to be called ONCE, because a second public entry
    point for the ambiguity would have doubled the S3 request the callable form
    exists to make conditional.

    **Validates: Requirements 5.1, 5.3**
    """
    reads = []

    def _counting_reader():
        reads.append(1)
        return {"signup_date": [_AMBIGUOUS_VALUE]}

    findings: list[dict[str, str]] = []
    ambiguities: list[dict[str, str]] = []
    with patch.object(_mod, "generate_transform_with_ai", return_value=_AMBIGUOUS_TRANSFORM):
        rendered = _mod.generate_code(
            _pipeline_metadata(),
            _SOURCED_PHONE_MAPPING,
            sample_reader=_counting_reader,
            date_format_findings=findings,
            date_format_ambiguities=ambiguities,
        )

    assert 'to_date(col("signup_date"), "MM/dd/yyyy")' in rendered
    assert findings == []
    assert [a["assumed_format"] for a in ambiguities] == ["MM/dd/yyyy"]
    assert reads == [1]


def test_the_ambiguity_out_list_works_without_the_mismatch_list():
    """Either out list alone runs the check; neither means it is skipped.

    Without this, `date_format_ambiguities` would be silently inert for any
    caller that did not also ask for mismatches — a parameter that reads as
    wired and is not.
    """
    ambiguities: list[dict[str, str]] = []
    with patch.object(_mod, "generate_transform_with_ai", return_value=_AMBIGUOUS_TRANSFORM):
        _mod.generate_code(
            _pipeline_metadata(),
            _SOURCED_PHONE_MAPPING,
            sample_reader=_reader({"signup_date": [_AMBIGUOUS_VALUE]}),
            date_format_ambiguities=ambiguities,
        )

    assert len(ambiguities) == 1


# ── R5.3 at the handler: recorded where a human reads, and NOT gated ────────


def test_the_ambiguity_lands_on_output_data_and_increments_the_summary_count():
    """R5.3, the record: the `create_code` state row approvers already read.

    `output_data.date_format_ambiguities` carries the full
    `{column, function, assumed_format, alternative_format, samples, reason}`
    entry and `summary.date_format_ambiguity_count` carries its length — the
    two-place shape `LGR-9e76249-4` names, under its own key and its own count.

    **Validates: Requirements 5.1, 5.3**
    """
    result, state_row, _ = _run_date_handler(_AMBIGUOUS_TRANSFORM, samples={"signup_date": [_AMBIGUOUS_VALUE]})

    assert result["statusCode"] == 200
    output_data = state_row["output_data"]
    ambiguities = output_data["date_format_ambiguities"]
    assert len(ambiguities) == 1
    assert ambiguities[0]["column"] == "signup_date"
    assert ambiguities[0]["assumed_format"] == "MM/dd/yyyy"
    assert ambiguities[0]["alternative_format"] == "dd/MM/yyyy"
    assert output_data["summary"]["date_format_ambiguity_count"] == 1


def test_the_three_finding_kinds_stay_separate_on_the_row():
    """Three keys, three counts, no summing.

    "A rule can never pass", "the format matches nothing" and "the samples do
    not settle the format" are three different facts about three different
    runs, and an approver reading a single merged number could not tell which
    happened. This is the assertion that would fail if a later change tidied
    them together.

    **Validates: Requirements 5.3**
    """
    result, state_row, _ = _run_date_handler(_AMBIGUOUS_TRANSFORM, samples={"signup_date": [_AMBIGUOUS_VALUE]})

    assert result["statusCode"] == 200
    output_data = state_row["output_data"]
    summary = output_data["summary"]

    assert output_data["date_format_findings"] == []
    assert output_data["code_contradictions"] == []
    assert len(output_data["date_format_ambiguities"]) == 1
    assert summary["contradiction_count"] == 0
    assert summary["date_format_finding_count"] == 0
    assert summary["date_format_ambiguity_count"] == 1
    # The field sets really are different, which is why one list could not
    # carry all three.
    assert "assumed_format" in output_data["date_format_ambiguities"][0]


def test_a_settled_run_records_a_zero_ambiguity_count():
    """0 is a real value: "checked, and the samples settle it".

    Without this, a handler that dropped the key whenever there was nothing to
    say would pass the two assertions above.

    **Validates: Requirements 5.3**
    """
    result, state_row, _ = _run_date_handler(_CORRECTED_DATE_TRANSFORM)

    assert result["statusCode"] == 200
    assert state_row["output_data"]["date_format_ambiguities"] == []
    assert state_row["output_data"]["summary"]["date_format_ambiguity_count"] == 0


def test_an_ambiguity_alone_does_not_trigger_a_regeneration():
    """The RECORD arm, pinned. Design O3, and the reason it is not a gate.

    A regeneration cannot resolve an ambiguity: the model would be handed the
    same two readings the checker has and asked to choose between them on no
    more evidence, which is the silent pick do-NOT 8 / R5.1 forbid — bought
    with a Bedrock call. So `date_format_ambiguities` is deliberately NOT a
    fourth term in the regeneration trigger, and `generate_transform_with_ai`
    is called exactly once.

    If a later change adds a forcing clause or a fourth trigger term, this test
    is the one that should make it deliberate rather than incidental. Design O3
    and backlog item 21 say what would justify it: a measurement of how often
    this fires, which nobody has.

    **Validates: Requirements 5.3**
    """
    _, state_row, generate_transform = _run_date_handler(
        _AMBIGUOUS_TRANSFORM, samples={"signup_date": [_AMBIGUOUS_VALUE]}
    )

    # Non-vacuity: an ambiguity really was recorded on this run, so the single
    # call below is a decision and not an absence.
    assert len(state_row["output_data"]["date_format_ambiguities"]) == 1
    assert state_row["output_data"]["date_format_findings"] == []
    assert state_row["output_data"]["code_contradictions"] == []
    assert generate_transform.call_count == 1


def test_an_ambiguity_does_not_fail_the_step():
    """`LGR-9e76249-4` inherited by the third arm as well as the second.

    "A false gate on code generation is worse than a missed finding." An
    unresolved assumption is recorded and the run proceeds; the containment
    chain downstream is unchanged.

    **Validates: Requirements 5.3, 6.4**
    """
    result, state_row, _ = _run_date_handler(_AMBIGUOUS_TRANSFORM, samples={"signup_date": [_AMBIGUOUS_VALUE]})

    assert len(state_row["output_data"]["date_format_ambiguities"]) == 1
    assert result["statusCode"] == 200
    assert state_row["status"] == "success"
    assert state_row["output_data"]["status"] == "success"


def test_the_recorded_ambiguity_describes_the_transform_that_was_stored():
    """A regeneration that fixes a mismatch can INTRODUCE an ambiguity.

    Attempt 1 emits `yyyy-MM-dd` over `05/12/2024` — refuted, so a mismatch and
    one regeneration. Attempt 2 emits `MM/dd/yyyy`, which the value accepts but
    does not settle. The row must describe attempt 2: its ambiguity is the one
    an approver needs, and attempt 1's absence of one describes code nobody
    will run.

    This is why the ambiguity carrier is replaced after the re-call rather than
    left as the first attempt's, and it is load-bearing in the direction a
    reader would not guess — the carrier goes from empty to non-empty.

    **Validates: Requirements 5.3**
    """
    result, state_row, generate_transform = _run_date_handler(
        [_OBSERVED_TRANSFORM, _AMBIGUOUS_TRANSFORM], samples={"signup_date": [_AMBIGUOUS_VALUE]}
    )

    # Non-vacuity: attempt 1 mismatched (which is why there was a regeneration)
    # and carried no ambiguity of its own.
    assert generate_transform.call_count == 2
    first_pass: list[dict[str, str]] = []
    assert _ca.detect_date_format_mismatches(
        _OBSERVED_TRANSFORM, _reader({"signup_date": [_AMBIGUOUS_VALUE]}), first_pass
    )
    assert first_pass == []

    assert result["statusCode"] == 200
    output_data = state_row["output_data"]
    assert output_data["date_format_findings"] == []
    assert output_data["summary"]["date_format_finding_count"] == 0
    assert len(output_data["date_format_ambiguities"]) == 1
    assert output_data["summary"]["date_format_ambiguity_count"] == 1


def test_a_run_with_no_samples_records_a_zero_ambiguity_count():
    """An inert check still records that it ran and found nothing.

    A JDBC pipeline, an older run with no `json_s3_uri`, or a failed sample read
    all land here, and all of them produce the same honest 0 as a settled
    check.

    **Validates: Requirements 5.3, 6.4**
    """
    result, state_row, _ = _run_date_handler(_AMBIGUOUS_TRANSFORM, samples={})

    assert result["statusCode"] == 200
    assert state_row["output_data"]["date_format_ambiguities"] == []
    assert state_row["output_data"]["summary"]["date_format_ambiguity_count"] == 0


def test_the_ambiguity_is_not_merged_into_the_regeneration_feedback():
    """The merge helper is untouched by C3, and that is deliberate.

    `_feedback_with_contradictions` takes two finding kinds and not three. An
    ambiguity in the prompt would be an instruction to choose, which is the one
    thing the model must not do here — it has no evidence the checker lacked.

    **Validates: Requirements 5.1**
    """
    import inspect as _inspect

    parameters = _inspect.signature(_mod._feedback_with_contradictions).parameters

    assert list(parameters) == ["regeneration_feedback", "contradictions", "date_format_findings"]
    # And the prompt-facing merge of a mismatch still names no source layout,
    # for the same reason: `05/12/2024` is valid both ways round.
    merged = _mod._feedback_with_contradictions(
        None, [], [{"column": "signup_date", "reason": 'NONE of the sampled values match "yyyy-MM-dd"'}]
    )
    assert merged["issues_found"] == ['NONE of the sampled values match "yyyy-MM-dd"']
