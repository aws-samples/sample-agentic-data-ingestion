# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_compare does not commit ``target_schema_version`` (p30 R8.4).

R8.4 has two halves. This file covers the *upstream* half — that
``schema_compare`` no longer advances the version — and
``tests/unit/lambdas/job_completion/test_target_schema_version_commit.py``
covers the downstream half, that a successful run commits it and a failed run
does not. The write crossed a Lambda boundary, so the requirement cannot be
asserted in one place.

**The defect.** ``lambda_handler`` used to call
``pipelines_table.update_item`` the moment ``check_target_schema_changed``
reported a change — at step 3 of the 9-step machine, with the Glue write six
steps away (``orchestration-stack.ts:702`` … ``:718``). If the run then failed,
the pipelines table claimed a target-schema version that had never been mapped
against or written to. The *next* run compared its target schema against that
version, found no change, and skipped the remap the failure had left
outstanding — so the drift the check exists to catch became invisible. It is the
same shape as the task 4.5 defect (a run compared a schema against itself) and
it compounds with it: both make a real change look like no change.

**Why the observed version is still reported when no baseline exists.** Nothing
initialises ``target_schema_version``. ``create_pipeline`` persists
``target_schema_bucket`` + ``target_schema_s3_key`` and no version
(``pipelines_crud.py:737-740``), and the old writer sat *inside* the
changed-branch, which cannot be reached without a stored version — so the
version could never be established and target-schema drift could never fire at
all. ``check_target_schema_changed`` now returns the version it read even with
no baseline, so the first successful run can commit it. It still reports
``changed=False`` there: with no baseline there is nothing to have changed
*from*, and reporting True would route every pipeline's first run to
``remap_required``.

Existing coverage in ``test_read_path_errors.py::TestCheckTargetSchemaChanged``
(3 tests, P28 R17.3) pins the read-error classification of the same function and
is unaffected — the no-key case still returns ``(False, None, None)``, and every
case with a stored version behaves exactly as before.

**Validates: Requirement 8.4**
"""

import importlib.util
import os
import sys
import unittest.mock as mock

import pytest

LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_schema_compare",
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, SHARED_DIR)

os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

_mods = {
    "metrics_facade": mock.MagicMock(PipelineMetrics=None),
}


def _load_module():
    """Load the schema_compare lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_compare_lf_p30_version_commit",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_p30_version_commit"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()

PIPELINE_ID = "pipe-r84"
SCHEMA = {"type": "object", "properties": {"id": {"type": "integer"}}}


def _s3_returning(content: bytes) -> mock.MagicMock:
    s3 = mock.MagicMock()
    body = mock.MagicMock()
    body.read.return_value = content
    s3.get_object.return_value = {"Body": body}
    return s3


def _run_handler(target_versions: tuple[bool, str | None, str | None], previous_schema: dict | None = SCHEMA):
    """Drive ``lambda_handler`` to completion with a stubbed target-schema verdict.

    Returns ``(response, pipelines_table_mock, store_comparison_result_mock)``.
    """
    current_row = {
        "extracted_schema": SCHEMA,
        "schema_hash": "hash-current",
        "original_pipeline_id": PIPELINE_ID,
    }
    previous_row = None
    if previous_schema is not None:
        previous_row = {"extracted_schema": previous_schema, "schema_hash": "hash-previous"}

    pipelines_table = mock.MagicMock()
    with (
        mock.patch.object(_lf, "get_current_schema", return_value=current_row),
        mock.patch.object(_lf, "get_previous_schema", return_value=previous_row),
        mock.patch.object(
            _lf,
            "get_pipeline_metadata",
            return_value={
                "target_schema_bucket": "bucket-r84",
                "target_schema_s3_key": "schemas/target.json",
            },
        ),
        mock.patch.object(_lf, "check_target_schema_changed", return_value=target_versions),
        mock.patch.object(_lf, "pipelines_table", pipelines_table),
        mock.patch.object(_lf, "store_comparison_result") as stored,
    ):
        response = _lf.lambda_handler({"run_id": "run-r84"}, None)
    return response, pipelines_table, stored


