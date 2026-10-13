# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the target schema reaches the transform-generation prompt.

p33 task 4.7 — the load-bearing change of the spec. Everything else in p33
makes the measurement honest; this is what gives the generator new information,
and therefore what makes a regenerated script *able* to differ.

The evidence that it was needed: p32 proved the regeneration loop's mechanics
work — three ``CreateCode`` attempts, ``regeneration_attempt`` advancing 0→1→2
against a bound of 2, reviewer feedback stored and read back — and still
produced three byte-identical scripts at sha256
``e110e570ceafcafd040e4b9b43825a962ab39042f96c7511390df93d8579a047``. The
generator's only information about target field names was the field mappings,
and those do not change between attempts, so a deterministic model returned the
same answer. Requirement 2.5 closes that.

WHAT IS PINNED HERE, AND AT WHICH LAYER
---------------------------------------
The value crosses four layers, and a test at only the template layer would pass
while the plumbing was broken. So each layer has its own assertion:

1. ``shared/prompt_templates/transform_generation.j2`` — the render itself.
2. ``transform_generation.generate_transform_with_ai`` — the render context.
3. ``lambda_function.generate_transform_with_ai`` / ``generate_code`` (the two
   shims) — forwarded, not shortcut past. The shims exist so
   ``patch.object(lambda_function, "Agent", ...)`` and patches on
   ``generate_transform_with_ai`` keep intercepting; a change that reached the
   template by bypassing one of them would look correct here and break the ten-
   plus test files that reach the prompt builder through them (enumerated in
   p33 task 3.3).
4. ``lambda_handler``'s ``pm`` dict — where the schema the resolver produced is
   actually put.

Layers 1-3 are covered end to end by ``_prompt_through_the_whole_chain``, which
calls the outermost shim (``_mod.generate_code``) with only the AI boundary
stubbed. Layer 4 has its own handler test.

THE ASYMMETRY WITH ``source_schema``, STATED RATHER THAN LEFT HANGING
--------------------------------------------------------------------
``generate_transform_with_ai`` has accepted a ``source_schema`` parameter since
P28 R19.1 and has never rendered it (p33 design R4). Task 4.7 adds a second
parameter down the same path and deliberately renders only the new one. The
reason is not tidiness, and it is asserted below
(``test_source_schema_is_still_not_rendered_and_that_is_deliberate``):

* the mapping block already renders ``source_field (source_type)`` for every
  mapping, so the source side's names and types are already in the prompt —
  rendering ``source_schema`` would add a redundant second copy;
* the target side is not symmetric. A mapping line enumerates a mapped *pair*,
  so a declared target field that no mapping covers appears **nowhere** in the
  prompt — and that is exactly the shortfall ``schema_compliance`` now fails a
  run for (``test_a_declared_field_no_mapping_covers_still_reaches_the_prompt``
  is the case that makes this concrete).

LEDGER STATUS — a gap, not an all-clear
---------------------------------------
``why`` on ``transform_generation.py`` returns only ``LGR-0176ccc-1``, a
634-file working-tree snapshot with no bearing on the prompt. ``why`` on the
``.j2`` adds ``LGR-2600abb-1`` (**backfilled**), whose only relevant line is
"Add regexp_replace to allowed functions in transform generation prompt" — that
governs the **allowed-function list**, which this change does not touch.
**Nothing governs the template's render context**, so adding an input overrides
no decision. Stated as a gap in the ledger rather than as permission.

One record does bind the render: ``LGR-d3c81fc-1`` (V7) — the loader renders
with ``StrictUndefined`` (``shared/prompt_template_loader.py:56``), so a
referenced-but-unsupplied variable raises **mid-pipeline**, not at synth. The
new slot is read through ``| default({}, true)`` for that reason, and
``test_render_tolerates_an_omitted_or_none_target_schema`` is what keeps it
true.

**Validates: Requirements 2.5, 3.8, 3.10**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _CREATE_CODE_DIR)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")))

# Imported OUTSIDE the patch.dict block below so the loader is the REAL loader
# and `transform_generation.j2` is really rendered. A mocked loader would make
# every assertion in this file a statement about the mock (`LGR-28b4d75-5`).
import transform_generation as _tg

