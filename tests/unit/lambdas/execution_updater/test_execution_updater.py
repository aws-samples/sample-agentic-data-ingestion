# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for the execution updater Lambda (DynamoDB Stream handler).

Validates that Stream records from the state table are correctly processed
into upserts on the agentic_data_ingestion_executions table.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_execution_updater"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")

# ---------------------------------------------------------------------------
# Fixtures — DynamoDB Stream event builders
# ---------------------------------------------------------------------------

PIPELINE_ID = "pipe-test-001"
RUN_ID = "run-2025-01-15-12-00-00-abcd1234"
FIXED_NOW = "2025-01-15T12:05:30+00:00"


def _stream_record(
    event_name, step_name, step_status, run_id=RUN_ID, pipeline_id=PIPELINE_ID, file_name=None, extra_attrs=None
):
    """Build a DynamoDB Stream record in NEW_IMAGE format."""
    new_image = {
        "run_id": {"S": run_id},
        "pipeline_id": {"S": pipeline_id},
        "step_name": {"S": step_name},
        "status": {"S": step_status},
    }
    if file_name:
        new_image["file_name"] = {"S": file_name}
    if extra_attrs:
        new_image.update(extra_attrs)
    return {
        "eventID": f"evt-{step_name}-{step_status}",
        "eventName": event_name,
        "dynamodb": {"NewImage": new_image},
    }


def _stream_event(*records):
    """Wrap records into a Lambda event payload."""
    return {"Records": list(records)}


# ---------------------------------------------------------------------------
# Mock setup — patch the module-level executions_table before importing handler
# ---------------------------------------------------------------------------
mock_executions_table = MagicMock()

# Clear any cached lambda_function from other test files to avoid cross-contamination
sys.modules.pop("lambda_function", None)
# Re-insert our Lambda directory at position 0 to ensure correct module is found
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))

with patch("boto3.resource") as mock_resource:
    mock_dynamo = MagicMock()
    mock_dynamo.Table.return_value = mock_executions_table
    mock_resource.return_value = mock_dynamo
    import lambda_function as updater


@pytest.fixture(autouse=True)
def _reset_mock():
    """Reset the mock table before each test."""
    mock_executions_table.reset_mock()
    yield


# ---------------------------------------------------------------------------
# Test: INSERT event for file_validation step
# ---------------------------------------------------------------------------


