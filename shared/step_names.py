# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Canonical_Step_Names — the single source of truth for pipeline step names.

P28 R8.2: one shared constants module, one casing, to be used by Lambdas, the
ASL definition (via :func:`to_sfn_state_name`), retry_utils, and approvals.
This module only DEFINES the canonical names (P28 task 2.1); codebase-wide
adoption happens in task 14.2.

Casing decision: **snake_case is canonical.**
    - ``shared/step_errors.py`` and all ten ``@step_handler`` decorators
      already use snake_case (``"file_validation"``, ``"schema_mapping"``, …),
      so StepFailure serialization and Failure_Records are snake_case.
    - The RecordFailure Lambda's ``PROGRESS_ORDER`` and ``failed_step``
      attribution are snake_case.
    - The most-queried DynamoDB state rows (``file_validation``,
      ``schema_extract``, ``schema_compare``, ``schema_mapping``) are
      snake_case.
    - PascalCase is confined to Step Functions state ids plus six legacy state
      rows — :func:`to_sfn_state_name` covers the SFN surface, and task 14.2
      migrates the rows. snake_case is therefore the least-churn choice.

Step-name variants cataloged in the codebase (July 2026), all parsed by
:func:`from_any`:

    ==========================  ==========================  ============================
    Canonical (snake_case)      SFN state id (PascalCase)   Legacy variants found
    ==========================  ==========================  ============================
    file_validation             FileValidation              —
    schema_extract              SchemaExtract               —
    schema_compare              SchemaCompare               —
    check_schema_action         CheckSchemaAction           —
    schema_mapping              SchemaMapping               —
    check_schema_mapping_       CheckSchemaMappingApproval  schema_mapping_approval
      approval                                                (record_failure)
    create_code                 CreateCode                  CodeGeneration (state rows),
                                                              code_generation (approvals)
    test_code                   TestCode                    CodeTesting (state rows),
                                                              code_testing (approvals,
                                                              agent traces)
    check_code_approval         CheckCodeApproval           code_approval (record_failure)
    create_infrastructure       CreateInfrastructure        infrastructure (approval_type,
                                                              phantom state rows)
    create_glue_pipeline        CreateGluePipeline          —
    create_quality_checks       CreateQualityChecks         first_run (approval_type,
                                                              phantom state rows)
    run_pipeline                RunPipeline                 —
    ==========================  ==========================  ============================

The three ``check_*`` members are the state machine's Choice states (approval
gates). They appear in ``retry_utils.STEP_ORDER`` and RecordFailure's
progress inference, but they never write state rows, so they have no entry in
:data:`STEP_SEQUENCE`.

