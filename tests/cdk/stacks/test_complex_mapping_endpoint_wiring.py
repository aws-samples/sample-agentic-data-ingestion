# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK structural tests for complex mapping agent endpoint wiring.

Validates:
- AgentCore stack defines the ComplexMappingAgentEndpoint CfnOutput
- Compute stack's SchemaMapping Lambda has COMPLEX_MAPPING_AGENT_ENDPOINT env var
- bin/agentic_data_ingestion.ts passes the endpoint import to Compute stack props
"""

import os

import pytest

AGENTCORE_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "agentcore-stack.ts"
)

COMPUTE_STACK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "compute-stack.ts")

AGENTIC_DATA_INGESTION_TS_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "bin", "agentic_data_ingestion.ts"
)


@pytest.fixture
def agentcore_source():
    """Load the agentcore-stack.ts source code."""
    with open(AGENTCORE_STACK_PATH) as f:
        return f.read()


@pytest.fixture
def compute_source():
    """Load the compute-stack.ts source code."""
    with open(COMPUTE_STACK_PATH) as f:
        return f.read()


@pytest.fixture
def app_source():
    """Load the agentic_data_ingestion.ts app entry point source code."""
    with open(AGENTIC_DATA_INGESTION_TS_PATH) as f:
        return f.read()


class TestAgentCoreComplexMappingOutput:
    """Verify AgentCore stack exports the ComplexMappingAgentEndpoint."""

    def test_complex_mapping_agent_endpoint_property(self, agentcore_source):
        """complexMappingAgentEndpoint should be a public property."""
        assert "complexMappingAgentEndpoint" in agentcore_source

    def test_complex_mapping_cfn_output_exists(self, agentcore_source):
        """CfnOutput for ComplexMappingAgentEndpoint should exist."""
        assert "ComplexMappingAgentEndpoint" in agentcore_source

    def test_complex_mapping_export_name(self, agentcore_source):
        """Export name should follow the ${stackName}-ComplexMappingAgentEndpoint pattern."""
        assert "ComplexMappingAgentEndpoint" in agentcore_source
        # Verify it uses the stackName-based export pattern
        assert "${this.stackName}-ComplexMappingAgentEndpoint" in agentcore_source


class TestComputeStackComplexMappingEnvVar:
    """Verify SchemaMapping Lambda has the COMPLEX_MAPPING_AGENT_ENDPOINT env var."""

    def test_complex_mapping_endpoint_in_schema_mapping_env(self, compute_source):
        """COMPLEX_MAPPING_AGENT_ENDPOINT should be set on SchemaMapping Lambda."""
        assert "COMPLEX_MAPPING_AGENT_ENDPOINT" in compute_source

    def test_complex_mapping_endpoint_prop_in_interface(self, compute_source):
        """agentCoreComplexMappingEndpoint should be in ComputeStackProps."""
        assert "agentCoreComplexMappingEndpoint" in compute_source

    def test_complex_mapping_endpoint_defaults_to_empty(self, compute_source):
        """Endpoint should default to empty string when not provided."""
        assert "props.agentCoreComplexMappingEndpoint ?? ''" in compute_source


class TestBinImportsComplexMappingEndpoint:
    """Verify bin/agentic_data_ingestion.ts imports the complex mapping endpoint."""

    def test_complex_mapping_import_value(self, app_source):
        """agentCoreComplexMappingEndpoint should be passed to Compute stack."""
        assert "agentCoreComplexMappingEndpoint" in app_source

    def test_complex_mapping_uses_fn_import_value(self, app_source):
        """Should use cdk.Fn.importValue with ComplexMappingAgentEndpoint."""
        assert "ComplexMappingAgentEndpoint" in app_source
