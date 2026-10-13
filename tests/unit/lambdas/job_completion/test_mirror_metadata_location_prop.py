# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 6 — metadata_location is written only on SUCCEEDED, and never as "".

# Feature: p35-schema-surfaces, Property 6: metadata_location is written only on SUCCEEDED, and never as an empty string

For any Glue job state and any ``s3tables:GetTable`` outcome, the completion path
issues a ``glue:UpdateTable`` **only** when the state is ``SUCCEEDED`` **and** a
non-empty ``metadataLocation`` resolved. On every other combination it issues
none, does **not** raise, and leaves the registration's existing state unchanged.
When it does write, the resulting ``TableInput`` preserves the
``StorageDescriptor`` and the ``Columns`` read immediately before.

WHY THIS FILE EXISTS AT ALL — IT IS FILLING A VOID, NOT DUPLICATING ONE
----------------------------------------------------------------------
The Glue mirror registration is untested by **both** of its covering test files.
``tests/unit/lambdas/create_infrastructure/test_create_infrastructure.py``
asserts ``glue.create_table.assert_called_once()`` and nothing about its
argument; ``tests/integration/pipeline_flow/test_create_infrastructure_integration.py``
contains **zero** occurrences of ``glue``, ``create_table``, ``create_database``,
``metadata_location`` or ``Columns`` — confirmed by search, not assumed. So the
`Parameters` dict every one of the seven live mirrors carries has never had an
assertion on it. Requirement 8.11 asks for a test on **both sides of the Lambda
boundary** because Requirement 7's rewrite moves a write across one; this file is
the ``job_completion`` side and
``tests/unit/lambdas/create_infrastructure/test_glue_mirror_registration.py`` is
the ``create_infrastructure`` side.

THE SUBJECT DOES NOT EXIST YET, AND THAT IS THE POINT
----------------------------------------------------
``_populate_mirror_metadata_location`` is task 10.3's; ``Columns`` at
registration is task 10.2's. **Expected state: RED**, on the write clause. Design
§5 risk 9: a reviewer who sees red and reverts has undone the only thing that
would have caught the defect. ``TestTheSpecificationIsSatisfiable`` below is what
separates "not implemented yet" from "cannot be satisfied" — it demonstrates the
prescribed shape passing every clause and the naive shape failing exactly one.

**Which clause is red, per branch:**

=========================================  ===============================
clause                                     against today's code
=========================================  ===============================
1. no ``glue:UpdateTable`` on any           passes — no writer exists, so
   non-``SUCCEEDED`` state, or when no      no call is ever made
   non-empty location resolved
2. the run is unaffected — ``200``,         passes — nothing to fail
   callback already sent, no raise
3. **on SUCCEEDED with a resolved           **RED** — no call at all
   location, exactly one write, whose
   ``TableInput`` carries the resolved
   pointer and preserves the read
   ``Columns`` / ``StorageDescriptor``
=========================================  ===============================

Clause 3 is not padding. Without it the property is satisfied by a handler that
does nothing at all, and "written only on SUCCEEDED" would be provable by never
writing — the same vacuity tasks 3.1 and 4.3 both hit at 0 reached examples out
of 200.

ASSERT ON THE ``TableInput``, NOT ON THE CALL COUNT
--------------------------------------------------
Glue ``UpdateTable`` **replaces** the stored table with the ``TableInput`` given;
it does not merge (design §4.1, and the reason DD1 has to grant ``glue:GetTable``
alongside ``glue:UpdateTable`` rather than treating the read as redundant). So a
writer that sends ``TableInput={"Name": …, "Parameters": {…}}`` — the obvious
implementation, and the one that reads correctly — **erases** the
``StorageDescriptor`` and with it the ``Columns`` Requirement 7.7 maintains and
the ``Location`` that points at the real warehouse. A call-count assertion passes
against that writer. The preservation assertion is the one that does not, which is
why the property asserts the argument.
``TestTheSpecificationIsSatisfiable::test_the_naive_parameters_only_write_passes_the_count_and_fails_the_argument``
demonstrates exactly that asymmetry rather than asserting it in prose.

THE THIRD AXIS, AND WHY THE PROPERTY NEEDS IT
---------------------------------------------
The task names two axes — job state × ``s3tables:GetTable`` outcome. The
preservation clause forces a third: **what the mirror already holds**. "Preserves
the ``Columns`` read immediately before" is only an assertion if the ``Columns``
vary, so the generator draws the mirror's existing state across today's live shape
(``Columns: []``, ``metadata_location: ""`` — all seven mirrors in the account),
the post-10.2 shape (populated ``Columns``), Requirement 7.6's shape (an already
populated pointer), a mirror with no ``Parameters`` key at all, and the two ways
the ``glue:GetTable`` read itself can fail.

**A failed read must produce no write**, and that is derived rather than
stipulated: without the read there is nothing to merge into, so writing anyway is
the clobber the read exists to prevent. ``EntityNotFoundException`` is also how a
non-S3-Tables target presents — there is no mirror to update — so that case is
covered by the same branch rather than by a separate target-type axis.

THE SEAM THIS FILE PINS, WHICH TASK 10.3 MUST HONOUR
----------------------------------------------------
Deliberately wide, so the property constrains the *behaviour* and not the
plumbing. ``_install_clients`` binds:

* ``_mod.glue_client`` — the module-level client, already present (``:57``).
* ``_mod.s3tables_client`` — bound with ``create=True`` because the attribute
  does **not** exist today; task 10.3 adds it, or does not, and either way the
  next line covers it.
* ``_mod.boto3`` — so a client constructed *inside* the writer
  (``boto3.client("s3tables", region_name=…)``, which is what
  ``create_infrastructure:174`` and ``:278`` do) is intercepted too.
* ``_mod.pipelines_table`` — the module-level handle, already read on this path
  by ``calculate_performance_comparison`` (``:161``). Per design §4.3 **no new
  environment variable is needed**: ``PIPELINES_TABLE`` is already on the
  function's environment (``compute-stack.ts:596``), and
  ``deploy-cdk/lib/compute-stack.ts`` is not in this spec's change set. A writer
  that built its own ``boto3.resource("dynamodb")`` instead would read a mock
  table this file never seeded and find no ``target_config`` — loud, but for the
  wrong reason.

The table bucket ARN is reachable **two** ways in the fixture, because both are
real: ``target_config["table_bucket_arn"]`` (read at
``schema_mapping/lambda_function.py:236`` and ``resources.py:335``) and
``s3tables:ListTableBuckets`` (``schema_mapping:347-353``'s fallback when the
config does not carry one). The property asserts nothing about which is used, and
nothing about ``get_table``'s arguments.

WHAT IS DELIBERATELY **NOT** ASSERTED
-------------------------------------
1. **Whether the ``TableInput`` strips Glue's read-only fields.** The fixture's
   ``get_table`` response carries ``DatabaseName``, ``CreateTime``,
   ``UpdateTime``, ``CreatedBy``, ``VersionId``,
   ``IsRegisteredWithLakeFormation`` and ``CatalogId``, because a real
   ``glue:GetTable`` does and a writer that passes the read ``Table`` straight
   back as its ``TableInput`` would be rejected live. Asserting it here would be
   inventing a clause neither the requirements nor design §6 names; it is recorded
   so the omission is deliberate rather than an oversight.
2. **The Glue/Hive type vocabulary.** Design §5 risk 4: no test and no live
   registration in this account has ever passed a non-empty ``Columns`` for an
   Iceberg ``EXTERNAL_TABLE``, so the accepted type set is unverified. This file
   asserts the ``Columns`` list is carried through **unchanged**, byte for byte,
   whatever it holds — which is exactly what Requirement 7.6/7.7 need from this
   writer and makes no claim about the mapping. Task 10.2's file owns the mapping,
   with a ``string`` fallback and a warning.

