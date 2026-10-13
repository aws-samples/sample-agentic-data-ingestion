# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.5 — Property 30: PySpark Column anti-pattern detection.

For any rendered PySpark template, the AST validator detects zero instances of
suspect methods on Column expressions.

**Validates: Requirements 20.1**
"""

import os

import jinja2
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.templates.test_pyspark_ast_validation import validate_pyspark_ast

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

TEMPLATE_FILES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_jdbc.py.j2",
]

SOURCE_FORMATS = ["csv", "json", "parquet", "avro", "orc"]
WRITE_MODES_BY_TEMPLATE = {
    "glue_job_s3_tables.py.j2": ["append", "overwrite", "merge"],
    "glue_job_glue_catalog.py.j2": ["append", "overwrite"],
    "glue_job_s3_parquet.py.j2": ["append", "overwrite"],
    "glue_job_jdbc.py.j2": ["append", "overwrite"],
}

# Safe transform blocks that should NOT trigger anti-pattern detection
SAFE_TRANSFORMS = [
    "df_transformed = df",
    'df_transformed = df.filter(col("status") == "active")',
    'df_transformed = df.withColumn("name_clean", trim(col("name")))',
    'df_transformed = df.withColumn("name_lower", lower(col("name")))',
]


def _make_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


template_config_strategy = st.fixed_dictionaries(
    {
        "template_name": st.sampled_from(TEMPLATE_FILES),
        "source_format": st.sampled_from(SOURCE_FORMATS),
        "checkpoint_enabled": st.booleans(),
        "transform_block": st.sampled_from(SAFE_TRANSFORMS),
    }
)


@settings(max_examples=10)
@given(config=template_config_strategy)
def test_ast_validator_detects_zero_suspect_methods(config):
    """P30: For any rendered PySpark template with safe transforms, zero Column anti-patterns detected."""
    env = _make_env()
    template_name = config["template_name"]
    write_modes = WRITE_MODES_BY_TEMPLATE[template_name]
    # Pick a deterministic write mode based on template
    write_mode = write_modes[0]

    template = env.get_template(template_name)
    rendered = template.render(
        write_mode=write_mode,
        source_format=config["source_format"],
        transform_block=config["transform_block"],
        checkpoint_enabled=config["checkpoint_enabled"],
        checkpoint_bucket="test-bucket" if config["checkpoint_enabled"] else "",
        partition_columns=None,
        merge_key="id",
        # p55 R4.1: the csv branch's `.option("sep", …)`. StrictUndefined, so
        # supplied on every render regardless of the drawn source_format.
        source_delimiter=",",
    )

    violations = validate_pyspark_ast(rendered)
    assert violations == [], (
        f"Unexpected Column anti-patterns in {template_name} "
        f"(format={config['source_format']}, checkpoint={config['checkpoint_enabled']}): {violations}"
    )
