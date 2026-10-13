# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Req 16.1's own claim: a MULTI-sheet workbook yields a schema without error.

Subject: ``lambda/agentic_data_ingestion_schema_extract/lambda_function.py`` —
the ``elif source_format == "excel"`` dispatch branch (``:2644``), the
``refuse_ambiguous_multi_sheet=True`` call into ``_read_excel_table`` (``:1490``)
that only that branch makes, and ``extract_schema_from_excel`` (``:1650``).
p55 task 7.2.

**Validates: Requirements 9.2**

What R9.2 means now that task 5.1 has landed, and why
----------------------------------------------------
R9.2 restates ``.kiro/specs/e2e-aws-test-coverage/requirements.md:225``
(Req 16.1): *"WHEN a multi-sheet Excel file is provided, THE file-validation /
schema-extract Lambda SHALL extract a schema without error."* Read literally and
unconditionally, that is **false in this tree**, and deliberately so. Task 5.1
landed R8.4's residual refusal: a multi-sheet workbook arriving on a pipeline
whose record carries no ``selected_worksheet`` **fails the run**, names every
Worksheet present, and says where the selection is set. Task 4.6's routing tests
had to be amended to seed ``selected_worksheet`` for exactly this reason.

So the two requirements only disagree under a reading Req 16.1 never had. Its
"without error" was written when ``.xlsx`` reached ``FORMAT_MAP`` at all —
``detect_format("data.xlsx")`` returned ``"unknown"`` — and the e2e test
discharging it (``tests/e2e/formats/test_format_checks.py``) asserts exactly
that: ``assert not schema_result.get("_lambda_error")``, having passed
``source_format: "unknown"``. It was a claim that the Lambda does not crash on
Excel input, not a claim about resolving an ambiguous workbook.

**The reading taken here, and asserted below, is that Req 16.1 / R9.2 holds for a
CONFIGURED multi-sheet workbook, and that the unconfigured case is a deliberate
refusal rather than a failure to support Excel.** That is not a convenient
narrowing: a refusal that names every sheet and the attribute to set is a
*determinate, actionable* outcome, which is what "support" means for a format
where the file itself does not say which table is the data. The alternative — the
behaviour before 5.1 — was to read ``workbook.active`` and report success, which
satisfies a literal "without error" while ingesting one sheet's data on a
pipeline whose owner may believe another is in play. R8.3 exists to prevent
precisely that, so a test that pinned the literal reading would be pinning the
defect.

:func:`test_the_same_workbook_with_no_selection_refuses_over_configuration_not_over_the_format`
is what keeps that reading honest rather than self-serving. Without it, "we
assert the configured case" is indistinguishable from "we assert the case that
happens to pass".

Why the handler is the seam and not ``extract_schema_from_excel``
----------------------------------------------------------------
The R8.4 gate is not in the extractor. ``_read_excel_table`` refuses only when
its caller passes ``refuse_ambiguous_multi_sheet=True``, and
``lambda_handler``'s ``excel`` branch is the only caller that does — because it
is the only one that has consulted the pipeline record, so the only one for which
``selected_worksheet=None`` means *no human has chosen* rather than *this caller
did not say* (``_read_excel_table``'s own docstring, ``:1526-1549``). Asserting
"produces a schema without raising" against ``extract_schema_from_excel``
directly would therefore assert it on a path where **nothing can refuse**, which
proves nothing about the requirement. Every test here drives
``lambda_handler``.

What this adds that 4.3, 4.6 and 5.1 do not
-------------------------------------------
Req 16.1's claim is currently a *side effect* of a test about something else.
Task 4.6's ``test_an_xlsx_upload_reaches_the_excel_branch_and_records_its_canonical_format``
does compare the extracted schema against
``EXPECTED_EXCEL_JSON_SCHEMA`` over this same multi-sheet fixture — but its claim
is R6.3, **routing**, and an edit that narrowed it to the dispatch (which is what
it is about) would silently take Req 16.1's coverage with it. R9.2 gets its own
home here so it cannot be lost that way.

More concretely: **nothing in the tree today asserts that the fixture has more
than one Worksheet**, which is the antecedent Req 16.1 turns on. 4.6 asserts the
second sheet's columns are *absent* from the extracted schema — an assertion that
passes vacuously on a single-sheet workbook, so it cannot carry the precondition.
:func:`test_the_committed_fixture_carries_more_than_one_worksheet` reads it out
of the committed bytes.

Deliberately not duplicated: 4.3's ``test_excel_field_count_prop.py`` (Property 6
— the counting claim over generated workbooks, including the active-sheet read on
multi-sheet ones); 4.6's dispatch, state row and not-a-zip refusal; 5.1's sixteen
worksheet-selection tests, which own R8.1/R8.2/R8.4/R8.5 over **synthetic**
workbooks. The refusal is touched here once, over the **committed** fixture, and
only to establish R9.2's scope boundary — not to re-pin R8.4, whose message
contents are 5.1's.