import target_schema_resolution
from prompt_template_loader import load_prompt_template

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


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    sys.modules["transform_generation"] = _tg
    yield


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

# A target schema in the shape `resolve_target_schema` returns: normalised to
# `properties` (shared/target_schema_resolution.py:151-209 converts the
# `fields`-array form too, which is why the template has no `fields` branch).
TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "order_key": {"type": "string"},
        "order_total": {"type": "number"},
        "ordered_on": {"type": "string", "format": "date"},
    },
}

# Field mappings that cover only SOME of the declared target fields. The
# uncovered one (`ordered_on`) is the information the mappings cannot carry.
MAPPINGS = [
    {
        "source_field": "id",
        "source_type": "string",
        "target_field": "order_key",
        "target_type": "string",
        "transform": "direct",
    },
    {
        "source_field": "amount",
        "source_type": "string",
        "target_field": "order_total",
        "target_type": "number",
        "transform": "cast",
    },
]

FEEDBACK_A = {
    "summary": "Code regeneration needed (attempt 1/2)",
    "issues_found": ["review verdict: fail"],
    "reviewer_issues": [
        {"severity": "major", "category": "Schema Compliance", "description": "ordered_on is never produced"}
    ],
}
FEEDBACK_B = {
    "summary": "Code regeneration needed (attempt 2/2)",
    "issues_found": ["review verdict: fail"],
    "reviewer_issues": [
        {"severity": "critical", "category": "Correctness", "description": "to_date called without a format"}
    ],
}


def _prompt_via_generator(target_schema=None, regeneration_feedback=None, mappings=None, source_schema=None):
    """Render through ``transform_generation.generate_transform_with_ai`` (layers 2-1).

    The prompt is captured at the model boundary, so the render context is the
    one production supplies rather than one this test assembled.
    """
    seen: list[str] = []

    def _agent_factory(**_kwargs):
        def _agent(prompt_text):
            seen.append(prompt_text)
            return "df_transformed = df"

        return _agent

    _tg.generate_transform_with_ai(
        MAPPINGS if mappings is None else mappings,
        source_schema=source_schema,
        target_schema=target_schema,
        regeneration_feedback=regeneration_feedback,
        agent_factory=_agent_factory,
    )
    assert len(seen) == 1, "expected exactly one model call"
    return seen[0]


def _target_field_names(prompt: str) -> set[str]:
    """The field names rendered in the TARGET SCHEMA block only.

    Scoped to the block on purpose: a target field is usually also some
    mapping's ``target_field``, so a whole-prompt substring check would pass on
    the mapping lines alone and prove nothing about the new block.
    """
    if "TARGET SCHEMA declares" not in prompt:
        return set()
    block = prompt.split("TARGET SCHEMA declares", 1)[1].split("Generate ONLY", 1)[0]
    return {line.strip().split(" (")[0] for line in block.splitlines() if line.startswith("  ")}


_S3_TABLES_TARGET_CONFIG = {
    "type": "s3_tables",
    "bucket_name": "my-table-bucket",
    "namespace": "analytics",
    "table_name": "orders",
    "write_mode": "append",
    "source_format": "json",
}


def _prompt_through_the_whole_chain(target_schema, regeneration_feedback=None):
    """Render through ``_mod.generate_code`` — the OUTERMOST shim (layers 3-1).

    This is the plumbing test. It enters at ``lambda_function.generate_code``,
    so the path is generate_code shim → ``code_assembly.generate_code`` →
    ``generate_transform_with_ai`` shim → the impl → the real template. Only
    the AI boundary is stubbed, via ``patch.object(_mod, "Agent", ...)`` — the
    seam the shims exist to preserve.
    """
    seen: list[str] = []

    def _agent(prompt_text):
        seen.append(prompt_text)
        return 'df_transformed = df.withColumnRenamed("id", "order_key")'

    pm = {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": _S3_TABLES_TARGET_CONFIG,
        # AMENDED (p42 branch A), with the reason at the fixture rather than
        # deleted: this declared only `id` while `MAPPINGS` names `id` AND
        # `amount`, so it was a mapping claiming a source column the schema does
        # not declare. `code_assembly.generate_code` now refuses that input
        # (`_validate_mapped_source_fields`), which is the whole point of the
        # change — the fixture was under-specified, not the check
        # over-eager. Declaring both keeps this file's subject (the target schema
        # reaching the prompt through all three shims, p33 requirement 2.5)
        # exactly as it was.
        "source_schema": {"properties": {"id": {"type": "string"}, "amount": {"type": "string"}}},
        "target_schema": target_schema,
    }
    with patch.object(_mod, "Agent", MagicMock(return_value=_agent)):
        _mod.generate_code(
            pipeline_metadata=pm,
            mapping_result={"field_mappings": MAPPINGS},
            regeneration_feedback=regeneration_feedback,
        )
    assert len(seen) == 1, "expected exactly one model call through the chain"
    return seen[0]


