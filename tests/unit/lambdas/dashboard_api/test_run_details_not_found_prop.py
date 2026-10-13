# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: an unknown run id 404s, naming itself.

# Feature: p38-retry-run-identity, Property 3: An unknown run id 404s, naming itself

*For any* run id string, when the state-table query returns zero items,
``get_run_details`` responds ``404`` and the body names that run id.

**Validates: Requirements 1.4**

**This pins behaviour that already exists** — ``get_run_details`` returns
``create_response(404, {"error": f"Run not found: {run_id}"})`` on an empty
``Items`` list (``handlers/pipelines_runs.py:447-448``, read end to end). The
property is here for two reasons, neither of which is "make the 404 work":

1. **do-NOT 6 cannot be violated later.** The tempting repair for the p38 bug is
   a tolerant reader — a fallback that resolves a run from the executions row
   when no state row exists — and a ``200`` describing a run that did not happen
   is worse than the error box (``requirements.md`` R1.4 and its note). This test
   fails the moment such a fallback is added.
2. **R4 needs a stable 404 to have something to distinguish.** R4.1/R4.2 split
   "no such run" from "the request did not complete", and that split is only
   meaningful while the backend answers the first case with a 404 that names the
   id it could not find.

**The generator is shaped by the observed defect, not by ``st.text()`` alone.**
It emits the phantom ``retry-run-…`` shape — the id class the live bug produced
and the one a bookmarked URL still carries (``requirements.md`` §5 risk 8) —
including the retry-of-a-retry form whose 47 characters R6.2 is about, plus the
empty id and non-ASCII ids.

**How the assertions stay non-vacuous for the empty id.** ``"" in message`` is
trivially true, so containment alone would prove nothing for that case. The
"not found" assertion carries the weight there. Neither assertion pins the exact
wording — the message may be reworded, but it must still say the run was not
found and must still name the id.

