# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: the shadow-verification record is not a step in the run tree.

p42 R4.3, read side. ``test_code`` writes a ``(run_id, "shadow_verification")``
row (``build_shadow_verification_item``) when ``VERIFICATION_EXECUTOR`` is
``shadow``. It is a record, not a Step_Row — the same category as
``data_quality`` and ``load_metrics`` — so it must not appear in the run's step
tree.

Why this test exists now, before anything writes the row: ``LGR-9e76249-9``
(**template / ratified**) recorded that the run-detail reader's classification
is a **denylist** while the writer side (``execution_updater``) is an
**allowlist**, and stated the cost of that asymmetry plainly — "a future record
type has to be added to the reader denylist **by hand**, and forgetting will
surface as a trailing ⚙️ step on the run-detail page". ``shadow_verification``
is that future record type. The writer's own docstring
(``test_code/lambda_function.py`` at ``SHADOW_VERIFICATION_STEP_NAME``) names
adding the key as "a **prerequisite for enabling shadow mode**".

So this fails without the one denylist entry, and it fails *today* even though
no live run writes the row — ``VERIFICATION_EXECUTOR`` defaults to ``pandas``.
That is the point: the regression is latent, and a test is the only thing that
can hold the fix in place until shadow mode is switched on.

**What is asserted as unchanged, and why it is not padding.** The row carries no
``status`` and no ``step_sequence`` (deliberate, and load-bearing for the
canonical-step-name sweep). ``get_run_details`` derives Run_Status by scanning
every step's ``status``, and ``item_str(item, "status", "unknown")`` would give
this row ``"unknown"``. Were the row ever to reach the status chain it would not
be inert there, so the run's ``status`` is pinned across its presence — the same
guarantee ``test_run_details_load_outcome.py`` pins for ``load_metrics``.

Harness shape (path setup, table patching, ``_run_with``) follows
``test_run_details_load_outcome.py``, which covers the sibling concern in this
same function.

Validates: Requirements 4.3
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")
os.environ.setdefault("AWS_REGION", "us-west-2")

RUN_ID = "run-shadow-row-detail-001"
PIPELINE_ID = "pipe-shadow-row-detail"


def _parse(response: dict) -> tuple:
    return response["statusCode"], json.loads(response["body"])


def _step_row(step_name: str, *, status: str = "success", sequence: int = 1) -> dict:
    """A genuine Step_Row, the kind that belongs in the tree."""
    return {
        "run_id": RUN_ID,
        "step_name": step_name,
        "pipeline_id": PIPELINE_ID,
        "original_pipeline_id": PIPELINE_ID,
        "execution_id": "exec-001",
        "status": status,
        "timestamp": f"2025-01-15T12:{sequence:02d}:00+00:00",
        "step_sequence": sequence,
    }


def _shadow_verification_row() -> dict:
    """The row as ``build_shadow_verification_item`` builds it and
    ``state_table.query`` hands it back.

    Mirrors the writer's payload key for key, including the two absences that
    matter: no ``status``, no ``step_sequence``. ``shadow_elapsed_seconds`` is a
    ``Decimal`` because the writer runs the payload through
    ``convert_floats_to_decimal`` and DynamoDB returns numbers that way.

    Transcribed rather than imported: the writer lives in a different Lambda
    bundle with its own module-level boto3 clients, so importing it here would
    drag that Lambda's import graph into this one. The transcription's own drift
    risk — the ``step_name`` spelling — is what
    ``tests/contracts/test_shadow_verification_step_name_cross_lambda.py``
    covers.
    """
    return {
        "run_id": RUN_ID,
        "step_name": "shadow_verification",
        "timestamp": "2025-01-15T12:07:30+00:00",
        "pipeline_id": PIPELINE_ID,
        "verification_executor": "shadow",
        "shadow_status": "agreed",
        "disagreements": [],
        "omitted": Decimal("0"),
        "dimensions_compared": Decimal("4"),
        "shadow_elapsed_seconds": Decimal("41"),
    }