**No new property test.** Property 6 already covers the counting claim over
generated workbooks, and the spec names no property for task 7.2; adding an
unnumbered one would be inventing a property the design did not ratify.

Fixture provenance
------------------
``customer_data.xlsx``, committed under ``tests/fixtures/data/`` and generated by
``generate_binary_fixtures.py``. Task 4.6 made it multi-sheet **for this task** —
its own docstring says the second Worksheet exists partly because "p55 task 7.2
… is told to use *this* fixture rather than write a second one". No new fixture
is needed and none was generated: a plain ``generate_binary_fixtures.py`` run
rewrites all four outputs, and ``fastavro`` stamps a random sync marker into
every OCF whose byte offsets another suite reads. Verified instead with
``--verify-only``: 6086 bytes, ``PK\\x03\\x04``, active sheet ``Customers`` with
``INTENDED_SCHEMA``'s six columns, second sheet ``Notes`` with three different
ones.

Ledger status
-------------
``why`` on ``.kiro/specs/e2e-aws-test-coverage/requirements.md:225`` and on
``tests/e2e/formats/test_format_checks.py`` returns only broad snapshot commits
with ``records: []`` (``5feb9b7647``, ``2b45b72912``, ``7ad6ca1ad9``), so neither
Req 16.1 nor its e2e test is governed by any ledger record — the reading argued
above is this task's, not a record's, and is written down here for that reason.
``decisions(scope="…/tests/unit/lambdas/schema_extract")`` returns
``LGR-5c539a6-1/-2/-3`` and ``LGR-0739ab8-1``; two bear on what is asserted:

* ``LGR-5c539a6-2`` (**standing invariant**) — every key an ASL path could read
  off ``$.schema_extract_result`` exists on both arms. ``processed_worksheet`` is
  read off the envelope below on that basis, unconditionally.
* ``LGR-60c4985-7`` (**template / ratified**) — the ``if``/``elif`` chain, its
  terminal ``else`` (``:2700``) and the ``jdbc`` early guard are untouched. This
  file changes no production code at all.

``LGR-28b4d75-5`` (**template / ratified**) is why the expected schema is the
generator's hand-written declaration and never a second parse of the workbook:
"a test written from the producer's output cannot detect the producer drifting,
because the output is its own oracle."

This file runs no e2e test and creates no Glue interactive session. It reads
committed bytes and a mocked S3.
"""

import importlib.util
import os
import pathlib
import sys
from unittest.mock import MagicMock, patch

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parents[3]
PROJECT_ROOT = TESTS_ROOT.parent
FIXTURE_DIR = TESTS_ROOT / "fixtures" / "data"

sys.path.insert(0, str(PROJECT_ROOT / "shared"))
sys.path.insert(0, str(PROJECT_ROOT / "lambda" / "agentic_data_ingestion_schema_extract"))

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test_state_table")

#: Named the way the deployed source bucket is named, so
#: ``convert_source_to_csv``'s ``adi-source-files-`` → ``adi-processed-files-``
#: derivation resolves to a *different* bucket. With an arbitrary name it
#: resolves to itself and the converter fail-closes — task 4.3 owns that
#: refusal, and it is not what any test here is about.
SOURCE_BUCKET = "adi-source-files-123456789012-us-west-2"

RUN_ID = "run-2026-09-01-12-00-00-cafe1234"
PIPELINE_ID = "pipe-multi-sheet-1"
EXECUTION_ARN = "arn:aws:states:us-west-2:123456789012:execution:adi:multisheet"


def _load_fixture_declaration():
    """Load ``generate_binary_fixtures`` by path, per its module docstring.

    ``tests/fixtures/data/`` is not a package. The ``sys.modules`` registration
    before ``exec_module`` is the recipe that module documents and asks callers
    to keep — unnecessary as it stands, required the moment anyone adds
    ``from __future__ import annotations`` there.
    """
    path = FIXTURE_DIR / "generate_binary_fixtures.py"
    spec = importlib.util.spec_from_file_location("generate_binary_fixtures", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _committed_workbook_bytes(name):
    """The committed fixture's bytes, with a regeneration recipe on absence.

    The recipe names ``--out-dir`` deliberately: a plain run rewrites the Avro
    and ORC fixtures too, and ``fastavro``'s random sync marker means those
    rewrites are not no-ops.
    """
    path = FIXTURE_DIR / name
    assert path.is_file(), (
        f"{path} is missing. Regenerate it with:\n"
        f"    .venv/bin/python tests/fixtures/data/generate_binary_fixtures.py --out-dir /tmp/fx\n"
        f"and copy the .xlsx file across (a plain run also rewrites the Avro and ORC fixtures)."
    )
    return path.read_bytes()


@pytest.fixture
def schema_extract():
    """The real ``schema_extract`` module with AWS mocked.

    Same shape as ``test_excel_routing_and_refusal.py``'s and
    ``test_worksheet_selection.py``'s: boto3 and the ``shared`` modules needing a
    non-Mock return are patched, then ``s3_client`` and ``table`` are injected so
    the workbook download and the state reads/writes can be driven.

    ``openpyxl`` is deliberately NOT patched. R9.2 is a claim about a real
    workbook being read; against a stubbed reader "produces a schema without
    raising" would be a property of the stub.

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


