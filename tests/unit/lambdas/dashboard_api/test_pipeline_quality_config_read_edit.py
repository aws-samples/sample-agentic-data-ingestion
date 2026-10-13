# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p64 R7.4 — the Quality_Config's read and write halves on an EXISTING pipeline.

Feature: p64-reliability-report-remediation (gap closure)

**Validates: Requirements 7.4**

p64 shipped per-pipeline quality settings **create-only**. Three facts, each
verified rather than assumed:

1. ``edit_pipeline`` had branches for ``target_schema``, ``ai_model``,
   ``pipeline_name``, ``target_config``, ``approval_config``, ``schedule`` and
   ``mapping_context`` — and **no** ``quality_config`` branch. A setting could be
   chosen once, at creation, and never changed.
2. ``get_pipeline_details`` projected **neither** ``quality_config`` nor
   ``approval_config``, on either of its two arms.
3. ``hydrateWizardStateFromPipeline`` (task 2c.2) implements the client half of
   the round trip correctly and had no production producer to read from.

``PipelineFormModal.tsx``'s ``isEdit`` branch recorded the order to fix this in —
"Add the handler branch first, then the control" — because a toggle over a
handler with no branch renders, accepts a click, submits, and changes nothing.
This module guards the handler branch.

## What is load-bearing here, and why each test exists

**The merge is per key.** A whole-map ``SET quality_config = :qc`` would make
every submission a replacement, so a client sending only ``dedup_enabled`` would
erase ``dedup_columns`` — and erase an explicit ``data_quality_enabled``, putting
the pipeline back on its template default. That is the silent reclassification
R6.6 forbids, arriving through the edit path.

**Absence is a state, not a missing value.** ``_default_quality_config``
deliberately seeds no ``data_quality_enabled`` because absence means "use the
template's own default" — on for ``glue_job_s3_tables.py.j2``, off for the other
four (``LGR-b993b94-7``, template / ratified; ``LGR-b993b94-5`` for the resolver
that reads it). So an edit that does not mention the key must not write it, and
the read must not manufacture one.

**Malformed values are refused at the boundary, not clamped and not left to the
reader.** Task 2a.2's ratified position (``LGR-b993b94-2``; ``LGR-4ea43f3-4`` on
why a silently-altered value is worse than a refused one). The edit path reuses
``_validate_quality_config`` rather than carrying a second validator, so the two
endpoints cannot drift into two policies for one wire shape.

**``null`` is refused, on both endpoints.** It is not given a second meaning
("revert to the template default") on the edit path. Doing so would put a wire
value on one endpoint that the other 400s, and reverting is a product question —
whether the UI offers "use the template default" at all — rather than plumbing.
``buildQualityConfigRequest`` (``CreatePipelinePage.tsx:281-283``) omits the key
entirely when it is not a boolean, so no live client sends ``null``.

**Both read arms are asserted.** The no-state-data arm is the one a pipeline that
has never run takes — i.e. the pipeline most likely to be edited — so projecting
only the enriched arm would make hydration work on the rare case and fail on the
common one, which looks intermittent rather than broken.

Region is pinned by ``tests/unit/conftest.py`` (LGR-3e45e65-1, template /
ratified) — this module deliberately does not call ``os.environ.setdefault``.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# Stub out Lambda-layer dependencies before importing the handler package.
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_audit_utils = MagicMock()
mock_audit_utils.extract_request_context = MagicMock(return_value={})
mock_audit_utils.write_audit_entry = MagicMock()
sys.modules["audit_utils"] = mock_audit_utils

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: {
    "UPLOAD_BUCKET": "agentic-data-ingestion-source",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    "PIPELINE_TRIGGER_ARN": "",
}.get(name, default or name)
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = mock_env_utils

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)

try:
    from handlers import pipelines_crud as pipelines

    # Import through the re-export the router actually binds.
    from handlers.pipelines import edit_pipeline, get_pipeline_details
    from handlers.pipelines_crud import APPROVAL_CONFIG_KEY

    # The ONE definition of these attribute names, shared with `code_assembly`, which
    # is the reader (LGR-eee8ac8-5). Imported, never transcribed: a second spelling
    # would not fail, it would write an attribute nothing reads.
    from quality_verdict import (
        DATA_QUALITY_ENABLED_KEY,
        DEDUP_COLUMNS_KEY,
        DEDUP_ENABLED_KEY,
        NONPRINTABLE_CHECK_ENABLED_KEY,
        QUALITY_CONFIG_KEY,
    )
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "quality-config-read-edit-pipeline"

