# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 R8.4 / task 5.2 — the pipeline-record write path for the Selected_Worksheet.

The subject is ``handlers/pipelines_crud.py``'s ``create_pipeline`` and its helper
``_submitted_selected_worksheet``. The **reader** is
``schema_extract._configured_worksheet``, in a different Lambda, and the two are
only ever compared through ``SELECTED_WORKSHEET_ATTRIBUTE``.

## What makes these tests load-bearing rather than shape checks

The reader tests ``SELECTED_WORKSHEET_ATTRIBUTE in record`` — a **membership**
test, never a truth test — so absent and empty mean opposite things:

* attribute **absent** → no human has chosen. A single-sheet workbook proceeds
  (R8.1); a multi-sheet one refuses and names every Worksheet present (R8.4
  residual).
* attribute **present**, *including* ``""`` → a selection naming no sheet, which
  refuses under R8.5 rather than falling back to the workbook's active sheet.

So persisting ``""`` for "the user chose nothing" would break **every single-sheet
Excel pipeline** created through the wizard, and it would do so silently: the
record would look populated and every run would refuse. That is the failure this
module pins.

## The expected key is imported, never transcribed

Every assertion names ``SELECTED_WORKSHEET_ATTRIBUTE`` rather than the literal
``"selected_worksheet"``. A test written from the producer's own literal cannot
detect the producer drifting (``LGR-28b4d75-5``, **template / ratified**), and
here the literal has to agree with a constant in *another* Lambda's read path —
which is precisely ``LGR-eee8ac8-5``'s silent-disagreement case.

## What is deliberately NOT asserted

That an unknown or impossible worksheet name is **rejected** with a 400. It is
not, and must not be: no workbook exists at creation time, so existence is
unknowable here, and ``create_pipeline``'s permissive posture is governed by
``LGR-cad8032-2`` (**template / ratified**), whose blast radius is nine test
modules asserting 201. The wizard blocks an impossible name client-side and R8.5
catches a merely-absent one at run time, naming both sides.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

# Stub out Lambda-layer dependencies before importing.
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = MagicMock()
sys.modules["env_utils"].require_env = lambda name, default=None: default or name

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

try:
    from handlers.pipelines import create_pipeline
    from handlers.pipelines_crud import _submitted_selected_worksheet

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


def _body(**extra) -> str:
    payload = {
        "pipeline_name": "Worksheet Pipeline",
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "source_type": "S3_UPLOAD",
    }
    payload.update(extra)
    return json.dumps(payload)


def _boto3_stub(mock_boto3):
    """Make the schema read fail so `create_pipeline` skips schema loading."""
    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: json.dumps({"status": "ok"}).encode())}
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_lambda
        if svc == "lambda"
        else MagicMock(
            get_object=MagicMock(side_effect=Exception("skip")),
            get_caller_identity=MagicMock(return_value={"Account": "123456789012"}),
        )
    )


def _written_item(mock_table) -> dict:
    mock_table.put_item.assert_called_once()
    return mock_table.put_item.call_args[1]["Item"]


# ─── The helper, in isolation ──────────────────────────────────────────────


class TestSubmittedSelectedWorksheet:
    """`_submitted_selected_worksheet` returns `None` for "write no attribute"."""

    def test_absent_key_is_no_selection(self):
        assert _submitted_selected_worksheet({}) is None

    def test_empty_string_is_no_selection_and_is_never_stored(self):
        # `""` must NOT round-trip to the record. Stored, it would be a selection
        # naming no sheet and would refuse every run (R8.5).
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: ""}) is None

    def test_whitespace_only_is_no_selection(self):
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: "   \t"}) is None

    def test_none_is_no_selection(self):
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: None}) is None

    def test_non_string_is_no_selection_rather_than_coerced(self):
        # Coercing would record a selection the caller did not make. A numeric
        # sheet name has to arrive as a string from the producer.
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: 3}) is None
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: ["Sheet1"]}) is None

    def test_a_chosen_name_is_returned(self):
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: "Q3 Actuals"}) == "Q3 Actuals"

    def test_padding_survives_because_the_gate_trims_and_the_value_does_not(self):
        # An Excel worksheet may legitimately be named with padding, so trimming
        # the stored value would make that sheet permanently unselectable while
        # looking correct. The gate above still rejects whitespace-ONLY input.
        assert _submitted_selected_worksheet({SELECTED_WORKSHEET_ATTRIBUTE: " Data "}) == " Data "