# ---------------------------------------------------------------------------
# Requirement 2.5 — the declared target field names reach the prompt
# ---------------------------------------------------------------------------


def test_rendered_prompt_names_every_declared_target_field_and_its_type():
    """The prompt carries the target schema's declared field names and types.

    **Validates: Requirements 2.5**
    """
    prompt = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema=TARGET_SCHEMA)

    for name in TARGET_SCHEMA["properties"]:
        assert name in prompt, f"declared target field {name!r} is absent from the prompt"
    assert "order_key (string)" in prompt
    assert "order_total (number)" in prompt
    # `format` rides along where the schema declares one: it is the difference
    # between `to_date(col("f"), "dd-MM-yyyy")` and the format-less call the
    # prompt's own CRITICAL RULES forbid.
    assert "ordered_on (string, format date)" in prompt
    # The count is stated, so a truncated render is visible rather than silent.
    assert "TARGET SCHEMA declares these 3 field(s)" in prompt


def test_the_prompt_names_the_same_field_set_the_compliance_gate_scores():
    """The rendered names are exactly ``properties.keys()`` — the list
    ``_build_validation_code`` derives ``expected`` from
    (``code_interpreter_runner.py:356``).

    This is the coherence that makes requirement 2.5 worth anything: the
    generator is now told the same column set the run will be measured against,
    so a ``schema_compliance`` failure is actionable rather than a surprise.

    **Validates: Requirements 2.5**
    """
    prompt = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema=TARGET_SCHEMA)

    assert _target_field_names(prompt) == set(TARGET_SCHEMA["properties"]), (
        "the prompt's field set has drifted from the set the gate scores against"
    )


def test_a_declared_field_no_mapping_covers_still_reaches_the_prompt():
    """A target field with no mapping is in the prompt — the whole point.

    ``ordered_on`` is declared by the target schema and covered by no mapping,
    so before this change it appeared nowhere in the prompt: a mapping line
    enumerates a mapped PAIR. It is also precisely the kind of shortfall
    ``schema_compliance`` fails a run for. This is the new information, and it
    is why the asymmetry with ``source_schema`` is not arbitrary.

    **Validates: Requirements 2.5**
    """
    prompt = _prompt_via_generator(target_schema=TARGET_SCHEMA)

    assert "ordered_on" in prompt
    assert not any(m["target_field"] == "ordered_on" for m in MAPPINGS), (
        "fixture drift: ordered_on must stay unmapped for this test to mean anything"
    )


def test_the_whole_chain_delivers_the_target_schema_to_the_prompt():
    """Layers 3-1: entering at the outermost shim still reaches the render.

    The shims are the seam ``patch.object(lambda_function, "Agent", ...)`` and
    the ``generate_transform_with_ai`` patches rely on, so the parameter is
    added at every layer instead of shortcutting past one.

    **Validates: Requirements 2.5**
    """
    prompt = _prompt_through_the_whole_chain(TARGET_SCHEMA)

    assert "TARGET SCHEMA declares these 3 field(s)" in prompt
    for name in TARGET_SCHEMA["properties"]:
        assert name in prompt


# ---------------------------------------------------------------------------
# Requirement 2.5 — two attempts that differ produce prompts that differ
# ---------------------------------------------------------------------------


