# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ``glue:UpdateTable`` ``TableInput`` allowlist — one definition, pinned.

Glue ``UpdateTable`` **replaces** the stored table with the ``TableInput``
given, so the only correct writer is a read-modify-write. The read returns
fields ``UpdateTable`` rejects inside a ``TableInput``, so the projection has to
strip them. p35 gives that projection two callers on the **same** mirror table —
``create_infrastructure``'s registration refresh (R7.6/R7.7) and
``job_completion``'s ``metadata_location`` writer (R7.3) — so it lives once in
``shared/glue_table_input.py`` rather than twice.

What these tests pin, and why each one is worth a test rather than a comment:

1. **Every read-only field a real ``glue:GetTable`` returns is dropped.** The
   fixture below is the shape p35's property test uses for the live
   ``testingbucket`` mirror, so this is asserted against what the API actually
   sends rather than against a list of names invented here.
2. **Absent keys stay absent** rather than arriving as ``None``. ``UpdateTable``
   reads a present key as a value to store, so a ``None`` ``StorageDescriptor``
   is a request to *erase* it — the exact clobber the read exists to prevent.
3. **The projection is a new dict and does not mutate the read**, because both
   callers modify ``Parameters`` after projecting.
4. **The two callers agree.** Asserted against both Lambdas' **source**, so a
   future copy re-inlined in one of them fails here instead of silently
   producing two different tables from one read (``LGR-eee8ac8-5``'s reason for
   putting the processed-file fingerprint in ``shared/``: a second definition
   disables the shared behaviour silently rather than loudly).

