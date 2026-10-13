# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The producer's dimension schema equals the consumer's (p39 task 13.3, R6.1/R6.3).

**What this file exists to catch, and why it is not covered elsewhere.** Task 8.1
made the AgentCore container's ``Pipeline.StageDuration`` reach CloudWatch, and
task 8.3's tests proved it: ``put_metric_data`` is called, with the right metric
names, values and units. The series appeared — 6 samples for ``CodeGeneration``,
mean 3750.77 ms — and was still **unreadable**, because nothing asserted what
dimension NAMES it carried.

Powertools stamps a ``service`` dimension on every EMF metric, so a
``POWERTOOLS_ENABLED`` Lambda published ``{PipelineId, StageName, Status,
service}`` while the container's fallback to ``put_direct_metric`` published
``{PipelineId, StageName, Status}``. ``_stage_durations_cw`` hardcodes the
four-name brace form, and a CloudWatch SEARCH brace schema is an **exact** match
on the dimension-NAME set — so the three-name shape matched nothing. A read-only
``get-metric-data`` probe with the consumer's own expression returned **zero**
series for ``CodeGeneration`` against 26 for ``SchemaExtract``.

**Both sides are derived from the code, never restated here.** The consumer's set
is parsed out of the expression ``_stage_durations_cw`` actually builds; the
producer's is read off the ``put_metric_data`` call ``record_stage_execution``
actually makes. A future change to either side — a fifth dimension on the
producer, a narrowed brace schema on the consumer — fails here instead of going
quiet until someone reads a dashboard tile. Naming the four dimensions literally
would only pin today's answer twice.

Fixed in ``put_metric``'s Powertools fallback, not in ``put_direct_metric``: the
publishers that call ``put_direct_metric`` directly have no EMF twin and their
consumers' brace schemas already name their exact dimension sets (see the fence
in ``put_direct_metric``'s docstring and ``test_metrics_facade_run_cost.py``).

Mocking: CloudWatch is stubbed with ``MagicMock`` on both sides — the shared
fixtures in ``tests/fixtures/conftest.py`` cover DynamoDB, S3 and SQS only. Same
convention as ``test_metrics_facade_flush.py`` and, for the consumer,
``test_metrics_stage_durations_char.py``.

