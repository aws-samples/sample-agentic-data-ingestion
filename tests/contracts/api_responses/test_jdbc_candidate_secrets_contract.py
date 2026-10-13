# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /api/resources/secrets/jdbc-candidates response shape (P20).

Pins the wire shape:

    HTTP 200 → {"secrets": [{arn, name, description}], "count": N}

Validates: P20 Requirements 4.7
"""

from __future__ import annotations

import json
import os
import sys
from unittest.mock import MagicMock, patch

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"
sys.modules["env_utils"] = mock_env_utils

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)


class TestListJdbcCandidateSecretsResponseShape:
    @patch("handlers.rds_resources.secretsmanager_client")
    def test_200_response_shape(self, mock_sm):
        mock_sm.list_secrets.return_value = {
            "SecretList": [
                {
                    "Name": "rds-prod-postgres",
                    "ARN": "arn:aws:secretsmanager:us-west-2:123456789012:secret:rds-prod-postgres-AbCdEf",
                    "Description": "Production Postgres credentials",
                },
                {
                    "Name": "team-access-token",
                    "ARN": "arn:aws:secretsmanager:us-west-2:123456789012:secret:team-access-token-XyZ",
                    "Description": "Team API token",
                },
            ]
        }

        from handlers.rds_resources import list_jdbc_candidate_secrets

        response = list_jdbc_candidate_secrets({}, None)
        assert response["statusCode"] == 200
        assert response["headers"]["Content-Type"] == "application/json"
        body = json.loads(response["body"])

        assert set(body.keys()) == {"secrets", "count"}
        assert body["count"] == len(body["secrets"])

        for secret in body["secrets"]:
            assert set(secret.keys()) == {"arn", "name", "description"}
            assert isinstance(secret["arn"], str)
            assert isinstance(secret["name"], str)
            assert isinstance(secret["description"], str)
