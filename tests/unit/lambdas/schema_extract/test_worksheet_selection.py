# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Worksheet selection: the three cases, and what each refusal must name.

Subject: ``lambda/agentic_data_ingestion_schema_extract/lambda_function.py`` —
``get_pipeline_metadata``, ``_configured_worksheet``, ``_read_excel_table``'s
selection arms, and the ``elif source_format == "excel"`` dispatch branch that
wires them together. p55 task 5.1.

**Validates: Requirements 8.1, 8.2, 8.3, 8.4, 8.5**

Three cases, and only one of them succeeds without a human
----------------------------------------------------------
1. **R8.1** — one Worksheet, no Selected_Worksheet → extract from that sheet.
   There is no ambiguity to resolve, so there is nothing for a human to decide.
2. **R8.4 residual** — more than one Worksheet, no Selected_Worksheet → **fail
   the run**, naming every Worksheet present and saying the selection is set on
   the pipeline. It does **not** default to the first sheet or to the active one.
3. **R8.5** — a Selected_Worksheet the workbook does not carry → **fail the
   run**, naming the configured value **and** the names present.

R8.2 (record which Worksheet was processed) and R8.3 (process the same one on
every subsequent run) are asserted alongside 1 and 3, because they are
properties of a run that succeeded rather than cases of their own.

What was already true before this task, and is asserted here anyway
-------------------------------------------------------------------
Task 4.3's ``_read_excel_table`` already raised on a Selected_Worksheet the
workbook lacks, and its message already named both sides — so **R8.5's refusal
was already built** and this file pins it rather than introducing it. What 4.3
did *not* have is any notion of pipeline configuration: the parameter existed
and nothing passed it, and the no-selection path read ``workbook.active``
unconditionally. So case 2 **succeeded** before this task, on the active sheet,
which is precisely what R8.4's residual forbids.

The two refusals are pinned here together despite that difference in age,
because a reader needs them side by side to see that neither falls back — the
rejected alternative in both cases is the same one (take the first sheet), and
it is rejected for the same reason (it ingests different data than the user
believes is in play, which is the failure R8.3 exists to prevent).

Why the active-sheet read is asserted to still work
--------------------------------------------------
``test_the_active_sheet_read_survives_for_a_caller_that_made_no_claim`` looks
like it contradicts case 2 and does not. ``refuse_ambiguous_multi_sheet``
distinguishes *no human has chosen* from *this caller did not say*, and only the
first is R8.4's subject. Task 4.3's property test
``test_the_field_count_matches_the_active_sheet_when_none_is_selected`` pins the
active-sheet read over generated **multi-sheet** workbooks, so a change that
made the reader refuse unconditionally would break ratified behaviour. This task
must not weaken it, and that test is the guard; the assertion here states the
distinction locally so the flag's default does not read as an oversight.

Ledger status
-------------
``why`` on ``schema_extract/lambda_function.py`` returns ``LGR-5c539a6-1/-2/-3``
and ``LGR-0739ab8-1``; the lines this task changes are uncommitted, so ``why``
on them reports file history with ``records: []`` and no record governs them
directly. Three records shaped what is asserted:

* ``LGR-5c539a6-2`` (**standing invariant**) — every key an ASL path could read
  off ``$.schema_extract_result`` exists on BOTH arms, resolved in Python and
  echoed unconditionally. ``processed_worksheet`` follows it: always present on
  the envelope, ``""`` where the source has no Worksheet.
* ``LGR-b397ee2-2`` / ``LGR-b397ee2-3`` (**template / ratified**) — the pipelines
  -record read goes through ``shared/pipeline_record_read.read_pipeline_record``
  with the table, logger and classifier as arguments, and each caller keeps its
  own module-level ``def get_pipeline_metadata`` because tests patch it as a
  module attribute. That is why these tests patch
  ``lf.get_pipeline_metadata`` rather than a boto3 client.
* ``LGR-60c4985-7`` (**template / ratified**) — the ``if``/``elif`` chain, its
  terminal ``else`` and the ``jdbc`` early guard are untouched by this task. No
  branch is added and none moves; the ``excel`` branch's body changed.
