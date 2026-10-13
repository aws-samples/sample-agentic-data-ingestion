# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for data quality agent: row count, duplicates, report generation, root cause."""

import ast
import os
import re
import sys
from unittest.mock import MagicMock, patch

AGENT_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "data_quality_agent")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

_mock_athena = MagicMock()
_mock_boto3 = MagicMock()
_mock_boto3.client.return_value = _mock_athena
_mock_boto3.resource.return_value = MagicMock()

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": MagicMock(),
        "bedrock_agentcore.runtime": MagicMock(),
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    import main as dqa_main

    dqa_main._athena_client = _mock_athena
    dqa_main._require_env = MagicMock(return_value="agentic-data-ingestion-athena-results")


def _setup_athena_query(rows):
    """Helper to mock a successful Athena query returning given rows.

    rows: list of dicts — first dict keys become headers.
    """
    if not rows:
        _mock_athena.get_query_results.return_value = {"ResultSet": {"Rows": []}}
        return

    headers = list(rows[0].keys())
    header_row = {"Data": [{"VarCharValue": h} for h in headers]}
    data_rows = [{"Data": [{"VarCharValue": str(row.get(h, ""))} for h in headers]} for row in rows]
    _mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    _mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    _mock_athena.get_query_results.return_value = {"ResultSet": {"Rows": [header_row] + data_rows}}


# ── check_row_count ─────────────────────────────────────────────────


class TestCheckRowCount:
    def test_pass_when_counts_match(self):
        _setup_athena_query([{"row_count": "1500"}])
        result = dqa_main.check_row_count("db", "tbl", 1500)
        assert result["status"] == "pass"
        assert result["actual"] == 1500

    def test_fail_when_counts_differ(self):
        _setup_athena_query([{"row_count": "1200"}])
        result = dqa_main.check_row_count("db", "tbl", 1500)
        assert result["status"] == "fail"
        assert result["actual"] == 1200
        assert result["expected"] == 1500


# ── check_duplicates ────────────────────────────────────────────────


class TestCheckDuplicates:
    def test_no_duplicates(self):
        _setup_athena_query([{"dup_count": "0"}])
        result = dqa_main.check_duplicates("db", "tbl", ["customer_id"])
        assert result["status"] == "pass"
        assert result["duplicates"] == 0

    def test_duplicates_found(self):
        # First call: count query returns 3 duplicates
        # Second call: sample query returns sample rows
        call_count = [0]
        original_get_results = _mock_athena.get_query_results

        def side_effect(**kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return {
                    "ResultSet": {
                        "Rows": [
                            {"Data": [{"VarCharValue": "dup_count"}]},
                            {"Data": [{"VarCharValue": "3"}]},
                        ]
                    }
                }
            return {
                "ResultSet": {
                    "Rows": [
                        {"Data": [{"VarCharValue": "customer_id"}, {"VarCharValue": "cnt"}]},
                        {"Data": [{"VarCharValue": "1001"}, {"VarCharValue": "2"}]},
                    ]
                }
            }

        _mock_athena.get_query_results.side_effect = side_effect
        _mock_athena.start_query_execution.return_value = {"QueryExecutionId": "qid-2"}
        _mock_athena.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}

        result = dqa_main.check_duplicates("db", "tbl", ["customer_id"])
        assert result["status"] == "fail"
        assert result["duplicates"] == 3
        _mock_athena.get_query_results.side_effect = None

    def test_no_key_columns_skips(self):
        result = dqa_main.check_duplicates("db", "tbl", [])
        assert result["status"] == "pass"
        assert result["duplicates"] == 0


# ── generate_report ─────────────────────────────────────────────────


class TestGenerateReport:
    def test_all_pass(self):
        checks = [
            {"name": "row_count", "status": "pass"},
            {"name": "duplicate_keys", "status": "pass"},
        ]
        report = dqa_main.generate_report("pipe-1", "run-1", checks)
        assert report["overall_status"] == "pass"
        assert report["root_cause"] is None
        assert report["recommendations"] == []

    def test_warning_status(self):
        checks = [
            {"name": "row_count", "status": "pass"},
            {"name": "null_rate_email", "status": "warning", "rate": 0.12, "threshold": 0.05},
        ]
        report = dqa_main.generate_report("pipe-1", "run-1", checks)
        assert report["overall_status"] == "warning"
        assert len(report["recommendations"]) == 1

    def test_fail_status(self):
        checks = [
            {"name": "row_count", "status": "fail", "expected": 1500, "actual": 1200},
            {"name": "duplicate_keys", "status": "pass"},
        ]
        report = dqa_main.generate_report("pipe-1", "run-1", checks)
        assert report["overall_status"] == "fail"

    def test_idempotent_report(self):
        checks = [{"name": "row_count", "status": "pass"}]
        r1 = dqa_main.generate_report("pipe-1", "run-1", checks)
        r2 = dqa_main.generate_report("pipe-1", "run-1", checks)
        assert r1 == r2


