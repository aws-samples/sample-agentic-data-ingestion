# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 R8.4 — the Selected_Worksheet's READ path and EDIT path.

``create_pipeline`` wrote this attribute and nothing read it back or updated it,
so a pipeline created without a worksheet — the default, since the wizard field is
optional and no workbook exists at creation time — had **no route to one**. The
R8.4 refusal instructed the operator to set a worksheet the product gave them no
way to set; delete-and-recreate was the only way to comply. This module pins the
two halves that close that: ``get_pipeline_details`` reporting what is configured,
and ``edit_pipeline`` setting, changing and clearing it.

## What makes these load-bearing rather than shape checks

The downstream reader, ``schema_extract._configured_worksheet``, tests
``SELECTED_WORKSHEET_ATTRIBUTE in record`` — a **membership** test, never a truth
test — so absent and empty mean opposite things:

* attribute **absent** → no human has chosen. A single-sheet workbook proceeds
  (R8.1); a multi-sheet one refuses and names every Worksheet present (R8.4).
* attribute **present**, *including* ``""`` → a selection naming no sheet, which
  refuses under R8.5 on **every** run rather than falling back to the active
  sheet.

Two consequences this module exists to catch:

1. **On the read path**, reporting the attribute with a truth test — the idiom
   every sibling key in ``get_pipeline_details`` uses — would collapse a
   recorded-empty selection into "nothing configured", so the operator would be
   shown a pipeline that looks fine and refuses every run.
2. **On the edit path**, clearing by storing ``""`` instead of REMOVEing the
   attribute would turn a working single-sheet pipeline into one that can never
   run again. ``LGR-44d601e-1`` (**template / ratified**) settled exactly this for
   the Display_Label in exactly this direction.

And the third intent matters as much as the other two: an edit that does not
mention the worksheet must leave it alone, or changing a description would clear
a configured sheet.

## The expected key is imported, never transcribed

Every assertion names ``SELECTED_WORKSHEET_ATTRIBUTE`` rather than the literal
``"selected_worksheet"``. A test written from the producer's own literal cannot
detect the producer drifting (``LGR-28b4d75-5``, **template / ratified**), and
here the literal has to agree with a constant another Lambda reads through —
precisely ``LGR-eee8ac8-5``'s silent-disagreement case.

## What is deliberately NOT asserted

That the edit path rejects an impossible or unknown worksheet name with a 400. It
does not, and must not: existence is unknowable server-side (no workbook is in
hand), the length / forbidden-character rule lives once in
``dashboard-react/src/utils/worksheet-selection.ts`` and is applied by the form
that submits, and ``create_pipeline``'s permissive posture is governed by
``LGR-cad8032-2``. A rejection here would make the two halves of one attribute
disagree about the same input.

**Validates: p55 R8.3, R8.4**
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies before importing handlers
# ---------------------------------------------------------------------------
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

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"),
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

try:
    from handlers import pipelines_crud as pipelines
    from handlers.pipelines import edit_pipeline, get_pipeline_details
    from handlers.pipelines_crud import (
        _WORKSHEET_UNCHANGED,
        _submitted_worksheet_edit,
    )

    # The one definition, shared with `schema_extract`. Imported, not transcribed.
    from pipeline_record_read import SELECTED_WORKSHEET_ATTRIBUTE
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

PIPELINE_ID = "worksheet-read-edit-pipeline"

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


# ─── Helpers ───────────────────────────────────────────────────────────────


