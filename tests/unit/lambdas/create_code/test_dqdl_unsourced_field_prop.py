# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 2 — no rule survives for a field with no source, and the two call
sites agree.

`_generate_dqdl_rules` (`lambda/agentic_data_ingestion_create_code/code_assembly.py:134`)
must emit neither a `Completeness` nor a `Uniqueness` rule for an
Unsourced_Target_Field — a mapping entry carrying a `target_field` and no usable
`source_field`. Such a rule can only be satisfied by a constant, and the AI
transform sets the column to `lit(None)` because it was told to produce the
column, so `Completeness >= 0.95` becomes a rule the same code generation makes
impossible to satisfy: every row fails it and the inline DQ split quarantines the
whole batch. Observed on pipeline `newtest-ab2a08a0`, run
`run-2026-08-09-15-38-19-5c5f5f8a` — target field `phone` had no source, 10/11
rules passed, 5 rows quarantined, 0 rows loaded, and the run reported SUCCEEDED.

**"Usable" is an explicit str/strip test, not truthiness.** A mapping record is
parsed JSON, so `source_field` can be `None`, `""`, `"   "`, a number, a bool, a
list, a dict, or absent entirely. The generator draws all of those.

**The second half is what makes do-NOT 8 mechanical.** `_generate_dqdl_rules` has
exactly two real call sites — the render path in `code_assembly.generate_code`
(the `template_vars["dqdl_rules"]` assignment) and the approval-payload path at
`create_code/lambda_function.py:622` — and requirement 2.6 is the claim that they
agree. A guard added at one of them satisfies requirement 2.1 at that site and
breaks 2.6, which is why the skip belongs in the function. So this property
executes **both call sites' own source**, not `_generate_dqdl_rules` twice:

- the render path is reached through `lambda_function.generate_code`, and the
  ruleset is read back out of the **rendered job** (`dq_ruleset = \"\"\"…\"\"\"`,
  `pyspark_templates/glue_job_s3_tables.py.j2:167`) — i.e. the ruleset the Glue
  job actually enforces, not a value handed back by a stub;
- the approval-payload path is located in `lambda_function`'s source by `ast`
  (the single `generated_dqdl_rules = <call>` assignment) and that expression is
  evaluated verbatim against the module's own globals.

A filter comprehension, an `if`, or any other guard inserted at either site is
therefore *executed* by this property rather than reviewed for.

**Two deliberate constraints on the generated input space, both so a red is a
real finding rather than a malformed record:**

1. `target_field` names satisfy the Identifier_Allowlist
   (`shared/identifier_utils.py:30`, `^[A-Za-z_][A-Za-z0-9_]*$`). Hostile names
   are task 8.3's business: `validate_identifier` at `code_assembly.py:170` is
   P28 R12.1's injection sink and must still raise for an unsourced field, which
   is an example assertion, not a property.
2. `target_field` names are unique within a mapping list. A record carrying two
   entries for one target field is malformed, and it would make the absence
   assertion unanswerable — one entry sourced and one not means the name
   legitimately appears in a rule.

Non-vacuity: every **sourced** target field is asserted to keep its Completeness
rule, so a `_generate_dqdl_rules` that emitted nothing at all would fail.

Nothing in this file changes production code.

**Validates: Requirements 2.1, 2.2, 2.6**
"""

import ast
import inspect
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
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

# Stub heavy dependencies so the module imports cleanly in unit context
# (the pattern in test_dqdl_rule_inference.py / test_dq_quarantine_render.py).
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

# sys.modules.pop + sys.path.insert: `lambda_function` is a name 23 Lambda dirs
# share, so it cross-contaminates between test files (LGR-b7d5490-1, backfilled).
with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

# shared/, reachable after the sys.path insert above. This is the repo's own
# rule counter, which is what the Quality_Verdict's `dqdl_rule_count` is
# computed from (`shared/quality_verdict.py:108`) — requirement 2.6 is stated in
# terms of it, so it is not re-implemented here.
from quality_verdict import count_dqdl_rules


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


# --- Call site 1: the render path, read back out of the rendered job ----------

# `pyspark_templates/glue_job_s3_tables.py.j2:167` embeds the ruleset verbatim as
# `dq_ruleset = """{{ dqdl_rules }}"""`, and the whole DQ block is gated on
# `{% if dqdl_rules is defined and dqdl_rules %}` (`:147`). So an absent block
# means an empty ruleset, which is what `_generate_dqdl_rules` returns for an
# empty mapping list.
_RULESET_OPEN = 'dq_ruleset = """'


