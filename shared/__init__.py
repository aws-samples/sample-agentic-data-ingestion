# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
Shared modules for AgenticDataIngestion Lambda functions.

In dev/test mode (AGENTIC_DATA_INGESTION_ENV=dev), validates that no module imports
from a higher dependency layer on first import.
"""

import logging
import os

logger = logging.getLogger(__name__)

# Layer assignments: module_name → layer_number
_LAYER_MAP = {
    "config": 0,  # single config source for thresholds/TTLs (P28 R18.2) — no shared imports
    "cost_recommendation_ids": 0,  # p30a R1.4 recommendation-id derivation — no shared imports
    "decimal_utils": 0,
    "env_utils": 0,
    # p55 R4.3/R4.4/R4.6 Source_Delimiter table keyed by extension — pure, no
    # shared imports. Sibling of the file_validation FORMAT_MAP it must never be
    # nested inside (p55 do-NOT 15).
    "format_delimiters": 0,
    # p35 R5.7 Glue-mirror (database, table) derivation — pure, no shared imports
    "glue_mirror_identifiers": 0,
    # p35 R7.3/R7.6 the keys glue:UpdateTable accepts inside TableInput, shared
    # by the two writers that read-modify-write the same mirror — pure, no
    # shared imports
    "glue_table_input": 0,
    # p37 R1.2-R1.6 Load_Outcome derivation — pure, no shared imports. Sibling of
    # quality_verdict; the two answer different questions and stay separate
    # (LGR-eee8ac8-6).
    "load_outcome": 0,
    "processed_file_ledger": 0,  # p30 R9.1 Processed_File_Ledger — no shared imports
    "quality_verdict": 0,  # p30 R7.2 Quality_Verdict derivation — pure, no shared imports
    "identifier_utils": 0,
    # The shared "approved for reuse" predicate and its revocation (LGR-5d68eeb-2)
    # — boto3/botocore only, no shared imports.
    "mapping_reuse": 0,
    # Which write modes each target implements, read by create, edit and code
    # generation — pure, standard library only.
    "write_modes": 0,
    "model_config": 1,  # imports step_errors (layer 0)
    # p47 R12.4 pipelines-table record read — imports step_errors (layer 0).
    # Sibling of target_schema_resolution: same layer, same reason.
    "pipeline_record_read": 1,
    "rerun_contract": 1,  # imports step_names (layer 0)
    "step_errors": 0,
    "step_names": 0,
    "target_schema_resolution": 1,  # p33 D5 target-schema pair read — imports step_errors (layer 0)
    "response_utils": 0,
    "dynamo_typing": 0,
    "metrics_facade": 1,
    "eventbridge_utils": 1,
    "eventbridge_publisher": 2,
    "notification_service": 2,
    "prompt_template_loader": 2,
    "mapping_memory": 1,
    "agentcore_proxy": 0,
    "sfn_task_token": 1,  # imports step_errors (layer 0) — P28 R19.11 token callback
    "sfn_token_errors": 0,  # P28 R2.3 consumed-token code set (p28b R3) — third-party imports only
    "audit_utils": 0,
    "retry_utils": 0,
    "jdbc_helpers": 0,
    "strands_mcp_factory": 3,
    "mcp_integration": 3,
    "agent_trace_plugin": 1,
    # Vendored SigV4 MCP transport (single-sourced here for the dual
    # Lambda-layer/AgentCore-container path, P28 R19.6) — third-party imports only.
    "streamable_http_sigv4": 0,
}


def validate_import_order() -> None:
    """
    Parse AST of every shared module and verify no module imports from a higher layer.
    Only checks top-level imports (not imports inside function bodies, which are
    runtime-conditional and gated by feature flags).
    Raises ImportError with details if a violation is found.
    Only runs when AGENTIC_DATA_INGESTION_ENV=dev.
    """
    import ast
    import pathlib

    shared_dir = pathlib.Path(__file__).parent
    violations = []

    for py_file in sorted(shared_dir.glob("*.py")):
        module_name = py_file.stem
        if module_name == "__init__" or module_name not in _LAYER_MAP:
            continue
        my_layer = _LAYER_MAP[module_name]

        tree = ast.parse(py_file.read_text())
        # Only check top-level statements (not inside functions/classes)
        for node in ast.iter_child_nodes(tree):
            imported_name = None
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported_name = alias.name.split(".")[0]
                    if imported_name and imported_name in _LAYER_MAP:
                        dep_layer = _LAYER_MAP[imported_name]
                        if dep_layer > my_layer:
                            violations.append(
                                f"{module_name} (layer {my_layer}) imports {imported_name} (layer {dep_layer})"
                            )
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_name = node.module.split(".")[0]
                if imported_name and imported_name in _LAYER_MAP:
                    dep_layer = _LAYER_MAP[imported_name]
                    if dep_layer > my_layer:
                        violations.append(
                            f"{module_name} (layer {my_layer}) imports {imported_name} (layer {dep_layer})"
                        )

    if violations:
        raise ImportError(
            "Shared module dependency hierarchy violations:\n" + "\n".join(f"  - {v}" for v in violations)
        )
    logger.info(f"Import order validation passed for {len(_LAYER_MAP)} modules")


# Auto-validate in dev/test
if os.environ.get("AGENTIC_DATA_INGESTION_ENV", "").lower() in ("dev", "test"):
    validate_import_order()