mock_pipelines_table = MagicMock()
mock_state_table = MagicMock()
mock_executions_table = MagicMock()
mock_approvals_table = MagicMock()
mock_mappings_table = MagicMock()
mock_s3_client = MagicMock()
mock_dynamodb = MagicMock()


@pytest.fixture(autouse=True)
def _reset_mocks():
    for m in (
        mock_pipelines_table,
        mock_state_table,
        mock_executions_table,
        mock_approvals_table,
        mock_mappings_table,
        mock_s3_client,
        mock_dynamodb,
    ):
        m.reset_mock()

    mock_state_table.query.return_value = {"Items": []}
    mock_executions_table.query.return_value = {"Items": []}

    originals = {
        "pipelines_table": pipelines.pipelines_table,
        "state_table": pipelines.state_table,
        "executions_table": pipelines.executions_table,
        "approvals_table": pipelines.approvals_table,
        "mappings_table": pipelines.mappings_table,
        "dynamodb": pipelines.dynamodb,
        "s3_client": pipelines.s3_client,
    }
    pipelines.pipelines_table = mock_pipelines_table
    pipelines.state_table = mock_state_table
    pipelines.executions_table = mock_executions_table
    pipelines.approvals_table = mock_approvals_table
    pipelines.mappings_table = mock_mappings_table
    pipelines.dynamodb = mock_dynamodb
    pipelines.s3_client = mock_s3_client

    original_require_env = pipelines.require_env

    def _patched_require_env(name, default=None):
        overrides = {
            "UPLOAD_BUCKET": "agentic-data-ingestion-source",
            "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
            "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
            "PIPELINE_TRIGGER_ARN": "",
        }
        if name in overrides:
            return overrides[name]
        return original_require_env(name, default)

    pipelines.require_env = _patched_require_env
    yield
    pipelines.require_env = original_require_env
    for name, value in originals.items():
        setattr(pipelines, name, value)


# ─── Helpers ───────────────────────────────────────────────────────────────


def _nested_path(key: str) -> str:
    """The document path `edit_pipeline` derives for one Quality_Config key."""
    return f"{QUALITY_CONFIG_KEY}.#qc_{key} = :qc_{key}"


def _edit(quality_config, **extra_body):
    """Edit with a `quality_config` body; return the main update kwargs.

    The handler issues a separate `if_not_exists` bootstrap call before the
    document-path SET, so the write under test is the *second* `update_item`.
    """
    body = {QUALITY_CONFIG_KEY: quality_config, **extra_body}
    result = edit_pipeline(PIPELINE_ID, json.dumps(body), event={})
    assert result["statusCode"] == 200, result
    assert mock_pipelines_table.update_item.call_count == 2
    return mock_pipelines_table.update_item.call_args_list[1][1]


def _edit_refused(quality_config):
    """Edit with a malformed `quality_config`; assert a 400 and return the body."""
    body = json.dumps({QUALITY_CONFIG_KEY: quality_config})
    result = edit_pipeline(PIPELINE_ID, body, event={})
    assert result["statusCode"] == 400, result
    return json.loads(result["body"])


def _record(**extra) -> dict:
    item = {
        "pipeline_id": PIPELINE_ID,
        "pipeline_name": "Quality Pipeline",
        "status": "active",
        "target_schema_name": "test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
    }
    item.update(extra)
    return item


def _seed_no_state(**extra) -> None:
    """The no-state-data arm: a pipeline that exists but has never run."""
    mock_state_table.query.return_value = {"Items": []}
    mock_pipelines_table.get_item.return_value = {"Item": _record(**extra)}


def _seed_with_state(**extra) -> None:
    """The enriched arm: a pipeline with state rows, i.e. one that has run."""
    mock_state_table.query.return_value = {
        "Items": [
            {
                "pipeline_id": PIPELINE_ID,
                "step_name": "file_validation",
                "timestamp": "2026-01-01T00:00:00Z",
                "status": "success",
                "original_s3_uri": f"s3://bucket/{PIPELINE_ID}/data.csv",
                "run_id": "run-1",
            }
        ]
    }
    mock_pipelines_table.get_item.return_value = {"Item": _record(**extra)}


