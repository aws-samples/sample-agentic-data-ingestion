# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: the target-schema bucket comes from the attribute that is written.

**The live failure.** Account 777788889999, run
``run-2026-08-04-11-47-47-86d7b76c``, step ``schema_compare``::

    Invalid bucket name "": Bucket name must match the regex
    "^[a-zA-Z0-9.\\-_]{1,255}$"
    ... get_target_schema_from_s3 -> s3_client.get_object(Bucket=bucket, ...)
    botocore.exceptions.ParamValidationError

``lambda_handler`` resolved the bucket as
``item_str(pipeline_metadata, "generated_code_bucket")`` and passed it to
``check_target_schema_changed``. No writer has ever put ``generated_code_bucket``
on a pipeline record: ``pipelines_crud.create_pipeline`` splits the single
``s3://`` upload location into ``target_schema_bucket`` + ``target_schema_s3_key``
and writes them together, and that is the only bucket attribute a pipeline
record carries. ``item_str`` returns ``""`` for an absent attribute, so an empty
string travelled four frames and surfaced as a botocore parameter error rather
than as a configuration error naming the attribute.

**Why no existing test caught it.** The two handler-level tests in this
directory (``test_declared_type_mismatch.py``, ``test_target_schema_version_deferred.py``)
monkeypatch ``check_target_schema_changed`` out entirely, so the bucket never
reaches S3 in them — and both supplied ``{"generated_code_bucket": ...}`` as the
metadata fixture, which encoded the wrong attribute name as if it were correct.
The tests that do exercise ``check_target_schema_changed``
(``test_read_path_errors.py``, ``test_target_schema_version_deferred.py``) passed
the bucket in as a literal argument, so no test ever asserted that the bucket
came from an attribute a real record actually has. That is the gap these tests
close: they run the handler → check → ``s3_client.get_object`` seam with the
metadata shape the writer produces, and they pin the writer/reader agreement
directly.

