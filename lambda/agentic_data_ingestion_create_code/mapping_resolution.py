# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Mapping/metadata read layer for create_code (P28 R19.1 decomposition).

Extracted verbatim from ``lambda_function.py`` so the mapping-resolution
logic is a testable unit. This module owns NO AWS clients: every function
takes the DynamoDB table handle(s) it reads as parameters. ``lambda_function``
keeps the boto3 resources and passes them in via thin delegating wrappers,
which also preserves the existing test seam (``patch.object(lambda_function,
"pipelines_table", ...)`` still intercepts, because the wrapper resolves the
table from lambda_function's namespace at call time).

Contracts carried by this module:
- P28 R6: freshest-approved-mapping selection (``_select_freshest_mapping``),
  including the source-schema compatibility filter — a candidate mapping is
  eligible only when it was built against the schema THIS run extracted.
- P28 R17.3: read-error classification in the getters — infrastructure
  errors (throttles, access denied) raise via ``raise_for_read_client_error``
  instead of masquerading as not-found.
"""

import json
from typing import Any

from aws_lambda_powertools import Logger
from boto3.dynamodb.conditions import Attr, Key
from botocore.exceptions import ClientError

# Typed accessors for DynamoDB item values (P22 Req 2)
from dynamo_typing import item_dict, item_str

# One definition of "approved for reuse", shared with schema_compare and
# schema_mapping (LGR-5d68eeb-2).
from mapping_reuse import approved_for_reuse_condition

# p47 R12.4: the one pipelines-table record read, parameterised by this
# module's not-found sentinel ({}).
from pipeline_record_read import read_pipeline_record

# P28 R17.3: read-error classification for the DynamoDB getters.
from step_errors import raise_for_read_client_error
from step_names import StepName

logger = Logger(service="agentic_data_ingestion")

# P28 R6: page bound for the newer-mapping GSI walk. DynamoDB applies `Limit`
# BEFORE FilterExpression, so a small Limit could return zero schema_mapping
# rows even when they exist (the old Limit=5 bug) — instead we paginate the
# full partition with a defensive page cap. Each run writes ~8 step rows and
# a page holds up to 1MB, so 10 pages covers thousands of runs per pipeline.
_MAPPING_LOOKUP_MAX_PAGES = 10


def _extract_field_mappings(mapping_result: dict) -> list[dict]:
    """Extract the field-mappings list from a mapping record.

    The mapping item stores mappings in multiple possible locations:
    1. 'field_mappings' (legacy direct field)
    2. 'mapping_config.field_mappings' (legacy nested)
    3. 'mapping_result' (JSON string with 'mappings' key — current
       schema_mapping output)

    Shared by generate_code (template render) and lambda_handler (approval
    payload) so both derive rules from the identical mapping view.
    """
    field_mappings = mapping_result.get(
        "field_mappings", mapping_result.get("mapping_config", {}).get("field_mappings", [])
    )
    if not field_mappings:
        # Parse the JSON-serialized mapping_result from schema_mapping Lambda
        raw_mapping_result = mapping_result.get("mapping_result", "")
        if isinstance(raw_mapping_result, str) and raw_mapping_result:
            try:
                parsed = json.loads(raw_mapping_result)
                field_mappings = parsed.get("mappings", [])
            except (json.JSONDecodeError, TypeError):
                pass
        elif isinstance(raw_mapping_result, dict):
            field_mappings = raw_mapping_result.get("mappings", [])
    return field_mappings or []


def _extract_unmapped_target_fields(mapping_result: dict) -> list:
    """Extract the ``unmapped_target_fields`` list from a mapping record.

    The sibling of :func:`_extract_field_mappings`, resolving the SAME three
    storage variants in the same order and with the same fall-through rule:

    1. ``unmapped_target_fields`` (top level, alongside legacy ``field_mappings``)
    2. ``mapping_config.unmapped_target_fields`` (legacy nested)
    3. ``mapping_result`` (JSON string, or dict, with an ``unmapped_target_fields``
       key — current schema_mapping output)

    It exists because variant 3 is the live one and on that variant the list is
    nested *inside the JSON string* ``_extract_field_mappings`` parses and then
    discards — it returns ``parsed.get("mappings", [])`` and nothing else. So a
    caller reading ``mapping_result["unmapped_target_fields"]`` directly works on
    the legacy shape and silently finds nothing on the shape production writes.

    Elements are returned untouched, in the record's own order: this resolves
    WHERE the list lives, never what its elements mean. Both element shapes the
    two producers write (a ``{field, default_value, reason}`` dict from
    ``schema_mapping.generate_simple_mapping``, and a bare string from model
    output parsed as returned) are handled by the reader, in
    ``code_assembly._unmapped_target_field_name``.

    Total, like its sibling: an unparseable JSON string, a non-list value or an
    absent key all answer ``[]`` rather than raising. This feeds a prompt
    annotation, so it must never be the thing that fails code generation.
    """
    unmapped = mapping_result.get(
        "unmapped_target_fields", mapping_result.get("mapping_config", {}).get("unmapped_target_fields", [])
    )
    if not unmapped:
        raw_mapping_result = mapping_result.get("mapping_result", "")
        if isinstance(raw_mapping_result, str) and raw_mapping_result:
            try:
                parsed = json.loads(raw_mapping_result)
                unmapped = parsed.get("unmapped_target_fields", [])
            except (json.JSONDecodeError, TypeError, AttributeError):
                pass
        elif isinstance(raw_mapping_result, dict):
            unmapped = raw_mapping_result.get("unmapped_target_fields", [])
    return unmapped if isinstance(unmapped, list) else []


def get_pipeline_metadata(pipeline_id: str, pipelines_table: Any) -> dict:
    """Get pipeline metadata including approval configuration.

    Not-found returns {} so approval-config lookups fall back to defaults
    (P21 Req 6.4). Infrastructure errors (throttles, access denied) raise
    instead of masquerading as missing metadata (P28 R17.3).

    p47 E2 (R12.4): the ``get_item`` + classify + ``"Item" in response``
    mechanism lives once in
    ``shared/pipeline_record_read.read_pipeline_record``. The ``{}`` here is
    passed in, not assumed — ``test_code`` passes ``None`` and that difference is
    ratified (``LGR-b2f79ef-8``), so the shared mechanism must not pick. The
    table stays an injected parameter, and the delegating wrapper at
    ``lambda_function.py:153`` stays (R12.2): it is the seam
    ``create_code/test_read_path_errors.py`` reads this function *through*.
    """
    item: dict = read_pipeline_record(
        pipelines_table,
        pipeline_id,
        step_name="create_code",
        operation="get_pipeline_metadata",
        not_found={},
        logger=logger,
        classify=raise_for_read_client_error,
    )
    return item


def get_regeneration_feedback(run_id: str, pipeline_runs_table: Any) -> dict:
    """Get feedback from previous testing iterations for code regeneration.

    Not-found returns {} so regeneration proceeds as a first-pass generation
    (P21 Req 6.4). Infrastructure errors (throttles, access denied) raise
    instead of silently dropping prior feedback (P28 R17.3 — a dropped
    feedback read would produce the identical-prompt retry R13 forbids).
    """
    try:
        # Look for testing feedback in DynamoDB from previous iterations
        # (test_code writes its state row under the canonical step name).
        response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": StepName.TEST_CODE})
    except ClientError as e:
        raise_for_read_client_error(e, "create_code", operation="get_regeneration_feedback")
        logger.error(f"Pipeline-runs table not found getting regeneration feedback for {run_id}: {e}")
        return {}

    if "Item" in response:
        item = response["Item"]
        regeneration_feedback = item_dict(item, "regeneration_feedback")
        if regeneration_feedback:
            logger.info(f"Found regeneration feedback for run_id: {run_id}")
            return regeneration_feedback

    logger.info(f"No regeneration feedback found for run_id: {run_id}")
    return {}


def _query_approved_schema_mapping_rows(pipeline_id: str, pipeline_runs_table: Any) -> list[dict]:
    """All APPROVED schema_mapping state rows for a pipeline (P28 R6).

    Queries the state table's PipelineIndex GSI (PK pipeline_id, SK timestamp)
    and paginates until exhausted (or the defensive page cap).

    "Approved" semantics: mappings-table records carry no status attribute —
    approval state lives on the state-table schema_mapping row. Its lifecycle:
    schema_mapping writes ``status="success"``; when a manual approval is
    required the approval_handler flips it to ``waiting_approval``; the
    approvals flow then sets ``success`` (approved) or ``rejected``. A
    rejection at a later run that reused the mapping stamps
    ``reuse_revoked_at`` on this row instead. So
    ``mapping_reuse.approved_for_reuse_condition()`` — ``status == "success"``
    and no ``reuse_revoked_at`` — is exactly the set of mappings safe to
    substitute, and it is the same predicate ``schema_compare`` uses.
    """
    items: list[dict] = []
    last_key = None
    for _ in range(_MAPPING_LOOKUP_MAX_PAGES):
        kwargs: dict = {
            "IndexName": "PipelineIndex",
            "KeyConditionExpression": Key("pipeline_id").eq(pipeline_id),
            "FilterExpression": (
                Attr("step_name").eq("schema_mapping") & Attr("mapping_id").exists() & approved_for_reuse_condition()
            ),
            "ScanIndexForward": False,
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        resp = pipeline_runs_table.query(**kwargs)
        items.extend(resp.get("Items", []))
        last_key = resp.get("LastEvaluatedKey")
        if not last_key:
            break
    return items


def _select_freshest_mapping(
    pipeline_id: str,
    state_mapping_id: str,
    current_mapping_response: dict,
    *,
    pipeline_runs_table: Any,
    mappings_table: Any,
    run_schema_hash: str = "",
) -> tuple[str, dict]:
    """Return the ``(mapping_id, get_item response)`` to use for codegen (P28 R6).

    Evaluates ALL approved schema_mapping state rows for the pipeline (no
    first-item break), resolves each candidate's mappings-table record, and
    selects the newest by ``created_timestamp`` — the attribute
    store_mapping_separately actually writes (not ``created_at``/``timestamp``).
    Ties break on mapping_id so selection is deterministic; candidates with a
    missing created_timestamp sort as ``""`` (oldest) and can never win over a
    timestamped mapping.

    The newer mapping is substituted only when its created_timestamp is
    strictly greater than the state-carried mapping's, and the substitution is
    logged with both ids (R6.3). A dangling state-carried mapping_id (no
    mappings-table record → current timestamp ``""``) is rescued by any
    timestamped approved candidate.

    The lookup is best-effort: on any error the state-carried mapping is kept.

    **``run_schema_hash`` — freshness is not compatibility.** A candidate is
    eligible only when its ``source_schema_hash`` equals the hash of the schema
    THIS run extracted. Recency alone was the whole eligibility test, and
    "newest approved mapping for this pipeline" silently means "newest mapping
    for whatever the source looked like when it was built". Observed on
    pipeline ``latesttest-50e07536``, run
    ``run-2026-08-15-19-15-27-e99dbcbe``: the run extracted
    ``c76edff25e69…`` (declaring ``email_address``), the state-carried mapping
    ``a5a00ebac4a903530e6a785af8295005`` was built against that same hash and
    mapped ``email_address → email``, and a strictly newer approved mapping
    ``96ca07130bea2308215a46b321c5ee63`` — built against ``d9d3b7e1…``, whose
    source declared ``email`` — won on recency for all three regeneration
    attempts. Both hashes were written onto the generated-code record and
    nothing compared them, so the transform prompt was told ``email`` was a
    source column and the pandas proxy raised ``KeyError: 'email'``.

    Two properties of the comparison, each load-bearing:

    - **An absent or empty ``run_schema_hash`` disables the filter entirely.**
      The comparison is then impossible, and a lookup that cannot establish
      compatibility must not start refusing candidates it previously accepted —
      that would degrade codegen for every pipeline whose ``schema_extract``
      row is missing the attribute. Recency-only selection is the documented
      behaviour in that case.
    - **A candidate carrying no ``source_schema_hash`` is treated as NOT
      matching** and skipped. ``store_mapping_separately`` writes the attribute
      on every put, substituting the literal ``"no-hash"`` when its own value
      is falsy (``schema_mapping/lambda_function.py:1489``), so an absent hash
      is a real anomaly rather than a supported state — the stance
      ``LGR-b2f79ef-11`` (template / ratified) already took when it made an
      absent hash a ``StepFailure`` instead of a fabricated UUID. Such a
      candidate could not be used even if selected: ``lambda_function``'s
      schema-identity read fails naming the absent attribute. Skipping it lets
      a hash-carrying compatible candidate win instead of failing the step.
      The literal ``"no-hash"`` needs no special case — it never equals a
      real hash.

    **The incumbent is NOT validated here.** The state-carried mapping competes
    only as the incumbent; if it too was built against a different schema, the
    net is ``code_assembly._validate_mapped_source_fields``, which fails
    ``generate_code`` naming the undeclared source field. Rejecting the
    incumbent in this function would be a second behaviour change on a
    different code path. Known limitation, deliberately out of scope.
    """
    try:
        state_rows = _query_approved_schema_mapping_rows(pipeline_id, pipeline_runs_table)

        # Distinct candidate ids; the state-carried mapping competes only as
        # the incumbent (substitution requires strictly newer).
        candidate_ids = {item_str(row, "mapping_id") for row in state_rows}
        candidate_ids.discard("")
        candidate_ids.discard(state_mapping_id)

        # Resolve each candidate's mappings-table record; skip dangling ids and
        # any candidate built against a different source schema than this run
        # extracted.
        candidates: list[tuple[str, str, dict]] = []
        for cand_id in candidate_ids:
            cand_resp = mappings_table.get_item(Key={"mapping_id": cand_id})
            if "Item" not in cand_resp:
                continue
            cand_source_hash = item_str(cand_resp["Item"], "source_schema_hash")
            if run_schema_hash and cand_source_hash != run_schema_hash:
                logger.info(
                    "Skipping approved mapping built against a different source schema",
                    extra={
                        "state_mapping_id": state_mapping_id,
                        "skipped_mapping_id": cand_id,
                        "run_schema_hash": run_schema_hash,
                        "skipped_mapping_source_schema_hash": cand_source_hash,
                    },
                )
                continue
            cand_ts = item_str(cand_resp["Item"], "created_timestamp")
            candidates.append((cand_ts, cand_id, cand_resp))

        if not candidates:
            return state_mapping_id, current_mapping_response

        # Newest first, deterministic tiebreak on mapping_id.
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        selected_ts, selected_id, selected_resp = candidates[0]

        current_item = current_mapping_response.get("Item", {})
        current_ts = item_str(current_item, "created_timestamp")

        if selected_ts > current_ts:
            logger.info(
                "Substituting newer approved mapping for state-carried mapping",
                extra={
                    "state_mapping_id": state_mapping_id,
                    "selected_mapping_id": selected_id,
                    "state_mapping_created_timestamp": current_ts,
                    "selected_mapping_created_timestamp": selected_ts,
                },
            )
            return selected_id, selected_resp
    except Exception as lookup_err:
        logger.warning(f"Failed to check for newer mapping: {lookup_err}")
    return state_mapping_id, current_mapping_response
