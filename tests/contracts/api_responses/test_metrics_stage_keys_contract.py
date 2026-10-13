# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test — the backend's mapped stage keys ARE the frontend's stage keys.

Feature: p39-metrics-truth, task 11.2. _Requirements: R6.2, R6.4_

Two clauses, both cross-layer:

1. **``R6.2``** — ``_CW_STAGE_NAMES``' key set equals the stage key set the page
   renders. The page's set is ``STAGE_KEYS``
   (``MetricsDashboardPage.tsx:118-125``), and it is the *only* gate on what
   reaches the screen: the render at ``:276-278`` iterates ``STAGE_KEYS`` and
   returns ``null`` for a key the payload omits, so a stage the backend maps but
   the page does not list is invisible, and a stage the page lists but the
   backend does not map renders nothing. That asymmetry is what R6.2 closes —
   the map used to carry two of the six keys, so four stages read
   ``Avg Time: N/A`` no matter what the producer emitted.
2. **``R6.4``** — a stage with no datapoint from either source reports
   Not_Established (``None``), never a zero. ``LGR-4854b3d-1``
   (template / ratified) is the fence: a three-state value — a measured
   duration, a measured zero, no measurement at all — is never collapsed to one
   of its ends. A ``0`` here would claim the stage ran instantly.

WHY THE ORACLE IS THE TSX AND NOT A LIST IN THIS FILE
-----------------------------------------------------
``R6.2`` is a claim about two layers agreeing, so a test that transcribes six
strings and compares them to ``_CW_STAGE_NAMES`` asserts almost nothing: it
passes when the map and the transcription are wrong in the same way, and it
cannot see the frontend adding a seventh stage. That is exactly the failure
``LGR-28b4d75-5`` (template / ratified) names — a contract test written from the
producer's own output cannot detect the producer drifting, because the output is
its own oracle. So the frontend key set is **read out of the frontend source at
test time**, the way every other test in this directory reads its expectations
out of ``types.ts``.

The oracle is exercised against mutated copies of the real source before it is
trusted (``TestTheOracleItself`` below), because an oracle that silently returns
an empty set makes every assertion built on it pass.

WHY THIS FILE IS IN ``tests/contracts/api_responses/`` AND NOT ``tests/unit/``
-----------------------------------------------------------------------------
Its expectations come from frontend TypeScript, which is this layer's defining
pattern and this layer's machinery: ``helpers.py`` exists precisely to hold the
TS reader (``LGR-99050e6-5``, template / ratified), and it is importable only as
a sibling module here. ``tests/unit/lambdas/dashboard_api/`` has no reader and
no precedent for reading ``dashboard-react/`` source; putting a cross-layer
oracle there would start a second copy of the pattern, which is the divergence
``LGR-99050e6-5`` extracted the reader to prevent. ``testing-standards.md``
assigns "API contract tests" to this directory, and the stage keys are the wire
contract for the ``stages`` map.

HOW THE FRONTEND SET IS OBTAINED — reuse where the construct matches, a local
reader where it does not, and no widening of the shared one
---------------------------------------------------------------------------
* ``helpers.load_interfaces`` / ``declares_null`` are **reused as-is** for the
  ``R6.4`` guard: ``StageMetrics.avg_time_ms`` must still be declared
  ``number | null`` (``types.ts:662-667``), or ``None`` is not a value the
  consumer accepts and ``R6.4``'s Not_Established has nowhere to land. That is
  ``LGR-99050e6-5``'s own guard pattern, applied to the field this task makes
  absent.
* ``STAGE_KEYS`` is **not** an ``export interface`` — it is a
  ``const … as const`` array of string literals — so ``read_interface`` cannot
  read it and would raise "not found". It is parsed by ``read_stage_keys``
  below, local to this file, following ``helpers.py``'s discipline exactly:
  understand one construct, raise on anything else, and refuse to return an
  empty set.
* ``helpers.py`` is deliberately **not extended** with an array reader. Its
  docstring scopes it to interfaces, and ``LGR-99050e6-5`` extracted it because
  a *second* endpoint needed the same reader — the same "two writers, not one"
  test the spec applied at task 3.1 (constraint 17, ``LGR-aa15a1b-1``). One
  caller does not earn a shared parser; the second one will, and moving it then
  is a mechanical lift.

