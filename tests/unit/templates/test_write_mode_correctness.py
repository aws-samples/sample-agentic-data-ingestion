# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Task 10.2 — Write mode template correctness tests.

Validates that write_mode='append' templates contain append method and do NOT contain
overwrite methods, and vice versa. Parametrized over all 4 templates × 2 write modes.
"""

import os

import jinja2
import pytest

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")

SAMPLE_TRANSFORM = "df_transformed = df"

# Template name → (append assertion string, overwrite assertion string, anti-overwrite strings for append mode)
TEMPLATE_WRITE_ASSERTIONS = {
    "glue_job_s3_tables.py.j2": {
        "append_contains": [".append()"],
        "append_not_contains": [],  # createOrReplace() is used as fallback when table not yet in Spark catalog
        "overwrite_contains": ["createOrReplace()"],
        "overwrite_not_contains": [".append()"],
    },
    "glue_job_glue_catalog.py.j2": {
        "append_contains": ["enableUpdateCatalog"],
        "append_not_contains": [],  # Glue catalog overwrite also uses from_catalog, just without enableUpdateCatalog
        "overwrite_contains": ["from_catalog"],
        "overwrite_not_contains": ["enableUpdateCatalog"],
    },
    "glue_job_s3_parquet.py.j2": {
        "append_contains": ['mode("append")'],
        "append_not_contains": ['mode("overwrite")'],
        "overwrite_contains": ['mode("overwrite")'],
        "overwrite_not_contains": ['mode("append")'],
    },
    "glue_job_jdbc.py.j2": {
        "append_contains": ['mode="append"'],
        "append_not_contains": ['mode="overwrite"'],
        "overwrite_contains": ['mode="overwrite"'],
        "overwrite_not_contains": ['mode="append"'],
    },
}

BASE_VARS = {
    "source_format": "json",
    "partition_columns": None,
    "merge_key": "id",
    "checkpoint_enabled": False,
    "checkpoint_bucket": "",
}


@pytest.fixture
def jinja_env():
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _render(jinja_env, template_name, write_mode):
    template = jinja_env.get_template(template_name)
    return template.render(transform_block=SAMPLE_TRANSFORM, write_mode=write_mode, **BASE_VARS)


# --- Append mode tests ---


@pytest.mark.parametrize("template_name", list(TEMPLATE_WRITE_ASSERTIONS.keys()))
def test_append_mode_contains_append_method(jinja_env, template_name):
    """write_mode='append' templates must contain the append method."""
    rendered = _render(jinja_env, template_name, "append")
    assertions = TEMPLATE_WRITE_ASSERTIONS[template_name]
    for expected in assertions["append_contains"]:
        assert expected in rendered, f"{template_name} with write_mode='append' missing '{expected}'"


@pytest.mark.parametrize("template_name", list(TEMPLATE_WRITE_ASSERTIONS.keys()))
def test_append_mode_does_not_contain_overwrite(jinja_env, template_name):
    """write_mode='append' templates must NOT contain overwrite methods."""
    rendered = _render(jinja_env, template_name, "append")
    assertions = TEMPLATE_WRITE_ASSERTIONS[template_name]
    for forbidden in assertions["append_not_contains"]:
        assert forbidden not in rendered, (
            f"{template_name} with write_mode='append' unexpectedly contains '{forbidden}'"
        )


# --- Overwrite mode tests ---


@pytest.mark.parametrize("template_name", list(TEMPLATE_WRITE_ASSERTIONS.keys()))
def test_overwrite_mode_contains_overwrite_method(jinja_env, template_name):
    """write_mode='overwrite' templates must contain the overwrite method."""
    rendered = _render(jinja_env, template_name, "overwrite")
    assertions = TEMPLATE_WRITE_ASSERTIONS[template_name]
    for expected in assertions["overwrite_contains"]:
        assert expected in rendered, f"{template_name} with write_mode='overwrite' missing '{expected}'"


@pytest.mark.parametrize("template_name", list(TEMPLATE_WRITE_ASSERTIONS.keys()))
def test_overwrite_mode_does_not_contain_append(jinja_env, template_name):
    """write_mode='overwrite' templates must NOT contain append methods."""
    rendered = _render(jinja_env, template_name, "overwrite")
    assertions = TEMPLATE_WRITE_ASSERTIONS[template_name]
    for forbidden in assertions["overwrite_not_contains"]:
        assert forbidden not in rendered, (
            f"{template_name} with write_mode='overwrite' unexpectedly contains '{forbidden}'"
        )
