# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: ``timeout_behavior`` on a written approval record is always "manual".

p47 R6 ratified Arm B — ``auto_approve`` / ``auto_reject`` are removed from the
advertised config surface rather than made reachable. These tests pin the
producer-side fact that decision rests on, which had no coverage: **no code path
can write any value but "manual" onto an approval record.**

That matters because the value is what the sweeper dispatches on
(``approval_timeout_handler/lambda_function.py``'s ``process_approval_timeout``),
and the sweeper's ``auto_approve`` / ``auto_reject`` enforcement code was
deliberately RETAINED rather than deleted. Retaining it is only defensible while
nothing can select it, so that is the thing worth a test: if a producer for
``pipeline_config.approvals.timeout_behavior`` ever appears, the branch stops
being unreached and the un-advertising becomes a lie. These assertions fail in
that case, which is the intended signal to read the p47 R6 record before going
further.

THE TWO BLOCKERS, AND WHICH ONE THIS FILE COVERS
------------------------------------------------
1. *No sweep can arrive in time.* ``LGR-69ca78b-2`` (template / ratified) records
   it: the Step Functions gate abandons the execution at exactly 72 h while the
   sweep runs every 15 minutes, so it always finds a consumed token. Already
   asserted, from the other side, by
   ``tests/contracts/test_approval_window_cross_language.py`` — which pins the
   window/gate equality by *parsing* ``APPROVAL_TIMEOUT_SECONDS`` out of the CDK
   source. **This file does not restate those numbers**; a second literal is the
   drift that test exists to prevent.
2. *Nothing can set the value.* ``timeout_behavior`` is read at
   ``lambda_function.py:411`` out of ``get_pipeline_config``'s ``approvals``
   sub-map. ``get_pipeline_config`` reads a ``pipeline_config`` JSON string off a
   **state-table** row; every config surface that writes approval settings writes
   an ``approval_config`` **map** on the **pipelines** table instead
   (``approval_config/lambda_function.py:85``, ``pipelines_crud.py:903`` and
   ``:1140-1159``). Different table, different attribute, different nesting. The
   state-row attribute has no producer at all. **This is the blocker this file
   covers**, and it is the one ``LGR-69ca78b-2`` does not name — it was found at
   p47 C1.

WHAT THIS FILE DELIBERATELY DOES NOT DUPLICATE
----------------------------------------------
* ``tests/unit/lambdas/approval_timeout/test_timeout_sweep_dispatch.py``
  (``TestTimeoutBehaviorDispatch``) already pins the consumer side in full: all
  three declared values dispatch to their branch, and an absent or unrecognised
  value falls through to ``manual`` without raising. Nothing here re-asserts
  dispatch.
* ``test_advertised_window_precedence.py`` pins ``timeout_at`` resolution and the
  per-pipeline ``timeout_hours`` override (``LGR-69ca78b-2``). Nothing here
  asserts anything about the deadline.
* ``tests/contracts/test_approval_window_cross_language.py`` owns the
  window-equals-gate relationship. Nothing here touches it.

The module-loading preamble mirrors ``test_advertised_window_precedence.py`` in
the same directory: several Lambda dirs contain a module named
``lambda_function``, and this one resolves ``DEFAULT_TIMEOUT_HOURS`` at module
scope, so it is loaded from its path under a unique alias with AWS clients
mocked out.

Validates: Requirements R6.1, R6.3 (p47)
"""

import importlib.util
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

for _p in (_LAMBDA_DIR, _SHARED_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REQUIRED_ENV = {
    "AGENTIC_DATA_INGESTION_ENV": "local",
    "APPROVALS_TABLE": "test-approvals",
    "STATE_TABLE": "test-state",
    "TASK_TOKENS_TABLE": "test-task-tokens",
}

_EVENT = {
    "approval_type": "schema_mapping",
    "pipeline_id": "pipe-1",
    "run_id": "run-1",
}

#: The one advertised value. Named once so a reader of a failure sees the claim.
ONLY_ADVERTISED_BEHAVIOR = "manual"


def _load_handler(alias: str):
    """Import the approval handler fresh, with AWS clients mocked out."""
    for name, value in _REQUIRED_ENV.items():
        os.environ.setdefault(name, value)

    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_resource.return_value.Table.side_effect = lambda _name: MagicMock()
        sys.modules.pop(alias, None)
        spec = importlib.util.spec_from_file_location(alias, os.path.join(_LAMBDA_DIR, "lambda_function.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
    return module


@pytest.fixture
def handler():
    return _load_handler("ah_timeout_behavior_unadvertised")


def _written_record(handler, state_items: list[dict]) -> dict:
    """Create an approval against a given set of state rows; return the item written.

    ``state_items`` is what ``state_table.query`` returns, i.e. the rows
    ``get_pipeline_config`` walks looking for a ``pipeline_config`` attribute.
    Nothing is patched over ``get_pipeline_config`` itself — the read path under
    test is the deployed one, which is the whole point here.
    """
    captured: dict = {}
    handler.approvals_table = MagicMock()
    handler.approvals_table.put_item.side_effect = lambda **kw: captured.update(kw["Item"])
    handler.state_table = MagicMock()
    handler.state_table.query.return_value = {"Items": state_items}

    with (
        patch.object(handler, "get_approval_data", return_value={}),
        patch.object(handler, "dynamodb", MagicMock()),
    ):
        handler.create_approval_request(dict(_EVENT))

    return captured


class TestOnlyManualIsWritable:
    """No reachable input puts a non-"manual" behaviour on an approval record."""

    def test_no_state_rows_writes_manual(self, handler):
        """The default-config path — the one every deployment takes."""
        record = _written_record(handler, [])

        assert record["timeout_behavior"] == ONLY_ADVERTISED_BEHAVIOR

    def test_state_rows_without_a_pipeline_config_attribute_write_manual(self, handler):
        """Rows exist but carry no ``pipeline_config``: still the default path.

        This is the real shape of the state table. ``get_pipeline_config`` walks
        every row looking for the attribute and finds it on none of them, because
        nothing writes it.
        """
        record = _written_record(
            handler,
            [
                {"run_id": "run-1", "step_name": "schema_extract", "status": "completed"},
                {"run_id": "run-1", "step_name": "schema_mapping", "status": "completed"},
            ],
        )

        assert record["timeout_behavior"] == ONLY_ADVERTISED_BEHAVIOR

    def test_pipelines_table_approval_config_does_not_reach_the_record(self, handler):
        """An ``approval_config`` map cannot select a timeout behaviour.

        This is blocker 2 stated as a test. Every config surface — the
        ``approval_config`` Lambda and both ``pipelines_crud`` write paths — puts
        an ``approval_config`` MAP on the PIPELINES table. ``get_pipeline_config``
        reads a ``pipeline_config`` JSON STRING off a STATE row. Feeding a state
        row the pipelines-table shape, with ``auto_approve`` set as loudly as
        possible, changes nothing: different attribute, different nesting.

        If this ever starts returning ``auto_approve``, the plumbing was
        converged and the p47 R6 posture needs revisiting before the sweeper's
        retained branches can be trusted to be unreached.
        """
        record = _written_record(
            handler,
            [
                {
                    "run_id": "run-1",
                    "step_name": "schema_mapping",
                    "approval_config": {"timeout_behavior": "auto_approve"},
                }
            ],
        )

        assert record["timeout_behavior"] == ONLY_ADVERTISED_BEHAVIOR

    def test_echoed_pipeline_config_advertises_only_manual(self, handler):
        """The ``pipeline_config`` echoed onto the record carries "manual" too.

        ``:431`` serialises ``get_pipeline_config``'s result onto the approval
        record. That echo is the only writer of anything named
        ``pipeline_config`` on this path, and it must not reintroduce the
        option list it just stopped advertising.
        """
        record = _written_record(handler, [])

        echoed = json.loads(record["pipeline_config"])

        assert echoed["approvals"]["timeout_behavior"] == ONLY_ADVERTISED_BEHAVIOR


class TestTheUnreachableOverrideStillWorksIfItEverArrives:
    """The read path is intact — it is starved of a producer, not broken.

    Worth pinning separately, because "unreachable" is a claim about the
    *producer*, and the two are easy to conflate. If this test started failing,
    the correct reading would be that the reader broke, not that the feature
    became safe.
    """

    def test_a_state_row_carrying_pipeline_config_is_honoured(self, handler):
        """A hand-planted state row does select a behaviour.

        No producer writes this shape (verified at p47 C1/C3: nothing under
        ``lambda/``, ``shared/``, ``deploy-cdk/lib/`` or ``agentcore/`` writes a
        ``pipeline_config`` attribute to a state row), which is exactly why the
        preceding class holds in every deployment. This is the *only* input that
        selects an auto behaviour, and it is constructed here by the test rather
        than by any code path.
        """
        record = _written_record(
            handler,
            [
                {
                    "run_id": "run-1",
                    "step_name": "schema_mapping",
                    "pipeline_config": json.dumps({"approvals": {"timeout_behavior": "auto_reject"}}),
                }
            ],
        )

        assert record["timeout_behavior"] == "auto_reject"