"""

import io
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROJECT_ROOT = TESTS_ROOT.parent

sys.path.insert(0, str(PROJECT_ROOT / "shared"))
sys.path.insert(0, str(PROJECT_ROOT / "lambda" / "agentic_data_ingestion_schema_extract"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test_state_table")

#: Named so ``convert_source_to_csv``'s ``adi-source-files-`` →
#: ``adi-processed-files-`` derivation resolves to a *different* bucket; with an
#: arbitrary name it resolves to itself and the converter fail-closes (task 4.3
#: owns that refusal).
SOURCE_BUCKET = "adi-source-files-123456789012-us-west-2"
PROCESSED_BUCKET = "adi-processed-files-123456789012-us-west-2"

RUN_ID = "run-2026-09-01-11-00-00-beef1234"
PIPELINE_ID = "pipe-worksheet-1"
EXECUTION_ARN = "arn:aws:states:us-west-2:123456789012:execution:adi:worksheet"
KEY = "uploads/book.xlsx"

ONLY_SHEET = "Ledger"
FIRST_SHEET = "Customers"
SECOND_SHEET = "Notes"
THIRD_SHEET = "Archive"


@pytest.fixture
def schema_extract():
    """The real ``schema_extract`` module with AWS mocked.

    Same shape as ``test_excel_routing_and_refusal.py``'s and task 4.3's:
    boto3 and the ``shared`` modules needing a non-Mock return are patched,
    then ``s3_client`` and ``table`` are injected. ``openpyxl`` is deliberately
    NOT patched — real ``.xlsx`` bytes go through the real reader, so a sheet
    list is observed rather than stipulated.

    Duplicated rather than shared because this directory has no ``conftest.py``
    and each sibling carries its own; the duplicate-mocking meta-test scans for
    ``moto.mock_aws()``, which none of these use.
    """
    mock_s3 = MagicMock()
    mock_table = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = mock_table

    mock_eb = MagicMock()
    mock_eb.get_eventbridge_publishers.return_value = (MagicMock(), MagicMock(), MagicMock())
    mock_env = MagicMock()
    mock_env.require_env.return_value = "test_state_table"
    mock_artifact = MagicMock()
    mock_artifact.create_output_data.return_value = {}

    with patch.dict(
        "sys.modules",
        {
            "boto3": MagicMock(),
            "eventbridge_utils": mock_eb,
            "env_utils": mock_env,
            "artifact_schema": mock_artifact,
            "metrics_facade": MagicMock(),
            "aws_lambda_powertools": MagicMock(),
        },
    ):
        import boto3 as _boto3

        _boto3.client.return_value = mock_s3
        _boto3.resource.return_value = mock_dynamodb

        for mod in list(sys.modules):
            if "lambda_function" in mod:
                del sys.modules[mod]
        import lambda_function

        lambda_function.s3_client = mock_s3
        lambda_function.table = mock_table
        yield lambda_function, mock_s3, mock_table


def _workbook_bytes(sheets):
    """Real ``.xlsx`` bytes for *sheets*, a ``{title: [header, *rows]}`` mapping.

    The first entry becomes the workbook's **active** sheet, by renaming the
    default rather than creating a second one — the same construction
    ``generate_binary_fixtures.write_xlsx`` uses, so "active" here means what it
    means for the committed fixture.
    """
    import openpyxl

    workbook = openpyxl.Workbook()
    for index, (title, rows) in enumerate(sheets.items()):
        worksheet = workbook.active if index == 0 else workbook.create_sheet()
        worksheet.title = title
        for row in rows:
            worksheet.append(list(row))
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _seed(mock_table, mock_s3, payload):
    """A ``file_validation`` state row plus the workbook bytes S3 will serve."""
    mock_table.get_item.return_value = {
        "Item": {
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "original_pipeline_id": PIPELINE_ID,
            "source_s3_uri": f"s3://{SOURCE_BUCKET}/{KEY}",
        }
    }
    mock_table.put_item.return_value = {}
    body = MagicMock()
    body.read.return_value = payload
    mock_s3.get_object.return_value = {"Body": body}


def _event():
    return {"run_id": RUN_ID, "execution_arn": EXECUTION_ARN, "source_format": "excel"}


def _stored_items(mock_table):
    return [call.kwargs["Item"] for call in mock_table.put_item.call_args_list if "Item" in call.kwargs]


def _schema_row(mock_table):
    rows = [item for item in _stored_items(mock_table) if item.get("step_name") == "schema_extract"]
    assert len(rows) == 1, f"expected exactly one schema_extract row, got {rows}"
    return rows[0]


SINGLE_SHEET = {ONLY_SHEET: [("ledger_id", "amount"), (1, "10.00"), (2, "20.00")]}
MULTI_SHEET = {
    FIRST_SHEET: [("customer_id", "name", "email"), (1, "Alice", "alice@example.com")],
    SECOND_SHEET: [("note_id", "customer_id", "note"), (1, 1, "called")],
    THIRD_SHEET: [("archived_at",), ("2026-01-01",)],
}


# ── Absence is read with a membership test ─────────────────────────────


class TestAbsenceIsMeaningful:
    """``_configured_worksheet``: the membership test, and what it refuses to coerce.

    This is the whole of "its absence must not be defaulted away", isolated from
    S3 and DynamoDB so the distinction is asserted on its own terms. The
    requirement is not that absence produces ``None`` — that is easy — it is that
    a **recorded empty string** does *not*, because an empty selection is a
    selection and must reach R8.5's refusal rather than the active-sheet
    fallback.
    """

    def test_an_absent_key_is_absence(self, schema_extract):
        """No ``selected_worksheet`` attribute at all → no human has chosen."""
        lf, _, _ = schema_extract
        assert lf._configured_worksheet({"pipeline_id": PIPELINE_ID}) is None

    def test_a_missing_record_is_absence(self, schema_extract):
        """No pipelines-table record → also no human has chosen.

        A missing record and a record with no selection are different facts, and
        this function collapses them **deliberately**: both mean the selection
        was never made, and the run's behaviour is identical either way. The
        distinction stays available upstream, where ``read_pipeline_record``'s
        ``not_found=None`` and its log keep a missing *table* legible.
        """
        lf, _, _ = schema_extract
        assert lf._configured_worksheet(None) is None

    def test_a_recorded_empty_string_is_PRESENT_not_absent(self, schema_extract):
        """``""`` is a recorded selection, and coercing it would be the defect.

        The failure this forbids is concrete: read with ``.get(...)`` tested for
        truth, a pipeline whose selection was recorded empty would silently fall
        back to ``workbook.active`` and ingest whichever sheet happened to be
        active — the exact behaviour R8.3 exists to prevent. Returning ``""``
        here is what routes it to R8.5's refusal instead, which names the
        configured value and the names present.
        """
        lf, _, _ = schema_extract
        assert lf._configured_worksheet({"selected_worksheet": ""}) == ""

    def test_a_whitespace_selection_is_also_present(self, schema_extract):
        """A blank-looking name is still a name, and still refuses.

        ``" "`` is not a Worksheet any workbook carries, so this reaches R8.5 and
        reports what was configured. Included because ``.strip()`` is the second
        way absence gets coerced away after truthiness.
        """
        lf, _, _ = schema_extract
        assert lf._configured_worksheet({"selected_worksheet": " "}) == " "

    def test_a_numeric_selection_is_coerced_to_its_string(self, schema_extract):
        """A number-like sheet name survives its DynamoDB round trip.

        ``Decimal("2024")`` is what a sheet named ``2024`` comes back as. It is
        stringified rather than passed through, so the R8.5 message reports the
        configured name instead of raising a ``TypeError`` a reader cannot act
        on. Note this is a *presence* case: it must not become ``None``.
        """
        from decimal import Decimal

        lf, _, _ = schema_extract
        assert lf._configured_worksheet({"selected_worksheet": Decimal("2024")}) == "2024"


# ── R8.1: one Worksheet, nothing to resolve ────────────────────────────


def test_a_single_sheet_workbook_with_no_selection_is_extracted(schema_extract, monkeypatch):
    """R8.1: exactly one Worksheet → extract from it, no human needed.

    Driven through ``lambda_handler`` because the claim is about a *run*: the
    dispatch consults pipeline configuration, finds none, and must still proceed.
    A single-sheet workbook offers no choice, so refusing here would fail runs
    that have nothing to decide — which is why the multi-sheet count, and not
    the mere absence of a selection, is what R8.4's refusal turns on.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(SINGLE_SHEET))

    with patch.object(lf, "get_pipeline_metadata", return_value={"pipeline_id": PIPELINE_ID}):
        result = lf.lambda_handler(_event(), None)

    assert result["statusCode"] == 200
    assert sorted(result["extracted_schema"]["properties"]) == ["amount", "ledger_id"]


