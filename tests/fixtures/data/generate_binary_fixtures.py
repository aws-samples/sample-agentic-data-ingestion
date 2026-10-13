#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Generate the Avro, ORC and Excel schema-extraction fixtures.

``tests/fixtures/data/`` held only CSV and JSON before this script, so there was
nothing for ``extract_schema_from_avro`` / ``extract_schema_from_orc`` to be
tested against (p46 design §5 R4). This script produces the two binaries; p46 task
2.2 runs it and commits the outputs. **p55 task 4.6 extends it** with the two
Excel fixtures — see "The Excel fixtures" below.

Run it from the repository's ``agentic_data_ingestion/`` directory::

    .venv/bin/python tests/fixtures/data/generate_binary_fixtures.py

    # write somewhere else, e.g. to diff against the committed copies
    .venv/bin/python tests/fixtures/data/generate_binary_fixtures.py --out-dir /tmp/fx

A plain run rewrites **all four** outputs, and two of them churn even when their
declared content has not changed: ``fastavro`` stamps a random 16-byte sync
marker into every OCF, and both zip and ORC containers carry writer metadata. So
when only one fixture is wanted, write to ``--out-dir`` and copy that file across
— p55 task 4.6 added the two Excel fixtures that way rather than re-emitting
p46's committed binaries, whose byte offsets one test suite reads.

    # re-read the committed fixtures and check them against INTENDED_SCHEMA
    .venv/bin/python tests/fixtures/data/generate_binary_fixtures.py --verify-only

Outputs, all beside this file:

- ``customer_data.avro`` — Avro Object Container File, written by ``fastavro``
- ``customer_data.orc`` — ORC, written by ``pyarrow.orc.write_table``
- ``customer_data.xlsx`` — a two-Worksheet Excel_Workbook, written by ``openpyxl``
- ``not_a_workbook.xlsx`` — an ``.xlsx`` that is deliberately **not a zip**

Why a script rather than committed binaries with no provenance: a reader has to
be able to see what the fixtures contain. Avro and ORC keep their schema in
container metadata, and an ``.xlsx`` is a zip, so ``git diff`` shows nothing
useful for any of the three and ``file`` shows nothing at all.

The columns
-----------

The first five are ``customer_data.csv``'s, same names and same order, so the
CSV, Avro and ORC schemas for one logical dataset are comparable. Every cell in
those five columns is the CSV's cell. ``account_balance`` is added because the
CSV has no floating-point column and no nulls, and Requirement 8's type mapping
should be exercised by the fixture rather than only by the property test.

=================  ==============================  =================  =================  ============
column             Avro type                       Arrow written      Arrow read back    JSON Schema
=================  ==============================  =================  =================  ============
id                 ``long``                        ``int64``          ``int64``          ``integer``
name               ``string``                      ``string``         ``string``         ``string``
email              ``string``                      ``string``         ``string``         ``string``
phone              ``string``                      ``string``         ``string``         ``string``
created_at         ``long`` / ``timestamp-millis``  ``timestamp[ms]``  ``timestamp[ns]``  ``string``
account_balance    ``["null", "double"]``          ``float64``        ``double``         ``number``
=================  ==============================  =================  =================  ============

That covers one integer, one float, one string (three, in fact), one timestamp
and one nullable column, which is what task 2.1 asks the fixture to carry.

The JSON Schema column is not a prediction. It was checked by calling the real
``arrow_schema_to_json_schema`` (``schema_extract/lambda_function.py:327-392``)
on the written file's footer schema, which returns
:data:`EXPECTED_JSON_SCHEMA` exactly. ``--verify-only`` does not repeat that —
it has no reason to import a Lambda module — so the check belongs to task 5.2.

Two things do not survive the ORC round trip, observed rather than assumed:

- **Precision.** ``timestamp[ms]`` is written and ``timestamp[ns]`` is read
  back; ORC stores nanoseconds. Immaterial to the schema, because
  ``pa.types.is_timestamp`` catches both and maps them to ``string``
  (``lambda_function.py:377-378``).
- **Nullability.** Every ORC column reads back ``nullable=True``, including the
  four declared ``nullable=False`` — ORC has no NOT NULL constraint. So the
  ``nullable`` flags on :data:`INTENDED_SCHEMA` are write-side intent, and a
  test must not assert them from the ORC footer. The nullable *declaration* is
  observable only in Avro, where the ``["null", "double"]`` union is preserved
  verbatim in the OCF header.

