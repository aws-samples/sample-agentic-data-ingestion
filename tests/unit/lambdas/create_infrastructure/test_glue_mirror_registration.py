# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Glue mirror registration — R7.1's omission, R7.2's log, R7.6, R7.7.

The ``create_infrastructure`` half of Requirement 8.11's "a test on **both sides**
rather than one". The ``job_completion`` half is
``tests/unit/lambdas/job_completion/test_mirror_metadata_location_prop.py``, which
carries Property 6. Requirement 7's rewrite moves a write across a Lambda
boundary, and R8.11 exists because that is precisely the change a test on one side
cannot see.

THIS IS FILLING A VOID, NOT DUPLICATING AN INTEGRATION ASSERTION
---------------------------------------------------------------
The registration block (``lambda_function.py:276-350``) is untested by **both** of
its covering test files:

* ``tests/unit/lambdas/create_infrastructure/test_create_infrastructure.py``
  reaches it twice — ``glue.create_table.assert_called_once()`` at the end of
  ``test_creates_all_resources_when_none_exist``, and a ``glue`` mock routed away
  from the s3tables assertions in ``test_skips_creation_when_all_exist``. Neither
  looks at an argument. Searched for ``create_database`` and ``Columns``: **zero**
  occurrences in the file.
* ``tests/integration/pipeline_flow/test_create_infrastructure_integration.py``
  contains **zero** occurrences of ``glue``, ``create_table``,
  ``create_database``, ``metadata_location`` or ``Columns`` — confirmed by search
  rather than assumed.

So the ``Parameters`` dict that every one of the seven live mirrors carries, and
the ``"Columns": []`` hardcoded in both branches (``:314`` create, ``:336``
refresh), have never had an assertion on them.

WHAT IS RED AND WHY
-------------------
**Expected state: RED.** Tasks 10.2 and 10.3 are unimplemented. Per branch:

=======================================================  ==========================
assertion                                                today
=======================================================  ==========================
``metadata_location`` omitted when none resolved (R7.1)  **RED** — ``:306`` writes
                                                         ``""`` unconditionally
the omission is logged naming the table (R7.2)           **RED** — nothing logs it
a resolved location is written verbatim                   passes — the control that
                                                          keeps this file from
                                                          being uniformly red
``Columns`` carry the declared fields (R7.7)              **RED** — ``"Columns":
                                                          []`` is hardcoded
an unmapped type falls back to ``string`` + a warning     **RED** — nothing maps
the refresh preserves a populated pointer (R7.6)          **RED** — the refresh
                                                          rebuilds ``Parameters``
                                                          from locals, so it
                                                          overwrites with ``""``
the refresh preserves the ``Columns`` it read              **RED** — no read at all
the pair is the one R5.7 derives                          passes — task 4.1 landed
a Glue failure stays non-blocking                         passes — and it must
                                                          keep passing
=======================================================  ==========================

The last two are controls, and the last one is a **standing constraint on task
10.2** rather than a characterization: design §5 risk 4 records that no test and
no live registration in this account has ever passed a non-empty ``Columns`` for
an Iceberg ``EXTERNAL_TABLE``, so Glue may reject a type string. The whole
registration must stay inside its existing non-blocking ``except`` (``:349-350``)
so a rejection degrades to today's behaviour instead of failing provisioning.

R7.6 IS THE ONE WORTH READING TWICE
-----------------------------------
It is not "do not write an empty string" — that is R7.1. It is "do not **erase**
what the other writer put there". The refresh branch runs **once per pipeline
run**: ``should_create_infrastructure`` returns ``(True, …)`` for ``S3_TABLES`` /
``NEW_S3_TABLE_BUCKET`` unconditionally (``:504-523``, whose own comment says
"Always run infrastructure creation for S3 Tables"), and ``CreateInfrastructure``
is on the machine's path for every execution. ``requirements.md``'s live evidence
shows it: the ``testingbucket`` mirror's Glue ``UpdateTime`` is 22 minutes after
its ``CreateTime``, and that was this branch.

So satisfying R7.1 alone — omitting the key instead of writing ``""`` — turns
R7.1's honest omission into **R7.6's violation on the very next run**, because
the branch rebuilds ``Parameters`` from local variables and a rebuilt dict without
the key deletes the value ``job_completion`` had written. That is why the branch
has to become read-modify-write, and why ``glue:GetTable`` is not redundant
(design §4.1; Glue ``UpdateTable`` **replaces** the stored table with the
``TableInput`` given). ``glue:GetTable`` is already allowed for this role
(``foundation-stack.ts:803-809``), verified.