def test_a_single_sheet_run_records_the_worksheet_it_processed(schema_extract, monkeypatch):
    """R8.2 on the single-sheet path, where the configured value is ``None``.

    The recorded value is taken from the **parsed table**, not from the
    configuration, and this is the case that proves the difference matters: with
    no selection and one sheet there is nothing configured to record, yet a
    Worksheet was unambiguously processed and has a name. Recording the request
    rather than the outcome would leave this run's row silent about which sheet
    its schema describes.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(SINGLE_SHEET))

    with patch.object(lf, "get_pipeline_metadata", return_value=None):
        result = lf.lambda_handler(_event(), None)

    assert result["processed_worksheet"] == ONLY_SHEET
    assert _schema_row(mock_table)["processed_worksheet"] == ONLY_SHEET


# ── R8.4 residual: multi-sheet with nothing chosen ─────────────────────


def test_a_multi_sheet_workbook_with_no_selection_fails_the_run(schema_extract, monkeypatch):
    """R8.4 residual: fail, name every Worksheet, and say where the choice goes.

    The rejected alternative is defaulting to the first sheet — or, as the code
    stood before this task, to the *active* one. Either ingests one sheet's data
    while the user may believe another is in play, and says nothing about it.
    That is the failure R8.3 exists to prevent, so the run fails instead.

    Three things the message must carry, and each is load-bearing rather than
    decorative: **every** sheet name, because that is the list the user has to
    choose from and a refusal that withheld it sends them back to the file;
    that the selection is set on the **pipeline**, because a plain "no worksheet
    selected" invites a retry of the run, which cannot help; and the object,
    because a pipeline can hold several uploads.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(MULTI_SHEET))

    with (
        patch.object(lf, "get_pipeline_metadata", return_value={"pipeline_id": PIPELINE_ID}),
        pytest.raises(lf.StepFailure) as exc_info,
    ):
        lf.lambda_handler(_event(), None)

    message = exc_info.value.message
    for sheet in (FIRST_SHEET, SECOND_SHEET, THIRD_SHEET):
        assert sheet in message, f"{sheet!r} is present in the workbook but not named in the refusal: {message}"
    assert "selected_worksheet" in message, message
    assert f"s3://{SOURCE_BUCKET}/{KEY}" in message, message

    # Permanent, for the same reason task 4.3's parse failure is: a workbook does
    # not acquire a selection on attempt two, so a retry is waste.
    assert exc_info.value.step_name == "schema_extract"
    assert exc_info.value.error_type == "permanent"