class TestInsertFileValidation:
    """INSERT event for file_validation → upserts execution with started_at, file_name."""

    @patch.object(updater, "_iso_now", return_value=FIXED_NOW)
    def test_insert_file_validation_upserts_execution(self, _mock_now):
        record = _stream_record(
            "INSERT",
            "file_validation",
            "running",
            file_name="customers.json",
        )
        event = _stream_event(record)

        result = updater.lambda_handler(event, None)

        assert result["statusCode"] == 200
        assert result["processed"] == 1

        # Two update_item calls: first for scalars + map init, second for nested step status
        assert mock_executions_table.update_item.call_count == 2
        first_kwargs = mock_executions_table.update_item.call_args_list[0][1]
        second_kwargs = mock_executions_table.update_item.call_args_list[1][1]

        # First call: scalar fields, map init, started_at, file_name
        assert first_kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        # _derive_execution_status('file_validation', 'running') → 'RUNNING'
        assert first_kwargs["ExpressionAttributeValues"][":status"] == "RUNNING"
        assert first_kwargs["ExpressionAttributeValues"][":step"] == "file_validation"
        assert first_kwargs["ExpressionAttributeValues"][":file_name"] == "customers.json"
        assert "started_at" in first_kwargs["UpdateExpression"]
        assert "file_name" in first_kwargs["UpdateExpression"]

        # Second call: nested step status (normalized to uppercase)
        assert second_kwargs["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        assert second_kwargs["ExpressionAttributeValues"][":step_status"] == "RUNNING"


# ---------------------------------------------------------------------------
# Test: MODIFY event for step completed
# ---------------------------------------------------------------------------


class TestModifyStepCompleted:
    """MODIFY event for a mid-pipeline step completing → updates steps_status and current_step."""

    @patch.object(updater, "_iso_now", return_value=FIXED_NOW)
    def test_modify_step_completed_updates_status(self, _mock_now):
        record = _stream_record("MODIFY", "schema_extract", "completed")
        event = _stream_event(record)

        updater.lambda_handler(event, None)

        assert mock_executions_table.update_item.call_count == 2
        first_kwargs = mock_executions_table.update_item.call_args_list[0][1]
        second_kwargs = mock_executions_table.update_item.call_args_list[1][1]

        # _derive_execution_status('schema_extract', 'completed') → 'RUNNING' (not final step)
        assert first_kwargs["ExpressionAttributeValues"][":status"] == "RUNNING"
        assert first_kwargs["ExpressionAttributeValues"][":step"] == "schema_extract"
        # Non-final step → no completed_at
        assert "completed_at" not in first_kwargs["UpdateExpression"]

        # Second call sets the nested step status (normalized to uppercase)
        assert second_kwargs["ExpressionAttributeValues"][":step_status"] == "SUCCEEDED"


# ---------------------------------------------------------------------------
# Test: MODIFY event for final step completed → sets completed_at
# ---------------------------------------------------------------------------


class TestModifyFinalStepCompleted:
    """MODIFY event for terminal step completing → sets completed_at and status='completed'."""

    @patch.object(updater, "_iso_now", return_value=FIXED_NOW)
    def test_final_step_completed_sets_completed_at(self, _mock_now):
        record = _stream_record("MODIFY", "create_glue_pipeline", "completed")
        event = _stream_event(record)

        updater.lambda_handler(event, None)

        first_kwargs = mock_executions_table.update_item.call_args_list[0][1]
        # _derive_execution_status('create_glue_pipeline', 'completed') → 'SUCCEEDED'
        assert first_kwargs["ExpressionAttributeValues"][":status"] == "SUCCEEDED"
        assert "completed_at" in first_kwargs["UpdateExpression"]

    @patch.object(updater, "_iso_now", return_value=FIXED_NOW)
    def test_create_quality_checks_is_also_final(self, _mock_now):
        record = _stream_record("MODIFY", "create_quality_checks", "completed")
        event = _stream_event(record)

        updater.lambda_handler(event, None)

        first_kwargs = mock_executions_table.update_item.call_args_list[0][1]
        assert first_kwargs["ExpressionAttributeValues"][":status"] == "SUCCEEDED"
        assert "completed_at" in first_kwargs["UpdateExpression"]


# ---------------------------------------------------------------------------
# Test: MODIFY event for step failed → status='failed'
# ---------------------------------------------------------------------------


class TestModifyStepFailed:
    """MODIFY event for a step failing → sets execution status to 'failed'."""

    @patch.object(updater, "_iso_now", return_value=FIXED_NOW)
    def test_step_failed_sets_failed_status(self, _mock_now):
        record = _stream_record("MODIFY", "code_generation", "failed")
        event = _stream_event(record)

        updater.lambda_handler(event, None)

        first_kwargs = mock_executions_table.update_item.call_args_list[0][1]
        # _derive_execution_status('code_generation', 'failed') → 'FAILED'
        assert first_kwargs["ExpressionAttributeValues"][":status"] == "FAILED"


# ---------------------------------------------------------------------------
# Test: Idempotent — same event twice produces same update_item call
# ---------------------------------------------------------------------------


class TestIdempotent:
    """Processing the same event twice produces identical update_item calls."""

    @patch.object(updater, "_iso_now", return_value=FIXED_NOW)
    def test_duplicate_event_same_update(self, _mock_now):
        record = _stream_record("MODIFY", "schema_mapping", "completed")
        event = _stream_event(record)

        updater.lambda_handler(event, None)
        first_calls = mock_executions_table.update_item.call_args_list[:]

        mock_executions_table.reset_mock()

        updater.lambda_handler(event, None)
        second_calls = mock_executions_table.update_item.call_args_list[:]

        assert first_calls == second_calls


# ---------------------------------------------------------------------------
# Test: Skips records with missing run_id or pipeline_id
# ---------------------------------------------------------------------------


class TestSkipsMissingKeys:
    """Records missing run_id or pipeline_id are silently skipped."""

    def test_missing_run_id_skipped(self):
        record = {
            "eventID": "evt-no-run-id",
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "pipeline_id": {"S": PIPELINE_ID},
                    "step_name": {"S": "file_validation"},
                    "status": {"S": "running"},
                }
            },
        }
        event = _stream_event(record)

        result = updater.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_executions_table.update_item.assert_not_called()

    def test_missing_pipeline_id_skipped(self):
        record = {
            "eventID": "evt-no-pipeline-id",
            "eventName": "INSERT",
            "dynamodb": {
                "NewImage": {
                    "run_id": {"S": RUN_ID},
                    "step_name": {"S": "file_validation"},
                    "status": {"S": "running"},
                }
            },
        }
        event = _stream_event(record)

        result = updater.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_executions_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# Test: Skips DELETE events