WHAT IS DELIBERATELY **NOT** ASSERTED
-------------------------------------
**The Glue/Hive type vocabulary.** Design §5 risk 4: the accepted type set is
unverified, because it has never been exercised live or in a test. So the column
assertions pin the **names and their order** — which come straight from
``target_schema["fields"]`` and are what a Data Quality rule attaches to
(``LGR-7c18e81-1``, backfilled) — plus "every ``Type`` is a non-empty string" and
the ``string`` fallback for an unrecognised type that task 10.2's own text
prescribes. Pinning ``integer -> int`` here would be transcribing
``_map_json_type_to_iceberg`` (``:120-131``) as an oracle, which is the
anti-pattern ``LGR-28b4d75-5`` (**template / ratified**) and R8.7 forbid: a test
whose oracle is its own copy of the producer cannot detect the producer being
wrong.

INVENTORY ROWS 9, 10 AND 11 ARE NOT TOUCHED, AND THIS FILE IS WHY IT NEED NOT BE
--------------------------------------------------------------------------------
* **Row 9** — ``test_create_infrastructure.py:57-66``'s ``REQUIRED_RETURN_KEYS``
  contains ``"metadata_location"``. That is ``create_s3_table_bucket``'s **return
  dict**, read by the state machine; R7.1 changes the Glue registration's
  ``Parameters``. Different contract, different consumer, and the function keeps
  returning the key — asserted here by ``test_the_return_dict_still_carries_metadata_location``
  so the distinction is a test rather than a claim in a table.
* **Row 10** — ``:285``/``:315`` set ``metadata_location`` on a **mocked
  ``s3tables.get_table`` response**. The new code consumes a resolved value the
  same way; those tests supply one.
* **Row 11** — ``test_create_infrastructure_contract.py:94`` is inside
  ``mock_create_infra.return_value``, and ``create_s3_table_infrastructure`` is
  patched there, so the registration never executes in that test.

FIXTURES AND REGION
-------------------
``MagicMock`` clients, following ``test_create_infrastructure.py`` in this
directory — there is no ``moto.mock_aws()`` call here
(``tests/unit/test_no_duplicate_mocking_prop.py``), and moto has no S3 Tables
backend to use instead. **``AWS_REGION`` is deliberately not set**
(``LGR-3e45e65-1``); only ``AWS_DEFAULT_REGION``, which is what boto3 needs to
build the module-level clients at import.

**Validates: Requirements 7.1, 7.2, 7.6, 7.7, 8.11**
"""

from __future__ import annotations

import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Import-time env and path. AWS_REGION is absent on purpose (LGR-3e45e65-1).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_CREATE_INFRA_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_infrastructure"
    )
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
for _path in (_SHARED_DIR, _CREATE_INFRA_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

# Several Lambdas share the module name ``lambda_function``, so the entry is
# popped before importing and rebound per test by ``_ensure_correct_module``
# below — the pattern every file in ``tests/unit/lambdas/job_completion/`` uses.
# The import is wrapped so no client construction depends on ambient credentials.
sys.modules.pop("lambda_function", None)
with patch("boto3.client"), patch("boto3.resource"):
    import lambda_function as _infra


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Rebind ``sys.modules['lambda_function']`` to this Lambda for each test."""
    previous = sys.modules.get("lambda_function")
    sys.modules["lambda_function"] = _infra
    yield
    if previous is not None:
        sys.modules["lambda_function"] = previous


# ---------------------------------------------------------------------------
# The address under test — the live pair from requirements.md's evidence table
# ---------------------------------------------------------------------------
PIPELINE_ID = "testing-a1d99f1b"
BUCKET_NAME = "testingbucket"
NAMESPACE = "testingbucketns"
TABLE_NAME = "testingbuckettn"

#: Spelled out from Requirement 5.7 rather than imported from
#: ``shared/glue_mirror_identifiers``, so the oracle is the requirement and not
#: the helper the registration calls. ``tests/unit/shared/test_glue_mirror_identifiers.py``
#: pins the helper itself.
EXPECTED_DATABASE = f"agentic_data_ingestion_{BUCKET_NAME.replace('-', '_')}"
EXPECTED_TABLE = f"{NAMESPACE}_{TABLE_NAME}"

BUCKET_ARN = f"arn:aws:s3tables:us-west-2:123456789012:bucket/{BUCKET_NAME}"
TABLE_ARN = f"{BUCKET_ARN}/table/{TABLE_NAME}"
WAREHOUSE_LOCATION = "s3://8fdb0e0f-3f68-4b3a-9c9a-1f2f0d5c7a11--table-s3"

#: A **first** Iceberg metadata version, which is the shape every live table
#: carries (``requirements.md`` R7.6's evidence: ``.../metadata/00000-<uuid>.metadata.json``,
#: i.e. produced by a first commit rather than by ``CreateTable``).
RESOLVED_LOCATION = f"{WAREHOUSE_LOCATION}/metadata/00000-4c1e9b7a-1f0a-4c2b-9a55-9e2f1d3b6c07.metadata.json"