def _record(**extra) -> dict:
    """A pipelines-table record. Extra keys are written verbatim."""
    item = {
        "pipeline_id": PIPELINE_ID,
        "pipeline_name": "Worksheet Pipeline",
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
    """The main arm: a pipeline with state rows, i.e. one that has run."""
    mock_state_table.query.return_value = {
        "Items": [
            {
                "pipeline_id": PIPELINE_ID,
                "step_name": "file_validation",
                "timestamp": "2026-01-01T00:00:00Z",
                "status": "success",
                "original_s3_uri": f"s3://bucket/{PIPELINE_ID}/book.xlsx",
                "run_id": "run-1",
            }
        ]
    }
    mock_pipelines_table.get_item.return_value = {"Item": _record(**extra)}


def _details_body() -> dict:
    result = get_pipeline_details(PIPELINE_ID)
    assert result["statusCode"] == 200, result
    return json.loads(result["body"])


def _update_call() -> dict:
    """The single `update_item` kwargs, asserting there was exactly one."""
    calls = [c for c in mock_pipelines_table.update_item.call_args_list]
    assert len(calls) == 1, f"expected one update_item, got {len(calls)}"
    return calls[0][1]


def _edit(body: dict) -> dict:
    result = edit_pipeline(PIPELINE_ID, json.dumps(body))
    assert result["statusCode"] == 200, result
    return result


# ─── The edit-intent helper, in isolation ──────────────────────────────────


class TestSubmittedWorksheetEdit:
    """Three intents, and the sentinel that keeps them three.

    A two-state ``str | None`` return could not express "leave alone", because
    ``None`` already means "clear". The sentinel is what makes the third arm
    sayable rather than inferred.
    """

    def test_an_omitted_key_is_unchanged_not_cleared(self):
        assert _submitted_worksheet_edit({}) is _WORKSHEET_UNCHANGED

    def test_an_omitted_key_is_unchanged_even_beside_other_edits(self):
        """The intent is per-attribute: editing a description says nothing here."""
        assert _submitted_worksheet_edit({"pipeline_description": "new"}) is _WORKSHEET_UNCHANGED

    def test_a_name_is_a_set(self):
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: "Q3 Actuals"}) == "Q3 Actuals"

    def test_a_name_is_set_verbatim_and_untrimmed(self):
        """A sheet may legitimately be named `" Data "`; trimming would orphan it."""
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: " Data "}) == " Data "

    def test_an_empty_string_is_a_clear(self):
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: ""}) is None

    def test_whitespace_only_is_a_clear(self):
        """The gate trims to ask "did the user type anything"; the value does not."""
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: "  \t "}) is None

    def test_null_is_a_clear(self):
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: None}) is None

    def test_a_non_string_is_a_clear_not_a_rejection(self):
        """`create_pipeline` already rules a non-string "not a selection"."""
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: 3}) is None
        assert _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: ["Sheet1"]}) is None

    def test_the_three_intents_are_mutually_distinguishable(self):
        """No two intents share a return value — the property the caller relies on."""
        unchanged = _submitted_worksheet_edit({})
        cleared = _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: ""})
        was_set = _submitted_worksheet_edit({SELECTED_WORKSHEET_ATTRIBUTE: "Sheet1"})
        assert unchanged is not cleared
        assert unchanged is not was_set
        assert cleared is not was_set
        # And "clear" is never spelled as an empty string, which is the one shape
        # that must not reach the record.
        assert cleared != ""


# ─── The edit path, end to end ─────────────────────────────────────────────