def _details_body() -> dict:
    result = get_pipeline_details(PIPELINE_ID)
    assert result["statusCode"] == 200, result
    return json.loads(result["body"])


# ─── The write half ────────────────────────────────────────────────────────


class TestEditPersistsQualityConfig:
    """`edit_pipeline` has a `quality_config` branch, and it writes booleans.

    **Validates: Requirements 7.4**
    """

    def test_dedup_enabled_true_lands_as_a_json_boolean(self):
        kwargs = _edit({DEDUP_ENABLED_KEY: True})

        assert _nested_path(DEDUP_ENABLED_KEY) in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeNames"][f"#qc_{DEDUP_ENABLED_KEY}"] == DEDUP_ENABLED_KEY

        written = kwargs["ExpressionAttributeValues"][f":qc_{DEDUP_ENABLED_KEY}"]
        assert written is True
        assert isinstance(written, bool)

    def test_data_quality_enabled_false_lands_as_a_json_boolean(self):
        """An explicit opt-OUT is honoured and stays a boolean.

        This is the value the resolver keys off: `resolve_data_quality_enabled`
        honours an explicit `bool` and otherwise falls back to the per-template
        default, so a coerced `"False"` would resolve an S3 Tables pipeline back
        to DQ **on** for an operator who had just switched it off.
        """
        kwargs = _edit({DATA_QUALITY_ENABLED_KEY: False})

        written = kwargs["ExpressionAttributeValues"][f":qc_{DATA_QUALITY_ENABLED_KEY}"]
        assert written is False
        assert isinstance(written, bool)
        assert written != "False"

    def test_dedup_columns_list_survives_verbatim(self):
        kwargs = _edit({DEDUP_COLUMNS_KEY: ["order_id", "customer_id"]})

        assert kwargs["ExpressionAttributeValues"][f":qc_{DEDUP_COLUMNS_KEY}"] == [
            "order_id",
            "customer_id",
        ]

    def test_the_map_is_seeded_before_the_nested_set(self):
        """`if_not_exists` runs first, in its own call.

        Overlapping document paths are illegal in one expression, so the map has
        to be created separately — the same two-call shape `approval_config`
        uses. Without it, a nested SET on a pipeline created before this field
        existed would fail.
        """
        _edit({DEDUP_ENABLED_KEY: True})

        bootstrap = mock_pipelines_table.update_item.call_args_list[0][1]
        assert f"if_not_exists({QUALITY_CONFIG_KEY}" in bootstrap["UpdateExpression"]
        assert bootstrap["ExpressionAttributeValues"] == {":empty_map": {}}


class TestPerKeyMerge:
    """A partial submission cannot erase the keys it does not mention.

    **Validates: Requirements 7.4**
    """

    def test_whole_map_overwrite_never_happens(self):
        """The nested path is taken; there is no `quality_config = :qc` arm.

        A whole-map SET would let a client that submits only `dedup_enabled`
        erase `dedup_columns` and an explicit `data_quality_enabled`.
        """
        kwargs = _edit({DEDUP_ENABLED_KEY: True})

        assert f"{QUALITY_CONFIG_KEY} = :qc," not in kwargs["UpdateExpression"]
        assert ":qc" not in kwargs["ExpressionAttributeValues"]
        assert _nested_path(DEDUP_ENABLED_KEY) in kwargs["UpdateExpression"]

    def test_only_submitted_keys_appear_in_the_write(self):
        """Two of the four keys submitted; the other two are absent from the write.

        This is the property that makes the merge a merge: DynamoDB leaves a
        nested key alone when no path names it.
        """
        kwargs = _edit({DEDUP_ENABLED_KEY: True, DEDUP_COLUMNS_KEY: ["sku"]})
        expr = kwargs["UpdateExpression"]

        assert _nested_path(DEDUP_ENABLED_KEY) in expr
        assert _nested_path(DEDUP_COLUMNS_KEY) in expr
        assert f"#qc_{DATA_QUALITY_ENABLED_KEY}" not in expr
        assert f"#qc_{NONPRINTABLE_CHECK_ENABLED_KEY}" not in expr
        assert f":qc_{DATA_QUALITY_ENABLED_KEY}" not in kwargs["ExpressionAttributeValues"]

    def test_absent_data_quality_enabled_is_not_written(self):
        """The load-bearing absence survives an edit that does not mention it.

        `LGR-b993b94-7`: absence means "use the template's own default". An edit
        that wrote `false` for an untouched control would move an existing S3
        Tables pipeline's verdict from `enforced` to `advisory`, which R6.6
        forbids.
        """
        kwargs = _edit({DEDUP_ENABLED_KEY: False})

        assert f"#qc_{DATA_QUALITY_ENABLED_KEY}" not in kwargs["UpdateExpression"]
        for value in kwargs["ExpressionAttributeValues"].values():
            assert value is not None

    def test_a_body_without_quality_config_writes_no_quality_path(self):
        """An unrelated edit does not touch the sub-object, or seed its map."""
        result = edit_pipeline(PIPELINE_ID, json.dumps({"pipeline_name": "Renamed"}), event={})
        assert result["statusCode"] == 200, result

        assert mock_pipelines_table.update_item.call_count == 1
        expr = mock_pipelines_table.update_item.call_args_list[0][1]["UpdateExpression"]
        assert QUALITY_CONFIG_KEY not in expr