#: What R7.6 protects: a pointer ``job_completion`` wrote on an earlier run.
PREVIOUSLY_WRITTEN_LOCATION = f"{WAREHOUSE_LOCATION}/metadata/00000-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.metadata.json"

#: The declared target schema. ``lambda_handler`` refuses to proceed without
#: ``fields`` (``:724-725`` raises ``ValueError("target_schema with fields is
#: required for S3 table bucket creation")``), so this is non-empty on every path
#: that reaches the registration — which is what makes R7.7 cost no extra read.
TARGET_SCHEMA: dict[str, Any] = {
    "name": TABLE_NAME,
    "fields": [
        {"name": "customer_id", "type": "integer", "nullable": False},
        {"name": "email", "type": "string", "nullable": True},
        {"name": "amount", "type": "number", "nullable": True},
        {"name": "active", "type": "boolean", "nullable": True},
    ],
}

EXPECTED_COLUMN_NAMES = ["customer_id", "email", "amount", "active"]

#: The columns an existing mirror already holds, once task 10.2 has run once.
EXISTING_COLUMNS = [
    {"Name": "customer_id", "Type": "int"},
    {"Name": "email", "Type": "string"},
    {"Name": "amount", "Type": "double"},
    {"Name": "active", "Type": "boolean"},
]


# ---------------------------------------------------------------------------
# The mocks
# ---------------------------------------------------------------------------
def _s3tables(metadata_location: str | None, *, exists: bool = False) -> MagicMock:
    """The S3 Tables client.

    ``metadata_location=None`` models the key being **absent** from the
    ``GetTable`` response, which is the structural state Phase 1 established:
    ``create_table(..., format="ICEBERG")`` at ``:245-247`` passes no schema, so
    S3 Tables writes no initial Iceberg metadata and ``metadataLocation`` stays
    absent until the first commit. ``""`` models the same absence as the empty
    string the current code then stores.
    """
    client = MagicMock(name="s3tables")
    client.exceptions.ConflictException = type("ConflictException", (Exception,), {})
    client.exceptions.NotFoundException = type("NotFoundException", (Exception,), {})

    client.list_table_buckets.return_value = {
        "tableBuckets": ([{"name": BUCKET_NAME, "arn": BUCKET_ARN}] if exists else [])
    }
    client.create_table_bucket.return_value = {"arn": BUCKET_ARN}
    client.list_namespaces.return_value = {"namespaces": ([{"namespace": [NAMESPACE]}] if exists else [])}
    client.list_tables.return_value = {"tables": ([{"name": TABLE_NAME, "tableARN": TABLE_ARN}] if exists else [])}
    client.create_table.return_value = {"tableARN": TABLE_ARN}

    details: dict[str, Any] = {"warehouseLocation": WAREHOUSE_LOCATION, "name": TABLE_NAME}
    if metadata_location is not None:
        details["metadataLocation"] = metadata_location
    client.get_table.return_value = details
    return client


def _glue(*, table_already_exists: bool = False, stored_table: dict[str, Any] | None = None) -> MagicMock:
    """The Glue client.

    ``exceptions.*`` are real classes rather than ``MagicMock`` attributes,
    because the registration spells its guards ``except
    glue_client.exceptions.AlreadyExistsException`` — against a bare
    ``MagicMock`` that raises ``TypeError`` inside the ``except`` clause and reads
    as a mysterious failure rather than as a missing fixture.
    """
    client = MagicMock(name="glue")
    client.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})
    client.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
    client.exceptions.InvalidInputException = type("InvalidInputException", (Exception,), {})
    client.exceptions.AccessDeniedException = type("AccessDeniedException", (Exception,), {})

    if table_already_exists:
        client.create_table.side_effect = client.exceptions.AlreadyExistsException("Table already exists")
    client.get_table.return_value = {"Table": stored_table if stored_table is not None else _stored_mirror()}
    return client