def test_two_attempts_with_different_feedback_render_different_prompts():
    """The feedback axis, re-pinned here in the presence of a target schema.

    ``test_regeneration_prompt_differs.py`` pins this without one. What is
    added here is that rendering a target schema on every attempt does not make
    the feedback difference disappear — a fixed block above a varying one could
    have swamped it in a template with a misplaced ``{% if %}``.

    **Validates: Requirements 2.5, 3.8**
    """
    first = _prompt_via_generator(target_schema=TARGET_SCHEMA, regeneration_feedback=None)
    second = _prompt_via_generator(target_schema=TARGET_SCHEMA, regeneration_feedback=FEEDBACK_A)
    third = _prompt_via_generator(target_schema=TARGET_SCHEMA, regeneration_feedback=FEEDBACK_B)

    assert first != second != third
    assert first != third
    assert "ordered_on is never produced" in second
    assert "to_date called without a format" in third
    # The target block survives on every attempt — it is not a first-attempt-only
    # input, or a regeneration would lose the information it needs most.
    for prompt in (first, second, third):
        assert "TARGET SCHEMA declares these 3 field(s)" in prompt


def test_prompts_differ_when_only_the_target_schema_differs():
    """The NEW axis: same mappings, same feedback, different target schema.

    This is the axis p32 did not have. Its absence is why three attempts
    produced three byte-identical scripts, so it is worth pinning directly
    rather than inferring it from the feedback case.

    **Validates: Requirements 2.5**
    """
    other = {"type": "object", "properties": {"completely": {"type": "string"}, "different": {"type": "integer"}}}

    a = _prompt_via_generator(target_schema=TARGET_SCHEMA)
    b = _prompt_via_generator(target_schema=other)

    assert a != b
    # Compared inside the target block: `order_key` is also a mapping's
    # target_field, so it appears in the mapping lines of BOTH prompts. The
    # whole-prompt assertion would have been about the mappings, not the schema.
    assert _target_field_names(a) == set(TARGET_SCHEMA["properties"])
    assert _target_field_names(b) == {"completely", "different"}


# ---------------------------------------------------------------------------
# V7 (LGR-d3c81fc-1) — the slot must not raise mid-pipeline, and must be free
# ---------------------------------------------------------------------------


def test_render_tolerates_an_omitted_or_none_target_schema():
    """Omitted, ``None`` and ``{}`` all render, and all render identically.

    The loader uses ``StrictUndefined``, so a referenced-but-unsupplied
    variable raises at render time — mid-pipeline, not at synth
    (``LGR-d3c81fc-1``). ``generate_transform_with_ai``'s parameter defaults to
    ``None``, and the handler supplies ``{}`` for a pipeline with no target
    schema (requirement 2.6), so all three shapes are live. ``| default({},
    true)`` rather than a bare ``| default`` is what covers ``None``.

    Byte-identity also means every pipeline that has no target schema keeps its
    pre-p33 prompt exactly, so this change cannot move their generated scripts.

    **Validates: Requirements 2.5, 3.10**
    """
    omitted = load_prompt_template("transform_generation.j2", mappings=MAPPINGS)
    none = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema=None)
    empty = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema={})

    assert omitted == none == empty
    assert "TARGET SCHEMA declares" not in omitted


def test_the_empty_target_slot_costs_zero_tokens():
    """The slot must emit ZERO bytes when there is no target schema — not blank
    lines.

    This is not hypothetical tidiness. The first draft of the template block
    put its explanation on ~20 tag-only comment lines plus a ``set``/``if``
    pair, and with ``trim_blocks`` off each of those contributed a newline: 22
    bytes of nothing. That took a 10-field mapping from 498 to 504 of the
    500-token budget ``test_transform_prompt.py::test_prompt_under_500_tokens_for_10_fields``
    asserts — a budget p32's T11a had already spent down to about 2 tokens of
    headroom (recorded in ``test_regeneration_prompt_differs.py``'s docstring).
    The fix is trailing-dash whitespace control on every tag in the block.

    Asserted here as well as over in the budget test because the cause and the
    symptom are in different files: that test says "the prompt is too big", this
    one says which slot did it.

    **Validates: Requirements 2.5, 3.10**
    """
    types = ["string", "integer", "double", "boolean", "date", "timestamp"]
    ten = [
        {
            "source_field": f"source_field_{i}",
            "source_type": types[i % len(types)],
            "target_field": f"target_field_{i}",
            "target_type": types[i % len(types)],
        }
        for i in range(10)
    ]

    prompt = load_prompt_template("transform_generation.j2", mappings=ten)

    # SUBJECT (p52 R1.5): the ceiling below is on the template's FIXED COST and
    # not on the production render — `ten` is Fixture_Shape (no `transformation`,
    # no `confidence`), so what it measures is the template's own instructions and
    # structure plus an empty target slot, which is why it is retained unchanged
    # at 500 (p52 do-NOT 2, do-NOT 3) rather than raised to admit the production
    # shape; the Production_Shape ceilings live in `test_transform_prompt.py`, in
    # `test_production_shape_prompt_budget` and its `_with_target_schema` sibling.
    assert len(prompt) // 4 < 500, f"the empty target slot spent budget: ~{len(prompt) // 4} tokens"
    assert "TARGET SCHEMA" not in prompt


