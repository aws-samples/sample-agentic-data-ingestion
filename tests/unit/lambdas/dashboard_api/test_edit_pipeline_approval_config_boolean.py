# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R7.3 regression guard — the pipeline-edit path writes the Code_Approval_Setting as a boolean.

Feature: p34-code-approval-gate-honors-config

**Validates: Requirements 7.3**

R7.3 is a statement about *existing, verified* behaviour, not an implementation
task, so this module is a regression guard rather than a change. Design §2 walked
the write path end to end and confirmed it already works:

    dashboard-react/src/components/PipelineFormModal.tsx:103-106
        sends approval_config.code_generation_enabled as a real JS boolean
    dashboard-react/src/hooks/use-pipeline-mutations.ts:20-23
        types the field `code_generation_enabled?: boolean`
    lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_crud.py:1140-1157
        writes it as a nested document-path SET on the approval_config map

What is guarded here is the last hop: that `edit_pipeline` emits
`approval_config.#ac_code_generation_enabled = :ac_code_generation_enabled` with
the JSON boolean intact, for `false` as well as for `true`. `false` is the
load-bearing value — R1.3 turns the gate off only on an explicit boolean `false`,
and R1.4 treats every other value, `"false"` included, as `true`. A write that
coerced the boolean to a string would silently leave the gate on for a pipeline
owner who had switched it off.

The nested-path form matters too, and is asserted: writing the whole map
(`pipelines_crud.py:1158-1160`, the non-dict branch) would clobber the sibling
`schema_mapping_enabled` / threshold keys that `create_pipeline` seeds at `:903`.

**The caveat design §2 recorded at `pipelines_crud.py:1157` — SUPERSEDED, and
the reason is worth reading before trusting any older copy of this note.** That
line was

    expr_values[placeholder] = value

and it was the whole of the handling: nothing inspected, coerced or rejected the
value, so a request-body value reached the DynamoDB write **unvalidated**. The
dashboard cannot produce a non-boolean — `PipelineFormModal` passes React state
that is only ever `true` or `false` — but a direct API caller can, and that was
the route by which R1.4/R1.5's malformed case was reachable at all.

This module used to pin that reachability, and instructed that it "must not grow
an assertion that `pipelines_crud` rejects a non-boolean", on the premise that
the malformed case is resolved on the read side by design Property 1 on
`test_code.resolve_code_approval_setting` (only an explicit boolean `false`
disables the gate). **That premise holds for `code_generation_enabled` and fails
for the other two gates**, which is why the position moved: `schema_mapping`
reads `schema_mapping_enabled` (`lambda_function.py:1669`) and
`always_approve_schema_changes` (`:1657`) by **truthiness**, so `0`, `""`, `[]`
or `None` under either key disabled a human review gate silently, having passed
no boolean check anywhere. p64 closed that at the boundary with
`_validate_approval_config`, on `create_pipeline` first and now on
`edit_pipeline` too.

So the malformed value no longer reaches the write: it is a 400.
`TestMalformedValueRefusedAtTheBoundary` below is the retargeted guard — same
input, opposite expectation, and it is deliberately in this module because this
is where the superseded claim lived. Validation is asserted here; the *policy*
behind it belongs to `_validate_approval_config`'s docstring, which is the thing
to read before relaxing it.

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

# edit_pipeline lives in handlers.pipelines_crud (P21 Task 7.2) and is re-exported
# by handlers.pipelines, which is the name the router binds. Import through the
# re-export so this exercises the same object the router calls.
try:
    from handlers import pipelines_crud
    from handlers.pipelines import edit_pipeline
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "test-approval-config-boolean-pipeline"

# The nested document path pipelines_crud.py:1150-1155 derives for this key.
CODE_GEN_PATH = "approval_config.#ac_code_generation_enabled = :ac_code_generation_enabled"
CODE_GEN_NAME = "#ac_code_generation_enabled"
CODE_GEN_VALUE = ":ac_code_generation_enabled"

mock_pipelines_table = MagicMock()


@pytest.fixture(autouse=True)
def _wire_mock_table():
    """Point the handler's module-global table at a mock and reset it per test."""
    mock_pipelines_table.reset_mock()
    original_table = pipelines_crud.pipelines_table
    pipelines_crud.pipelines_table = mock_pipelines_table
    yield
    pipelines_crud.pipelines_table = original_table


def _edit(approval_config):
    """Call edit_pipeline with an approval_config body and return the main update kwargs.

    ``pipelines_crud.py:1144-1148`` issues a separate ``if_not_exists`` bootstrap
    call before the document-path SET, so the write under test is the *second*
    ``update_item`` call.
    """
    result = edit_pipeline(PIPELINE_ID, json.dumps({"approval_config": approval_config}), event={})
    assert result["statusCode"] == 200, result
    assert mock_pipelines_table.update_item.call_count == 2
    return mock_pipelines_table.update_item.call_args_list[1][1]