# ─── The write path ────────────────────────────────────────────────────────


class TestCreatePipelineWritesTheSelection:
    @patch("handlers.pipelines_crud.boto3")
    @patch("handlers.pipelines_crud.pipelines_table")
    def test_no_attribute_when_the_request_omits_it(self, mock_table, mock_boto3):
        _boto3_stub(mock_boto3)

        response = create_pipeline(_body())

        assert response["statusCode"] == 201
        # `not in`, not a falsy check: `{key: ""}` and no key at all are exactly
        # the two shapes that must not be confused, and a truthiness assertion
        # would pass for both.
        assert SELECTED_WORKSHEET_ATTRIBUTE not in _written_item(mock_table)

    @patch("handlers.pipelines_crud.boto3")
    @patch("handlers.pipelines_crud.pipelines_table")
    def test_no_attribute_when_the_request_sends_an_empty_string(self, mock_table, mock_boto3):
        _boto3_stub(mock_boto3)

        response = create_pipeline(_body(**{SELECTED_WORKSHEET_ATTRIBUTE: ""}))

        assert response["statusCode"] == 201
        assert SELECTED_WORKSHEET_ATTRIBUTE not in _written_item(mock_table)

    @patch("handlers.pipelines_crud.boto3")
    @patch("handlers.pipelines_crud.pipelines_table")
    def test_the_chosen_worksheet_is_written_top_level(self, mock_table, mock_boto3):
        _boto3_stub(mock_boto3)

        response = create_pipeline(_body(**{SELECTED_WORKSHEET_ATTRIBUTE: "Q3 Actuals"}))

        assert response["statusCode"] == 201
        item = _written_item(mock_table)
        # TOP-LEVEL, because the reader's membership test is against the record
        # itself (`schema_extract._configured_worksheet`), not against
        # `source_config`. Nested, it would never be found.
        assert item[SELECTED_WORKSHEET_ATTRIBUTE] == "Q3 Actuals"
        assert SELECTED_WORKSHEET_ATTRIBUTE not in item.get("source_config", {})

    @patch("handlers.pipelines_crud.boto3")
    @patch("handlers.pipelines_crud.pipelines_table")
    def test_a_padded_name_is_stored_verbatim(self, mock_table, mock_boto3):
        _boto3_stub(mock_boto3)

        create_pipeline(_body(**{SELECTED_WORKSHEET_ATTRIBUTE: " Data "}))

        assert _written_item(mock_table)[SELECTED_WORKSHEET_ATTRIBUTE] == " Data "

    @patch("handlers.pipelines_crud.boto3")
    @patch("handlers.pipelines_crud.pipelines_table")
    def test_an_unverifiable_name_still_returns_201(self, mock_table, mock_boto3):
        """`LGR-cad8032-2` — the creation path makes no claim it cannot verify, and
        it answers by writing or not writing, never by rejecting the request.

        Whether the sheet exists is unknowable here: the upload prefix this
        workbook would arrive at is created by this very request.
        """
        _boto3_stub(mock_boto3)

        response = create_pipeline(_body(**{SELECTED_WORKSHEET_ATTRIBUTE: "Sheet That May Not Exist"}))

        assert response["statusCode"] == 201
        assert _written_item(mock_table)[SELECTED_WORKSHEET_ATTRIBUTE] == "Sheet That May Not Exist"