The Excel fixtures (p55 task 4.6)
---------------------------------

``customer_data.xlsx`` carries :data:`INTENDED_SCHEMA`'s six columns and
:data:`ROWS`' ten rows on its **active** Worksheet, :data:`EXCEL_ACTIVE_SHEET`,
so the Excel schema is comparable with the Avro, ORC and CSV ones for the same
logical dataset. A **second** Worksheet, :data:`EXCEL_SECOND_SHEET`, holds three
different columns. That is not decoration and not scope creep:

* it makes the workbook a multi-sheet one, which is what
  ``.kiro/specs/e2e-aws-test-coverage/requirements.md:225`` (Req 16.1) has been
  asserting support for and what p55 task 7.2 turns into a live test — that task
  is told to use *this* fixture rather than write a second one; and
* its columns differ from the active sheet's, so a reader that counted one
  Worksheet's header while reporting another's is visible as a wrong field set
  rather than as a coincidence.

The active sheet is the one openpyxl creates first, so
``workbook.active`` — ``_read_excel_table``'s no-``selected_worksheet`` path —
resolves to :data:`EXCEL_ACTIVE_SHEET`.

:data:`EXPECTED_EXCEL_JSON_SCHEMA` types **every** field ``string``, unlike
:data:`EXPECTED_JSON_SCHEMA`. That is the extractor's deliberate answer, not a
loss: ``extract_schema_from_excel`` declares ``string`` for every column because
the frame the generated Glue job receives is the converted CSV, read with
``.option("inferSchema", False)``, so a column typed ``integer`` here would
describe the *workbook* rather than the frame the mapping reasons about. Same
divergence as CSV's, one format over, and ratified for the same reason
``LGR-eee8ac8-2`` gives — csv reads every column as ``string`` while parquet
reads the same bytes as ``integer``.

``not_a_workbook.xlsx`` is :data:`MALFORMED_EXCEL_BYTES`: CSV text under an
``.xlsx`` name, which is the realistic form of the mistake (an export renamed
rather than converted) and is definitively not a zip. It exists because R6.5
requires a file carrying the ``xlsx`` extension that cannot be opened as a
workbook to **fail the run naming the file and the reason**, and the failure
that must not happen — returning ``{"type": "object", "properties": {}}`` and
reporting success — is p46's Empty_Schema defect exactly. Committing it rather
than assembling the bytes inside a test keeps the two Excel inputs' provenance
in one place; it is plain text, so ``git diff`` shows it in full.

Reading the JSON Schema column across formats
---------------------------------------------

The five shared columns yield the same *field names* from all three fixtures but
not the same *types*. ``extract_schema_from_csv`` runs S3 Select with a JSON
output serialization and then ``infer_schema_from_records``
(``schema_extract/lambda_function.py:189-234``, ``:146-181``), so every CSV
column arrives as ``string`` — ``id`` included. Avro and ORC carry real types,
so ``id`` is ``integer`` there.

That divergence is expected and ratified, not a defect in this fixture:
``LGR-eee8ac8-2`` (template / ratified) deliberately declines to exempt the
csv→parquet format switch from incompatibility, on the ground that "csv reads
every column as ``string`` while parquet reads the same bytes as ``integer``".
Avro and ORC are the same case one format over (requirements.md C3).

Why INTENDED_SCHEMA is written out by hand
------------------------------------------

``INTENDED_SCHEMA`` below is the *declaration*: it is transcribed from
Requirement 8's mapping table, and both writers are derived from it. It is
deliberately not derived from anything the writers produce.

p46 task 4 hand-parses the Avro OCF header rather than calling ``fastavro``,
because ``fastavro`` did not fit the Lambda layer (see ``layer-measurement.md``:
12.70 MiB needed against 2.14 MiB of headroom). requirements.md rejected
hand-parsing "on correctness, not size" — a varint bug yields a *wrong schema*
rather than a failure. So task 4.2 needs an oracle that is independent of its
own parser, and this constant is it: compare the parser's output field-by-field
against ``EXPECTED_JSON_SCHEMA``, not against a second call to ``fastavro`` and
not against the parser's own previous output. ``LGR-28b4d75-5`` (template /
ratified) is the standing form of that argument — "a test written from the
producer's output cannot detect the producer drifting, because the output is its
own oracle".

