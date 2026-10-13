# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
CDK assertion tests for the Model Call Budget table wiring (P28 R15.2, Task 12.2).

Validates:
- data-stack.ts defines the budget table: run_id partition key (STRING),
  expires_at TTL attribute, PAY_PER_REQUEST billing, exported property
- compute-stack.ts wires MODEL_CALL_BUDGET_TABLE into the schema mapping
  Lambda environment and grants it dynamodb:UpdateItem on the table
- agentcore-stack.ts sets MODEL_CALL_BUDGET_TABLE in the shared agent
  container environment (containers vendor shared/model_config.py)

Validates: Requirements 15.2, 15.3
"""

import os
import re

import pytest

_LIB_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib")
DATA_STACK_PATH = os.path.join(_LIB_DIR, "data-stack.ts")
COMPUTE_STACK_PATH = os.path.join(_LIB_DIR, "compute-stack.ts")
AGENTCORE_STACK_PATH = os.path.join(_LIB_DIR, "agentcore-stack.ts")


@pytest.fixture(scope="module")
def data_stack_content():
    with open(DATA_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def compute_stack_content():
    with open(COMPUTE_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def agentcore_stack_content():
    with open(AGENTCORE_STACK_PATH) as f:
        return f.read()


def _extract_section(content: str, header_keyword: str) -> str:
    """Extract a section between `// ── <header_keyword>` and the next `// ──` marker."""
    pattern = rf"(// ──[^\n]*{re.escape(header_keyword)}[^\n]*\n)(.*?)(?=// ──|\Z)"
    match = re.search(pattern, content, re.DOTALL)
    if not match:
        return ""
    return match.group(1) + match.group(2)


# ── Data stack: budget table definition ─────────────────────────────


class TestModelCallBudgetTableDefinition:
    """Budget table has correct PK, TTL, and billing mode."""

    def test_section_exists(self, data_stack_content):
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert section, "Model Call Budget Table section not found in data-stack.ts"

    def test_table_name(self, data_stack_content):
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert "tableName: 'agentic_data_ingestion_model_call_budget'" in section

    def test_partition_key_run_id_string(self, data_stack_content):
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert re.search(r"partitionKey:\s*\{[^}]*name:\s*'run_id'[^}]*AttributeType\.STRING", section), (
            "partition key must be run_id (STRING)"
        )

    def test_no_sort_key(self, data_stack_content):
        """CallBudget keys items by run_id alone — a sort key would break get/update."""
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert "sortKey" not in section

    def test_ttl_attribute_expires_at(self, data_stack_content):
        """TTL attribute name must match what shared/model_config.py writes."""
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert "timeToLiveAttribute: 'expires_at'" in section

    def test_billing_mode_pay_per_request(self, data_stack_content):
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert "BillingMode.PAY_PER_REQUEST" in section

    def test_destroy_removal_policy(self, data_stack_content):
        """Ephemeral TTL counters follow the task-tokens/ws/errors DESTROY pattern."""
        section = _extract_section(data_stack_content, "Model Call Budget Table")
        assert "RemovalPolicy.DESTROY" in section

    def test_public_readonly_property(self, data_stack_content):
        assert re.search(
            r"public\s+readonly\s+modelCallBudgetTable:\s*dynamodb\.Table",
            data_stack_content,
        )


# ── Compute stack: schema mapping Lambda wiring ─────────────────────


class TestSchemaMappingLambdaBudgetWiring:
    """Schema mapping Lambda gets the env var and UpdateItem grant."""

    def _schema_mapping_env_block(self, content: str) -> str:
        match = re.search(
            r"schemaMappingLambda = new lambda\.Function\(this, 'SchemaMapping',.*?environment:\s*\{(.*?)\n\s*\},",
            content,
            re.DOTALL,
        )
        assert match, "SchemaMapping Lambda definition not found in compute-stack.ts"
        return match.group(1)

    def test_env_var_from_table_name(self, compute_stack_content):
        env_block = self._schema_mapping_env_block(compute_stack_content)
        assert re.search(
            r"MODEL_CALL_BUDGET_TABLE:\s*data\.modelCallBudgetTable\.tableName",
            env_block,
        ), "schema mapping Lambda env must wire MODEL_CALL_BUDGET_TABLE from the data-stack table"

    def test_update_item_grant(self, compute_stack_content):
        assert re.search(
            r"data\.modelCallBudgetTable\.grant\(\s*this\.schemaMappingLambda,\s*'dynamodb:UpdateItem'\s*\)",
            compute_stack_content,
        ), "schema mapping Lambda must be granted dynamodb:UpdateItem on the budget table"


# ── AgentCore stack: shared agent container environment ─────────────


class TestAgentContainerBudgetEnv:
    """Agent containers (which vendor shared/model_config.py) see the table name."""

    def test_agent_container_env_has_budget_table(self, agentcore_stack_content):
        match = re.search(
            r"const agentContainerEnv = \(serviceName: string\) => \(\{(.*?)\n\s*\}\);",
            agentcore_stack_content,
            re.DOTALL,
        )
        assert match, "agentContainerEnv helper not found in agentcore-stack.ts"
        env_block = match.group(1)
        assert "MODEL_CALL_BUDGET_TABLE: 'agentic_data_ingestion_model_call_budget'" in env_block, (
            "agent container env must carry MODEL_CALL_BUDGET_TABLE"
        )
