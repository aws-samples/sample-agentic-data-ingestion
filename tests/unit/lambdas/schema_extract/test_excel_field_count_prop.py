# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
# Feature: p55-format-support-and-teardown, Property 6: Excel field count matches the selected sheet

*For any* Excel workbook with >= 1 sheet, the extracted field count equals the
selected sheet's column count.

**Validates: Requirements 6.2**

Why this is a property and not a handful of examples
---------------------------------------------------
R6.2 is a *counting* claim — "one field per column of the processed Worksheet,
named from that column's header cell" — and a count is exactly the kind of claim
that passes on tidy input and fails on real spreadsheets. The two ways a real
workbook makes the count wrong are both silent:

* a **blank** header cell, which a comprehension over ``{cell: …}`` drops; and
* a **duplicate** header, which two columns collapse into one dict key for.

Either shrinks ``properties`` below the column count, and the run then reports
success describing fewer columns than it is about to load. That is the same class
of quiet wrongness as p46's Empty_Schema defect, one step less obvious: a partial
schema looks healthy where an empty one at least looks broken. ``_excel_field_names``
(``schema_extract/lambda_function.py:1352``) exists to make both cases keep their
column — ``column_{n}`` for a blank, a ``_2``/``_3`` suffix for a duplicate — and
this test is what holds it to that over the space rather than over two examples.

The oracle is independent of the code under test
------------------------------------------------
The generator decides how many columns a sheet has and writes exactly that many
header cells; the assertion compares ``len(schema["properties"])`` against the
generator's own number. Nothing reads the width back out of ``openpyxl`` and
compares it to a schema derived from the same read, which would be the code
agreeing with itself. Workbooks are real ``.xlsx`` bytes written by ``openpyxl``
and handed to the extractor through the same ``s3_client.get_object`` seam
production uses, so the zip round trip is exercised rather than mocked away.

The two selection paths, and why both are driven here
-----------------------------------------------------
``_read_excel_table`` reaches a Worksheet two ways: an explicit
``selected_worksheet`` name, and ``workbook.active`` when none is given. Both are
covered, because "the *selected* sheet's column count" is a claim about the
sheet that was actually processed — a bug that read sheet 1's header while
reporting sheet 3's name would satisfy a single-sheet test and violate R6.2 on
every multi-sheet workbook. Worksheet *selection* itself (R8.1–R8.5: the
Selected_Worksheet as pipeline config and the two refusals) is task 5.1's, not
this test's; ``selected_worksheet`` is driven here only as the seam that decides
which sheet is counted.

What is deliberately NOT asserted here
--------------------------------------
* **The field names.** ``column_{n}`` and the ``_2`` suffix are naming decisions
  and belong in example tests, where a changed answer names the shape that
  changed. Asserting them here would make any future naming amendment fail with
  a generated workbook, which tells a reader nothing about what they broke.
* **The routing** (an ``.xlsx`` upload reaching the ``excel`` branch and
  recording ``source_format``, R6.3) and **the malformed-input refusal** (an
  ``.xlsx`` that is not a zip, R6.5). Both are **task 4.6**'s, as example tests.
  The raise paths are touched here only where a generated workbook would
  otherwise make the count claim vacuous — see ``test_an_empty_sheet_raises``.
* **The conversion** (``convert_source_to_csv``, R13.3) and the envelope pair
  ``readable_source_uri`` / ``readable_source_format`` (R13.4). Different
  requirements, different claims.