FIXTURES AND REGION
-------------------
``MagicMock`` clients, following the three sibling files in this directory
(``test_target_schema_version_commit.py``, ``test_processed_file_ledger_write.py``,
``test_heal_rerun_linking.py``) — there is no ``moto.mock_aws()`` call here
(``tests/unit/test_no_duplicate_mocking_prop.py``), and moto has no S3 Tables
backend to use instead. **``AWS_REGION`` is deliberately not set**
(``LGR-3e45e65-1``); only ``AWS_DEFAULT_REGION`` is, which is what boto3 needs to
construct the module-level clients at import and what
``job_completion:53``'s ``AWS_REGION`` constant falls back to.

**Validates: Requirements 7.1, 7.3, 7.4, 7.5, 7.6, 7.7, 8.11**
"""

from __future__ import annotations

import copy
import os
import sys
from typing import Any, NamedTuple
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from hypothesis import HealthCheck, Phase, example, given, settings
from hypothesis import event as _hypothesis_event
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import-time env and path. AWS_REGION is absent on purpose (LGR-3e45e65-1).
# ---------------------------------------------------------------------------
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_JOB_COMPLETION_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_job_completion")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
for _path in (_SHARED_DIR, _JOB_COMPLETION_DIR):
    if _path not in sys.path:
        sys.path.insert(0, _path)

sys.modules.pop("lambda_function", None)
import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Rebind ``sys.modules['lambda_function']`` — the sibling files' pattern.

    Several Lambdas share the module name ``lambda_function``, so whichever test
    module imported last owns the entry. Every sibling in this directory does
    this, and it is what keeps a late import inside the handler resolving to
    *this* Lambda.
    """
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# The run under test
# ---------------------------------------------------------------------------
RUN_ID = "run-p35-r73-1"
PIPELINE_ID = "testing-a1d99f1b"
JOB_RUN_ID = "jr-p35-r73"
JOB_NAME = "agentic-data-ingestion-testing-a1d99f1b"

#: A **first** Iceberg metadata version, which is the shape every one of the
#: seven live tables actually carries (``requirements.md`` R7.6's evidence table:
#: ``.../metadata/00000-<uuid>.metadata.json``, i.e. produced by a first commit
#: rather than by ``CreateTable``). Using the real shape matters because it is the
#: evidence that falsified the race hypothesis: the pointer appears at the first
#: successful write, which is why this writer lives on the SUCCEEDED callback.
RESOLVED_LOCATION = "s3://8fdb0e0f-3f68-4b3a-9c9a-1f2f0d5c7a11--table-s3/metadata/00000-4c1e9b7a-1f0a-4c2b-9a55-9e2f1d3b6c07.metadata.json"

#: What Requirement 7.6 protects: a pointer ``job_completion`` already wrote on an
#: earlier successful run. A refresh must not replace it with an empty one.
PREVIOUSLY_WRITTEN_LOCATION = "s3://8fdb0e0f-3f68-4b3a-9c9a-1f2f0d5c7a11--table-s3/metadata/00000-aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.metadata.json"

WAREHOUSE_LOCATION = "s3://8fdb0e0f-3f68-4b3a-9c9a-1f2f0d5c7a11--table-s3"

#: The post-task-10.2 ``Columns``. Preserving these is what makes Requirement 7.7
#: survive this writer; ``LGR-7c18e81-1`` (backfilled) records that the
#: registration exists for Data Quality console integration, and a DQ rule is
#: attached to a named column.
POPULATED_COLUMNS = [
    {"Name": "customer_id", "Type": "int"},
    {"Name": "email", "Type": "string"},
    {"Name": "amount", "Type": "double"},
    {"Name": "active", "Type": "boolean"},
]


class Identifiers(NamedTuple):
    """An S3 Tables address, and the Glue mirror pair it derives."""

    bucket_name: str
    namespace: str
    table_name: str

    @property
    def database(self) -> str:
        """``agentic_data_ingestion_{bucket with '-' replaced by '_'}``.

        Spelled out from Requirement 5.7 rather than imported from
        ``shared/glue_mirror_identifiers``, so this property's oracle is the
        requirement and not the code the writer will call. ``LGR-acca05d-1``'s
        ``revisit_when`` is "``create_infrastructure`` changes either
        derivation" — if that ever happens, this expression is one of the places
        that must be re-read, which is exactly the visibility one definition is
        for.
        """
        return f"agentic_data_ingestion_{self.bucket_name.replace('-', '_')}"

    @property
    def table(self) -> str:
        """``{namespace}_{table_name}``."""
        return f"{self.namespace}_{self.table_name}"


#: The live pair (``requirements.md``'s evidence table names
#: ``agentic_data_ingestion_testingbucket.testingbucketns_testingbuckettn``), plus
#: a hyphenated bucket and an underscored namespace and table. The last three are
#: DD7's axis 2 — the reachable underscore collision the requirements did not
#: name — carried here so the pair derivation is exercised rather than assumed.
_IDENTIFIERS = st.sampled_from(
    (
        Identifiers("testingbucket", "testingbucketns", "testingbuckettn"),
        Identifiers("agentic-data-ingestion-table-hello123", "hello123", "orders"),
        Identifiers("sales-db-bucket", "sales_ns", "customer_master"),
        Identifiers("a-b", "ns_one", "tbl_two"),
    )
)


# ---------------------------------------------------------------------------
# The two named axes, plus the mirror-state axis the preservation clause forces
# ---------------------------------------------------------------------------
#: Every Glue job state the completion event can carry, plus three that are
#: **not** ``SUCCEEDED`` while looking like it. ``state != "SUCCEEDED"`` is an
#: exact comparison in both existing SUCCEEDED-gated writers (``:307``, ``:375``),
#: so a lowercase, title-cased or space-padded spelling must write nothing. Those
#: three are the difference between a guard that is right and one that is lucky.
#:
#: **``SUCCEEDED`` is its own branch, deliberately, and the reason is measured.**
#: With one flat ten-element pool the property's *write* branch — the conjunction
#: of ``SUCCEEDED`` and a resolved location and a readable mirror — came out at
#: 1/10 × 1/6 × 4/6 ≈ **1.1%**, which over 100 examples is roughly one generated
#: case, and the one case the naive implementation fails was reached **exactly
#: once in 212 examples, entirely from its ``@example``**. Hypothesis's ``one_of``
#: splits roughly evenly across branches, so hoisting the value the antecedent
#: needs into a branch of its own lifts the write branch to ~17% without removing
#: anything from the pool. The counts are recorded in the task; the aggregate
#: coverage test is what keeps them honest.
_JOB_STATES = st.one_of(
    st.just("SUCCEEDED"),
    st.sampled_from(("FAILED", "STOPPED", "TIMEOUT", "ERROR", "RUNNING", "succeeded", "Succeeded", "SUCCEEDED ", "")),
)

#: Axis 2 — every way ``s3tables:GetTable`` can answer.
#:
#: ``raises_access_denied`` is not hypothetical: it is the **deployed** state of
#: this account until Unit A's Foundation deploy lands. Design §2 verified
#: ``s3tables:GetTable`` as ``implicitDeny`` for the job-completion role with
#: ``iam:simulate-principal-policy`` against a real resource ARN, so on every
#: successful run between task 10.3 shipping and Foundation deploying, this is
#: the branch that executes. It must log and return, never fail the run (R7.4).
#:
#: ``resolved`` is hoisted into its own branch for the same measured reason as
#: ``SUCCEEDED`` above — it is the second half of the property's antecedent.
_GET_TABLE_OUTCOMES = st.one_of(
    st.just("resolved"),
    st.sampled_from(("empty_string", "absent_key", "raises_access_denied", "raises_not_found", "raises_unexpected")),
)