def _supply_workbook(mock_table, mock_s3, key, payload):
    """A ``file_validation`` state row naming *key*, and the bytes S3 serves.

    This pair is what "supplied to the suite" reduces to at this seam: an object
    in the source bucket and a prior step that recorded it.
    """
    mock_table.get_item.return_value = {
        "Item": {
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "original_pipeline_id": PIPELINE_ID,
            "source_s3_uri": f"s3://{SOURCE_BUCKET}/{key}",
        }
    }
    mock_table.put_item.return_value = {}
    body = MagicMock()
    body.read.return_value = payload
    mock_s3.get_object.return_value = {"Body": body}


def _event():
    """The Excel dispatch event, keyed on the Canonical_Format for ``xlsx``.

    Written as ``"excel"`` here rather than derived from ``FORMAT_MAP``, because
    that derivation is R6.3's claim and task 4.6's file owns it — see its
    ``_canonical_format_for_xlsx``. Restating it would put a second, weaker copy
    of that check in the tree.
    """
    return {"run_id": RUN_ID, "execution_arn": EXECUTION_ARN, "source_format": "excel"}


def _pipeline_selecting(sheet_name):
    """A pipelines-table record carrying *sheet_name* as the Selected_Worksheet."""
    return {"pipeline_id": PIPELINE_ID, "selected_worksheet": sheet_name}


# ── The antecedent: the workbook really has more than one Worksheet ─────


def test_the_committed_fixture_carries_more_than_one_worksheet():
    """Req 16.1's antecedent, read out of the committed bytes.

    Everything below is a claim about a *multi-sheet* workbook, and nothing else
    in the tree checks that this fixture is one. Task 4.6's routing test asserts
    the second sheet's columns are **absent** from the extracted schema, which is
    satisfied vacuously by a single-sheet workbook — so if a regeneration
    dropped the second sheet, every Excel test in this directory would still
    pass and Req 16.1's coverage would quietly become a single-sheet claim.

    Read with ``openpyxl`` against the bytes rather than trusted from the
    generator's constants, because the constants are a declaration of intent and
    this test is about the file. The declared names are then compared, so a
    fixture and a declaration that have drifted apart fail here rather than
    somewhere confusing.
    """
    import openpyxl

    gen = _load_fixture_declaration()
    workbook = openpyxl.load_workbook(
        FIXTURE_DIR / gen.EXCEL_FIXTURE,
        read_only=True,
        data_only=True,
    )
    try:
        sheet_names = list(workbook.sheetnames)
    finally:
        workbook.close()

    assert len(sheet_names) > 1, (
        f"{gen.EXCEL_FIXTURE} has {sheet_names}, which is not a multi-sheet workbook — "
        f"Req 16.1 / R9.2 has no antecedent to test"
    )
    assert gen.EXCEL_ACTIVE_SHEET in sheet_names
    assert gen.EXCEL_SECOND_SHEET in sheet_names


# ── R9.2: the configured multi-sheet workbook yields a schema ──────────


def test_a_multi_sheet_workbook_supplied_to_a_run_produces_a_schema_without_raising(schema_extract, monkeypatch):
    """R9.2 / Req 16.1: no raise, and a schema that describes something.

    "Without error" is asserted as two separate facts, because either alone is
    satisfiable while the requirement is not. Nothing raising is the obvious
    half. The other is that the returned ``properties`` is **non-empty**: p46's
    Empty_Schema defect returned ``{"type": "object", "properties": {}}`` with
    ``statusCode`` 200 from a binary file, which raises nothing, reports success,
    and hands ``schema_mapping`` nothing to map. A test that only caught
    exceptions would pass on it.

    The field set is then compared against the generator's declared oracle, so
    "describes something" cannot be satisfied by describing the wrong sheet.

    Driven through ``lambda_handler`` because that is the only caller passing
    ``refuse_ambiguous_multi_sheet=True`` — the seam where a multi-sheet workbook
    *can* refuse. Asserting this against ``extract_schema_from_excel`` would
    assert it where refusal is impossible.
    """
    lf, mock_s3, mock_table = schema_extract
    gen = _load_fixture_declaration()
    key = f"uploads/{gen.EXCEL_FIXTURE}"

    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _supply_workbook(mock_table, mock_s3, key, _committed_workbook_bytes(gen.EXCEL_FIXTURE))

    with patch.object(lf, "get_pipeline_metadata", return_value=_pipeline_selecting(gen.EXCEL_ACTIVE_SHEET)):
        # No pytest.raises and no try/except: an exception out of here IS the
        # failure R9.2 forbids, and letting it propagate reports it with its own
        # message rather than behind an assertion of ours.
        result = lf.lambda_handler(_event(), None)

    assert result["statusCode"] == 200
    assert result["extracted_schema"]["properties"], (
        "a schema with no properties is p46's Empty_Schema defect: success reported with zero fields"
    )
    assert result["extracted_schema"] == gen.EXPECTED_EXCEL_JSON_SCHEMA
    assert result["processed_worksheet"] == gen.EXCEL_ACTIVE_SHEET


