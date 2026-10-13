# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""P28 R12 — identifier validation + parameterized values in the Athena
query builder (data_quality_agent).

Every identifier the agent f-strings into Athena SQL — table names, null-rate
columns (discovered from user-influenced table metadata), duplicate-check key
columns (payload-supplied), and the freshness timestamp column — is validated
against the Identifier_Allowlist BEFORE query assembly. A hostile value fails
the check loudly, naming the identifier (R12.2), and no Athena query is ever
started.

Data VALUES (the database/table strings in the ``information_schema`` lookup)
are bound via Athena ``ExecutionParameters`` with ``?`` placeholders (R12.3)
instead of being interpolated into the query text.

Hostile inputs cover quotes, semicolons, newlines, and SQL comment tokens
(R12.4).

**Validates: Requirements 12.1, 12.2, 12.3, 12.4**
"""

import importlib.util
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

AGENT_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "agentcore", "data_quality_agent")
)
SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
MAIN_PATH = os.path.join(AGENT_DIR, "main.py")

sys.path.insert(0, AGENT_DIR)
sys.path.insert(0, SHARED_DIR)

from identifier_utils import IdentifierValidationError

# ── Import the agent with a PASSTHROUGH @app.entrypoint ─────────────
# (same pattern as test_report_bounds.py: a private module copy under a
# unique sys.modules name, with `entrypoint` as the identity decorator so
# the real handler is callable.)

_mock_boto3 = MagicMock()
_mock_runtime = MagicMock()
_mock_runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
_mock_bedrock_agentcore = MagicMock()
_mock_bedrock_agentcore.runtime = _mock_runtime

with patch.dict(
    "sys.modules",
    {
        "bedrock_agentcore": _mock_bedrock_agentcore,
        "bedrock_agentcore.runtime": _mock_runtime,
        "boto3": _mock_boto3,
        "agent_trace_plugin": MagicMock(),
        "audit_utils": MagicMock(),
        "env_utils": MagicMock(),
    },
):
    _spec = importlib.util.spec_from_file_location("dqa_injection_main", MAIN_PATH)
    dqa = importlib.util.module_from_spec(_spec)
    sys.modules["dqa_injection_main"] = dqa
    _spec.loader.exec_module(dqa)


# Hostile identifiers per R12.4: quotes, semicolons, newlines, comment tokens.
HOSTILE_IDENTIFIERS = [
    pytest.param('col"; DROP TABLE x; --', id="double-quote-semicolon-comment"),
    pytest.param("col'; DROP TABLE x; --", id="single-quote-semicolon-comment"),
    pytest.param("col; DELETE FROM t", id="semicolon"),
    pytest.param("col\nDROP TABLE x", id="newline"),
    pytest.param("col--comment", id="line-comment-token"),
    pytest.param("col/*comment*/", id="block-comment-token"),
]


@pytest.fixture()
def no_athena(monkeypatch):
    """Fail the test if any Athena query is started."""

    def _forbidden(query, database, execution_parameters=None):  # pragma: no cover - guard
        raise AssertionError(f"no Athena query may run for a hostile identifier: {query}")

    monkeypatch.setattr(dqa, "run_athena_query", _forbidden)


# ── R12.1/R12.2/R12.4: hostile identifiers never reach SQL ──────────


class TestCheckRowCount:
    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_table_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError) as exc_info:
            dqa.check_row_count("db", hostile, 100)
        assert exc_info.value.name == hostile

    def test_skip_branch_still_wins_over_validation(self, no_athena):
        """No expectation → skipped: the identifier never enters SQL, so the
        skip result (R11.4) is returned without validation noise."""
        result = dqa.check_row_count("db", 'tbl"; DROP TABLE x; --', None)
        assert result["status"] == "skipped"


class TestCheckNullRates:
    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_column_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError) as exc_info:
            dqa.check_null_rates("db", "tbl", ["email", hostile])
        assert exc_info.value.name == hostile
        assert "null-rate" in exc_info.value.context

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_table_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError):
            dqa.check_null_rates("db", hostile, ["email"])

    def test_empty_columns_short_circuits(self, no_athena):
        assert dqa.check_null_rates("db", "tbl", []) == []


class TestCheckDuplicates:
    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_key_column_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError) as exc_info:
            dqa.check_duplicates("db", "tbl", ["customer_id", hostile])
        assert exc_info.value.name == hostile

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_table_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError):
            dqa.check_duplicates("db", hostile, ["customer_id"])

    def test_no_key_columns_short_circuits(self, no_athena):
        result = dqa.check_duplicates("db", 'tbl"; DROP TABLE x; --', [])
        assert result["status"] == "pass"


class TestCheckFreshness:
    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_timestamp_column_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError) as exc_info:
            dqa.check_freshness("db", "tbl", hostile)
        assert exc_info.value.name == hostile

    @pytest.mark.parametrize("hostile", HOSTILE_IDENTIFIERS)
    def test_hostile_table_raises_without_querying(self, no_athena, hostile):
        with pytest.raises(IdentifierValidationError):
            dqa.check_freshness("db", hostile, "updated_at")


# ── R12.3: parameterized Athena values ──────────────────────────────


def _wire_athena_client(monkeypatch):
    """Replace the module's Athena client with a canned-success mock."""
    client = MagicMock()
    client.start_query_execution.return_value = {"QueryExecutionId": "qid-1"}
    client.get_query_execution.return_value = {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}}
    client.get_query_results.return_value = {"ResultSet": {"Rows": []}}
    monkeypatch.setattr(dqa, "_athena_client", client)
    monkeypatch.setattr(dqa, "_require_env", lambda name, default=None: default or "test-bucket")
    return client


