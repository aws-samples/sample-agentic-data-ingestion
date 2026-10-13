
import sys
import logging
from awsglue.transforms import *
from awsglue.utils import getResolvedOptions
from pyspark.context import SparkContext
from awsglue.context import GlueContext
from awsglue.job import Job
from awsglue.dynamicframe import DynamicFrame
from pyspark.sql.functions import col, lit, trim, lower, to_date, to_timestamp, when, concat_ws, count as spark_count, regexp_replace, upper, initcap, substring, coalesce, length, split, array_contains, expr
from pyspark.sql.types import StringType, IntegerType, LongType, DoubleType, BooleanType, DateType, TimestampType

logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# Initialize Glue context
args = getResolvedOptions(sys.argv, [
    'JOB_NAME', 'source_path', 'database_name', 'table_name',
])
sc = SparkContext()
glue_context = GlueContext(sc)
spark = glue_context.spark_session
job = Job(glue_context)
job.init(args['JOB_NAME'], args)


try:
    # Validate source path
    source_path = args['source_path']
    if not source_path.startswith('s3://'):
        raise ValueError(f"Invalid source_path: {source_path} (must start with s3://)")

    # Read source data
    logger.info(f"Reading source data from {source_path}")

    df = spark.read.option("multiLine", True).json(source_path)


    source_count = df.count()
    logger.info(f"Source record count: {source_count}")
    if source_count == 0:
        logger.warning("Source data is empty — skipping transformation and write")
        job.commit()
        sys.exit(0)

    # === AI-Generated Transform Block ===

    df_transformed = df

    # === End Transform Block ===

    # === Auto-cast columns to match target Glue Catalog schema ===
    try:
        target_df = glue_context.create_dynamic_frame.from_catalog(
            database=args['database_name'], table_name=args['table_name']
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
    # === End Auto-cast ===

    # === Select only target columns ===
    try:
        target_table_name = f"{args['database_name']}.{args['table_name']}"
        target_columns = [f.name for f in spark.read.table(target_table_name).schema]
        available = [c for c in target_columns if c in df_transformed.columns]
        if available:
            logger.info(f"Selecting {len(available)} target columns from {len(df_transformed.columns)} available")
            df_transformed = df_transformed.select(*available)
    except Exception as _select_err:
        logger.info(f"Skipping column selection (target table may not exist yet): {_select_err}")
    # === End Select ===

    transformed_count = df_transformed.count()
    logger.info(f"Transformed record count: {transformed_count} (filtered {source_count - transformed_count} records)")

    # Convert to DynamicFrame and write to Glue Catalog

    dynamic_frame = DynamicFrame.fromDF(df_transformed, glue_context, "output")

    logger.info(f"Writing to Glue Catalog: {args['database_name']}.{args['table_name']}")

    glue_context.write_dynamic_frame.from_catalog(
        frame=dynamic_frame,
        database=args['database_name'],
        table_name=args['table_name'],
    )


    logger.info("Write to Glue Catalog complete")

except Exception as e:
    logger.error(f"ETL job failed: {str(e)}")
    raise
finally:
    job.commit()
