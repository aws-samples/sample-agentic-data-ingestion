# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase T2 — Lightweight Lambda-level file-format checks.

Invokes the `file_validation` and `schema_extract` Lambdas directly for:
1. A multi-sheet Excel file (schema extracted without error)
2. A corrupt/invalid file (descriptive validation error, no crash)
3. A >100MB file (large file handling, carries `slow` marker)

CSV/Parquet/JSON are NOT re-tested here — they are covered as full runs
in Phase T1 (tasks 7–8).

Requirements: 16.1, 16.2, 16.3, 16.4, 16.5
"""

from __future__ import annotations

import io
import json
import logging
import uuid

import pytest

logger = logging.getLogger(__name__)

# Lambda function names (deployed naming convention)
FILE_VALIDATION_LAMBDA = "agentic_data_ingestion_file_validation"
SCHEMA_EXTRACT_LAMBDA = "agentic_data_ingestion_schema_extract"


def _invoke_lambda(lambda_client, function_name: str, payload: dict) -> dict:
    """Invoke a Lambda function synchronously and return the parsed response."""
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    response_payload = json.loads(response["Payload"].read().decode("utf-8"))
    # Check for Lambda-level errors (unhandled exceptions)
    if response.get("FunctionError"):
        return {"_lambda_error": True, "errorMessage": response_payload}
    return response_payload


def _generate_xlsx_bytes(sheet_names: list[str], rows_per_sheet: int = 5) -> bytes:
    """Generate a simple multi-sheet Excel file using openpyxl."""
    from openpyxl import Workbook

    wb = Workbook()
    # Remove default sheet
    wb.remove(wb.active)

    for sheet_name in sheet_names:
        ws = wb.create_sheet(title=sheet_name)
        # Header row
        ws.append(["id", "name", "value", "category"])
        # Data rows
        for i in range(1, rows_per_sheet + 1):
            ws.append([f"row-{i}", f"Item {i}", i * 10, sheet_name])

    buffer = io.BytesIO()
    wb.save(buffer)
    return buffer.getvalue()


@pytest.mark.e2e
class TestFormatChecks:
    """Lightweight Lambda-level file-format checks (Req 16)."""

    @pytest.mark.skip(
        reason=(
            "MISSING PRECONDITION, not a flake: this test needs a pipeline record carrying a "
            "`selected_worksheet`, and it provisions none. p55 R8.4 fails a multi-sheet workbook "
            "whose pipeline has no worksheet configured, so the Excel branch cannot be exercised "
            "honestly without one. Provisioning it is the work to do here, and it cannot be "
            "validated without running this (billable) suite. Req 16.1 is meanwhile discharged by "
            "tests/unit/lambdas/schema_extract/test_multi_sheet_schema_without_error.py, so nothing "
            "is uncovered by this skip."
        )
    )
    def test_multi_sheet_excel_schema_extracted(
        self,
        requires_trigger,
        e2e_ledger,
        boto3_session,
        resource_names,
    ):
        """Multi-sheet Excel: schema_extract returns the selected worksheet's schema.

        Uploads a .xlsx file with multiple sheets, invokes file_validation then
        schema_extract, and asserts the schema describes the *chosen* worksheet.

        Requirements: 16.1, 16.3

        SKIPPED, AND THE SKIP IS THE HONEST STATE (p55 task 7.2)
        --------------------------------------------------------
        What this test used to be: it passed ``source_format: "unknown"``, so it
        never reached the Excel branch at all. What it actually asserted was
        that the Lambda does not crash — which was the whole of Req 16.1 when it
        was written, because ``.xlsx`` support did not exist. Worse, its
        ``run_id``-absent path logged "validation-only coverage for Excel" and
        **passed**, so a run that checked nothing reported success.

        Both are now fixed: the payload names the Excel Canonical_Format its own
        title claims, and the silent-pass branch is an assertion. That leaves one
        genuine gap, which is what the skip records rather than papers over —
        p55 R8.4 fails a multi-sheet workbook whose pipeline record carries no
        ``selected_worksheet``, and this test creates no pipeline record. The
        assertions below are the ones a correct run needs; the missing piece is
        the provisioning, and writing provisioning that cannot be executed would
        be a claim of coverage rather than coverage.

        Req 16.1 is discharged live, and for free, by
        ``tests/unit/lambdas/schema_extract/test_multi_sheet_schema_without_error.py``,
        which reads the committed multi-sheet fixture through ``schema_extract``'s
        ``lambda_handler``. Nothing is uncovered while this stays skipped.
        """
        # Generate a multi-sheet Excel file
        sheet_names = ["Customers", "Orders", "Products"]
        xlsx_bytes = _generate_xlsx_bytes(sheet_names, rows_per_sheet=5)

        # Upload to source bucket
        source_bucket = resource_names["source_bucket"]
        unique_id = uuid.uuid4().hex[:12]
        pipeline_prefix = f"e2e-format-test-{unique_id}"
        s3_key = f"{pipeline_prefix}/multi-sheet-{unique_id}.xlsx"

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=xlsx_bytes,
            ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        e2e_ledger.add_s3_object(source_bucket, s3_key)
        logger.info("Uploaded multi-sheet Excel: s3://%s/%s", source_bucket, s3_key)

        # Invoke file_validation Lambda
        lambda_client = boto3_session.client("lambda")
        validation_event = {
            "source_bucket": source_bucket,
            "source_key": s3_key,
            "pipeline_id": pipeline_prefix,
        }

        validation_result = _invoke_lambda(lambda_client, FILE_VALIDATION_LAMBDA, validation_event)
        logger.info("file_validation result: %s", json.dumps(validation_result, default=str)[:500])

        # The file_validation Lambda should not crash — it should process the file
        assert not validation_result.get("_lambda_error"), f"file_validation Lambda crashed: {validation_result}"

        # Invoke schema_extract Lambda with the file location
        # schema_extract expects a run_id and reads previous state from DynamoDB.
        # For a direct invoke test, we pass the S3 info and run_id from the
        # file_validation output.
        # A missing run_id is a FAILURE, not a reason to report success. The
        # previous version logged and fell through here, so a run that never
        # invoked schema_extract at all still passed — the shape of quiet
        # wrongness this suite exists to catch.
        run_id = validation_result.get("run_id")
        assert run_id, (
            f"file_validation returned no run_id for a recognised .xlsx upload, so schema_extract "
            f"cannot be invoked and the Excel branch is untested: {validation_result}"
        )

        schema_event = {
            "run_id": run_id,
            "execution_arn": f"e2e-format-test-{unique_id}",
            # The Excel Canonical_Format, which is what this test's name claims
            # to exercise. `FORMAT_MAP` maps `.xlsx` -> `excel`
            # (`file_validation/lambda_function.py`), and `excel` is deliberately
            # NOT in `NATIVE_FORMATS`, so this value routes schema_extract to the
            # branch that opens the workbook and converts it to CSV.
            "source_format": "excel",
        }
        schema_result = _invoke_lambda(lambda_client, SCHEMA_EXTRACT_LAMBDA, schema_event)
        logger.info(
            "schema_extract result: %s",
            json.dumps(schema_result, default=str)[:500],
        )

        # schema_extract should not crash (no unhandled Lambda error)
        assert not schema_result.get("_lambda_error"), (
            f"schema_extract Lambda crashed on multi-sheet Excel: {schema_result}"
        )

        # Unconditional. Gating this on `status == "success"` meant a refusal
        # passed the test, which is precisely what a multi-sheet workbook with no
        # configured worksheet produces (p55 R8.4).
        assert schema_result.get("status") == "success", (
            f"schema_extract did not succeed on the multi-sheet workbook. If the message names "
            f"'selected_worksheet', this test's missing precondition is the cause — see the skip "
            f"reason: {schema_result}"
        )
        assert schema_result.get("schema_hash") or schema_result.get("extracted_schema"), (
            "Schema extraction reported success but returned no schema data"
        )

        # p55 R13: `excel` is outside `NATIVE_FORMATS`, so this arm must report a
        # converted object for the Glue job to read rather than the .xlsx itself.
        readable_uri = schema_result.get("readable_source_uri")
        assert readable_uri, "the excel arm must report a readable_source_uri for the converted CSV"
        assert not readable_uri.endswith(".xlsx"), (
            f"readable_source_uri still points at the workbook ({readable_uri}); Spark and Glue have "
            f"no Excel datasource, so a converted object is required"
        )
        assert schema_result.get("readable_source_format") == "csv", (
            f"the converted object is a CSV, so readable_source_format must say so: "
            f"{schema_result.get('readable_source_format')!r}"
        )

    def test_corrupt_file_descriptive_error(
        self,
        requires_trigger,
        e2e_ledger,
        boto3_session,
        resource_names,
    ):
        """Corrupt file: file_validation returns a descriptive error, no crash.

        Uploads garbage bytes with a .csv extension and invokes file_validation.
        Asserts the Lambda returns a descriptive validation error rather than
        crashing with an unhandled exception.

        Requirements: 16.2, 16.3
        """
        # Generate corrupt/garbage bytes
        import os

        corrupt_bytes = os.urandom(1024)  # 1KB of random garbage

        # Upload to source bucket with .csv extension (misleading extension)
        source_bucket = resource_names["source_bucket"]
        unique_id = uuid.uuid4().hex[:12]
        pipeline_prefix = f"e2e-format-test-{unique_id}"
        s3_key = f"{pipeline_prefix}/corrupt-{unique_id}.csv"

        s3_client = boto3_session.client("s3")
        s3_client.put_object(
            Bucket=source_bucket,
            Key=s3_key,
            Body=corrupt_bytes,
            ContentType="text/csv",
        )
        e2e_ledger.add_s3_object(source_bucket, s3_key)
        logger.info("Uploaded corrupt file: s3://%s/%s", source_bucket, s3_key)

        # Invoke file_validation Lambda
        lambda_client = boto3_session.client("lambda")
        validation_event = {
            "source_bucket": source_bucket,
            "source_key": s3_key,
            "pipeline_id": pipeline_prefix,
        }

        result = _invoke_lambda(lambda_client, FILE_VALIDATION_LAMBDA, validation_event)
        logger.info("file_validation result for corrupt file: %s", json.dumps(result, default=str)[:500])

        # The Lambda must NOT crash with an unhandled exception
        assert not result.get("_lambda_error"), (
            f"file_validation Lambda crashed on corrupt input (expected graceful error handling): {result}"
        )

        # The Lambda should have processed the file (even garbage bytes with .csv
        # extension will pass format detection since the extension is recognized).
        # The key assertion: the Lambda did not raise an unhandled exception.
        # If it returned an error status, it should be descriptive (not a stack trace).
        if result.get("status") in ("failed", "error", "unsupported"):
            error_msg = result.get("error_message", result.get("errorMessage", ""))
            assert error_msg, "file_validation returned a failure status but no descriptive error message"
            logger.info("Descriptive error returned: %s", error_msg)

    @pytest.mark.slow
    def test_large_file_over_100mb(
        self,
        requires_trigger,
        e2e_ledger,
        boto3_session,
        resource_names,
    ):
        """Large file (>100MB): file_validation handles without crash or timeout.

        Generates a >100MB file and invokes file_validation to confirm
        it handles large inputs gracefully. Carries the `slow` marker due to
        upload time and potential processing duration.

        Requirements: 16.3, 16.4

        Note: This test adds ~$0.01 S3 cost (PUT + storage for test duration)
        and takes 30–90s depending on upload bandwidth.
        """
        # Generate a large CSV file (>100MB)
        # Strategy: repeat a header + rows to exceed 100MB
        header = "id,name,email,department,value,timestamp,notes\n"
        # Each row is ~100 bytes; we need ~1M rows for 100MB
        row_template = "row-{i:07d},User {i},user{i}@example.com,dept-{dept},${val:.2f},2024-01-{day:02d}T{hour:02d}:00:00Z,Lorem ipsum dolor sit amet\n"

        # Build the file in chunks to avoid memory issues
        source_bucket = resource_names["source_bucket"]
        unique_id = uuid.uuid4().hex[:12]
        pipeline_prefix = f"e2e-format-test-{unique_id}"
        s3_key = f"{pipeline_prefix}/large-{unique_id}.csv"

        # Use multipart upload for the large file
        s3_client = boto3_session.client("s3")
        mpu = s3_client.create_multipart_upload(
            Bucket=source_bucket,
            Key=s3_key,
            ContentType="text/csv",
        )
        upload_id = mpu["UploadId"]

        try:
            parts = []
            part_number = 1
            total_bytes = 0
            target_size = 101 * 1024 * 1024  # 101MB to be safely over 100MB
            min_part_size = 5 * 1024 * 1024  # 5MB minimum for multipart

            chunk = io.StringIO()
            chunk.write(header)

            row_idx = 0
            while total_bytes < target_size:
                row_idx += 1
                row = row_template.format(
                    i=row_idx,
                    dept=row_idx % 10,
                    val=row_idx * 1.5,
                    day=(row_idx % 28) + 1,
                    hour=row_idx % 24,
                )
                chunk.write(row)

                # Flush part when chunk reaches minimum part size
                if chunk.tell() >= min_part_size:
                    body = chunk.getvalue().encode("utf-8")
                    resp = s3_client.upload_part(
                        Bucket=source_bucket,
                        Key=s3_key,
                        UploadId=upload_id,
                        PartNumber=part_number,
                        Body=body,
                    )
                    parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
                    total_bytes += len(body)
                    part_number += 1
                    chunk = io.StringIO()

            # Upload remaining data
            if chunk.tell() > 0:
                body = chunk.getvalue().encode("utf-8")
                resp = s3_client.upload_part(
                    Bucket=source_bucket,
                    Key=s3_key,
                    UploadId=upload_id,
                    PartNumber=part_number,
                    Body=body,
                )
                parts.append({"PartNumber": part_number, "ETag": resp["ETag"]})
                total_bytes += len(body)

            # Complete multipart upload
            s3_client.complete_multipart_upload(
                Bucket=source_bucket,
                Key=s3_key,
                UploadId=upload_id,
                MultipartUpload={"Parts": parts},
            )
            logger.info(
                "Uploaded large file (%d MB): s3://%s/%s",
                total_bytes // (1024 * 1024),
                source_bucket,
                s3_key,
            )
        except Exception:
            # Abort multipart upload on failure
            s3_client.abort_multipart_upload(Bucket=source_bucket, Key=s3_key, UploadId=upload_id)
            raise

        e2e_ledger.add_s3_object(source_bucket, s3_key)

        # Invoke file_validation Lambda
        lambda_client = boto3_session.client("lambda")
        validation_event = {
            "source_bucket": source_bucket,
            "source_key": s3_key,
            "pipeline_id": pipeline_prefix,
        }

        result = _invoke_lambda(lambda_client, FILE_VALIDATION_LAMBDA, validation_event)
        logger.info(
            "file_validation result for large file: %s",
            json.dumps(result, default=str)[:500],
        )

        # The Lambda must not crash or timeout on a large file
        assert not result.get("_lambda_error"), f"file_validation Lambda crashed on >100MB file: {result}"

        # It should have detected the file size (file_validation does a HEAD request)
        # and either succeeded or returned a descriptive response
        logger.info(
            "Large file (%d MB) handled successfully by file_validation",
            total_bytes // (1024 * 1024),
        )