class TestRunAthenaQueryExecutionParameters:
    def test_parameters_are_passed_to_start_query_execution(self, monkeypatch):
        client = _wire_athena_client(monkeypatch)
        dqa.run_athena_query(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = ? AND table_name = ?",
            "db",
            execution_parameters=["'db'", "'tbl'"],
        )
        kwargs = client.start_query_execution.call_args.kwargs
        assert kwargs["ExecutionParameters"] == ["'db'", "'tbl'"]

    def test_no_parameters_key_when_absent(self, monkeypatch):
        client = _wire_athena_client(monkeypatch)
        dqa.run_athena_query("SELECT 1", "db")
        kwargs = client.start_query_execution.call_args.kwargs
        assert "ExecutionParameters" not in kwargs


class TestHandlerInformationSchemaParameterization:
    """The handler's column-discovery lookup binds database/table as
    parameters — never inline string literals.

    **Validates: Requirements 12.3**
    """

    def _invoke(self, monkeypatch):
        calls = []

        def _recording_router(query, database, execution_parameters=None):
            calls.append({"query": query, "execution_parameters": execution_parameters})
            return []  # no columns → no downstream null-rate/freshness queries

        state_table = MagicMock()
        dynamo = MagicMock()
        dynamo.Table.return_value = state_table
        monkeypatch.setattr(dqa, "_dynamodb", dynamo)
        monkeypatch.setattr(dqa, "_s3_client", MagicMock())
        monkeypatch.setattr(dqa, "_require_env", lambda name, default=None: default or "unset")
        monkeypatch.setattr(dqa, "run_athena_query", _recording_router)

        with patch.dict(
            "sys.modules",
            {
                "agent_trace_plugin": MagicMock(),
                "audit_utils": MagicMock(),
                "env_utils": MagicMock(),
            },
        ):
            dqa.handler(
                {
                    "run_id": "run-param-1",
                    "pipeline_id": "pipe-1",
                    "target_table_info": {"database": "db", "table": "tbl"},
                }
            )
        return calls

    def test_information_schema_query_uses_placeholders_and_parameters(self, monkeypatch):
        calls = self._invoke(monkeypatch)
        info_calls = [c for c in calls if "information_schema" in c["query"]]
        assert len(info_calls) == 1
        call = info_calls[0]

        assert "table_schema = ?" in call["query"]
        assert "table_name = ?" in call["query"]
        # The values travel as parameters (pre-quoted per the Athena API
        # contract), never inline in the query text.
        assert call["execution_parameters"] == ["'db'", "'tbl'"]
        assert "'db'" not in call["query"]
        assert "'tbl'" not in call["query"]


class TestHandlerHostileTableNeverQueries:
    """End-to-end: a hostile table name produces a loudly-failed report and
    ZERO Athena queries.

    **Validates: Requirements 12.1, 12.2, 12.4**
    """

    def test_hostile_table_fails_checks_and_queries_nothing(self, monkeypatch):
        hostile_table = 'tbl"; DROP TABLE x; --'

        def _forbidden(query, database, execution_parameters=None):  # pragma: no cover - guard
            raise AssertionError(f"no Athena query may run for a hostile identifier: {query}")

        state_table = MagicMock()
        dynamo = MagicMock()
        dynamo.Table.return_value = state_table
        monkeypatch.setattr(dqa, "_dynamodb", dynamo)
        monkeypatch.setattr(dqa, "_s3_client", MagicMock())
        monkeypatch.setattr(dqa, "_require_env", lambda name, default=None: default or "unset")
        monkeypatch.setattr(dqa, "run_athena_query", _forbidden)

        with patch.dict(
            "sys.modules",
            {
                "agent_trace_plugin": MagicMock(),
                "audit_utils": MagicMock(),
                "env_utils": MagicMock(),
            },
        ):
            report = dqa.handler(
                {
                    "run_id": "run-hostile-1",
                    "pipeline_id": "pipe-1",
                    "target_table_info": {"database": "db", "table": hostile_table},
                    "source_row_count": 100,
                    "primary_key_columns": ["customer_id"],
                }
            )

        # Failure is loud and names the identifier — no sanitize-and-continue.
        assert report["overall_status"] == "fail"
        errors = " ".join(c.get("error", "") for c in report["checks"])
        assert "Invalid identifier" in errors
        assert "DROP TABLE" in errors  # the offending value is named


