
import sys
import logging
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from pyspark.sql.functions import col, lit, trim, lower, to_date, to_timestamp, when, concat_ws, count as spark_count, regexp_replace, upper, initcap, substring, coalesce, length, split, array_contains, expr
from pyspark.sql.types import StringType, IntegerType, LongType, DoubleType, BooleanType, DateType, TimestampType

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Initialize Glue context
args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'source_path', 'output_path',
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
    # Validate paths
    source_path = args['source_path']
    output_path = args['output_path']
    if not source_path.startswith('s3://'):
        raise ValueError(f"Invalid source_path: {source_path} (must start with s3://)")
    if not output_path.startswith('s3://'):
        raise ValueError(f"Invalid output_path: {output_path} (must start with s3://)")

    # Read source data
    logger.info(f"Reading source data from {source_path}")

    df = spark.read.option("multiLine", True).json(source_path)


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
        job.commit()
        sys.exit(0)

    # === AI-Generated Transform Block ===

    df_transformed = df

    # === End Transform Block ===

    # === Glue Data Quality Evaluation ===

    # === End Data Quality ===

    transformed_count = df_transformed.count()
    logger.info(f"Transformed record count: {transformed_count} (filtered {source_count - transformed_count} records)")

    # Write to S3 as Parquet
    logger.info(f"Writing to S3 path: {output_path}")


    df_transformed.write.mode("overwrite").parquet(output_path)



    logger.info(f"Write to S3 Parquet complete ({transformed_count} records)")
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