Ledger status
-------------
``why`` on ``schema_extract/lambda_function.py`` returns ``LGR-5c539a6-1/-2/-3``
and ``LGR-0739ab8-1``; none governs a test's shape. ``LGR-5c539a6-2``
(**standing invariant**) governs the envelope pair this branch also produces and
is asserted elsewhere. ``LGR-0739ab8-1`` (**template / ratified**) owns
``schema_extract``'s Layer_Budget and its exact layer list, which is why
``openpyxl`` went into ``shared/runtime_requirements.txt`` rather than into a
fourth layer — this test importing the real ``openpyxl`` is the same dependency
that decision put on the layer. ``LGR-1e135d2-11`` (**template / ratified**)
governs what happens if this test finds something: ``.hypothesis/examples`` is
not committed, so a counterexample does not survive a fresh clone and must be
pinned as an example test rather than left to the database.
"""

import io
import os
import sys
from unittest.mock import MagicMock, patch

import openpyxl
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Ensure shared modules and the Lambda's own directory are importable.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_extract"),
)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("STATE_TABLE", "test_state_table")


@pytest.fixture
def schema_extract():
    """The real ``schema_extract`` module with AWS mocked, mirroring the sibling suites.

    Same shape as ``test_streaming_schema.py``'s ``_mock_aws``: boto3 and the
    ``shared`` modules that need a non-Mock return are patched, then
    ``s3_client`` is injected so ``_read_excel_table``'s ``get_object`` can be
    driven. ``openpyxl`` is deliberately NOT in the patched set — the whole point
    is that real ``.xlsx`` bytes go through the real reader.
    """
    mock_s3 = MagicMock()
    mock_dynamodb = MagicMock()
    mock_dynamodb.Table.return_value = MagicMock()

    mock_eb = MagicMock()
    mock_eb.get_eventbridge_publishers.return_value = (MagicMock(), MagicMock(), MagicMock())
    mock_env = MagicMock()
    mock_env.require_env.return_value = "test_state_table"
    mock_artifact = MagicMock()
    mock_artifact.create_output_data.return_value = {}

    schema_dir = os.path.abspath(
        os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_extract"
        )
    )
    sys.path.insert(0, schema_dir)

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
        lambda_function.table = MagicMock()
        yield lambda_function, mock_s3


# ── generators ─────────────────────────────────────────────────────────
#
# Sheet titles are restricted to what Excel itself accepts: openpyxl RAISES on
# `[]:*?/\`, on an empty title and on more than 31 characters, so a generator
# drawing arbitrary text would fail in the fixture setup rather than in the
# assertion — a red test that says nothing about the property.
_SHEET_TITLE = st.text(
    alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters=" _-"),
    min_size=1,
    max_size=18,
).map(lambda s: s.strip() or "Sheet")

#: A header cell that is present but carries no name. Both spellings a real
#: workbook produces: a genuinely empty cell, and one holding whitespace.
_BLANK_HEADER = st.sampled_from([None, "", "   ", "\t"])

#: A header cell carrying a name. Duplicates across a header row are drawn from
#: a deliberately small pool so collisions happen often rather than by luck —
#: the `_2`/`_3` suffix path is one of the two ways the count can shrink.
_NAMED_HEADER = st.one_of(
    st.sampled_from(["id", "name", "email", "amount", "date", "id ", " Name"]),
    st.text(
        alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd"), whitelist_characters=" _-."),
        min_size=1,
        max_size=12,
    ),
)


@st.composite
def _header_row(draw, min_columns=1, max_columns=8):
    """A header row of known width, with the last cell guaranteed non-blank.

    The last cell is pinned non-blank for a mechanical reason, not to dodge a
    case: ``openpyxl`` writes no cell at all for a trailing ``None``, so a row
    ending in a blank has a *smaller* ``max_column`` than the list handed to
    ``append``. The generator's width would then stop being the sheet's width
    and the oracle would be measuring the writer rather than the extractor.
    Interior blanks — which do keep their column — are generated freely, and
    they are the case R6.2's ``column_{n}`` naming exists for.
    """
    width = draw(st.integers(min_value=min_columns, max_value=max_columns))
    cells = draw(st.lists(st.one_of(_BLANK_HEADER, _NAMED_HEADER), min_size=width, max_size=width))
    cells[-1] = draw(_NAMED_HEADER)
    return cells


@st.composite
def _workbook_spec(draw, max_sheets=4):
    """``{title: (header_cells, data_rows)}`` for a workbook with >= 1 sheet."""
    titles = draw(
        st.lists(_SHEET_TITLE, min_size=1, max_size=max_sheets, unique_by=lambda t: t.lower()),
    )
    spec = {}
    for title in titles:
        header = draw(_header_row())
        row_count = draw(st.integers(min_value=0, max_value=3))
        rows = [[f"r{r}c{c}" for c in range(len(header))] for r in range(row_count)]
        spec[title] = (header, rows)
    return spec


def _xlsx_bytes(spec):
    """Write ``spec`` as real ``.xlsx`` bytes; the first sheet is the active one."""
    workbook = openpyxl.Workbook()
    first = True
    for title, (header, rows) in spec.items():
        worksheet = workbook.active if first else workbook.create_sheet()
        worksheet.title = title
        first = False
        worksheet.append(header)
        for row in rows:
            worksheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    return buffer.getvalue()


def _serve(mock_s3, payload):
    body = MagicMock()
    body.read.return_value = payload
    mock_s3.get_object.return_value = {"Body": body}


# ── Property 6 ─────────────────────────────────────────────────────────


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=_workbook_spec(), pick=st.integers(min_value=0, max_value=3))
def test_the_field_count_equals_the_selected_sheets_column_count(schema_extract, spec, pick):
    """Feature: p55-format-support-and-teardown, Property 6: Excel field count
    matches the selected sheet.

    *For any* Excel workbook with >= 1 sheet, the extracted field count equals
    the selected sheet's column count — with the sheet named explicitly.

    **Validates: Requirements 6.2**
    """
    lambda_function, mock_s3 = schema_extract
    _serve(mock_s3, _xlsx_bytes(spec))

    titles = list(spec)
    selected = titles[pick % len(titles)]
    expected_columns = len(spec[selected][0])

    schema = lambda_function.extract_schema_from_excel(
        "adi-source-files-test", "uploads/workbook.xlsx", selected_worksheet=selected
    )

    assert schema["type"] == "object"
    assert len(schema["properties"]) == expected_columns, (
        f"sheet {selected!r} has {expected_columns} columns but the schema declares "
        f"{len(schema['properties'])} fields: {sorted(schema['properties'])}"
    )


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=_workbook_spec())
def test_the_field_count_matches_the_active_sheet_when_none_is_selected(schema_extract, spec):
    """Feature: p55-format-support-and-teardown, Property 6: Excel field count
    matches the selected sheet — the ``workbook.active`` selection path.

    Same claim, other seam. ``_read_excel_table`` falls back to
    ``workbook.active`` when no ``selected_worksheet`` is given, which is every
    single-sheet workbook and every multi-sheet one before task 5.1's config
    field exists. A reader that counted a different sheet's header than the one
    it processed would pass the explicit-name test above and violate R6.2 here.

    **Validates: Requirements 6.2**
    """
    lambda_function, mock_s3 = schema_extract
    _serve(mock_s3, _xlsx_bytes(spec))

    active_title = next(iter(spec))
    expected_columns = len(spec[active_title][0])

    schema = lambda_function.extract_schema_from_excel("adi-source-files-test", "uploads/workbook.xlsx")

    assert len(schema["properties"]) == expected_columns, (
        f"active sheet {active_title!r} has {expected_columns} columns but the schema declares "
        f"{len(schema['properties'])} fields: {sorted(schema['properties'])}"
    )


@settings(max_examples=100, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
@given(spec=_workbook_spec())
def test_no_column_is_lost_to_a_blank_or_duplicate_header(schema_extract, spec):
    """The two shrinking mechanisms, asserted as the absence of collapse.

    The count assertions above would already fail if a blank or a duplicate lost
    its column, but they would fail on an arbitrary generated workbook and leave
    a reader to work out *which* mechanism shrank. This one states the two by
    name: every field name is distinct (no duplicate collapse) and no name is
    empty (no blank dropped or named ``""``).

    **Validates: Requirements 6.2**
    """
    lambda_function, mock_s3 = schema_extract
    _serve(mock_s3, _xlsx_bytes(spec))

    active_title = next(iter(spec))
    header = spec[active_title][0]

    schema = lambda_function.extract_schema_from_excel("adi-source-files-test", "uploads/workbook.xlsx")
    names = list(schema["properties"])

    assert len(set(names)) == len(names), f"two columns collapsed into one field: {names}"
    assert all(name.strip() for name in names), f"a column was named blank: {names}"
    assert len(names) == len(header)


def test_an_empty_sheet_raises(schema_extract):
    """The boundary that keeps Property 6 non-vacuous, not a duplicate of 4.6's.

    The generator never produces a sheet with no header row, because a workbook
    with nothing to count cannot exercise a counting property. That case is a
    **raise** rather than a zero-field schema (R6.5), and it is asserted here so
    the generator's floor of one column reads as a deliberate exclusion rather
    than an untested gap. Task 4.6 owns the malformed-*input* refusal — an
    ``.xlsx`` that is not a zip at all — which is a different failure.
    """
    lambda_function, mock_s3 = schema_extract

    workbook = openpyxl.Workbook()
    workbook.active.title = "Empty"
    buffer = io.BytesIO()
    workbook.save(buffer)
    workbook.close()
    _serve(mock_s3, buffer.getvalue())

    with pytest.raises(ValueError, match=r"no header row|is empty"):
        lambda_function.extract_schema_from_excel("adi-source-files-test", "uploads/empty.xlsx")


# ── the converter's destination guard ──────────────────────────────────
#
# Two example tests, in this file rather than a new one for the same reason
# ``test_an_empty_sheet_raises`` is here: they guard the units task 4.3 added,
# and task 4.6 owns the *reader's* malformed-input refusal (an ``.xlsx`` that is
# not a zip), which is a different failure with a different remedy.


def _one_sheet_workbook_bytes():
    return _xlsx_bytes({"Sheet1": (["id", "name"], [["1", "a"], ["2", "b"]])})


def test_the_converter_writes_to_the_processed_bucket(schema_extract, monkeypatch):
    """R13.3: the converted object goes to the processed bucket, never the source one.

    The source bucket is watched by the S3-upload EventBridge rule, so a
    converted object written there re-triggers the pipeline on its own output.
    Asserted on the ``put_object`` call rather than on the returned URI, because
    the URI is derived from the same two locals and would agree with a wrong
    destination.
    """
    lambda_function, mock_s3 = schema_extract
    _serve(mock_s3, _one_sheet_workbook_bytes())
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

    uri = lambda_function.convert_source_to_csv(
        "adi-source-files-123456789012-us-west-2",
        "uploads/book.xlsx",
        run_id="run-1",
        pipeline_id="pipe-1",
    )

    written = mock_s3.put_object.call_args.kwargs
    assert written["Bucket"] == "adi-processed-files-123456789012-us-west-2"
    assert written["Key"] == "converted/pipe-1/run-1/source.csv"
    assert uri == "s3://adi-processed-files-123456789012-us-west-2/converted/pipe-1/run-1/source.csv"
    # The header the CSV carries is the header the schema declares, so the Glue
    # read's `.option("header", True)` does not rename every column (R6.4).
    assert written["Body"].decode("utf-8").splitlines()[0] == "id,name"


def test_the_converter_refuses_to_write_into_the_source_bucket(schema_extract, monkeypatch):
    """A destination equal to the source is a refusal, not a write.

    ``str.replace`` returns its input unchanged when the pattern does not match,
    so a source bucket without the ``adi-source-files-`` prefix resolves to
    itself. With ``PROCESSED_BUCKET`` unset — which it is on this function today
    (``compute-stack.ts:313-320``) — that is reachable by configuration rather
    than by code change, and the consequence is a re-trigger loop that looks
    like a duplicate upload. Fail-closed, and **nothing is written**: a
    conversion that could not be written safely must not be reported as one,
    because ``readable_source_uri`` would then name an object the Glue job
    cannot read.
    """
    lambda_function, mock_s3 = schema_extract
    _serve(mock_s3, _one_sheet_workbook_bytes())
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

    with pytest.raises(ValueError, match=r"source bucket"):
        lambda_function.convert_source_to_csv(
            "some-other-bucket", "uploads/book.xlsx", run_id="run-1", pipeline_id="pipe-1"
        )

    mock_s3.put_object.assert_not_called()
