# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.7 — Property 32: Checkpoint template completeness.

For any template with checkpoint_enabled=True, output contains checkpoint block.
Note: templates use bare `except Exception:` not `AnalysisException`.

**Validates: Requirements 20.3**
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


def _make_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


template_strategy = st.sampled_from(TEMPLATE_FILES)
checkpoint_strategy = st.booleans()
bucket_strategy = st.sampled_from(["my-bucket", "test-checkpoint", "agentic-data-ingestion-ckpt"])


@settings(max_examples=10)
@given(template_name=template_strategy, checkpoint_enabled=checkpoint_strategy, bucket=bucket_strategy)
def test_checkpoint_completeness(template_name, checkpoint_enabled, bucket):
    """P32: checkpoint_enabled=True → checkpoint block present; False → absent."""
    env = _make_env()
    template = env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        write_mode="append",
        source_format="json",
        partition_columns=None,
        merge_key="id",
        checkpoint_enabled=checkpoint_enabled,
        checkpoint_bucket=bucket if checkpoint_enabled else "",
    )

    if checkpoint_enabled:
        assert "checkpoint_path" in rendered, f"{template_name} with checkpoint_enabled=True missing checkpoint_path"
        assert "spark.read.parquet(checkpoint_path)" in rendered, (
            f"{template_name} with checkpoint_enabled=True missing checkpoint read"
        )
        assert '.write.mode("overwrite").parquet(checkpoint_path)' in rendered, (
            f"{template_name} with checkpoint_enabled=True missing checkpoint write"
        )
        assert bucket in rendered, f"{template_name} with checkpoint_enabled=True missing bucket name '{bucket}'"
    else:
        assert "checkpoint_path" not in rendered, (
            f"{template_name} with checkpoint_enabled=False unexpectedly contains checkpoint_path"
        )