# ---------------------------------------------------------------------------


class TestSkipsDeleteEvents:
    """DELETE events are ignored entirely."""

    def test_delete_event_skipped(self):
        record = {
            "eventID": "evt-delete",
            "eventName": "REMOVE",
            "dynamodb": {
                "NewImage": {
                    "run_id": {"S": RUN_ID},
                    "pipeline_id": {"S": PIPELINE_ID},
                    "step_name": {"S": "file_validation"},
                    "status": {"S": "running"},
                }
            },
        }
        event = _stream_event(record)

        result = updater.lambda_handler(event, None)

        assert result["statusCode"] == 200
        mock_executions_table.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# p37 task 2.2 — the Step_Row allowlist (C6)
# ---------------------------------------------------------------------------
#
# Task 2.1 added two module-level functions and one routing return to
# ``_process_record``. They are deliberately separate and are tested separately
# here:
#
# * ``_to_step_or_none`` (``lambda_function.py:93``) is **the allowlist**. It
#   canonicalizes a state row's ``step_name`` through ``shared/step_names``'
#   own lookup and answers ``None`` for anything that is not a member of
#   ``STEP_SEQUENCE``.
# * ``_drives_execution_row`` (``:117``) is **the routing predicate**. A
#   Step_Row, OR the one non-step record that carries a run's terminal status.
#
# ``_process_record`` calls the predicate at ``:210``, between the
# ``self_healing`` branch and the two derivations at ``:220-221`` — so a
# non-Step_Row returns **before any status is derived** from it. That
# positioning is the decision ``LGR-453f12d-5`` (**template / ratified**)
# records: "route non-step state rows away from the step machinery before any
# status is derived". C6 generalises it from one record type to the allowlist;
# ``test_self_healing_visibility.py`` remains the guard for the original.
#
# **Mocking route.** These tests use this file's existing module-level
# ``mock_executions_table`` (a ``MagicMock`` installed by patching
# ``boto3.resource`` at import, above). No new mock is introduced. The shared
# ``mock_dynamodb_tables`` fixture is moto-backed and can only be asked what a
# table *contains* afterwards, which cannot distinguish "nothing was written"
# from "a setup step did not run" — and R4.1/R4.2 are claims about what the
# handler *emits*. ``test_normalize_status_characterization.py::_load_handler``
# records the same reasoning for the same tree.
#
# **Validates: Requirements 4.1, 4.2, 4.3**

# The ten state-row-writing steps, transcribed from
# ``shared/step_names.STEP_SEQUENCE`` rather than read off it. Comparing the
# module's own table to itself would pass for any value it ever holds, which is
# LGR-28b4d75-5's "the output is its own oracle"; this literal is the oracle, so
# adding a step means editing here deliberately.
CANONICAL_STEP_ROWS = (
    "file_validation",
    "schema_extract",
    "schema_compare",
    "schema_mapping",
    "create_code",
    "test_code",
    "create_infrastructure",
    "create_glue_pipeline",
    "create_quality_checks",
    "run_pipeline",
)

# Cataloged legacy variants that persist in live tables and must still classify
# as Step_Rows (``shared/step_names._LEGACY_ALIASES`` and the PascalCase SFN
# ids, both reachable through ``from_any``'s ``_LOOKUP``).
LEGACY_STEP_ROWS = (
    "CodeGeneration",  # create_code's pre-task-14.2 step_name
    "CodeTesting",  # test_code's pre-task-14.2 step_name
    "infrastructure",  # create_infrastructure's approval_type phantom row
    "first_run",  # create_quality_checks' approval_type phantom row
    "RunPipeline",  # PascalCase SFN state id
    "CreateQualityChecks",
)