#: Axis 3 — what the mirror already holds, and whether it can be read.
#:
#: ``empty_columns`` is today's live shape for all seven mirrors:
#: ``Columns: []`` and ``metadata_location: ""``. ``populated_columns`` is the
#: post-10.2 shape. ``populated_pointer`` is Requirement 7.6's case.
#: ``no_parameters`` is a mirror with no ``Parameters`` key at all — Glue does not
#: require one, so an implementation subscripting it would raise.
#: The two ``read_*`` entries make the read's own failure a generated case:
#: ``entity_not_found`` is also how a non-S3-Tables target presents, since there
#: is no mirror to update.
_MIRROR_STATES = st.sampled_from(
    (
        "empty_columns",
        "populated_columns",
        "populated_pointer",
        "no_parameters",
        "read_entity_not_found",
        "read_access_denied",
    )
)

_READ_FAILS = ("read_entity_not_found", "read_access_denied")


class Scenario(NamedTuple):
    """One completion event, one ``s3tables`` answer, one mirror state."""

    state: str
    outcome: str
    mirror: str
    identifiers: Identifiers

    @property
    def should_write(self) -> bool:
        """Property 6's antecedent, written from the requirement.

        ``SUCCEEDED`` (R7.5) **and** a non-empty resolved location (R7.1's rule
        applied to this writer: a named location is a claim the thing exists,
        ``LGR-78ce0b7-14``) **and** a mirror that could be read — because Glue
        ``UpdateTable`` replaces rather than merges, so without the read there is
        nothing to merge into and writing anyway is the clobber the read exists
        to prevent.
        """
        return self.state == "SUCCEEDED" and self.outcome == "resolved" and self.mirror not in _READ_FAILS


# ---------------------------------------------------------------------------
# The mirror as Glue would return it
# ---------------------------------------------------------------------------
def _storage_descriptor(columns: list[dict[str, str]]) -> dict[str, Any]:
    """The ``StorageDescriptor`` ``create_infrastructure`` writes (``:309-315``)."""
    return {
        "Location": WAREHOUSE_LOCATION,
        "InputFormat": "org.apache.iceberg.mr.hive.HiveIcebergInputFormat",
        "OutputFormat": "org.apache.iceberg.mr.hive.HiveIcebergOutputFormat",
        "SerdeInfo": {"SerializationLibrary": "org.apache.iceberg.mr.hive.HiveIcebergSerDe"},
        "Columns": columns,
    }


def _existing_table(scenario: Scenario) -> dict[str, Any]:
    """The stored Glue table, including the read-only fields a real read returns.

    ``DatabaseName``, ``CreateTime``, ``UpdateTime``, ``CreatedBy``,
    ``VersionId``, ``IsRegisteredWithLakeFormation`` and ``CatalogId`` are what
    ``glue:GetTable`` actually sends back and what ``glue:UpdateTable`` rejects
    inside a ``TableInput``. They are here because the fixture should look like
    the API; whether the writer strips them is **not asserted** — see the module
    docstring.
    """
    identifiers = scenario.identifiers
    table: dict[str, Any] = {
        "Name": identifiers.table,
        "DatabaseName": identifiers.database,
        "Description": f"S3 Tables Iceberg table: {identifiers.namespace}.{identifiers.table_name}",
        "TableType": "EXTERNAL_TABLE",
        "CreateTime": "2026-08-06T12:31:12+00:00",
        "UpdateTime": "2026-08-06T12:53:13+00:00",
        "CreatedBy": "arn:aws:sts::777788889999:assumed-role/agentic-data-ingestion-dev-infrastructure-role/x",
        "IsRegisteredWithLakeFormation": False,
        "CatalogId": "777788889999",
        "VersionId": "1",
    }

    if scenario.mirror == "no_parameters":
        table["StorageDescriptor"] = _storage_descriptor([])
        return table

    columns = POPULATED_COLUMNS if scenario.mirror in ("populated_columns", "populated_pointer") else []
    existing_pointer = PREVIOUSLY_WRITTEN_LOCATION if scenario.mirror == "populated_pointer" else ""
    table["Parameters"] = {
        "table_type": "ICEBERG",
        "classification": "iceberg",
        # Today's live value on all seven mirrors is ``""`` — the thing R7.1
        # stops being written in the first place.
        "metadata_location": existing_pointer,
    }
    table["StorageDescriptor"] = _storage_descriptor(copy.deepcopy(columns))
    return table


def _expected_columns(scenario: Scenario) -> list[dict[str, str]]:
    """The ``Columns`` a write must carry forward: exactly what the read held."""
    return list(_existing_table(scenario).get("StorageDescriptor", {}).get("Columns", []))


# ---------------------------------------------------------------------------
# The mocks
# ---------------------------------------------------------------------------
def _access_denied(operation: str) -> ClientError:
    """The error the deployed role returns today (design §2's `implicitDeny`)."""
    return ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": f"not authorized to perform {operation}"}},
        operation,
    )


def _glue_client(scenario: Scenario) -> MagicMock:
    """The Glue client: the job-run read, the mirror read, and the write.

    ``exceptions.*`` are real exception classes rather than ``MagicMock``
    attributes, because a writer that spells its guard
    ``except glue_client.exceptions.EntityNotFoundException`` needs a class —
    against a bare ``MagicMock`` that raises ``TypeError`` inside the ``except``
    clause and would read as a mysterious failure rather than as a missing
    fixture.
    """
    glue = MagicMock(name="glue")
    glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (ClientError,), {})
    glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (ClientError,), {})
    glue.exceptions.AccessDeniedException = type("AccessDeniedException", (ClientError,), {})
    glue.exceptions.InvalidInputException = type("InvalidInputException", (ClientError,), {})

    glue.get_job_run.return_value = {
        "JobRun": {
            "JobRunState": scenario.state or "UNKNOWN",
            "ExecutionTime": 120,
            "NumberOfWorkers": 2,
            "WorkerType": "G.1X",
            "MaxCapacity": 2.0,
            "GlueVersion": "5.0",
            "ErrorMessage": "",
        }
    }

    if scenario.mirror == "read_entity_not_found":
        glue.get_table.side_effect = ClientError(
            {"Error": {"Code": "EntityNotFoundException", "Message": "Table not found"}}, "GetTable"
        )
    elif scenario.mirror == "read_access_denied":
        glue.get_table.side_effect = _access_denied("GetTable")
    else:
        glue.get_table.return_value = {"Table": _existing_table(scenario)}

    return glue


def _s3tables_client(scenario: Scenario) -> MagicMock:
    """The S3 Tables client, answering ``get_table`` per axis 2.

    ``list_table_buckets`` and ``list_tables`` are wired even though the property
    asserts nothing about them: the ARN is reachable either from
    ``target_config`` or by resolving the bucket name
    (``schema_mapping:347-353``'s own fallback), and the property should not
    dictate which route task 10.3 takes.
    """
    s3tables = MagicMock(name="s3tables")
    s3tables.exceptions.NotFoundException = type("NotFoundException", (ClientError,), {})
    s3tables.exceptions.AccessDeniedException = type("AccessDeniedException", (ClientError,), {})
    s3tables.exceptions.ConflictException = type("ConflictException", (ClientError,), {})

    identifiers = scenario.identifiers
    s3tables.list_table_buckets.return_value = {
        "tableBuckets": [{"name": identifiers.bucket_name, "arn": _bucket_arn(identifiers)}]
    }
    s3tables.list_tables.return_value = {
        "tables": [{"name": identifiers.table_name, "tableARN": _table_arn(identifiers)}]
    }

    if scenario.outcome == "resolved":
        s3tables.get_table.return_value = {
            "metadataLocation": RESOLVED_LOCATION,
            "warehouseLocation": WAREHOUSE_LOCATION,
            "name": identifiers.table_name,
        }
    elif scenario.outcome == "empty_string":
        # The value ``create_infrastructure`` reads today: GetTable succeeds and
        # returns the key empty, because CreateTable wrote no Iceberg metadata.
        s3tables.get_table.return_value = {
            "metadataLocation": "",
            "warehouseLocation": WAREHOUSE_LOCATION,
            "name": identifiers.table_name,
        }
    elif scenario.outcome == "absent_key":
        s3tables.get_table.return_value = {
            "warehouseLocation": WAREHOUSE_LOCATION,
            "name": identifiers.table_name,
        }
    elif scenario.outcome == "raises_access_denied":
        s3tables.get_table.side_effect = _access_denied("GetTable")
    elif scenario.outcome == "raises_not_found":
        s3tables.get_table.side_effect = ClientError(
            {"Error": {"Code": "NotFoundException", "Message": "table not found"}}, "GetTable"
        )
    else:
        s3tables.get_table.side_effect = RuntimeError("s3tables endpoint unreachable")

    return s3tables