def test_either_worksheet_of_the_multi_sheet_workbook_yields_its_own_schema(schema_extract, monkeypatch):
    """R9.2 does not hold only for whichever sheet happens to be active.

    ``EXCEL_ACTIVE_SHEET`` is the sheet ``workbook.active`` resolves to, so a
    reader that ignored the configured selection entirely would still satisfy the
    test above. Selecting the *other* sheet is what distinguishes "the multi-sheet
    workbook yields a schema" from "the active sheet yields a schema and the
    second one is along for the ride".

    ``Notes``' three columns are unlike the active sheet's six deliberately (task
    4.6's fixture note), so the wrong sheet is visible as a wrong field set
    rather than as a coincidence. Every field is ``string`` for the same reason
    ``EXPECTED_EXCEL_JSON_SCHEMA`` is: the frame the generated Glue job receives
    is the converted CSV read with ``inferSchema=False``.
    """
    lf, mock_s3, mock_table = schema_extract
    gen = _load_fixture_declaration()
    key = f"uploads/{gen.EXCEL_FIXTURE}"

    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _supply_workbook(mock_table, mock_s3, key, _committed_workbook_bytes(gen.EXCEL_FIXTURE))

    with patch.object(lf, "get_pipeline_metadata", return_value=_pipeline_selecting(gen.EXCEL_SECOND_SHEET)):
        result = lf.lambda_handler(_event(), None)

    assert result["statusCode"] == 200
    assert result["extracted_schema"] == {
        "type": "object",
        "properties": {name: {"type": "string"} for name in gen.EXCEL_SECOND_SHEET_COLUMNS},
    }
    assert result["processed_worksheet"] == gen.EXCEL_SECOND_SHEET


# ── R9.2's scope boundary: the refusal is about configuration ───────────


def test_the_same_workbook_with_no_selection_refuses_over_configuration_not_over_the_format(
    schema_extract, monkeypatch
):
    """The unconfigured case, so the reading above is checkable rather than chosen.

    This is the assertion that makes this file honest. R9.2's literal wording
    would have the *same bytes* yield a schema with no selection too, and they do
    not — R8.4 refuses. What is asserted here is that the refusal is a
    **configuration** gate: it names the attribute to set and every Worksheet
    present, so the remedy is available from the message alone. If instead it
    reported the format as unreadable or unsupported, reading R9.2 as scoped to a
    configured workbook would be a way of not admitting Excel is unsupported —
    and this test would fail.

    Over the **committed** fixture, unlike 5.1's synthetic workbooks. R8.4's
    message contents are ``test_worksheet_selection.py``'s to own; what is new
    here is that the refusal and the success are the same file, one configuration
    apart.
    """
    lf, mock_s3, mock_table = schema_extract
    gen = _load_fixture_declaration()
    key = f"uploads/{gen.EXCEL_FIXTURE}"

    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)
    _supply_workbook(mock_table, mock_s3, key, _committed_workbook_bytes(gen.EXCEL_FIXTURE))

    with (
        patch.object(lf, "get_pipeline_metadata", return_value={"pipeline_id": PIPELINE_ID}),
        pytest.raises(lf.StepFailure) as exc_info,
    ):
        lf.lambda_handler(_event(), None)

    message = exc_info.value.message

    # The remedy is in the message: the attribute to set, and the values to
    # choose between.
    assert lf.SELECTED_WORKSHEET_ATTRIBUTE in message, message
    assert gen.EXCEL_ACTIVE_SHEET in message, message
    assert gen.EXCEL_SECOND_SHEET in message, message

    # And it is NOT a claim that the workbook or the format could not be read —
    # which is what would make the scoped reading of R9.2 dishonest. The two
    # phrases are the ones `_read_excel_table`'s parse-failure and
    # missing-openpyxl arms use.
    assert "could not be opened as an Excel_Workbook" not in message, message
    assert "openpyxl is not available" not in message, message