def test_the_no_selection_refusal_writes_no_schema_and_no_converted_object(schema_extract, monkeypatch):
    """The refusal is total: no success row, and nothing converted.

    Two ways a refusal could still leave a misleading trace. A stored
    ``schema_extract`` row with ``status`` ``success`` would make the run look
    complete to every reader of that row regardless of the raise. A converted
    CSV written before the refusal would leave an object in the processed bucket
    that a later ``readable_source_uri`` could name — one sheet's data, presented
    as the whole source.

    The second cannot happen here by construction, because the selection is
    resolved during the parse that precedes the conversion. It is asserted
    anyway: "by construction" is a property of today's ordering, and the
    conversion is one line below the parse.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(MULTI_SHEET))

    with (
        patch.object(lf, "get_pipeline_metadata", return_value={"pipeline_id": PIPELINE_ID}),
        pytest.raises(lf.StepFailure),
    ):
        lf.lambda_handler(_event(), None)

    mock_s3.put_object.assert_not_called()
    items = _stored_items(mock_table)
    assert [item for item in items if item.get("status") == "success"] == []

    failed = [item for item in items if item.get("status") == "failed"]
    assert len(failed) == 1, f"expected one failed state row, got {items}"
    assert failed[0]["step_name"] == lf.StepName.SCHEMA_EXTRACT


def test_the_active_sheet_read_survives_for_a_caller_that_made_no_claim(schema_extract):
    """The active-sheet path stays reachable where absence means "did not say".

    ``_read_excel_table`` refuses a multi-sheet workbook only when its caller
    asserts that absence is *real* absence — that a pipeline record was consulted
    and carried no selection. A direct caller has made no such claim, so it still
    gets ``workbook.active``.

    This is what keeps task 4.3's ratified behaviour intact:
    ``test_excel_field_count_prop.py``'s
    ``test_the_field_count_matches_the_active_sheet_when_none_is_selected``
    generates **multi-sheet** workbooks and asserts the active sheet's column
    count, so a reader that refused unconditionally would break it. Asserted here
    too, locally, so the flag's ``False`` default reads as a decision rather than
    an oversight.
    """
    lf, mock_s3, _ = schema_extract
    body = MagicMock()
    body.read.return_value = _workbook_bytes(MULTI_SHEET)
    mock_s3.get_object.return_value = {"Body": body}

    sheet_name, field_names, _ = lf._read_excel_table(SOURCE_BUCKET, KEY)

    assert sheet_name == FIRST_SHEET
    assert field_names == ["customer_id", "name", "email"]


# ── R8.5: a selection the workbook does not carry ──────────────────────


def test_a_selection_the_workbook_lacks_fails_and_names_both_sides(schema_extract, monkeypatch):
    """R8.5: report the configured name AND the names present.

    The case this exists for is a workbook whose sheet was renamed after the
    pipeline was configured. Both sides are required because either alone leaves
    the reader guessing: the configured name without the present ones does not
    say what to choose instead, and the present ones without the configured name
    does not say what the pipeline is asking for.

    *Rejected: falling back to the first sheet.* It silently ingests different
    data, which is the exact failure R8.3 exists to prevent — and it would do so
    on a pipeline whose owner had explicitly chosen, which is worse than the
    no-selection case rather than better.

    This refusal predates task 5.1 — task 4.3's ``_read_excel_table`` already
    raised it — and is pinned here because 5.1 is what makes it *reachable* from
    a pipeline record, and because the two refusals belong together.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(MULTI_SHEET))

    with (
        patch.object(lf, "get_pipeline_metadata", return_value={"selected_worksheet": "Customers (2025)"}),
        pytest.raises(lf.StepFailure) as exc_info,
    ):
        lf.lambda_handler(_event(), None)

    message = exc_info.value.message
    assert "Customers (2025)" in message, message
    for sheet in (FIRST_SHEET, SECOND_SHEET, THIRD_SHEET):
        assert sheet in message, message
    assert exc_info.value.error_type == "permanent"


