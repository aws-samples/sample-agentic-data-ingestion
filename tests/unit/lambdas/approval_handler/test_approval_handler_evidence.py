# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Unit tests for evidence/questions inclusion in approval data.

Validates:
- Req 7.1: schema_mapping approval_data includes evidence/questions when present
- Req 7.2: evidence/questions omitted cleanly when agent did not run
- Req 7.3: 50,000-char approval_data trim still applies
- Req 12.1: Unit test coverage for approval handler
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Environment variables required before import
#
# THIS LIST IS EXHAUSTIVE BY NECESSITY, NOT BY CHOICE — and that is why p54 task
# 2.2 had to touch this file. It is the only module in this directory that execs
# the handler at MODULE scope (below), so `mock_lambda_env` has not run yet and
# every `require_env` in the handler must already be satisfiable from here.
#
# `AGENTIC_DATA_INGESTION_ENV=local` is NOT a safety net for a missing name.
# `env_utils._LOCAL_MODE` is computed once, when `env_utils` is first imported
# (`shared/env_utils.py:5`), so in a full `tests/unit/` run — where some earlier
# module has already imported it — this `setdefault` arrives too late and
# `require_env` raises for any name not listed here. That is exactly how p54's new
# `MAPPINGS_TABLE` handle surfaced: `make test-scope` on this directory passed while
# `tests/unit/` failed at COLLECTION with
# `OSError: Required environment variable 'MAPPINGS_TABLE' is not set`.
#
# So: a new `require_env` in the handler needs a new line here.
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("APPROVALS_TABLE", "test-approvals")
os.environ.setdefault("STATE_TABLE", "test-state")
os.environ.setdefault("TASK_TOKENS_TABLE", "test-task-tokens")
# p54 R1.1: the reuse arm's mappings-table handle.
os.environ.setdefault("MAPPINGS_TABLE", "test-schema-mappings")

# ---------------------------------------------------------------------------
# Mock setup — patch boto3.resource before importing the handler module
# ---------------------------------------------------------------------------
mock_task_tokens_table = MagicMock()
mock_approvals_table = MagicMock()
mock_state_table = MagicMock()
mock_mappings_table = MagicMock()

_table_map = {
    "test-task-tokens": mock_task_tokens_table,
    "test-approvals": mock_approvals_table,
    "test-state": mock_state_table,
    "test-schema-mappings": mock_mappings_table,
}

with patch("boto3.resource") as mock_resource, patch("boto3.client"):
    mock_dynamo = MagicMock()
    mock_dynamo.Table.side_effect = lambda name: _table_map.get(name, MagicMock())
    mock_resource.return_value = mock_dynamo
    import importlib.util

    sys.modules.pop("lambda_function", None)
    _spec = importlib.util.spec_from_file_location(
        "approval_handler_evidence",
        os.path.join(LAMBDA_DIR, "lambda_function.py"),
    )
    handler = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(handler)


@pytest.fixture(autouse=True)
def _reset_mocks():
    mock_state_table.reset_mock()
    mock_approvals_table.reset_mock()
    mock_task_tokens_table.reset_mock()
    handler.state_table = mock_state_table
    handler.approvals_table = mock_approvals_table
    handler.task_tokens_table = mock_task_tokens_table
    yield


# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

SAMPLE_EVIDENCE = {
    "customer_name": {
        "sample_values": ["John Doe", "Jane Smith", "Bob Builder"],
        "reasoning": "Field matches customer first+last name pattern",
        "memory_matches": [{"field": "full_name", "confidence": 0.95}],
        "confidence_before": 0.55,
        "confidence_after": 0.92,
    },
    "order_date": {
        "sample_values": ["2024-01-15", "2024-02-20", "2024-03-01"],
        "reasoning": "ISO date format matches target order_timestamp field",
        "memory_matches": [],
        "confidence_before": 0.60,
        "confidence_after": 0.88,
    },
}

