
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

    'watermark_column',
    'watermark_type',
    'watermarks_table_name',


    'target_table_bucket_arn',
    'target_namespace',
    'target_table_name',
    'target_warehouse_location',

])
sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args['JOB_NAME'], args)


# Configure Iceberg catalog for S3 Tables (target)
spark.conf.set("spark.sql.catalog.s3tablesbucket", "org.apache.iceberg.spark.SparkCatalog")
spark.conf.set("spark.sql.catalog.s3tablesbucket.catalog-impl", "software.amazon.s3tables.iceberg.S3TablesCatalog")
spark.conf.set("spark.sql.catalog.s3tablesbucket.warehouse", args['target_table_bucket_arn'])

target_table = f"s3tablesbucket.{args['target_namespace']}.{args['target_table_name']}"

# Cache target schema once (reused for auto-cast, column selection)
_target_schema = None
try:
    _target_schema = spark.read.table(target_table).schema
    logger.info(f"Target table schema loaded: {len(_target_schema)} fields")
except Exception:
    logger.info("Target table not yet in Spark catalog — schema will be inferred on first write")


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


    validate_identifier(args['watermark_column'])

    # Resolve the watermark lower bound for this run.
    #
    # Order of precedence (Req 7.3, 7.4):
    #   1. The `last_value` from the Watermarks_Table for this pipeline, if
    #      a previous run has populated one.
    #   2. The `initial_watermark` baked into the rendered template (set at
    #      pipeline-creation time from `source_config.initial_watermark`).
    #   3. None — first run with no initial watermark configured: the WHERE
    #      clause is omitted entirely and the full table is read.
    _watermarks_table = boto3.resource("dynamodb").Table(args['watermarks_table_name'])
    _wm_response = _watermarks_table.get_item(Key={'pipeline_id': args['pipeline_id']})
    _wm_item = _wm_response.get('Item')
    if _wm_item and _wm_item.get('last_value'):
        prev_value = str(_wm_item['last_value'])
    else:
        prev_value = "2024-01-01T00:00:00Z"

    if prev_value:
        validate_watermark_value(prev_value, args['watermark_type'])
        _predicate = build_watermark_predicate(
            "postgres",
            args['watermark_column'],
            args['watermark_type'],
            prev_value,
        )
        _query = f"(SELECT * FROM {_sch}.{_tbl} WHERE {_predicate}) AS subq"
        logger.info(f"Incremental read with watermark: {_predicate}")
        df = spark.read.format("jdbc") \
            .option("url", args['jdbc_url']) \
            .option("driver", args['jdbc_driver']) \
            .option("user", db_user) \
            .option("password", db_password) \
            .option("query", _query) \
            .load()
    else:
        logger.info("Incremental mode but no prior watermark — reading full table for first run")
        df = spark.read.format("jdbc") \
            .option("url", args['jdbc_url']) \
            .option("driver", args['jdbc_driver']) \
            .option("user", db_user) \
            .option("password", db_password) \
            .option("dbtable", f"{_sch}.{_tbl}") \
            .load()



    # Cache `df` BEFORE computing MAX (Req 7.6). Without `cache()`, Spark's
    # lazy evaluation would trigger a separate JDBC read for the MAX action
    # AND another for the transform — doubling source-database load.
    df = df.cache()

    # Compute the new watermark from the SOURCE DataFrame `df`, NOT
    # `df_transformed` (Req 7.6). Transforms may drop rows or rename columns;
    # we need the max from what was actually read so the next run picks up
    # exactly where this one left off.
    _wm_row = df.agg(spark_max(col(args['watermark_column'])).alias("_max_wm")).collect()
    new_watermark_value = _wm_row[0]["_max_wm"] if _wm_row else None


    source_count = df.count()
    logger.info(f"Source record count: {source_count}")
    if source_count == 0:
        logger.warning("Source data is empty — skipping transformation and write")

        # Per Req 7.7, leave the existing watermark unchanged on empty reads.
        df.unpersist()

        # NOTE: do NOT call job.commit() here. `sys.exit(0)` raises
        # SystemExit, which unwinds through the `finally:` block below —
        # and that block already calls job.commit() exactly once. Committing
        # here too would double-commit on the empty-data path.
        sys.exit(0)

    # === AI-Generated Transform Block ===
    df_transformed = df
    # === End Transform Block ===


    # === Auto-cast + column selection (single pass using cached schema) ===
    if _target_schema:
        for field in _target_schema:
            if field.name in df_transformed.columns:
                source_type = df_transformed.schema[field.name].dataType
                if source_type != field.dataType:
                    logger.info(f"Casting {field.name}: {source_type} -> {field.dataType}")
                    if isinstance(field.dataType, DateType):
                        df_transformed = df_transformed.withColumn(
                            field.name,
                            when(col(field.name).isNull(), lit(None).cast(DateType()))
                            .otherwise(to_date(col(field.name)))
                        )
                    elif isinstance(field.dataType, TimestampType):
                        df_transformed = df_transformed.withColumn(
                            field.name,
                            when(col(field.name).isNull(), lit(None).cast(TimestampType()))
                            .otherwise(to_timestamp(col(field.name)))
                        )
                    else:
                        df_transformed = df_transformed.withColumn(field.name, col(field.name).cast(field.dataType))
        target_columns = [f.name for f in _target_schema if f.name in df_transformed.columns]
        if target_columns:
            logger.info(f"Selecting {len(target_columns)} target columns from {len(df_transformed.columns)} available")
            df_transformed = df_transformed.select(*target_columns)
    # === End Auto-cast + Select ===


    transformed_count = df_transformed.count()
    logger.info(f"Transformed record count: {transformed_count} (filtered {source_count - transformed_count} records)")



    # Write to Iceberg table via S3 Tables
    logger.info(f"Writing to Iceberg table: {target_table}")

    try:

        df_transformed.writeTo(target_table).using("iceberg").append()

    except Exception as _append_err:
        if "TABLE_OR_VIEW_NOT_FOUND" in str(_append_err):
            logger.info("Table not yet in Spark catalog — creating with initial write")

            df_transformed.writeTo(target_table).using("iceberg").createOrReplace()

        else:
            raise


    logger.info("Write complete")

    # Record count reconciliation
    try:
        output_count = spark.read.table(target_table).count()
        if output_count != transformed_count:
            logger.warning(f"Record count mismatch: transformed={transformed_count}, written={output_count}")
        else:
            logger.info(f"Record count verified: {output_count}")
    except Exception:
        pass




    # === Watermark update — runs only after the target write succeeds ===
    #
    # Conditional PutItem ensures an out-of-order completion (an older run
    # finishing AFTER a newer run) cannot overwrite a newer watermark
    # (Req 7.6). The condition uses `last_updated_at` (always set on every
    # write) rather than `last_value` (whose ordering is type-dependent —
    # numeric vs ISO 8601 string — and which DDB cannot compare across types).
    if new_watermark_value is not None:
        _now_iso = datetime.now(timezone.utc).isoformat()
        try:
            _watermarks_table.put_item(
                Item={
                    'pipeline_id': args['pipeline_id'],
                    'watermark_column': args['watermark_column'],
                    'watermark_type': args['watermark_type'],
                    'last_value': str(new_watermark_value),
                    'last_updated_at': _now_iso,
                    'last_run_id': args['run_id'],
                },
                ConditionExpression='attribute_not_exists(last_updated_at) OR last_updated_at < :now',
                ExpressionAttributeValues={':now': _now_iso},
            )
            logger.info(f"Watermark updated: {args['watermark_column']} = {new_watermark_value}")
        except Exception as _wm_err:
            # ConditionalCheckFailedException is expected for out-of-order
            # completion — log and continue rather than failing the run,
            # since the target write already succeeded.
            if 'ConditionalCheckFailed' in str(type(_wm_err).__name__):
                logger.warning(
                    f"Watermark not updated — a newer run already advanced it: {_wm_err}"
                )
            else:
                logger.error(f"Watermark update failed: {_wm_err}")
                raise
    else:
        logger.info("No new watermark value computed (empty source) — leaving existing watermark unchanged")

    df.unpersist()


except Exception as e:
    logger.error(f"ETL job failed: {str(e)}")
    raise
finally:
    job.commit()