class TestEditPipelineWorksheetIntents:
    """All three intents, observed at the DynamoDB call."""

    def test_setting_a_name_writes_it_in_the_set_clause(self):
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: "Q3 Actuals"})

        kwargs = _update_call()
        assert f"{SELECTED_WORKSHEET_ATTRIBUTE} = :sw" in kwargs["UpdateExpression"]
        assert kwargs["ExpressionAttributeValues"][":sw"] == "Q3 Actuals"
        assert "REMOVE" not in kwargs["UpdateExpression"]

    def test_a_name_reaches_dynamodb_verbatim_and_untrimmed(self):
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: " Data "})

        assert _update_call()["ExpressionAttributeValues"][":sw"] == " Data "

    def test_changing_a_name_is_the_same_set(self):
        """R8.3's "change it" needs no separate mechanism — it is a SET."""
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: "Sheet Two"})

        assert _update_call()["ExpressionAttributeValues"][":sw"] == "Sheet Two"

    def test_clearing_removes_the_attribute_and_stores_no_empty_string(self):
        """The load-bearing one: `""` on the record refuses every run (R8.5)."""
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: ""})

        kwargs = _update_call()
        assert f"REMOVE {SELECTED_WORKSHEET_ATTRIBUTE}" in kwargs["UpdateExpression"]
        assert f"{SELECTED_WORKSHEET_ATTRIBUTE} = " not in kwargs["UpdateExpression"]
        assert "" not in kwargs["ExpressionAttributeValues"].values()

    def test_clearing_via_null_removes_the_attribute(self):
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: None})

        assert f"REMOVE {SELECTED_WORKSHEET_ATTRIBUTE}" in _update_call()["UpdateExpression"]

    def test_an_edit_that_does_not_mention_the_worksheet_leaves_it_alone(self):
        """Neither SET nor REMOVE — otherwise a description edit clears a sheet."""
        _edit({"pipeline_description": "a new description"})

        expr = _update_call()["UpdateExpression"]
        assert SELECTED_WORKSHEET_ATTRIBUTE not in expr

    def test_clearing_the_worksheet_and_the_mapping_context_together_removes_both(self):
        """Two clearable attributes in one request — the REMOVE clause is a list."""
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: "", "mapping_context": ""})

        expr = _update_call()["UpdateExpression"]
        assert "REMOVE " in expr
        remove_clause = expr.split("REMOVE ", 1)[1]
        assert SELECTED_WORKSHEET_ATTRIBUTE in remove_clause
        assert "mapping_context" in remove_clause

    def test_clearing_mapping_context_alone_is_unchanged_by_this_work(self):
        """A regression guard: the REMOVE clause was a bare string before."""
        _edit({"mapping_context": ""})

        assert _update_call()["UpdateExpression"].endswith("REMOVE mapping_context")

    def test_setting_the_worksheet_beside_another_field_does_both(self):
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: "Sheet1", "ai_model": "some-model"})

        kwargs = _update_call()
        assert kwargs["ExpressionAttributeValues"][":sw"] == "Sheet1"
        assert kwargs["ExpressionAttributeValues"][":model"] == "some-model"


class TestEditPipelineWorksheetProperty:
    """Property: every non-blank string round-trips verbatim; every blank clears.

    The generator is deliberately hostile in the ways that matter to this
    attribute — leading and trailing padding, and inner whitespace — because
    those are the inputs a trim would silently corrupt.

    **Validates: p55 R8.3, R8.4**
    """

    @settings(max_examples=60, deadline=None)
    @given(name=st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""))
    def test_any_non_blank_name_is_persisted_verbatim(self, name):
        mock_pipelines_table.reset_mock()

        edit_pipeline(PIPELINE_ID, json.dumps({SELECTED_WORKSHEET_ATTRIBUTE: name}))

        kwargs = mock_pipelines_table.update_item.call_args_list[-1][1]
        assert kwargs["ExpressionAttributeValues"][":sw"] == name
        assert f"REMOVE {SELECTED_WORKSHEET_ATTRIBUTE}" not in kwargs["UpdateExpression"]

    @settings(max_examples=30, deadline=None)
    @given(blank=st.text(alphabet=" \t\n\r", max_size=6))
    def test_any_blank_submission_clears_rather_than_storing_it(self, blank):
        mock_pipelines_table.reset_mock()

        edit_pipeline(PIPELINE_ID, json.dumps({SELECTED_WORKSHEET_ATTRIBUTE: blank}))

        kwargs = mock_pipelines_table.update_item.call_args_list[-1][1]
        assert f"REMOVE {SELECTED_WORKSHEET_ATTRIBUTE}" in kwargs["UpdateExpression"]
        # The blank must not have been stored under any placeholder.
        assert blank not in kwargs["ExpressionAttributeValues"].values()


# ─── The read path, both arms ──────────────────────────────────────────────


