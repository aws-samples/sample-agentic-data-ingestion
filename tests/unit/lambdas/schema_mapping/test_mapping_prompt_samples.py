# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p49 C2 — sampled source values reach the MAPPING prompt (task 3.1).

The observed defect: the mapping agent authored the `transformation` hint
*"TO_DATE(signup_date, 'YYYY-MM-DD')"* for MM/dd/yyyy data, and that string
reached the generated code, which nulled all five rows. Task 1.2 established
why it could not do better — `generate_mapping_with_bedrock`'s prompt carries
`json.dumps(source_schema)`, `json.dumps(target_schema)` and `context_section`
and no value anywhere. So C2 gives that prompt the values.

What this module pins:

- the reader (`read_source_samples`) — CSV and JSON/JSONL, the byte bound, and
  **inertness on every failure**. Inertness is not hygiene here: an exception
  reaching `generate_mapping_with_bedrock`'s outer `except Exception` (`:906`)
  would degrade an honest model mapping into `generate_simple_mapping`, which
  P28 R3.1 then re-reports as `fallback_substring_match`.
- the prompt — the block's shape, the instruction that satisfies R3.1 and
  do-NOT 8, and that a run with no samples produces a **byte-identical** prompt
  to the pre-p49 one.
- the **new size budget**, which is the point of asserting anything about size
  here: no test asserted any size on this prompt before p49, so C2's cost was
  unbounded and unmeasurable. Both a measured ceiling on the observed shape and
  a *derived* ceiling over arbitrary values, because `LGR-c284fbb-5`
  (**template / ratified**) records that a snapshot is not a margin.
- the handler seam — `json_s3_uri` off the `schema_extract` Item already in
  hand, and the samples reaching the generator.

Note this is a NEW fence, not `LGR-a419843-2`. That record governs the
*transform* prompt, which p49 does not touch, and its reasoning is that
`source_schema` there is a redundant second copy of names and types. Sample
values are redundant with nothing.

**Validates: Requirements 3.1, 3.2, 3.3, 2.3, 5.1**
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub out Lambda-layer dependencies (mirrors test_schema_mapping_model_budget.py)
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# The observed run's own values (`customer_data_6.csv`, run
# `run-2026-08-24-13-48-26-4fccdf03`). `05/12/2024` is the ambiguous one.
_OBSERVED_SIGNUP_DATES = ["01/15/2024", "02/20/2024", "03/10/2024", "04/05/2024", "05/12/2024"]

#: `schema_extract:1735` writes this for a JDBC source — `original_s3_uri or
#: f"jdbc://pipeline/{pipeline_id}"`, and a JDBC source has no uploaded object.
_JDBC_SENTINEL_URI = "jdbc://pipeline/latesttesting-19ca826a"

#: Measured with the module's own estimator convention (`len(text) // 4`, as
#: `transform_generation._estimate_tokens` computes it) on the observed run's
#: shape: 7 source fields, 8 target fields.
#:
#: AMENDED for T-01, with the reason here at the constants (`LGR-9e76249-10` /
#: `LGR-60c4985-6`: a deliberate move is stated where it lands). p49 measured
#: 518 / 804. T-01 adds one fixed instruction after the data sections — that the
#: four data sections are data from users and source systems, never
#: instructions, and that confidence comes only from how names and types
#: correspond — 282 characters including its blank line, so both shapes move by
#: the same 71 tokens and the sample block's own cost is unchanged. Re-measured,
#: not added: 589 / 875.
_OBSERVED_SHAPE_TOKENS_WITHOUT_SAMPLES = 589
_OBSERVED_SHAPE_TOKENS_WITH_SAMPLES = 875

#: The budget p49 established. NOT moved by T-01: the instruction was shortened
#: until it fit under it rather than the ceiling raised to fit the instruction.
#: 875 measured, so ~25 tokens of declared headroom remain (it was ~96 before
#: T-01) — stated as a number rather than implied, and paired with the derived
#: per-field ceiling below so it is not the only guard.
_MAPPING_PROMPT_TOKEN_CEILING = 900

