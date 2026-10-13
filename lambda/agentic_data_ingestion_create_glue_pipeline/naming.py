# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Deterministic resource-naming helpers for the create_glue_pipeline lambda.

Centralizes derivation of the per-pipeline Glue execution-role name so that the
three independent call sites — role *creation* (``iam_roles``), prerequisite
*validation* (``artifacts``), and ARN *reference* (``job_update_handler``) —
always agree on the same name.

Crucially, the derived name always satisfies IAM's hard 64-character ``RoleName``
constraint regardless of how long ``pipeline_id`` is. Before this helper, the
name was built inline as ``AgenticDataIngestion-{pipeline_id}-ExecutionRole``; for long
pipeline ids that exceeds 64 chars, and ``iam:GetRole`` rejects the malformed
name with a ``ValidationError`` (not ``NoSuchEntity``). That caused job
prerequisite validation to fail, the Glue job to never be created, and the
downstream RunPipeline step to fail with a misleading "Glue job name not found".

This module is intentionally dependency-free (no boto3, no shared-layer imports)
so it is cheap to import from any sibling module and trivial to unit test.
"""

from __future__ import annotations

import hashlib

# AWS hard limit on IAM RoleName length.
MAX_ROLE_NAME_LEN = 64

_ROLE_PREFIX = "AgenticDataIngestion-"
_ROLE_SUFFIX = "-ExecutionRole"
# Length of the deterministic hash appended when truncation is required.
_HASH_LEN = 8


def pipeline_execution_role_name(pipeline_id: str) -> str:
    """Return the Glue execution-role name for ``pipeline_id`` (<= 64 chars).

    The canonical form is ``AgenticDataIngestion-{pipeline_id}-ExecutionRole``. When that
    fits within IAM's 64-character limit it is returned unchanged (preserving
    existing role names for already-deployed pipelines).

    When it would exceed 64 characters, the ``pipeline_id`` is deterministically
    truncated and an 8-character hash of the *full* ``pipeline_id`` is appended.
    This keeps the name unique across pipelines whose ids share a long common
    prefix, and guarantees the same ``pipeline_id`` always maps to the same role
    name so creation, validation, and ARN reference stay consistent.
    """
    canonical = f"{_ROLE_PREFIX}{pipeline_id}{_ROLE_SUFFIX}"
    if len(canonical) <= MAX_ROLE_NAME_LEN:
        return canonical

    # sha256 (not md5/sha1) keeps the security linter happy; only the first
    # _HASH_LEN hex chars are used purely for collision-resistance, not secrecy.
    digest = hashlib.sha256(pipeline_id.encode("utf-8")).hexdigest()[:_HASH_LEN]
    suffix = f"-{digest}{_ROLE_SUFFIX}"
    avail = MAX_ROLE_NAME_LEN - len(_ROLE_PREFIX) - len(suffix)
    # Defensive: prefix+suffix alone are well under 64, so avail is always > 0.
    truncated = pipeline_id[: max(avail, 0)].rstrip("-")
    return f"{_ROLE_PREFIX}{truncated}{suffix}"


def pipeline_execution_role_arn(pipeline_id: str, account_id: str) -> str:
    """Return the IAM ARN for the pipeline's Glue execution role.

    Uses :func:`pipeline_execution_role_name` so the ARN always references the
    same (bounded) role name that ``iam_roles`` creates and ``artifacts``
    validates.
    """
    return f"arn:aws:iam::{account_id}:role/{pipeline_execution_role_name(pipeline_id)}"