def _bucket_arn(identifiers: Identifiers) -> str:
    return f"arn:aws:s3tables:us-east-1:123456789012:bucket/{identifiers.bucket_name}"


def _table_arn(identifiers: Identifiers) -> str:
    return f"{_bucket_arn(identifiers)}/table/{identifiers.table_name}"


def _pipelines_table(scenario: Scenario) -> MagicMock:
    """The pipelines table, carrying the ``target_config`` the writer reads.

    ``table_bucket_arn`` is present because it is present in real records
    (``schema_mapping:236``, ``resources.py:335``), and the bucket name is present
    because ``create_s3_table_bucket:161-165`` reads exactly these three keys. No
    ``worker_configuration`` / ``cost_information``, so
    ``calculate_performance_comparison`` reads ``{}`` for both via ``item_dict``
    and returns an all-zero comparison — which is not this property's subject.
    """
    identifiers = scenario.identifiers
    table = MagicMock(name="pipelines_table")
    table.get_item.return_value = {
        "Item": {
            "pipeline_id": PIPELINE_ID,
            "target_config": {
                "type": "S3_TABLES",
                "bucket_name": identifiers.bucket_name,
                "namespace_name": identifiers.namespace,
                "table_name": identifiers.table_name,
                "table_bucket_arn": _bucket_arn(identifiers),
            },
        }
    }
    return table


def _runs_table() -> MagicMock:
    """The state table: the GSI lookup plus the two rows the siblings read.

    ``file_validation`` carries no ``content_fingerprint`` and
    ``triggered_by="s3_upload"``, so ``_record_processed_file`` and
    ``_link_heal_rerun_outcome`` both return early — neither is this property's
    subject, and leaving them active would put unrelated writes in the record.
    """
    rows = {
        "file_validation": {"run_id": RUN_ID, "step_name": "file_validation", "triggered_by": "s3_upload"},
        "schema_compare": {"run_id": RUN_ID, "step_name": "schema_compare"},
    }
    runs = MagicMock(name="pipeline_runs_table")
    runs.query.return_value = {
        "Items": [
            {
                "run_id": RUN_ID,
                "step_name": "run_pipeline",
                "original_pipeline_id": PIPELINE_ID,
                "glue_job_run_id": JOB_RUN_ID,
                "status": "RUNNING",
            }
        ]
    }
    runs.get_item.side_effect = lambda Key: {"Item": rows[Key["step_name"]]} if Key.get("step_name") in rows else {}
    return runs


def _task_tokens_table() -> MagicMock:
    """The run's Glue-callback token row — run_pipeline stores it in the
    task-tokens table, never on the state row."""
    tokens = MagicMock(name="task_tokens_table")
    tokens.get_item.return_value = {"Item": {"approval_id": f"glue_run#{RUN_ID}", "task_token": "token-p35-r73"}}
    return tokens


class Clients(NamedTuple):
    """Everything the handler touched, for assertion after the run."""

    glue: MagicMock
    s3tables: MagicMock
    pipelines: MagicMock
    runs: MagicMock
    sfn: MagicMock


def _run_completion(scenario: Scenario) -> tuple[dict[str, Any], Clients]:
    """Drive ``lambda_handler`` for one scenario and hand back the clients.

    Expressed against the **handler**, not against
    ``_populate_mirror_metadata_location`` directly, for two reasons. Property 6
    is a statement about "the completion path", and a writer that exists but is
    never called satisfies nothing — the call site (after ``_record_processed_file``,
    ``:626``/``:627``, i.e. after the Step Functions callback) is half of what
    R7.4 asks for.
    """
    glue = _glue_client(scenario)
    s3tables = _s3tables_client(scenario)
    pipelines = _pipelines_table(scenario)
    runs = _runs_table()
    sfn = MagicMock(name="sfn")

    boto3_stub = MagicMock(name="boto3")
    boto3_stub.client.side_effect = lambda service, *a, **k: {
        "glue": glue,
        "s3tables": s3tables,
    }.get(service, MagicMock(name=f"{service}_client"))

    event = {"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": scenario.state}}

    with (
        patch.object(_mod, "pipeline_runs_table", runs),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "pipelines_table", pipelines),
        patch.object(_mod, "glue_client", glue),
        patch.object(_mod, "sfn_client", sfn),
        patch.object(_mod, "lambda_client", MagicMock(name="lambda")),
        patch.object(_mod, "s3tables_client", s3tables, create=True),
        patch.object(_mod, "boto3", boto3_stub),
        # SELF_HEALING_FN unset so a non-SUCCEEDED state does not add a dispatch
        # to the record. The dispatch is P28 R9.3's subject, not this one's.
        patch.dict(os.environ, {"SELF_HEALING_FN": ""}, clear=False),
    ):
        result = _mod.lambda_handler(event, None)

    return result, Clients(glue, s3tables, pipelines, runs, sfn)


# ---------------------------------------------------------------------------
# The oracles
# ---------------------------------------------------------------------------
#: Every Glue call that changes the catalog. A writer that answered a missing
#: mirror by *creating* one, or that deleted and re-registered instead of
#: updating, would leave ``update_table`` at zero and still have mutated the
#: registration — so the absence clause is checked against all of these, not
#: against ``update_table`` alone.
_MUTATING_GLUE_OPERATIONS = (
    "update_table",
    "create_table",
    "delete_table",
    "batch_delete_table",
    "create_database",
    "delete_database",
    "update_database",
    "create_partition",
    "delete_partition",
)


def _mutating_glue_calls(glue: MagicMock) -> list[str]:
    """The names of every catalog-mutating call the run made, in order."""
    return [name for name, _args, _kwargs in glue.mock_calls if name in _MUTATING_GLUE_OPERATIONS]


def _update_table_calls(glue: MagicMock) -> list[dict[str, Any]]:
    """The kwargs of every ``glue:UpdateTable``, in order."""
    return [call.kwargs for call in glue.update_table.call_args_list]


def _written_pointer(update_kwargs: dict[str, Any]) -> str | None:
    """``TableInput.Parameters.metadata_location``, or ``None`` when absent.

    ``None`` and ``""`` are deliberately distinguishable here: R7.1 makes
    "the key is absent" the honest state and ``""`` the dishonest one
    (``LGR-78ce0b7-14`` — a named location is a claim the thing exists, and
    ``""`` is a name).
    """
    parameters = update_kwargs.get("TableInput", {}).get("Parameters", {})
    return parameters.get("metadata_location")


# ---------------------------------------------------------------------------
# The generator
# ---------------------------------------------------------------------------
@st.composite
def completion_scenarios(draw) -> Scenario:
    """One (state × ``s3tables`` outcome × mirror state × address) combination."""
    return Scenario(
        state=draw(_JOB_STATES),
        outcome=draw(_GET_TABLE_OUTCOMES),
        mirror=draw(_MIRROR_STATES),
        identifiers=draw(_IDENTIFIERS),
    )


_LIVE = Identifiers("testingbucket", "testingbucketns", "testingbuckettn")
_HYPHENATED = Identifiers("agentic-data-ingestion-table-hello123", "hello123", "orders")