The reader no longer takes a bucket parameter — the bucket and the key are
resolved together, in one place, from the record that writes them together — so
the two cannot be sourced from different attributes again.
"""

import importlib.util
import os
import pathlib
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

_mods = {"metrics_facade": mock.MagicMock(PipelineMetrics=None)}


def _load_module():
    """Load the schema_compare lambda_function by path (sibling-test pattern)."""
    mod_path = os.path.join(LAMBDA_DIR, "lambda_function.py")
    spec = importlib.util.spec_from_file_location(
        "schema_compare_lf_bucket_source",
        mod_path,
        submodule_search_locations=[LAMBDA_DIR],
    )
    with mock.patch.dict("sys.modules", _mods):
        mod = importlib.util.module_from_spec(spec)
        sys.modules["schema_compare_lf_bucket_source"] = mod
        spec.loader.exec_module(mod)
    return mod


_lf = _load_module()

PIPELINE_ID = "pipe-bucket-source"
TARGET_BUCKET = "adi-target-schemas-777788889999-us-west-2"
TARGET_KEY = "shared/uploaded_schema_1785844048926_20260804_114729.json"
SCHEMA = {"type": "object", "properties": {"id": {"type": "integer"}}}

#: The pipeline record as ``pipelines_crud.create_pipeline`` writes it for an
#: uploaded target schema — the attribute set observed on the live record for
#: the failing run, minus the fields this step does not read. Note there is no
#: bucket attribute other than ``target_schema_bucket``, and no
#: ``target_schema_version``: nothing initialises one, so the no-baseline branch
#: is the branch a fresh pipeline takes.
LIVE_PIPELINE_RECORD = {
    "pipeline_id": PIPELINE_ID,
    "pipeline_name": "testing",
    "status": "active",
    "target_schema_name": "uploaded_schema_1785844048926",
    "target_schema_bucket": TARGET_BUCKET,
    "target_schema_s3_key": TARGET_KEY,
    "target_type": "New S3 Bucket",
    "source_type": "S3_UPLOAD",
}


def _s3_returning(content: bytes) -> mock.MagicMock:
    s3 = mock.MagicMock()
    body = mock.MagicMock()
    body.read.return_value = content
    s3.get_object.return_value = {"Body": body}
    return s3


def _run_handler(pipeline_record: dict, s3: mock.MagicMock):
    """Drive ``lambda_handler`` with a real ``check_target_schema_changed``.

    Only the DynamoDB reads and the result write are stubbed; the bucket
    resolution and the S3 call are the code under test.
    """
    current_row = {
        "extracted_schema": SCHEMA,
        "schema_hash": "hash-current",
        "original_pipeline_id": PIPELINE_ID,
    }
    with (
        mock.patch.object(_lf, "get_current_schema", return_value=current_row),
        mock.patch.object(_lf, "get_previous_schema", return_value=None),
        mock.patch.object(_lf, "get_pipeline_metadata", return_value=pipeline_record),
        mock.patch.object(_lf, "s3_client", s3),
        mock.patch.object(_lf, "store_comparison_result"),
    ):
        return _lf.lambda_handler({"run_id": "run-bucket-source"}, None)


def test_handler_reads_the_target_schema_from_the_recorded_bucket():
    """The regression: the bucket reaching S3 is the record's, and is non-empty.

    Before the fix this asserted-on call never happened — botocore rejected
    ``Bucket=""`` inside ``get_object`` and the run failed at step 3 of 9.
    """
    s3 = _s3_returning(b'{"type": "object"}')

    response = _run_handler(LIVE_PIPELINE_RECORD, s3)

    s3.get_object.assert_called_once()
    called_bucket = s3.get_object.call_args.kwargs["Bucket"]
    assert called_bucket, "the target-schema bucket must not be empty"
    assert called_bucket == TARGET_BUCKET
    assert s3.get_object.call_args.kwargs["Key"] == TARGET_KEY
    assert response["statusCode"] == 200


def test_key_without_a_bucket_fails_naming_the_missing_attribute():
    """A half-written record must fail loudly, not silently report "not changed".

    Reporting no change would make target-schema drift undetectable for the
    pipeline — the invisibility R8.4 (``LGR-eee8ac8-4``) exists to prevent.
    """
    record = {k: v for k, v in LIVE_PIPELINE_RECORD.items() if k != "target_schema_bucket"}
    s3 = _s3_returning(b'{"type": "object"}')

    with mock.patch.object(_lf, "s3_client", s3), pytest.raises(ValueError) as exc_info:
        _lf.check_target_schema_changed(record)

    assert "target_schema_bucket" in str(exc_info.value)
    s3.get_object.assert_not_called()


def test_empty_bucket_string_is_rejected_before_it_reaches_botocore():
    """An empty string, not just an absent attribute — the live failure's value."""
    record = {**LIVE_PIPELINE_RECORD, "target_schema_bucket": ""}
    s3 = _s3_returning(b'{"type": "object"}')

    with mock.patch.object(_lf, "s3_client", s3), pytest.raises(ValueError) as exc_info:
        _lf.check_target_schema_changed(record)

    assert "target_schema_bucket" in str(exc_info.value)
    s3.get_object.assert_not_called()


def test_no_bucket_attribute_other_than_target_schema_bucket_is_written():
    """Pin the writer/reader agreement that the defect broke.

    ``schema_compare`` read ``generated_code_bucket`` off a pipeline record for
    six months. This asserts the writer's side: ``create_pipeline`` produces
    ``target_schema_bucket`` and does not produce ``generated_code_bucket``, so
    reintroducing the old attribute name on the read side fails here rather than
    in a deployed run.
    """
    crud = pathlib.Path(__file__).resolve().parents[4] / (
        "lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_crud.py"
    )
    source = crud.read_text()

    assert 'pipeline_item["target_schema_bucket"]' in source
    assert "generated_code_bucket" not in source