def _stored_mirror(*, metadata_location: str = "", columns: list[dict[str, str]] | None = None) -> dict[str, Any]:
    """The mirror as ``glue:GetTable`` returns it.

    The read-only fields (``DatabaseName``, ``CreateTime``, ``UpdateTime``,
    ``CreatedBy``, ``VersionId``, ``IsRegisteredWithLakeFormation``,
    ``CatalogId``) are here because a real read returns them and a writer that
    hands the read ``Table`` straight back as its ``TableInput`` would be
    rejected live. Whether task 10.2 strips them is **not** asserted — recorded so
    the omission is deliberate.
    """
    return {
        "Name": EXPECTED_TABLE,
        "DatabaseName": EXPECTED_DATABASE,
        "Description": f"S3 Tables Iceberg table: {NAMESPACE}.{TABLE_NAME}",
        "TableType": "EXTERNAL_TABLE",
        "CreateTime": "2026-08-06T12:31:12+00:00",
        "UpdateTime": "2026-08-06T12:53:13+00:00",
        "CreatedBy": "arn:aws:sts::777788889999:assumed-role/agentic-data-ingestion-dev-infrastructure-role/x",
        "IsRegisteredWithLakeFormation": False,
        "CatalogId": "777788889999",
        "VersionId": "1",
        "Parameters": {
            "table_type": "ICEBERG",
            "classification": "iceberg",
            "metadata_location": metadata_location,
        },
        "StorageDescriptor": {
            "Location": WAREHOUSE_LOCATION,
            "InputFormat": "org.apache.iceberg.mr.hive.HiveIcebergInputFormat",
            "OutputFormat": "org.apache.iceberg.mr.hive.HiveIcebergOutputFormat",
            "SerdeInfo": {"SerializationLibrary": "org.apache.iceberg.mr.hive.HiveIcebergSerDe"},
            "Columns": list(columns if columns is not None else []),
        },
    }


def _pipeline_metadata() -> dict[str, Any]:
    """``target_config`` carrying exactly the three keys ``:161-165`` reads."""
    return {
        "pipeline_id": PIPELINE_ID,
        "target_config": {
            "type": "S3_TABLES",
            "bucket_name": BUCKET_NAME,
            "namespace_name": NAMESPACE,
            "table_name": TABLE_NAME,
        },
    }


def _register(
    *,
    metadata_location: str | None,
    table_already_exists: bool = False,
    stored_table: dict[str, Any] | None = None,
    target_schema: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], MagicMock, MagicMock, MagicMock]:
    """Run ``create_s3_table_bucket`` and hand back the result and the clients.

    The registration is exercised through its **caller**, not extracted, because
    it has no seam of its own — it is an inline block inside
    ``create_s3_table_bucket`` (``:276-350``). Asserting through the caller is
    also what keeps ``target_schema["fields"]`` on the real path R7.7 depends on.
    """
    s3tables = _s3tables(metadata_location, exists=table_already_exists)
    glue = _glue(table_already_exists=table_already_exists, stored_table=stored_table)
    logger = MagicMock(name="logger")

    boto3_stub = MagicMock(name="boto3")
    boto3_stub.client.side_effect = lambda service, *a, **k: {
        "glue": glue,
        "s3tables": s3tables,
    }.get(service, MagicMock(name=f"{service}_client"))

    with (
        patch.object(_infra, "boto3", boto3_stub),
        patch.object(_infra, "get_pipeline_metadata", return_value=_pipeline_metadata()),
        patch.object(_infra, "logger", logger),
    ):
        result = _infra.create_s3_table_bucket(
            PIPELINE_ID, target_schema if target_schema is not None else TARGET_SCHEMA
        )

    return result, glue, s3tables, logger


# ---------------------------------------------------------------------------
# The oracles
# ---------------------------------------------------------------------------
def _sole_call(mock: MagicMock, what: str) -> dict[str, Any]:
    """The kwargs of the single expected call, with a readable failure."""
    calls = mock.call_args_list
    assert len(calls) == 1, f"expected exactly one {what}; observed {len(calls)}: {calls}"
    return calls[0].kwargs


def _rendered_log_lines(logger: MagicMock) -> list[str]:
    """Every log call rendered as one searchable string, at any level.

    Args are folded in rather than only the format string, because the design's
    own snippet is ``logger.info("Glue mirror %s.%s registered with no
    metadata_location: …", catalog_db, catalog_table)`` — the table name is an
    **argument**, so a message-only search would miss R7.2's whole point. The
    level is deliberately not pinned: R7.2 says "log the omission naming the
    table", not at which severity.
    """
    lines = []
    for call in logger.mock_calls:
        name = call[0]
        if not name or name.startswith("append_keys"):
            continue
        lines.append(" ".join(str(part) for part in (name, *call.args, call.kwargs)))
    return lines