def test_a_schema_with_no_properties_renders_no_target_block():
    """An unrecognised schema shape says nothing rather than something wrong.

    ``normalize_schema_format`` returns an unrecognised schema unchanged
    (``shared/target_schema_resolution.py:208-209``), so ``properties`` can be
    absent. Telling the generator "0 declared fields" would be a claim the
    schema does not support.

    **Validates: Requirements 2.5**
    """
    prompt = load_prompt_template(
        "transform_generation.j2", mappings=MAPPINGS, target_schema={"weird": ["shape"], "properties": {}}
    )
    assert "TARGET SCHEMA declares" not in prompt


def test_source_schema_is_still_not_rendered_and_that_is_deliberate():
    """R4: ``source_schema`` stays accepted-and-dropped; the asymmetry is a
    decision, and this test is where it is written down as one.

    The mapping block already renders ``source_field (source_type)`` for every
    mapping, so rendering ``source_schema`` would repeat what the prompt
    already says. The target side is not symmetric — a declared target field no
    mapping covers appears nowhere — which is why one is rendered and one is
    not. If a later change decides to render the source schema too, this test
    should be amended with the reason (C11), not deleted.

    **Validates: Requirements 2.5**
    """
    with_source = _prompt_via_generator(
        target_schema=TARGET_SCHEMA,
        source_schema={"properties": {"a_source_only_field": {"type": "string"}}},
    )
    without_source = _prompt_via_generator(target_schema=TARGET_SCHEMA, source_schema=None)

    assert with_source == without_source, (
        "source_schema started affecting the prompt — R4 needs updating, not this test"
    )
    assert "a_source_only_field" not in with_source
    # ... while the source field names the mappings DO carry are present, which
    # is the reason the dropped parameter costs nothing today.
    assert "id" in with_source and "amount" in with_source


# ---------------------------------------------------------------------------
# Layer 4 — lambda_handler puts the RESOLVED schema into `pm`
# ---------------------------------------------------------------------------

_PIPELINE_ID = "pipe-t47"


def test_handler_puts_the_resolved_target_schema_into_the_codegen_context():
    """The schema in ``pm`` is the S3 object the pipeline record names.

    Without this the other tests would pass while the handler passed nothing.
    The resolver runs for real over a stubbed S3 client, so what lands in
    ``pm["target_schema"]`` is what ``resolve_target_schema`` produced.

    **Validates: Requirements 2.5**
    """
    body = json.dumps(TARGET_SCHEMA).encode("utf-8")
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": MagicMock(read=MagicMock(return_value=body))}))
    captured: dict = {}

    def _capture(pm, mapping_item, regeneration_feedback=None, **kwargs):
        captured["pm"] = pm
        return "# code\ndf = spark.read.json('s3://b/k')\n"

    state_table = MagicMock(
        get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )
    with (
        patch.object(
            _mod,
            "get_pipeline_metadata",
            return_value={
                "target_config": _S3_TABLES_TARGET_CONFIG,
                "target_schema_bucket": "adi-target-schemas-123456789012-us-east-1",
                "target_schema_s3_key": f"{_PIPELINE_ID}/target.json",
            },
        ),
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(
            _mod,
            "mappings_table",
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "mapping_id": "map-t47",
                            "mapping_result": "{}",
                            "field_mappings": MAPPINGS,
                            "source_schema_hash": "src-hash",
                            "target_schema_hash": "tgt-hash",
                        }
                    }
                )
            ),
        ),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generate_code", side_effect=_capture),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
        # The resolver builds its own lazily-cached client; patch the accessor on
        # the module the handler holds. Import order matters — see
        # test_target_schema_and_hash_reads.py's hazard note.
        patch.object(target_schema_resolution, "_s3_client", lambda: s3),
    ):
        result = _mod.lambda_handler({"run_id": "run-t47", "pipeline_id": _PIPELINE_ID, "mapping_id": "map-t47"}, None)

    assert result["statusCode"] == 200
    assert set(captured["pm"]["target_schema"]["properties"]) == set(TARGET_SCHEMA["properties"])