class TestEditBoundaryRefusal:
    """Malformed values are a 400, and nothing is written.

    **Validates: Requirements 7.4**
    """

    @pytest.mark.parametrize(
        "key",
        [DATA_QUALITY_ENABLED_KEY, DEDUP_ENABLED_KEY, NONPRINTABLE_CHECK_ENABLED_KEY],
    )
    def test_non_boolean_under_any_boolean_key_is_refused(self, key):
        """All three booleans in `_QUALITY_CONFIG_BOOLEAN_KEYS`, not just two.

        A submitted `"true"` is an opt-in that silently does nothing at the
        reader, and that is the one direction the resolver cannot report.
        """
        body = _edit_refused({key: "true"})
        assert key in body["error"]

    def test_integer_one_is_refused_too(self):
        """`isinstance(1, bool)` is False, deliberately.

        `0` is falsy and would read as "off" at a truthiness site while `1`
        would read as "on", neither having passed a boolean check.
        """
        _edit_refused({DEDUP_ENABLED_KEY: 1})

    def test_explicit_null_is_refused_not_treated_as_a_revert(self):
        """The recorded decision: `null` does NOT mean "revert to the template default".

        Giving it that meaning here would put a wire value on the edit endpoint
        that `create_pipeline` 400s, which is the "second spelling with nothing
        to arbitrate between them" class `LGR-b993b94-1` rejected. Reverting is a
        product question, not plumbing. `buildQualityConfigRequest` omits the key
        rather than sending `null`, so no live client is affected.
        """
        body = _edit_refused({DATA_QUALITY_ENABLED_KEY: None})
        assert DATA_QUALITY_ENABLED_KEY in body["error"]

    def test_bad_dedup_column_is_refused_never_filtered(self):
        """An offending entry 400s; it is not silently dropped.

        These names are interpolated into the generated Glue job's
        `dropDuplicates` call, so `LGR-4ea43f3-4`'s reasoning applies one value
        class over: a dedup key list quietly missing a column deduplicates on
        the wrong identity.
        """
        body = _edit_refused({DEDUP_COLUMNS_KEY: ["order_id", "1; DROP TABLE"]})
        assert DEDUP_COLUMNS_KEY in body["error"]

    def test_non_dict_quality_config_is_refused(self):
        _edit_refused("enabled")

    def test_a_refused_body_writes_nothing_at_all(self):
        """Not even the `if_not_exists` map seeding runs.

        The validator is called before any write, so a refused edit leaves the
        record byte-identical rather than creating an empty `quality_config` map
        on its way to a 400.
        """
        _edit_refused({DEDUP_ENABLED_KEY: "yes"})
        assert mock_pipelines_table.update_item.call_count == 0


# ─── The read half ─────────────────────────────────────────────────────────


