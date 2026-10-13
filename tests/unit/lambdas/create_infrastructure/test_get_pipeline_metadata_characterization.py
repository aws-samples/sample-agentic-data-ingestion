# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: ``create_infrastructure.get_pipeline_metadata`` (p47 task E1).

``lambda/agentic_data_ingestion_create_infrastructure/lambda_function.py:78`` is
one of five near-copies of the pipeline-record read that p47 task E2 replaces
with a shared primitive parameterised by its not-found sentinel (R12.4). This
file exists so E2 has a safety net at the **one site of the five whose read does
more than return a value**: it derives ``target_config`` from the target schema
in S3 (``:100-130``) when the record does not carry one.

**Not written from scratch — this is the gap half of an inventory.** Task E1's
first job was to locate what already exists, and most of this function is
already pinned by the sibling ``test_read_path_errors.py``: the throttle →
``TransientStepFailure`` classification, ``AccessDeniedException`` →
``StepFailure``, the missing-``Item`` → ``{}`` sentinel, and that the derivation
swallow is best-effort. **Those four assertions are deliberately not repeated
here.** R12.7: writing a sixth copy of an existing assertion is not progress.
The full inventory, including the seven candidate tests that were considered and
declined, is in
``.kiro/specs/p47-operative-gaps-and-hot-path-efficiency/e1-read-path-coverage-inventory.md``.

**The gap that motivated the file.** The sibling's
``test_target_config_derivation_stays_best_effort`` asserts that after an S3
failure the metadata still comes back and carries no ``target_config``. That
test **cannot detect deletion of the derivation at all**: delete ``:100-130``
outright and it still passes, because the S3 stub is simply never called and
``target_config`` is still absent. So before this file, nothing asserted that
``target_config`` is *ever* derived, or what it contains when it is. E2 could
drop or relocate the side effect and every existing test in this directory would
stay green.

**codegraph reports ``no covering tests`` for this symbol. It is wrong, and the
mechanism is worth recording.** The sibling test loads the Lambda through
``importlib.util.spec_from_file_location`` under a private module name and calls
``_lf.get_pipeline_metadata(...)``, so there is no static import edge to follow.
This file does the same, under its own private name so the two loads cannot
clobber each other. Treat a codegraph coverage flag as a prompt to check disk,
never as a finding.

**Two modes, marked per test.** Most tests here are *characterization* — they
pin today's behaviour so E2 cannot drift it, and they are the correct oracle for
that job. Two are *requirement-derived*:

- ``TestHalfWrittenRecordIsToleratedAtProvisioningTime`` is derived from **R12.3
  / do-NOT 5**. ``LGR-b2f79ef-5`` (template / ratified) excluded this file from
  convergence with its scope field saying verbatim "explicitly NOT
  ``…_create_infrastructure/**``", because its sites are "provisioning-time
  reads that tolerate absence by design (``if bucket and key:`` at ``:87``)" —
  that guard, now at ``:107``. Nothing asserted the tolerance, so do-NOT 5 was
  enforced only by intent. These tests make it enforced by test.
- ``test_a_non_client_error_is_not_degraded_into_the_sentinel`` is derived from
  **R12.4** (callers keep their contracts). A shared mechanism must not widen
  this site's ``except ClientError``. It matters more here than elsewhere:
  ``determine_infrastructure_approval_requirement`` (``:606-633``) wraps its
  call in a blanket ``except Exception`` that returns ``False``, so a widened
  inner ``except`` would turn an infrastructure error into "no approval
  required" with nothing in the classification to show it.

Sentinel note: this site's not-found value is ``{}``, **not** ``None``, and that
difference from ``test_code`` is deliberate and ratified — ``LGR-b2f79ef-8``
governs ``test_code``'s ``None`` only. E2 shares the *mechanism*, not the
contract (R12.4). Do not normalise the two.

**Validates: Requirements 12.7** (and pins the surface R12.3, R12.4 protect)
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest
from botocore.exceptions import ClientError

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_create_infrastructure",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

# `tests/unit/conftest.py` pins AWS_REGION / AWS_DEFAULT_REGION by assignment
# (`LGR-3e45e65-1`, template/ratified), so these setdefault calls are
# deliberate no-ops kept for parity with the sibling file rather than a way this
# module chooses its own region.
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
    "artifact_schema": mock.MagicMock(create_output_data=mock.MagicMock(return_value={})),
}


def _load_module():
    """Load the create_infrastructure lambda_function by path (sibling-test pattern).

    The private module name differs from ``test_read_path_errors.py``'s so the
    two files hold independent module objects. Finding 20 in ``findings.md``
    records what goes wrong when a test inherits whatever collection order left
    in ``sys.modules``; a path load under a unique name avoids that entirely.
    """
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "create_infrastructure_lf_p47_metadata_char",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["create_infrastructure_lf_p47_metadata_char"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()


def _client_error(code: str, operation: str = "GetItem") -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, operation)


PIPELINE_ID = "pipe-p47-e1"
TARGET_BUCKET = "adi-target-schemas"
TARGET_KEY = "schemas/target.json"

#: A record carrying the bucket/key pair the derivation needs, and no
#: ``target_config`` — the shape that makes the derivation fire.
RECORD_NEEDING_DERIVATION = {
    "pipeline_id": PIPELINE_ID,
    "pipeline_name": "orders-daily",
    "target_schema_bucket": TARGET_BUCKET,
    "target_schema_s3_key": TARGET_KEY,
}


def _table_returning(response: dict) -> mock.MagicMock:
    table = mock.MagicMock()
    table.get_item.return_value = response
    return table


def _table_raising(exc: BaseException) -> mock.MagicMock:
    table = mock.MagicMock()
    table.get_item.side_effect = exc
    return table


def _s3_returning(body: bytes) -> mock.MagicMock:
    s3 = mock.MagicMock()
    reader = mock.MagicMock()
    reader.read.return_value = body
    s3.get_object.return_value = {"Body": reader}
    return s3


class TestDerivationWritesTheConfigTheSchemaNames:
    """CHARACTERIZATION. The success side effect, previously unpinned entirely.

    The sibling ``test_read_path_errors.py`` covers only the derivation's
    *failure* path, and that test passes even if ``:100-130`` is deleted. These
    tests pin what the block produces when it succeeds, so E2 cannot drop or
    relocate the side effect silently. R12.4 leaves this derivation as a
    caller-side concern; leaving it caller-side is only safe if it is asserted.
    """

    def test_the_derived_config_carries_the_target_type_from_the_schema(self):
        s3 = _s3_returning(b'{"metadata": {"target_type": "S3_TABLES"}}')
        with (
            mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": dict(RECORD_NEEDING_DERIVATION)})),
            mock.patch.object(_lf.boto3, "client", return_value=s3),
        ):
            metadata = _lf.get_pipeline_metadata(PIPELINE_ID)

        assert metadata["target_config"] == {"type": "S3_TABLES", "derived": True}

    def test_the_derived_flag_marks_the_config_as_inferred_not_authored(self):
        """``derived: True`` is the only signal downstream has that the config was guessed."""
        s3 = _s3_returning(b'{"metadata": {"target_type": "GLUE_CATALOG"}}')
        with (
            mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": dict(RECORD_NEEDING_DERIVATION)})),
            mock.patch.object(_lf.boto3, "client", return_value=s3),
        ):
            metadata = _lf.get_pipeline_metadata(PIPELINE_ID)

        assert metadata["target_config"]["derived"] is True

    def test_the_schema_object_is_read_from_both_attributes_of_the_same_record(self):
        """The bucket and the key come from one record, resolved together.

        The same pairing ``LGR-7c9378d-1`` ratified one level down: a key
        without the bucket it was written beside addresses nothing.
        """
        s3 = _s3_returning(b'{"metadata": {"target_type": "S3_PARQUET"}}')
        with (
            mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": dict(RECORD_NEEDING_DERIVATION)})),
            mock.patch.object(_lf.boto3, "client", return_value=s3),
        ):
            _lf.get_pipeline_metadata(PIPELINE_ID)

        s3.get_object.assert_called_once_with(Bucket=TARGET_BUCKET, Key=TARGET_KEY)

    def test_a_schema_naming_no_target_type_derives_the_unknown_placeholder(self):
        """``"UNKNOWN"`` is a real value downstream reads, not an error path."""
        s3 = _s3_returning(b"{}")
        with (
            mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": dict(RECORD_NEEDING_DERIVATION)})),
            mock.patch.object(_lf.boto3, "client", return_value=s3),
        ):
            metadata = _lf.get_pipeline_metadata(PIPELINE_ID)

        assert metadata["target_config"] == {"type": "UNKNOWN", "derived": True}

    def test_an_existing_target_config_is_left_alone_and_no_object_is_read(self):
        """The derivation is backward-compatibility only — an authored config wins.

        Asserting that no S3 call happens is the half that matters for E2: the
        read must stay free for the records that already carry their config.
        """
        authored = {"type": "JDBC", "connection_name": "warehouse"}
        record = dict(RECORD_NEEDING_DERIVATION, target_config=authored)
        s3 = _s3_returning(b'{"metadata": {"target_type": "S3_TABLES"}}')
        with (
            mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": record})),
            mock.patch.object(_lf.boto3, "client", return_value=s3) as client,
        ):
            metadata = _lf.get_pipeline_metadata(PIPELINE_ID)

        assert metadata["target_config"] == authored
        client.assert_not_called()
        s3.get_object.assert_not_called()


class TestHalfWrittenRecordIsToleratedAtProvisioningTime:
    """REQUIREMENT-DERIVED from R12.3 / do-NOT 5 (``LGR-b2f79ef-5``).

    That record excluded this file from convergence, verbatim: "explicitly NOT
    ``agentic_data_ingestion/lambda/agentic_data_ingestion_create_infrastructure/**``",
    because these are "provisioning-time reads that tolerate absence by design
    (``if bucket and key:`` at ``:87``)" — the guard now at ``:107``. Its
    ``revisit_when`` releases the deferral only when this Lambda "gains
    requirements about half-written records at provisioning time". p47 states no
    such requirement, so the tolerance must survive E2 unchanged.

    Before these tests the guard was unasserted, so do-NOT 5 was enforced by
    intention only. A converging change would have passed every test in this
    directory.
    """

    @pytest.mark.parametrize(
        ("record", "case"),
        [
            ({"pipeline_id": PIPELINE_ID, "target_schema_bucket": TARGET_BUCKET}, "bucket without key"),
            ({"pipeline_id": PIPELINE_ID, "target_schema_s3_key": TARGET_KEY}, "key without bucket"),
            ({"pipeline_id": PIPELINE_ID}, "neither attribute"),
            (
                {"pipeline_id": PIPELINE_ID, "target_schema_bucket": "", "target_schema_s3_key": TARGET_KEY},
                "empty bucket string",
            ),
        ],
    )
    def test_no_object_is_read_and_nothing_raises(self, record, case):
        s3 = mock.MagicMock()
        with (
            mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": dict(record)})),
            mock.patch.object(_lf.boto3, "client", return_value=s3) as client,
        ):
            metadata = _lf.get_pipeline_metadata(PIPELINE_ID)

        assert metadata["pipeline_id"] == PIPELINE_ID, case
        assert "target_config" not in metadata, case
        client.assert_not_called()
        s3.get_object.assert_not_called()

    def test_a_half_written_record_is_not_the_not_found_sentinel(self):
        """Absence of a target schema is not absence of a record.

        The distinction ``LGR-b2f79ef-8`` drew for ``test_code`` applies here in
        the weaker form this site's ``{}`` sentinel permits: a record that
        exists always carries its key attribute, so it is never ``{}``, and a
        caller can still tell "no record" from "record without a schema".
        """
        with mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": {"pipeline_id": PIPELINE_ID}})):
            present = _lf.get_pipeline_metadata(PIPELINE_ID)
        with mock.patch.object(_lf, "pipelines_table", _table_returning({})):
            absent = _lf.get_pipeline_metadata(PIPELINE_ID)

        assert present != {}
        assert absent == {}


class TestTheReadItselfIsUnchangedByTheDerivation:
    """CHARACTERIZATION. The plain read contract E2 replaces with the primitive."""

    def test_a_record_carrying_its_own_config_is_returned_intact(self):
        record = dict(RECORD_NEEDING_DERIVATION, target_config={"type": "JDBC"}, extra="kept")
        with mock.patch.object(_lf, "pipelines_table", _table_returning({"Item": record})):
            assert _lf.get_pipeline_metadata(PIPELINE_ID) == record

    def test_the_record_is_read_by_its_pipeline_id_key(self):
        """Every existing test in this directory stubs ``get_item``, so a wrong
        ``Key`` would return the stub and pass. Pinned here because this site's
        record drives a side effect, which makes a wrong key doubly wrong.

        If E2 moves ``Key`` construction into the shared primitive, this
        assertion covers only this caller — see the inventory's §3 note.
        """
        table = _table_returning({"Item": dict(RECORD_NEEDING_DERIVATION, target_config={"type": "JDBC"})})
        with mock.patch.object(_lf, "pipelines_table", table):
            _lf.get_pipeline_metadata(PIPELINE_ID)

        table.get_item.assert_called_once_with(Key={"pipeline_id": PIPELINE_ID})


class TestNotFoundAndUnclassifiedErrorsAtTheReadBoundary:
    """The two boundaries of ``raise_for_read_client_error`` this site did not pin.

    The transient and permanent branches are already pinned by the sibling
    ``test_read_path_errors.py`` and are not repeated (R12.7). What was missing
    is the *not-found* branch — where the classifier returns normally and the
    read falls through to its own sentinel — and the case of an exception the
    ``except`` clause does not catch at all.
    """

    def test_a_not_found_client_error_returns_the_sentinel_and_never_derives(self):
        """CHARACTERIZATION. ``ResourceNotFoundException`` is in
        ``NOT_FOUND_CLIENT_ERROR_CODES`` (``shared/step_errors.py:54-62``), so
        the classifier returns and this read returns ``{}``.

        Distinct from every other site's version of this assertion, which is why
        it is written here and declined for ``schema_compare`` /
        ``schema_mapping``: returning ``{}`` from the ``except`` branch also
        means the derivation is never reached, so there is a second observable
        consequence no other site has.
        """
        s3 = mock.MagicMock()
        with (
            mock.patch.object(_lf, "pipelines_table", _table_raising(_client_error("ResourceNotFoundException"))),
            mock.patch.object(_lf.boto3, "client", return_value=s3) as client,
        ):
            assert _lf.get_pipeline_metadata(PIPELINE_ID) == {}

        client.assert_not_called()

    def test_a_non_client_error_is_not_degraded_into_the_sentinel(self):
        """REQUIREMENT-DERIVED from R12.4 — callers keep their contracts.

        The ``except`` at ``:89`` catches ``ClientError`` only, so anything else
        propagates. This is the property ``LGR-b2f79ef-13`` (template /
        ratified) was written about at the analogous ``schema_mapping`` site,
        where a blanket ``except Exception`` re-labelled an already-classified
        failure as a bare ``ValueError``.

        It matters more here than at the other four sites:
        ``determine_infrastructure_approval_requirement`` (``:606-633``) wraps
        its call in ``except Exception`` and returns ``False``, so a widened
        inner ``except`` would present an infrastructure error as "no approval
        required" and nothing in the classification would show it.
        """
        with (
            mock.patch.object(_lf, "pipelines_table", _table_raising(RuntimeError("connection reset"))),
            pytest.raises(RuntimeError, match="connection reset"),
        ):
            _lf.get_pipeline_metadata(PIPELINE_ID)