# ---------------------------------------------------------------------------
# R7.1 — omit the key rather than name the location empty
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("unresolved", "label"),
    [(None, "the key is absent from the GetTable response"), ("", "the key is present and empty")],
)
def test_registration_omits_metadata_location_when_none_resolved(unresolved, label):
    """R7.1: build ``Parameters`` **without** the key, not with ``""``.

    Both spellings of "not resolved" are covered because both occur:
    ``s3tables:GetTable`` returns no ``metadataLocation`` key at all before the
    first Iceberg commit, and ``:268``'s ``.get("metadataLocation", "")`` then
    turns that into ``""``, which ``:306`` stores.

    ``LGR-78ce0b7-14`` (**template / ratified**) is why this is not cosmetic: a
    named location is a **claim that the thing exists**, and ``""`` is a name. A
    consumer that follows it gets nothing and cannot tell whether the pointer was
    never attempted or genuinely is not there yet — which is the same
    "absent versus not established" distinction R1.4 turns on, one layer down.

    **Expected RED until task 10.2**, and the failure is exactly
    ``metadata_location == ''``.

    **Validates: Requirements 7.1, 8.11**
    """
    _result, glue, _s3tables, _logger = _register(metadata_location=unresolved)

    table_input = _sole_call(glue.create_table, "glue:CreateTable")["TableInput"]
    parameters = table_input["Parameters"]

    assert "metadata_location" not in parameters, (
        f"R7.1 requires the key to be OMITTED when no location resolved ({label}); the "
        f"registration wrote metadata_location={parameters.get('metadata_location')!r}"
    )
    # The two Parameters the registration owns are unaffected by the omission.
    assert parameters["table_type"] == "ICEBERG"
    assert parameters["classification"] == "iceberg"


def test_a_resolved_metadata_location_is_written_verbatim():
    """The control: when there IS one, it is written, unchanged.

    Passes today, and it is what keeps this file from being uniformly red — a
    file where everything fails is indistinguishable from a file that is wired up
    wrong. It is also the assertion that stops task 10.2 satisfying R7.1 by never
    writing the key at all.

    **Validates: Requirements 7.1, 8.11**
    """
    _result, glue, _s3tables, _logger = _register(metadata_location=RESOLVED_LOCATION)

    table_input = _sole_call(glue.create_table, "glue:CreateTable")["TableInput"]
    assert table_input["Parameters"]["metadata_location"] == RESOLVED_LOCATION


def test_no_wait_is_introduced_while_the_metadata_location_is_absent():
    """``create_infrastructure`` must not wait for a pointer that cannot arrive.

    R7.1's second clause, and it is a **negative** assertion because the positive
    version is untestable: the design's fence is "do NOT add a wait here". Phase 1
    established the emptiness is **structural, not a race** — a table 22 minutes
    old still had no metadata (``requirements.md`` R7.6's evidence table, Glue
    ``CreateTime`` 12:31:12 against ``UpdateTime`` 12:53:13, still writing ``""``)
    — so no wait inside the 600-second Lambda timeout
    (``compute-stack.ts:479``) could succeed, and that budget sits on the Step
    Functions critical path.

    Asserted as "``s3tables:GetTable`` was called exactly once": a retry loop or a
    poll is what a wait looks like from here.

    **Validates: Requirement 7.1**
    """
    _result, _glue, s3tables, _logger = _register(metadata_location=None)

    assert s3tables.get_table.call_count == 1, (
        f"the registration must read the metadata location once and move on; "
        f"{s3tables.get_table.call_count} calls looks like a wait, and design §4.3 forbids one "
        f"because the emptiness is structural rather than a race"
    )


# ---------------------------------------------------------------------------
# R7.2 — an absent pointer must be distinguishable from an unattempted one
# ---------------------------------------------------------------------------
def test_the_registration_logs_the_omission_naming_the_table():
    """R7.2: log the omission, naming the table it applies to.

    Without this, R7.1's omission is **invisible**: a mirror with no
    ``metadata_location`` key looks identical whether the registration decided
    honestly that there was nothing to point at, or never ran the read at all, or
    hit the non-blocking ``except`` at ``:349-350`` and gave up. That third case
    is not hypothetical — it is what the entire registration does today under the
    Lambda execution path, where ``glue:CreateTable`` is ``implicitDeny`` (design
    §5 risk 5).

    The level is not pinned and the message wording is not pinned. What is pinned
    is that **some** log record names the mirror table and says the pointer is
    absent, so an operator reading the logs can tell the three cases apart.

    **Expected RED until task 10.2.**

    **Validates: Requirements 7.2, 8.11**
    """
    _result, _glue, _s3tables, logger = _register(metadata_location=None)

    lines = _rendered_log_lines(logger)
    naming_the_table = [line for line in lines if EXPECTED_TABLE in line]
    assert naming_the_table, (
        f"R7.2 requires the omission to be logged NAMING the table; no log record mentions "
        f"{EXPECTED_TABLE!r}. Records observed:\n  " + "\n  ".join(lines)
    )
    about_the_omission = [line for line in naming_the_table if "metadata_location" in line]
    assert about_the_omission, (
        "a log record names the table but none of them is about the missing metadata_location, "
        "so an absent pointer is still indistinguishable from an unattempted one. Records "
        "naming the table:\n  " + "\n  ".join(naming_the_table)
    )