SAMPLE_QUESTIONS = [
    {
        "field": "addr_line_1",
        "question": "Does this field represent the primary street address?",
        "options": ["Yes, street address", "No, it's a PO Box line", "It's a combined address"],
        "sample_values": ["123 Main St", "456 Oak Ave", "PO Box 789"],
        "current_confidence": 0.45,
    },
]

RUN_ID = "test-run-001"


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetApprovalDataEvidencePresent:
    """When schema_mapping state record has evidence/questions, they are included.

    ``evidence`` and ``questions`` stay at the row's TOP level — see the note on
    ``TestGetApprovalDataEvidenceAbsent`` for why that is their correct address
    and why p50 C1 moved the other seven keys without touching these two. The
    six mapping keys are seeded under ``output_data.approval`` here too, even
    though nothing below asserts them: leaving them at the top level would leave
    a reader an example of the wrong address.
    """

    def test_evidence_included_in_approval_data(self):
        """Validates: Requirement 7.1"""
        mock_state_table.get_item.return_value = {
            "Item": {
                "run_id": RUN_ID,
                "step_name": "schema_mapping",
                "output_data": {
                    "artifact_type": "schema_mapping",
                    "approval": {
                        "source_schema": {"fields": [{"name": "customer_name"}]},
                        "target_schema": {"fields": [{"name": "full_name"}]},
                        "proposed_mappings": [{"source": "customer_name", "target": "full_name"}],
                        "confidence_scores": {"customer_name": 0.55},
                        "unmapped_fields": [],
                        "mapping_summary": "1 field mapped",
                    },
                },
                "evidence": SAMPLE_EVIDENCE,
                "questions": SAMPLE_QUESTIONS,
            }
        }

        result = handler.get_approval_data("schema_mapping", RUN_ID)

        assert result["evidence"] == SAMPLE_EVIDENCE
        assert result["questions"] == SAMPLE_QUESTIONS

    def test_evidence_preserves_per_field_shape(self):
        """Validates: Requirement 7.1 — per-field evidence shape preserved."""
        mock_state_table.get_item.return_value = {
            "Item": {
                "run_id": RUN_ID,
                "step_name": "schema_mapping",
                "evidence": SAMPLE_EVIDENCE,
                "questions": SAMPLE_QUESTIONS,
            }
        }

        result = handler.get_approval_data("schema_mapping", RUN_ID)

        # Verify per-field evidence structure
        evidence = result["evidence"]
        assert "customer_name" in evidence
        field_evidence = evidence["customer_name"]
        assert "sample_values" in field_evidence
        assert "reasoning" in field_evidence
        assert "memory_matches" in field_evidence
        assert "confidence_before" in field_evidence
        assert "confidence_after" in field_evidence