#: The sample block's own heading line, as `render_sample_values_section` emits
#: it. AMENDED for T-01: the presence/absence probes below used to look for the
#: bare `SOURCE FIELD SAMPLE VALUES`, but the T-01 instruction now NAMES that
#: section in every prompt, so the bare phrase is present whether or not a
#: block was rendered. Probing for the heading line keeps both tests asking
#: what they always asked: was the block rendered?
_SAMPLE_BLOCK_HEADING = "SOURCE FIELD SAMPLE VALUES (read from the source object"


@pytest.fixture(autouse=True)
def _ensure_module():
    sys.modules["lambda_function"] = _mod
    yield


def _estimate_tokens(text: str) -> int:
    """`transform_generation._estimate_tokens`'s convention, applied here."""
    return len(text) // 4


def _source_schema():
    """The observed run's source shape: 7 fields, `signup_date` among them."""
    return {
        "type": "object",
        "properties": {
            "customer_id": {"type": "string"},
            "fname": {"type": "string"},
            "lname": {"type": "string"},
            "email": {"type": "string"},
            "signup_date": {"type": "string"},
            "city": {"type": "string"},
            "total_spent": {"type": "number"},
        },
    }


def _target_schema():
    """8 target fields; `registration_date` is the one the defect landed on."""
    return {
        "type": "object",
        "properties": {
            "customer_key": {"type": "string"},
            "first_name": {"type": "string"},
            "last_name": {"type": "string"},
            "email_address": {"type": "string"},
            "registration_date": {"type": "string", "format": "date"},
            "city_name": {"type": "string"},
            "lifetime_value": {"type": "number"},
            "phone": {"type": "string"},
        },
    }


def _observed_samples():
    return {
        "customer_id": ["1", "2", "3", "4", "5"],
        "fname": ["Ada", "Bo", "Cy", "Di", "Ed"],
        "lname": ["Lovelace", "Nguyen", "Okafor", "Patel", "Quinn"],
        "email": ["ada@example.com", "bo@example.com", "cy@example.com", "di@example.com", "ed@example.com"],
        "signup_date": list(_OBSERVED_SIGNUP_DATES),
        "city": ["Seattle", "Austin", "Denver", "Boston", "Reno"],
        "total_spent": ["120.50", "98.00", "1500.25", "43.10", "770.00"],
    }


#: Every source field in `_source_schema()` paired with its target, so a run
#: built on this fixture is not refused by p55 R14's mappability hard fail.
#:
#: AMENDED for p55 task 3.1, with the reason stated here rather than by quietly
#: widening the fixture. `enforce_source_field_mappability`
#: (`schema_mapping/lambda_function.py:1815`) refuses a run whose mapped share of
#: the DECLARED source schema is at or below `MAPPABILITY_HARD_FAIL_THRESHOLD`,
#: and this file's `_source_schema()` declares seven fields. The previous fixture
#: carried the single `signup_date → registration_date` entry the p49 defect
#: landed on — 1 of 7, a ratio of 0.14 — which R14.1 now refuses before the run
#: reaches anything this file is about. `signup_date` stays first and keeps its
#: transformation and confidence verbatim; the other six are additions, and
#: `phone` is still left unsourced on the target side because that is the shape
#: the observed run produced.
_OBSERVED_FIELD_PAIRS = (
    ("registration_date", "signup_date"),
    ("customer_key", "customer_id"),
    ("first_name", "fname"),
    ("last_name", "lname"),
    ("email_address", "email"),
    ("city_name", "city"),
    ("lifetime_value", "total_spent"),
)