# ── The version is not advanced by schema_compare ────────────────────


def test_target_schema_change_does_not_write_the_version():
    """The core R8.4 assertion: the detecting step performs no pipelines-table write.

    **Validates: Requirement 8.4**
    """
    response, pipelines_table, _ = _run_handler((True, "v-old", "v-new"))

    assert response["target_schema_changed"] is True
    assert response["action"] == "remap_required"
    pipelines_table.update_item.assert_not_called()
    pipelines_table.put_item.assert_not_called()


def test_unchanged_target_schema_does_not_write_the_version():
    """No write on the unchanged path either — there never was one, pinned so
    the commit cannot drift back upstream.

    **Validates: Requirement 8.4**
    """
    _, pipelines_table, _ = _run_handler((False, "v-same", "v-same"))

    pipelines_table.update_item.assert_not_called()


def test_first_run_does_not_write_the_version():
    """A pipeline's first run establishes its baseline on success, not here.

    **Validates: Requirement 8.4**
    """
    response, pipelines_table, _ = _run_handler((False, None, "v-first"), previous_schema=None)

    assert response["comparison_result"]["summary"]["first_schema"] is True
    pipelines_table.update_item.assert_not_called()


# ── The observed version reaches the state row, which is what the ────
# ── completion handler reads ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ((True, "v-old", "v-new"), "v-new"),
        ((False, "v-same", "v-same"), "v-same"),
        ((False, None, "v-first"), "v-first"),
    ],
    ids=["changed", "unchanged", "no-baseline"],
)
def test_observed_version_is_recorded_on_the_state_row(verdict, expected):
    """``job_completion`` reads ``target_schema_new_version`` off this run's
    ``schema_compare`` row, so the deferred commit only works if the observed
    version lands there on every path — including when nothing changed.

    **Validates: Requirement 8.4**
    """
    _, _, stored = _run_handler(verdict, previous_schema=SCHEMA if verdict[1] else None)

    assert stored.called
    kwargs = stored.call_args.kwargs
    positional = stored.call_args.args
    # The changed/unchanged paths call through positionally; the first-run
    # branch uses keywords. Accept either spelling.
    if "target_schema_new_version" in kwargs:
        assert kwargs["target_schema_new_version"] == expected
    else:
        assert positional[8] == expected


# ── check_target_schema_changed's own contract ───────────────────────


def test_no_baseline_reports_the_observed_version_and_no_change():
    """A pipeline with a target schema but no stored version.

    ``changed`` must stay False — reporting True with no baseline would route
    every pipeline's first run to ``remap_required``.

    **Validates: Requirement 8.4**
    """
    s3 = _s3_returning(b'{"type": "object"}')
    with mock.patch.object(_lf, "s3_client", s3):
        changed, old, observed = _lf.check_target_schema_changed(
            {"target_schema_s3_key": "schemas/target.json", "target_schema_bucket": "bucket"}
        )

    assert changed is False
    assert old is None
    assert isinstance(observed, str) and len(observed) == 64


def test_no_target_schema_object_reports_no_version_at_all():
    """Nothing to version, so nothing to commit — and S3 is not read.

    **Validates: Requirement 8.4**
    """
    s3 = mock.MagicMock()
    with mock.patch.object(_lf, "s3_client", s3):
        assert _lf.check_target_schema_changed({"target_schema_version": "orphan"}) == (False, None, None)

    s3.get_object.assert_not_called()


def test_stored_baseline_still_detects_a_change():
    """The pre-existing contract, pinned next to the new branch so the
    no-baseline case is visibly an addition rather than a change.

    **Validates: Requirement 8.4**
    """
    s3 = _s3_returning(b'{"type": "object"}')
    with mock.patch.object(_lf, "s3_client", s3):
        changed, old, observed = _lf.check_target_schema_changed(
            {
                "target_schema_s3_key": "schemas/target.json",
                "target_schema_bucket": "bucket",
                "target_schema_version": "stale",
            }
        )

    assert changed is True
    assert old == "stale"
    assert observed != "stale"