def test_a_recorded_empty_selection_refuses_rather_than_reading_the_active_sheet(schema_extract, monkeypatch):
    """The membership test's consequence, asserted end to end.

    This is the case the whole read-absence-with-a-membership-test rule exists
    for, and it is asserted through ``lambda_handler`` rather than only against
    ``_configured_worksheet`` because the coercion that would break it can be
    reintroduced at either layer — a truthiness test in the reader is as
    sufficient to cause it as one in the resolver.

    A **single-sheet** workbook is used deliberately. Under R8.4 a multi-sheet
    workbook would refuse anyway, so it cannot distinguish "the empty string was
    treated as present" from "there were several sheets and none was chosen".
    With one sheet, the coerced behaviour would *succeed* — reading the only
    sheet — so only a refusal here shows the empty string was carried as a
    selection.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(SINGLE_SHEET))

    with (
        patch.object(lf, "get_pipeline_metadata", return_value={"selected_worksheet": ""}),
        pytest.raises(lf.StepFailure) as exc_info,
    ):
        lf.lambda_handler(_event(), None)

    message = exc_info.value.message
    assert ONLY_SHEET in message, message
    assert "''" in message, f"the configured (empty) selection is not reported: {message}"


# ── R8.2 / R8.3 on the configured path ─────────────────────────────────


def test_a_configured_run_processes_and_records_the_selected_worksheet(schema_extract, monkeypatch):
    """R8.2 and R8.3 together: the chosen sheet is read, and it is recorded.

    R8.3 — "process that Selected_Worksheet on every subsequent run" — needs no
    mechanism of its own once the selection is pipeline configuration rather than
    a per-run decision, which is design's stated reason for arm (a) (§4.4,
    W3-D5). What is asserted here is the property that makes that true: the run
    reads the configured sheet and **nothing about the run decides which**, so
    two runs of the same pipeline over the same configuration cannot diverge.

    The second sheet's columns are asserted absent as well as the first's
    present, because a schema that merely *contains* the right fields would also
    be produced by reading the wrong sheet of a workbook whose headers overlap.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(MULTI_SHEET))

    with patch.object(lf, "get_pipeline_metadata", return_value={"selected_worksheet": SECOND_SHEET}):
        result = lf.lambda_handler(_event(), None)

    assert result["statusCode"] == 200
    assert sorted(result["extracted_schema"]["properties"]) == ["customer_id", "note", "note_id"]
    assert "email" not in result["extracted_schema"]["properties"], "the active sheet was read, not the selected one"

    # R8.2 at both addresses: the envelope Step Functions carries, and the row
    # every Python consumer reads.
    assert result["processed_worksheet"] == SECOND_SHEET
    assert _schema_row(mock_table)["processed_worksheet"] == SECOND_SHEET