def _ruleset_baked_into_job(rendered_code: str) -> str:
    start = rendered_code.find(_RULESET_OPEN)
    if start == -1:
        return ""
    body_start = start + len(_RULESET_OPEN)
    end = rendered_code.find('"""', body_start)
    assert end != -1, "rendered job has an unterminated dq_ruleset literal"
    return rendered_code[body_start:end]


def _render_path_dqdl(mapping_record: dict, target_config: dict) -> str:
    """The ruleset the generated Glue job enforces (`code_assembly.generate_code`)."""
    pipeline_metadata = {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": target_config,
        "source_schema": {"properties": {}},
    }
    with patch.object(_mod, "generate_transform_with_ai", return_value="df_transformed = df"):
        rendered = _mod.generate_code(pipeline_metadata=pipeline_metadata, mapping_result=mapping_record)
    return _ruleset_baked_into_job(rendered)


# --- Call site 2: the approval-payload path, located and evaluated verbatim ---


def _callee_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _enclosing_function(tree: ast.AST, lineno: int) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The innermost function containing `lineno` — the scope the hoist below must come from."""
    innermost: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.lineno <= lineno <= (node.end_lineno or node.lineno) and (
            innermost is None or node.lineno > innermost.lineno
        ):
            innermost = node
    assert innermost is not None, f"no enclosing function for line {lineno} of lambda_function"
    return innermost


def _approval_payload_call_site_expression() -> tuple[str, tuple[str, ...]]:
    """Return the source of `lambda_function`'s `generated_dqdl_rules = <call>`, plus its prelude.

    Located by `ast` rather than restated here, so the expression this property
    evaluates is the one on disk. The `""` assignment on the full-AI fallback
    path is excluded by requiring the value to be a `Call`; the structural
    assertion below is what tells us if a second call-site assignment appears.

    **The prelude, and why it is lifted rather than restated (p37 C11, task 9.1).**
    The call site used to read `_generate_dqdl_rules(_extract_field_mappings(mapping_item), …)`
    and now reads `_generate_dqdl_rules(resolved_field_mappings, …)`, because `R3.4` requires
    the mapping view to be evaluated **once** and feed both the ruleset and the
    `agent_reasoning` count. So every `<name> = _extract_field_mappings(…)` assignment in the
    same function is returned as source and executed before the call rather than reconstructed
    here. This property therefore still evaluates the caller's own resolution: if the hoist ever
    stops feeding this call site, the name goes unbound and this test fails rather than silently
    testing a copy.
    """
    source = inspect.getsource(_mod)
    tree = ast.parse(source)
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and any(isinstance(target, ast.Name) and target.id == "generated_dqdl_rules" for target in node.targets)
    ]
    assert len(call_sites) == 1, (
        f"expected exactly one `generated_dqdl_rules = <call>` assignment in lambda_function, "
        f"found {len(call_sites)}: {[ast.get_source_segment(source, n.value) for n in call_sites]}"
    )
    call_site = call_sites[0]
    scope = _enclosing_function(tree, call_site.lineno)
    prelude = tuple(
        str(ast.get_source_segment(source, node))
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Call)
        and _callee_name(node.value.func) == "_extract_field_mappings"
    )
    return str(ast.get_source_segment(source, call_site.value)), prelude


_APPROVAL_CALL_SITE_SOURCE, _APPROVAL_CALL_SITE_PRELUDE_SOURCE = _approval_payload_call_site_expression()
_APPROVAL_CALL_SITE = compile(
    f"({_APPROVAL_CALL_SITE_SOURCE})",
    "<create_code approval-payload call site>",
    "eval",
)
_APPROVAL_CALL_SITE_PRELUDE = compile(
    "\n".join(_APPROVAL_CALL_SITE_PRELUDE_SOURCE),
    "<create_code approval-payload mapping-view hoist>",
    "exec",
)


def _approval_payload_dqdl(mapping_record: dict, target_config: dict) -> str:
    """The ruleset the code approval payload advertises (`create_code/lambda_function.py:680`)."""
    # `exec`/`eval` of source segments this repo compiled from its own tree is
    # the point of the exercise: a guard inserted at that call site — or a hoist
    # that stops reaching it — is executed here rather than reviewed for.
    # Nothing user-supplied reaches this.
    namespace: dict = {"mapping_item": mapping_record, "target_config": target_config}
    exec(_APPROVAL_CALL_SITE_PRELUDE, vars(_mod), namespace)
    return eval(_APPROVAL_CALL_SITE, vars(_mod), namespace)


# --- Generators ---------------------------------------------------------------

# Allowlist-safe identifiers (`shared/identifier_utils.py:30`). Length is capped
# well under MAX_IDENTIFIER_LENGTH — hostile and oversized names are task 8.3's.
_SAFE_IDENTIFIER = st.from_regex(r"[A-Za-z_][A-Za-z0-9_]{0,14}", fullmatch=True)

# Names chosen to straddle `_UNIQUENESS_NAME_PATTERN` (`code_assembly.py:107`),
# so the Uniqueness half of the property is exercised and not merely available:
# the first six match it in full, the rest do not (including "monkey", P28
# R11.2's substring-matching bug).
_INTERESTING_TARGET_NAMES = [
    "customer_id",
    "id",
    "uuid",
    "guid",
    "email",
    "pk_customer",
    "monkey",
    "phone",
    "fname",
    "state",
    "premium_member",
]

_TARGET_NAME = st.one_of(st.sampled_from(_INTERESTING_TARGET_NAMES), _SAFE_IDENTIFIER)

# Every shape a parsed-JSON `source_field` can take that is NOT a usable source.
# The four the requirement names (`None`, `""`, `"   "`, a non-string) plus the
# container and bool cases a model response can produce.
_UNUSABLE_SOURCE_FIELDS = [None, "", "   ", "\t\n ", 0, 1, 3.5, True, False, [], {}, ["mobile"], {"name": "mobile"}]

_CONFIDENCE = st.floats(min_value=0.0, max_value=1.0, allow_nan=False, allow_infinity=False)


@st.composite
def _mapping_entry(draw: st.DrawFn) -> dict:
    """One Field_Mapping_Entry, sourced or unsourced.

    An unsourced entry either carries an unusable value or omits `source_field`
    entirely — `mapping.get("source_field")` returns `None` for the omission, so
    both routes reach the same skip and both are worth generating.
    """
    entry: dict = {"target_field": draw(_TARGET_NAME), "confidence": draw(_CONFIDENCE)}
    if draw(st.booleans()):
        entry["source_field"] = draw(_SAFE_IDENTIFIER)
    elif draw(st.booleans()):
        entry["source_field"] = draw(st.sampled_from(_UNUSABLE_SOURCE_FIELDS))
    return entry


@st.composite
def _mapping_record_and_target_config(draw: st.DrawFn) -> tuple[dict, dict, list[dict]]:
    """A mapping record in one of its three storage variants, plus a target config.

    The variant axis matters to the second half: both call sites resolve their
    mapping view through the same `_extract_field_mappings`
    (`mapping_resolution.py:42`), whose docstring says so, and the JSON-string
    variant is the one `schema_mapping` actually writes today.
    """
    entries = draw(st.lists(_mapping_entry(), min_size=1, max_size=8, unique_by=lambda entry: entry["target_field"]))

    variant = draw(st.sampled_from(["field_mappings", "mapping_config", "mapping_result_json"]))
    if variant == "field_mappings":
        mapping_record: dict = {"field_mappings": entries}
    elif variant == "mapping_config":
        mapping_record = {"mapping_config": {"field_mappings": entries}}
    else:
        mapping_record = {"mapping_result": json.dumps({"mappings": entries})}

    target_config = {
        "type": "s3_tables",
        "bucket_name": "my-table-bucket",
        "namespace": "analytics",
        "table_name": "customers",
        "write_mode": "append",
        "source_format": "json",
    }
    # A merge_key naming one of the target fields is the interesting case: it is
    # a Uniqueness *hint* that wins regardless of name shape, so an unsourced
    # field named by it is where requirement 2.2 does real work.
    merge_key = draw(st.one_of(st.none(), st.sampled_from([e["target_field"] for e in entries]), _SAFE_IDENTIFIER))
    if merge_key is not None:
        target_config["merge_key"] = merge_key

    return mapping_record, target_config, entries


def _has_usable_source(entry: dict) -> bool:
    source_field = entry.get("source_field")
    return isinstance(source_field, str) and bool(source_field.strip())


# --- Property 2 ---------------------------------------------------------------


# Feature: p37-zero-row-success, Property 2: No rule survives for a field with no source, and the two call sites agree
@settings(max_examples=150, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(case=_mapping_record_and_target_config())
def test_no_rule_survives_for_a_field_with_no_source_and_both_call_sites_agree(case):
    """R2.1/R2.2: no rule for an unsourced target field. R2.6: the sites agree."""
    mapping_record, target_config, entries = case

    baked_into_job = _render_path_dqdl(mapping_record, target_config)
    advertised_in_payload = _approval_payload_dqdl(mapping_record, target_config)

    unsourced = [e["target_field"] for e in entries if not _has_usable_source(e)]
    sourced = [e["target_field"] for e in entries if _has_usable_source(e)]

    # R2.1 / R2.2 — the ruleset the job enforces names no unsourced field.
    for target_field in unsourced:
        assert f'Completeness "{target_field}"' not in baked_into_job, (
            f"Completeness rule emitted for unsourced target field {target_field!r} (R2.1): {baked_into_job!r}"
        )
        assert f'Uniqueness "{target_field}"' not in baked_into_job, (
            f"Uniqueness rule emitted for unsourced target field {target_field!r} (R2.2): {baked_into_job!r}"
        )

    # Non-vacuity — a generator that emitted no rules at all would pass the above.
    for target_field in sourced:
        assert f'Completeness "{target_field}" >= 0.95' in baked_into_job, (
            f"Completeness rule missing for sourced target field {target_field!r}: {baked_into_job!r}"
        )

    # R2.6 — the count in the approval payload equals the count in the code that
    # was stored for the run. Counted with the repo's own counter, which is what
    # the Quality_Verdict's `dqdl_rule_count` is computed from
    # (`shared/quality_verdict.py:108`).
    assert count_dqdl_rules(baked_into_job) == count_dqdl_rules(advertised_in_payload), (
        "the two call sites of _generate_dqdl_rules disagree on rule count (R2.6, do-NOT 8): "
        f"render path {count_dqdl_rules(baked_into_job)} vs approval payload "
        f"{count_dqdl_rules(advertised_in_payload)}\nrender: {baked_into_job!r}\npayload: {advertised_in_payload!r}"
    )
    # The stronger form the call site's own comment claims at
    # `lambda_function.py:619-621`: "the approval payload shows the exact ruleset
    # the job enforces". Asserted after the count so a count divergence — the
    # requirement — is the failure a reviewer reads first.
    assert baked_into_job == advertised_in_payload, (
        f"approval payload ruleset differs from the one baked into the job:\n"
        f"render:  {baked_into_job!r}\npayload: {advertised_in_payload!r}"
    )