def test_no_omission_is_logged_when_the_pointer_did_resolve():
    """The other half: a resolved pointer produces no omission notice.

    A log line emitted unconditionally carries no information, which would make
    the assertion above pass while leaving the three cases indistinguishable —
    the same reasoning that makes ``fieldCount: null`` load-bearing rather than
    ``0`` elsewhere in this spec.

    **Validates: Requirement 7.2**
    """
    _result, _glue, _s3tables, logger = _register(metadata_location=RESOLVED_LOCATION)

    omission_notices = [
        line
        for line in _rendered_log_lines(logger)
        if EXPECTED_TABLE in line and "metadata_location" in line and ("no " in line.lower() or "omit" in line.lower())
    ]
    assert not omission_notices, (
        "the omission must be reported only when there is an omission; observed:\n  " + "\n  ".join(omission_notices)
    )


# ---------------------------------------------------------------------------
# R7.7 — the registration carries the columns the table declares
# ---------------------------------------------------------------------------
def test_registration_carries_the_columns_the_target_schema_declares():
    """R7.7: ``Columns`` describes the table, in the order the schema declares.

    ``"Columns": []`` is hardcoded today (``:314`` on create, ``:336`` on
    refresh), and a Data Quality console entry for a table with no columns
    describes nothing — which is the thing ``LGR-7c18e81-1`` (**backfilled**) says
    the registration exists to provide.

    The columns cost **no read**: ``target_schema["fields"]`` is already in hand,
    because ``lambda_handler`` raises without it (``:724-725``). That is why
    design §4.3 puts R7.7 on the registration rather than on R7.3's ``SUCCEEDED``
    write, which would need the Iceberg schema, a SigV4 client and a fifth IAM
    grant (``LGR-2eaf8a3-1``).

    **The type mapping is deliberately not pinned** — design §5 risk 4: the
    accepted Glue type vocabulary has never been exercised, live or in a test.
    Names, order and "a non-empty ``Type``" are what this asserts.

    **Expected RED until task 10.2.**

    **Validates: Requirements 7.7, 8.11**
    """
    _result, glue, _s3tables, _logger = _register(metadata_location=RESOLVED_LOCATION)

    storage = _sole_call(glue.create_table, "glue:CreateTable")["TableInput"]["StorageDescriptor"]
    columns = storage["Columns"]

    assert [column["Name"] for column in columns] == EXPECTED_COLUMN_NAMES, (
        f"R7.7 requires the declared columns, in declaration order; the registration wrote {columns!r}"
    )
    unnamed_types = [column for column in columns if not str(column.get("Type", "")).strip()]
    assert not unnamed_types, f"every column needs a type Glue can store; these carry none: {unnamed_types!r}"
    # The rest of the StorageDescriptor is not this requirement's to disturb.
    assert storage["Location"] == WAREHOUSE_LOCATION
    assert storage["SerdeInfo"]["SerializationLibrary"] == "org.apache.iceberg.mr.hive.HiveIcebergSerDe"


def test_an_unrecognised_field_type_falls_back_to_string_with_a_warning():
    """Design §5 risk 4's fallback, as a test rather than as a note.

    The vocabulary ``target_schema["fields"][].type`` actually carries is
    JSON-Schema-ish (``string``, ``number``, ``integer``, ``boolean``), but
    nothing constrains it — a schema extracted from an unusual source can carry
    anything. An unmapped type must not become an empty ``Type``, a crash, or a
    guess: it becomes ``string``, and it is **logged naming the field and the
    unmapped type**, so the coarsening is visible instead of silent.

    **Expected RED until task 10.2.**

    **Validates: Requirement 7.7**
    """
    schema = {
        "name": TABLE_NAME,
        "fields": [
            {"name": "customer_id", "type": "integer"},
            {"name": "coordinates", "type": "geography"},
        ],
    }
    _result, glue, _s3tables, logger = _register(metadata_location=RESOLVED_LOCATION, target_schema=schema)

    columns = _sole_call(glue.create_table, "glue:CreateTable")["TableInput"]["StorageDescriptor"]["Columns"]
    by_name = {column["Name"]: column for column in columns}

    assert "coordinates" in by_name, (
        f"an unmapped type must not drop the column — a missing column is a lie about the table; wrote {columns!r}"
    )
    assert by_name["coordinates"]["Type"] == "string", (
        f"an unrecognised type falls back to 'string'; got {by_name['coordinates']['Type']!r}"
    )

    lines = _rendered_log_lines(logger)
    warned = [line for line in lines if "coordinates" in line and "geography" in line]
    assert warned, (
        "the fallback must be logged naming the field and the unmapped type, or a silently "
        "coarsened column looks like a declared one. Records observed:\n  " + "\n  ".join(lines)
    )