# ── _classify_root_cause ────────────────────────────────────────────


class TestClassifyRootCause:
    def test_no_failures_returns_none(self):
        checks = [{"name": "row_count", "status": "pass"}]
        assert dqa_main._classify_root_cause(checks) is None

    def test_duplicate_keys_is_transform_issue(self):
        checks = [{"name": "duplicate_keys", "status": "fail"}]
        assert dqa_main._classify_root_cause(checks) == "transform_issue"

    def test_row_count_is_transform_issue(self):
        checks = [{"name": "row_count", "status": "fail"}]
        assert dqa_main._classify_root_cause(checks) == "transform_issue"

    def test_freshness_is_infrastructure_issue(self):
        checks = [{"name": "freshness", "status": "fail"}]
        assert dqa_main._classify_root_cause(checks) == "infrastructure_issue"

    def test_null_rate_is_data_issue(self):
        checks = [{"name": "null_rate_email", "status": "warning"}]
        assert dqa_main._classify_root_cause(checks) == "data_issue"


# ---------------------------------------------------------------------------
# Producer/consumer drift guard — this agent as the SECOND producer of `checks[]`
# ---------------------------------------------------------------------------
#
# `QualityReportCard.formatActualValue` treats any check name absent from
# `COUNT_VALUED_CHECKS` as a 0-1 ratio and multiplies it by 100, so a count that
# is not classified renders as a fabricated percentage. `LGR-f80f6a2-1` fixed
# that for the generated Glue job and guarded it on the template's own gate, and
# left this producer open in its own words: "The on-demand quality agent is one
# today and its `row_count` still renders as a percentage; add it as a second
# parsed producer rather than loosening the match." An agent-reported 1000 rows
# rendered as "100000%" — `row_count` is snake_case and never matched the
# `rowcount` key that classifies the DQDL rule.
#
# The template-side guard cannot see any of this: it parses
# `_partials/_data_quality.py.j2` and nothing else. So the second producer needs
# its own guard, and it lives HERE, beside the producer, because a change to
# `agentcore/data_quality_agent/main.py` runs
# `make test-scope DIR=tests/unit/agents/data_quality_agent/` and never runs
# `make test-frontend`.
#
# WHAT IS ASSERTED IS THE `actual` KEY, NOT THE NAME LIST. Only a check whose
# dict carries `actual` reaches `formatActualValue` — the consumer guards the
# `Actual:` row on `check.actual != null`. Every other check here reports under a
# key of its own (`rate`, `duplicates`, `latest_record`, `actual_age_hours`) and
# reaches a different render branch. Keying the guard on name alone would demand
# a fabricated unit for `freshness`, whose value is HOURS and is not rendered at
# all. So the AST walk below looks for dict literals carrying BOTH keys, which is
# exactly the defect condition.
#
# The consumer's map is PARSED, never transcribed — a copied key list here could
# not detect the consumer dropping an entry. That is the same reason the
# template-side guard parses it. The regexes are duplicated rather than shared:
# both sides still derive from source, and importing across two test modules
# would drag this module's `sys.modules` mocking into that one.

AGENT_SOURCE = os.path.join(AGENT_DIR, "main.py")

QUALITY_CARD_TSX = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "dashboard-react", "src", "components", "QualityReportCard.tsx"
)

#: The consumer's classification map, parsed out of its declaration.
_COUNT_MAP_BLOCK = re.compile(r"const COUNT_VALUED_CHECKS:[^=]*=\s*\{(.*?)\};", re.DOTALL)
_COUNT_MAP_KEY = re.compile(r"([A-Za-z][A-Za-z0-9_]*)\s*:\s*['\"]([^'\"]+)['\"]")