**Validates: Requirements 7.3, 7.6, 7.7**
"""

import os
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.path.join(str(PROJECT_ROOT), "shared"))

from glue_table_input import GLUE_TABLE_INPUT_KEYS, glue_table_input_from_stored

_LAMBDA_DIR = PROJECT_ROOT / "lambda"
_CREATE_INFRASTRUCTURE_SOURCE = _LAMBDA_DIR / "agentic_data_ingestion_create_infrastructure" / "lambda_function.py"
_JOB_COMPLETION_SOURCE = _LAMBDA_DIR / "agentic_data_ingestion_job_completion" / "lambda_function.py"

#: The fields a real ``glue:GetTable`` adds around a ``TableInput`` and which
#: ``glue:UpdateTable`` rejects inside one. Handing the read ``Table`` straight
#: back fails live; these are what has to come off.
_READ_ONLY_FIELDS = (
    "DatabaseName",
    "CreateTime",
    "UpdateTime",
    "CreatedBy",
    "VersionId",
    "CatalogId",
    "IsRegisteredWithLakeFormation",
)


def _stored_mirror() -> dict[str, Any]:
    """A ``glue:GetTable`` ``Table`` for the live ``testingbucket`` mirror."""
    return {
        "Name": "testingbucketns_testingbuckettn",
        "DatabaseName": "agentic_data_ingestion_testingbucket",
        "Description": "S3 Tables Iceberg table: testingbucketns.testingbuckettn",
        "TableType": "EXTERNAL_TABLE",
        "CreateTime": "2026-08-06T12:31:12+00:00",
        "UpdateTime": "2026-08-06T12:53:13+00:00",
        "CreatedBy": "arn:aws:sts::777788889999:assumed-role/agentic-data-ingestion-dev-infrastructure-role/x",
        "IsRegisteredWithLakeFormation": False,
        "CatalogId": "777788889999",
        "VersionId": "1",
        "Parameters": {"table_type": "ICEBERG", "classification": "iceberg", "metadata_location": ""},
        "StorageDescriptor": {
            "Location": "s3://8fdb0e0f--table-s3",
            "InputFormat": "org.apache.iceberg.mr.hive.HiveIcebergInputFormat",
            "OutputFormat": "org.apache.iceberg.mr.hive.HiveIcebergOutputFormat",
            "SerdeInfo": {"SerializationLibrary": "org.apache.iceberg.mr.hive.HiveIcebergSerDe"},
            "Columns": [{"Name": "customer_id", "Type": "int"}],
        },
    }


class TestTheProjection:
    """What survives a read, and what does not."""

    def test_every_read_only_field_is_dropped(self):
        """The seven fields ``UpdateTable`` rejects are not in the result."""
        table_input = glue_table_input_from_stored(_stored_mirror())

        present = [field for field in _READ_ONLY_FIELDS if field in table_input]
        assert present == [], f"these fields would be rejected by glue:UpdateTable inside a TableInput: {present}"

    def test_the_writable_fields_survive_verbatim(self):
        """``Name``, ``Description``, ``TableType``, ``Parameters`` and the
        ``StorageDescriptor`` are carried through unchanged.

        The ``StorageDescriptor`` is the load-bearing one: it holds the
        warehouse ``Location`` and the ``Columns`` p35 R7.7 maintains, and
        ``UpdateTable`` erases whatever the ``TableInput`` does not name.
        """
        stored = _stored_mirror()
        table_input = glue_table_input_from_stored(stored)

        assert table_input["Name"] == stored["Name"]
        assert table_input["Description"] == stored["Description"]
        assert table_input["TableType"] == stored["TableType"]
        assert table_input["Parameters"] == stored["Parameters"]
        assert table_input["StorageDescriptor"] == stored["StorageDescriptor"]

    def test_an_absent_key_stays_absent_rather_than_becoming_none(self):
        """A mirror with no ``Parameters`` projects to a ``TableInput`` with none.

        ``UpdateTable`` reads a present key as a value to store, so
        ``{"StorageDescriptor": None}`` is a request to erase the storage
        descriptor — worse than omitting it, and the opposite of what a
        read-modify-write is for.
        """
        stored = _stored_mirror()
        del stored["Parameters"]
        del stored["StorageDescriptor"]

        table_input = glue_table_input_from_stored(stored)

        assert "Parameters" not in table_input
        assert "StorageDescriptor" not in table_input

    def test_an_empty_read_projects_to_an_empty_table_input(self):
        """Nothing read, nothing carried — and no exception."""
        assert glue_table_input_from_stored({}) == {}

    def test_the_projection_does_not_mutate_the_read(self):
        """Both callers modify ``Parameters`` after projecting, so the result
        must be a new mapping at the top level."""
        stored = _stored_mirror()
        before = _stored_mirror()

        table_input = glue_table_input_from_stored(stored)
        table_input["Name"] = "something_else"
        table_input["Parameters"] = {"replaced": "yes"}

        assert stored == before, "the stored table read from Glue was modified by the projection"

    def test_an_unknown_field_is_not_carried_forward(self):
        """A field Glue adds later is dropped rather than passed through.

        This is why the module is an allowlist: a denylist would forward the new
        field and the write would start failing for a reason no test names.
        """
        stored = _stored_mirror()
        stored["SomeFutureReadOnlyField"] = "x"

        assert "SomeFutureReadOnlyField" not in glue_table_input_from_stored(stored)


class TestTheKeyList:
    """The allowlist itself."""

    def test_it_carries_the_writable_members_and_none_of_the_read_only_ones(self):
        overlap = set(GLUE_TABLE_INPUT_KEYS) & set(_READ_ONLY_FIELDS)
        assert overlap == set(), f"read-only fields must not be in the allowlist: {sorted(overlap)}"
        for expected in ("Name", "Description", "TableType", "Parameters", "StorageDescriptor", "PartitionKeys"):
            assert expected in GLUE_TABLE_INPUT_KEYS

    def test_it_has_no_duplicates(self):
        assert len(GLUE_TABLE_INPUT_KEYS) == len(set(GLUE_TABLE_INPUT_KEYS))


class TestBothCallersUseTheSharedDefinition:
    """One definition, checked against both Lambdas' source.

    Two allowlists that disagree produce two different tables from one read of
    the same mirror, and Glue accepts both — the drift is silent. These
    assertions are what make "one definition" a fact rather than an intention.
    """

    def test_create_infrastructure_imports_it_and_does_not_re_inline_it(self):
        source = _CREATE_INFRASTRUCTURE_SOURCE.read_text()

        assert "from glue_table_input import glue_table_input_from_stored" in source
        assert "_GLUE_TABLE_INPUT_KEYS" not in source, (
            "create_infrastructure has re-inlined the allowlist; the two writers on this mirror "
            "can now disagree about which keys survive a write"
        )

    def test_job_completion_imports_it_and_does_not_re_inline_it(self):
        source = _JOB_COMPLETION_SOURCE.read_text()

        assert "from glue_table_input import glue_table_input_from_stored" in source
        assert "_GLUE_TABLE_INPUT_KEYS" not in source, (
            "job_completion has re-inlined the allowlist; the two writers on this mirror can now "
            "disagree about which keys survive a write"
        )
