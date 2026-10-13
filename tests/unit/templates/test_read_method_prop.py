# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.9 — Property 40: PySpark read method per format.

For any source_format in {csv, json, parquet, avro, orc}, rendered template uses
correct Spark read method.

**Validates: Requirements 24.6**
"""

import os

import jinja2
from hypothesis import given, settings
from hypothesis import strategies as st

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = "df_transformed = df"

TEMPLATE_FILES = [
    "glue_job_s3_tables.py.j2",
    "glue_job_glue_catalog.py.j2",
    "glue_job_s3_parquet.py.j2",
    "glue_job_jdbc.py.j2",
]

# source_format → expected string(s) in rendered output
FORMAT_EXPECTATIONS = {
    "csv": {
        "contains": [".csv(source_path)", 'option("header", True)'],
        "not_contains": [],
    },
    "parquet": {
        "contains": [".parquet(source_path)"],
        "not_contains": [".csv(", ".json(", ".orc(", 'format("avro")'],
    },
    "avro": {
        "contains": ['format("avro")', ".load(source_path)"],
        "not_contains": [],
    },
    "orc": {
        "contains": [".orc(source_path)"],
        "not_contains": [".csv(", ".json(", 'format("avro")'],
    },
    "json": {
        "contains": [".json(source_path)", 'option("multiLine", True)'],
        "not_contains": [".csv("],
    },
}


def _make_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


template_strategy = st.sampled_from(TEMPLATE_FILES)
format_strategy = st.sampled_from(list(FORMAT_EXPECTATIONS.keys()))


@settings(max_examples=10)
@given(template_name=template_strategy, source_format=format_strategy)
def test_read_method_per_format(template_name, source_format):
    """P40: For any source_format, rendered template uses correct Spark read method."""
    env = _make_env()
    template = env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        write_mode="append",
        source_format=source_format,
        partition_columns=None,
        merge_key="id",
        checkpoint_enabled=False,
        checkpoint_bucket="",
        # p55 R4.1: the csv branch's `.option("sep", …)`. StrictUndefined, so
        # supplied on every render regardless of the drawn format.
        source_delimiter=",",
    )

    expectations = FORMAT_EXPECTATIONS[source_format]

    for expected in expectations["contains"]:
        assert expected in rendered, f"{template_name} with source_format='{source_format}' missing '{expected}'"

    # For read-method section only (before transform block), check exclusions
    read_section = rendered.split("# === AI-Generated Transform Block ===")[0]
    for forbidden in expectations["not_contains"]:
        assert forbidden not in read_section, (
            f"{template_name} with source_format='{source_format}' unexpectedly contains '{forbidden}' in read section"
        )