No moto, no boto3 client mock: the module-level ``state_table`` object is
replaced with a ``MagicMock`` for the duration of each example, which is the
pattern the sibling readers' tests in this directory already use
(``test_run_details_load_outcome.py``, ``test_run_details_approval_parsing.py``).
The patch is applied inside the test body rather than through a fixture because
a Hypothesis ``@given`` test cannot take a function-scoped fixture.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock, patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path / env setup — same shape as test_run_details_load_outcome.py, which
# covers a sibling concern in the same function. The directory conftest.py also
# bootstraps sys.path; this is belt-and-braces for a direct single-file run.
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _path in (os.path.abspath(LAMBDA_DIR), os.path.abspath(SHARED_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")
os.environ.setdefault("DYNAMODB_TABLE", "test-state-table")
os.environ.setdefault("MAPPINGS_TABLE", "test-mappings-table")
os.environ.setdefault("PIPELINES_TABLE", "test-pipelines-table")
os.environ.setdefault("GENERATED_CODE_TABLE", "test-generated-code-table")
os.environ.setdefault("TEST_RESULTS_TABLE", "test-results-table")
os.environ.setdefault("APPROVALS_TABLE_NAME", "test-approvals-table")
os.environ.setdefault("EXECUTIONS_TABLE", "test-executions-table")

from handlers import pipelines_runs as runs

# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

#: The 8-hex-character suffix both real run ids and the phantom carry.
_hex8 = st.text(alphabet="0123456789abcdef", min_size=8, max_size=8)


def _format_run_timestamp(parts: tuple[int, int, int, int, int, int]) -> str:
    """``2026-08-09-15-38-19`` — the timestamp segment of a real run id."""
    year, month, day, hour, minute, second = parts
    return f"{year:04d}-{month:02d}-{day:02d}-{hour:02d}-{minute:02d}-{second:02d}"


#: ``run-2026-08-09-15-38-19-5c5f5f8a`` — the real run-id shape. Included
#: because a 404 on a well-formed id that simply has no rows is the common
#: case (a deleted run, a typo'd bookmark), not an exotic one.
_canonical_run_ids = st.builds(
    lambda ts, suffix: f"run-{ts}-{suffix}",
    ts=st.tuples(
        st.integers(min_value=2024, max_value=2030),
        st.integers(min_value=1, max_value=12),
        st.integers(min_value=1, max_value=28),
        st.integers(min_value=0, max_value=23),
        st.integers(min_value=0, max_value=59),
        st.integers(min_value=0, max_value=59),
    ).map(_format_run_timestamp),
    suffix=_hex8,
)

#: ``retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303`` — the phantom. This is
#: the id class the live bug reported to the caller and the browser navigated
#: to, and no state row has ever been written under one.
_phantom_retry_ids = st.builds(
    lambda base, h: f"retry-{base}-{h}",
    base=_canonical_run_ids,
    h=_hex8,
)

#: A retry *of* a phantom, sliced at 40 characters as ``:1335`` did. R6.2's
#: corrupted id: it still parses as an id and identifies nothing, so it is
#: exactly the kind of value that must reach a 404 rather than anything else.
_nested_phantom_ids = st.builds(
    lambda p, h: f"retry-{p[:40]}-{h}",
    p=_phantom_retry_ids,
    h=_hex8,
)

#: Empty, whitespace-only and non-ASCII ids. The concrete list is sampled
#: alongside unconstrained ``st.text()`` so the interesting shapes are hit on
#: every run rather than only when the random draw happens to produce them.
_edge_ids = st.sampled_from(
    [
        "",
        " ",
        "\t\n",
        "运行-2026-08-09",
        "run-ünïcødé-5c5f5f8a",
        "рун-42",
        "run-🚀-01",
        "retry-run-🚀-537b2303",
        "run-" + "x" * 500,
    ]
)

run_id_strategy = st.one_of(
    _phantom_retry_ids,
    _nested_phantom_ids,
    _canonical_run_ids,
    _edge_ids,
    st.text(min_size=0, max_size=40),
)


def _state_table_returning_no_items() -> MagicMock:
    """A state table whose ``query`` finds nothing for any run id."""
    table = MagicMock()
    table.query.return_value = {"Items": []}
    return table


def _parse(response: dict[str, Any]) -> tuple[int, dict[str, Any]]:
    return response["statusCode"], json.loads(response["body"])


# ---------------------------------------------------------------------------
# Property 3
# ---------------------------------------------------------------------------


@given(run_id=run_id_strategy)
@settings(max_examples=200, suppress_health_check=[HealthCheck.too_slow])
def test_unknown_run_id_404s_naming_itself(run_id: str) -> None:
    """Property 3: An unknown run id 404s, naming itself.

    # Feature: p38-retry-run-identity, Property 3: An unknown run id 404s, naming itself

    For any run id string, when the state-table query returns zero items,
    ``get_run_details`` responds ``404`` and the body names that run id.

    **Validates: Requirements 1.4**
    """
    with patch.object(runs, "state_table", _state_table_returning_no_items()):
        status, body = _parse(runs.get_run_details(run_id))

    assert status == 404, f"expected 404 for a run id with no state rows, got {status} for {run_id!r}"

    message = body.get("error")
    assert isinstance(message, str), f"the 404 body must carry a string 'error', got {body!r}"

    # It says the run was not found. This is the assertion that carries the
    # weight for the empty id, where containment below is vacuous.
    assert "not found" in message.lower(), f"the 404 message must say the run was not found, got {message!r}"

    # It names the run id — R1.4's "naming the run id". Round-tripped through
    # json.loads, so a non-ASCII id compares equal despite json.dumps escaping
    # it in the wire body.
    assert run_id in message, f"the 404 message must name the run id {run_id!r}, got {message!r}"

    # A 404 is not a run: nothing that a caller could mistake for run content
    # comes back with it. This is the do-NOT 6 tripwire — a tolerant reader
    # would return steps here rather than an error.
    assert "steps" not in body, f"a not-found response must not carry run content, got {body!r}"


# ---------------------------------------------------------------------------
# The observed id, as an example rather than a draw
# ---------------------------------------------------------------------------


def test_the_live_phantom_id_404s_naming_itself() -> None:
    """The exact id from the live defect, pinned as an example.

    ``/pipelines/newtest-ab2a08a0/runs/retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303``
    is the URL that rendered "Failed to load run details"
    (``requirements.md`` Introduction, defect 1). The state table holds Count 0
    for it. The 404 is the correct answer and this test says so by name, so a
    future reader does not have to trust that the generator happened to draw
    this shape.

    **Validates: Requirements 1.4**
    """
    run_id = "retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303"

    with patch.object(runs, "state_table", _state_table_returning_no_items()):
        status, body = _parse(runs.get_run_details(run_id))

    assert status == 404
    assert body["error"] == f"Run not found: {run_id}"
