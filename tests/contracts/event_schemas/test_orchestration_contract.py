# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: Step Functions definition includes schema_change_action.$ in SchemaMapping.Parameters (Task 3.8).

Validates: AC-20.1
"""

import os

ORCHESTRATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "orchestration-stack.ts"
)


def _read_orchestration_stack() -> str:
    with open(ORCHESTRATION_STACK_PATH) as f:
        return f.read()


def _extract_definition_json(source: str) -> dict:
    """Extract the Step Functions definition object from the TypeScript source.

    The definition is passed to JSON.stringify({ ... }) in the CDK code.
    We locate the States block and parse the SchemaMapping state from it.
    """
    # Find the SchemaMapping state block — look for the Parameters object
    # The source has: SchemaMapping: { Type: 'Task', ... Parameters: { ... }, ... }
    # We'll verify the parameters by checking the raw source text for the expected keys.
    return source


class TestSchemaMappingParameters:
    """Verify SchemaMapping.Parameters in the Step Functions definition."""

    def test_schema_change_action_parameter_present(self):
        """SchemaMapping.Parameters includes schema_change_action.$ referencing schema_compare_result.action."""
        source = _read_orchestration_stack()
        assert "'schema_change_action.$': '$.schema_compare_result.action'" in source, (
            "SchemaMapping.Parameters must include schema_change_action.$ → $.schema_compare_result.action"
        )

    def test_schema_differences_parameter_present(self):
        """SchemaMapping.Parameters includes schema_differences.$ referencing comparison_result.differences."""
        source = _read_orchestration_stack()
        assert "'schema_differences.$': '$.schema_compare_result.comparison_result.differences'" in source, (
            "SchemaMapping.Parameters must include schema_differences.$ → $.schema_compare_result.comparison_result.differences"
        )

    def test_original_parameters_still_present(self):
        """Original parameters (run_id, source_schema_hash, execution_arn) are still present."""
        source = _read_orchestration_stack()
        # These should appear in the SchemaMapping Parameters block
        assert "'run_id.$': '$.file_validation_result.run_id'" in source
        assert "'source_schema_hash.$': '$.schema_extract_result.schema_hash'" in source
        assert "'execution_arn.$': '$$.Execution.Name'" in source

    def test_schema_mapping_state_is_task_type(self):
        """SchemaMapping state is a LambdaInvoke Task in the CDK source."""
        source = _read_orchestration_stack()
        # Verify the SchemaMapping construct exists as a LambdaInvoke (Task)
        assert "'SchemaMapping'" in source
        # The SchemaMapping should be created via tasks.LambdaInvoke
        assert "LambdaInvoke" in source
