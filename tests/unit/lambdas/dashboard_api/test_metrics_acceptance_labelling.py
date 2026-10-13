# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The two ``measurement`` strings must say what their tiles now count.

Subject: the ``lines_ingested_24h`` and ``processing_rate`` entries in
``_build_comprehensive_metrics``' one ``_PublishedFact`` registry
(``handlers/metrics.py``).

WHY THE BACKEND STRING IS THE SUBJECT AND NOT THE TILE
------------------------------------------------------
``_PublishedFact.measurement``'s own docstring names the mechanism: "a tile's
label cannot drift from its source because the source is named in the payload the
label renders". So the measurement string is the PRIMARY carrier of what these
two figures are, and the tile label is the short form of it. The label is
asserted on the other side of the wire, in
``dashboard-react/src/pages/__tests__/MetricsDashboardPage.tile-labels.test.tsx``;
that file's own ``measurement`` map is fixture-supplied, so it tests rendering
rather than these strings, which is why both halves exist.

WHAT CHANGED, 2026-10-10
------------------------
This file is named for its first job. Until 2026-10-10 the records tile summed
``record_count_estimate`` — file size ÷ a fixed per-format divisor — over
successful ``file_validation`` rows, and this file made the measurement string
disclose that: accepted is not ingested, re-runs count again, the divisor is a
guess. The read was then REPLACED rather than relabelled: the tiles now sum
``rows_written`` on the generated Glue job's ``load_metrics`` state rows,
counted only where ``write_confirmed`` is true (see ``_get_rows_loaded``). The
estimate's disclaimers no longer describe anything, so they are gone from the
string and from this file. What is asserted now is that the string names the new
source and its confirmation rule, says it covers every source type (the estimate
could say nothing about JDBC), and does NOT describe the retired estimate.

WHAT IS ASSERTED, AND WHAT IS DELIBERATELY NOT
----------------------------------------------
The assertions are about CLAIMS, not sentences. Each required fact is asserted as
"the string says at least one of these things", and each forbidden claim as an
absence. Pinning the exact strings would make every future improvement to the
wording a test failure, and would teach the next reader to update a literal
without reading why it was there — the failure mode ``LGR-28b4d75-5`` describes
for tests whose oracle is the producer's own output.

Explicitly NOT asserted here:

* **The ``measurement`` map's key set.** That is Property 7's, in
  ``test_metrics_stage_measurement_prop.py``, derived from the API contract so
  the two cannot agree with each other by construction. Only the presence of the
  two keys this file is about is checked, as a non-vacuity guard.
* **The wire field names.** ``lines_ingested_24h`` and ``processing_rate`` are
  historical and stay — a contract test requires the keys and
  ``dashboard-react/src/types.ts`` declares them. Their presence is asserted so a
  rename shows up here as well as in the contract layer.
* **Any arithmetic.** No value produced by ``_get_rows_loaded`` or by the rate
  division is asserted; ``test_metrics_rows_loaded_char.py``,
  ``test_metrics_absence.py`` and ``test_metrics_handler.py`` own those.

