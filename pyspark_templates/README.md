# PySpark Templates

Jinja2 templates for AWS Glue ETL job code generation. The `agentic_data_ingestion_create_code` Lambda uses these templates to produce complete PySpark scripts — the AI generates only the transform block (5-15 lines) while templates handle all boilerplate.

## Template-First Architecture

Instead of asking the AI to generate entire 500+ line scripts, the system splits code generation into two layers:

1. **Static scaffold** (Jinja2 template, deterministic) — imports, GlueContext init, arg parsing, source read, target write, error handling, `job.commit()`
2. **AI-generated transform block** — field renames, type casts, derived columns, filters (5-15 lines)

Each template contains a `{{ transform_block }}` placeholder. The AI writes code that reads from `df` and produces `df_transformed`.

## Jinja2 Templates

### `glue_job_s3_tables.py.j2`

Target: **S3 Tables (Iceberg)**

Handles Iceberg catalog configuration (`spark.conf.set` for `SparkCatalog` and `S3TablesCatalog`), source read (JSON/CSV/Parquet aware), transform block execution, and `df_transformed.writeTo(table).using("iceberg").createOrReplace()`.

**Job parameters**: `JOB_NAME`, `source_path`, `table_bucket_arn`, `namespace`, `table_name`

Note: The Iceberg catalog `warehouse` config uses `table_bucket_arn` (the ARN), not `warehouse_location` (an S3 URI).

### `glue_job_glue_catalog.py.j2`

Target: **Glue Data Catalog**

Handles source read, transform block execution, and write via `glue_context.write_dynamic_frame.from_catalog(database, table)` with Parquet/ORC format options.

**Job parameters**: `JOB_NAME`, `source_path`, `database_name`, `table_name`

### `glue_job_jdbc.py.j2`

Target: **JDBC database**

Handles source read, transform block execution, and write via `df_transformed.write.jdbc(url, table, properties)`, through the shared `_partials/_write_jdbc.py.j2` partial (the same JDBC write `glue_job_jdbc_source.py.j2` uses, there with the `target_` prefix). URL, table and driver come from Glue job parameters. The credential does not: the job parameter `secret_arn` is a Secrets Manager reference, and the write fetches `username` / `password` with `get_secret_value` at run time. Plaintext credentials never appear in job parameters (standing invariant).

**Job parameters**: `JOB_NAME`, `source_path`, `jdbc_url`, `jdbc_table`, `jdbc_driver`, `secret_arn`

### `glue_job_s3_parquet.py.j2`

Target: **S3 (Parquet/CSV)**

Handles source read, transform block execution, and write via `df_transformed.write.parquet(output_path)` or `.csv()`. Supports partition columns and compression options.

**Job parameters**: `JOB_NAME`, `source_path`, `output_path`, `output_format`, `partition_columns` (optional)

## The `{{ transform_block }}` Placeholder

Each template includes this section:

```jinja2
    # === AI-Generated Transform Block ===
    {{ transform_block }}
    # === End Transform Block ===
```

