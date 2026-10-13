# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: create_code reads the target schema and the schema hashes honestly.

p33 task 4.6. Two defects in the same block of ``lambda_handler``, both of the
same class — a read that produced a valid-looking value meaning nothing:

1. ``target_schema = mapping_item.get("target_schema", {})``. No writer has ever
   set a ``target_schema`` attribute on a schema-mappings record
   (``schema_mapping.store_mapping_separately`` writes the two hashes and the
   serialised mapping result, never the schema), so the read returned ``{}`` on
   every run of every pipeline. The canonical location is the pipeline record's
   ``target_schema_bucket`` + ``target_schema_s3_key`` pair, resolved together by
   ``shared/target_schema_resolution.resolve_target_schema`` (requirement 2.1,
   ``LGR-7c9378d-1`` — resolve the pair where the pair is used).
2. ``mapping_item.get("source_schema_hash", str(uuid.uuid4()))`` and the
   ``target_schema_hash`` equivalent. A missing hash became a fresh random
   identifier that matches no schema, and it is written onto the generated-code
   record where the dashboard joins it back to the schema-extraction history
   (``dashboard_api/handlers/glue_jobs.py:157-180``). Requirement 2.9 makes the
   absence a ``StepFailure`` naming the attribute.

And one non-change that has to stay a non-change: ``mapping_id``'s
``str(uuid.uuid4())`` default. It looks like the same defect and is not — the
value is superseded by the mapping lookup, which already raises on an id that
resolves to nothing. Requirement 3.11 preserves that, and
``test_mapping_id_uuid_default_is_still_superseded_by_the_lookup`` pins it.

TEST-CONSTRUCTION HAZARD, recorded because this file trips over it
-----------------------------------------------------------------
``target_schema_resolution`` is imported at module scope **above** the
``patch.dict("sys.modules", ...)`` block. ``patch.dict`` RESTORES the dict on
exit, dropping any module first imported inside it, so importing the resolver
inside the block and again outside yields two distinct module objects — the
handler would hold one and the test would patch the other, and the resolver's
real boto3 client would then reach S3. That failure mode has already been
observed on this spec as a live ``NoSuchBucket`` escaping a unit test.

Validates: p33 Requirements 2.1, 2.9, 3.11
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_create_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")))

# BEFORE the patch.dict block — see the module docstring.
import target_schema_resolution

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
    yield


# Guard the hazard the docstring describes, so it fails loudly rather than by
# reaching the network, if the import order is ever disturbed.
def test_the_handler_and_this_test_share_one_resolver_module():
    """**Validates: Requirements 2.1** (as a precondition for every test below)."""
    assert _mod.resolve_target_schema is target_schema_resolution.resolve_target_schema


PIPELINE_ID = "pipe-t46"
RUN_ID = "run-t46"
MAPPING_ID = "map-t46"

_TARGET_SCHEMA_BODY = {
    "type": "object",
    "properties": {
        "customer_id": {"type": "string"},
        "order_total": {"type": "number"},
    },
}

_TEMPLATE_TARGET_CONFIG = {
    "type": "s3_tables",
    "bucket_name": "b",
    "namespace_name": "n",
    "table_name": "t",
}


def _mapping_item(**overrides):
    """A mappings record shaped like ``store_mapping_separately``'s output."""
    item = {
        "mapping_id": MAPPING_ID,
        "mapping_result": "{}",
        "source_schema_hash": "src-hash-abc",
        "target_schema_hash": "tgt-hash-def",
    }
    item.update(overrides)
    return {key: value for key, value in item.items() if value is not _ABSENT}


_ABSENT = object()


def _s3_returning(body: bytes):
    s3 = MagicMock()
    s3.get_object.return_value = {"Body": MagicMock(read=MagicMock(return_value=body))}
    return s3


def _handler_patches(
    *,
    mapping_item,
    pipeline_metadata,
    s3=None,
    target_config=None,
    load_prompt_template=None,
):
    """Everything lambda_handler touches, with the AI + AWS boundaries stubbed."""
    metadata = {"target_config": target_config or _TEMPLATE_TARGET_CONFIG, **pipeline_metadata}
    state_table = MagicMock(
        get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )
    patches = [
        patch.object(_mod, "get_pipeline_metadata", return_value=metadata),
        patch.object(_mod, "pipeline_runs_table", state_table),
        patch.object(
            _mod,
            "mappings_table",
            MagicMock(get_item=MagicMock(return_value={"Item": mapping_item} if mapping_item is not None else {})),
        ),
        patch.object(_mod, "generated_code_table", MagicMock()),
        patch.object(_mod, "s3_client", MagicMock()),
        patch.object(_mod, "publish_pipeline_state_change"),
        patch.object(_mod, "publish_approval_status_change"),
        patch.object(_mod, "generate_code", return_value="# code\ndf = spark.read.json('s3://b/k')\n"),
        patch.object(_mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}),
        patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
        # The resolver builds its own lazily-cached client; patch the accessor on
        # the module the handler actually holds (see the docstring's hazard note).
        patch.object(target_schema_resolution, "_s3_client", lambda: s3 or MagicMock()),
    ]
    if load_prompt_template is not None:
        patches.append(patch.object(_mod, "load_prompt_template", load_prompt_template))
    return patches