def _agent_actual_bearing_names() -> tuple[set[str], list[str]]:
    """Parse the agent for check dicts that carry an ``actual``.

    Returns ``(literal_names, dynamic_name_exprs)``. An AST walk rather than a
    regex, for two reasons: this producer is real Python (the template one is
    Jinja and has no choice), and `ast.parse` drops comments entirely, so this
    scan cannot be tripped by prose that happens to name a check — the
    self-matching hazard `LGR-b46a5bb-4` records.
    """
    with open(AGENT_SOURCE, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())

    literal: set[str] = set()
    dynamic: list[str] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = {key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str)}
        if not {"name", "actual"} <= keys:
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if not (isinstance(key, ast.Constant) and key.value == "name"):
                continue
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                literal.add(value.value)
            else:
                dynamic.append(ast.unparse(value))

    return literal, dynamic


def _consumer_count_units() -> dict[str, str]:
    with open(QUALITY_CARD_TSX, encoding="utf-8") as fh:
        source = fh.read()
    block = _COUNT_MAP_BLOCK.search(source)
    assert block is not None, (
        f"`const COUNT_VALUED_CHECKS = {{...}}` was not found in {QUALITY_CARD_TSX}. This guard "
        f"parses that declaration; if it was renamed or reshaped, update this test rather than "
        f"leaving it to pass vacuously — a guard that cannot find its subject protects nothing."
    )
    return dict(_COUNT_MAP_KEY.findall(block.group(1)))


class TestAgentCheckNameClassification:
    """The agent's count-valued checks must be classified by the dashboard."""

    def test_the_two_files_this_guard_reads_exist(self):
        """Fail loudly on a moved file instead of quietly asserting nothing."""
        assert os.path.isfile(AGENT_SOURCE), (
            f"{AGENT_SOURCE} is missing. This guard derives its expectations from that file."
        )
        assert os.path.isfile(QUALITY_CARD_TSX), (
            f"{QUALITY_CARD_TSX} is missing. If the component moved, repoint the path."
        )

    def test_both_halves_of_this_guard_are_non_empty(self):
        """The paired positive: neither side may silently degrade to empty.

        Both are derived from source, so either one coming back empty would make
        the coverage assertion below trivially true. This is the test that fails
        first if that happens.
        """
        literal, _dynamic = _agent_actual_bearing_names()
        units = _consumer_count_units()

        assert literal, (
            f"No check dict carrying both `name` and `actual` was found in {AGENT_SOURCE}. Either "
            f"the agent stopped reporting an `actual` or the AST walk no longer matches its shape."
        )
        assert units, (
            "`COUNT_VALUED_CHECKS` parsed as empty. Either the consumer's map lost every entry or "
            "`_COUNT_MAP_KEY` no longer matches its shape."
        )
        # Asserted by name so a silent producer refactor is a failure here rather
        # than a shrinking guard nobody notices.
        assert "row_count" in literal, (
            f"the agent no longer reports `row_count` with an `actual`: found {sorted(literal)}"
        )

    def test_every_agent_check_with_an_actual_is_classified_by_the_dashboard(self):
        """An unclassified count is rendered as a percentage of nothing.

        If this fails, add the name to `COUNT_VALUED_CHECKS` in
        `QualityReportCard.tsx` with the noun the agent counts — read off what is
        assigned to `actual`, not guessed from the name.
        """
        literal, _dynamic = _agent_actual_bearing_names()
        units = _consumer_count_units()

        unclassified = sorted(name for name in literal if name.lower() not in units)

        assert not unclassified, (
            f"{unclassified} are reported by {AGENT_SOURCE} with an `actual` but carry no unit in "
            f"`COUNT_VALUED_CHECKS` ({sorted(units)}) in QualityReportCard.tsx. The consumer's "
            f"fall-through branch multiplies an unclassified value by 100, so each renders as a "
            f"percentage of nothing — an agent-reported 1000 rows as '100000%'. Add the name with "
            f"the noun the producer counts."
        )

    def test_no_agent_check_with_an_actual_has_a_dynamic_name(self):
        """A per-name map cannot enumerate a name built at runtime.

        None exists today: the one dynamically-named check, `null_rate_<column>`,
        reports under `rate` and never reaches `formatActualValue`. If that ever
        changes, the map is the wrong shape for it and the fix is a narrow,
        explicit pattern on the consumer — not relaxing the whole-name lookup,
        which `test_the_classification_is_whole_name_and_not_a_substring` in
        `tests/unit/templates/test_data_quality_all_targets.py` forbids.
        """
        _literal, dynamic = _agent_actual_bearing_names()

        assert not dynamic, (
            f"{dynamic} build a check name at runtime AND report an `actual`, so no per-name map "
            f"can classify them. Revisit the classification shape: add a narrow, explicitly-scoped "
            f"pattern for this family on the consumer, and keep the exact whole-name lookup for "
            f"everything else."
        )