For the same reason the Avro fixture is written by ``fastavro`` and not by
hand-assembled OCF bytes. If this script and task 4's parser were both written
against the same reading of the OCF spec, a shared misunderstanding of the
varint framing would pass unnoticed in both directions. A published library on
the writing side makes the fixture an independent check on the parser.

Consuming this module from a test
---------------------------------

``tests/fixtures/data/`` is not a package, so load it by path — and register it
in ``sys.modules`` before executing it, which ``dataclasses`` needs in order to
resolve the annotations on :class:`FixtureColumn`::

    import importlib.util
    import sys
    from pathlib import Path

    path = Path(__file__).parents[3] / "fixtures" / "data" / "generate_binary_fixtures.py"
    spec = importlib.util.spec_from_file_location("generate_binary_fixtures", path)
    gen = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = gen          # see below
    spec.loader.exec_module(gen)

    assert extracted == gen.EXPECTED_JSON_SCHEMA

The ``sys.modules`` line is not needed as this module stands — verified, the load
works without it — but keep it. It is needed the moment anyone adds
``from __future__ import annotations`` here: with string annotations,
``@dataclass`` looks :class:`FixtureColumn`'s module up by ``cls.__module__`` to
resolve them, finds nothing, and raises ``AttributeError: 'NoneType' object has
no attribute '__dict__'`` from inside ``dataclasses``. That future import is
absent deliberately, for this reason.

Importing it costs nothing: ``pyarrow`` and ``fastavro`` are imported inside the
writer functions, so the declaration is readable without either installed.

Dependencies
------------

``pyarrow`` is already a dev dependency. ``fastavro`` is added to the ``dev``
group by p46 task 2.1 and is **dev-only** — it is a fixture writer, not a runtime
dependency, and it is explicitly not in the Lambda layer (p46 R7.2).

``openpyxl`` is the one dependency here that is **also** a runtime one: p55 task
4.3 pinned it in ``shared/runtime_requirements.txt`` (``openpyxl==3.1.5``), so
the same library writes this fixture and reads it in Lambda. That is deliberate
rather than an oversight of the independent-oracle argument below — the oracle
for the Excel fixture is :data:`EXCEL_ACTIVE_SHEET`'s declared column list, which
is hand-written here, and not a second parse of the file.