MOCKING
-------
CloudWatch is outside the shared fixtures' scope (they cover DynamoDB / S3 /
SQS), so the module-level ``cloudwatch_client``, ``state_table`` and
``_cache_table`` are patched with ``MagicMock`` — the pattern
``test_metrics_absence.py`` and ``test_metrics_stage_durations_char.py`` use. No
``mock_aws()`` is created, no fixture-covered service is stubbed by hand, and no
AWS call is made.
"""

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup. tests/unit/lambdas/dashboard_api/conftest.py already does this at
# collection start; repeated here for the same reason its siblings repeat it —
# under xdist --dist=loadfile any file in this directory may be imported first.
# ---------------------------------------------------------------------------
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")

from handlers import metrics as metrics_mod

# The two tiles this file governs, and the wire names they are published under.
_ROWS = "lines_ingested_24h"
_RATE = "processing_rate"

#: Words that describe the retired `file_validation` estimate. None of them is
#: true of the rows-loaded figure, so none may appear in either string.
_RETIRED_SOURCE_WORDS = (
    "file_validation",
    "record_count_estimate",
    "estimate",
    "file size",
    "divisor",
    "b/row",
    "acceptance",
    "validation",
)


@pytest.fixture
def measurement() -> dict[str, str]:
    """``performance["measurement"]`` from one run of the real handler.

    Read off the assembled payload rather than out of the registry literal, so
    what is asserted is what a browser receives.
    """
    mock_cw = MagicMock()
    mock_cw.get_metric_data.return_value = {"MetricDataResults": []}
    mock_cw.get_metric_statistics.return_value = {"Datapoints": []}
    mock_state = MagicMock()
    mock_state.query.return_value = {"Items": []}
    mock_state.get_item.return_value = {}
    mock_state.scan.return_value = {"Items": []}

    with (
        patch.object(metrics_mod, "cloudwatch_client", mock_cw),
        patch.object(metrics_mod, "state_table", mock_state),
        patch.object(metrics_mod, "_cache_table", mock_state),
        patch.object(metrics_mod, "_get_cached_metrics", lambda period: None),
        patch.object(metrics_mod, "_cache_metrics", lambda period, metrics: None),
    ):
        response = metrics_mod.get_comprehensive_metrics({"queryStringParameters": {"period": "30d"}})

    assert response["statusCode"] == 200, f"the payload call did not return: {response['body']}"
    performance = json.loads(response["body"])["performance"]
    return dict(performance["measurement"])


def _stated(text: str, *alternatives: str) -> bool:
    """Whether ``text`` states any of ``alternatives`` (case-insensitive)."""
    lowered = text.lower()
    return any(alt.lower() in lowered for alt in alternatives)


def _retired_words_in(text: str) -> list[str]:
    """The retired-source words ``text`` still uses."""
    lowered = text.lower()
    return [word for word in _RETIRED_SOURCE_WORDS if word in lowered]


class TestBothTilesStillDeclareAMeasurement:
    """Non-vacuity. Every assertion below is over a string that has to be there."""

    def test_both_wire_names_are_present_and_unrenamed(self, measurement):
        # The names are historical (`lines_ingested_24h` covers whatever window
        # the caller asked for — 30 days here) and renaming either is a breaking
        # wire change. Asserted here as well as in the contract layer so a rename
        # cannot be quiet on this side.
        assert _ROWS in measurement, f"the rows tile stopped naming its measurement: {sorted(measurement)}"
        assert _RATE in measurement, f"the rate tile stopped naming its measurement: {sorted(measurement)}"

    def test_neither_string_is_blank(self, measurement):
        for key in (_ROWS, _RATE):
            assert measurement[key].strip(), f"{key} published an empty measurement"


class TestTheRowsMeasurementNamesItsSource:
    """What the rows-loaded string must say, and the source it must not describe."""

    def test_it_names_the_load_metrics_rows_and_the_rows_written_count(self, measurement):
        """The source is the Glue job's own record, named as the payload's reader
        would look it up: the `load_metrics` state row and its `rows_written`."""
        text = measurement[_ROWS]
        assert _stated(text, "load_metrics"), f"the load_metrics source is not named: {text!r}"
        assert _stated(text, "rows_written"), f"the rows_written count is not named: {text!r}"

    def test_it_says_only_a_confirmed_write_counts(self, measurement):
        """`rows_written` alone is rows handed to the writer (p37 risk N1); the
        figure counts it only where the job confirmed the target write."""
        text = measurement[_ROWS]
        assert _stated(text, "confirmed", "confirm"), f"the write-confirmation rule is not stated: {text!r}"
        assert _stated(text, "write"), f"nothing says what was confirmed: {text!r}"

    def test_it_says_every_source_type_is_counted(self, measurement):
        """The point of the replacement: the estimate said nothing about a JDBC
        source, which has no file. The rows-loaded count covers every source."""
        text = measurement[_ROWS]
        assert _stated(text, "every source type", "all source types", "any source type", "every source"), (
            f"the string does not say it covers every source type: {text!r}"
        )

    def test_it_does_not_describe_the_retired_estimate(self, measurement):
        """The negative form: no file-size derivation, no divisor, no
        `file_validation` acceptance — a string that still says so describes a
        read the handler no longer performs."""
        text = measurement[_ROWS]
        assert not _retired_words_in(text), (
            f"the rows measurement still describes the retired estimate ({_retired_words_in(text)}): {text!r}"
        )


class TestTheRateMeasurementSaysWhatItAverages:
    """It is the rows-loaded figure over the window's wall clock."""

    def test_it_names_the_wall_clock_hours_denominator(self, measurement):
        text = measurement[_RATE]
        assert _stated(text, "wall-clock", "wall clock"), f"the wall-clock denominator is not named: {text!r}"
        assert _stated(text, "hour"), f"the denominator's unit is not named: {text!r}"

    def test_it_says_the_numerator_is_the_rows_loaded_count(self, measurement):
        """One read, two tiles — so a reader of THIS tile learns the numerator
        without having read the other one."""
        text = measurement[_RATE]
        assert _stated(text, "rows-loaded", "rows loaded", "rows_written"), (
            f"the numerator's quantity is not named: {text!r}"
        )

    def test_it_says_it_is_not_the_speed_of_a_run(self, measurement):
        """An average over the selected window, not how fast any one run went.
        R7.3's original point — the clock is not pipeline running time — in the
        current wording."""
        text = measurement[_RATE]
        assert _stated(text, "not the speed", "not a run's speed", "not the rate of any", "not any one run"), (
            f"nothing says the rate is not a run's speed: {text!r}"
        )
        assert _stated(text, "average", "averaged"), f"the window average is not named: {text!r}"

    def test_it_refuses_the_word_throughput(self, measurement):
        text = measurement[_RATE]
        # A negated use ("not a throughput") is allowed; the claim is the
        # affirmative use: `throughput` not preceded by a negation.
        claimed = re.search(r"(?<!not a )(?<!not )\bthroughput\b", text, re.IGNORECASE)
        assert claimed is None, f"the rate measurement claims throughput: {text!r}"

    def test_it_does_not_describe_the_retired_estimate(self, measurement):
        text = measurement[_RATE]
        assert not _retired_words_in(text), (
            f"the rate measurement still describes the retired estimate ({_retired_words_in(text)}): {text!r}"
        )