class TestCodeApprovalSettingWrittenAsBoolean:
    """The Code_Approval_Setting survives the edit path as a JSON boolean.

    **Validates: Requirements 7.3**
    """

    def test_enabled_true_written_as_json_boolean(self):
        """code_generation_enabled=true lands on the nested path as the boolean True."""
        kwargs = _edit({"code_generation_enabled": True})

        assert CODE_GEN_PATH in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeNames"][CODE_GEN_NAME] == "code_generation_enabled"

        written = kwargs["ExpressionAttributeValues"][CODE_GEN_VALUE]
        assert written is True
        assert isinstance(written, bool)

    def test_disabled_false_written_as_json_boolean(self):
        """code_generation_enabled=false lands as the boolean False, not a truthy string.

        This is the value R1.3 keys off. Coercing it to ``"False"`` would leave
        the gate on for an owner who switched it off, because R1.4 resolves every
        non-boolean to ``true``.
        """
        kwargs = _edit({"code_generation_enabled": False})

        assert CODE_GEN_PATH in kwargs["UpdateExpression"]

        written = kwargs["ExpressionAttributeValues"][CODE_GEN_VALUE]
        assert written is False
        assert isinstance(written, bool)
        assert written != "False"

    def test_nested_path_write_does_not_replace_the_whole_map(self):
        """The dict branch (`:1142-1157`) is taken, never the whole-map SET at `:1158-1160`.

        A whole-map write would drop the sibling keys create_pipeline seeds at
        `pipelines_crud.py:903` — schema_mapping_enabled, the confidence
        threshold and always_approve_schema_changes.
        """
        kwargs = _edit({"code_generation_enabled": False})

        assert "approval_config = :ac," not in kwargs["UpdateExpression"]
        assert ":ac" not in kwargs["ExpressionAttributeValues"]
        assert CODE_GEN_PATH in kwargs["UpdateExpression"]

    def test_sibling_keys_get_their_own_paths(self):
        """Both approval keys the edit modal sends are written as separate document paths."""
        kwargs = _edit({"schema_mapping_enabled": True, "code_generation_enabled": False})

        assert CODE_GEN_PATH in kwargs["UpdateExpression"]
        assert "approval_config.#ac_schema_mapping_enabled = :ac_schema_mapping_enabled" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][CODE_GEN_VALUE] is False
        assert kwargs["ExpressionAttributeValues"][":ac_schema_mapping_enabled"] is True

    def test_bootstrap_call_creates_the_map_before_the_nested_set(self):
        """A record with no approval_config map still receives the nested write.

        `pipelines_crud.py:1144-1148` runs an ``if_not_exists`` SET on the
        top-level attribute first, which is what lets the R7.2 pre-change record
        shape — no ``approval_config`` key at all — be edited.
        """
        _edit({"code_generation_enabled": False})

        bootstrap = mock_pipelines_table.update_item.call_args_list[0][1]
        assert bootstrap["UpdateExpression"] == "SET approval_config = if_not_exists(approval_config, :empty_map)"
        assert bootstrap["ExpressionAttributeValues"] == {":empty_map": {}}
        assert bootstrap["Key"] == {"pipeline_id": PIPELINE_ID}


def _edit_expecting_refusal(approval_config):
    """Call edit_pipeline with a malformed approval_config and return the response.

    Deliberately does NOT go through :func:`_edit`, which asserts a 200 and
    asserts two ``update_item`` calls. The point of a boundary refusal is that
    neither happens.
    """
    return edit_pipeline(PIPELINE_ID, json.dumps({"approval_config": approval_config}), event={})


class TestMalformedValueRefusedAtTheBoundary:
    """A non-boolean gate value is a 400, and nothing is written.

    Retargets this module's original ``TestMalformedValueReachability``, which
    pinned the value reaching the write unvalidated. See the module docstring for
    why that position was superseded rather than simply relaxed: read-side
    fail-closed covers ``code_generation_enabled`` and does **not** cover the two
    gates ``schema_mapping`` reads by truthiness.

    **Validates: Requirements 7.3**
    """

    def test_non_boolean_gate_value_is_refused(self):
        """The string ``"false"`` is a 400, not a write.

        ``"false"`` is the load-bearing case: truthy in Python, and not the JSON
        boolean ``false``, so an owner submitting it means "off" and — before this
        refusal — got a 200 for a value that read as "on" at two of the three
        gates.
        """
        result = _edit_expecting_refusal({"code_generation_enabled": "false"})

        assert result["statusCode"] == 400, result
        assert "code_generation_enabled" in json.loads(result["body"])["error"]

    def test_refusal_writes_nothing_at_all(self):
        """The refusal precedes the `if_not_exists` map-seeding call.

        The bootstrap write at ``pipelines_crud.py``'s ``approval_config`` branch
        is a real mutation — it creates an empty map on a record that had none —
        so a validator running after it would leave a record altered on its way to
        a 400.
        """
        _edit_expecting_refusal({"code_generation_enabled": "false"})

        assert mock_pipelines_table.update_item.call_count == 0

    @pytest.mark.parametrize("falsy", [0, "", [], None])
    def test_falsy_non_boolean_disabling_a_truthiness_gate_is_refused(self, falsy):
        """The case read-side fail-closed does NOT cover.

        ``schema_mapping_enabled`` is read as
        ``if not approval_config.get("schema_mapping_enabled", True):``
        (``schema_mapping/lambda_function.py:1669``), so each of these silently
        disabled schema-mapping approval. That is the gap this module's superseded
        note did not account for.
        """
        result = _edit_expecting_refusal({"schema_mapping_enabled": falsy})

        assert result["statusCode"] == 400, result
        assert mock_pipelines_table.update_item.call_count == 0

    def test_unrecognised_keys_are_still_accepted(self):
        """Value validation is not key filtering — an older Lambda must not 400 a newer client.

        ``_validate_approval_config`` validates only the four keys that have a
        meaning (``LGR-c88d326-4``: deployment drift is a measured property of this
        project). ``test_pipelines_mapping_context.py`` relies on this — it edits
        with ``{"enabled": True}``, which is not a recognised gate.
        """
        kwargs = _edit({"enabled": True})

        assert "approval_config.#ac_enabled = :ac_enabled" in kwargs["UpdateExpression"]