# State-table ``step_name`` values that are records rather than pipeline steps
# (``shared/step_names.py``'s module docstring names all four; ``load_metrics``
# is the fifth, added by p37 C1).
RECORD_ROWS = ("data_quality", "load_metrics", "cache", "self_healing", "pipeline_failure")


def _record_row(step_name, *, pipeline_id=PIPELINE_ID, run_id=RUN_ID, **attrs):
    """A stream record with NO ``status`` attribute — a record row, not a step.

    The DQ_Report_Row carries ``overall_status`` and no ``status``
    (``pyspark_templates/glue_job_s3_tables.py.j2:285``, and the on-demand agent
    writes the same field at ``agentcore/data_quality_agent/main.py:582``), which
    is the whole mechanism behind R4.1. Transcribed rather than generated, per
    LGR-28b4d75-5. ``pipeline_id`` is carried deliberately: omitting it would
    make ``_process_record`` drop the row at ``:172-174`` for want of a pipeline,
    which would pass these assertions for the wrong reason.
    """
    new_image = {
        "run_id": {"S": run_id},
        "pipeline_id": {"S": pipeline_id},
        "step_name": {"S": step_name},
    }
    new_image.update({k: {"S": v} for k, v in attrs.items()})
    return {
        "eventID": f"evt-{step_name}",
        "eventName": "INSERT",
        "dynamodb": {"NewImage": new_image},
    }


def _update_expressions(table):
    """Every ``UpdateExpression`` the handler emitted, in order."""
    return [call.kwargs.get("UpdateExpression", "") for call in table.update_item.call_args_list]


def _steps_status_writes(table):
    """Every ``(step_name, step_status)`` pair written into the steps_status map.

    Keyed on the second update's shape (``SET #steps_status.#step_name =
    :step_status``). A list rather than a bool so a failure prints the offending
    pair.
    """
    writes = []
    for call in table.update_item.call_args_list:
        kwargs = call.kwargs
        if "#steps_status.#step_name" not in kwargs.get("UpdateExpression", ""):
            continue
        names = kwargs.get("ExpressionAttributeNames") or {}
        values = kwargs.get("ExpressionAttributeValues") or {}
        writes.append((names.get("#step_name", ""), values.get(":step_status", "")))
    return writes


class TestTheAllowlistClassifier:
    """``_to_step_or_none`` (``:93``) — the allowlist, tested as one.

    Keyed on ``shared/step_names.STEP_SEQUENCE`` rather than on a denylist of
    record types, which is what makes C1's ``load_metrics`` row safe by
    construction rather than by remembering to extend a list.
    """

    @pytest.mark.parametrize("step_name", CANONICAL_STEP_ROWS)
    def test_every_state_row_writing_step_is_a_step_row(self, step_name):
        assert updater._to_step_or_none(step_name) is not None
        assert updater._drives_execution_row(step_name) is True

    @pytest.mark.parametrize("step_name", LEGACY_STEP_ROWS)
    def test_a_cataloged_legacy_variant_is_still_a_step_row(self, step_name):
        """Rows written before task 14.2's rename persist in live tables.

        This is a Stream consumer: a legacy row can still be MODIFY'd, and
        routing it away would lose the step from the run's ``steps_status``.
        """
        assert updater._to_step_or_none(step_name) is not None
        assert updater._drives_execution_row(step_name) is True

    @pytest.mark.parametrize(
        "step_name",
        ["check_schema_action", "check_schema_mapping_approval", "check_code_approval", "CheckCodeApproval"],
    )
    def test_a_choice_state_parses_but_is_not_a_step_row(self, step_name):
        """The three ``check_*`` Choice states never write state rows.

        They are members of ``StepName`` (so ``from_any`` accepts them) but not
        of ``STEP_SEQUENCE``, and the allowlist is keyed on the latter. Pinned
        because widening the membership test from ``STEP_SEQUENCE`` to
        ``StepName`` would be an invisible change otherwise.
        """
        assert updater._to_step_or_none(step_name) is None

    @pytest.mark.parametrize("step_name", RECORD_ROWS)
    def test_a_record_row_is_not_a_step_row(self, step_name):
        assert updater._to_step_or_none(step_name) is None

    def test_pipeline_failure_is_not_a_step_row_but_still_drives_the_execution_row(self):
        """The one deliberate non-step admission, and a documented exception.

        R4.3 reads literally as "route a non-Step_Row away". ``pipeline_failure``
        is admitted anyway, and that is **required behaviour, not an accident** —
        do not "fix" it. ``record_failure`` writes ``(run_id,
        "pipeline_failure")`` with ``status="error"`` and
        ``original_pipeline_id`` set to the real pipeline
        (``record_failure/lambda_function.py:432-447``), so it lands on the
        run's execution row and is the ONLY carrier of that run's terminal
        FAILED status whenever the failing step never wrote a row of its own —
        ``States.Timeout`` parked at an approval gate,
        ``Lambda.ServiceException``, ``GlueJobFailed``. Routing it away would
        leave those runs reading RUNNING forever: the same inflated-running-count
        defect ``LGR-453f12d-5`` (**template / ratified**) fixed for
        ``self_healing``, reintroduced by the fix for it.
        """
        assert updater._to_step_or_none("pipeline_failure") is None
        assert updater._drives_execution_row("pipeline_failure") is True

    def test_self_healing_is_not_admitted_by_the_predicate(self):
        """It keeps its own dedicated branch at ``:192`` — it has an outcome to
        apply — and is reached before the predicate, so the predicate must not
        also admit it. ``test_self_healing_visibility.py`` guards the branch."""
        assert updater._drives_execution_row("self_healing") is False