Versions this script was last run against: ``pyarrow`` 24.0.0, ``fastavro``
1.12.2, ``openpyxl`` 3.1.5. Note that the *deployed* layer resolves ``pyarrow`` 20.0.0, not 24 —
``--platform manylinux2014_aarch64`` caps it there. The ORC file written here is
therefore read back by an older pyarrow in Lambda than the one that wrote it,
which the ORC format supports; ``--verify-only`` is the local check that the
file round-trips at all.
"""

import argparse
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

AVRO_FIXTURE = "customer_data.avro"
ORC_FIXTURE = "customer_data.orc"
EXCEL_FIXTURE = "customer_data.xlsx"
MALFORMED_EXCEL_FIXTURE = "not_a_workbook.xlsx"

#: The Avro record name recorded in the OCF header's ``avro.schema`` metadata.
#: Task 4's hand-parser reads that JSON out of the header, so the name is part
#: of what it will see.
AVRO_RECORD_NAME = "Customer"
AVRO_NAMESPACE = "com.example.agentic_data_ingestion.fixtures"


@dataclass(frozen=True)
class FixtureColumn:
    """One column, declared once and used by both writers.

    ``arrow_type`` is a string key rather than a ``pyarrow.DataType`` so this
    module imports without pyarrow; :func:`_arrow_type` resolves it.

    ``nullable`` is honoured on both write paths but is only *observable* on the
    Avro side, as a ``["null", …]`` union in the OCF header. ORC has no NOT NULL
    constraint, so every ORC column reads back nullable regardless.
    """

    name: str
    avro_type: Any
    arrow_type: str
    json_schema_type: str
    nullable: bool
    role: str


#: Transcribed from Requirement 8's mapping table. The writers are derived from
#: this; nothing here is derived from the writers. See the module docstring.
INTENDED_SCHEMA: tuple[FixtureColumn, ...] = (
    FixtureColumn(
        name="id",
        avro_type="long",
        arrow_type="int64",
        json_schema_type="integer",
        nullable=False,
        role="integer column (R8.1: Avro long -> integer)",
    ),
    FixtureColumn(
        name="name",
        avro_type="string",
        arrow_type="string",
        json_schema_type="string",
        nullable=False,
        role="string column (R8.1)",
    ),
    FixtureColumn(
        name="email",
        avro_type="string",
        arrow_type="string",
        json_schema_type="string",
        nullable=False,
        role="string column (R8.1)",
    ),
    FixtureColumn(
        name="phone",
        avro_type="string",
        arrow_type="string",
        json_schema_type="string",
        nullable=False,
        role="string column (R8.1)",
    ),
    FixtureColumn(
        name="created_at",
        avro_type={"type": "long", "logicalType": "timestamp-millis"},
        arrow_type="timestamp_ms",
        json_schema_type="string",
        nullable=False,
        role="timestamp column (R8.3: timestamp-millis -> string; Arrow timestamp -> string)",
    ),
    FixtureColumn(
        name="account_balance",
        avro_type=["null", "double"],
        arrow_type="float64",
        json_schema_type="number",
        nullable=True,
        role="float + nullable column (R8.1: double -> number; R8.4: union drops null)",
    ),
)

#: What both extractors must return for these fixtures. This is the field-by-field
#: oracle task 4.2 and task 5.2 compare against.
EXPECTED_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {column.name: {"type": column.json_schema_type} for column in INTENDED_SCHEMA},
}

#: The Worksheet ``customer_data.xlsx`` opens on, and the one ``workbook.active``
#: resolves to — openpyxl leaves the first sheet created as the active one, and
#: :func:`write_xlsx` creates this one first.
EXCEL_ACTIVE_SHEET = "Customers"

#: A second Worksheet, so the fixture is a MULTI-sheet workbook (p55 task 7.2 /
#: e2e Req 16.1) and so a reader that counted the wrong sheet's header is visible
#: as a wrong field set rather than as a coincidence. Its columns are
#: deliberately unlike the active sheet's.
EXCEL_SECOND_SHEET = "Notes"
EXCEL_SECOND_SHEET_COLUMNS: tuple[str, ...] = ("note_id", "customer_id", "note")
EXCEL_SECOND_SHEET_ROWS: tuple[tuple[Any, ...], ...] = (
    (1, 1, "prefers email contact"),
    (2, 3, "duplicate account merged 2024-02-01"),
)

#: What ``extract_schema_from_excel`` must return for ``customer_data.xlsx``.
#:
#: EVERY field is ``string``, unlike :data:`EXPECTED_JSON_SCHEMA`, because the
#: frame the generated Glue job receives is the CONVERTED CSV read with
#: ``.option("inferSchema", False)`` — see the module docstring. The oracle is
#: :data:`INTENDED_SCHEMA`'s hand-written column NAMES; nothing here is read back
#: out of the written file.
EXPECTED_EXCEL_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {column.name: {"type": "string"} for column in INTENDED_SCHEMA},
}

#: ``not_a_workbook.xlsx``'s bytes: CSV text under an ``.xlsx`` name.
#:
#: The realistic shape of the mistake R6.5 governs — an export renamed rather
#: than converted — and definitively not a zip, so ``openpyxl.load_workbook``
#: cannot open it. It must NOT begin with the zip local-file magic ``PK\x03\x04``;
#: :func:`verify` asserts that, because a fixture that happened to be openable
#: would make the refusal test vacuous while still passing.
MALFORMED_EXCEL_BYTES = b"id,name,email\n1,Alice Johnson,alice.johnson@example.com\n2,Bob Smith,bob.smith@example.com\n"

#: The five shared columns are ``customer_data.csv``'s ten rows verbatim.
#: ``account_balance`` is this script's addition; it is null on two rows so the
#: nullable declaration is exercised by data and not only by the schema.
ROWS: tuple[dict[str, Any], ...] = (
    {
        "id": 1,
        "name": "Alice Johnson",
        "email": "alice.johnson@example.com",
        "phone": "+1-555-0101",
        "created_at": datetime(2024, 1, 15, 10, 30, 0, tzinfo=UTC),
        "account_balance": 1234.56,
    },
    {
        "id": 2,
        "name": "Bob Smith",
        "email": "bob.smith@example.com",
        "phone": "+1-555-0102",
        "created_at": datetime(2024, 1, 16, 11, 0, 0, tzinfo=UTC),
        "account_balance": 0.0,
    },
    {
        "id": 3,
        "name": "Carol Williams",
        "email": "carol.williams@example.com",
        "phone": "+1-555-0103",
        "created_at": datetime(2024, 1, 17, 9, 15, 0, tzinfo=UTC),
        "account_balance": 987.65,
    },
    {
        "id": 4,
        "name": "David Brown",
        "email": "david.brown@example.com",
        "phone": "+1-555-0104",
        "created_at": datetime(2024, 1, 18, 14, 45, 0, tzinfo=UTC),
        "account_balance": None,
    },
    {
        "id": 5,
        "name": "Eve Davis",
        "email": "eve.davis@example.com",
        "phone": "+1-555-0105",
        "created_at": datetime(2024, 1, 19, 8, 0, 0, tzinfo=UTC),
        "account_balance": -42.5,
    },
    {
        "id": 6,
        "name": "Frank Miller",
        "email": "frank.miller@example.com",
        "phone": "+1-555-0106",
        "created_at": datetime(2024, 1, 20, 16, 30, 0, tzinfo=UTC),
        "account_balance": 15000.0,
    },
    {
        "id": 7,
        "name": "Grace Wilson",
        "email": "grace.wilson@example.com",
        "phone": "+1-555-0107",
        "created_at": datetime(2024, 1, 21, 12, 0, 0, tzinfo=UTC),
        "account_balance": 0.01,
    },
    {
        "id": 8,
        "name": "Henry Moore",
        "email": "henry.moore@example.com",
        "phone": "+1-555-0108",
        "created_at": datetime(2024, 1, 22, 10, 0, 0, tzinfo=UTC),
        "account_balance": None,
    },
    {
        "id": 9,
        "name": "Iris Taylor",
        "email": "iris.taylor@example.com",
        "phone": "+1-555-0109",
        "created_at": datetime(2024, 1, 23, 13, 20, 0, tzinfo=UTC),
        "account_balance": 3141.59,
    },
    {
        "id": 10,
        "name": "Jack Anderson",
        "email": "jack.anderson@example.com",
        "phone": "+1-555-0110",
        "created_at": datetime(2024, 1, 24, 15, 45, 0, tzinfo=UTC),
        "account_balance": 250.0,
    },
)


# ---------------------------------------------------------------------------
# Schema construction — both halves derived from INTENDED_SCHEMA
# ---------------------------------------------------------------------------


def build_avro_schema() -> dict[str, Any]:
    """The Avro writer schema, as it will appear in the OCF header.

    ``fastavro`` serializes this dict into the container metadata under
    ``avro.schema``, which is the JSON task 4's hand-parser has to recover.
    """
    return {
        "type": "record",
        "name": AVRO_RECORD_NAME,
        "namespace": AVRO_NAMESPACE,
        "doc": "p46 schema-extraction fixture. Generated by tests/fixtures/data/generate_binary_fixtures.py.",
        "fields": [{"name": column.name, "type": column.avro_type} for column in INTENDED_SCHEMA],
    }


def _arrow_type(key: str):
    """Resolve a ``FixtureColumn.arrow_type`` key to a pyarrow type."""
    import pyarrow as pa

    types = {
        "int64": pa.int64(),
        "string": pa.string(),
        "float64": pa.float64(),
        # Deliberately no timezone. ORC's timestamp type carries none, so a
        # tz-aware Arrow type would not come back as written; the unit does not
        # come back as written either (ms in, ns out — see the module
        # docstring). Neither matters to the extracted schema, because
        # arrow_schema_to_json_schema maps every timestamp to "string" via
        # pa.types.is_timestamp (schema_extract/lambda_function.py:377-378).
        "timestamp_ms": pa.timestamp("ms"),
    }
    if key not in types:
        raise KeyError(f"unknown arrow_type key {key!r}; add it to _arrow_type")
    return types[key]


def build_arrow_schema():
    """The Arrow schema pyarrow writes into the ORC footer."""
    import pyarrow as pa

    return pa.schema(
        [pa.field(column.name, _arrow_type(column.arrow_type), nullable=column.nullable) for column in INTENDED_SCHEMA]
    )


def build_arrow_table():
    import pyarrow as pa

    schema = build_arrow_schema()
    columns = {column.name: [row[column.name] for row in ROWS] for column in INTENDED_SCHEMA}
    return pa.table(columns, schema=schema)


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------


def write_avro(path: Path) -> Path:
    """Write the Avro OCF fixture.

    ``fastavro`` is a dev-only dependency. It is deliberately absent from the
    Lambda layer (R7.2, ``layer-measurement.md``), which is why task 4 parses
    the OCF header by hand rather than calling it.
    """
    try:
        import fastavro
    except ImportError as exc:  # pragma: no cover - developer-facing message
        raise SystemExit(
            "fastavro is required to write the Avro fixture. It is a dev-only dependency:\n"
            "    uv sync            # it is in the `dev` group\n"
            "    uv pip install fastavro\n"
            "It is intentionally NOT in the Lambda layer — see "
            ".kiro/specs/p46-avro-orc-schema-extraction/layer-measurement.md"
        ) from exc

    schema = fastavro.parse_schema(build_avro_schema())
    with path.open("wb") as handle:
        fastavro.writer(handle, schema, list(ROWS))
    return path


def write_orc(path: Path) -> Path:
    """Write the ORC fixture via ``pyarrow.orc.write_table``."""
    import pyarrow.orc as orc

    orc.write_table(build_arrow_table(), str(path))
    return path


def _excel_cell(value: Any) -> Any:
    """Coerce one :data:`ROWS` value into something Excel can store.

    Observed, not anticipated: ``openpyxl`` **raises**
    ``TypeError: Excel does not support timezones in datetimes`` on a tz-aware
    value, so :data:`ROWS`' UTC ``created_at`` cannot be written as it stands.
    The tzinfo is dropped rather than the value reformatted, because every
    ``created_at`` in :data:`ROWS` is already UTC, so the wall clock is
    unchanged and the workbook keeps a real date cell rather than a string.

    It does not affect the oracle either way: ``extract_schema_from_excel``
    declares every column ``string`` (see :data:`EXPECTED_EXCEL_JSON_SCHEMA`), so
    no cell's type reaches the schema. Only the header row does.
    """
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def write_xlsx(path: Path) -> Path:
    """Write the two-Worksheet Excel_Workbook fixture via ``openpyxl``.

    :data:`EXCEL_ACTIVE_SHEET` is created FIRST — by renaming the workbook's
    default sheet — so it is the sheet ``workbook.active`` resolves to when the
    file is loaded. ``_read_excel_table``'s no-``selected_worksheet`` path reads
    exactly that sheet, so the creation order is load-bearing rather than
    incidental.

    Unlike the Avro and ORC writers, this one uses the SAME library the runtime
    reads with (``openpyxl``, pinned in ``shared/runtime_requirements.txt`` by p55
    task 4.3). The independent oracle here is the hand-written column list in
    :data:`INTENDED_SCHEMA` / :data:`EXCEL_SECOND_SHEET_COLUMNS`, not a second
    parse of these bytes.
    """
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover - developer-facing message
        raise SystemExit(
            "openpyxl is required to write the Excel fixture:\n"
            "    uv sync            # it is in the project dependencies\n"
            "It is also a RUNTIME dependency — shared/runtime_requirements.txt pins openpyxl==3.1.5."
        ) from exc

    workbook = openpyxl.Workbook()
    active = workbook.active
    active.title = EXCEL_ACTIVE_SHEET
    active.append([column.name for column in INTENDED_SCHEMA])
    for row in ROWS:
        active.append([_excel_cell(row[column.name]) for column in INTENDED_SCHEMA])

    second = workbook.create_sheet(EXCEL_SECOND_SHEET)
    second.append(list(EXCEL_SECOND_SHEET_COLUMNS))
    for note_row in EXCEL_SECOND_SHEET_ROWS:
        second.append(list(note_row))

    workbook.save(str(path))
    workbook.close()
    return path


def write_malformed_xlsx(path: Path) -> Path:
    """Write the ``.xlsx``-named file that is deliberately not a zip (R6.5)."""
    path.write_bytes(MALFORMED_EXCEL_BYTES)
    return path


# ---------------------------------------------------------------------------
# Verification — read each fixture back and check it against the declaration
# ---------------------------------------------------------------------------


def _avro_observed(path: Path) -> list[tuple[str, str]]:
    """``(field name, Avro type as written)`` read back from the OCF header."""
    import fastavro

    with path.open("rb") as handle:
        writer_schema = fastavro.reader(handle).writer_schema
    return [(field["name"], _render(field["type"])) for field in writer_schema["fields"]]


def _orc_observed(path: Path) -> list[tuple[str, str]]:
    """``(column name, Arrow type as read)`` from the ORC footer."""
    import pyarrow.orc as orc

    schema = orc.ORCFile(str(path)).schema
    return [(field.name, str(field.type)) for field in schema]


def _render(avro_type: Any) -> str:
    if isinstance(avro_type, dict):
        logical = avro_type.get("logicalType")
        return f"{avro_type['type']}/{logical}" if logical else str(avro_type["type"])
    if isinstance(avro_type, list):
        return "[" + ", ".join(_render(branch) for branch in avro_type) + "]"
    return str(avro_type)


def _expected_avro() -> list[tuple[str, str]]:
    return [(column.name, _render(column.avro_type)) for column in INTENDED_SCHEMA]


def _excel_observed(path: Path) -> tuple[str, dict[str, list[str]], int]:
    """``(active sheet title, {title: header cells}, active row count)`` read back.

    Read with the same ``read_only=True, data_only=True`` arguments
    ``_read_excel_table`` uses, so what this reports is what the extractor sees.
    """
    import openpyxl

    workbook = openpyxl.load_workbook(str(path), read_only=True, data_only=True)
    try:
        headers: dict[str, list[str]] = {}
        row_counts: dict[str, int] = {}
        for title in workbook.sheetnames:
            rows = workbook[title].iter_rows(values_only=True)
            header = next(rows, ())
            headers[title] = ["" if cell is None else str(cell) for cell in header]
            row_counts[title] = sum(1 for _ in rows)
        active_title = workbook.active.title if workbook.active is not None else workbook.sheetnames[0]
        return active_title, headers, row_counts.get(active_title, 0)
    finally:
        workbook.close()


def verify(out_dir: Path) -> int:
    """Check both fixtures against ``INTENDED_SCHEMA``. Returns an exit code.

    This is the same field-by-field comparison task 4.2 and 5.2 make against the
    extractors — made here first, so a fixture that does not carry what this
    script declares is caught at generation time rather than surfacing as a
    confusing extractor failure.
    """
    failures: list[str] = []

    avro_path = out_dir / AVRO_FIXTURE
    orc_path = out_dir / ORC_FIXTURE
    excel_path = out_dir / EXCEL_FIXTURE
    malformed_path = out_dir / MALFORMED_EXCEL_FIXTURE

    for path in (avro_path, orc_path, excel_path, malformed_path):
        if not path.is_file():
            failures.append(f"missing: {path}")
    if failures:
        for line in failures:
            print(f"  FAIL {line}")
        return 1

    header = avro_path.read_bytes()[:4]
    if header != b"Obj\x01":
        failures.append(f"{AVRO_FIXTURE}: OCF magic is {header!r}, expected b'Obj\\x01'")

    observed_avro = _avro_observed(avro_path)
    expected_avro = _expected_avro()
    if observed_avro != expected_avro:
        failures.append(f"{AVRO_FIXTURE}: writer schema is {observed_avro}, declared {expected_avro}")

    observed_orc = _orc_observed(orc_path)
    if [name for name, _ in observed_orc] != [column.name for column in INTENDED_SCHEMA]:
        failures.append(f"{ORC_FIXTURE}: columns are {observed_orc}, declared {[c.name for c in INTENDED_SCHEMA]}")

    # strict=False on both: a length mismatch is already recorded as a failure
    # above, and printing the rows that do line up is more useful here than
    # raising over the ones that do not.
    print(f"{AVRO_FIXTURE}  ({avro_path.stat().st_size} bytes, magic {header!r})")
    for (name, observed), (_, expected) in zip(observed_avro, expected_avro, strict=False):
        mark = "ok  " if observed == expected else "DIFF"
        print(f"  {mark} {name:<18} {observed:<24} declared {expected}")

    # Names are checked; the Arrow types are reported, not asserted. Neither the
    # unit nor the nullability survives the ORC round trip (module docstring),
    # and confirming the type -> JSON Schema step means calling the Lambda's
    # Arrow_Converter, which is task 5.2's job rather than this script's.
    print(f"{ORC_FIXTURE}  ({orc_path.stat().st_size} bytes)")
    for (name, observed), column in zip(observed_orc, INTENDED_SCHEMA, strict=False):
        print(f"  name {name:<18} {observed:<24} declared -> {column.json_schema_type}")

    # ── the two Excel fixtures (p55 task 4.6) ──────────────────────────
    excel_magic = excel_path.read_bytes()[:4]
    if excel_magic != b"PK\x03\x04":
        failures.append(f"{EXCEL_FIXTURE}: zip magic is {excel_magic!r}, expected b'PK\\x03\\x04' — an .xlsx is a zip")

    active_title, headers, active_rows = _excel_observed(excel_path)
    expected_headers = {
        EXCEL_ACTIVE_SHEET: [column.name for column in INTENDED_SCHEMA],
        EXCEL_SECOND_SHEET: list(EXCEL_SECOND_SHEET_COLUMNS),
    }
    if active_title != EXCEL_ACTIVE_SHEET:
        # Load-bearing: `_read_excel_table` with no `selected_worksheet` reads
        # `workbook.active`, so the active sheet IS the sheet under test.
        failures.append(f"{EXCEL_FIXTURE}: active sheet is {active_title!r}, declared {EXCEL_ACTIVE_SHEET!r}")
    if headers != expected_headers:
        failures.append(f"{EXCEL_FIXTURE}: headers are {headers}, declared {expected_headers}")
    if active_rows != len(ROWS):
        failures.append(f"{EXCEL_FIXTURE}: active sheet has {active_rows} data rows, declared {len(ROWS)}")

    print(f"{EXCEL_FIXTURE}  ({excel_path.stat().st_size} bytes, magic {excel_magic!r}, active {active_title!r})")
    for title, header in headers.items():
        mark = "ok  " if header == expected_headers.get(title) else "DIFF"
        print(f"  {mark} {title:<12} {header}")

    malformed = malformed_path.read_bytes()
    if malformed != MALFORMED_EXCEL_BYTES:
        failures.append(f"{MALFORMED_EXCEL_FIXTURE}: bytes differ from MALFORMED_EXCEL_BYTES")
    if malformed[:4] == b"PK\x03\x04":
        failures.append(f"{MALFORMED_EXCEL_FIXTURE}: carries zip magic, so it is not the not-a-zip case R6.5 needs")
    try:
        import openpyxl

        opened = openpyxl.load_workbook(str(malformed_path), read_only=True, data_only=True)
        opened.close()
    except Exception as exc:
        # The expected outcome. Report the exception the extractor's own
        # `except Exception` will wrap into its ValueError, so a reader can see
        # what the refusal message will name as the reason.
        print(f"{MALFORMED_EXCEL_FIXTURE}  ({len(malformed)} bytes) refuses to open: {type(exc).__name__}: {exc}")
    else:
        failures.append(
            f"{MALFORMED_EXCEL_FIXTURE}: opened as an Excel_Workbook — the R6.5 refusal test would be vacuous"
        )

    if failures:
        print()
        for line in failures:
            print(f"FAIL {line}")
        return 1

    print()
    print(f"all fixtures match INTENDED_SCHEMA ({len(INTENDED_SCHEMA)} columns, {len(ROWS)} rows)")
    print(f"EXPECTED_JSON_SCHEMA:       {EXPECTED_JSON_SCHEMA}")
    print(f"EXPECTED_EXCEL_JSON_SCHEMA: {EXPECTED_EXCEL_JSON_SCHEMA}")
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path(__file__).resolve().parent,
        help="where to write the fixtures (default: beside this script)",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="do not write; read the existing fixtures and check them against INTENDED_SCHEMA",
    )
    args = parser.parse_args(argv)

    out_dir: Path = args.out_dir
    if not args.verify_only:
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f"writing to {out_dir}")
        write_avro(out_dir / AVRO_FIXTURE)
        write_orc(out_dir / ORC_FIXTURE)
        write_xlsx(out_dir / EXCEL_FIXTURE)
        write_malformed_xlsx(out_dir / MALFORMED_EXCEL_FIXTURE)

    return verify(out_dir)


if __name__ == "__main__":
    sys.exit(main())