def test_the_selection_is_read_from_the_pipeline_record_not_from_the_event(schema_extract, monkeypatch):
    """R8.3's mechanism: the pipeline id is what the selection is looked up by.

    If the selection travelled on the event instead, "the same Worksheet on every
    subsequent run" would depend on every producer of that event agreeing to
    carry it — the S3-upload trigger, a manual run, a retry — and a producer that
    forgot would silently change which sheet was ingested. Reading it by pipeline
    id is what makes R8.3 hold for producers that know nothing about worksheets.

    The looked-up id is the run's ``original_pipeline_id`` from the
    ``file_validation`` row, not ``event["pipeline_id"]``, matching every other
    pipeline-record read on this path.
    """
    lf, mock_s3, mock_table = schema_extract
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _seed(mock_table, mock_s3, _workbook_bytes(MULTI_SHEET))

    reader = MagicMock(return_value={"selected_worksheet": THIRD_SHEET})
    with patch.object(lf, "get_pipeline_metadata", reader):
        result = lf.lambda_handler(_event(), None)

    reader.assert_called_once_with(PIPELINE_ID)
    assert result["processed_worksheet"] == THIRD_SHEET


# ── The envelope key is never conditional ──────────────────────────────


def test_processed_worksheet_is_present_and_empty_for_a_source_with_no_worksheet(schema_extract):
    """``processed_worksheet`` is echoed on every path, never absent.

    ``LGR-5c539a6-2`` (**standing invariant**) requires every key an ASL path
    could read off ``$.schema_extract_result`` to exist on both arms, because a
    missing payload path raises ``States.ParameterPathFailure``, which neither
    Retry nor Catch can intercept. Nothing reads this key from the state machine
    today; it is echoed unconditionally now so that a future reader does not need
    a deploy boundary of exactly that shape, which is the cost task 4.4 measured
    at 19 replayable runs.

    A CSV source is used because it is the ordinary case: it has no Worksheet, so
    the honest value is empty — and *empty* rather than *absent* is the whole
    distinction being asserted. The row is checked in the other direction: there
    the attribute is omitted, because on a row absence means "this source has no
    Worksheet" and a reader gets a ``.get()`` rather than a failure.
    """
    lf, _mock_s3, mock_table = schema_extract
    mock_table.get_item.return_value = {
        "Item": {
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "original_pipeline_id": PIPELINE_ID,
            "source_s3_uri": f"s3://{SOURCE_BUCKET}/uploads/data.csv",
        }
    }
    mock_table.put_item.return_value = {}

    csv_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with patch.object(lf, "extract_schema_from_csv", return_value=csv_schema):
        result = lf.lambda_handler(
            {"run_id": RUN_ID, "execution_arn": EXECUTION_ARN, "source_format": "csv"},
            None,
        )

    assert "processed_worksheet" in result, "the key is conditional; an absent ASL path is uncatchable"
    assert result["processed_worksheet"] == ""
    assert "processed_worksheet" not in _schema_row(mock_table)


def test_a_csv_run_does_not_read_the_pipeline_record_at_all(schema_extract):
    """The pipeline-record read is on the Excel path only.

    ``schema_extract`` held ``STATE_TABLE`` alone before this task and reached
    pipeline configuration not at all, so the read is new surface on a Lambda
    that runs on every pipeline. Keeping it inside the ``excel`` branch — rather
    than hoisting it beside ``get_previous_state`` where it would read more
    naturally — is what stops a CSV or Parquet run paying a DynamoDB read, and
    what stops ``require_env("PIPELINES_TABLE")`` being reachable on a path that
    does not need it. A deploy that lands this code before the environment
    variable therefore degrades the Excel path only.
    """
    lf, _mock_s3, mock_table = schema_extract
    mock_table.get_item.return_value = {
        "Item": {
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "original_pipeline_id": PIPELINE_ID,
            "source_s3_uri": f"s3://{SOURCE_BUCKET}/uploads/data.csv",
        }
    }
    mock_table.put_item.return_value = {}

    reader = MagicMock()
    csv_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
    with (
        patch.object(lf, "get_pipeline_metadata", reader),
        patch.object(lf, "extract_schema_from_csv", return_value=csv_schema),
    ):
        lf.lambda_handler({"run_id": RUN_ID, "execution_arn": EXECUTION_ARN, "source_format": "csv"}, None)

    reader.assert_not_called()