class TestGetApprovalDataEvidenceAbsent:
    """When agent did not run, evidence/questions are None (omitted cleanly).

    p50 C1 — DELIBERATE FIXTURE AMENDMENT, both test names kept verbatim. The two
    tests below used to seed ``source_schema`` / ``target_schema`` /
    ``proposed_mappings`` / ``confidence_scores`` / ``unmapped_fields`` /
    ``mapping_summary`` at the state row's TOP level and assert the handler read
    them back. No producer writes any of them there: ``create_approval_payload``
    (``schema_mapping/lambda_function.py:1732-1780``) builds those six and
    ``store_mapping_result`` stores the payload whole at ``output_data.approval``
    (``:1927``). So they pinned a shape that only ever existed inside this file —
    live, the payload read back with eight of nine keys null.

    The fixtures now use the producer's real shape. Amending the fixture rather
    than teaching the handler the fixture's shape follows ``LGR-3650694-1``
    (template / ratified), which set that direction, and matches what p34 R3.6
    did for the ``code_generation`` twin in
    ``tests/integration/approval_flow/test_approval_system.py``.

    Requirement 7.2 itself is **unchanged and is the point**: ``evidence`` and
    ``questions`` are still read from the row's TOP level, because that is where
    their writer puts them (the complex-mapping agent's ``update_item`` at
    ``schema_mapping/lambda_function.py:2277-2291``). These two tests are the
    coverage that would have caught moving all nine keys instead of seven.
    """

    def test_evidence_none_when_agent_did_not_run(self):
        """Validates: Requirement 7.2"""
        mock_state_table.get_item.return_value = {
            "Item": {
                "run_id": RUN_ID,
                "step_name": "schema_mapping",
                "output_data": {
                    "artifact_type": "schema_mapping",
                    "approval": {
                        "source_schema": {"fields": [{"name": "id"}]},
                        "target_schema": {"fields": [{"name": "id"}]},
                        "proposed_mappings": [{"source": "id", "target": "id"}],
                        "confidence_scores": {"id": 0.95},
                        "unmapped_fields": [],
                        "mapping_summary": "1 field mapped",
                    },
                },
                # No evidence or questions keys at all — the agent did not run,
                # and the row's top level is where it would have written them.
            }
        }

        result = handler.get_approval_data("schema_mapping", RUN_ID)

        # evidence and questions should be None (item.get returns None)
        assert result["evidence"] is None
        assert result["questions"] is None
        # Other fields still present — now from output_data.approval (p50 C1)
        assert result["source_schema"] == {"fields": [{"name": "id"}]}
        assert result["proposed_mappings"] == [{"source": "id", "target": "id"}]

    def test_existing_fields_unchanged_when_no_evidence(self):
        """Validates: Requirement 7.2 — existing fields unchanged."""
        mock_state_table.get_item.return_value = {
            "Item": {
                "run_id": RUN_ID,
                "step_name": "schema_mapping",
                "output_data": {
                    "artifact_type": "schema_mapping",
                    "approval": {
                        "source_schema": {"fields": [{"name": "order_id"}]},
                        "target_schema": {"fields": [{"name": "order_identifier"}]},
                        "proposed_mappings": [{"source": "order_id", "target": "order_identifier"}],
                        "confidence_scores": {"order_id": 0.85},
                        "unmapped_fields": ["extra_field"],
                        "mapping_summary": "1 mapped, 1 unmapped",
                    },
                },
            }
        }

        result = handler.get_approval_data("schema_mapping", RUN_ID)

        assert result["source_schema"] == {"fields": [{"name": "order_id"}]}
        assert result["target_schema"] == {"fields": [{"name": "order_identifier"}]}
        assert result["proposed_mappings"] == [{"source": "order_id", "target": "order_identifier"}]
        assert result["confidence_scores"] == {"order_id": 0.85}
        assert result["unmapped_fields"] == ["extra_field"]
        assert result["mapping_summary"] == "1 mapped, 1 unmapped"


class TestApprovalDataTrim:
    """The 50,000-char approval_data trim guard still applies with evidence."""

    def test_large_evidence_triggers_trim(self):
        """Validates: Requirement 7.3 — 50k trim applies."""
        # Create a large evidence payload that exceeds 50k when serialized
        large_evidence = {}
        for i in range(200):
            large_evidence[f"field_{i}"] = {
                "sample_values": [f"value_{j}" * 50 for j in range(20)],
                "reasoning": "x" * 200,
                "memory_matches": [{"field": f"match_{j}", "confidence": 0.9} for j in range(10)],
                "confidence_before": 0.3,
                "confidence_after": 0.9,
            }

        mock_state_table.get_item.return_value = {
            "Item": {
                "run_id": RUN_ID,
                "step_name": "schema_mapping",
                # The six mapping keys under the producer's path (p50 C1); the
                # trim is driven by ``evidence``, which stays at the top level.
                "output_data": {
                    "artifact_type": "schema_mapping",
                    "approval": {
                        "source_schema": {"fields": []},
                        "target_schema": {"fields": []},
                        "proposed_mappings": [],
                        "confidence_scores": {},
                        "unmapped_fields": [],
                        "mapping_summary": "large test",
                    },
                },
                "evidence": large_evidence,
                "questions": SAMPLE_QUESTIONS,
            }
        }

        # Mock supporting table calls for create_approval_request
        mock_state_table.query.return_value = {"Items": []}
        mock_state_table.update_item.return_value = {}
        mock_approvals_table.put_item.return_value = {}

        event = {
            "approval_type": "schema_mapping",
            "pipeline_id": "test-pipeline",
            "run_id": RUN_ID,
        }

        # Call create_approval_request which invokes get_approval_data + trim logic
        with patch.object(handler, "send_approval_notifications"):
            with patch.object(handler, "publish_approval_status_change"):
                handler.create_approval_request(event)

        # Verify put_item was called and the approval_data was trimmed
        put_call = mock_approvals_table.put_item.call_args
        item = put_call[1]["Item"] if put_call[1] else put_call[0][0]
        approval_data_str = item["approval_data"]

        # The trim guard caps at 50,000 chars + truncation suffix
        assert len(approval_data_str) <= 50000 + len("...(truncated)")


