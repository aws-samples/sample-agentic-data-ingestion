
import sys
import json
import logging
from datetime import datetime, timezone

import boto3
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, lit, trim, lower, to_date, to_timestamp, when, concat_ws, count as spark_count, regexp_replace, upper, initcap, substring, coalesce, length, split, array_contains, expr, max as spark_max
from pyspark.sql.types import StringType, IntegerType, LongType, DoubleType, BooleanType, DateType, TimestampType


# `jdbc_helpers` is shipped to the Glue Job classpath via `--extra-py-files`
# (the same shared module that powers wizard-time validation in the
# dashboard API Lambda). Re-validating identifiers and watermark values at
# job runtime is mandatory defence-in-depth (Req 7.5) because Spark JDBC
# does NOT support `?` bind parameters on the `query` option.
from jdbc_helpers import (
    validate_identifier,
    validate_watermark_value,
    quote_identifier,
    build_watermark_predicate,
)

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# `getResolvedOptions` requires every declared key to be present in
# `sys.argv`. The keys list is built per render so that incremental-only
# and target-specific keys are only declared when applicable (Req 8.3).
args = getResolvedOptions(sys.argv, [
    'JOB_NAME',
    'jdbc_url',
    'jdbc_driver',
    'jdbc_schema',
    'jdbc_table',
    'secret_arn',
    'read_mode',
    'pipeline_id',
    'run_id',


    'target_jdbc_url',
    'target_jdbc_driver',
    'target_jdbc_table',
    'target_secret_arn',

])
sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args['JOB_NAME'], args)

# Optional runtime arguments (P28 R19.9): resolved via getResolvedOptions,
# gated on sys.argv presence (getResolvedOptions raises on missing args and
# manual job starts pass no Arguments).
def _runtime_arg(_name, _default=""):
    if f"--{_name}" in sys.argv:
        return getResolvedOptions(sys.argv, [_name])[_name]
    return _default

# Manual runs fall back to the Glue-provided JOB_RUN_ID (already parsed into
# `args` by getResolvedOptions as a reserved optional argument) so per-run S3
# locations never collapse into a shared 'default'.
_adi_run_id = _runtime_arg("run_id")
_adi_effective_run_id = _adi_run_id or args.get("JOB_RUN_ID") or "unknown"