class TestTheClassifierNeverRaises:
    """Risk N2, asserted directly rather than inferred.

    ``step_names.from_any`` raises ``ValueError`` on an unknown name **by
    contract** (``shared/step_names.py:228-231``) and
    ``lambda_handler`` re-raises so DynamoDB Streams retries the batch
    (``:157-159``). Every state row of a run shares the ``run_id`` partition
    key, so one unrecognised ``step_name`` propagating out of the classifier
    would stall the shard for **the whole run** — not one record.

    Each assertion below calls the classifier outside ``pytest.raises``, so a
    future change that lets an exception escape fails these tests loudly rather
    than being discovered in a stalled shard.

    **One finding came out of writing these, and it is recorded rather than
    fixed** (task 2.2 changes no production code). ``_to_step_or_none`` — the
    allowlist, and the function risk N2's mitigation actually names — is total
    over ``object``: every value below returns ``None``. ``_drives_execution_row``
    — the routing predicate — is total over *hashable* values only, because its
    second clause is a ``frozenset`` membership test. It raises ``TypeError`` for
    ``[]``, ``{}`` or any other unhashable argument. That is unreachable from
    ``_process_record``, whose ``step_name`` comes from ``_get_str`` and is
    therefore always a ``str``, and the predicate is annotated ``step_name:
    str`` — so the risk-N2 consequence (a stalled shard) does not follow. The
    three tests below split along that line and the last one pins it.
    """

    @pytest.mark.parametrize(
        "value",
        [
            "totally_unknown_thing_\u2603",
            "load_metrics",
            "not a step",
            "",
            " ",
            "run_pipeline_v2",
            "\u0000",
        ],
    )
    def test_an_unknown_step_name_is_a_non_step_row_and_does_not_raise(self, value):
        assert updater._to_step_or_none(value) is None
        assert updater._drives_execution_row(value) is False

    @pytest.mark.parametrize("value", [None, 0, 17, [], {}, 3.5, b"run_pipeline"])
    def test_a_non_string_step_name_is_a_non_step_row_and_does_not_raise(self, value):
        """The allowlist is total over ``object``, not only over ``str``.

        ``_get_str`` returns ``""`` for a missing or non-``S`` attribute, so none
        of these is reachable through ``_process_record`` today — but the
        allowlist is the fence risk N2 names (``design.md`` C6: "use the
        non-raising shape ``retry_utils.py:142`` already has
        (``_to_step_or_none``)"), and an ``isinstance`` guard that regressed to a
        truthiness test would admit ``0`` and ``[]`` and then hand ``17`` to
        ``str.replace`` inside ``step_names._normalize``.
        """
        assert updater._to_step_or_none(value) is None

    @pytest.mark.parametrize("value", [None, 0, 17, 3.5, b"run_pipeline"])
    def test_the_routing_predicate_is_false_for_a_hashable_non_string(self, value):
        """Split from the case above, and the split is a finding rather than a
        convenience — see the module-level note under
        ``TestTheClassifierNeverRaises`` for the unhashable case.
        """
        assert updater._drives_execution_row(value) is False

    @pytest.mark.parametrize("value", [[], {}])
    def test_the_routing_predicate_raises_typeerror_for_an_unhashable_value(self, value):
        """**Recorded finding, not endorsed behaviour.**

        ``_drives_execution_row``'s second clause is ``step_name in
        _STATUS_BEARING_RECORD_ROWS`` — a ``frozenset`` membership test — so an
        unhashable argument raises ``TypeError`` there. The allowlist itself
        (``_to_step_or_none``, asserted total above) is unaffected; the predicate
        is what breaks.

        **Reachability, stated rather than assumed.** ``_process_record`` obtains
        ``step_name`` from ``_get_str`` (``:171``), which is
        ``image.get(key, {}).get("S", "")`` and therefore always returns a
        ``str``. So this cannot be reached from the Stream path, and risk N2's
        stalled-shard consequence does not follow from it. The predicate is also
        annotated ``step_name: str``, so an unhashable argument violates its
        declared contract.

        ``set()`` is deliberately not in the list: it is unhashable, but CPython's
        ``set.__contains__`` converts a ``set`` argument to a ``frozenset`` rather
        than raising, so it returns ``False`` here. Measured, not assumed.

        Pinned so the boundary is visible instead of latent: if the predicate is
        later hardened (e.g. by checking ``isinstance(step_name, str)`` before the
        membership test, or by ordering the clauses so the allowlist short-
        circuits), this test is where that change announces itself and this note
        is where the reader learns why it existed. Task 2.2 does not change
        production code, so it is recorded here rather than fixed.
        """
        with pytest.raises(TypeError):
            updater._drives_execution_row(value)

    def test_the_non_raising_behaviour_is_the_wrappers_doing_not_from_anys(self):
        """The classifier catches; ``from_any`` still raises.

        Asserted as a pair so the guarantee cannot silently come to rest on
        ``from_any`` becoming lenient instead — which would change a documented
        contract other callers rely on.
        """
        from step_names import from_any

        with pytest.raises(ValueError):
            from_any("totally_unknown_thing_\u2603")
        assert updater._to_step_or_none("totally_unknown_thing_\u2603") is None