# ---------------------------------------------------------------------------
# R7.6 — the refresh preserves what the other writer put there
# ---------------------------------------------------------------------------
def test_the_refresh_preserves_a_populated_metadata_location():
    """R7.6, and it is the trap R7.1 sets if implemented on its own.

    The refresh branch runs **once per pipeline run** —
    ``should_create_infrastructure`` returns ``(True, …)`` for S3 Tables
    unconditionally (``:504-523``) and ``CreateInfrastructure`` is on the
    machine's path for every execution. So the sequence is: ``job_completion``
    writes a real pointer on a ``SUCCEEDED`` run, and the **next** run's refresh
    rebuilds ``Parameters`` from local variables in which ``metadata_location`` is
    ``""`` again, because ``s3tables:GetTable`` at registration time still returns
    nothing useful for an unchanged table.

    Satisfying R7.1 alone — omitting the key rather than writing ``""`` — turns
    that into a **deletion**, because Glue ``UpdateTable`` replaces the stored
    table with the ``TableInput`` given. Which is why the branch has to become
    read-modify-write.

    **Expected RED until task 10.2.**

    **Validates: Requirements 7.6, 8.11**
    """
    stored = _stored_mirror(metadata_location=PREVIOUSLY_WRITTEN_LOCATION, columns=EXISTING_COLUMNS)
    _result, glue, _s3tables, _logger = _register(
        metadata_location=None, table_already_exists=True, stored_table=stored
    )

    table_input = _sole_call(glue.update_table, "glue:UpdateTable")["TableInput"]
    written = table_input["Parameters"].get("metadata_location")

    assert written == PREVIOUSLY_WRITTEN_LOCATION, (
        f"R7.6: a refresh must not overwrite a populated metadata_location with an empty or "
        f"absent one. The mirror held {PREVIOUSLY_WRITTEN_LOCATION!r} and the refresh wrote "
        f"{written!r} — which, because Glue UpdateTable REPLACES rather than merges, deletes "
        f"the pointer job_completion wrote on the last successful run"
    )


def test_the_refresh_reads_the_mirror_before_writing_it():
    """The read is load-bearing, so its absence is asserted directly.

    ``glue:GetTable`` is not redundant with ``glue:UpdateTable`` (design §4.1, and
    the reason DD1 grants all three actions rather than two): ``UpdateTable``
    replaces the stored table with the ``TableInput`` given, so a write composed
    from local variables destroys whatever it did not mention. This assertion is
    what makes the preservation above a *mechanism* rather than a coincidence —
    R7.6 cannot be satisfied without it, and ``glue:GetTable`` is already allowed
    for this role (``foundation-stack.ts:803-809``), verified.

    **Expected RED until task 10.2.**

    **Validates: Requirement 7.6**
    """
    stored = _stored_mirror(metadata_location=PREVIOUSLY_WRITTEN_LOCATION, columns=EXISTING_COLUMNS)
    _result, glue, _s3tables, _logger = _register(
        metadata_location=None, table_already_exists=True, stored_table=stored
    )

    assert glue.get_table.called, (
        "the refresh must read the stored table before writing it; without the read there is "
        "nothing to merge into and the write is a replacement"
    )
    read_kwargs = glue.get_table.call_args_list[0].kwargs
    assert read_kwargs.get("DatabaseName") == EXPECTED_DATABASE
    assert read_kwargs.get("Name") == EXPECTED_TABLE


def test_the_refresh_preserves_the_columns_it_read():
    """R7.7 has to survive the refresh, not only the create.

    Task 10.2 writes ``Columns`` on both branches, and the refresh additionally
    must not lose what it read — the same read-modify-write clause Property 6
    asserts on the ``job_completion`` side. Asserted on the ``TableInput``
    argument, never on the call count: a refresh that sends ``Parameters`` alone
    issues exactly one ``UpdateTable`` and erases four columns.

    **Expected RED until task 10.2.**

    **Validates: Requirements 7.6, 7.7**
    """
    stored = _stored_mirror(metadata_location=PREVIOUSLY_WRITTEN_LOCATION, columns=EXISTING_COLUMNS)
    _result, glue, _s3tables, _logger = _register(
        metadata_location=None, table_already_exists=True, stored_table=stored
    )

    table_input = _sole_call(glue.update_table, "glue:UpdateTable")["TableInput"]
    storage = table_input.get("StorageDescriptor")

    assert storage is not None, (
        "the refresh's TableInput carries no StorageDescriptor: Glue UpdateTable REPLACES rather "
        "than merges, so this write erases the warehouse Location and every column"
    )
    assert [column["Name"] for column in storage["Columns"]] == EXPECTED_COLUMN_NAMES, (
        f"the refresh must carry the columns forward; wrote {storage['Columns']!r}"
    )
    assert storage["Location"] == WAREHOUSE_LOCATION


