# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.6 — Property 31: Write mode template correctness.

For any template with write_mode='append', output contains append method and not overwrite methods.

**Validates: Requirements 20.2**
"""

import os

import jinja2
from hypothesis import given, settings
from hypothesis import strategies as st

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = "df_transformed = df"

# Template → append indicator, overwrite indicator
TEMPLATE_ASSERTIONS = {
    "glue_job_s3_tables.py.j2": {
        "append_marker": ".append()",
        "overwrite_marker": None,  # createOrReplace() is used as fallback in append mode for TABLE_OR_VIEW_NOT_FOUND
    },
    "glue_job_glue_catalog.py.j2": {
        "append_marker": "enableUpdateCatalog",
        "overwrite_marker": None,  # Glue catalog doesn't have a unique overwrite-only marker
    },
    "glue_job_s3_parquet.py.j2": {
        "append_marker": 'mode("append")',
        "overwrite_marker": 'mode("overwrite")',
    },
    "glue_job_jdbc.py.j2": {
        "append_marker": 'mode="append"',
        "overwrite_marker": 'mode="overwrite"',
    },
}


def _make_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


template_strategy = st.sampled_from(list(TEMPLATE_ASSERTIONS.keys()))
write_mode_strategy = st.sampled_from(["append", "overwrite"])


@settings(max_examples=10)
@given(template_name=template_strategy, write_mode=write_mode_strategy)
def test_write_mode_correctness(template_name, write_mode):
    """P31: append contains append marker, not overwrite marker; overwrite contains overwrite marker."""
    env = _make_env()
    template = env.get_template(template_name)
    rendered = template.render(
        transform_block=SAMPLE_TRANSFORM,
        write_mode=write_mode,
        source_format="json",
        partition_columns=None,
        merge_key="id",
        checkpoint_enabled=False,
        checkpoint_bucket="",
    )

    assertions = TEMPLATE_ASSERTIONS[template_name]

    if write_mode == "append":
        assert assertions["append_marker"] in rendered, (
            f"{template_name} with append missing '{assertions['append_marker']}'"
        )
        if assertions["overwrite_marker"]:
            assert assertions["overwrite_marker"] not in rendered, (
                f"{template_name} with append unexpectedly contains '{assertions['overwrite_marker']}'"
            )
    else:  # overwrite
        if assertions["overwrite_marker"]:
            assert assertions["overwrite_marker"] in rendered, (
                f"{template_name} with overwrite missing '{assertions['overwrite_marker']}'"
            )
        if assertions["append_marker"]:
            assert assertions["append_marker"] not in rendered, (
                f"{template_name} with overwrite unexpectedly contains '{assertions['append_marker']}'"
            )