def test_handler_passes_an_empty_target_schema_when_the_pipeline_has_none():
    """Requirement 2.6's legitimate state reaches ``pm`` as ``{}``, not as a
    missing key or a raise — and the template renders no target block for it,
    so those pipelines keep their pre-p33 prompt.

    **Validates: Requirements 2.5, 3.10**
    """
    captured: dict = {}

    def _capture(pm, mapping_item, regeneration_feedback=None, **kwargs):
        captured["pm"] = pm
        return "# code\ndf = spark.read.json('s3://b/k')\n"

    state_table = MagicMock(
        get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )
    s3 = MagicMock()
    with (
        patch.object(_mod, "get_pipeline_metadata", return_value={"target_config": _S3_TABLES_TARGET_CONFIG}),
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(
            _mod,
            "mappings_table",
            MagicMock(
                get_item=MagicMock(
                    return_value={
                        "Item": {
                            "mapping_id": "map-t47",
                            "mapping_result": "{}",
                            "field_mappings": MAPPINGS,
                            "source_schema_hash": "src-hash",
                            "target_schema_hash": "tgt-hash",
                        }
                    }
                )
            ),
        ),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generate_code", side_effect=_capture),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
        patch.object(target_schema_resolution, "_s3_client", lambda: s3),
    ):
        result = _mod.lambda_handler({"run_id": "run-t47", "pipeline_id": _PIPELINE_ID, "mapping_id": "map-t47"}, None)

    assert result["statusCode"] == 200
    assert captured["pm"]["target_schema"] == {}
    s3.get_object.assert_not_called()


# ---------------------------------------------------------------------------
# Property — over generated schemas, not just the fixture
# ---------------------------------------------------------------------------

_FIELD_NAMES = st.lists(
    st.from_regex(r"[a-z][a-z0-9_]{2,12}", fullmatch=True),
    min_size=1,
    max_size=8,
    unique=True,
)


def _schema_of(names):
    types = ["string", "integer", "number", "boolean"]
    return {"type": "object", "properties": {n: {"type": types[i % len(types)]} for i, n in enumerate(names)}}


@given(names=_FIELD_NAMES)
@settings(max_examples=50, deadline=None)
def test_every_declared_field_reaches_the_prompt_for_any_schema(names):
    """For any non-empty target schema, every declared field name is rendered.

    **Validates: Requirements 2.5**
    """
    prompt = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema=_schema_of(names))

    missing = [n for n in names if n not in prompt]
    assert not missing, f"{len(missing)} declared field(s) absent from the prompt: {missing}"
    assert f"declares these {len(names)} field(s)" in prompt


@given(left=_FIELD_NAMES, right=_FIELD_NAMES)
@settings(max_examples=50, deadline=None)
def test_different_target_schemas_render_different_prompts(left, right):
    """Two schemas with different field sets never render the same prompt.

    The generator's information about target names now varies with the target
    schema — the axis p32 was missing. Same field sets are allowed to render
    identically, so the property is scoped to differing sets.

    **Validates: Requirements 2.5**
    """
    if set(left) == set(right):
        return
    a = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema=_schema_of(left))
    b = load_prompt_template("transform_generation.j2", mappings=MAPPINGS, target_schema=_schema_of(right))

    assert a != b
