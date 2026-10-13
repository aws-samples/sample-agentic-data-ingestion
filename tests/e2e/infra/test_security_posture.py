# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E: security-posture checks against the deployed AWS environment.

Conformance: Requirement 24 — brought under the shared E2E_Harness contracts.

These close coverage gaps flagged in the E2E_TEST_PLAN for which only
infrastructure-level verification is meaningful:

  TC-14.1  API Gateway IAM auth
  TC-14.3  S3 buckets block public access
  TC-14.4  DynamoDB encryption at rest
  TC-14.7  Bedrock access scoped (no bedrock:* wildcard)
  TC-14.8  WebSocket auth (KNOWN GAP — xfail, tracked by Req 18)

**Harness conformance:**
- Gated by Real_AWS_Gate (session autouse via ``require_real_aws``)
- Read-only: all assertions query deployed config without mutation
- No ``requires_trigger`` needed — does not start or mutate executions
- No Cleanup_Contract obligations (no artifacts created)
- Reuses E2E_Harness fixtures (``boto3_session``, ``resource_names``,
  ``rest_api_endpoint``, ``account_id``)

Run with: AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true make test-e2e
"""

import urllib.error
import urllib.request

import pytest


class TestApiGatewayAuth:
    """TC-14.1 — the dashboard REST API enforces Cognito User Pools auth."""

    def test_proxy_method_uses_cognito_auth(self, boto3_session):
        apigw = boto3_session.client("apigateway")
        rid = None
        for api in apigw.get_rest_apis(limit=200).get("items", []):
            if "dashboard-api" in api.get("name", ""):
                rid = api["id"]
                break
        assert rid, "Dashboard REST API not found"
        proxy = next(
            r["id"] for r in apigw.get_resources(restApiId=rid, limit=200)["items"] if r.get("path") == "/api/{proxy+}"
        )
        method = apigw.get_method(restApiId=rid, resourceId=proxy, httpMethod="ANY")
        assert method["authorizationType"] == "COGNITO_USER_POOLS"

    def test_anonymous_request_is_rejected(self, dashboard_base_url):
        """An unauthenticated request to a protected route must be denied.

        Through CloudFront (the only supported entry point) a request without
        an Authorization header must yield 401 (Cognito authorizer) — never a
        2xx or the data.
        """
        req = urllib.request.Request(f"{dashboard_base_url}/api/pipelines", method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                status = resp.status
        except urllib.error.HTTPError as e:
            status = e.code
        assert status in (401, 403), f"Expected 401/403 for anonymous request, got {status}"


class TestS3PublicAccess:
    """TC-14.3 — core buckets fully block public access."""

    @pytest.mark.parametrize("key", ["source_bucket", "processed_bucket"])
    def test_public_access_blocked(self, boto3_session, resource_names, key):
        s3 = boto3_session.client("s3")
        cfg = s3.get_public_access_block(Bucket=resource_names[key])["PublicAccessBlockConfiguration"]
        assert cfg["BlockPublicAcls"]
        assert cfg["IgnorePublicAcls"]
        assert cfg["BlockPublicPolicy"]
        assert cfg["RestrictPublicBuckets"]


class TestDynamoEncryption:
    """TC-14.4 — DynamoDB tables are encrypted at rest."""

    @pytest.mark.parametrize(
        "table",
        ["agentic_data_ingestion_pipelines", "agentic_data_ingestion_state", "agentic_data_ingestion_approvals"],
    )
    def test_table_encrypted(self, boto3_session, table):
        ddb = boto3_session.client("dynamodb")
        sse = ddb.describe_table(TableName=table)["Table"].get("SSEDescription", {})
        # DynamoDB is always encrypted; for these tables we require a KMS CMK
        # (SSEDescription is only present for KMS, not AWS-owned default keys).
        assert sse.get("Status") == "ENABLED", f"{table} SSE not ENABLED"
        assert sse.get("SSEType") == "KMS", f"{table} not using KMS encryption"


class TestBedrockAccessControl:
    """TC-14.7 — the agent Lambda Bedrock policy is scoped to specific
    actions, not a bedrock:* wildcard."""

    @staticmethod
    def _find_bedrock_policy_arn(iam_client) -> str | None:
        """Locate the deployment's bedrock managed policy regardless of the
        configured ``projectName`` (deployments may pin a legacy value — see
        the README upgrade note)."""
        paginator = iam_client.get_paginator("list_policies")
        for page in paginator.paginate(Scope="Local"):
            for pol in page.get("Policies", []):
                if pol["PolicyName"].endswith("-bedrock-policy"):
                    return pol["Arn"]
        return None

    def test_bedrock_policy_is_scoped(self, boto3_session, account_id):
        iam = boto3_session.client("iam")
        arn = self._find_bedrock_policy_arn(iam)
        assert arn, "No *-bedrock-policy managed policy found in this account"
        ver = iam.get_policy(PolicyArn=arn)["Policy"]["DefaultVersionId"]
        doc = iam.get_policy_version(PolicyArn=arn, VersionId=ver)["PolicyVersion"]["Document"]
        actions = []
        for stmt in doc["Statement"]:
            act = stmt.get("Action", [])
            actions.extend(act if isinstance(act, list) else [act])
        assert actions, "Bedrock policy has no actions"
        assert "bedrock:*" not in actions, "Bedrock policy must not grant bedrock:*"
        assert all(a.startswith("bedrock:") for a in actions)
        assert "bedrock:InvokeModel" in actions


class TestWebSocketAuth:
    """TC-14.8 — WebSocket $connect authorization.

    The $connect route carries a Lambda authorizer that validates Cognito JWTs
    from the ``?token=`` query string. $disconnect/$default stay NONE by design:
    API Gateway only invokes them on connections that passed $connect (see the
    AwsSolutions-APIG4 acknowledgment).

    NOTE: the previous version of this test xfailed with "no authorizer
    attached" — that was a test bug, not a product gap: it searched for the
    snake_case API name while the deployed API is kebab-case, so the API was
    never found and the lookup error masqueraded as the expected failure.
    """

    def test_connect_route_requires_auth(self, boto3_session):
        v2 = boto3_session.client("apigatewayv2")
        ws = next(
            (
                a
                for a in v2.get_apis()["Items"]
                if a["ProtocolType"] == "WEBSOCKET" and "agentic-data-ingestion" in a["Name"].lower()
            ),
            None,
        )
        assert ws, "WebSocket API not found in this account/region"
        connect = next(r for r in v2.get_routes(ApiId=ws["ApiId"])["Items"] if r["RouteKey"] == "$connect")
        assert connect.get("AuthorizationType", "NONE") != "NONE", (
            "$connect must carry the Cognito JWT Lambda authorizer"
        )