def _event(**overrides):
    event = {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": MAPPING_ID}
    event.update(overrides)
    return event


def _run(patches, event=None):
    with _nested(patches):
        return _mod.lambda_handler(event or _event(), None)


class _nested:
    """Enter a list of context managers, exit them in reverse."""

    def __init__(self, managers):
        self._managers = managers

    def __enter__(self):
        self._entered = []
        for manager in self._managers:
            manager.__enter__()
            self._entered.append(manager)
        return self

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


# ── Requirement 2.9: a missing schema hash fails, naming the attribute ──────


@pytest.mark.parametrize("absent", ["source_schema_hash", "target_schema_hash"])
def test_absent_schema_hash_fails_naming_the_attribute(absent):
    """A mapping record missing either hash fails the step and NAMES the absent
    attribute — no random UUID stands in for it.
    **Validates: Requirements 2.9**
    """
    patches = _handler_patches(
        mapping_item=_mapping_item(**{absent: _ABSENT}),
        pipeline_metadata={},
    )
    with _nested(patches), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(_event(), None)
    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert absent in exc.message
    assert MAPPING_ID in exc.message
    assert exc.details["missing_attribute"] == absent
    assert exc.details["mapping_id"] == MAPPING_ID


@pytest.mark.parametrize("absent", ["source_schema_hash", "target_schema_hash"])
def test_empty_schema_hash_fails_like_an_absent_one(absent):
    """An empty-string hash cannot identify a schema either, so it fails the same
    way rather than being written through as an identity.
    **Validates: Requirements 2.9**
    """
    patches = _handler_patches(
        mapping_item=_mapping_item(**{absent: ""}),
        pipeline_metadata={},
    )
    with _nested(patches), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(_event(), None)
    assert exc_info.value.details["missing_attribute"] == absent


def test_present_hashes_are_recorded_verbatim_not_regenerated():
    """The hashes the mapping record carries reach the generated-code record
    unchanged — the identity is the mapping's, not a fresh one.
    **Validates: Requirements 2.9**
    """
    code_table = MagicMock()
    patches = _handler_patches(mapping_item=_mapping_item(), pipeline_metadata={})
    patches = [p for p in patches if getattr(p, "attribute", None) != "generated_code_table"]
    patches.append(patch.object(_mod, "generated_code_table", code_table))
    result = _run(patches)

    assert result["statusCode"] == 200
    written = code_table.put_item.call_args.kwargs["Item"]
    assert written["source_schema_id"] == "src-hash-abc"
    assert written["target_schema_id"] == "tgt-hash-def"


# ── Requirement 2.1: the target schema comes from the pipeline record ───────


def test_target_schema_is_read_from_the_pipeline_record_not_the_mapping():
    """The resolved schema is the S3 object the PIPELINE record names, and the
    mappings record's ``target_schema`` attribute is not consulted at all.
    **Validates: Requirements 2.1**
    """
    s3 = _s3_returning(json.dumps(_TARGET_SCHEMA_BODY).encode("utf-8"))
    rendered: dict = {}

    def _capture_prompt(template_name, **kwargs):
        rendered[template_name] = kwargs
        return "prompt"

    patches = _handler_patches(
        # A decoy on the mappings record: if it were still the source, the
        # assertion below would see `decoy_only` instead of the real fields.
        mapping_item=_mapping_item(target_schema={"properties": {"decoy_only": {"type": "string"}}}),
        pipeline_metadata={
            "target_schema_bucket": "adi-target-schemas-123456789012-us-east-1",
            "target_schema_s3_key": f"{PIPELINE_ID}/target.json",
        },
        s3=s3,
        # A target type outside TEMPLATE_RULES routes to the full-AI fallback,
        # whose `code_generation_enhanced.j2` render is the consumer this test
        # observes the resolved value through.
        #
        # COMMENT AMENDED BY TASK 4.7, not an assertion change: this said the
        # fallback was "the ONLY live consumer of `target_schema` in this
        # handler today". It no longer is — 4.7 threads the resolved schema into
        # the template-first path's transform prompt as well
        # (`pm["target_schema"]` → `code_assembly` → `transform_generation.j2`),
        # which is the spec's load-bearing change. That path is asserted in
        # test_target_schema_in_transform_prompt.py; this test keeps using the
        # fallback because `load_prompt_template` capture is the cheapest place
        # to read the resolved value back.
        target_config={"type": "redshift"},
        load_prompt_template=MagicMock(side_effect=_capture_prompt),
    )
    patches.append(patch.object(_mod, "Agent", MagicMock(return_value=MagicMock(return_value="# code"))))
    patches.append(patch.object(_mod, "extract_code_only", return_value="# code\ndf = spark.read.json('s3://b/k')\n"))
    result = _run(patches)

    assert result["statusCode"] == 200
    s3.get_object.assert_called_once_with(
        Bucket="adi-target-schemas-123456789012-us-east-1",
        Key=f"{PIPELINE_ID}/target.json",
    )
    prompt_target_schema = json.loads(rendered["code_generation_enhanced.j2"]["target_schema"])
    assert set(prompt_target_schema["properties"]) == {"customer_id", "order_total"}
    assert "decoy_only" not in prompt_target_schema.get("properties", {})


def test_no_target_schema_key_resolves_to_no_schema_without_failing():
    """A pipeline that genuinely has no target schema still generates code — the
    absent key is the one input that is not an error (requirement 2.6's state).
    **Validates: Requirements 2.1**
    """
    s3 = MagicMock()
    patches = _handler_patches(mapping_item=_mapping_item(), pipeline_metadata={}, s3=s3)
    result = _run(patches)

    assert result["statusCode"] == 200
    s3.get_object.assert_not_called()


def test_key_without_bucket_fails_naming_both_attributes():
    """A half-written pipeline record fails the step rather than being read as a
    pipeline without a target schema (requirement 2.7, ``LGR-7c9378d-1``).
    **Validates: Requirements 2.1**
    """
    patches = _handler_patches(
        mapping_item=_mapping_item(),
        pipeline_metadata={"target_schema_s3_key": f"{PIPELINE_ID}/target.json"},
    )
    with _nested(patches), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(_event(), None)
    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert "target_schema_s3_key" in exc.message
    assert "target_schema_bucket" in exc.message


def test_a_throttle_on_the_target_schema_read_does_not_present_as_no_schema():
    """An infrastructure error propagates as transient instead of degrading to
    the no-schema path (requirement 2.8's classification, reached through the
    shared resolver).
    **Validates: Requirements 2.1**
    """
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError(
        {"Error": {"Code": "ThrottlingException", "Message": "slow down"}}, "GetObject"
    )
    patches = _handler_patches(
        mapping_item=_mapping_item(),
        pipeline_metadata={
            "target_schema_bucket": "adi-target-schemas-123456789012-us-east-1",
            "target_schema_s3_key": f"{PIPELINE_ID}/target.json",
        },
        s3=s3,
    )
    with _nested(patches), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(_event(), None)
    assert type(exc_info.value).__name__ == "TransientStepFailure"
    assert exc_info.value.error_type == "transient"


def test_the_pipeline_record_is_read_once_and_reused():
    """The resolver runs over the record the handler already fetched for
    ``target_config`` — no second get_item, so the two reads cannot disagree.
    **Validates: Requirements 2.1**
    """
    metadata_reader = MagicMock(return_value={"target_config": _TEMPLATE_TARGET_CONFIG})
    patches = _handler_patches(mapping_item=_mapping_item(), pipeline_metadata={})
    patches = [p for p in patches if getattr(p, "attribute", None) != "get_pipeline_metadata"]
    patches.append(patch.object(_mod, "get_pipeline_metadata", metadata_reader))
    result = _run(patches)

    assert result["statusCode"] == 200
    assert metadata_reader.call_count == 1


# ── Requirement 3.11: the mapping_id UUID default is deliberately unchanged ──


def test_mapping_id_uuid_default_is_still_superseded_by_the_lookup():
    """An event with no ``mapping_id`` still fails at the mapping lookup, naming
    the unresolvable id. The ``str(uuid.uuid4())`` default at the top of the
    handler stays: it is superseded here, so it is not the same defect as the
    schema-hash defaults, and requirement 3.11 preserves this behaviour.
    **Validates: Requirements 3.11**
    """
    patches = _handler_patches(mapping_item=None, pipeline_metadata={})
    event = _event()
    del event["mapping_id"]
    with _nested(patches), pytest.raises(_mod.StepFailure) as exc_info:
        _mod.lambda_handler(event, None)
    exc = exc_info.value
    assert exc.step_name == "create_code"
    assert "not found" in exc.message
    # The generated id is what the message names — proof the default survived
    # and that the lookup, not the default, is what decides the outcome.
    assert exc.details["mapping_id"]