# ---------------------------------------------------------------------------
# p30 R7.2 — the first_run payload carries the Quality_Verdict mode
# ---------------------------------------------------------------------------


class TestFirstRunQualityVerdict:
    """The first_run payload is where a human approves a pipeline's first run
    while being shown a rule count that, for four of five (source_type,
    target_type) combinations, gates nothing. The Quality_Verdict mode
    ``create_quality_checks`` records (p30 R7.2) travels with that payload so
    the approver can tell which case they are in.

    It is read from ``output_data``, which is where that writer puts it.
    """

    def _state_rows(self, quality_output_data):
        """Answer the first_run branch's two keyed reads in call order:
        create_infrastructure first, then create_quality_checks."""
        mock_state_table.get_item.side_effect = [
            {
                "Item": {
                    "run_id": RUN_ID,
                    "step_name": "create_infrastructure",
                    "glue_job_name": "adi-job",
                }
            },
            {
                "Item": {
                    "run_id": RUN_ID,
                    "step_name": "create_quality_checks",
                    "output_data": quality_output_data,
                }
            },
        ]

    def test_enforced_verdict_reaches_the_approval_payload(self):
        """Validates: p30 Requirement 7.2"""
        self._state_rows(
            {
                "total_rules": 2,
                "quality_verdict": {
                    "mode": "enforced",
                    "evaluation_point": "before_write",
                    "blocks_run": False,
                    "dqdl_rule_count": 3,
                    "template_used": "glue_job_s3_tables.py.j2",
                },
            }
        )
        result = handler.get_approval_data("first_run", RUN_ID)
        assert result["quality_verdict"]["mode"] == "enforced"
        assert result["quality_verdict"]["dqdl_rule_count"] == 3
        # The verdict is additive — the existing first_run fields still resolve.
        assert result["glue_job_name"] == "adi-job"

    def test_advisory_verdict_reaches_the_approval_payload(self):
        """Validates: p30 Requirement 7.2"""
        self._state_rows(
            {
                "quality_verdict": {
                    "mode": "advisory",
                    "evaluation_point": "none",
                    "blocks_run": False,
                    "dqdl_rule_count": 0,
                    "template_used": "glue_job_jdbc_source.py.j2",
                }
            }
        )
        result = handler.get_approval_data("first_run", RUN_ID)
        assert result["quality_verdict"]["mode"] == "advisory"
        assert result["quality_verdict"]["blocks_run"] is False

    def test_row_without_a_verdict_yields_none_rather_than_a_fabricated_mode(self):
        """Rows written before this field existed must not be labelled — an
        absent verdict is None, never a defaulted 'enforced'/'advisory'."""
        self._state_rows({"total_rules": 2})
        result = handler.get_approval_data("first_run", RUN_ID)
        assert result["quality_verdict"] is None

    def test_non_dict_output_data_does_not_raise(self):
        """Legacy rows stored output_data as a JSON string in places; reading a
        verdict out of one must degrade, not break the approval gate."""
        self._state_rows("{\"total_rules\": 2}")
        result = handler.get_approval_data("first_run", RUN_ID)
        assert result["quality_verdict"] is None