#: The cases this property exists for, as **explicit** inputs rather than as
#: draws that might happen.
#:
#: Every earlier task in this plan that relied on the draw got burned. Task 3.1
#: and task 4.3 each shipped a generator that reached its target case **0 times
#: out of 200 and passed anyway**, and task 8.1 measured a case appearing 0 times
#: in one run and ~25 in the next purely on the Hypothesis seed, because
#: ``one_of``/``sampled_from`` bias toward their first branch and first elements.
#: A coverage claim that depends on the seed is not a claim. So the pools stay —
#: they are what gives the random half its variety — and these twelve are
#: guaranteed. ``Phase.explicit`` runs them before any generated example, in both
#: the survey and the property.
_CRITICAL_SCENARIOS = (
    # ── The write branch, across every mirror state ──
    # Today's live shape: Columns == [] and metadata_location == "".
    Scenario("SUCCEEDED", "resolved", "empty_columns", _LIVE),
    # The post-10.2 shape. THIS is the example the preservation clause is for.
    Scenario("SUCCEEDED", "resolved", "populated_columns", _LIVE),
    # R7.6 on the same-direction write: an already populated pointer is replaced
    # by a resolved one, and the Columns still survive.
    Scenario("SUCCEEDED", "resolved", "populated_pointer", _HYPHENATED),
    # A mirror with no Parameters key at all — Glue does not require one.
    Scenario("SUCCEEDED", "resolved", "no_parameters", _LIVE),
    # ── SUCCEEDED, nothing resolved: R7.4 ──
    # The exact live state today: GetTable answers, with "".
    Scenario("SUCCEEDED", "empty_string", "populated_pointer", _LIVE),
    Scenario("SUCCEEDED", "absent_key", "populated_columns", _LIVE),
    # The deployed IAM state until Unit A lands (design §2, verified implicitDeny).
    Scenario("SUCCEEDED", "raises_access_denied", "empty_columns", _LIVE),
    Scenario("SUCCEEDED", "raises_unexpected", "populated_columns", _HYPHENATED),
    # ── Non-SUCCEEDED: R7.5 ──
    Scenario("FAILED", "resolved", "populated_columns", _LIVE),
    # An exact-match guard, not a lucky one.
    Scenario("succeeded", "resolved", "populated_columns", _LIVE),
    # ── The read itself fails: no write, because there is nothing to merge into ──
    Scenario("SUCCEEDED", "resolved", "read_entity_not_found", _LIVE),
    Scenario("SUCCEEDED", "resolved", "read_access_denied", _HYPHENATED),
)


def _with_critical_examples(test):
    """Attach ``_CRITICAL_SCENARIOS`` as explicit examples, in order."""
    for scenario in reversed(_CRITICAL_SCENARIOS):
        test = example(scenario=scenario)(test)
    return test


#: Every case ``_report_generated_cases`` observed, accumulated across the survey.
#: A module-level accumulator, because Hypothesis's own statistics are per-example
#: and cannot carry a cross-example claim — and because **the property
#: short-circuits.** While the writer does not exist the property fails on its
#: first write-branch example, so Hypothesis stops generating after a handful and
#: its statistics measure nothing. The survey is what keeps the coverage claim
#: measurable during exactly the window in which the property is expected to be red.
_OBSERVED_CASES: dict[str, int] = {}


def _report_generated_cases(scenario: Scenario) -> None:
    """Record which of the cases this property is about the example reached."""

    def event(label: str) -> None:
        _OBSERVED_CASES[label] = _OBSERVED_CASES.get(label, 0) + 1
        _hypothesis_event(label)

    event(f"state={scenario.state!r}")
    event(f"s3tables:GetTable outcome={scenario.outcome}")
    event(f"mirror={scenario.mirror}")

    if scenario.should_write:
        event("the write branch: SUCCEEDED with a resolved non-empty location")
    else:
        event("the no-write branch")

    if scenario.state == "SUCCEEDED":
        event("state is exactly SUCCEEDED")
    elif scenario.state.upper().strip() == "SUCCEEDED":
        event("a state that LOOKS like SUCCEEDED but is not (R7.5's exact match)")
    else:
        event("a plainly non-SUCCEEDED state")

    if scenario.outcome.startswith("raises"):
        event("s3tables:GetTable raised (R7.4 — must not fail the run)")
    if scenario.mirror in _READ_FAILS:
        event("glue:GetTable failed, so there is nothing to merge into")
    if scenario.mirror in ("populated_columns", "populated_pointer"):
        event("the mirror already carries Columns (the preservation clause bites)")
    if scenario.mirror == "populated_pointer":
        event("the mirror already carries a populated metadata_location (R7.6)")
    if scenario.should_write and scenario.mirror == "populated_columns":
        event("a write over populated Columns — the naive implementation's failure")
    if "-" in scenario.identifiers.bucket_name:
        event("a hyphenated bucket name (the database derivation replaces them)")
    if "_" in scenario.identifiers.namespace or "_" in scenario.identifiers.table_name:
        event("an underscored namespace or table name (DD7 axis 2)")


# ---------------------------------------------------------------------------
# The generator survey — measured coverage, asserted rather than hoped for
# ---------------------------------------------------------------------------
# These two run BEFORE the property, in definition order, because the second
# reads what the first accumulated. Both are cheap: no handler, no clients,
# nothing but the generator and the reporter.
@_with_critical_examples
@given(scenario=completion_scenarios())
@settings(max_examples=200, deadline=None, phases=[Phase.explicit, Phase.generate])
def test_the_generator_produces_the_shapes_the_property_needs(scenario):
    """Every scenario, explicit or drawn, is a well-formed combination.

    **Validates: Requirement 8.11**
    """
    _report_generated_cases(scenario)

    assert scenario.outcome in (
        "resolved",
        "empty_string",
        "absent_key",
        "raises_access_denied",
        "raises_not_found",
        "raises_unexpected",
    ), f"unknown s3tables outcome {scenario.outcome!r}"
    assert scenario.should_write == (
        scenario.state == "SUCCEEDED" and scenario.outcome == "resolved" and scenario.mirror not in _READ_FAILS
    ), "should_write must be exactly the requirement's antecedent"
    # The mirror fixture must be constructible for every non-read-failure state,
    # or the property would be asserting preservation of something absent.
    if scenario.mirror not in _READ_FAILS:
        assert "StorageDescriptor" in _existing_table(scenario)


def test_the_generator_reaches_every_case_the_property_is_about():
    """The aggregate half: each named case was reached at least once in 200 draws.

    This is the assertion tasks 3.1 and 4.3 wished they had had — both shipped a
    generator that reached its target case 0 times out of 200 and passed anyway,
    and the first was only caught by someone reading
    ``--hypothesis-show-statistics`` by hand. Here the coverage claim is a test,
    so it fails when the generator drifts away from the cases rather than going
    quiet.

    Every required case is guaranteed by ``_CRITICAL_SCENARIOS`` rather than left
    to the draw, which is what makes this deterministic instead of seed-dependent.

    **Validates: Requirement 8.11**
    """
    assert _OBSERVED_CASES, (
        "no cases recorded — this test reads the survey defined immediately above it, so the "
        "whole module has to run, in definition order"
    )

    required = (
        # Both branches of the property.
        "the write branch: SUCCEEDED with a resolved non-empty location",
        "the no-write branch",
        # Axis 1 — the job state, including the two that only look right.
        "state is exactly SUCCEEDED",
        "a state that LOOKS like SUCCEEDED but is not (R7.5's exact match)",
        "a plainly non-SUCCEEDED state",
        # Axis 2 — every s3tables:GetTable outcome the task names.
        "s3tables:GetTable outcome=resolved",
        "s3tables:GetTable outcome=empty_string",
        "s3tables:GetTable outcome=absent_key",
        "s3tables:GetTable outcome=raises_access_denied",
        "s3tables:GetTable outcome=raises_unexpected",
        "s3tables:GetTable raised (R7.4 — must not fail the run)",
        # Axis 3 — what the mirror holds, and whether it can be read.
        "mirror=empty_columns",
        "mirror=populated_columns",
        "mirror=populated_pointer",
        "mirror=no_parameters",
        "the mirror already carries Columns (the preservation clause bites)",
        "the mirror already carries a populated metadata_location (R7.6)",
        "glue:GetTable failed, so there is nothing to merge into",
        # The single case the naive implementation fails.
        "a write over populated Columns — the naive implementation's failure",
        # The pair derivation.
        "a hyphenated bucket name (the database derivation replaces them)",
        "an underscored namespace or table name (DD7 axis 2)",
    )
    missing = [case for case in required if not _OBSERVED_CASES.get(case)]
    assert not missing, (
        f"the generator reached these cases 0 times, so the property does not test them:\n"
        f"  missing: {missing}\n"
        f"  observed: {dict(sorted(_OBSERVED_CASES.items()))}"
    )


