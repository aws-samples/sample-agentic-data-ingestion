# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Create-time validation of JDBC targets in EVERY connection mode (p31 task 4.2).

Before this change, ``create_pipeline``'s target-type if/elif chain had a
``RDS_DATABASE`` branch but **no** ``JDBC`` branch
(``handlers/pipelines_crud.py:561-597``). Since the P17 type consolidation the
wizard only ever emits ``type: 'JDBC'`` (``JdbcTargetForm.tsx:15-18``), so the
``jdbc_url`` / ``jdbc_driver`` / ``jdbc_table`` checks the ``RDS_DATABASE``
branch performs were unreachable for real payloads: a ``select_rds`` JDBC
target with none of those fields was accepted with HTTP 201 and persisted, and
only failed later inside the Glue job. ``LGR-2474614-1`` records that failure
class (empty ``--target_jdbc_*`` arguments Glue rejects with "expected one
argument"); the remedy there was a fail-fast one layer down, at pipeline-build
time. These tests pin the remaining step: a specific HTTP 400 at create time.

The checks are deliberately mode-INDEPENDENT, matching what ``RDS_DATABASE``
has always done, so each case runs for all three connection modes:

    * ``select_rds`` — previously received no target-side validation at all
      (``_augment_glue_connection_config`` early-returns for any mode other
      than ``glue_connection``, ``pipelines_jdbc.py:288-289``, and the
      manual-mode gate does not fire).
    * ``glue_connection`` — previously received only the augmenter's
      connection-exists / engine-derivable checks, and no presence check on
      any of the three fields.
    * ``manual`` — already had the networking validator; the three field
      checks are additive and must not regress it.

The complementary "does not over-tighten" direction is covered too: a
well-formed target in each mode still returns 201. Per the task-0.5 finding
the MODE-INDEPENDENT field set is exactly the three the ``RDS_DATABASE``
branch already required; ``engine`` is not among them.

``secret_arn`` is deliberately not in that mode-independent set either, but it
is NOT unrequired everywhere — the distinction is per mode, and conflating the
two is what let an unprovisionable pipeline reach HTTP 201:

    * ``glue_connection`` — exempt in the REQUEST only. The wizard sends no
      ``secret_arn`` in this mode; ``_augment_glue_connection_config`` supplies
      it server-side from the connection's ``SECRET_ID``, and refuses a
      connection that has none on the target side as on the source side
      (T-19, ``66e97e5``, which withdrew the inline-credential tolerance in
      favour of invariant ``LGR-819980f-1``). Requiring ``secret_arn`` in the
      request would refuse a payload the wizard legitimately sends.
    * ``select_rds`` / ``manual`` — required, because these modes CREATE the
      Glue Connection. ``create_glue_jdbc_connection`` sends ``SECRET_ID`` only
      when ``secret_arn`` is set, and Glue rejects a credential-less JDBC
      connection outright (``LGR-819980f-2``). ``manual`` has always required
      it via ``_JDBC_MANUAL_REQUIRED_FIELDS``; ``select_rds`` did not, and now
      does.

Does not overlap ``test_glue_connection_augmentation.py`` (the augmenter's own
raise paths), ``test_jdbc_target_manual_validator.py`` (the manual-mode
networking validator in isolation), or ``test_pipelines_manual_mode.py`` (the
dispatcher's mode routing).

Validates: Requirements 9.2
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub the Lambda-layer modules ``handlers.pipelines_crud`` imports at module
# scope. Same idempotent bootstrap as ``test_glue_connection_augmentation.py``
# — a no-op when another file in this directory has already installed it.
# ``sys.path`` itself is set up by this directory's ``conftest.py``.
# ---------------------------------------------------------------------------

sys.modules.setdefault("eventbridge_utils", MagicMock())
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

if "audit_utils" not in sys.modules or not isinstance(sys.modules["audit_utils"], MagicMock):
    _mock_audit_utils = MagicMock()
    _mock_audit_utils.extract_request_context = MagicMock(return_value={})
    _mock_audit_utils.write_audit_entry = MagicMock()
    sys.modules["audit_utils"] = _mock_audit_utils

_prev_env_utils = sys.modules.get("env_utils")
if "env_utils" not in sys.modules or not isinstance(sys.modules["env_utils"], MagicMock):
    _mock_env_utils = MagicMock()
    _mock_env_utils.require_env = lambda name, default=None: {
        "UPLOAD_BUCKET": "agentic-data-ingestion-source",
        "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
        "LOCKS_TABLE": "agentic_data_ingestion_pipeline_locks",
    }.get(name, default or name)
    sys.modules["env_utils"] = _mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

try:
    from handlers import pipelines_crud
    from handlers.pipelines_crud import create_pipeline, edit_pipeline
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

SECRET_ARN = "arn:aws:secretsmanager:us-east-1:123456789012:secret:adi/db-creds-AbCdEf"

# The three fields the JDBC target branch requires, in the order
# ``create_pipeline`` checks them. Order matters for the single-field-missing
# cases: a payload missing two of them reports the earlier one.
_REQUIRED_TARGET_FIELDS = ("jdbc_url", "jdbc_driver", "jdbc_table")

# The two modes the 0.5 finding identified as unvalidated. ``manual`` is
# included separately where the point is that the new checks are additive.
_UNVALIDATED_MODES = ("select_rds", "glue_connection")
_ALL_MODES = ("select_rds", "glue_connection", "manual")


def _valid_jdbc_target(mode: str) -> dict:
    """A well-formed ``target_config`` for ``mode``, shaped as the wizard sends it.

    ``engine`` / ``secret_arn`` are present for every mode so the
    ``glue_connection`` case takes ``_augment_glue_connection_config``'s
    already-populated fast path (``pipelines_jdbc.py:297-299``) and makes no
    Glue call; and so the ``manual`` case satisfies the networking validator.
    """
    config: dict = {
        "type": "JDBC",
        "_connection_mode": mode,
        "jdbc_url": "jdbc:postgresql://target-db.example.com:5432/analytics",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_table": "analytics.users_mirror",
        "engine": "postgres",
        "secret_arn": SECRET_ARN,
    }
    if mode == "glue_connection":
        config["glue_connection_name"] = "customer-owned-postgres"
    if mode == "manual":
        config["subnet_id"] = "subnet-0a1b2c3d"
        config["security_group_ids"] = ["sg-0a1b2c3d"]
    return config


def _body(target_config: dict, *, pipeline_name: str = "p31 jdbc target") -> str:
    """A ``create_pipeline`` request body with an S3 source and the given target.

    The source is ``S3_UPLOAD`` so nothing on the source side can be the
    reason for a rejection — the target is the only variable.
    """
    return json.dumps(
        {
            "pipeline_name": pipeline_name,
            "target_schema": "uploaded_test_schema",
            "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
            "target_config": target_config,
        }
    )


# ---------------------------------------------------------------------------
# Handler fixture — shared table/client mocks bound onto the module.
# ---------------------------------------------------------------------------

_mock_pipelines_table = MagicMock()


@pytest.fixture(autouse=True)
def _handler_mocks(monkeypatch):
    """Bind mock tables/clients so no test can reach real AWS."""
    _mock_pipelines_table.reset_mock()

    monkeypatch.setattr(pipelines_crud, "pipelines_table", _mock_pipelines_table)
    monkeypatch.setattr(pipelines_crud, "state_table", MagicMock())
    monkeypatch.setattr(pipelines_crud, "executions_table", MagicMock())
    monkeypatch.setattr(pipelines_crud, "approvals_table", MagicMock())
    monkeypatch.setattr(pipelines_crud, "dynamodb", MagicMock())
    monkeypatch.setattr(pipelines_crud, "s3_client", MagicMock())

    # The handler builds fresh clients for the S3 target-schema lookup and
    # STS GetCallerIdentity. Failing the lookup is the documented fallback
    # path (the handler logs and continues with the bare schema ID).
    fake_client = MagicMock()
    fake_client.get_object.side_effect = Exception("stubbed S3")
    fake_client.get_caller_identity.return_value = {"Account": "123456789012"}
    monkeypatch.setattr(pipelines_crud.boto3, "client", lambda *a, **kw: fake_client)

    yield


# ---------------------------------------------------------------------------
# 1. Malformed targets are rejected with a specific 400, in every mode
# ---------------------------------------------------------------------------


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestMalformedTargetRejected:
    """A malformed JDBC target returns a specific 400 and persists nothing.

    Validates: Requirements 9.2
    """

    @pytest.mark.parametrize("mode", _UNVALIDATED_MODES)
    @pytest.mark.parametrize("missing_field", _REQUIRED_TARGET_FIELDS)
    def test_missing_field_returns_specific_400(self, _mock_provision, mode: str, missing_field: str):
        """Each of the three fields, dropped on its own, names itself in the 400."""
        target_config = _valid_jdbc_target(mode)
        del target_config[missing_field]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 400, (
            f"mode={mode!r} missing {missing_field!r} must be rejected; got {result['statusCode']}: {result['body']}"
        )
        error = json.loads(result["body"])["error"]
        assert missing_field in error, f"400 must name the missing field {missing_field!r}; got {error!r}"
        assert "JDBC" in error, f"400 must name the target type; got {error!r}"

    @pytest.mark.parametrize("mode", _UNVALIDATED_MODES)
    def test_malformed_target_is_not_persisted(self, _mock_provision, mode: str):
        """The rejection happens before the DDB write — nothing is stored.

        This is the whole point of R9.2: the malformed pipeline must not
        exist, rather than existing and failing at Glue-job runtime.
        """
        target_config = _valid_jdbc_target(mode)
        for field in _REQUIRED_TARGET_FIELDS:
            del target_config[field]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 400
        _mock_pipelines_table.put_item.assert_not_called()

    @pytest.mark.parametrize("mode", _UNVALIDATED_MODES)
    def test_empty_string_field_is_rejected(self, _mock_provision, mode: str):
        """An empty value is as unusable as an absent one and is treated alike.

        ``JdbcConnectionSelector.tsx:198`` can emit ``jdbc_driver: ''`` in
        ``select_rds`` mode when the engine has no ``JDBC_DRIVER_MAP`` entry.
        Glue needs a driver class, so such a pipeline cannot run — rejecting
        it at create time is the intended behaviour, not an accident.
        """
        target_config = _valid_jdbc_target(mode)
        target_config["jdbc_driver"] = ""

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 400
        assert "jdbc_driver" in json.loads(result["body"])["error"]
        _mock_pipelines_table.put_item.assert_not_called()

    def test_manual_mode_also_gets_the_field_checks(self, _mock_provision):
        """The checks are mode-independent, so ``manual`` gets them too.

        ``_validate_jdbc_target_config_manual`` never checked ``jdbc_table``
        (``pipelines_jdbc.py:489-528``), so before this change a manual
        target could also be created without one.
        """
        target_config = _valid_jdbc_target("manual")
        del target_config["jdbc_table"]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 400
        assert "jdbc_table" in json.loads(result["body"])["error"]
        _mock_pipelines_table.put_item.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Well-formed targets are still accepted — the no-over-tightening direction
# ---------------------------------------------------------------------------


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestWellFormedTargetStillAccepted:
    """Restoring the checks must not reject payloads the wizard can produce.

    Validates: Requirements 9.2
    """

    @pytest.mark.parametrize("mode", _ALL_MODES)
    def test_valid_target_returns_201_and_persists(self, _mock_provision, mode: str):
        """A complete target in each of the three modes is created as before."""
        result = create_pipeline(_body(_valid_jdbc_target(mode)))

        assert result["statusCode"] == 201, (
            f"well-formed mode={mode!r} target must be accepted; got {result['statusCode']}: {result['body']}"
        )
        assert _mock_pipelines_table.put_item.called
        persisted = _mock_pipelines_table.put_item.call_args[1]["Item"]["target_config"]
        for field in _REQUIRED_TARGET_FIELDS:
            assert persisted[field], f"{field} must survive into the persisted record"

    def test_engine_is_not_required(self, _mock_provision):
        """``engine`` is still outside the mode-independent required set.

        It is derived downstream rather than demanded here, and that half of
        the original C8 assertion is unchanged.
        """
        target_config = _valid_jdbc_target("select_rds")
        del target_config["engine"]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 201, f"target without engine must still be accepted; got {result['body']}"

    def test_glue_connection_target_without_secret_arn_is_accepted(self, _mock_provision):
        """C8, in the mode it actually applies to — ``secret_arn`` supplied server-side.

        In ``glue_connection`` mode the request carries no ``secret_arn``, and
        ``_augment_glue_connection_config`` fills it from the connection's
        ``SECRET_ID``. That path runs ONLY in ``glue_connection`` mode — the
        augmenter early-returns for every other mode — and it stays creatable.
        Here the fake Glue client answers ``get_connection`` with a
        ``MagicMock``, so the ``SECRET_ID`` lookup is truthy and the augmenter
        takes that arm. A target connection with **no** ``SECRET_ID`` is
        refused since T-19 (``66e97e5``, invariant ``LGR-819980f-1``); that is
        asserted in ``test_glue_connection_augmentation.py``
        (``test_missing_secret_is_fatal_on_both_sides``), not here.
        """
        target_config = _valid_jdbc_target("glue_connection")
        del target_config["secret_arn"]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 201, (
            f"glue_connection target without secret_arn must still be accepted; got {result['body']}"
        )

    def test_select_rds_without_secret_arn_is_rejected(self, _mock_provision):
        """``select_rds`` without a credential is unprovisionable, so 400 not 201.

        This inverts the ``select_rds`` half of the original C8 assertion,
        which was justified by a mechanism that does not run in this mode:
        it cited ``_augment_glue_connection_config`` leaving ``secret_arn``
        unset for an inline-credential connection, but that augmenter
        early-returns for any mode other than ``glue_connection``, and
        ``create_glue_jdbc_connection`` never writes inline credentials
        (invariant ``LGR-819980f-1`` forbids plaintext). So in ``select_rds``
        there is no inline-credential connection to preserve — the connection
        is created from the config, and without ``secret_arn`` it carries
        ``JDBC_CONNECTION_URL`` alone, which Glue rejects with
        ``InvalidInputException: Validation for connection properties failed``
        (``LGR-819980f-2``, verified against the live API).

        The old 201 was reachable only because the Glue client is a
        ``MagicMock`` here, which accepts the credential-less connection that
        real Glue refuses. In the dev account the refusal was swallowed at
        ``pipelines_crud.py:3005`` and the run failed three steps later
        claiming a Glue connection was required.
        """
        target_config = _valid_jdbc_target("select_rds")
        del target_config["secret_arn"]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 400
        assert "secret_arn" in json.loads(result["body"])["error"]
        _mock_pipelines_table.put_item.assert_not_called()

    def test_legacy_rds_database_alias_is_unaffected(self, _mock_provision):
        """The legacy alias branch keeps its own behaviour and message.

        Already-persisted pipelines carry ``type: 'RDS_DATABASE'``; the new
        ``JDBC`` branch sits beside it rather than replacing it.
        """
        target_config = _valid_jdbc_target("select_rds")
        target_config["type"] = "RDS_DATABASE"
        del target_config["jdbc_url"]

        result = create_pipeline(_body(target_config))

        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "jdbc_url" in error
        assert "RDS_DATABASE" in error


# ---------------------------------------------------------------------------
# 3. Write modes the target cannot run are refused (2026-10-10)
# ---------------------------------------------------------------------------
#
# Only S3 Tables implements `merge`. The JDBC writer ran a stored `merge` as
# append, the S3 Parquet writer as overwrite, and each run reported success;
# create accepted `merge` for every target, and edit checked no write mode at
# all while the Edit Pipeline dialog offered "Merge" everywhere. The policy is
# `shared/write_modes.py`; these tests pin the two API boundaries.


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestCreateRefusesMergeOutsideS3Tables:
    def test_jdbc_merge_is_refused_before_any_write(self, _mock_provision):
        target = {**_valid_jdbc_target("select_rds"), "write_mode": "merge", "merge_key": "id"}
        result = create_pipeline(_body(target), event={})
        assert result["statusCode"] == 400
        error = json.loads(result["body"])["error"]
        assert "merge" in error and "S3 Tables" in error
        assert "append" in error and "overwrite" in error, "the message names the modes that would work"
        _mock_pipelines_table.put_item.assert_not_called()

    @pytest.mark.parametrize("mode", ["append", "overwrite"])
    def test_jdbc_append_and_overwrite_are_still_accepted(self, _mock_provision, mode):
        target = {**_valid_jdbc_target("select_rds"), "write_mode": mode}
        result = create_pipeline(_body(target), event={})
        assert result["statusCode"] == 201, result["body"]


def _stored(target_config: dict) -> None:
    """What `pipelines_table.get_item` returns for the pipeline being edited."""
    _mock_pipelines_table.get_item.return_value = {"Item": {"pipeline_id": "pipe-1", "target_config": target_config}}


def _edit(target_config: dict | None = None, **fields) -> dict:
    body = dict(fields)
    if target_config is not None:
        body["target_config"] = target_config
    return edit_pipeline("pipe-1", json.dumps(body), event={})


class TestEditChecksTheEffectiveWriteMode:
    @pytest.fixture(autouse=True)
    def _forget_the_stored_record(self):
        """`reset_mock()` in the module fixture keeps return values, so the
        record a test stores would otherwise leak into the next test."""
        yield
        _mock_pipelines_table.get_item.reset_mock(return_value=True)

    def test_switching_a_jdbc_pipeline_to_merge_is_refused_without_a_write(self):
        """The edit carries only `write_mode`; the type comes from the record."""
        _stored({"type": "JDBC", "write_mode": "append"})
        result = _edit({"write_mode": "merge", "merge_key": "id"})
        assert result["statusCode"] == 400
        assert "S3 Tables" in json.loads(result["body"])["error"]
        _mock_pipelines_table.update_item.assert_not_called()

    def test_a_legacy_jdbc_merge_cannot_be_resaved(self):
        """The dialog re-submits the whole stored target_config. A stored merge
        is refused, which is what makes the user pick a mode that runs."""
        _stored({"type": "JDBC", "write_mode": "merge", "merge_key": "id"})
        result = _edit({"type": "JDBC", "write_mode": "merge", "merge_key": "id"})
        assert result["statusCode"] == 400
        _mock_pipelines_table.update_item.assert_not_called()

    def test_a_legacy_jdbc_merge_can_be_corrected(self):
        _stored({"type": "JDBC", "write_mode": "merge", "merge_key": "id"})
        assert _edit({"write_mode": "append"})["statusCode"] == 200

    def test_an_edit_that_does_not_touch_the_target_is_not_blocked(self):
        """Renaming, describing or archiving a legacy pipeline still works, and
        reads no record for a check it does not need."""
        _stored({"type": "JDBC", "write_mode": "merge", "merge_key": "id"})
        assert _edit(description="still here")["statusCode"] == 200
        _mock_pipelines_table.get_item.assert_not_called()

    def test_switching_the_type_under_a_stored_merge_is_refused(self):
        _stored({"type": "NEW_S3_TABLE_BUCKET", "write_mode": "merge", "merge_key": "id"})
        result = _edit({"type": "S3_PARQUET"})
        assert result["statusCode"] == 400
        _mock_pipelines_table.update_item.assert_not_called()

    def test_s3_tables_merge_is_accepted_with_a_merge_key(self):
        _stored({"type": "NEW_S3_TABLE_BUCKET", "write_mode": "append"})
        assert _edit({"write_mode": "merge", "merge_key": "customer_id"})["statusCode"] == 200

    def test_s3_tables_merge_still_needs_a_merge_key(self):
        _stored({"type": "NEW_S3_TABLE_BUCKET", "write_mode": "append"})
        result = _edit({"write_mode": "merge"})
        assert result["statusCode"] == 400
        assert "merge_key" in json.loads(result["body"])["error"]

    def test_an_unknown_write_mode_is_refused(self):
        """Edit used to store any string, which the templates then ran as append."""
        _stored({"type": "JDBC", "write_mode": "append"})
        result = _edit({"write_mode": "upsert"})
        assert result["statusCode"] == 400
        assert "upsert" in json.loads(result["body"])["error"]


# ---------------------------------------------------------------------------
# 4. A failed target connection fails the create, and undoes it (2026-10-10)
# ---------------------------------------------------------------------------
#
# `create_pipeline` used to log a failed JDBC target connection and return 201
# "Pipeline created successfully". The pipeline then looked active and its first
# run failed in CreateGluePipeline with "A Glue connection is required for a JDBC
# target". Expectations below come from the requirement: a required stage that
# fails produces a failure response naming the stage, removes what this request
# created by its own deterministic names, and never touches a user-owned
# connection.


def _client_error(code: str, operation: str = "CreateConnection"):
    from botocore.exceptions import ClientError

    return ClientError({"Error": {"Code": code, "Message": f"{code} from Glue"}}, operation)


class _RecordingGlue:
    """`boto3.client("glue")` for the rollback: records every delete."""

    def __init__(self, delete_connection_error=None):
        self.deleted_connections: list[str] = []
        self.deleted_crawlers: list[str] = []
        self._delete_connection_error = delete_connection_error

    def delete_connection(self, ConnectionName):
        if self._delete_connection_error is not None:
            raise self._delete_connection_error
        self.deleted_connections.append(ConnectionName)

    def delete_crawler(self, Name):
        self.deleted_crawlers.append(Name)

    def get_caller_identity(self):
        return {"Account": "123456789012"}

    def get_object(self, **_kwargs):
        raise Exception("stubbed S3")


@pytest.fixture
def recording_glue(monkeypatch):
    glue = _RecordingGlue()
    monkeypatch.setattr(pipelines_crud.boto3, "client", lambda *a, **kw: glue)
    return glue


def _created_pipeline_id() -> str:
    return _mock_pipelines_table.put_item.call_args.kwargs["Item"]["pipeline_id"]


@patch("handlers.pipelines_crud._provision_pipeline_resources")
class TestTargetConnectionFailureFailsTheCreate:
    def test_a_service_failure_is_a_500_naming_the_stage(self, _mock_provision, recording_glue):
        with patch(
            "handlers.pipelines_crud._provision_jdbc_target_connection",
            side_effect=_client_error("ResourceNumberLimitExceededException"),
        ):
            result = create_pipeline(_body(_valid_jdbc_target("select_rds")), event={})

        assert result["statusCode"] == 500, "the caller cannot fix a Glue quota by changing the request"
        body = json.loads(result["body"])
        assert body["reason"] == "provisioning_failed"
        assert body["stage"] == "target_connection"
        assert body["error"].startswith("Pipeline was not created")
        assert "ResourceNumberLimitExceededException" in body["error"]

    def test_everything_this_request_made_is_removed(self, _mock_provision, recording_glue):
        with patch(
            "handlers.pipelines_crud._provision_jdbc_target_connection",
            side_effect=_client_error("InternalServiceException"),
        ):
            result = create_pipeline(_body(_valid_jdbc_target("select_rds")), event={})

        pipeline_id = _created_pipeline_id()
        assert recording_glue.deleted_connections == [f"agentic-data-ingestion-{pipeline_id}-rds"]
        _mock_pipelines_table.delete_item.assert_called_once_with(Key={"pipeline_id": pipeline_id})
        assert json.loads(result["body"])["cleanup"]["pipeline_record"] == "deleted"

    def test_invalid_input_from_glue_is_a_400(self, _mock_provision, recording_glue):
        with patch(
            "handlers.pipelines_crud._provision_jdbc_target_connection",
            side_effect=_client_error("InvalidInputException"),
        ):
            result = create_pipeline(_body(_valid_jdbc_target("select_rds")), event={})
        assert result["statusCode"] == 400

    def test_a_connection_created_but_not_recorded_is_rolled_back(self, _mock_provision, recording_glue):
        """The record update that stores `jdbc_connection_name` is inside the
        same stage, so its failure must not orphan the connection it names."""
        _mock_pipelines_table.update_item.side_effect = [_client_error("ProvisionedThroughputExceededException")]
        try:
            with patch(
                "handlers.pipelines_crud._provision_jdbc_target_connection",
                side_effect=lambda pid, _cfg: f"agentic-data-ingestion-{pid}-rds",
            ):
                result = create_pipeline(_body(_valid_jdbc_target("select_rds")), event={})
        finally:
            _mock_pipelines_table.update_item.side_effect = None

        pipeline_id = _created_pipeline_id()
        assert result["statusCode"] == 500
        assert recording_glue.deleted_connections == [f"agentic-data-ingestion-{pipeline_id}-rds"]

    def test_a_cleanup_that_fails_archives_the_pipeline_and_says_so(self, _mock_provision, monkeypatch):
        glue = _RecordingGlue(delete_connection_error=_client_error("AccessDeniedException", "DeleteConnection"))
        monkeypatch.setattr(pipelines_crud.boto3, "client", lambda *a, **kw: glue)
        with patch(
            "handlers.pipelines_crud._provision_jdbc_target_connection",
            side_effect=_client_error("InternalServiceException"),
        ):
            result = create_pipeline(_body(_valid_jdbc_target("select_rds")), event={})

        body = json.loads(result["body"])
        pipeline_id = _created_pipeline_id()
        assert body["cleanup"]["pipeline_record"] == "archived"
        assert body["cleanup"]["residual"][0]["resource"] == f"Glue connection agentic-data-ingestion-{pipeline_id}-rds"
        assert "archived" in body["error"] and "delete it from the dashboard" in body["error"]
        assert "InternalServiceException" in body["error"], "the primary failure is still the one reported"
        _mock_pipelines_table.delete_item.assert_not_called()
        archive = _mock_pipelines_table.update_item.call_args.kwargs
        assert archive["ExpressionAttributeValues"][":archived"] == "archived"

    def test_a_target_on_a_users_own_connection_never_loses_it(self, _mock_provision, recording_glue):
        """In glue_connection mode nothing is created for the target, and the
        rollback deletes no connection by the user's name or by ours."""
        with patch(
            "handlers.pipelines_crud._provision_jdbc_target_connection",
            side_effect=_client_error("InternalServiceException"),
        ):
            create_pipeline(_body(_valid_jdbc_target("glue_connection")), event={})
        assert recording_glue.deleted_connections == []


class TestTheRollbackNeverDeletesAUsersConnection:
    def test_user_supplied_connection_names_are_never_deleted(self, recording_glue):
        """Called directly with both sides on user-owned connections."""
        undo = pipelines_crud._undo_pipeline_create(
            "pipe-abc12345",
            "JDBC",
            {"glue_connection_name": "customer-source-conn"},
            {"type": "JDBC", "glue_connection_name": "customer-target-conn", "jdbc_connection_name": "anything"},
            folder_marker_written=False,
            schedule_rule_may_exist=False,
        )
        assert recording_glue.deleted_connections == []
        assert recording_glue.deleted_crawlers == ["agentic-data-ingestion-pipe-abc12345-source-discover"]
        assert undo["pipeline_record"] == "deleted"

    def test_an_absent_resource_counts_as_removed(self, monkeypatch):
        glue = _RecordingGlue(delete_connection_error=_client_error("EntityNotFoundException", "DeleteConnection"))
        monkeypatch.setattr(pipelines_crud.boto3, "client", lambda *a, **kw: glue)
        undo = pipelines_crud._undo_pipeline_create(
            "pipe-abc12345",
            "S3_UPLOAD",
            {},
            {"type": "JDBC"},
            folder_marker_written=False,
            schedule_rule_may_exist=False,
        )
        assert undo["residual"] == []
        assert undo["pipeline_record"] == "deleted"