try:
    # === SOURCE READ — fetch credentials, validate identifiers, build query ===
    secret_payload = boto3.client("secretsmanager").get_secret_value(
        SecretId=args['secret_arn']
    )
    secret = json.loads(secret_payload["SecretString"])
    db_user = secret["username"]
    db_password = secret["password"]

    # Re-validate identifiers at runtime (defence-in-depth — Req 7.5).
    validate_identifier(args['jdbc_schema'])
    validate_identifier(args['jdbc_table'])

    # Engine-aware identifier quoting. `engine` is baked in at Jinja2 render
    # time — MySQL/MariaDB/Aurora-MySQL produce backticks; everything else
    # produces double quotes.
    _sch = quote_identifier("postgres", args['jdbc_schema'])
    _tbl = quote_identifier("postgres", args['jdbc_table'])


    # Full-table read.
    df = spark.read.format("jdbc") \
        .option("url", args['jdbc_url']) \
        .option("driver", args['jdbc_driver']) \
        .option("user", db_user) \
        .option("password", db_password) \
        .option("dbtable", f"{_sch}.{_tbl}") \
        .load()




    source_count = df.count()
    logger.info(f"Source record count: {source_count}")
    if source_count == 0:
        logger.warning("Source data is empty — skipping transformation and write")
        # p37 R1.1/R1.11: record Rows_Read / Rows_Written for this run
        # (stage: empty_source). Best-effort telemetry (R1.13) — a failure
        # here must NOT fail a job that has already written to the target, so the
        # whole block is wrapped like the DQ state write. Reuses the existing
        # source_count / transformed_count locals — this block counts nothing itself.
        try:
            import boto3 as _adi_lc_boto3
            from botocore.config import Config as _adi_lc_Config
            from datetime import datetime as _adi_lc_dt, timezone as _adi_lc_tz

            # Bounded, single attempt: this job may have no route to DynamoDB, and an
            # unbounded client turns a best-effort metric into minutes of stalled time.
            _adi_lc_ddb_config = _adi_lc_Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})
            # run_id comes from _runtime_args; pipeline_id and state_table are
            # resolved here through the same _runtime_arg helper (_runtime_args
            # resolves run_id only).
            _adi_lc_pipeline_id = _runtime_arg("pipeline_id")
            _adi_lc_state_table = _runtime_arg("state_table", "agentic_data_ingestion_state")
            # int()/bool() are explicit so no float and no py4j numeric can reach
            # DynamoDB — the row is ints, strings and bools only.
            _adi_lc_rows_read = int(source_count)
            _adi_lc_rows_written = int(0)
            _adi_lc_write_confirmed = bool(False)
            if _adi_run_id and _adi_lc_pipeline_id:
                _adi_lc_boto3.resource("dynamodb", config=_adi_lc_ddb_config).Table(_adi_lc_state_table).put_item(
                    Item={
                        "run_id": _adi_run_id,
                        "step_name": "load_metrics",
                        # Both spellings, same value, as run_pipeline writes them
                        # (run_pipeline/lambda_function.py:161-163).
                        # execution_updater prefers original_pipeline_id (:173).
                        "pipeline_id": _adi_lc_pipeline_id,
                        "original_pipeline_id": _adi_lc_pipeline_id,
                        "rows_read": _adi_lc_rows_read,
                        "rows_written": _adi_lc_rows_written,
                        "write_confirmed": _adi_lc_write_confirmed,
                        "source": "glue_job",
                        "timestamp": _adi_lc_dt.now(_adi_lc_tz.utc).isoformat(),
                    }
                )
                logger.info(
                    f"Load metrics recorded: rows_read={_adi_lc_rows_read}, "
                    f"rows_written={_adi_lc_rows_written}, "
                    f"write_confirmed={_adi_lc_write_confirmed}"
                )
            else:
                logger.info(
                    "Load metrics not recorded: no --run_id/--pipeline_id "
                    "(manual job start) — Load_Outcome reads not_established"
                )
        except Exception as _adi_lc_err:
            logger.warning(f"Failed to write load metrics to DynamoDB: {_adi_lc_err}")

        # NOTE: do NOT call job.commit() here. `sys.exit(0)` raises
        # SystemExit, which unwinds through the `finally:` block below —
        # and that block already calls job.commit() exactly once. Committing
        # here too would double-commit on the empty-data path.
        sys.exit(0)


    # === p53 C1: bounded source sample. At most 100 rows of the frame already
    # read, under 1 MiB, as ONE JSON array object — the shape every downstream
    # consumer can read. Rationale and fences: the Jinja comment on this block
    # in pyspark_templates/glue_job_jdbc_source.py.j2.
    #
    # Best-effort (R1.6): a telemetry failure must NOT fail a run whose read
    # succeeded, so the whole block is wrapped. The SUCCESS path logs at INFO
    # too, so "no sample ever appeared" is diagnosable from this log rather
    # than by inference (a subnet with no S3 endpoint fails it silently).
    try:
        # 1 MiB, the ceiling create_code's sample reader stops at.
        _ADI_SAMPLE_MAX_BYTES = 1048576

        # Account and region are neither hardcoded nor a new job argument: an
        # ARN is arn:aws:secretsmanager:<region>:<account>:secret:name, so
        # segments 3 and 4 of --secret_arn (already held, already used above)
        # are what `adi-processed-files-<account>-<region>` needs. No API call,
        # so no VPC-endpoint dependency beyond the S3 write's own.
        _adi_smp_arn = args['secret_arn'].split(":")
        _adi_smp_region = _adi_smp_arn[3] if len(_adi_smp_arn) > 4 else ""
        _adi_smp_account = _adi_smp_arn[4] if len(_adi_smp_arn) > 4 else ""
        _adi_smp_pipeline_id = args['pipeline_id']

        if _adi_smp_region and _adi_smp_account and _adi_smp_pipeline_id:
            # Deterministic key, overwritten each run. schema_extract probes
            # exactly this location, so the key is a contract.
            _adi_smp_bucket = f"adi-processed-files-{_adi_smp_account}-{_adi_smp_region}"
            _adi_smp_key = f"samples/{_adi_smp_pipeline_id}/source-sample.json"

            def _adi_smp_value(_v):
                # Scalars keep their JSON type so the sample frame gets real
                # numbers. Everything else — date, timestamp, Decimal, bytes,
                # struct, array — becomes its string form (JSON has no
                # timestamp type, and the date-format check WANTS the textual
                # form). Non-finite floats become null: NaN and Infinity are
                # not valid JSON.
                if _v is None or isinstance(_v, (bool, int, str)):
                    return _v
                if isinstance(_v, float):
                    return _v if float("-inf") < _v < float("inf") else None
                return str(_v)

            # At most 100 rows, off the frame already read. Bound first, then
            # collect — nothing here materialises the full frame.
            _adi_smp_rows = []
            for _adi_smp_row in df.limit(100).collect():
                _adi_smp_clean = {}
                for _adi_smp_k, _adi_smp_v in _adi_smp_row.asDict(recursive=True).items():
                    try:
                        _adi_smp_clean[str(_adi_smp_k)] = _adi_smp_value(_adi_smp_v)
                    except Exception:
                        # A field that will not coerce is OMITTED, not fatal.
                        # A partial sample is useful; a failed write is not.
                        pass
                _adi_smp_rows.append(_adi_smp_clean)

            # Drop WHOLE rows from the end until the serialised array is under
            # the reader's ceiling. json.dumps cannot raise here — every value
            # is already None/bool/int/float/str.
            _adi_smp_body = json.dumps(_adi_smp_rows)
            while _adi_smp_rows and len(_adi_smp_body.encode("utf-8")) >= _ADI_SAMPLE_MAX_BYTES:
                _adi_smp_rows.pop()
                _adi_smp_body = json.dumps(_adi_smp_rows)
            _adi_smp_payload = _adi_smp_body.encode("utf-8")

            boto3.client("s3").put_object(
                Bucket=_adi_smp_bucket,
                Key=_adi_smp_key,
                Body=_adi_smp_payload,
                ContentType="application/json",
            )
            logger.info(
                f"Source sample written: {len(_adi_smp_rows)} rows, "
                f"{len(_adi_smp_payload)} bytes -> "
                f"s3://{_adi_smp_bucket}/{_adi_smp_key}"
            )
        else:
            logger.warning(
                "Source sample not written: could not derive the sample "
                "location from --secret_arn / --pipeline_id"
            )
    except Exception as _adi_smp_err:
        logger.warning(f"Failed to write source sample: {_adi_smp_err}")
    # === End p53 C1 ===

    # === AI-Generated Transform Block ===
    df_transformed = df
    # === End Transform Block ===



    # === Glue Data Quality Evaluation ===

    # === End Data Quality ===

    transformed_count = df_transformed.count()
    logger.info(f"Transformed record count: {transformed_count} (filtered {source_count - transformed_count} records)")



    # Write to JDBC target
    # Credentials are fetched from Secrets Manager at run time, by the reference
    # passed as a job argument. Glue job arguments are visible in the console,
    # CloudTrail and job-run history, so the reference is all they ever hold.
    _target_secret = json.loads(
        boto3.client("secretsmanager").get_secret_value(
            SecretId=args['target_secret_arn']
        )["SecretString"]
    )
    jdbc_properties = {
        "user": _target_secret["username"],
        "password": _target_secret["password"],
        "driver": args['target_jdbc_driver'],
    }
    logger.info(f"Writing to JDBC table: {args['target_jdbc_table']}")

    df_transformed.write.jdbc(
        url=args['target_jdbc_url'],
        table=args['target_jdbc_table'],
        mode="overwrite",
        properties=jdbc_properties,
    )


    logger.info(f"Write to JDBC target complete ({transformed_count} records)")



    # p37 R1.1/R1.11: record Rows_Read / Rows_Written for this run
    # (stage: post_write). Best-effort telemetry (R1.13) — a failure
    # here must NOT fail a job that has already written to the target, so the
    # whole block is wrapped like the DQ state write. Reuses the existing
    # source_count / transformed_count locals — this block counts nothing itself.
    try:
        import boto3 as _adi_lc_boto3
        from botocore.config import Config as _adi_lc_Config
        from datetime import datetime as _adi_lc_dt, timezone as _adi_lc_tz

        # Bounded, single attempt: this job may have no route to DynamoDB, and an
        # unbounded client turns a best-effort metric into minutes of stalled time.
        _adi_lc_ddb_config = _adi_lc_Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 0})
        # run_id comes from _runtime_args; pipeline_id and state_table are
        # resolved here through the same _runtime_arg helper (_runtime_args
        # resolves run_id only).
        _adi_lc_pipeline_id = _runtime_arg("pipeline_id")
        _adi_lc_state_table = _runtime_arg("state_table", "agentic_data_ingestion_state")
        # int()/bool() are explicit so no float and no py4j numeric can reach
        # DynamoDB — the row is ints, strings and bools only.
        _adi_lc_rows_read = int(source_count)
        _adi_lc_rows_written = int(transformed_count)
        _adi_lc_write_confirmed = bool(True)
        if _adi_run_id and _adi_lc_pipeline_id:
            _adi_lc_boto3.resource("dynamodb", config=_adi_lc_ddb_config).Table(_adi_lc_state_table).put_item(
                Item={
                    "run_id": _adi_run_id,
                    "step_name": "load_metrics",
                    # Both spellings, same value, as run_pipeline writes them
                    # (run_pipeline/lambda_function.py:161-163).
                    # execution_updater prefers original_pipeline_id (:173).
                    "pipeline_id": _adi_lc_pipeline_id,
                    "original_pipeline_id": _adi_lc_pipeline_id,
                    "rows_read": _adi_lc_rows_read,
                    "rows_written": _adi_lc_rows_written,
                    "write_confirmed": _adi_lc_write_confirmed,
                    "source": "glue_job",
                    "timestamp": _adi_lc_dt.now(_adi_lc_tz.utc).isoformat(),
                }
            )
            logger.info(
                f"Load metrics recorded: rows_read={_adi_lc_rows_read}, "
                f"rows_written={_adi_lc_rows_written}, "
                f"write_confirmed={_adi_lc_write_confirmed}"
            )
        else:
            logger.info(
                "Load metrics not recorded: no --run_id/--pipeline_id "
                "(manual job start) — Load_Outcome reads not_established"
            )
    except Exception as _adi_lc_err:
        logger.warning(f"Failed to write load metrics to DynamoDB: {_adi_lc_err}")



except Exception as e:
    logger.error(f"ETL job failed: {str(e)}")
    raise
finally:
    job.commit()
