# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: ``shared/pipeline_record_read.read_pipeline_record`` (p47 E2).

The module under test is the one definition of the pipelines-table record read.
These tests pin the mechanism itself, once, rather than restating what the five
per-Lambda ``test_read_path_errors.py`` files already assert per site.

WHAT IS DELIBERATELY *NOT* RE-ASSERTED HERE
-------------------------------------------
R12.7: a sixth copy of an existing assertion is not progress. Each migrated
caller's own ``test_read_path_errors.py`` already pins, *through* its Lambda
module, that a throttle raises ``TransientStepFailure`` with that Lambda's
``step_name``, that access-denied raises ``StepFailure``, and that a missing
``Item`` yields that caller's sentinel. Those are the per-site contracts and
they stay where they are — R12.8 requires all five to pass unmodified, and they
do. What is new here is only what those files cannot express: that there is one
mechanism, and that it is the mechanism which is parameterised rather than
forked.

THE FOUR SEAMS, AND WHY THEY ARE ARGUMENTS
------------------------------------------
``pipelines_table``, ``logger`` and ``classify`` all arrive as arguments so that
each caller resolves them from *its own* module globals at call time. Around 60
existing test sites patch module attributes on the Lambda under test
(``patch.object(_mod, "dynamodb", …)``, ``patch.object(_mod, "pipelines_table", …)``,
``patch.object(_mod, "raise_for_read_client_error", …)``); a primitive that
resolved any of the three internally would leave those patches inert. Inert is
worse than broken here: ``test_code``'s
``test_an_already_classified_step_failure_is_not_reclassified_on_the_way_out``
would still *pass*, having stopped testing anything. That is p47 finding 20's
failure mode, so ``test_classify_is_taken_from_the_caller_not_resolved_here``
pins the seam directly.