class TestNonStepRowsAreRoutedAwayBeforeAnyStatusIsDerived:
    """R4.1 and R4.2 at the caller — asserted as "no write at all".

    R4.2 is deliberately stronger than "no ``steps_status`` entry": the first
    update sets ``#status`` and ``current_step`` unconditionally in the
    ``update_parts`` list, and ``_derive_execution_status("data_quality", "")``
    returns ``"RUNNING"``, so the latent second symptom is a complete run
    flipping back to ``RUNNING -> data_quality``. Both halves are closed by the
    same return, so both are asserted.
    """

    def test_a_data_quality_row_writes_nothing_at_all(self):
        """R4.1 + R4.2. The DQ_Report_Row is the row that produced the trailing
        gear on the run list (``formatting.ts:199``'s default arm, reached via
        ``steps_status.data_quality == "UNKNOWN"``)."""
        record = _record_row(
            "data_quality",
            overall_status="fail",
            source="glue_inline_dq",
            quarantine_path="s3://adi-quarantine/newtest-ab2a08a0/",
        )

        result = updater.lambda_handler(_stream_event(record), None)

        assert result["statusCode"] == 200
        assert mock_executions_table.update_item.call_count == 0
        assert _steps_status_writes(mock_executions_table) == []
        assert not any("#status" in expr for expr in _update_expressions(mock_executions_table))
        assert not any("current_step" in expr for expr in _update_expressions(mock_executions_table))

    def test_a_load_metrics_row_writes_nothing_at_all(self):
        """The row p37 C1 (task 3.2) will start writing, on the same stream shard
        as the run's step rows. It must already be routed away — otherwise every
        run grows a second phantom gear, which is R4's defect doubled. This
        assertion is why C6 lands before C1."""
        record = _record_row(
            "load_metrics",
            rows_read="5",
            rows_written="0",
            write_confirmed="false",
        )

        result = updater.lambda_handler(_stream_event(record), None)

        assert result["statusCode"] == 200
        assert mock_executions_table.update_item.call_count == 0
        assert _steps_status_writes(mock_executions_table) == []

    def test_an_unknown_step_name_row_writes_nothing_and_does_not_raise(self):
        """Risk N2 at the caller. A raising classifier would propagate out of
        ``lambda_handler`` (``:157-159`` re-raises so Streams retries), and every
        row of the run shares the ``run_id`` partition key — so the shard would
        stall for the whole run rather than for the one row."""
        record = _record_row("totally_unknown_thing_\u2603", status="success")

        result = updater.lambda_handler(_stream_event(record), None)

        assert result["statusCode"] == 200
        assert result["processed"] == 1
        assert mock_executions_table.update_item.call_count == 0

    def test_a_genuine_step_row_is_unaffected(self):
        """The allowlist admits real steps, unchanged. If this goes red the
        allowlist rejected a step, which is the other way C6 can go wrong."""
        record = _stream_record("MODIFY", "run_pipeline", "success")

        updater.lambda_handler(_stream_event(record), None)

        assert mock_executions_table.update_item.call_count == 2
        first = mock_executions_table.update_item.call_args_list[0].kwargs
        assert first["ExpressionAttributeValues"][":status"] == "SUCCEEDED"
        assert first["ExpressionAttributeValues"][":step"] == "run_pipeline"
        assert _steps_status_writes(mock_executions_table) == [("run_pipeline", "SUCCEEDED")]

    def test_a_legacy_code_generation_row_is_still_treated_as_a_step_row(self):
        """``CodeGeneration`` rows persist in live tables from before task 14.2.
        The row keeps its own key in ``steps_status`` — the allowlist decides
        whether a row is a step, it does not rewrite the key."""
        record = _stream_record("MODIFY", "CodeGeneration", "success")

        updater.lambda_handler(_stream_event(record), None)

        assert mock_executions_table.update_item.call_count == 2
        first = mock_executions_table.update_item.call_args_list[0].kwargs
        assert first["ExpressionAttributeValues"][":step"] == "CodeGeneration"
        assert _steps_status_writes(mock_executions_table) == [("CodeGeneration", "SUCCEEDED")]

    def test_a_pipeline_failure_row_still_drives_status_and_its_steps_status_entry(self):
        """The documented exception to R4.3's literal reading — keep it.

        ``record_failure`` is the only carrier of a run's terminal FAILED status
        when the failing step never wrote a row of its own, so this row must go
        on deriving ``#status`` and its own ``steps_status`` entry. See
        ``TestTheAllowlistClassifier::
        test_pipeline_failure_is_not_a_step_row_but_still_drives_the_execution_row``
        for the full reasoning and the record it rests on.
        """
        # Faithful to the producer: the row's own ``pipeline_id`` is the legacy
        # synthetic ``{run_id}.99``, and the real pipeline arrives on
        # ``original_pipeline_id`` — which ``_process_record`` resolves first
        # (``:170``). That is how the row lands on the run's execution row.
        record = _record_row(
            "pipeline_failure",
            pipeline_id=f"{RUN_ID}.99",
            original_pipeline_id=PIPELINE_ID,
            status="error",
            error_message="States.Timeout",
            failed_step="check_code_approval",
        )

        updater.lambda_handler(_stream_event(record), None)

        assert mock_executions_table.update_item.call_count == 2
        first = mock_executions_table.update_item.call_args_list[0].kwargs
        assert first["Key"] == {"pipeline_id": PIPELINE_ID, "run_id": RUN_ID}
        assert first["ExpressionAttributeValues"][":status"] == "FAILED"
        assert "#status = :status" in first["UpdateExpression"]
        assert _steps_status_writes(mock_executions_table) == [("pipeline_failure", "FAILED")]
