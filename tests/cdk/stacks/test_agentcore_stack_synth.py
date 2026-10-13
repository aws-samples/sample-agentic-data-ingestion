# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK structural test for AgentCore stack with 3 focused server runtimes (Task 2.14).

Validates that the agentcore-stack.ts defines:
- 3 focused AgentCore runtimes (codegen, infra, analytics)
- Each with distinct runtime names and Docker image sources
- All 3 endpoints are exported as public properties
- CfnOutputs exist for all 3 focused server endpoints

Note: This is a structural/static analysis test. Full `cdk synth` validation
requires the CDK CLI and Node.js environment — use `npx cdk synth` manually.
"""

import os

import pytest

AGENTCORE_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "agentcore-stack.ts"
)

AGENTIC_DATA_INGESTION_TS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "bin", "agentic_data_ingestion.ts"
)


@pytest.fixture
def stack_source():
    """Load the agentcore-stack.ts source code."""
    with open(AGENTCORE_STACK_PATH) as f:
        return f.read()


@pytest.fixture
def app_source():
    """Load the agentic_data_ingestion.ts app entry point source code."""
    with open(AGENTIC_DATA_INGESTION_TS_PATH) as f:
        return f.read()


class TestAgentCoreStackFocusedRuntimes:
    """Verify the AgentCore stack defines 3 focused server runtimes."""

    def test_codegen_runtime_defined(self, stack_source):
        """CodegenMCPRuntime CfnResource should exist."""
        assert "'CodegenMCPRuntime'" in stack_source or '"CodegenMCPRuntime"' in stack_source

    def test_infra_runtime_defined(self, stack_source):
        """InfraMCPRuntime CfnResource should exist."""
        assert "'InfraMCPRuntime'" in stack_source or '"InfraMCPRuntime"' in stack_source

    def test_analytics_runtime_defined(self, stack_source):
        """AnalyticsMCPRuntime CfnResource should exist."""
        assert "'AnalyticsMCPRuntime'" in stack_source or '"AnalyticsMCPRuntime"' in stack_source

    def test_codegen_runtime_name(self, stack_source):
        """Codegen runtime should use 'agentic_data_ingestion_mcp_codegen' name."""
        assert "agentic_data_ingestion_mcp_codegen" in stack_source

    def test_infra_runtime_name(self, stack_source):
        """Infra runtime should use 'agentic_data_ingestion_mcp_infra' name."""
        assert "agentic_data_ingestion_mcp_infra" in stack_source

    def test_analytics_runtime_name(self, stack_source):
        """Analytics runtime should use 'agentic_data_ingestion_mcp_analytics' name."""
        assert "agentic_data_ingestion_mcp_analytics" in stack_source

    def test_three_distinct_docker_images(self, stack_source):
        """Three distinct DockerImageAsset resources for focused servers."""
        assert "'CodegenImage'" in stack_source or '"CodegenImage"' in stack_source
        assert "'InfraImage'" in stack_source or '"InfraImage"' in stack_source
        assert "'AnalyticsImage'" in stack_source or '"AnalyticsImage"' in stack_source

    def test_docker_directories_point_to_correct_servers(self, stack_source):
        """Docker image directories should point to the 3 focused server dirs."""
        assert "codegen_server" in stack_source
        assert "infra_server" in stack_source
        assert "analytics_server" in stack_source


class TestAgentCoreStackEndpointExports:
    """Verify all 3 focused endpoints are exported."""

    def test_codegen_invocation_url_property(self, stack_source):
        """codegenInvocationUrl should be a public property."""
        assert "codegenInvocationUrl" in stack_source

    def test_infra_invocation_url_property(self, stack_source):
        """infraInvocationUrl should be a public property."""
        assert "infraInvocationUrl" in stack_source

    def test_analytics_invocation_url_property(self, stack_source):
        """analyticsInvocationUrl should be a public property."""
        assert "analyticsInvocationUrl" in stack_source

    def test_codegen_cfn_output(self, stack_source):
        """CfnOutput for CodegenInvocationURL should exist."""
        assert "CodegenInvocationURL" in stack_source

    def test_infra_cfn_output(self, stack_source):
        """CfnOutput for InfraInvocationURL should exist."""
        assert "InfraInvocationURL" in stack_source

    def test_analytics_cfn_output(self, stack_source):
        """CfnOutput for AnalyticsInvocationURL should exist."""
        assert "AnalyticsInvocationURL" in stack_source


class TestAgentCoreStackLegacyPreserved:
    """Verify legacy monolithic runtime is still present for backward compat."""

    def test_data_processing_runtime_still_exists(self, stack_source):
        """Legacy DataProcessingMCPRuntime should still be defined."""
        assert "DataProcessingMCPRuntime" in stack_source

    def test_s3_tables_runtime_still_exists(self, stack_source):
        """S3TablesMCPRuntime should still be defined."""
        assert "S3TablesMCPRuntime" in stack_source


class TestComputeStackEndpointWiring:
    """Verify the app entry point passes focused endpoints to ComputeStack."""

    def test_codegen_endpoint_passed(self, app_source):
        """agentCoreCodegenEndpoint should be passed to ComputeStack."""
        assert "agentCoreCodegenEndpoint" in app_source

    def test_infra_endpoint_passed(self, app_source):
        """agentCoreInfraEndpoint should be passed to ComputeStack."""
        assert "agentCoreInfraEndpoint" in app_source

    def test_analytics_endpoint_passed(self, app_source):
        """agentCoreAnalyticsEndpoint should be passed to ComputeStack."""
        assert "agentCoreAnalyticsEndpoint" in app_source


class TestAgentCoreStackAgentRuntimes:
    """Verify the AgentCore stack defines 4 agent Runtime containers (Optimization 3)."""

    def test_create_code_agent_runtime_defined(self, stack_source):
        """CreateCodeAgentRuntime CfnResource should exist."""
        assert "'CreateCodeAgentRuntime'" in stack_source or '"CreateCodeAgentRuntime"' in stack_source

    def test_test_code_agent_runtime_defined(self, stack_source):
        """TestCodeAgentRuntime CfnResource should exist."""
        assert "'TestCodeAgentRuntime'" in stack_source or '"TestCodeAgentRuntime"' in stack_source

    def test_create_glue_pipeline_agent_runtime_defined(self, stack_source):
        """CreateGluePipelineAgentRuntime CfnResource should exist."""
        assert "'CreateGluePipelineAgentRuntime'" in stack_source or '"CreateGluePipelineAgentRuntime"' in stack_source

    def test_create_infra_agent_runtime_defined(self, stack_source):
        """CreateInfraAgentRuntime CfnResource should exist."""
        assert "'CreateInfraAgentRuntime'" in stack_source or '"CreateInfraAgentRuntime"' in stack_source

    def test_agent_runtimes_use_protocol_http(self, stack_source):
        """Agent runtimes use ProtocolConfiguration: 'HTTP' (agent containers,
        not MCP servers — MCP servers use 'MCP')."""
        assert "ProtocolConfiguration: 'HTTP'" in stack_source

    def test_four_agent_docker_images(self, stack_source):
        """Four distinct DockerImageAsset resources for agent containers."""
        assert "CreateCodeAgentImage" in stack_source
        assert "TestCodeAgentImage" in stack_source
        assert "CreateGluePipelineAgentImage" in stack_source
        assert "CreateInfraAgentImage" in stack_source

    def test_agent_docker_directories(self, stack_source):
        """Docker image assets should point to agentcore/ Dockerfiles."""
        assert "'agentcore/create_code/Dockerfile'" in stack_source
        assert "'agentcore/test_code/Dockerfile'" in stack_source
        assert "'agentcore/create_glue_pipeline/Dockerfile'" in stack_source
        assert "'agentcore/create_infrastructure/Dockerfile'" in stack_source


class TestAgentCoreStackAgentEndpoints:
    """Verify agent Runtime endpoints are exported."""

    def test_create_code_agent_endpoint_property(self, stack_source):
        """createCodeAgentEndpoint should be a public property."""
        assert "createCodeAgentEndpoint" in stack_source

    def test_test_code_agent_endpoint_property(self, stack_source):
        """testCodeAgentEndpoint should be a public property."""
        assert "testCodeAgentEndpoint" in stack_source

    def test_create_glue_pipeline_agent_endpoint_property(self, stack_source):
        """createGluePipelineAgentEndpoint should be a public property."""
        assert "createGluePipelineAgentEndpoint" in stack_source

    def test_create_infra_agent_endpoint_property(self, stack_source):
        """createInfraAgentEndpoint should be a public property."""
        assert "createInfraAgentEndpoint" in stack_source

    def test_create_code_agent_cfn_output(self, stack_source):
        """CfnOutput for CreateCodeAgentEndpoint should exist."""
        assert "CreateCodeAgentEndpoint" in stack_source

    def test_test_code_agent_cfn_output(self, stack_source):
        """CfnOutput for TestCodeAgentEndpoint should exist."""
        assert "TestCodeAgentEndpoint" in stack_source

    def test_create_glue_pipeline_agent_cfn_output(self, stack_source):
        """CfnOutput for CreateGluePipelineAgentEndpoint should exist."""
        assert "CreateGluePipelineAgentEndpoint" in stack_source

    def test_create_infra_agent_cfn_output(self, stack_source):
        """CfnOutput for CreateInfraAgentEndpoint should exist."""
        assert "CreateInfraAgentEndpoint" in stack_source


class TestComputeStackAgentRuntimeEnvVars:
    """Verify compute-stack.ts has USE_AGENTCORE_RUNTIME and endpoint env vars."""

    @pytest.fixture
    def compute_source(self):
        """Load the compute-stack.ts source code."""
        compute_path = os.path.join(
            os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "compute-stack.ts"
        )
        with open(compute_path) as f:
            return f.read()

    def test_use_agentcore_runtime_env_var(self, compute_source):
        """USE_AGENTCORE_RUNTIME should be set on agent Lambdas."""
        assert "USE_AGENTCORE_RUNTIME" in compute_source

    def test_create_code_endpoint_env_var(self, compute_source):
        """AGENTCORE_CREATE_CODE_ENDPOINT should be set on CodeGeneration Lambda."""
        assert "AGENTCORE_CREATE_CODE_ENDPOINT" in compute_source

    def test_test_code_endpoint_env_var(self, compute_source):
        """AGENTCORE_TEST_CODE_ENDPOINT should be set on CodeTesting Lambda."""
        assert "AGENTCORE_TEST_CODE_ENDPOINT" in compute_source

    def test_create_pipeline_endpoint_env_var(self, compute_source):
        """AGENTCORE_CREATE_PIPELINE_ENDPOINT should be set on CreateGluePipeline Lambda."""
        assert "AGENTCORE_CREATE_PIPELINE_ENDPOINT" in compute_source

    def test_create_infra_endpoint_env_var(self, compute_source):
        """AGENTCORE_CREATE_INFRA_ENDPOINT should be set on CreateInfrastructure Lambda."""
        assert "AGENTCORE_CREATE_INFRA_ENDPOINT" in compute_source

    def test_default_on(self, compute_source):
        """USE_AGENTCORE_RUNTIME defaults to 'true' — AgentCore Runtime is now
        the default execution path for the agent Lambdas."""
        assert "USE_AGENTCORE_RUNTIME: 'true'" in compute_source

    def test_compute_stack_props_include_agent_endpoints(self, compute_source):
        """ComputeStackProps should include agent Runtime endpoint props."""
        assert "agentCoreCreateCodeEndpoint" in compute_source
        assert "agentCoreTestCodeEndpoint" in compute_source
        assert "agentCoreCreatePipelineEndpoint" in compute_source
        assert "agentCoreCreateInfraEndpoint" in compute_source
