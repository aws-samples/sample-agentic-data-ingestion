# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for ``_select_template`` totality (Task 9.6).

**Property 1: Template selection is total**

For every supported ``(source_type, target_type)`` pair, ``_select_template``
returns exactly one non-None template filename. This is the behavioural
contract that lets ``generate_code`` and ``assemble_pipeline_code`` rely on
deterministic codegen without ever falling through to the full-AI fallback
for a known pair.

A negative-side property guards the contract from the opposite direction:
unknown source types (with no wildcard fallback) MUST return ``None`` so
upstream callers can detect and handle the unsupported case explicitly.

**Validates: Requirements 9.1, 9.2** (p17-jdbc-data-source).
"""

import os
import string
import sys
import unittest.mock as mock

import hypothesis.strategies as st
from hypothesis import given, settings

# Add Lambda dir + shared dir to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Set AWS region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

# Patch heavy module-level dependencies before importing lambda_function.
_mods = {
    "strands": mock.MagicMock(),
    "strands_mcp_factory": mock.MagicMock(),
    "agentcore_client": mock.MagicMock(),
    "eventbridge_utils": mock.MagicMock(
        get_eventbridge_publishers=mock.MagicMock(return_value=(mock.MagicMock(), mock.MagicMock(), mock.MagicMock()))
    ),
    "code_validator": mock.MagicMock(),
    "env_utils": mock.MagicMock(
        get_aws_account_id=mock.MagicMock(return_value="123456789012"),
        require_env=mock.MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": mock.MagicMock(),
    "parameter_extractor": mock.MagicMock(),
}

with mock.patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    from lambda_function import _select_template


# ---- Supported domain (per Requirement 9.2) -------------------------------- #
SUPPORTED_SOURCE_TYPES = ["S3_UPLOAD", "JDBC"]
SUPPORTED_TARGET_TYPES = [
    "s3_tables",
    "glue_catalog",
    "jdbc",
    "rds_database",
    "s3_parquet",
]


# ---- Property 1: Template selection is total ------------------------------- #
@given(
    source_type=st.sampled_from(SUPPORTED_SOURCE_TYPES),
    target_type=st.sampled_from(SUPPORTED_TARGET_TYPES),
)
@settings(max_examples=100)
def test_select_template_is_total_over_supported_pairs(source_type: str, target_type: str):
    """Every supported (source_type, target_type) pair resolves to exactly one
    non-None ``.py.j2`` filename.

    Hypothesis explores all ``2 × 5 = 10`` combinations from the sampled
    domain. Failure here means the deterministic codegen path has a hole —
    a supported pair would fall through to the full-AI fallback in
    production.

    **Validates: Requirements 9.1, 9.2**
    """
    result = _select_template(source_type, target_type)

    assert result is not None, (
        f"Expected a template for ({source_type!r}, {target_type!r}), got None. "
        "TEMPLATE_RULES has a hole for this supported pair."
    )
    assert isinstance(result, str)
    assert result.endswith(".py.j2"), f"Expected a Jinja2 template filename, got {result!r}"


# ---- Negative side: unknown sources resolve to None ------------------------ #
# Generate plausible-but-unsupported source identifiers. Excludes the
# wildcard "*" (which is reserved as the rule-side marker, not a valid
# pipeline source_type) and any value already in SUPPORTED_SOURCE_TYPES.
_UNSUPPORTED_SOURCE_ALPHABET = string.ascii_letters + string.digits + "_-"


@st.composite
def _unsupported_source_types(draw):
    candidate = draw(st.text(alphabet=_UNSUPPORTED_SOURCE_ALPHABET, min_size=1, max_size=24))
    # Reject collisions with supported sources or the wildcard marker.
    if candidate == "*" or candidate in SUPPORTED_SOURCE_TYPES:
        candidate = candidate + "_x"
    return candidate


@given(
    source_type=_unsupported_source_types(),
    target_type=st.sampled_from(SUPPORTED_TARGET_TYPES),
)
@settings(max_examples=100)
def test_select_template_returns_none_for_unsupported_source(source_type: str, target_type: str):
    """Sources outside the supported set (and not ``"*"``) resolve to ``None``.

    The resolver must NOT silently apply a wildcard fallback to arbitrary
    source identifiers — callers rely on ``None`` to detect unsupported
    pipelines and surface a clear error. This complements Property 1: the
    resolver is total over the *supported* domain and partial elsewhere.

    **Validates: Requirements 9.1, 9.2**
    """
    result = _select_template(source_type, target_type)
    assert result is None, (
        f"Expected None for unsupported source {source_type!r}, got {result!r}. "
        "TEMPLATE_RULES may have an over-permissive wildcard."
    )
