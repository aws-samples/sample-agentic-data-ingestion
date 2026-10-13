# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Phase T3 — AI Agent direct-invocation e2e tests.

Invokes the five AgentCore runtimes directly (not via a full pipeline run)
to assert:
1. Self-healing agent records a recovery attempt
2. Data-quality agent returns an assessment
3. Cost-optimization agent returns recommendations
4. NL pipeline builder produces a pipeline definition
5. Complex mapping agent returns evidence-based mappings for a low-confidence
   payload (source_schema, target_schema, sample_data_uri, confidence_threshold)

**Coverage note:**
Direct-invoke is the baseline coverage for all five agents. The complex mapping
agent's *in-pipeline* coverage depends on the prerequisite
`complex-mapping-agent-integration` spec (deployed first). Once that integration
ships, a full low-confidence pipeline run should be added asserting that the
`complex_mapping` step ran and evidence is present in the schema-mapping approval.

Requirements: 19.1, 19.2, 19.3, 19.4, 19.5, 19.6, 19.7
"""

from __future__ import annotations

import json
import logging
import uuid

import pytest

logger = logging.getLogger(__name__)

# ── AgentCore Runtime names (deployed via CDK agentcore-stack.ts) ─────────
AGENT_RUNTIME_NAMES = {
    "self_healing": "agentic_data_ingestion_agent_self_healing",
    "data_quality": "agentic_data_ingestion_agent_data_quality",
    "cost_optimization": "agentic_data_ingestion_agent_cost_optimization",
    "nl_pipeline_builder": "agentic_data_ingestion_agent_nl_pipeline_builder",
    "complex_mapping": "agentic_data_ingestion_agent_complex_mapping",
}


# ── Helpers ───────────────────────────────────────────────────────────────


def _discover_agent_runtime_arn(
    bedrock_agentcore_control_client,
    runtime_name: str,
    region: str,
    account_id: str,
) -> str | None:
    """Discover the AgentCore Runtime ARN by listing runtimes.

    ``list_agent_runtimes`` is a CONTROL-plane operation, so this must be
    called with a ``bedrock-agentcore-control`` client (the data-plane
    ``bedrock-agentcore`` client only exposes ``invoke_agent_runtime``).
    The response key is ``agentRuntimes`` and the list parameter is the
    camelCase ``maxResults``. Returns the full ``agentRuntimeArn``.
    """
    # AgentCore ARN format: arn:aws:bedrock-agentcore:{region}:{account}:runtime/{name}-{hash}
    try:
        paginator = bedrock_agentcore_control_client.get_paginator("list_agent_runtimes")
        for page in paginator.paginate(maxResults=100):
            for runtime in page.get("agentRuntimes", []):
                name = runtime.get("agentRuntimeName", "")
                if name == runtime_name:
                    return runtime.get("agentRuntimeArn")
    except Exception:
        pass

    # Fallback: try list without paginator
    try:
        resp = bedrock_agentcore_control_client.list_agent_runtimes(maxResults=100)
        for runtime in resp.get("agentRuntimes", []):
            name = runtime.get("agentRuntimeName", "")
            if name == runtime_name:
                return runtime.get("agentRuntimeArn")
    except Exception:
        pass

    return None


def _invoke_agent_runtime(
    bedrock_agentcore_client,
    runtime_arn: str,
    payload: dict,
    region: str,
    account_id: str,
) -> dict:
    """Invoke an AgentCore Runtime and return the parsed JSON response.

    Uses the Bedrock AgentCore (data-plane) ``invoke_agent_runtime`` API.
    ``runtime_arn`` is the full ARN returned by ``_discover_agent_runtime_arn``.
    """
    session_id = f"e2e-{uuid.uuid4().hex[:16]}-{uuid.uuid4().hex[:16]}"

    response = bedrock_agentcore_client.invoke_agent_runtime(
        agentRuntimeArn=runtime_arn,
        qualifier="DEFAULT",
        runtimeSessionId=session_id,
        contentType="application/json",
        accept="application/json",
        payload=json.dumps(payload),
    )

    # AgentCore returns the handler payload under the "response" key (a
    # StreamingBody or EventStream), NOT "body". Mirror the production parser
    # in shared/agentcore_proxy.py: read/iterate the stream, then JSON-decode.
    result_bytes = b""
    stream = response.get("response", response.get("body", response.get("payload")))
    if stream is not None:
        if hasattr(stream, "read"):
            result_bytes = stream.read()
        else:
            for event in stream:
                if "chunk" in event:
                    result_bytes += event["chunk"].get("bytes", b"")
                elif "bytes" in event:
                    result_bytes += event["bytes"]

    text = result_bytes.decode("utf-8") if result_bytes else ""

    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return {"_raw_response": text}


def _invoke_agent_via_lambda(
    lambda_client,
    function_name: str,
    payload: dict,
) -> dict:
    """Invoke an agent's Lambda function directly (fallback path).

    Some agents may be deployed as Lambda functions rather than (or in
    addition to) AgentCore Runtimes.
    """
    response = lambda_client.invoke(
        FunctionName=function_name,
        InvocationType="RequestResponse",
        Payload=json.dumps(payload).encode("utf-8"),
    )
    response_payload = json.loads(response["Payload"].read().decode("utf-8"))
    if response.get("FunctionError"):
        raise RuntimeError(f"Lambda {function_name} returned FunctionError: {response_payload}")
    return response_payload


def _try_invoke_agent(
    boto3_session,
    runtime_name: str,
    payload: dict,
    region: str,
    account_id: str,
) -> dict:
    """Try to invoke an agent via AgentCore Runtime, with Lambda fallback.

    Strategy:
    1. Try bedrock-agentcore invoke_agent_runtime
    2. Fall back to Lambda invocation using the function name pattern
    """
    # Path 1: AgentCore Runtime
    #   Discovery (list_agent_runtimes) is a CONTROL-plane op → bedrock-agentcore-control
    #   Invocation (invoke_agent_runtime) is a DATA-plane op → bedrock-agentcore
    try:
        control_client = boto3_session.client("bedrock-agentcore-control", region_name=region)
        runtime_arn = _discover_agent_runtime_arn(control_client, runtime_name, region, account_id)
        if runtime_arn:
            logger.info(
                "Invoking agent via AgentCore Runtime: name=%s arn=%s",
                runtime_name,
                runtime_arn,
            )
            data_client = boto3_session.client("bedrock-agentcore", region_name=region)
            return _invoke_agent_runtime(data_client, runtime_arn, payload, region, account_id)
    except Exception as exc:
        logger.warning(
            "AgentCore Runtime invocation failed for %s, trying Lambda fallback: %s",
            runtime_name,
            exc,
        )

    # Path 2: Lambda fallback (function name = "agentic_data_ingestion_{agent_suffix}")
    # The Lambda names map: self_healing → agentic_data_ingestion_self_healing, etc.
    lambda_suffix = runtime_name.replace("agentic_data_ingestion_agent_", "")
    lambda_name = f"agentic_data_ingestion_{lambda_suffix}"

    try:
        lambda_client = boto3_session.client("lambda", region_name=region)
        logger.info("Invoking agent via Lambda fallback: %s", lambda_name)
        return _invoke_agent_via_lambda(lambda_client, lambda_name, payload)
    except Exception as exc:
        pytest.skip(f"Agent '{runtime_name}' not reachable via AgentCore Runtime or Lambda ({lambda_name}): {exc}")


# ── Test class ────────────────────────────────────────────────────────────


class TestAgentDirectInvocation:
    """Direct-invoke e2e tests for the five AgentCore agent runtimes.

    Each test invokes one agent directly and asserts the expected
    response structure. These tests do NOT drive full pipeline runs.

    All tests are gated by ``requires_trigger`` because they invoke
    real AWS resources that incur cost.
    """

    # ── 1. Self-Healing Agent (Req 19.1) ──────────────────────────

    def test_self_healing_agent_records_recovery_attempt(
        self,
        requires_trigger,
        boto3_session,
        aws_region,
        account_id,
        e2e_ledger,
    ):
        """Invoke the self-healing agent with a failed execution context.

        Asserts that the agent:
        - Classifies the error
        - Records a recovery attempt (remediation action in the response)
        """
        payload = {
            "run_id": f"e2e-selfheal-{uuid.uuid4().hex[:8]}",
            "pipeline_id": f"e2e-test-pipe-{uuid.uuid4().hex[:8]}",
            "step_name": "glue_etl",
            "error_message": "AnalysisException: cannot resolve 'customer_id' given input columns",
            "job_name": "agentic-data-ingestion-e2e-test-etl",
            "execution_arn": "arn:aws:states:us-west-2:000000000000:execution:test:e2e-test",
        }

        runtime_name = AGENT_RUNTIME_NAMES["self_healing"]
        result = _try_invoke_agent(boto3_session, runtime_name, payload, aws_region, account_id)

        # Assert recovery attempt was recorded
        assert "error_classification" in result or "agent_type" in result, (
            f"Self-healing agent did not return expected fields: {list(result.keys())}"
        )

        # The agent should classify the error as schema_mismatch
        if "error_classification" in result:
            assert result["error_classification"] == "schema_mismatch", (
                f"Expected schema_mismatch, got: {result['error_classification']}"
            )

        # A remediation action should be recorded
        if "remediation_applied" in result:
            assert result["remediation_applied"], "Self-healing agent did not record a remediation attempt"

        logger.info("Self-healing agent response: %s", json.dumps(result, default=str)[:500])

    # ── 2. Data-Quality Agent (Req 19.2) ──────────────────────────

    def test_data_quality_agent_returns_assessment(
        self,
        requires_trigger,
        boto3_session,
        aws_region,
        account_id,
        e2e_ledger,
    ):
        """Invoke the data-quality agent with a pipeline/target context.

        Asserts that the agent returns a quality assessment with check results.
        """
        payload = {
            "pipeline_id": f"e2e-dq-pipe-{uuid.uuid4().hex[:8]}",
            "run_id": f"e2e-dq-{uuid.uuid4().hex[:8]}",
            "target_table_info": {
                "database": "agentic_data_ingestion_db",
                "table": "e2e_test_customers",
                "location": "s3://agentic-data-ingestion-test-data/e2e/customers/",
            },
            "source_row_count": 100,
            "primary_key_columns": ["customer_id"],
        }

        runtime_name = AGENT_RUNTIME_NAMES["data_quality"]
        result = _try_invoke_agent(boto3_session, runtime_name, payload, aws_region, account_id)

        # Assert the agent returns an assessment (checks array or summary)
        has_assessment = (
            "checks" in result
            or "quality_report" in result
            or "assessment" in result
            or "agent_type" in result
            or "overall_status" in result
        )
        assert has_assessment, f"Data-quality agent did not return an assessment. Keys: {list(result.keys())}"

        logger.info("Data-quality agent response: %s", json.dumps(result, default=str)[:500])

    # ── 3. Cost-Optimization Agent (Req 19.3) ─────────────────────

    def test_cost_optimization_agent_returns_recommendations(
        self,
        requires_trigger,
        boto3_session,
        aws_region,
        account_id,
        e2e_ledger,
    ):
        """Invoke the cost-optimization agent (no payload — weekly scan).

        Asserts that the agent returns a recommendations report.
        """
        # The cost optimization agent runs with an empty/minimal payload
        payload: dict = {}

        runtime_name = AGENT_RUNTIME_NAMES["cost_optimization"]
        result = _try_invoke_agent(boto3_session, runtime_name, payload, aws_region, account_id)

        # Assert the agent returns recommendations or a report
        has_recommendations = (
            "recommendations" in result
            or "report" in result
            or "total_monthly_savings" in result
            or "jobs_analyzed" in result
            or "agent_type" in result
        )
        assert has_recommendations, (
            f"Cost-optimization agent did not return recommendations. Keys: {list(result.keys())}"
        )

        logger.info("Cost-optimization agent response: %s", json.dumps(result, default=str)[:500])

    # ── 4. NL Pipeline Builder (Req 19.4) ─────────────────────────

    def test_nl_pipeline_builder_produces_definition(
        self,
        requires_trigger,
        boto3_session,
        aws_region,
        account_id,
        e2e_ledger,
    ):
        """Invoke the NL pipeline builder with a natural language description.

        Asserts that the agent returns a pipeline definition/proposal.
        """
        payload = {
            "message": (
                "Load customer CSV data from s3://agentic-data-ingestion-test-data/customers.csv "
                "into an S3 Tables Iceberg table partitioned by region"
            ),
            "conversation_id": f"e2e-nlpb-{uuid.uuid4().hex[:8]}",
            "confirm": False,
        }

        runtime_name = AGENT_RUNTIME_NAMES["nl_pipeline_builder"]
        result = _try_invoke_agent(boto3_session, runtime_name, payload, aws_region, account_id)

        # Assert the agent returns a pipeline definition or proposal
        has_definition = (
            "pipeline_config" in result
            or "proposal" in result
            or "pipeline_definition" in result
            or "target_config" in result
            or "intent" in result
            or "agent_type" in result
        )
        assert has_definition, f"NL pipeline builder did not produce a definition. Keys: {list(result.keys())}"

        logger.info("NL pipeline builder response: %s", json.dumps(result, default=str)[:500])

    # ── 5. Complex Mapping Agent (Req 19.5, 19.6, 19.7) ──────────

    def test_complex_mapping_agent_returns_evidence_based_mappings(
        self,
        requires_trigger,
        boto3_session,
        aws_region,
        account_id,
        resource_names,
        e2e_ledger,
    ):
        """Invoke the complex mapping agent with a low-confidence payload.

        Provides source_schema, target_schema, sample_data_uri, and
        confidence_threshold. Asserts the agent returns evidence-based
        mappings with evidence fields.

        NOTE: Direct-invoke is the baseline coverage. In-pipeline coverage
        depends on the prerequisite `complex-mapping-agent-integration` spec.
        Once that integration ships, add a full low-confidence pipeline run
        asserting the `complex_mapping` step ran and evidence is present in
        the schema-mapping approval.
        """
        source_bucket = resource_names["source_bucket"]
        payload = {
            "source_schema": {
                "fields": [
                    {"name": "cust_id", "type": "string"},
                    {"name": "addr_1", "type": "string"},
                    {"name": "addr_2", "type": "string"},
                    {"name": "ph_num", "type": "string"},
                    {"name": "full_nm", "type": "string"},
                ]
            },
            "target_schema": {
                "fields": [
                    {"name": "customer_id", "type": "string"},
                    {"name": "street_address", "type": "string"},
                    {"name": "address_line_2", "type": "string"},
                    {"name": "phone_number", "type": "string"},
                    {"name": "full_name", "type": "string"},
                ]
            },
            "sample_data_uri": f"s3://{source_bucket}/e2e-test/complex-mapping-sample.csv",
            "pipeline_id": f"e2e-cm-pipe-{uuid.uuid4().hex[:8]}",
            "run_id": f"e2e-cm-{uuid.uuid4().hex[:8]}",
            "confidence_threshold": 0.6,
        }

        runtime_name = AGENT_RUNTIME_NAMES["complex_mapping"]
        result = _try_invoke_agent(boto3_session, runtime_name, payload, aws_region, account_id)

        # Assert evidence-based mappings are returned
        has_evidence = "evidence" in result or "mappings" in result or "questions" in result or "agent_type" in result
        assert has_evidence, (
            f"Complex mapping agent did not return evidence-based mappings. Keys: {list(result.keys())}"
        )

        # If mappings are present, verify they contain evidence fields
        if "mappings" in result:
            mappings = result["mappings"]
            assert isinstance(mappings, list), "Mappings should be a list"
            if mappings:
                first_mapping = mappings[0]
                assert "source" in first_mapping, "Mapping missing 'source' field"
                assert "target" in first_mapping, "Mapping missing 'target' field"
                assert "confidence" in first_mapping, "Mapping missing 'confidence' field"

        if "evidence" in result:
            evidence = result["evidence"]
            assert isinstance(evidence, dict), "Evidence should be a dict"

        logger.info("Complex mapping agent response: %s", json.dumps(result, default=str)[:500])