Validates: Requirements R6.1, R6.3
"""

import os
import pathlib
import re
import sys
from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. Self-sufficient: under xdist --dist=loadfile any file may be the
# first import in its worker, so this file adds both the ``shared`` dir it tests
# and the ``dashboard_api`` dir whose consumer it reads — the same two entries
# ``test_metrics_stage_durations_char.py`` adds for the same reason.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent.parent
for _p in (
    _PROJECT_ROOT / "shared",
    _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

# ``handlers.common`` resolves table names through ``require_env`` at import time;
# the sanctioned development switch is AGENTIC_DATA_INGESTION_ENV=local. AWS_REGION
# is deliberately NOT set here — the unit layer pins it in tests/unit/conftest.py
# (LGR-3e45e65-1, template / ratified).
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

STAGE_CW_NAME = "CodeGeneration"
PIPELINE = "pipe-service-dim"

# SEARCH('{<namespace>,<dim>,...} <filters>', '<stat>', <period>)
_BRACE_RE = re.compile(r"SEARCH\('\{(?P<schema>[^}]*)\}")


def _reload_metrics_facade(monkeypatch, enabled: str = "false"):
    """Force-reload ``metrics_facade`` with the given POWERTOOLS_ENABLED value.

    The module resolves the flag at import time (``metrics_facade.py:38-46``), so
    the container path is only reachable by dropping it from the module cache and
    importing again. Same helper as ``test_metrics_facade_flush.py:32-45``.
    """
    monkeypatch.setenv("POWERTOOLS_ENABLED", enabled)
    sys.modules.pop("metrics_facade", None)

    import metrics_facade

    return metrics_facade


@pytest.fixture(autouse=True)
def _drop_reloaded_module():
    """Evict the reloaded module afterwards so no flag state leaks to other files."""
    yield
    sys.modules.pop("metrics_facade", None)


def _emitted_stage_duration_dimensions(monkeypatch) -> dict[str, str]:
    """The Dimensions the container path actually puts on Pipeline.StageDuration.

    Read off the ``put_metric_data`` call, not off the buffer: the buffer is an
    implementation detail and the API call is what CloudWatch indexes.
    """
    mod = _reload_metrics_facade(monkeypatch, "false")
    assert mod.POWERTOOLS_AVAILABLE is False, "this is the container path — Powertools must be off"

    mock_cw = MagicMock()
    mod.metrics_facade._cloudwatch = mock_cw

    mod.PipelineMetrics.record_stage_execution(
        stage_name=STAGE_CW_NAME,
        pipeline_id=PIPELINE,
        status="completed",
        duration_ms=3750.77,
    )

    assert mock_cw.put_metric_data.call_count == 1, "the stage flush publishes in one call"
    published = {m["MetricName"]: m for m in mock_cw.put_metric_data.call_args.kwargs["MetricData"]}
    duration = published["Pipeline.StageDuration"]
    return {d["Name"]: d["Value"] for d in duration["Dimensions"]}


def _searched_dimension_names(monkeypatch) -> set[str]:
    """The dimension names ``_stage_durations_cw``'s brace schema searches.

    Parsed from the expression the function builds when called, so this follows
    the consumer wherever the expression moves — nothing here restates it.

    ``handlers.common`` resolves its table names through ``require_env`` at import
    time, and ``env_utils._LOCAL_MODE`` — the switch that lets ``require_env``
    fall back to a default — is itself resolved when ``env_utils`` is imported
    (``env_utils.py:9``). Setting ``AGENTIC_DATA_INGESTION_ENV`` is therefore not
    enough on its own: whether it takes effect depends on whether some earlier
    test in this process imported ``env_utils`` first, which is exactly the kind
    of order dependence a test should not carry. ``require_env`` reads
    ``_LOCAL_MODE`` at CALL time, so the flag is patched directly and
    ``monkeypatch`` restores it.
    """
    import env_utils

    monkeypatch.setenv("AGENTIC_DATA_INGESTION_ENV", "local")
    monkeypatch.setattr(env_utils, "_LOCAL_MODE", True)

    from handlers import metrics as metrics_mod

    mock_cw = MagicMock()
    mock_cw.get_metric_data.return_value = {"MetricDataResults": []}
    with patch.object(metrics_mod, "cloudwatch_client", mock_cw):
        metrics_mod._stage_durations_cw(
            datetime(2025, 1, 1, tzinfo=UTC),
            datetime(2025, 1, 2, tzinfo=UTC),
            300,
        )

    queries = mock_cw.get_metric_data.call_args.kwargs["MetricDataQueries"]
    expressions = [q["Expression"] for q in queries if f'StageName="{STAGE_CW_NAME}"' in q.get("Expression", "")]
    # Two SEARCHes per stage since 2026-10-10 — the `Sum` and `SampleCount`
    # statistics a weighted mean needs — and both must carry the same brace.
    assert len(expressions) == 2, (
        f"expected two SEARCHes (Sum, SampleCount) for StageName={STAGE_CW_NAME}; the stage must be "
        f"mapped in _CW_STAGE_NAMES for this comparison to mean anything: {expressions}"
    )

    braces = set()
    for expression in expressions:
        match = _BRACE_RE.search(expression)
        assert match, f"not a brace-form SEARCH: {expression!r}"
        parts = [p.strip() for p in match.group("schema").split(",")]
        braces.add(frozenset(parts[1:]))  # parts[0] is the namespace, not a dimension
    assert len(braces) == 1, f"the two SEARCHes name different dimension sets: {braces}"
    return set(braces.pop())


def test_direct_path_emits_a_service_dimension(monkeypatch):
    """The container's direct-emit path carries ``service`` (R6.1, R6.3).

    Its VALUE is not what makes the series readable — a SEARCH brace schema
    matches on dimension NAMES — so this asserts presence and non-emptiness, not
    a particular string. The two producers are free to disagree on the value.
    """
    dims = _emitted_stage_duration_dimensions(monkeypatch)

    assert "service" in dims, (
        "Powertools stamps `service` on the EMF path; without it here the container's "
        f"datapoints exist and no brace schema matches them. Emitted: {sorted(dims)}"
    )
    assert dims["service"], "an empty value would make the dimension itself unmatchable"


def test_emitted_dimension_names_equal_the_names_the_consumer_searches(monkeypatch):
    """Producer and consumer agree on the dimension-NAME set (R6.1, R6.3).

    **This is the assertion that would have caught the mismatch before a deploy
    rather than after one.** Both sides are derived: the emitted set off the
    ``put_metric_data`` call, the searched set out of the expression
    ``_stage_durations_cw`` builds. A CloudWatch brace schema is an exact match on
    that set, so any inequality means the consumer reads an empty series while the
    datapoints sit in the namespace.
    """
    emitted = set(_emitted_stage_duration_dimensions(monkeypatch))
    searched = _searched_dimension_names(monkeypatch)

    assert emitted == searched, (
        "Pipeline.StageDuration is published with dimension names "
        f"{sorted(emitted)} but _stage_durations_cw searches {sorted(searched)}. "
        "A CloudWatch SEARCH brace schema is an EXACT match on the dimension-name set, "
        f"so the read returns nothing. Missing from the emit: {sorted(searched - emitted)}; "
        f"emitted but not searched: {sorted(emitted - searched)}."
    )