**Validates: Requirements R12.4, R12.8**
"""

import logging
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

import pipeline_record_read as prr
from step_errors import StepFailure, TransientStepFailure

PIPELINE_ID = "pipe-e2"
RECORD = {"pipeline_id": PIPELINE_ID, "ai_model": "anthropic.claude-3", "target_schema_bucket": "b"}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


def _table(*, returns=None, raises=None) -> MagicMock:
    table = MagicMock()
    if raises is not None:
        table.get_item.side_effect = raises
    else:
        table.get_item.return_value = returns if returns is not None else {}
    return table


def _read(table, *, not_found, step_name="test_step", logger=None, **kwargs):
    return prr.read_pipeline_record(
        table,
        PIPELINE_ID,
        step_name=step_name,
        operation="get_pipeline_metadata",
        not_found=not_found,
        logger=logger if logger is not None else MagicMock(),
        **kwargs,
    )


class TestTheKeyShape:
    """The key is built here, so it is one definition and pinned once.

    E1 §3 left the per-caller ``Key`` pin conditional on this design choice: if
    key construction had become a caller-supplied argument, three sites would
    have needed their own pin. It did not — the primitive owns the key — so
    ``test_code``'s existing ``assert_called_once_with(Key={"pipeline_id": …})``
    and this test cover it between them.
    """

    def test_key_is_the_partition_attribute_only(self):
        assert prr.pipeline_record_key(PIPELINE_ID) == {"pipeline_id": PIPELINE_ID}

    def test_the_record_is_read_by_that_key(self):
        table = _table(returns={"Item": RECORD})
        _read(table, not_found=None)
        table.get_item.assert_called_once_with(Key={"pipeline_id": PIPELINE_ID})

    def test_the_attribute_name_is_a_named_constant(self):
        """A literal at the call site is how a key shape drifts."""
        assert prr.PIPELINE_ID_ATTRIBUTE == "pipeline_id"

    def test_the_selected_worksheet_attribute_name_is_a_named_constant(self):
        """p55 R8.3: one spelling, because the reader and the writer are in
        different Lambdas and are only ever compared through this string.

        A second spelling would not fail — it would make the worksheet selection
        silently never match, so a configured pipeline would keep refusing
        multi-sheet workbooks as if nothing had been chosen. That is
        ``LGR-eee8ac8-5``'s silent-disagreement failure mode, which is the case
        that record says warrants one shared definition.

        Pinned here rather than at either end for the same reason
        ``PIPELINE_ID_ATTRIBUTE`` is: a literal at a call site is how the name
        drifts. This module is **not** a reader of the attribute — the primitive
        returns the whole record and each caller reads its own attributes — so
        this is a name pin and not a behaviour claim.
        """
        assert prr.SELECTED_WORKSHEET_ATTRIBUTE == "selected_worksheet"


class TestTheRecordIsReturnedUnchanged:
    def test_a_present_record_is_returned_as_is(self):
        table = _table(returns={"Item": RECORD})
        assert _read(table, not_found=None) is RECORD

    def test_a_present_record_is_returned_even_when_the_sentinel_is_a_dict(self):
        """Guards against ``return item or not_found``-style collapsing."""
        table = _table(returns={"Item": RECORD})
        assert _read(table, not_found={}) is RECORD

    def test_an_empty_record_is_still_a_present_record(self):
        """A record that exists and carries nothing is not absence.

        This is the distinction ``LGR-b2f79ef-8`` is about, one level down: the
        mechanism must key on ``"Item" in response``, not on the item's truth
        value.
        """
        empty_item: dict = {}
        table = _table(returns={"Item": empty_item})
        assert _read(table, not_found=None) is empty_item


class TestBothNotFoundPathsReturnTheCallersSentinel:
    """Two distinct code paths return the same value; R12.4 shares both."""

    @pytest.mark.parametrize("sentinel", [None, {}])
    def test_missing_item_returns_the_sentinel(self, sentinel):
        assert _read(_table(returns={}), not_found=sentinel) == sentinel

    @pytest.mark.parametrize("sentinel", [None, {}])
    @pytest.mark.parametrize("code", ["ResourceNotFoundException", "NotFound"])
    def test_a_not_found_client_error_returns_the_sentinel(self, sentinel, code):
        table = _table(raises=_client_error(code))
        assert _read(table, not_found=sentinel) == sentinel

    def test_the_sentinel_is_returned_by_identity_not_reconstructed(self):
        """``not_found`` comes back as the object handed in.

        A primitive that rebuilt the value could silently normalise ``{}`` and
        ``None`` toward each other, which is precisely what R12.1 forbids.
        """
        sentinel: dict = {}
        assert _read(_table(returns={}), not_found=sentinel) is sentinel


class TestInfrastructureErrorsRaiseClassified:
    """P28 R17.3. The classification belongs to the mechanism, not the caller."""

    @pytest.mark.parametrize(
        "code",
        ["ThrottlingException", "ProvisionedThroughputExceededException", "InternalServerError", "RequestTimeout"],
    )
    def test_transient_codes_raise_transient_step_failure(self, code):
        table = _table(raises=_client_error(code))
        with pytest.raises(StepFailure) as exc_info:
            _read(table, not_found=None, step_name="schema_mapping")
        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == "schema_mapping"
        assert exc_info.value.details["operation"] == "get_pipeline_metadata"

    @pytest.mark.parametrize("code", ["AccessDeniedException", "ValidationException"])
    def test_other_client_errors_raise_permanent_step_failure(self, code):
        table = _table(raises=_client_error(code))
        with pytest.raises(StepFailure) as exc_info:
            _read(table, not_found={}, step_name="create_code")
        assert type(exc_info.value).__name__ == "StepFailure"
        assert exc_info.value.error_type == "permanent"
        assert exc_info.value.details["error_code"] == code

    def test_the_step_name_is_the_callers_not_this_modules(self):
        """Five callers, five step names — SFN Retry matches on them."""
        table = _table(raises=_client_error("ThrottlingException"))
        with pytest.raises(StepFailure) as exc_info:
            _read(table, not_found=None, step_name="test_code")
        assert exc_info.value.step_name == "test_code"

    def test_a_non_client_error_propagates_untouched(self):
        """``except ClientError`` is narrow on purpose.

        ``LGR-b2f79ef-13`` is the standing example of the cost of widening it:
        a blanket ``except Exception`` around a read re-labelled an
        already-classified ``StepFailure`` as a bare ``ValueError``. A widened
        clause here would degrade every non-``ClientError`` into a sentinel at
        three call sites at once.
        """
        table = _table(raises=RuntimeError("connection reset"))
        with pytest.raises(RuntimeError, match="connection reset"):
            _read(table, not_found=None)

    def test_an_already_classified_failure_is_not_reclassified(self):
        table = _table(raises=_client_error("ThrottlingException"))
        pre_classified = TransientStepFailure("test_code", "already classified")
        with pytest.raises(StepFailure) as exc_info:
            _read(table, not_found=None, classify=MagicMock(side_effect=pre_classified))
        assert exc_info.value is pre_classified


class TestTheNotFoundLogSurvivesTheExtraction:
    """``LGR-b2f79ef-8``: the ``logger.error`` is not dropped.

    Its reason is a property of the *mechanism*, not of one caller:
    ``ResourceNotFoundException`` is in ``NOT_FOUND_CLIENT_ERROR_CODES``, so a
    missing **table** and a missing **record** both return the sentinel and the
    log is the only place the two stay distinguishable. Centralising it is why
    a single site can no longer lose it.
    """

    def test_a_not_found_client_error_logs_at_error_through_the_callers_logger(self):
        logger = MagicMock()
        table = _table(raises=_client_error("ResourceNotFoundException"))
        _read(table, not_found=None, logger=logger)
        logger.error.assert_called_once()
        assert PIPELINE_ID in logger.error.call_args.args[0]

    def test_a_missing_item_owes_no_log(self):
        """P21 Req 6.4's antecedent is "a handler catches an exception"."""
        logger = MagicMock()
        _read(_table(returns={}), not_found=None, logger=logger)
        logger.error.assert_not_called()

    def test_a_present_record_owes_no_log(self):
        logger = MagicMock()
        _read(_table(returns={"Item": RECORD}), not_found=None, logger=logger)
        logger.error.assert_not_called()

    def test_the_logger_is_required(self):
        """Not optional, so no caller can quietly drop the log."""
        table = _table(returns={})
        with pytest.raises(TypeError):
            prr.read_pipeline_record(
                table,
                PIPELINE_ID,
                step_name="test_code",
                operation="get_pipeline_metadata",
                not_found=None,
            )

    def test_a_stdlib_logger_also_satisfies_the_protocol(self, caplog):
        """The Protocol exists because callers hold a Powertools ``Logger``.

        Checked against a real ``logging.Logger`` so the structural type is not
        just satisfied by ``MagicMock``.
        """
        table = _table(raises=_client_error("ResourceNotFoundException"))
        with caplog.at_level(logging.ERROR):
            assert _read(table, not_found=None, logger=logging.getLogger("prr-test")) is None
        assert any(r.levelno == logging.ERROR and PIPELINE_ID in r.getMessage() for r in caplog.records)