Deliberately NOT members (state-table ``step_name`` values that are records,
not pipeline steps): ``pipeline_failure`` (RecordFailure sentinel, sequence
99), ``self_healing`` / ``data_quality`` (agent records), ``cache``
(dashboard metrics cache row).
"""

from collections.abc import Mapping
from enum import StrEnum, unique
from typing import Final


@unique
class StepName(StrEnum):
    """Canonical pipeline step names (snake_case).

    ``StrEnum`` (not ``str, Enum``) so that f-string interpolation and
    ``str()`` yield the canonical value — ``f"{StepName.FILE_VALIDATION}"``
    is ``"file_validation"``, never ``"StepName.FILE_VALIDATION"``. Members
    ARE strings: safe to use directly as DynamoDB keys and in JSON payloads.
    """

    FILE_VALIDATION = "file_validation"
    SCHEMA_EXTRACT = "schema_extract"
    SCHEMA_COMPARE = "schema_compare"
    CHECK_SCHEMA_ACTION = "check_schema_action"
    SCHEMA_MAPPING = "schema_mapping"
    CHECK_SCHEMA_MAPPING_APPROVAL = "check_schema_mapping_approval"
    CREATE_CODE = "create_code"
    TEST_CODE = "test_code"
    CHECK_CODE_APPROVAL = "check_code_approval"
    CREATE_INFRASTRUCTURE = "create_infrastructure"
    CREATE_GLUE_PIPELINE = "create_glue_pipeline"
    CREATE_QUALITY_CHECKS = "create_quality_checks"
    RUN_PIPELINE = "run_pipeline"


# Pipeline execution order — mirrors the state machine chain in
# deploy-cdk/lib/orchestration-stack.ts and retry_utils.STEP_ORDER.
# The single source that retry_utils and RecordFailure's PROGRESS_ORDER
# adopt in task 14.2.
CANONICAL_ORDER: Final[tuple[StepName, ...]] = (
    StepName.FILE_VALIDATION,
    StepName.SCHEMA_EXTRACT,
    StepName.SCHEMA_COMPARE,
    StepName.CHECK_SCHEMA_ACTION,
    StepName.SCHEMA_MAPPING,
    StepName.CHECK_SCHEMA_MAPPING_APPROVAL,
    StepName.CREATE_CODE,
    StepName.TEST_CODE,
    StepName.CHECK_CODE_APPROVAL,
    StepName.CREATE_INFRASTRUCTURE,
    StepName.CREATE_GLUE_PIPELINE,
    StepName.CREATE_QUALITY_CHECKS,
    StepName.RUN_PIPELINE,
)

# Canonical step → Step Functions state id (orchestration-stack.ts).
# Explicit (not derived) so a future state id that doesn't PascalCase-derive
# cleanly cannot silently diverge; completeness is enforced by tests.
SFN_STATE_NAMES: Final[Mapping[StepName, str]] = {
    StepName.FILE_VALIDATION: "FileValidation",
    StepName.SCHEMA_EXTRACT: "SchemaExtract",
    StepName.SCHEMA_COMPARE: "SchemaCompare",
    StepName.CHECK_SCHEMA_ACTION: "CheckSchemaAction",
    StepName.SCHEMA_MAPPING: "SchemaMapping",
    StepName.CHECK_SCHEMA_MAPPING_APPROVAL: "CheckSchemaMappingApproval",
    StepName.CREATE_CODE: "CreateCode",
    StepName.TEST_CODE: "TestCode",
    StepName.CHECK_CODE_APPROVAL: "CheckCodeApproval",
    StepName.CREATE_INFRASTRUCTURE: "CreateInfrastructure",
    StepName.CREATE_GLUE_PIPELINE: "CreateGluePipeline",
    StepName.CREATE_QUALITY_CHECKS: "CreateQualityChecks",
    StepName.RUN_PIPELINE: "RunPipeline",
}

# SFN state ids in execution order — the drop-in replacement for
# retry_utils.STEP_ORDER (task 14.2: STEP_ORDER = list(SFN_STATE_ORDER)).
SFN_STATE_ORDER: Final[tuple[str, ...]] = tuple(SFN_STATE_NAMES[step] for step in CANONICAL_ORDER)

# step_sequence attribute written to DynamoDB state rows by the task-step
# Lambdas (cataloged from lambda/*/lambda_function.py put_item calls).
# Choice states (check_*) never write state rows and have no sequence.
# RecordFailure's synthetic pipeline_failure row uses 99 (not a step).
STEP_SEQUENCE: Final[Mapping[StepName, int]] = {
    StepName.FILE_VALIDATION: 1,
    StepName.SCHEMA_EXTRACT: 2,
    StepName.SCHEMA_COMPARE: 3,
    StepName.SCHEMA_MAPPING: 4,
    StepName.CREATE_CODE: 5,
    StepName.TEST_CODE: 6,
    StepName.CREATE_INFRASTRUCTURE: 7,
    StepName.CREATE_GLUE_PIPELINE: 8,
    StepName.CREATE_QUALITY_CHECKS: 9,
    StepName.RUN_PIPELINE: 10,
}

# The ten task steps (state-row writers) in execution order.
TASK_STEPS: Final[tuple[StepName, ...]] = tuple(step for step in CANONICAL_ORDER if step in STEP_SEQUENCE)

# Legacy name variants found in the codebase that are NOT a casing variant of
# the canonical or SFN name. Keys are written in their as-found form and
# normalized when the lookup table is built. Each entry cites its source.
_LEGACY_ALIASES: Final[Mapping[str, StepName]] = {
    # create_code WROTE state rows with step_name="CodeGeneration" until
    # task 14.2 adopted the canonical name; "code_generation" is its
    # approval_type. Rows with the legacy name persist in live tables.
    "CodeGeneration": StepName.CREATE_CODE,
    # test_code WROTE state rows with step_name="CodeTesting" until task
    # 14.2; "code_testing" appears as an approval_type mapping key and in
    # agent trace records. Rows with the legacy name persist in live tables.
    "CodeTesting": StepName.TEST_CODE,
    # record_failure PROGRESS_ORDER names the approval gates without the
    # Check prefix.
    "schema_mapping_approval": StepName.CHECK_SCHEMA_MAPPING_APPROVAL,
    "code_approval": StepName.CHECK_CODE_APPROVAL,
    # create_infrastructure's approval_type is "infrastructure" (~444); the
    # approvals handler writes phantom state rows keyed by approval_type
    # (R8.3 removes the writes; old rows may persist).
    "infrastructure": StepName.CREATE_INFRASTRUCTURE,
    # create_quality_checks' approval_type is "first_run" (~206) — same
    # phantom-row mechanism as "infrastructure".
    "first_run": StepName.CREATE_QUALITY_CHECKS,
}


def _normalize(value: str) -> str:
    """Casing/word-separator-insensitive normal form for name lookup."""
    return value.replace("_", "").replace("-", "").lower()


def _build_lookup() -> dict[str, StepName]:
    """Build the normalized name → StepName table (canonical, SFN, legacy)."""
    lookup: dict[str, StepName] = {}
    for step in StepName:
        lookup[_normalize(step.value)] = step
        lookup[_normalize(SFN_STATE_NAMES[step])] = step
    for alias, step in _LEGACY_ALIASES.items():
        lookup[_normalize(alias)] = step
    return lookup


_LOOKUP: Final[Mapping[str, StepName]] = _build_lookup()


def to_sfn_state_name(step: StepName) -> str:
    """Return the Step Functions state id for a canonical step.

    >>> to_sfn_state_name(StepName.FILE_VALIDATION)
    'FileValidation'
    """
    return SFN_STATE_NAMES[step]


def from_any(value: str | StepName) -> StepName:
    """Parse any known step-name variant into the canonical StepName.

    Accepts canonical snake_case, PascalCase SFN state ids, the cataloged
    legacy variants (``CodeGeneration``, ``code_testing``, ``infrastructure``,
    ``first_run``, ``schema_mapping_approval``, ``code_approval``, …), and any
    casing/word-separator variation of those (matching is case- and
    ``_``/``-``-insensitive).

    Args:
        value: A step name in any known variant, or an existing StepName.

    Returns:
        The canonical StepName member.

    Raises:
        ValueError: For unknown names, naming the offending value.
    """
    if isinstance(value, StepName):
        return value
    step = _LOOKUP.get(_normalize(value))
    if step is None:
        raise ValueError(
            f"Unknown step name: {value!r} — not a canonical step name, "
            f"SFN state id, or cataloged legacy variant (see shared/step_names.py)"
        )
    return step
