# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the JDBC-candidate Secrets Manager endpoint (P20).

Covers ``list_jdbc_candidate_secrets`` added to ``handlers/rds_resources.py``
by P20 Task 3.1.

Test scope (P20 Task 3.3):
    * Each keyword in the regex (``db``, ``database``, ``rds``, ``jdbc``,
      ``postgres``, ``mysql``, ``oracle``, ``sqlserver``) matches
    * Unrelated names are filtered out
    * Empty / single / mixed lists
    * HTTP 500 on Secrets Manager failure
    * The pre-existing ``list_rds_secrets`` continues to work unchanged

Validates: P20 Requirements 4.7, 4.8
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)


def _secrets_payload(*names: str) -> dict:
    """Helper — build a synthetic Secrets Manager ``list_secrets`` response."""
    return {
        "SecretList": [
            {
                "Name": n,
                "ARN": f"arn:aws:secretsmanager:us-west-2:123456789012:secret:{n}-AbCdEf",
                "Description": f"description for {n}",
            }
            for n in names
        ]
    }


class TestListJdbcCandidateSecrets:
    """Validates: P20 Req 4.7."""

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_each_keyword_in_pattern_matches(self, mock_sm):
        names = [
            "rds-prod",
            "database-creds",
            "main-db-secret",
            "jdbc-pool",
            "postgres-staging",
            "mysql-beta",
            "oracle-archive",
            "sqlserver-dw",
        ]
        mock_sm.list_secrets.return_value = _secrets_payload(*names)

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        # Every keyword-matching secret was returned.
        returned_names = {s["name"] for s in body["secrets"]}
        assert returned_names == set(names)
        assert body["count"] == 8

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_unrelated_names_are_filtered_out(self, mock_sm):
        mock_sm.list_secrets.return_value = _secrets_payload(
            "team-access-token-2025",
            "github-app-creds",
            "stripe-key",
        )

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        body = json.loads(response["body"])
        assert body["secrets"] == []
        assert body["count"] == 0

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_mixed_list_returns_only_matches(self, mock_sm):
        mock_sm.list_secrets.return_value = _secrets_payload(
            "rds-prod",
            "github-token",
            "postgres-replica",
            "stripe-key",
        )

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        body = json.loads(response["body"])
        returned = {s["name"] for s in body["secrets"]}
        assert returned == {"rds-prod", "postgres-replica"}

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_empty_secret_list(self, mock_sm):
        mock_sm.list_secrets.return_value = {"SecretList": []}

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        body = json.loads(response["body"])
        assert body == {"secrets": [], "count": 0}

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_single_match(self, mock_sm):
        mock_sm.list_secrets.return_value = _secrets_payload("postgres-prod")

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        body = json.loads(response["body"])
        assert body["count"] == 1
        # Each entry has exactly the documented keys per Req 4.7.
        s = body["secrets"][0]
        assert set(s.keys()) == {"arn", "name", "description"}
        assert s["name"] == "postgres-prod"

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_case_insensitive(self, mock_sm):
        mock_sm.list_secrets.return_value = _secrets_payload(
            "PROD-Postgres-Cluster",
            "MAIN-MYSQL",
            "Backup-DataBase-Access",
        )

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        body = json.loads(response["body"])
        # All three names match the regex case-insensitively.
        assert body["count"] == 3

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_client_error_returns_500(self, mock_sm):
        mock_sm.list_secrets.side_effect = ClientError(
            {"Error": {"Code": "InternalServerError", "Message": "sm boom"}},
            "ListSecrets",
        )

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        assert response["statusCode"] == 500
        body = json.loads(response["body"])
        assert body["error"] == "Failed to list JDBC candidate secrets"
        assert "sm boom" in body["message"]