# ── Reserved words: validated identifiers are also quoted ───────────
#
# The allowlist makes a name safe to interpolate, not correct to interpolate.
# Athena's SELECT grammar reserves words that pass it (ORDER, GROUP, END,
# FIRST, LAST, SKIP, TABLE, VALUES, ...), and an unquoted reserved word is a
# parse error: `SELECT order FROM ...` is rejected at submission with
# "mismatched input 'order'". Athena's documented rule is that such names must
# be enclosed in double quotes in SELECT statements, and quoted identifiers
# stay case-insensitive, so every interpolated identifier is quoted.
#
# Reserved words below are taken from Athena's list of keywords to escape in
# SQL SELECT statements (docs: athena/latest/ug/reserved-words.html).

ATHENA_SELECT_RESERVED = ["order", "group", "end", "first", "last", "skip", "table", "values"]


def _bare(word: str) -> re.Pattern[str]:
    """Match ``word`` as a whole identifier that is NOT enclosed in quotes.

    ``\\b`` treats ``_`` as a word character, so ``null_rate_order`` does not
    count as a bare ``order``; only a free-standing token does.
    """
    return re.compile(rf'(?<!")\b{re.escape(word)}\b(?!")')


@pytest.fixture()
def recorded_queries(monkeypatch):
    """Record every query the checks submit; answer with plausible rows so the
    follow-up sample queries (null rows, duplicate keys) are also issued."""
    queries: list[str] = []

    def _router(query, database, execution_parameters=None):
        queries.append(query)
        if "AS row_count" in query:
            return [{"row_count": "10"}]
        if "SUM(CASE WHEN" in query:
            row = {"total_rows": "10"}
            # Athena names a result column exactly as the alias is spelled,
            # quoted or not (verified live), so model that rather than one form.
            row.update({key: "0.5" for key in re.findall(r'AS "?(null_rate_\w+)"?', query)})
            return [row]
        if "dup_count" in query:
            return [{"dup_count": "2"}]
        if "latest_record" in query:
            return [{"latest_record": "2026-01-01 00:00:00", "check_time": "2026-01-01 00:00:00"}]
        return []

    monkeypatch.setattr(dqa, "run_athena_query", _router)
    return queries


class TestReservedWordIdentifiersAreQuoted:
    @pytest.mark.parametrize("word", ATHENA_SELECT_RESERVED)
    def test_row_count_quotes_the_table(self, recorded_queries, word):
        dqa.check_row_count("db", word, 10)
        (query,) = recorded_queries
        assert f'FROM "{word}"' in query
        assert not _bare(word).search(query), query

    @pytest.mark.parametrize("word", ATHENA_SELECT_RESERVED)
    def test_null_rates_quotes_every_column_and_the_table(self, recorded_queries, word):
        dqa.check_null_rates("db", word, ["email", word])
        aggregate, *samples = recorded_queries
        assert f'WHEN "{word}" IS NULL' in aggregate
        assert 'WHEN "email" IS NULL' in aggregate
        assert f'FROM "{word}"' in aggregate
        # One sample query per column over threshold (both are, at 0.5).
        assert len(samples) == 2
        assert all(f'FROM "{word}" WHERE "' in s for s in samples)
        for query in recorded_queries:
            assert not _bare(word).search(query), query

    def test_null_rates_alias_contract_is_unchanged(self, recorded_queries):
        """The check reads each rate back by its alias. Quoting the column must
        not change the name the rate comes back under, or a reserved-word
        column would silently read as 0 and pass."""
        checks = dqa.check_null_rates("db", "tbl", ["order"])
        (check,) = checks
        assert check["name"] == "null_rate_order"
        assert check["rate"] == 0.5
        assert check["status"] == "warning"

    @pytest.mark.parametrize("word", ATHENA_SELECT_RESERVED)
    def test_duplicates_quotes_key_columns_and_the_table(self, recorded_queries, word):
        dqa.check_duplicates("db", word, ["customer_id", word])
        count_query, sample_query = recorded_queries
        for query in (count_query, sample_query):
            assert f'"customer_id", "{word}"' in query
            assert f'FROM "{word}"' in query
            assert not _bare(word).search(query), query

    @pytest.mark.parametrize("word", ATHENA_SELECT_RESERVED)
    def test_freshness_quotes_the_column_and_the_table(self, recorded_queries, word):
        dqa.check_freshness("db", word, word)
        (query,) = recorded_queries
        assert f'MAX("{word}")' in query
        assert f'FROM "{word}"' in query
        assert not _bare(word).search(query), query

    def test_quoting_does_not_bypass_validation(self):
        """The quote is only safe because the allowlist excludes it."""
        with pytest.raises(IdentifierValidationError):
            dqa._quoted_identifier('col" OR "1"="1', "test")
        assert dqa._quoted_identifier("Order_Id", "test") == '"Order_Id"'