class TestTheSeamsStayCallerSide:
    def test_classify_is_taken_from_the_caller_not_resolved_here(self):
        """The seam ``test_code``'s reclassification test depends on.

        Patching ``step_errors.raise_for_read_client_error`` must NOT change the
        behaviour of a call that passed its own classifier — that is what proves
        the caller's ``patch.object(_mod, "raise_for_read_client_error", …)`` is
        the thing in control.
        """
        table = _table(raises=_client_error("ThrottlingException"))
        callers_classifier = MagicMock()
        with patch.object(prr, "raise_for_read_client_error", MagicMock(side_effect=AssertionError("wrong seam"))):
            assert _read(table, not_found=None, classify=callers_classifier) is None
        callers_classifier.assert_called_once()

    def test_the_default_classifier_is_the_shared_one(self):
        """A caller that does not thread it still gets real classification."""
        table = _table(raises=_client_error("ThrottlingException"))
        with pytest.raises(StepFailure) as exc_info:
            prr.read_pipeline_record(
                table,
                PIPELINE_ID,
                step_name="test_code",
                operation="get_pipeline_metadata",
                not_found=None,
                logger=MagicMock(),
            )
        assert type(exc_info.value).__name__ == "TransientStepFailure"

    def test_the_table_is_used_as_given_and_never_constructed_here(self):
        """E1 §4.2 / finding 25: a table *object*, not a table name.

        Acquisition timing stays at each caller, which is what keeps
        ``patch.object(_mod, "dynamodb", …)`` and
        ``patch.object(_mod, "pipelines_table", …)`` both working. A primitive
        that built the resource itself would break both.
        """
        source = prr.read_pipeline_record.__doc__ or ""
        assert "boto3" not in source
        table = _table(returns={"Item": RECORD})
        _read(table, not_found=None)
        assert table.get_item.call_count == 1
        # Nothing was asked of the table beyond the one read.
        assert [c[0] for c in table.method_calls] == ["get_item"]

    def test_this_module_raises_the_same_StepFailure_class_these_tests_assert_on(self):
        """Guards the double-import hazard ``test_target_schema_resolution.py`` names."""
        assert prr.raise_for_read_client_error.__globals__["StepFailure"] is StepFailure