class TestRdsSecretsUsesAlignedPattern:
    """Validates: P20 Req 4.8 (relaxed) — P14 endpoint behaviour preserved
    for `rds`/`db`/`database`/`jdbc` matches; broadened to also surface
    engine-named secrets so the test-fixtures' `agentic-data-ingestion/test-fixtures/
    mysql-source` and `postgres-target` appear in the legacy
    `select_rds`-mode dropdown alongside the new manual-mode dropdown."""

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_list_rds_secrets_keeps_legacy_keyword_matches(self, mock_sm):
        """Existing keyword matches still pass the filter."""
        mock_sm.list_secrets.return_value = _secrets_payload(
            "rds-prod",
            "team-access-token",
        )

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        body = json.loads(response["body"])
        names = {s["name"] for s in body["secrets"]}
        # Legacy match still works.
        assert "rds-prod" in names
        # Non-matching secret still filtered out.
        assert "team-access-token" not in names

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_list_rds_secrets_now_surfaces_engine_named_secrets(self, mock_sm):
        """P20 alignment: engine-named secrets (e.g.
        `agentic-data-ingestion/test-fixtures/mysql-source`) now appear in the
        legacy dropdown so RDS-mode users see the same Secrets Manager
        secrets the manual-mode dropdown surfaces."""
        mock_sm.list_secrets.return_value = _secrets_payload(
            "agentic-data-ingestion/test-fixtures/mysql-source",
            "agentic-data-ingestion/test-fixtures/postgres-target",
            "team-access-token-2025",
        )

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        body = json.loads(response["body"])
        names = {s["name"] for s in body["secrets"]}
        # Engine-named secrets now appear.
        assert "agentic-data-ingestion/test-fixtures/mysql-source" in names
        assert "agentic-data-ingestion/test-fixtures/postgres-target" in names
        # Non-matching secret still filtered out.
        assert "team-access-token-2025" not in names


class TestPagination:
    """Validates: P20 — secrets on page 2+ MUST be returned.

    Secrets Manager's ``ListSecrets`` API paginates with a default
    ``MaxResults`` of 10. Without pagination support, secrets that
    happen to land on page 2 (e.g. the test-fixtures
    ``agentic-data-ingestion/test-fixtures/mysql-source`` in our dev account) are
    invisible to the wizard's Secret dropdown.
    """

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_list_jdbc_candidate_secrets_follows_next_token(self, mock_sm):
        """Multi-page response: matches across BOTH pages are returned."""
        mock_sm.list_secrets.side_effect = [
            {
                "SecretList": [
                    {
                        "Name": f"page1-team-{i}",
                        "ARN": (f"arn:aws:secretsmanager:us-west-2:1:secret:p1-{i}"),
                        "Description": "",
                    }
                    for i in range(10)
                ]
                + [
                    {
                        "Name": "page1-mysql-prod",
                        "ARN": ("arn:aws:secretsmanager:us-west-2:1:secret:mysql-prod"),
                        "Description": "",
                    }
                ],
                "NextToken": "page2-token",
            },
            {
                "SecretList": [
                    {
                        "Name": "agentic-data-ingestion/test-fixtures/mysql-source",
                        "ARN": (
                            "arn:aws:secretsmanager:us-west-2:1:secret:agentic-data-ingestion/test-fixtures/mysql-source"
                        ),
                        "Description": "",
                    },
                    {
                        "Name": "agentic-data-ingestion/test-fixtures/postgres-target",
                        "ARN": (
                            "arn:aws:secretsmanager:us-west-2:1:secret:agentic-data-ingestion/test-fixtures/postgres-target"
                        ),
                        "Description": "",
                    },
                ],
                # No NextToken → stop paginating.
            },
        ]

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])
        names = {s["name"] for s in body["secrets"]}
        # Page-1 match included.
        assert "page1-mysql-prod" in names
        # Page-2 matches included — this is the regression we're fixing.
        assert "agentic-data-ingestion/test-fixtures/mysql-source" in names
        assert "agentic-data-ingestion/test-fixtures/postgres-target" in names
        # ListSecrets was invoked twice (once per page).
        assert mock_sm.list_secrets.call_count == 2

    @patch("handlers.rds_resources.secretsmanager_client")
    def test_list_rds_secrets_follows_next_token(self, mock_sm):
        """Same fix applied to the legacy P14 endpoint."""
        mock_sm.list_secrets.side_effect = [
            {
                "SecretList": [
                    {
                        "Name": "page1-team-token",
                        "ARN": ("arn:aws:secretsmanager:us-west-2:1:secret:page1-team-token"),
                        "Description": "",
                    },
                ],
                "NextToken": "tok",
            },
            {
                "SecretList": [
                    {
                        "Name": "agentic-data-ingestion/test-fixtures/mysql-source",
                        "ARN": ("arn:aws:secretsmanager:us-west-2:1:secret:mysql-source"),
                        "Description": "",
                    },
                ],
            },
        ]

        from handlers.rds_resources import list_rds_secrets

        response = list_rds_secrets({}, None)
        body = json.loads(response["body"])
        names = {s["name"] for s in body["secrets"]}
        assert "agentic-data-ingestion/test-fixtures/mysql-source" in names
        assert mock_sm.list_secrets.call_count == 2
