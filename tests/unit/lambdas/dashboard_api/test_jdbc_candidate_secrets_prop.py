# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test for JDBC-candidate Secrets Manager endpoint (P20).

Validates: P20 Requirements 4.7, Property 9.
"""

from __future__ import annotations

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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


# Feature: p20-jdbc-manual-mode, Property 9: JdbcCandidateSecrets filters by name regex
class TestProperty9CandidateSecretsFiltersByRegex:
    """Property 9: ``JdbcCandidateSecrets`` filters by name regex.

    *For any* list of secret names, the endpoint returns exactly the
    secrets whose name (case-insensitive) matches
    ``/db|database|rds|jdbc|postgres|mysql|oracle|sqlserver/i`` AND
    each returned object has exactly ``{arn, name, description}``.

    Validates: P20 Requirements 4.7
    """

    _PATTERN = re.compile(
        r"db|database|rds|jdbc|postgres|mysql|oracle|sqlserver",
        re.IGNORECASE,
    )

    @given(
        names=st.lists(
            st.text(
                alphabet=st.characters(
                    min_codepoint=ord("0"),
                    max_codepoint=ord("z"),
                    whitelist_characters="-_",
                ),
                min_size=1,
                max_size=24,
            ),
            min_size=0,
            max_size=20,
        )
    )
    @settings(max_examples=100)
    @patch("handlers.rds_resources.secretsmanager_client")
    def test_returned_set_equals_regex_matches(self, mock_sm, names: list[str]):
        """Returned secrets exactly match those passing the regex."""
        mock_sm.list_secrets.return_value = {
            "SecretList": [
                {
                    "Name": n,
                    "ARN": (f"arn:aws:secretsmanager:us-west-2:123456789012:secret:{n}-AbCdEf"),
                    "Description": f"d-{n}",
                }
                for n in names
            ]
        }

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        assert response["statusCode"] == 200
        body = json.loads(response["body"])

        expected = {n for n in names if self._PATTERN.search(n)}
        actual = {s["name"] for s in body["secrets"]}
        assert actual == expected, f"Returned set differs from regex-matched set. Expected {expected}, got {actual}"

        # Each returned object has exactly the documented key set.
        for secret in body["secrets"]:
            assert set(secret.keys()) == {"arn", "name", "description"}