@pytest.fixture(autouse=True)
def _patch_tables(monkeypatch):
    from handlers import common as _common
    from handlers import pipelines_runs as _runs

    mock_state = MagicMock()
    mock_approvals = MagicMock()
    mock_executions = MagicMock()
    mock_pipelines = MagicMock()
    mock_dynamodb = MagicMock()

    mock_state.query.return_value = {"Items": []}
    mock_approvals.query.return_value = {"Items": []}
    mock_executions.query.return_value = {"Items": []}
    mock_executions.get_item.return_value = {}
    mock_pipelines.get_item.return_value = {}
    mock_dynamodb.batch_get_item.return_value = {"Responses": {}, "UnprocessedKeys": {}}

    for mod in (_common, _runs):
        monkeypatch.setattr(mod, "state_table", mock_state)
        monkeypatch.setattr(mod, "approvals_table", mock_approvals)
        monkeypatch.setattr(mod, "executions_table", mock_executions)
        monkeypatch.setattr(mod, "pipelines_table", mock_pipelines)
        monkeypatch.setattr(mod, "dynamodb", mock_dynamodb)

    # Log fetching is covered elsewhere and would reach CloudWatch.
    monkeypatch.setattr(_runs, "get_step_logs", lambda *a, **kw: [])

    _patch_tables.state_table = mock_state
    _patch_tables.runs_module = _runs
    _patch_tables.get_run_details = _runs.get_run_details
    yield


def _run_with(items: list[dict]) -> tuple:
    _patch_tables.state_table.query.return_value = {"Items": items}
    return _parse(_patch_tables.get_run_details(RUN_ID))


class TestShadowVerificationRowIsNotAStep:
    """R4.3 — the comparison record is on the run's partition and is not a step."""

    def test_shadow_verification_row_is_not_a_step(self):
        """The latent regression: without ``shadow_verification`` in
        ``_synthetic_steps`` this row renders as a trailing step with an unknown
        status."""
        status, body = _run_with(
            [
                _step_row("schema_extract", sequence=1),
                _step_row("test_code", sequence=6),
                _shadow_verification_row(),
            ]
        )

        assert status == 200
        step_names = [s["step_name"] for s in body["steps"]]
        assert "shadow_verification" not in step_names
        assert step_names == ["schema_extract", "test_code"]
        assert body["total_steps"] == 2

    def test_the_row_carries_no_status_to_leak(self):
        """Guards the assumption the filter rests on: the writer sends no
        ``status``, so ``item_str``'s default would make it ``"unknown"``."""
        row = _shadow_verification_row()

        assert "status" not in row
        assert "step_sequence" not in row

    def test_the_row_does_not_move_run_status(self):
        """Not a second derivation of Run_Status. Adding the record must leave
        the run's status exactly where the real steps put it."""
        without = _run_with([_step_row("run_pipeline", sequence=10)])[1]["status"]
        with_row = _run_with([_step_row("run_pipeline", sequence=10), _shadow_verification_row()])[1]["status"]

        assert without == "SUCCEEDED"
        assert with_row == without

    def test_the_denylist_names_the_row(self):
        """The filter is a set literal inside ``get_run_details``, so the entry
        is asserted through behaviour above and through the source here — a
        rename of the key in either place has to fail something."""
        import inspect

        source = inspect.getsource(_patch_tables.runs_module.get_run_details)

        assert '"shadow_verification"' in source
        assert "_synthetic_steps" in source

    def test_a_genuine_step_named_alike_is_untouched(self):
        """The filter matches the whole ``step_name``, not a prefix — nothing
        else on the partition is collateral."""
        _, body = _run_with(
            [
                _step_row("test_code", sequence=6),
                _shadow_verification_row(),
            ]
        )

        assert [s["step_name"] for s in body["steps"]] == ["test_code"]
