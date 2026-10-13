# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Module-size guideline enforcement (P21 Req 4.4).

The project documents a maximum-module-size guideline of **800 physical source
lines per module** (see ``shared/README.md`` → "Module Size Guideline"). This
test enforces that limit for the two primary hotspot files identified in the
P21 code review.

Metric: "physical source lines" is measured as the **total line count** of the
file (``len(path.read_text().splitlines())``) — i.e. every line in the file,
including blanks and comments. This is the simplest, most reproducible
interpretation of "physical source lines" and is the exact metric Task 7.4
re-checks now that decomposition is complete.

Both primary hotspots were decomposed in Tasks 7.2 / 7.3 / 7.4 (façade +
sibling-module pattern), so this is now a **hard assertion**: both files must
be at or below the 800-line guideline. The earlier ``xfail`` marker (used while
decomposition was pending) has been removed per Task 7.4.

**Validates: Requirements 4.4**
"""

import os

import pytest

# tests/unit/test_module_sizes.py → agentic_data_ingestion/
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Documented guideline: see shared/README.md → "Module Size Guideline".
MAX_PHYSICAL_SOURCE_LINES = 800

# The two PRIMARY hotspot files required to meet the guideline after
# decomposition (P21 Req 4.4). Paths are relative to agentic_data_ingestion/.
PRIMARY_HOTSPOT_FILES = [
    "lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines.py",
    "lambda/agentic_data_ingestion_create_glue_pipeline/lambda_function.py",
]


def _count_physical_source_lines(path: str) -> int:
    """Return the total physical source-line count for a file.

    "Physical source lines" = total lines in the file (including blank and
    comment lines), matching the documented guideline metric.
    """
    with open(path, encoding="utf-8") as f:
        return len(f.read().splitlines())


@pytest.mark.parametrize("rel_path", PRIMARY_HOTSPOT_FILES)
def test_primary_hotspot_within_size_guideline(rel_path: str):
    """Each primary hotspot file is at or below the 800-line guideline."""
    abs_path = os.path.join(REPO_ROOT, rel_path)
    assert os.path.isfile(abs_path), f"Expected hotspot file not found: {abs_path}"

    line_count = _count_physical_source_lines(abs_path)
    assert line_count <= MAX_PHYSICAL_SOURCE_LINES, (
        f"{rel_path} has {line_count} physical source lines, exceeding the "
        f"documented guideline of {MAX_PHYSICAL_SOURCE_LINES} "
        f"(see shared/README.md → 'Module Size Guideline')."
    )
