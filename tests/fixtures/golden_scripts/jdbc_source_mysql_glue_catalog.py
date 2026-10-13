
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

from awsglue.dynamicframe import DynamicFrame


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


    'target_database_name',
    'target_table_name',

])
sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args['JOB_NAME'], args)



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
    _sch = quote_identifier("mysql", args['jdbc_schema'])
    _tbl = quote_identifier("mysql", args['jdbc_table'])


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

        # NOTE: do NOT call job.commit() here. `sys.exit(0)` raises
        # SystemExit, which unwinds through the `finally:` block below —
        # and that block already calls job.commit() exactly once. Committing
        # here too would double-commit on the empty-data path.
        sys.exit(0)

    # === AI-Generated Transform Block ===
    df_transformed = df
    # === End Transform Block ===


    # === Auto-cast columns to match target Glue Catalog schema ===
    try:
        target_df = glue_context.create_dynamic_frame.from_catalog(
            database=args['target_database_name'], table_name=args['target_table_name']
        ).toDF()
        if target_df.schema:
            for field in target_df.schema:
                if field.name in df_transformed.columns:
                    source_type = df_transformed.schema[field.name].dataType
                    if source_type != field.dataType:
                        logger.info(f"Casting {field.name}: {source_type} -> {field.dataType}")
                        df_transformed = df_transformed.withColumn(field.name, col(field.name).cast(field.dataType))
    except Exception as _cast_err:
        logger.info(f"Skipping auto-cast (target table may not exist yet): {_cast_err}")

    try:
        target_table_name = f"{args['target_database_name']}.{args['target_table_name']}"
        target_columns = [f.name for f in spark.read.table(target_table_name).schema]
        available = [c for c in target_columns if c in df_transformed.columns]
        if available:
            logger.info(f"Selecting {len(available)} target columns from {len(df_transformed.columns)} available")
            df_transformed = df_transformed.select(*available)
    except Exception as _select_err:
        logger.info(f"Skipping column selection (target table may not exist yet): {_select_err}")
    # === End Auto-cast + Select ===


    transformed_count = df_transformed.count()
    logger.info(f"Transformed record count: {transformed_count} (filtered {source_count - transformed_count} records)")



    # Convert to DynamicFrame and write to Glue Catalog

    dynamic_frame = DynamicFrame.fromDF(df_transformed, glue_context, "output")

    logger.info(f"Writing to Glue Catalog: {args['target_database_name']}.{args['target_table_name']}")

    glue_context.write_dynamic_frame.from_catalog(
        frame=dynamic_frame,
        database=args['target_database_name'],
        table_name=args['target_table_name'],
        additional_options={"enableUpdateCatalog": True},
    )


    logger.info("Write to Glue Catalog complete")





except Exception as e:
    logger.error(f"ETL job failed: {str(e)}")
    raise
finally:
    job.commit()