class TestDetailsProjectsQualityConfig:
    """`get_pipeline_details` reports both governance sub-objects, on both arms.

    **Validates: Requirements 7.4**
    """

    def test_no_state_arm_projects_quality_config(self):
        """The arm a never-run pipeline takes — the one most likely to be edited."""
        stored = {DEDUP_ENABLED_KEY: True, DEDUP_COLUMNS_KEY: ["order_id"]}
        _seed_no_state(**{QUALITY_CONFIG_KEY: stored})

        assert _details_body()[QUALITY_CONFIG_KEY] == stored

    def test_enriched_arm_projects_quality_config(self):
        stored = {DEDUP_ENABLED_KEY: True, DEDUP_COLUMNS_KEY: ["order_id"]}
        _seed_with_state(**{QUALITY_CONFIG_KEY: stored})

        assert _details_body()[QUALITY_CONFIG_KEY] == stored

    def test_no_state_arm_projects_approval_config(self):
        """Additive: `Pipeline.approval_config` in `types.ts` had a dead declaration."""
        stored = {"code_generation_enabled": False, "schema_mapping_enabled": True}
        _seed_no_state(**{APPROVAL_CONFIG_KEY: stored})

        assert _details_body()[APPROVAL_CONFIG_KEY] == stored

    def test_enriched_arm_projects_approval_config(self):
        stored = {"code_generation_enabled": False}
        _seed_with_state(**{APPROVAL_CONFIG_KEY: stored})

        assert _details_body()[APPROVAL_CONFIG_KEY] == stored

    def test_absent_keys_are_omitted_not_defaulted(self):
        """Membership, not a default. A manufactured `{}` is a claim the record does not make."""
        _seed_no_state()
        body = _details_body()

        assert QUALITY_CONFIG_KEY not in body
        assert APPROVAL_CONFIG_KEY not in body

    def test_absent_keys_are_omitted_on_the_enriched_arm_too(self):
        _seed_with_state()
        body = _details_body()

        assert QUALITY_CONFIG_KEY not in body
        assert APPROVAL_CONFIG_KEY not in body

    def test_an_empty_stored_map_is_projected_not_dropped(self):
        """`in`, NOT `.get()` — a truth test would collapse `{}` into absence.

        A stored `{}` is a recorded "no overrides"; an absent attribute is
        "nothing was ever asked for". The client's three-state reader
        (`utils/quality-config.ts`, `LGR-b993b94-9`) distinguishes them, so the
        response must mirror the record's membership exactly.
        """
        _seed_no_state(**{QUALITY_CONFIG_KEY: {}})
        body = _details_body()

        assert QUALITY_CONFIG_KEY in body
        assert body[QUALITY_CONFIG_KEY] == {}

    def test_a_map_without_data_quality_enabled_projects_without_it(self):
        """The read does not manufacture the key whose absence is load-bearing."""
        _seed_no_state(**{QUALITY_CONFIG_KEY: {DEDUP_ENABLED_KEY: False, DEDUP_COLUMNS_KEY: []}})

        projected = _details_body()[QUALITY_CONFIG_KEY]
        assert DATA_QUALITY_ENABLED_KEY not in projected


class TestRoundTrip:
    """What is written by an edit is what a subsequent read reports.

    **Validates: Requirements 7.4**
    """

    def test_edited_keys_come_back_out_of_details(self):
        """The two halves agree on the attribute name and the value's type.

        Writer and reader are only ever compared through `QUALITY_CONFIG_KEY`
        (LGR-eee8ac8-5), so this is the test that would fail if either side
        transcribed a second spelling instead of importing the constant.
        """
        kwargs = _edit({DEDUP_ENABLED_KEY: True, DEDUP_COLUMNS_KEY: ["sku"]})

        # Reconstruct the stored map from the write, then serve it back.
        stored = {
            kwargs["ExpressionAttributeNames"][f"#qc_{key}"]: kwargs["ExpressionAttributeValues"][f":qc_{key}"]
            for key in (DEDUP_ENABLED_KEY, DEDUP_COLUMNS_KEY)
        }
        mock_pipelines_table.reset_mock()
        _seed_no_state(**{QUALITY_CONFIG_KEY: stored})

        assert _details_body()[QUALITY_CONFIG_KEY] == {
            DEDUP_ENABLED_KEY: True,
            DEDUP_COLUMNS_KEY: ["sku"],
        }