The AI generates PySpark code that:
- Reads from input DataFrame `df`
- Produces output DataFrame `df_transformed`
- Uses one frame operation per statement, one statement per line — the shape `shared/prompt_templates/transform_generation.j2` asks for and the shape the pandas proxy checker converts
- Uses only the constructs listed under [What the pandas proxy converts](#what-the-pandas-proxy-converts) — that list is the converter's own vocabulary, not a restatement of it
- Does NOT create classes, helper functions, or imports

Example transform block:
```python
df_transformed = df.withColumnRenamed("customer_id", "cust_id")
df_transformed = df_transformed.withColumn("fname", trim(col("first_name")))
df_transformed = df_transformed.withColumn("signup_date", to_date(col("signup_date"), "yyyy-MM-dd"))
df_transformed = df_transformed.drop("internal_notes")
df_transformed = df_transformed.select("cust_id", "fname", "signup_date")
```

Each statement stands alone. This example used to be a parenthesised fluent chain
mixing `withColumnRenamed`, `withColumn` and `drop`, which the checker refuses —
a chain that mixes frame operations has no single-frame pandas expression, so it
refuses at its first non-composable step and names it. `tests/unit/templates/test_pyspark_readme_examples.py`
runs the block above through the real converter, so the worked example cannot
drift from what the checker accepts.

## Template Selection

The `create_code` Lambda selects the template via `TEMPLATE_MAP`:

```python
TEMPLATE_MAP = {
    's3_tables': 'glue_job_s3_tables.py.j2',
    'glue_catalog': 'glue_job_glue_catalog.py.j2',
    'jdbc': 'glue_job_jdbc.py.j2',
    's3_parquet': 'glue_job_s3_parquet.py.j2',
}
```

## Common Template Structure

All four templates follow the same structure:

```
1. Imports (awsglue, pyspark, pyspark.sql.functions, pyspark.sql.types)
2. Logging configuration
3. Job parameter resolution (getResolvedOptions)
4. GlueContext / SparkSession initialization
5. Target-specific configuration (e.g., Iceberg catalog for s3_tables)
6. Source read (format-aware: JSON multiLine, CSV with header, Parquet)
7. {{ transform_block }} placeholder
8. Target write (type-specific)
9. Record count verification logging
10. Error handling (try/except with logging)
11. job.commit() in finally block
```

## Parameter Contract

The template's `getResolvedOptions` parameter names are the **single source of truth** for Glue job `DefaultArguments` keys. The `create_glue_pipeline` Lambda MUST set `DefaultArguments` with `--{param}` keys that exactly match the names in `getResolvedOptions`. Mismatches cause `GlueArgumentError` at runtime.

### Rules

1. All parameter names use **lowercase_snake_case** (e.g., `source_path`, `table_bucket_arn`)
2. `getResolvedOptions` is **case-sensitive** — `--TABLE_BUCKET_ARN` ≠ `--table_bucket_arn`
3. `JOB_NAME` is provided by Glue automatically — do not include it in `DefaultArguments`
4. Every parameter listed in `getResolvedOptions` (except `JOB_NAME`) must have a non-empty value in `DefaultArguments`

### Per-Template Contract

| Template | `getResolvedOptions` Parameters |
|----------|-------------------------------|
| `glue_job_s3_tables.py.j2` | `JOB_NAME`, `source_path`, `table_bucket_arn`, `namespace`, `table_name` |
| `glue_job_glue_catalog.py.j2` | `JOB_NAME`, `source_path`, `database_name`, `table_name` |
| `glue_job_jdbc.py.j2` | `JOB_NAME`, `source_path`, `jdbc_url`, `jdbc_table`, `jdbc_driver`, `secret_arn` |
| `glue_job_s3_parquet.py.j2` | `JOB_NAME`, `source_path`, `output_path` |

### How It Works

```
Template defines:     getResolvedOptions(sys.argv, ['source_path', 'table_bucket_arn', ...])
                                    ↓
Extractor reads:      parameter_extractor.py parses the rendered code, extracts param names
                                    ↓
Builders map:         build_<target>_arguments() emits a value for each name from pipeline config
                                    ↓
Validator checks:     validate_required_parameters() ensures all params present and non-empty
                                    ↓
Glue job receives:    DefaultArguments = {'--source_path': '...', '--table_bucket_arn': '...'}
                                    ↓
PySpark reads:        args = getResolvedOptions(sys.argv, [...])  ← names match, no error
```

### Contract Tests

`tests/test_glue_parameter_contract.py` validates the contract for all 4 templates. For each template it:
1. Renders the template with mock data
2. Extracts `getResolvedOptions` parameter names
3. Builds mock `DefaultArguments` using the corresponding `build_*_arguments()` function
4. Verifies every required parameter is present and non-empty

Run: `python -m pytest tests/test_glue_parameter_contract.py -v`

## Adding a New Target Type

1. Create `glue_job_{target_type}.py.j2` in this directory
2. Follow the common structure above
3. Define `getResolvedOptions` with lowercase_snake_case parameter names
4. Include the `{{ transform_block }}` placeholder between source read and target write
5. Add the target type to `TEMPLATE_MAP` in `lambda/agentic_data_ingestion_create_code/lambda_function.py`
6. Add a `build_{target_type}_arguments()` function in `lambda/agentic_data_ingestion_create_glue_pipeline/job_arguments.py` (re-exported by `lambda_function.py`) — keys MUST match `getResolvedOptions` names exactly. A credential is passed only as a Secrets Manager reference the job resolves at run time, never as a value
7. Add a contract test in `tests/test_glue_parameter_contract.py` verifying template params match `DefaultArguments`
8. Add a unit test in `tests/test_pyspark_templates.py` verifying the template renders valid Python
9. Run `python -m pytest tests/test_glue_parameter_contract.py -v` to validate the contract

## Legacy Templates (Deprecated)

The following files are legacy templates from before the Jinja2 migration. They are no longer used at runtime:

- `base_template.py` — old scaffold with placeholder comments (not Jinja2)
- `read_parquet_v1.py` — Parquet read snippet
- `write_s3_tables_v1.py` — S3 Tables write snippet using `{{region}}`/`{{account_id}}` mustache-style placeholders

These files are retained for reference but are not loaded by any Lambda.

## Last Updated

February 22, 2026 | Version 3.0

---

## Transform Block Testing (pandas proxy)

Transform blocks are tested via **AgentCore Code Interpreter** using pandas conversion:

1. `PySpark2PandasConverter` (in `lambda/agentic_data_ingestion_test_code/pyspark_to_pandas.py`) parses the transform block as Python and translates each statement to its pandas equivalent
2. Code Interpreter executes the converted code against generated test data (500 rows, 5% null injection)
3. Validation checks: record counts, null rates, schema compliance, data quality scores

What runs there is a **proxy** for the Glue job, not the Glue job: pandas semantics
over generated sample data. A block that converts and executes cleanly has not been
run against Spark.

### Statement shapes

The converter walks each statement's call chain, so what it accepts is a shape rule
rather than a line-matching rule. Two grammars convert; everything else refuses:

| Shape | Example | Outcome |
|---|---|---|
| Any number of chained `withColumn` calls, optionally closed by one `select` | `df_transformed = df.withColumn("fname", trim(col("first_name"))).withColumn("lname", trim(col("last_name")))` | converts |
| The same shape with the trailing `select` | `df_transformed = df.withColumn("fname", trim(col("first_name"))).select("fname")` | converts |
| One `withColumnRenamed`, `drop`, `filter`, `select` or `withColumn` owning the whole statement | `df_transformed = df.withColumnRenamed("customer_id", "cust_id")` | converts |
| A chain that mixes frame operations | `df_transformed = df.withColumnRenamed("a", "b").withColumn("c", trim(col("b")))` | refuses `withColumn` |
| Column literals in single quotes | `df_transformed = df.withColumn('fname', trim(col('first_name')))` | refuses `withColumn` |
| A construct with no single-frame pandas equivalent | `df_transformed = df.groupBy("state").agg(count("*"))` | refuses `groupBy` |

The single-quote fence is deliberate: the generation prompt emits double quotes, so
a block that does not is a block this checker has never verified. Splitting a mixed
chain into one statement per line converts — that is what the worked example above
does.

### What the pandas proxy converts

These lists are the converter's own vocabulary, read out of the named frozensets in
`pyspark_to_pandas.py`. `tests/unit/templates/test_pyspark_readme_examples.py`
compares both sides and fails if they drift, so this document cannot go stale
against the code again.

- Functions with a translation rule (`_SUPPORTED_FUNCTIONS`): `col`, `concat_ws`, `lit`, `lower`, `regexp_replace`, `to_date`, `to_timestamp`, `trim`, `upper`, `when`
- Methods with a translation rule (`_SUPPORTED_METHODS`): `alias`, `cast`, `drop`, `filter`, `isNotNull`, `isNull`, `otherwise`, `rlike`, `select`, `when`, `withColumn`, `withColumnRenamed`

Method support is position-dependent — `isNull`, `isNotNull` and `rlike` only mean
something as a `filter` predicate, and `otherwise` only as the tail of a `when`
chain. Chained `when(...).when(...).otherwise(...)` converts, with every branch
preserved in order, and `otherwise` accepts a column reference as well as a literal.

### What the pandas proxy refuses

- Functions (`_UNSUPPORTED_FUNCTIONS`): `Window`, `array`, `array_contains`, `avg`, `coalesce`, `collect_list`, `collect_set`, `concat`, `count`, `current_date`, `current_timestamp`, `date_format`, `datediff`, `dayofmonth`, `dense_rank`, `explode`, `expr`, `initcap`, `lag`, `lead`, `length`, `max`, `min`, `month`, `posexplode`, `rank`, `regexp_extract`, `row_number`, `spark_count`, `spark_max`, `split`, `struct`, `substring`, `sum`, `udf`, `year`
- Methods (`_UNSUPPORTED_METHODS`): `agg`, `cache`, `coalesce`, `collect`, `count`, `createOrReplaceTempView`, `crossJoin`, `distinct`, `dropDuplicates`, `groupBy`, `join`, `na`, `orderBy`, `over`, `partitionBy`, `rdd`, `repartition`, `sort`, `union`, `unionAll`, `unionByName`, `unpersist`, `writeTo`
- Spark entry points (`_UNSUPPORTED_NAMES`): `F`, `StructField`, `functions`, `sc`, `spark`, `sqlContext`

The generation prompt's allowed-function list is now exactly the
`_SUPPORTED_FUNCTIONS` set above: p49 task 4.2 removed `initcap`, `substring`,
`coalesce`, `length`, `split` and `expr` from `transform_generation.j2` and states
the caveat at the point of offer. Two things still name refused functions to the
model — `spark_count` (the alias the scaffolds import `count` under, carried by a
naming rule rather than an offer), and an `Only if forced` rule giving correct
spellings for `substring`, `initcap`, `expr` and `length` for the case a mapping
hint forces one. The scaffolds import all of them at module scope regardless, so
they stay bound in the generated job. Generated code that uses them is not
necessarily wrong; it is unverified by the proxy.

The refusal lists are not the whole story, and deliberately so. **Any bare name
called in the transform block that is not in `_SUPPORTED_FUNCTIONS` refuses**,
whether or not it is enumerated above. A bare name inside a transform block can
only have come from `pyspark.sql.functions` — that is the sole function import the
scaffolds provide, and the prompt forbids the model from writing its own — so an
unrecognised one is a gap in this converter rather than something to pass through.
Before that default existed, such a name was emitted verbatim and the Code
Interpreter raised `NameError` on it, which is a conversion that "succeeded" and
then failed at runtime. `_UNSUPPORTED_FUNCTIONS` still earns its keep: membership
is what lets a refusal name the leftmost construct in a statement carrying several,
and what puts the name in this document. Method position has no equivalent default
— a receiver can be any object — so `_UNSUPPORTED_METHODS` is the only fence there.

### What happens when it refuses

Conversion is all-or-nothing. A construct the converter cannot express raises
`ConversionError(construct, line, line_number)` from `convert()`, with a message of
the form `unconvertible PySpark construct '<construct>' on line <n>: <line>`. For a
chain spread over several physical lines the number is the line the construct's own
name sits on, not the line the statement starts on.

Nothing is ever commented out, dropped, or partially translated. Earlier versions of
this document described unconvertible patterns as being wrapped in commented-out
markers in the converted code; no such behaviour exists, and no path returns a
partial conversion.

Downstream, a refusal is classified as *not verified by the pandas proxy* rather
than as a failed execution, and `code_interpreter_runner.classify_conversion_refusal`
splits the two reasons apart: a construct that is permanently inexpressible in a
single-frame pandas proxy (`groupBy`, joins, window functions, UDFs) versus one this
converter cannot express yet, which is a defect to fix.

See `lambda/agentic_data_ingestion_test_code/README.md` for full testing architecture details.