class TestPipelineDetailsWorksheetVisibility:
    """`get_pipeline_details` reports the record's membership, not its truthiness.

    Both arms are covered because they are separate code paths with separate
    idioms: the no-state-data arm builds its response with conditional splats and
    the main arm assigns onto a dict, and the truth-test trap is available in
    both.
    """

    # --- no-state-data arm (a pipeline that has never run) ---

    def test_a_configured_worksheet_is_reported_when_the_pipeline_has_not_run(self):
        _seed_no_state(**{SELECTED_WORKSHEET_ATTRIBUTE: "Q3 Actuals"})

        assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == "Q3 Actuals"

    def test_an_absent_attribute_omits_the_key_when_the_pipeline_has_not_run(self):
        """Absent stays absent — the client can tell "none chosen" from "chose nothing"."""
        _seed_no_state()

        assert SELECTED_WORKSHEET_ATTRIBUTE not in _details_body()

    def test_a_recorded_empty_selection_is_reported_as_present_when_not_run(self):
        """The trap: a truth test here would report this as "not configured"."""
        _seed_no_state(**{SELECTED_WORKSHEET_ATTRIBUTE: ""})

        body = _details_body()
        assert SELECTED_WORKSHEET_ATTRIBUTE in body
        assert body[SELECTED_WORKSHEET_ATTRIBUTE] == ""

    def test_padding_survives_the_read_when_the_pipeline_has_not_run(self):
        _seed_no_state(**{SELECTED_WORKSHEET_ATTRIBUTE: " Data "})

        assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == " Data "

    # --- main arm (a pipeline with state rows) ---

    def test_a_configured_worksheet_is_reported_for_a_pipeline_that_has_run(self):
        _seed_with_state(**{SELECTED_WORKSHEET_ATTRIBUTE: "Customers (2025)"})

        assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == "Customers (2025)"

    def test_an_absent_attribute_omits_the_key_for_a_pipeline_that_has_run(self):
        _seed_with_state()

        assert SELECTED_WORKSHEET_ATTRIBUTE not in _details_body()

    def test_a_recorded_empty_selection_is_reported_as_present_for_a_run_pipeline(self):
        _seed_with_state(**{SELECTED_WORKSHEET_ATTRIBUTE: ""})

        body = _details_body()
        assert SELECTED_WORKSHEET_ATTRIBUTE in body
        assert body[SELECTED_WORKSHEET_ATTRIBUTE] == ""

    def test_padding_survives_the_read_for_a_pipeline_that_has_run(self):
        _seed_with_state(**{SELECTED_WORKSHEET_ATTRIBUTE: " Data "})

        assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == " Data "

    def test_both_arms_agree_on_all_three_states(self):
        """The two arms must not disagree — a client cannot tell which it hit."""
        for seed in (_seed_no_state, _seed_with_state):
            seed(**{SELECTED_WORKSHEET_ATTRIBUTE: "Sheet1"})
            assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == "Sheet1"

            seed(**{SELECTED_WORKSHEET_ATTRIBUTE: ""})
            assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == ""

            seed()
            assert SELECTED_WORKSHEET_ATTRIBUTE not in _details_body()


class TestReadEditRoundTrip:
    """Set through the edit path, read back through the detail path.

    The two live in different functions and are only connected by the attribute
    name, which is the join a second literal would break silently.
    """

    def test_a_set_worksheet_is_visible_on_the_next_read(self):
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: "Q3 Actuals"})
        stored = _update_call()["ExpressionAttributeValues"][":sw"]

        # Model the write landing on the record, then read it back.
        _seed_with_state(**{SELECTED_WORKSHEET_ATTRIBUTE: stored})
        assert _details_body()[SELECTED_WORKSHEET_ATTRIBUTE] == "Q3 Actuals"

    def test_a_cleared_worksheet_reads_back_as_absent_not_as_empty(self):
        _edit({SELECTED_WORKSHEET_ATTRIBUTE: ""})
        assert f"REMOVE {SELECTED_WORKSHEET_ATTRIBUTE}" in _update_call()["UpdateExpression"]

        # REMOVE means the attribute is gone, so the read reports absence — which
        # is what lets a single-sheet workbook proceed again (R8.1).
        _seed_with_state()
        assert SELECTED_WORKSHEET_ATTRIBUTE not in _details_body()