DELIBERATELY NOT ASSERTED HERE
------------------------------
* **That a ``RunPipeline`` series exists, or that it never will.**
  ``pipeline_run`` is mapped with no producer on purpose — ``run_pipeline``
  holds no ``PipelineMetrics`` reference and p39 adds none, so its CloudWatch
  arm is empty today and the state-row derivation answers for it. But the
  precedence rule (``metrics.py:1046-1087``) is written to be general, so a
  future producer would win. The datapoint test below is therefore
  parametrised over **every** mapped key, including ``pipeline_run``: it pins
  the rule, not today's producer roster.
* **The ``measurement`` map's key set.** That is a different key set — the
  performance tile set — and task 21.6 (Property 7) owns it.
* **``_CW_STAGE_NAMES``' VALUES** (the ``StageName`` dimension strings), the
  SEARCH shape, and the ``AVG`` reduction. Task 1.3's
  ``test_metrics_stage_durations_char.py`` pins those, derived from the map at
  call time.
* **Label strings.** Task 18 owns those.

Mocking: CloudWatch is outside the shared fixtures' scope (DynamoDB / S3 / SQS),
so a ``MagicMock`` is the established pattern here, as in the sibling
``test_metrics_contract.py``. No ``mock_aws()``.
"""

from __future__ import annotations

import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from .helpers import declares_null, load_interfaces

# ---------------------------------------------------------------------------
# Path / env bootstrap, matching the sibling metrics contract tests
# (test_metrics_response_contract.py:19-36). Repeated per file rather than
# hoisted, because under xdist --dist=loadfile any file in this directory may
# be imported first.
#
# AWS_REGION / AWS_DEFAULT_REGION are set with setdefault: `handlers.metrics`
# builds a boto3 client at import time (`metrics.py:49`) and this layer has no
# conftest that pins a region. LGR-3e45e65-1's assignment lives in
# tests/unit/conftest.py and is scoped to `tests/unit/**`; setdefault cannot
# fight it, and cannot fight a sibling contract file that got here first.
# ---------------------------------------------------------------------------
_REPO_TESTS = Path(__file__).resolve().parents[2]
_PROJECT_ROOT = _REPO_TESTS.parent
_LAMBDA_DIR = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api"
_SHARED_DIR = _PROJECT_ROOT / "shared"
for _p in (str(_LAMBDA_DIR), str(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# `handlers.common` resolves nine table names through `require_env` at import
# time. Both switches are set: `AGENTIC_DATA_INGESTION_ENV=local` is the
# sanctioned development fallback, and the names are also set explicitly because
# `env_utils` freezes `_LOCAL_MODE` at ITS import (`env_utils.py:6`) — so if a
# sibling file imported it first under a different value, only the explicit
# names get this module collected.
for _name, _value in (
    ("AGENTIC_DATA_INGESTION_ENV", "local"),
    ("STATE_TABLE", "agentic_data_ingestion_state"),
    ("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings"),
    ("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"),
    ("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code"),
    ("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results"),
    ("APPROVALS_TABLE", "agentic_data_ingestion_approvals"),
    ("EXECUTIONS_TABLE", "agentic_data_ingestion_executions"),
    ("AUDIT_TABLE", "agentic_data_ingestion_audit_log"),
    ("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces"),
    ("AWS_REGION", "us-east-1"),
    ("AWS_DEFAULT_REGION", "us-east-1"),
):
    os.environ.setdefault(_name, _value)

from handlers import metrics as metrics_mod

# ---------------------------------------------------------------------------
# The frontend sources this contract is pinned against
# ---------------------------------------------------------------------------
_FRONTEND_SRC = _PROJECT_ROOT / "dashboard-react" / "src"
_METRICS_PAGE = _FRONTEND_SRC / "pages" / "MetricsDashboardPage.tsx"
_FRONTEND_TYPES = _FRONTEND_SRC / "types.ts"

START = datetime(2025, 1, 1, tzinfo=UTC)
END = datetime(2025, 1, 2, tzinfo=UTC)
PERIOD = 300


# ═══════════════════════════════════════════════════════════════════════════
# The oracle: read STAGE_KEYS out of the page
# ═══════════════════════════════════════════════════════════════════════════
#
# `const STAGE_KEYS = [ 'a', 'b', ] as const;` — one string literal per line.
# `as const` is required, not optional, in the pattern: it is what makes the
# array a literal tuple type rather than `string[]`, so a match without it is a
# different declaration and should not be read as this one.
_STAGE_KEYS_DECL = re.compile(
    r"const\s+STAGE_KEYS\s*(?::[^=]+)?=\s*\[(?P<body>.*?)\]\s*as\s+const\s*;",
    re.DOTALL,
)
_STRING_ENTRY = re.compile(r"""^['"](?P<key>[^'"]+)['"]\s*,?$""")


def read_stage_keys(source: str, source_path: Path | str = "<source>") -> list[str]:
    """Return the string literals in the page's ``STAGE_KEYS`` declaration.

    Strict in the same three ways ``helpers.read_interface`` is strict, and for
    the same reason — this list is the expected value of an equality assertion,
    so a silently empty or half-parsed result would make that assertion trivial:

    * an absent declaration raises rather than yielding ``[]``;
    * a line inside the literal that is not a bare string entry raises rather
      than being skipped, so a computed or spread entry is a loud failure and
      not a quietly dropped stage;
    * a duplicate entry raises, because a duplicated key would make the parsed
      *set* agree with the backend while the page rendered a stage twice.

    Comment lines are skipped, as in ``read_interface``.
    """
    match = _STAGE_KEYS_DECL.search(source)
    assert match, (
        f"STAGE_KEYS not found in {source_path} — this contract is pinned against that declaration. "
        f"If the stage list moved, repoint the constant rather than transcribing the keys into this test."
    )

    keys: list[str] = []
    for line in match.group("body").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("//", "*", "/*")):
            continue
        entry = _STRING_ENTRY.match(stripped)
        assert entry, (
            f"Unparsed entry in STAGE_KEYS ({source_path}): {stripped!r}. Only bare string literals are "
            f"understood; teach read_stage_keys about it rather than letting the stage be dropped."
        )
        keys.append(entry.group("key"))

    assert keys, f"STAGE_KEYS parsed as empty from {source_path} — every assertion built on it would pass vacuously"
    duplicates = {key for key in keys if keys.count(key) > 1}
    assert not duplicates, f"STAGE_KEYS declares {sorted(duplicates)} more than once in {source_path}"
    return keys


def _page_source() -> str:
    assert _METRICS_PAGE.is_file(), (
        f"{_METRICS_PAGE} is missing. This contract derives the expected stage key set from that file."
    )
    return _METRICS_PAGE.read_text(encoding="utf-8")


def _frontend_stage_keys() -> set[str]:
    """The stage key set the page renders, read from the page."""
    return set(read_stage_keys(_page_source(), _METRICS_PAGE))


# ═══════════════════════════════════════════════════════════════════════════
# Handler invocation helpers
# ═══════════════════════════════════════════════════════════════════════════


def _stage_metrics(cw_response: dict, state_items: list[dict] | None = None) -> dict[str, dict]:
    """``_get_stage_metrics`` against a stubbed CloudWatch and a stubbed state table."""
    mock_cw = MagicMock()
    mock_cw.get_metric_data.return_value = cw_response
    mock_state = MagicMock()
    mock_state.query.return_value = {"Items": state_items or []}

    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "state_table", mock_state),
    ):
        # p59: the read returns the stage map plus the failure-record
        # reconciliation. This file's subject is the stage KEY SET.
        return metrics_mod._get_stage_metrics(START, END, PERIOD).stages


def _duration_series(stage_key: str, values: list[float]) -> dict:
    """A GetMetricData response carrying the reduced duration series for ONE stage.

    ``_stage_durations_cw`` reads two reductions per stage, the summed duration
    ``<stage>_dur_sum`` and the summed sample count ``<stage>_dur_n``, and
    ignores the raw SEARCHes (``ReturnData: False``), so only the reductions are
    supplied here, one invocation per value. Task 1.3's characterization file
    owns that query shape.
    """
    return {
        "MetricDataResults": [
            {"Id": f"{stage_key}_dur_sum", "Values": values, "Timestamps": []},
            {"Id": f"{stage_key}_dur_n", "Values": [1.0] * len(values), "Timestamps": []},
        ]
    }


_EMPTY_CW = {"MetricDataResults": []}


# ═══════════════════════════════════════════════════════════════════════════
# The oracle itself — proof the detector can fail
# ═══════════════════════════════════════════════════════════════════════════


class TestTheOracleItself:
    """The reader is exercised against mutated real source before it is trusted.

    Without this, the R6.2 assertion below is only as good as an unverified
    regex: a reader that returned the same set on every input would make the
    equality hold no matter what either layer said. Each test here mutates the
    **real** page source and asserts the mutation is visible.
    """

    def test_reads_a_non_trivial_key_set_from_the_real_page(self):
        keys = read_stage_keys(_page_source(), _METRICS_PAGE)
        assert len(keys) > 1, f"expected a multi-stage list, read {keys!r}"
        for key in keys:
            assert re.fullmatch(r"[a-z][a-z0-9_]*", key), f"{key!r} is not a snake_case payload key"

    def test_an_added_frontend_stage_makes_the_key_sets_disagree(self):
        """A seventh stage on the page is a red, not a silent pass.

        This is the case a transcribed six-string list cannot see, and the
        reason the expectation is read from source.
        """
        mutated = _page_source().replace(
            "  'pipeline_run',\n",
            "  'pipeline_run',\n  'lineage_capture',\n",
            1,
        )
        keys = set(read_stage_keys(mutated, "<mutated page>"))
        assert "lineage_capture" in keys, "the mutation did not reach the reader — rewrite it, do not weaken it"
        assert keys != set(metrics_mod._CW_STAGE_NAMES), (
            "the R6.2 assertion would still pass with a stage the backend does not map — the detector is vacuous"
        )

    def test_a_dropped_frontend_stage_makes_the_key_sets_disagree(self):
        mutated = _page_source().replace("  'quality_checks',\n", "", 1)
        keys = set(read_stage_keys(mutated, "<mutated page>"))
        assert "quality_checks" not in keys, "the mutation did not reach the reader"
        assert keys != set(metrics_mod._CW_STAGE_NAMES), (
            "the R6.2 assertion would still pass with a stage the page no longer renders"
        )

    def test_an_absent_declaration_raises_rather_than_reading_as_empty(self):
        mutated = _page_source().replace("const STAGE_KEYS", "const RENAMED_STAGE_KEYS", 1)
        with pytest.raises(AssertionError, match="STAGE_KEYS not found"):
            read_stage_keys(mutated, "<mutated page>")

    def test_an_entry_the_reader_does_not_understand_raises(self):
        """A computed or spread entry must fail loudly, not be dropped."""
        mutated = _page_source().replace(
            "  'pipeline_run',\n",
            "  'pipeline_run',\n  ...EXTRA_STAGE_KEYS,\n",
            1,
        )
        with pytest.raises(AssertionError, match="Unparsed entry in STAGE_KEYS"):
            read_stage_keys(mutated, "<mutated page>")

    def test_a_duplicated_entry_raises(self):
        mutated = _page_source().replace(
            "  'pipeline_run',\n",
            "  'pipeline_run',\n  'pipeline_run',\n",
            1,
        )
        with pytest.raises(AssertionError, match="more than once"):
            read_stage_keys(mutated, "<mutated page>")


# ═══════════════════════════════════════════════════════════════════════════
# R6.2 — the mapped key set IS the frontend stage key set
# ═══════════════════════════════════════════════════════════════════════════


class TestMappedKeySetEqualsFrontendStageKeys:
    def test_cw_stage_names_key_set_equals_frontend_stage_keys(self):
        """R6.2. Both sides read from their own source; neither is transcribed."""
        frontend = _frontend_stage_keys()
        mapped = set(metrics_mod._CW_STAGE_NAMES)

        unmapped = frontend - mapped
        unrendered = mapped - frontend
        assert not unmapped, (
            f"stages the page renders but _CW_STAGE_NAMES does not map: {sorted(unmapped)} — "
            f"each one reports Avg Time: N/A however much its producer emits (R6.2)"
        )
        assert not unrendered, (
            f"stages _CW_STAGE_NAMES maps but the page does not render: {sorted(unrendered)} — "
            f"the duration is fetched and then dropped on the floor"
        )

    def test_payload_stage_key_set_equals_frontend_stage_keys(self):
        """The same claim observed where it is consumed: the ``stages`` map.

        ``_get_stage_metrics`` builds its keys from ``_STAGE_STEP_NAMES`` while
        durations arrive keyed by ``_CW_STAGE_NAMES``, so the two maps agreeing
        with each other is what turns R6.2's mapping into a number on screen.
        A key in only one of them is a duration fetched for a stage that is
        never emitted, or a stage emitted with no duration source.
        """
        stages = _stage_metrics(_EMPTY_CW)
        assert set(stages) == _frontend_stage_keys()

    def test_duration_reader_covers_every_frontend_stage_key(self):
        """``_stage_durations_cw`` answers for every stage the page renders.

        Keyed off the reader's own return rather than off ``_CW_STAGE_NAMES``,
        so a map whose keys and whose query ids drifted apart is a red here.
        """
        mock_cw = MagicMock()
        mock_cw.get_metric_data.return_value = _EMPTY_CW
        with patch.object(metrics_mod, "cloudwatch_client", mock_cw):
            durations = metrics_mod._stage_durations_cw(START, END, PERIOD)
        assert set(durations) == _frontend_stage_keys()


# ═══════════════════════════════════════════════════════════════════════════
# R6.4 — a stage with no datapoint is Not_Established, never a zero
# ═══════════════════════════════════════════════════════════════════════════


class TestStageWithoutDatapointReportsNotEstablished:
    def test_no_source_anywhere_reports_none_for_every_stage(self):
        """R6.4 / LGR-4854b3d-1. Empty CloudWatch, empty state table.

        ``is None`` is asserted rather than a falsy check on purpose: ``0``,
        ``0.0`` and ``None`` are all falsy, so ``not avg`` would accept exactly
        the collapse this pins against.
        """
        stages = _stage_metrics(_EMPTY_CW)
        assert stages, "no stages returned — the assertions below would be vacuous"
        for key, stage in stages.items():
            assert "avg_time_ms" in stage, f"{key} carries no avg_time_ms field"
            assert stage["avg_time_ms"] is None, (
                f"{key} reports avg_time_ms={stage['avg_time_ms']!r} with nothing measured; "
                f"R6.4 requires Not_Established, and a zero claims the stage ran instantly"
            )

    def test_absence_is_not_reported_as_a_zero_count_of_milliseconds(self):
        """The specific collapse LGR-4854b3d-1 forbids, stated as its own claim."""
        stages = _stage_metrics(_EMPTY_CW)
        zeroed = {key for key, stage in stages.items() if stage["avg_time_ms"] == 0}
        assert not zeroed, f"stages reporting a measured zero duration on no measurement: {sorted(zeroed)}"

    @pytest.mark.parametrize("stage_key", sorted(metrics_mod._CW_STAGE_NAMES))
    def test_one_stage_with_a_datapoint_reports_it_and_the_rest_stay_not_established(self, stage_key: str):
        """The Not_Established above is a consequence of absence, not a constant.

        Parametrised over **every** mapped key, ``pipeline_run`` included. That
        stage has no producer today and its CloudWatch arm is empty in
        practice, but the precedence rule (``metrics.py:1046-1087``) is written
        to be general — CloudWatch wins wherever it answers — so this pins the
        rule rather than the current producer roster. Nothing here asserts that
        a ``RunPipeline`` series does or does not exist.
        """
        stages = _stage_metrics(_duration_series(stage_key, [1234.0]))

        assert stages[stage_key]["avg_time_ms"] == 1234.0, (
            f"{stage_key} published a datapoint and still reports "
            f"{stages[stage_key]['avg_time_ms']!r} — the stage is mapped but its value never arrives"
        )
        others = {key: stage["avg_time_ms"] for key, stage in stages.items() if key != stage_key}
        assert all(value is None for value in others.values()), (
            f"stages with no datapoint reported a value: "
            f"{ {key: value for key, value in others.items() if value is not None} }"
        )

    def test_frontend_declares_avg_time_ms_nullable(self):
        """R6.4's landing site on the consumer, guarded per LGR-99050e6-5.

        ``None`` is only a reportable outcome while ``StageMetrics`` still
        declares the field nullable. Narrowing it to ``number`` to quiet a
        parser complaint is what that record rejects outright — the union is
        the true wire shape — so a future narrowing fails here rather than
        turning every stage into a rendered ``0``.
        """
        stage_metrics = load_interfaces(_FRONTEND_TYPES, "StageMetrics")["StageMetrics"]
        assert "avg_time_ms" in stage_metrics, "StageMetrics no longer declares avg_time_ms"
        required, ts_type = stage_metrics["avg_time_ms"]
        assert required, "avg_time_ms must stay a required key — absence of the KEY is a different fact from a null"
        assert declares_null(ts_type), (
            f"StageMetrics.avg_time_ms is declared {ts_type!r}, which admits no null; R6.4's "
            f"Not_Established has nowhere to land (LGR-99050e6-5)"
        )