def test_the_refresh_writes_a_newly_resolved_pointer_over_an_empty_one():
    """The refresh is still allowed to *improve* the pointer.

    R7.6 forbids replacing a populated value with an empty one; it does not
    freeze the key. A mirror carrying today's ``""`` whose table has since
    committed metadata must end up with the real pointer, or the seven existing
    live mirrors would stay wrong forever and R7.3 would be the only path that
    could ever fix them.

    **Expected RED until task 10.2** — the current refresh does write the resolved
    value, but this asserts it alongside the preserved ``Columns``, which do not
    exist yet.

    **Validates: Requirements 7.1, 7.6**
    """
    stored = _stored_mirror(metadata_location="", columns=EXISTING_COLUMNS)
    _result, glue, _s3tables, _logger = _register(
        metadata_location=RESOLVED_LOCATION, table_already_exists=True, stored_table=stored
    )

    table_input = _sole_call(glue.update_table, "glue:UpdateTable")["TableInput"]
    assert table_input["Parameters"]["metadata_location"] == RESOLVED_LOCATION
    assert [column["Name"] for column in table_input["StorageDescriptor"]["Columns"]] == EXPECTED_COLUMN_NAMES


# ---------------------------------------------------------------------------
# Controls — these pass today, and task 10.2 must keep them passing
# ---------------------------------------------------------------------------
def test_the_registration_addresses_the_pair_r5_7_derives():
    """The mirror's ``(database, table)`` pair, on both branches.

    Passes today: task 4.1 made ``create_infrastructure`` the defining caller of
    ``shared/glue_mirror_identifiers`` (``:282``). Asserted here because tasks
    10.2 and 10.3 must both derive the pair **through** that helper rather than
    re-inlining it — ``LGR-acca05d-1``'s ``revisit_when`` is precisely
    "``create_infrastructure`` changes either derivation at ``:272`` or ``:273``",
    and two definitions of one identifier is how a reader and a writer drift apart
    without either being wrong.

    **Validates: Requirement 8.11**
    """
    _result, glue, _s3tables, _logger = _register(metadata_location=RESOLVED_LOCATION)

    assert _sole_call(glue.create_database, "glue:CreateDatabase")["DatabaseInput"]["Name"] == EXPECTED_DATABASE
    create = _sole_call(glue.create_table, "glue:CreateTable")
    assert create["DatabaseName"] == EXPECTED_DATABASE
    assert create["TableInput"]["Name"] == EXPECTED_TABLE


def test_the_return_dict_still_carries_metadata_location():
    """Inventory row 9's subject, protected rather than amended.

    ``test_create_infrastructure.py:57-66``'s ``REQUIRED_RETURN_KEYS`` contains
    ``"metadata_location"``, and that is ``create_s3_table_bucket``'s **return
    dict** — a different contract with a different consumer (the state machine)
    from the Glue registration's ``Parameters``. R7.1 changes the second and not
    the first, so the function keeps returning the key **even when it is empty**.
    Stated as a test here so the distinction cannot be lost by someone reading
    R7.1 as "stop producing empty metadata locations".

    **Validates: Requirement 8.11**
    """
    result, _glue, _s3tables, _logger = _register(metadata_location=None)

    assert "metadata_location" in result, (
        "R7.1 is about the Glue registration's Parameters, not this return dict — inventory row 9"
    )
    assert result["metadata_location"] == "", (
        "and it stays the empty string here, because that is what the caller's contract has "
        "always meant by 'not resolved'"
    )
    assert result["status"] in ("created", "already_exists")


def test_a_glue_failure_remains_non_blocking():
    """Design §5 risk 4's fallback: a rejected registration must not fail provisioning.

    Passes today, because the whole registration sits inside a non-blocking
    ``except`` (``:349-350``). It is asserted here as a **standing constraint on
    task 10.2**: populating ``Columns`` has never been done in this repo and Glue
    may reject a type string, so the failure has to degrade to today's behaviour
    rather than break the Step Functions execution. A ``ValidationException`` on
    ``create_table`` is the concrete shape that would take.

    **Validates: Requirement 7.7**
    """
    s3tables = _s3tables(RESOLVED_LOCATION, exists=False)
    glue = _glue()
    glue.create_table.side_effect = RuntimeError("ValidationException: Unsupported column type")

    boto3_stub = MagicMock(name="boto3")
    boto3_stub.client.side_effect = lambda service, *a, **k: {"glue": glue, "s3tables": s3tables}.get(
        service, MagicMock(name=f"{service}_client")
    )

    with (
        patch.object(_infra, "boto3", boto3_stub),
        patch.object(_infra, "get_pipeline_metadata", return_value=_pipeline_metadata()),
    ):
        result = _infra.create_s3_table_bucket(PIPELINE_ID, TARGET_SCHEMA)

    assert result["status"] == "created", (
        f"a Glue registration failure must not fail S3 Tables provisioning; got {result}"
    )
    assert "error_message" not in result