def _converse_response():
    body = {
        "mappings": [
            {
                "target_field": target,
                "source_field": source,
                "transformation": "to_date with MM/dd/yyyy" if source == "signup_date" else "direct",
                "confidence": 0.96,
            }
            for target, source in _OBSERVED_FIELD_PAIRS
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "mapping_summary": "ai mapping",
    }
    return {
        "output": {"message": {"content": [{"text": json.dumps(body)}]}},
        "usage": {"inputTokens": 10, "outputTokens": 20},
    }


def _prompt_for(**kwargs) -> str:
    """The prompt `generate_mapping_with_bedrock` actually sent, via `_prompt_text`."""
    with patch.object(_mod, "bedrock_runtime") as bedrock:
        bedrock.converse.return_value = _converse_response()
        result = _mod.generate_mapping_with_bedrock(_source_schema(), _target_schema(), "", "us.test-model", **kwargs)
    return result["_prompt_text"]


def _body_stream(payload: bytes):
    """An S3 `Body` whose `read(n)` honours the byte bound, as botocore's does."""
    return MagicMock(read=MagicMock(side_effect=lambda amount=None: payload[:amount] if amount else payload))


# --- The reader: what it reads ------------------------------------------------


def test_the_reader_samples_a_csv_source():
    """A CSV object — the observed run's shape. Unique non-empty values, in order."""
    body = "customer_id,signup_date\n1,01/15/2024\n2,02/20/2024\n3,02/20/2024\n"
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": _body_stream(body.encode())}))

    with patch.object(_mod, "s3_client", s3):
        samples = _mod.read_source_samples("s3://adi-source-files-123/uploads/customer_data_6.csv")

    assert samples == {"customer_id": ["1", "2", "3"], "signup_date": ["01/15/2024", "02/20/2024"]}


def test_the_reader_samples_a_json_lines_source():
    body = '{"signup_date": "01/15/2024"}\n{"signup_date": "02/20/2024"}\n'
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": _body_stream(body.encode())}))

    with patch.object(_mod, "s3_client", s3):
        samples = _mod.read_source_samples("s3://adi-source-files-123/uploads/data.jsonl")

    assert samples == {"signup_date": ["01/15/2024", "02/20/2024"]}


def test_the_reader_bounds_the_body_read():
    """The read is byte-bounded, and this asserts it rather than trusting it.

    `schema_mapping` has 1024 MiB (`compute-stack.ts:349`), but a `MemoryError`
    is an `Exception` — an unbounded read that blew up inside
    `generate_mapping_with_bedrock` would be swallowed at `:906` and the run
    would re-report a good model mapping as `fallback_substring_match`.
    """
    stream = _body_stream(b"customer_id,signup_date\n1,01/15/2024\n")
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": stream}))

    with patch.object(_mod, "s3_client", s3):
        _mod.read_source_samples("s3://adi-source-files-123/uploads/x.csv")

    (amount,) = stream.read.call_args.args
    assert amount == _mod._MAX_SAMPLE_BYTES
    assert amount > 0


# --- The reader: inertness on every failure -----------------------------------


def test_the_reader_declines_a_jdbc_uri_before_any_aws_call():
    """JDBC is the one source type C2 does not improve — asserted, not assumed."""
    s3 = MagicMock()

    with patch.object(_mod, "s3_client", s3):
        assert _mod.read_source_samples(_JDBC_SENTINEL_URI) == {}

    s3.get_object.assert_not_called()


def test_a_declined_jdbc_uri_logs_at_info_not_warning():
    """For a JDBC pipeline the sentinel is the normal state, not a fault."""
    with patch.object(_mod, "s3_client", MagicMock()), patch.object(_mod, "logger") as log:
        _mod.read_source_samples(_JDBC_SENTINEL_URI)

    log.warning.assert_not_called()
    log.info.assert_called()


def test_the_reader_is_inert_on_an_absent_uri():
    """A run predating `json_s3_uri` is not evidence about the source's formats."""
    s3 = MagicMock()

    with patch.object(_mod, "s3_client", s3):
        assert _mod.read_source_samples("") == {}

    s3.get_object.assert_not_called()


def test_the_reader_is_inert_on_an_unexpected_uri_shape():
    s3 = MagicMock()

    with patch.object(_mod, "s3_client", s3), patch.object(_mod, "logger") as log:
        assert _mod.read_source_samples("https://example.com/data.csv") == {}

    s3.get_object.assert_not_called()
    log.warning.assert_called()


def test_the_reader_is_inert_when_the_read_raises():
    """`AccessDenied`, `NoSuchKey`, throttling, decode errors — all `{}`."""
    s3 = MagicMock(get_object=MagicMock(side_effect=RuntimeError("AccessDenied")))

    with patch.object(_mod, "s3_client", s3):
        assert _mod.read_source_samples("s3://adi-source-files-123/uploads/x.csv") == {}


def test_the_reader_is_inert_on_an_empty_object():
    s3 = MagicMock(get_object=MagicMock(return_value={"Body": _body_stream(b"")}))

    with patch.object(_mod, "s3_client", s3):
        assert _mod.read_source_samples("s3://adi-source-files-123/uploads/x.csv") == {}


# --- The prompt ---------------------------------------------------------------


def test_the_prompt_carries_the_sampled_values():
    """R3.3 — the model sees the actual data when it authors `transformation`."""
    prompt = _prompt_for(sample_values=_observed_samples())

    assert _SAMPLE_BLOCK_HEADING in prompt
    for value in _OBSERVED_SIGNUP_DATES:
        assert value in prompt
    assert 'signup_date: Sample values: ["01/15/2024"' in prompt


def test_the_prompt_forbids_naming_a_format_it_did_not_derive():
    """R3.1 — the instruction that separates a derived format from a copied example.

    The observed hint's `'YYYY-MM-DD'` came from this prompt's own JSON example.
    The rule has to say both halves out loud: use the values, and do not name a
    format for a field that has none.
    """
    prompt = _prompt_for(sample_values=_observed_samples())

    assert "MUST be the one" in prompt
    assert "never copy a format from this prompt's own example" in prompt
    assert "never name a format for a field with no sample values above" in prompt


def test_the_prompt_refuses_to_settle_an_ambiguous_layout():
    """R5.1 / do-NOT 8 — `05/12/2024` fits MM/dd/yyyy and dd/MM/yyyy alike.

    C3 (task 5.1) owns surfacing the ambiguity to a human. This only pins that
    the model is told not to resolve it silently first.
    """
    prompt = _prompt_for(sample_values=_observed_samples())

    assert "05/12/2024" in prompt
    assert "MM/dd/yyyy" in prompt and "dd/MM/yyyy" in prompt
    assert "do not pick one silently" in prompt


def test_a_run_with_no_samples_gets_the_pre_p49_prompt_byte_for_byte():
    """The inert paths — JDBC, a failed read, an old run — cost exactly nothing.

    This is what makes the new budget spend only where there is evidence, and
    it is why the JDBC gap costs no tokens rather than some.
    """
    baseline = _prompt_for()

    assert _prompt_for(sample_values={}) == baseline
    assert _prompt_for(sample_values=None) == baseline
    assert _SAMPLE_BLOCK_HEADING not in baseline


def test_only_declared_source_fields_are_rendered():
    """A column in the object but not in the schema is not part of the mapping."""
    prompt = _prompt_for(sample_values={"signup_date": ["01/15/2024"], "internal_notes": ["secret"]})

    assert "signup_date: Sample values:" in prompt
    assert "internal_notes" not in prompt


# --- The budget this task establishes ----------------------------------------


def test_the_observed_shape_measures_what_the_ceiling_assumes():
    """The two figures the ceiling is derived from, pinned so they cannot drift."""
    assert _estimate_tokens(_prompt_for()) == _OBSERVED_SHAPE_TOKENS_WITHOUT_SAMPLES
    assert _estimate_tokens(_prompt_for(sample_values=_observed_samples())) == _OBSERVED_SHAPE_TOKENS_WITH_SAMPLES


def test_the_mapping_prompt_holds_its_new_token_ceiling():
    """R2.3 — the first size assertion this prompt has ever had.

    Before p49 no test asserted any size on the mapping prompt (grep-verified
    across `tests/unit/lambdas/schema_mapping/`), so C2's cost had no ceiling at
    all. 518 -> 804 tokens on the observed shape; the ceiling is 900. T-01's
    fixed instruction then moved both shapes by 71 (589 -> 875) under the same,
    unmoved ceiling — see the constants above.
    """
    prompt = _prompt_for(sample_values=_observed_samples())

    tokens = _estimate_tokens(prompt)
    assert tokens < _MAPPING_PROMPT_TOKEN_CEILING, (
        f"mapping prompt is {tokens} tokens against a {_MAPPING_PROMPT_TOKEN_CEILING} ceiling; "
        "if this is deliberate, move the ceiling with its reason at the assertion "
        "(`LGR-9e76249-10` / `LGR-60c4985-6`), do not widen it silently"
    )


def test_the_sample_block_is_bounded_by_its_derived_ceiling():
    """The guard that is a formula rather than a snapshot.

    `LGR-c284fbb-5` (**template / ratified**) chose a property over a snapshot
    assertion because "14 characters is a snapshot rather than a margin". A CSV
    cell is of unbounded width, so without a per-value cap the block's cost has
    no expression; with one it is O(declared source fields), the same axis
    `json.dumps(source_schema)` already scales on.
    """
    for field_count in (1, 3, 7, 20):
        schema = {"properties": {f"field_{i}" + "x" * 300: {"type": "string"} for i in range(field_count)}}
        samples = {name: ["v" * 900] * 10 for name in schema["properties"]}

        rendered = _mod.render_sample_values_section(schema, samples)

        ceiling = _mod._sample_section_char_ceiling(field_count)
        assert len(rendered) <= ceiling, f"{field_count} fields rendered {len(rendered)} chars against {ceiling}"


def test_the_fixed_part_of_the_block_matches_its_declared_constant():
    """`_SAMPLE_SECTION_FIXED_CHARS` is pinned to the live render, not trusted."""
    one_line = '  - a: Sample values: ["1"]\n'
    rendered = _mod.render_sample_values_section({"properties": {"a": {}}}, {"a": ["1"]})

    fixed = len(rendered) - len(one_line)
    assert fixed <= _mod._SAMPLE_SECTION_FIXED_CHARS
    assert fixed == 573  # measured; the constant carries 47 chars of declared slack


def test_a_long_value_is_truncated_and_marked_as_truncated():
    """A truncated value must not read as a complete one — it would teach a wrong format."""
    rendered = _mod.render_sample_values_section(
        {"properties": {"blob": {"type": "string"}}}, {"blob": ["01/15/2024" + "0" * 500]}
    )

    assert "…" in rendered
    assert len(rendered) < 900
    assert "0" * 200 not in rendered


# --- The handler seam --------------------------------------------------------

_PATCHES = [
    "lambda_function.get_source_schema",
    "lambda_function.table",
    "lambda_function.get_pipeline_metadata",
    "lambda_function.get_target_schema",
    "lambda_function.normalize_schema_format",
    "lambda_function.get_ai_model_for_pipeline",
    "lambda_function.generate_mapping_id",
    "lambda_function.store_mapping_separately",
    "lambda_function.determine_approval_requirement",
    "lambda_function.store_mapping_result",
    "lambda_function.publish_pipeline_state_change",
    "lambda_function.write_agent_audit_entry",
    "lambda_function.AgentTracePlugin",
]


def _handler_mocks(json_s3_uri: str, sample_s3_uri: str | None = None):
    """Patch the handler's collaborators, leaving the generator and reader real.

    ``sample_s3_uri`` is p53's additional attribute and is threaded here rather
    than into a second helper so the existing callers keep their shape: passing
    nothing reproduces the pre-p53 Item exactly, which is the shape
    ``store_schema_in_dynamodb`` writes when no Sample_Artifact exists (it omits
    the key — ``schema_extract:1364-1368``). ``""`` is passed explicitly to
    exercise the present-but-empty case.
    """
    patches = {name.split(".", 1)[1]: patch(name) for name in _PATCHES}
    started = {key: patcher.start() for key, patcher in patches.items()}

    started["get_source_schema"].return_value = _source_schema()
    item = {"original_pipeline_id": "pipe-1"}
    if json_s3_uri:
        item["json_s3_uri"] = json_s3_uri
    if sample_s3_uri is not None:
        item["sample_s3_uri"] = sample_s3_uri
    started["table"].get_item.return_value = {"Item": item}
    started["get_pipeline_metadata"].return_value = {"pipeline_id": "pipe-1"}
    started["get_target_schema"].return_value = (_target_schema(), "tgt-hash")
    started["normalize_schema_format"].side_effect = lambda schema: schema
    started["get_ai_model_for_pipeline"].return_value = "us.test-model"
    started["generate_mapping_id"].return_value = "map-1"
    started["determine_approval_requirement"].return_value = False
    return patches, started


def _event():
    return {"run_id": "run-1", "pipeline_id": "pipe-1", "source_schema_hash": "abc", "additional_context": ""}


def _mappable_result() -> dict:
    """A generator stub whose mapping covers `_source_schema()`.

    AMENDED for p55 task 3.1. The four handler tests below previously stubbed the
    generator with `{"mappings": []}` — the minimum that let the run complete,
    because none of them is about the mapping's content. A mapping covering none
    of seven declared source fields is a Source_Field_Mappability_Ratio of 0.0,
    which `enforce_source_field_mappability` now refuses (p55 R14.1) before the
    seam each of these tests exists to observe. The stub is widened rather than
    the check relaxed: every one of these tests asserts something about the
    sample URI or the generator's arguments, and all of that is unchanged.
    """
    return {
        "mappings": [
            {"target_field": target, "source_field": source, "confidence": 0.96, "transformation": "direct"}
            for target, source in _OBSERVED_FIELD_PAIRS
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
    }


def test_the_handler_samples_the_uri_off_the_schema_extract_item():
    """The seam: `json_s3_uri` from the row the handler already fetched (`:1730`)."""
    patches, _started = _handler_mocks("s3://adi-source-files-123/uploads/customer_data_6.csv")
    try:
        with (
            patch.object(_mod, "memory_client", None),
            patch.object(_mod, "read_source_samples", return_value={}) as read,
            patch.object(_mod, "generate_mapping_with_bedrock", return_value=_mappable_result()),
        ):
            result = _mod.lambda_handler(_event(), None)
    finally:
        for patcher in patches.values():
            patcher.stop()

    assert result["statusCode"] == 200
    read.assert_called_once_with("s3://adi-source-files-123/uploads/customer_data_6.csv")


# ── p53 R2.4/R2.5: the sample URI is a PREFERENCE, not a replacement ────────
#
# The test above stays unmodified and is the absent case: its Item carries no
# `sample_s3_uri` key. The two below cover present and present-but-empty.


def _uri_the_reader_was_given(sample_s3_uri):
    patches, _started = _handler_mocks(
        "s3://adi-source-files-123/uploads/customer_data_6.csv", sample_s3_uri=sample_s3_uri
    )
    try:
        with (
            patch.object(_mod, "memory_client", None),
            patch.object(_mod, "read_source_samples", return_value={}) as read,
            patch.object(_mod, "generate_mapping_with_bedrock", return_value=_mappable_result()),
        ):
            result = _mod.lambda_handler(_event(), None)
    finally:
        for patcher in patches.values():
            patcher.stop()

    assert result["statusCode"] == 200
    return read.call_args.args[0]


def test_the_handler_prefers_the_sample_uri_when_the_row_records_one():
    """p53 R2.4: a recorded `sample_s3_uri` wins over `json_s3_uri`.

    Consumer 2's whole change. The reader keeps its signature, its caps and its
    `{}`-never-raise contract (R2.1–R2.3); it is simply handed the
    Sample_Artifact's `s3://` URI instead of the `jdbc://` sentinel, so a JDBC
    pipeline's mapping prompt carries real sample values from run 2 onwards.
    **Validates: Requirements 2.4**
    """
    given = _uri_the_reader_was_given("s3://adi-processed-files-1-r/samples/pipe-1/source-sample.json")

    assert given == "s3://adi-processed-files-1-r/samples/pipe-1/source-sample.json"


def test_an_empty_sample_uri_falls_back_to_json_s3_uri():
    """p53 R2.5: present-but-empty is treated as absent.

    `schema_extract`'s return envelope always carries `sample_s3_uri`, as `""`
    when there is no artifact (`schema_extract:1791-1804`), so the fallback is
    keyed on truthiness rather than on key presence.
    **Validates: Requirements 2.5**
    """
    assert _uri_the_reader_was_given("") == "s3://adi-source-files-123/uploads/customer_data_6.csv"


def test_the_samples_reach_the_generator():
    patches, _started = _handler_mocks("s3://adi-source-files-123/uploads/customer_data_6.csv")
    try:
        with (
            patch.object(_mod, "memory_client", None),
            patch.object(_mod, "read_source_samples", return_value={"signup_date": _OBSERVED_SIGNUP_DATES}),
            patch.object(_mod, "generate_mapping_with_bedrock", return_value=_mappable_result()) as generate,
        ):
            _mod.lambda_handler(_event(), None)
    finally:
        for patcher in patches.values():
            patcher.stop()

    assert generate.call_args.kwargs["sample_values"] == {"signup_date": _OBSERVED_SIGNUP_DATES}
    # The existing positional contract is untouched — `test_schema_mapping_context`
    # asserts `call_args[0][2]` and `[0][3]`.
    assert generate.call_args[0][2] == ""
    assert generate.call_args[0][3] == "us.test-model"


def test_a_failing_sample_read_does_not_downgrade_the_mapping_to_the_fallback():
    """The constraint that made the read's placement load-bearing (P28 R3.1).

    A raise from the read would be caught by `generate_mapping_with_bedrock`'s
    outer `except Exception` (`:906-909`) and re-reported as
    `fallback_substring_match`. Two things prevent it: the reader is inert, and
    the read happens outside that function. Here the S3 call raises and the run
    still reports the model's own provenance.
    """
    patches, _started = _handler_mocks("s3://adi-source-files-123/uploads/customer_data_6.csv")
    captured = {}
    try:
        with (
            patch.object(_mod, "memory_client", None),
            patch.object(_mod, "s3_client", MagicMock(get_object=MagicMock(side_effect=RuntimeError("AccessDenied")))),
            patch.object(_mod, "bedrock_runtime") as bedrock,
            patch.object(
                _mod,
                "store_mapping_result",
                side_effect=lambda *args, **kwargs: captured.setdefault("mapping_result", args[3]),
            ),
        ):
            bedrock.converse.return_value = _converse_response()
            result = _mod.lambda_handler(_event(), None)
    finally:
        for patcher in patches.values():
            patcher.stop()

    assert result["statusCode"] == 200
    bedrock.converse.assert_called_once()
    assert captured["mapping_result"]["generated_by"] == "us.test-model"
    assert captured["mapping_result"]["generated_by"] != _mod.FALLBACK_GENERATED_BY


def test_a_jdbc_source_run_still_completes_with_no_samples():
    """R1.3's JDBC row, at the handler: inert, and the run is unaffected."""
    patches, _started = _handler_mocks(_JDBC_SENTINEL_URI)
    try:
        with (
            patch.object(_mod, "memory_client", None),
            patch.object(_mod, "s3_client", MagicMock()) as s3,
            patch.object(_mod, "generate_mapping_with_bedrock", return_value=_mappable_result()) as generate,
        ):
            result = _mod.lambda_handler(_event(), None)
    finally:
        for patcher in patches.values():
            patcher.stop()

    assert result["statusCode"] == 200
    s3.get_object.assert_not_called()
    assert generate.call_args.kwargs["sample_values"] == {}