# ---------------------------------------------------------------------------
# Property 6
# ---------------------------------------------------------------------------
@_with_critical_examples
@given(scenario=completion_scenarios())
@settings(
    max_examples=100,
    # Each example builds five MagicMocks and drives the whole handler. The
    # property asserts what was written, not how fast.
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` is excluded deliberately. This property is RED from task
    # 10.1 until tasks 10.2 and 10.3, and on every one of those runs the explain
    # phase re-runs hundreds of further examples to annotate a failure whose
    # cause the assertion message already states verbatim. Every checkpoint in
    # this plan runs the full suite, so that is a standing cost for no
    # information. Shrinking is kept: a minimal counter-example is the thing
    # worth having when this fails for a real reason after 10.3 lands.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_metadata_location_is_written_only_on_succeeded_and_never_empty(scenario):
    """Property 6: written only on ``SUCCEEDED``, and never as an empty string.

    Four clauses. Clause 3 is the one that is red today, and clause 4 is the one
    a call-count assertion would have missed.

    **Validates: Requirements 7.1, 7.3, 7.4, 7.5, 7.6, 7.7, 8.11**
    """
    _report_generated_cases(scenario)
    result, clients = _run_completion(scenario)

    updates = _update_table_calls(clients.glue)
    mutations = _mutating_glue_calls(clients.glue)

    # ── Clause 1: the run is unaffected, whatever happened here (R7.4) ──
    # Asserted first and unconditionally. The Step Functions callback is the
    # run's source of truth and has already been sent by the time this writer
    # runs, so nothing it does may change the run's outcome. `statusCode == 200`
    # rather than "did not raise": the handler's broad `except` converts an
    # escaped exception into a 500, so 500 is how a raise would present.
    assert result["statusCode"] == 200, (
        f"the completion path must not fail the run; state={scenario.state!r} "
        f"outcome={scenario.outcome} mirror={scenario.mirror} → {result}"
    )
    callback_sent = clients.sfn.send_task_success.called or clients.sfn.send_task_failure.called
    assert callback_sent, "the Step Functions callback must have been sent regardless of the mirror write"

    # ── Clause 2: never an empty string, on any branch (R7.1's rule here) ──
    # Universal, not conditional: `LGR-78ce0b7-14` makes a named location a claim
    # that the thing exists, and `""` is a name. A writer that "helpfully" wrote
    # `""` when nothing resolved would satisfy clause 3 and violate the
    # requirement, so this is checked over every recorded write.
    for index, update in enumerate(updates):
        pointer = _written_pointer(update)
        assert pointer != "", (
            f"write {index} carried metadata_location='' — R7.1 requires the key to be OMITTED "
            f"rather than named empty; TableInput={update.get('TableInput')}"
        )

    if not scenario.should_write:
        # ── Clause 3a: no mutation at all, and the registration is unchanged ──
        assert mutations == [], (
            f"no catalog write is permitted for state={scenario.state!r} "
            f"outcome={scenario.outcome} mirror={scenario.mirror} — R7.5 forbids it on a "
            f"non-SUCCEEDED state, R7.4 forbids it when nothing resolved, and a failed "
            f"glue:GetTable leaves nothing to merge into; observed {mutations}"
        )
        return

    # ── Clause 3b: exactly one write, addressed at the derived pair ──
    assert len(updates) == 1, (
        f"SUCCEEDED with a resolved metadataLocation must issue exactly one glue:UpdateTable "
        f"(mirror={scenario.mirror}); observed {len(updates)} update_table calls and "
        f"mutations={mutations}"
    )
    update = updates[0]
    assert update.get("DatabaseName") == scenario.identifiers.database, (
        f"the write must address the mirror database R5.7 derives; expected "
        f"{scenario.identifiers.database!r}, got {update.get('DatabaseName')!r}"
    )
    table_input = update.get("TableInput", {})
    assert table_input.get("Name") == scenario.identifiers.table, (
        f"the write must address the mirror table R5.7 derives; expected "
        f"{scenario.identifiers.table!r}, got {table_input.get('Name')!r}"
    )

    # ── Clause 3c: the resolved pointer landed ──
    assert _written_pointer(update) == RESOLVED_LOCATION, (
        f"the TableInput must carry the resolved metadataLocation; expected "
        f"{RESOLVED_LOCATION!r}, got {_written_pointer(update)!r}"
    )

    # ── Clause 4: preservation. THE clause a naive implementation fails ──
    # Glue UpdateTable REPLACES the stored table with the TableInput given, so a
    # writer that sends Parameters alone erases everything else. Asserted on the
    # argument, never on the count — the count is identical either way.
    storage = table_input.get("StorageDescriptor")
    assert storage is not None, (
        "the TableInput carries no StorageDescriptor: Glue UpdateTable REPLACES rather than "
        "merges, so this write erases the warehouse Location and every column the "
        "registration holds. This is the failure a call-count assertion cannot see."
    )
    assert storage.get("Columns") == _expected_columns(scenario), (
        f"the Columns read immediately before must be carried forward verbatim (R7.7 survives "
        f"this writer only if they are); expected {_expected_columns(scenario)}, got "
        f"{storage.get('Columns')}"
    )
    assert storage.get("Location") == WAREHOUSE_LOCATION, (
        f"the warehouse Location must be preserved from the read; expected "
        f"{WAREHOUSE_LOCATION!r}, got {storage.get('Location')!r}"
    )
    for field in ("InputFormat", "OutputFormat", "SerdeInfo"):
        assert storage.get(field) == _storage_descriptor([]).get(field), (
            f"StorageDescriptor.{field} must be preserved from the read; got {storage.get(field)!r}"
        )

    # The two Parameters the registration owns are not this writer's to drop.
    parameters = table_input.get("Parameters", {})
    assert parameters.get("table_type") == "ICEBERG", (
        f"Parameters.table_type must survive the write; got {parameters.get('table_type')!r}"
    )
    assert parameters.get("classification") == "iceberg", (
        f"Parameters.classification must survive the write; got {parameters.get('classification')!r}"
    )


# ---------------------------------------------------------------------------
# The tripwire, proved armed
# ---------------------------------------------------------------------------
class TestTheTripwireIsArmed:
    """The oracles catch what they exist to catch.

    Clause 3a passes today because no writer exists, so on its own it is
    indistinguishable from an oracle that looks at the wrong object and reports
    nothing. These tests make the calls **themselves** against the same mock the
    property inspects and require each oracle to see them, so the guards are
    known-good before there is any code for them to guard.
    """

    @staticmethod
    def _scenario() -> Scenario:
        return Scenario("SUCCEEDED", "resolved", "populated_columns", _LIVE)

    def test_an_update_table_is_seen_by_both_oracles(self):
        """A write is reported by ``_update_table_calls`` and ``_mutating_glue_calls``."""
        glue = _glue_client(self._scenario())
        glue.update_table(DatabaseName="db", TableInput={"Name": "t", "Parameters": {}})

        assert _mutating_glue_calls(glue) == ["update_table"]
        assert _update_table_calls(glue) == [{"DatabaseName": "db", "TableInput": {"Name": "t", "Parameters": {}}}]

    def test_a_create_table_is_seen_even_though_update_table_is_not_called(self):
        """The absence clause must not be satisfiable by mutating a different way.

        A writer answering a missing mirror by *creating* one leaves
        ``update_table`` at zero. ``_mutating_glue_calls`` is what catches that.
        """
        glue = _glue_client(self._scenario())
        glue.create_table(DatabaseName="db", TableInput={"Name": "t"})

        assert _update_table_calls(glue) == []
        assert _mutating_glue_calls(glue) == ["create_table"]

    def test_the_reads_the_writer_must_make_are_not_reported_as_mutations(self):
        """``get_job_run`` and ``get_table`` are the correct calls, not offences.

        An oracle that flagged the read would make the property unsatisfiable —
        and the read is load-bearing, because ``UpdateTable`` replaces.
        """
        glue = _glue_client(self._scenario())
        glue.get_job_run(JobName=JOB_NAME, RunId=JOB_RUN_ID)
        glue.get_table(DatabaseName=_LIVE.database, Name=_LIVE.table)

        assert _mutating_glue_calls(glue) == []

    def test_the_empty_pointer_oracle_distinguishes_absent_from_empty(self):
        """``_written_pointer`` returns ``None`` for absent and ``""`` for empty.

        R7.1 turns on that distinction: omitting the key is the honest state and
        ``""`` is the dishonest one. An oracle that collapsed them would accept
        the exact value all seven live mirrors carry today.
        """
        assert _written_pointer({"TableInput": {"Parameters": {}}}) is None
        assert _written_pointer({"TableInput": {"Parameters": {"metadata_location": ""}}}) == ""
        assert _written_pointer({"TableInput": {"Parameters": {"metadata_location": RESOLVED_LOCATION}}}) == (
            RESOLVED_LOCATION
        )
        # A TableInput with no Parameters at all is "absent", not "empty".
        assert _written_pointer({"TableInput": {"Name": "t"}}) is None


# ---------------------------------------------------------------------------
# Satisfiability, and the exact asymmetry the task names
# ---------------------------------------------------------------------------
class TestTheSpecificationIsSatisfiable:
    """All four clauses can hold at once, so today's red means "unimplemented".

    Design §5 risk 9: a reviewer who sees red and reverts has undone the only
    thing that would have caught the defect. These tests separate "the subject is
    not written yet" from "the property cannot be satisfied" — the second would
    be a reason to revisit Requirement 7, and it is not the case here.

    ``_populate_read_modify_write`` is a **discrimination probe, not a reference
    implementation.** It exists so the property's oracles can be shown to accept
    the prescribed shape and reject the rejected one. The property itself never
    calls it — it drives ``lambda_handler``. Task 10.3 still owns the real
    writer, with the ARN resolution, the ``ClientError`` / ``Exception`` split,
    the logging and the guards, none of which is here.
    """

    @staticmethod
    def _resolve(scenario: Scenario, glue: MagicMock, s3tables: MagicMock) -> str:
        """The metadataLocation, or ``""`` — tolerating every axis-2 outcome."""
        try:
            return s3tables.get_table(
                tableBucketARN=_bucket_arn(scenario.identifiers),
                namespace=scenario.identifiers.namespace,
                name=scenario.identifiers.table_name,
            ).get("metadataLocation", "")
        except Exception:
            return ""

    @classmethod
    def _populate_read_modify_write(cls, scenario: Scenario, glue: MagicMock, s3tables: MagicMock) -> None:
        """Design §4.3's prescribed shape: guard, resolve, read, merge, write."""
        if scenario.state != "SUCCEEDED":
            return
        resolved = cls._resolve(scenario, glue, s3tables)
        if not resolved:
            return
        try:
            stored = glue.get_table(DatabaseName=scenario.identifiers.database, Name=scenario.identifiers.table)[
                "Table"
            ]
        except Exception:
            # Nothing to merge into. Writing anyway is the clobber the read
            # exists to prevent.
            return
        table_input = {
            "Name": stored["Name"],
            "Description": stored.get("Description", ""),
            "TableType": stored.get("TableType", "EXTERNAL_TABLE"),
            "Parameters": {**stored.get("Parameters", {}), "metadata_location": resolved},
            "StorageDescriptor": stored.get("StorageDescriptor", {}),
        }
        table_input["Parameters"].setdefault("table_type", "ICEBERG")
        table_input["Parameters"].setdefault("classification", "iceberg")
        glue.update_table(DatabaseName=scenario.identifiers.database, TableInput=table_input)

    @classmethod
    def _populate_parameters_only(cls, scenario: Scenario, glue: MagicMock, s3tables: MagicMock) -> None:
        """The naive shape: correct-looking, and it erases the Columns.

        This is what ``create_infrastructure``'s refresh branch does today
        (``:322-339``) minus the StorageDescriptor — the obvious implementation
        of "write metadata_location only", and the reason design §4.1 has to say
        ``glue:GetTable`` is not redundant.
        """
        if scenario.state != "SUCCEEDED":
            return
        resolved = cls._resolve(scenario, glue, s3tables)
        if not resolved:
            return
        glue.update_table(
            DatabaseName=scenario.identifiers.database,
            TableInput={
                "Name": scenario.identifiers.table,
                "Parameters": {
                    "table_type": "ICEBERG",
                    "classification": "iceberg",
                    "metadata_location": resolved,
                },
            },
        )

    @classmethod
    def _populate_writing_empty_string(cls, scenario: Scenario, glue: MagicMock, s3tables: MagicMock) -> None:
        """The shape ``LGR-78ce0b7-14`` rejects: name the location ``""``."""
        if scenario.state != "SUCCEEDED":
            return
        resolved = cls._resolve(scenario, glue, s3tables)
        stored = glue.get_table(DatabaseName=scenario.identifiers.database, Name=scenario.identifiers.table)["Table"]
        glue.update_table(
            DatabaseName=scenario.identifiers.database,
            TableInput={
                "Name": stored["Name"],
                "Parameters": {**stored.get("Parameters", {}), "metadata_location": resolved},
                "StorageDescriptor": stored.get("StorageDescriptor", {}),
            },
        )

    @staticmethod
    def _probe(writer, scenario: Scenario) -> tuple[MagicMock, list[dict[str, Any]]]:
        """Run one probe against the property's own fixtures."""
        glue = _glue_client(scenario)
        s3tables = _s3tables_client(scenario)
        writer(scenario, glue, s3tables)
        return glue, _update_table_calls(glue)

    @pytest.mark.parametrize(
        "scenario", _CRITICAL_SCENARIOS, ids=lambda s: f"{s.state or 'empty'}-{s.outcome}-{s.mirror}"
    )
    def test_the_prescribed_shape_satisfies_every_clause(self, scenario):
        """The read-modify-write passes all four clauses on all twelve cases.

        Run over ``_CRITICAL_SCENARIOS`` rather than one hand-picked case, so
        satisfiability is demonstrated across both branches of the property and
        every mirror state — including the two where the read fails.
        """
        glue, updates = self._probe(self._populate_read_modify_write, scenario)
        mutations = _mutating_glue_calls(glue)

        if not scenario.should_write:
            assert mutations == [], f"the probe must write nothing for {scenario}; observed {mutations}"
            return

        assert len(updates) == 1, f"the probe must write exactly once for {scenario}"
        update = updates[0]
        assert update["DatabaseName"] == scenario.identifiers.database
        assert update["TableInput"]["Name"] == scenario.identifiers.table
        assert _written_pointer(update) == RESOLVED_LOCATION
        assert update["TableInput"]["StorageDescriptor"]["Columns"] == _expected_columns(scenario)
        assert update["TableInput"]["StorageDescriptor"]["Location"] == WAREHOUSE_LOCATION
        assert update["TableInput"]["Parameters"]["table_type"] == "ICEBERG"
        assert update["TableInput"]["Parameters"]["classification"] == "iceberg"

    def test_the_naive_parameters_only_write_passes_the_count_and_fails_the_argument(self):
        """The task's central claim, demonstrated rather than asserted in prose.

        "Assert on the ``TableInput`` argument, not on the call count." Here is
        why: over a mirror carrying four columns, the naive writer produces
        **exactly one** ``glue:UpdateTable`` with the **correct**
        ``metadata_location``. Every count-shaped and pointer-shaped assertion
        passes. The only thing that catches it is the ``StorageDescriptor``,
        which it does not send — and which Glue therefore erases along with the
        four columns Requirement 7.7 exists to put there.
        """
        scenario = Scenario("SUCCEEDED", "resolved", "populated_columns", _LIVE)
        _glue, updates = self._probe(self._populate_parameters_only, scenario)

        # What a count-based property would have checked, and it all passes.
        assert len(updates) == 1
        assert _written_pointer(updates[0]) == RESOLVED_LOCATION
        assert updates[0]["TableInput"]["Name"] == scenario.identifiers.table

        # And what the property actually checks.
        assert "StorageDescriptor" not in updates[0]["TableInput"], (
            "this probe exists to omit the StorageDescriptor; if it stops omitting it, the "
            "asymmetry this test demonstrates is no longer demonstrated"
        )
        assert _expected_columns(scenario) == POPULATED_COLUMNS, (
            "the mirror must carry columns for the erasure to be observable"
        )

    def test_the_empty_string_write_is_caught_by_the_never_empty_clause(self):
        """``LGR-78ce0b7-14``'s rejected shape fails clause 2, on its own.

        The writer that "helpfully" writes whatever it resolved, empty included,
        preserves the StorageDescriptor perfectly and writes exactly once. Clause
        2 is the only thing between it and the state all seven live mirrors are
        in today.
        """
        scenario = Scenario("SUCCEEDED", "empty_string", "populated_pointer", _LIVE)
        _glue, updates = self._probe(self._populate_writing_empty_string, scenario)

        assert len(updates) == 1, "the rejected shape writes, which is the problem"
        assert updates[0]["TableInput"]["StorageDescriptor"]["Columns"] == POPULATED_COLUMNS, (
            "it preserves the columns, so clause 4 does not catch it"
        )
        assert _written_pointer(updates[0]) == "", (
            "clause 2 is what catches it: a named location is a claim the thing exists"
        )
        # And R7.6's damage: the pointer an earlier successful run wrote is gone.
        assert _written_pointer(updates[0]) != PREVIOUSLY_WRITTEN_LOCATION, (
            "the previously populated pointer was replaced by an empty one — R7.6's violation"
        )


# ---------------------------------------------------------------------------
# Example tests — the job_completion side of R8.11's "both sides"
# ---------------------------------------------------------------------------
def test_the_succeeded_path_populates_the_pointer_while_preserving_columns():
    """R7.3 and R7.7 together, on one run, against the handler.

    The named example the task asks for. It is the property's write branch
    reduced to a single readable case, so a failure here reads as "the SUCCEEDED
    path did not populate the pointer" rather than as a shrunk counter-example.

    **Expected RED until tasks 10.2 and 10.3.**

    **Validates: Requirements 7.3, 7.7, 8.11**
    """
    scenario = Scenario("SUCCEEDED", "resolved", "populated_columns", _LIVE)
    result, clients = _run_completion(scenario)

    assert result["statusCode"] == 200
    updates = _update_table_calls(clients.glue)
    assert len(updates) == 1, (
        f"a SUCCEEDED run with a resolvable metadataLocation must write it into the mirror "
        f"(R7.3); observed {len(updates)} glue:UpdateTable calls"
    )
    table_input = updates[0]["TableInput"]
    assert _written_pointer(updates[0]) == RESOLVED_LOCATION
    assert table_input["StorageDescriptor"]["Columns"] == POPULATED_COLUMNS, (
        "R7.7's columns must survive R7.3's write — Glue UpdateTable replaces the stored table"
    )


def test_a_non_succeeded_run_writes_nothing_into_the_mirror():
    """R7.5, as its own readable case.

    Passes today (there is no writer at all), and it is the clause that stops the
    property being satisfiable by a writer with no guard.

    **Validates: Requirement 7.5**
    """
    for state in ("FAILED", "STOPPED", "TIMEOUT"):
        scenario = Scenario(state, "resolved", "populated_columns", _LIVE)
        result, clients = _run_completion(scenario)

        assert result["statusCode"] == 200
        assert _mutating_glue_calls(clients.glue) == [], (
            f"state={state} must leave the registration untouched, even with a resolvable metadataLocation in hand"
        )


def test_an_unresolvable_location_on_success_leaves_the_registration_unchanged():
    """R7.4: log the omission, leave the existing state alone, do not fail.

    Both sub-cases the account can actually be in: ``s3tables:GetTable``
    answering with ``""`` (the structural state Phase 1 established — the Iceberg
    table has no committed metadata yet) and ``s3tables:GetTable`` being denied
    (the deployed IAM state until Unit A's Foundation deploy lands, verified
    ``implicitDeny`` in design §2).

    **Validates: Requirements 7.4, 7.6**
    """
    for outcome in ("empty_string", "absent_key", "raises_access_denied", "raises_unexpected"):
        scenario = Scenario("SUCCEEDED", outcome, "populated_pointer", _LIVE)
        result, clients = _run_completion(scenario)

        assert result["statusCode"] == 200, f"outcome={outcome} must not fail the run (R7.4): {result}"
        assert _mutating_glue_calls(clients.glue) == [], (
            f"outcome={outcome} must leave the registration's existing state unchanged — including "
            f"the metadata_location an earlier successful run wrote (R7.6)"
        )


def test_the_callback_is_sent_before_any_mirror_write():
    """R7.4's structural half: the run's outcome precedes the bookkeeping.

    The same assertion ``test_callback_is_sent_before_the_version_commit`` makes
    for the ``target_schema_version`` commit, and for the same reason —
    ``LGR-eee8ac8-4`` (**template / ratified**) put that write on this callback
    because terminal write success is knowable here and nowhere upstream. A
    mirror write that ran *before* the callback could delay or fail the run it is
    only describing.

    **Expected RED until task 10.3** — the second element never appears.

    **Validates: Requirement 7.4**
    """
    scenario = Scenario("SUCCEEDED", "resolved", "populated_columns", _LIVE)
    order: list[str] = []

    glue = _glue_client(scenario)
    s3tables = _s3tables_client(scenario)
    pipelines = _pipelines_table(scenario)
    runs = _runs_table()
    sfn = MagicMock(name="sfn")
    sfn.send_task_success.side_effect = lambda **kwargs: order.append("callback")
    glue.update_table.side_effect = lambda **kwargs: order.append("mirror_write") or {}

    boto3_stub = MagicMock(name="boto3")
    boto3_stub.client.side_effect = lambda service, *a, **k: {"glue": glue, "s3tables": s3tables}.get(
        service, MagicMock(name=f"{service}_client")
    )

    with (
        patch.object(_mod, "pipeline_runs_table", runs),
        patch.object(_mod, "task_tokens_table", _task_tokens_table()),
        patch.object(_mod, "pipelines_table", pipelines),
        patch.object(_mod, "glue_client", glue),
        patch.object(_mod, "sfn_client", sfn),
        patch.object(_mod, "lambda_client", MagicMock(name="lambda")),
        patch.object(_mod, "s3tables_client", s3tables, create=True),
        patch.object(_mod, "boto3", boto3_stub),
        patch.dict(os.environ, {"SELF_HEALING_FN": ""}, clear=False),
    ):
        _mod.lambda_handler({"detail": {"jobName": JOB_NAME, "jobRunId": JOB_RUN_ID, "state": "SUCCEEDED"}}, None)

    assert order == ["callback", "mirror_write"], (
        f"the mirror write must follow the Step Functions callback, so a failure in it can never "
        f"affect the run's outcome; observed {order}"
    )
