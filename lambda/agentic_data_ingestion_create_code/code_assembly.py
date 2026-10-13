# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Template selection, render, and DQDL assembly for create_code (P28 R19.1).

Extracted verbatim from ``lambda_function.py`` so the deterministic
template-first code-assembly path is a testable unit:

- ``TEMPLATE_RULES`` / ``_select_template``: ordered (source_type,
  target_type) template selection.
- ``_generate_dqdl_rules`` / ``_uniqueness_hint_fields``: DQDL ruleset
  inference (P28 R11.2 word-boundary matching + merge_key schema hint).
- ``detect_rule_contradictions``: the p37 R2.3 contradiction check — a DQDL
  rule asserting non-nullness of a column the AI transform assigns a
  constant null. AST-based, non-blocking.
- ``detect_date_format_mismatches``: the p49 R1.1 date-format check — a
  ``to_date``/``to_timestamp`` format that parses NONE of the source column's
  sampled values. AST-based, non-blocking, and given its samples by an
  injected reader. It answers a second question on the same nodes (p49 R5.1,
  design C3) into an optional ``ambiguities`` out list: a format the samples
  do not *refute* may still not be *settled* by them, and neither arm is ever
  allowed to pick a layout silently.
- ``generate_code``: template render with the AI transform block.

This module owns NO AWS clients and does NOT import strands: the transform
generator is injected via ``generate_transform`` (lambda_function passes its
own ``generate_transform_with_ai`` binding, preserving the existing test
seam), the quarantine bucket is passed in by the caller, and — p49 C1 — the
sample reader arrives the same way, as the injected ``sample_reader``
callable. ``lambda_function.py`` owns ``s3_client``; do NOT add a boto3
import here to save a parameter (p49 do-NOT 10).

The deprecated ``TEMPLATE_MAP`` alias that lived alongside these in
lambda_function is DELETED (P28 R19.1 dead-path removal) — its two test
consumers were migrated to ``_select_template``/``TEMPLATE_RULES``.
"""

import ast
import json
import os
import re
from collections.abc import Callable
from datetime import datetime
from typing import Any, Final

import jinja2
from aws_lambda_powertools import Logger
from mapping_resolution import _extract_field_mappings, _extract_unmapped_target_fields

# Threat model T-01: deny-by-default AST guard on the AI transform block.
from transform_safety import validate_transform_block_safety

# p55 R4.1: the Source_Delimiter for the four templates' csv read. Flat import,
# matching this module's other shared imports (`identifier_utils` and
# `step_errors`) — `shared/` is on `/opt/python` in the Lambda and on the
# AgentCore container's path. NOT a local copy of the table: `LGR-eee8ac8-5`'s
# reason applies verbatim ("a second definition in either Lambda would disable
# the check silently by never matching").
from format_delimiters import delimiter_for_key

# P28 R12.1: Identifier_Allowlist validator applied at this module's two
# injection sinks — field names entering DQDL rule strings and the merge_key
# entering the rendered MERGE INTO statement.
from identifier_utils import parse_identifier_list, validate_identifier, validate_identifiers

# The first-run watermark lower bound, resolved by the same function the
# dashboard API validates it with. Pure: `jdbc_helpers` imports only `re` and
# `typing` at module level (boto3 is imported inside the connection helpers),
# so this keeps `code_assembly` free of AWS clients (`LGR-1e135d2-2`).
from jdbc_helpers import initial_watermark_entry

# p64 R6.6: the DEFAULT DQ posture, imported rather than restated for the same
# reason `format_delimiters` above is. `shared/quality_verdict.py` is the module
# that reports the consequence of this default to the dashboard, so a second copy
# of the set here would let the renderer and the reporter disagree about which
# pipelines run DQ — the exact divergence R6.6 exists to prevent. Layer 0, pure,
# no AWS client: importing it does not break
# `test_code_assembly_still_owns_no_aws_client`.
from quality_verdict import (
    CAST_NULL_CHECK_ENABLED_KEY,
    DATA_QUALITY_ENABLED_KEY,
    DEDUP_COLUMNS_KEY,
    DEDUP_ENABLED_KEY,
    DQ_DEFAULT_ON_TEMPLATES,
    NONPRINTABLE_CHECK_ENABLED_KEY,
    QUALITY_CONFIG_KEY,
)

# P28 R1.1 / R3.3: loud degradation in this Lambda travels as StepFailure —
# `generate_transform_with_ai` raises it for the same class of reason.
from step_errors import StepFailure

# Which write modes each target's writer implements — the same policy the
# dashboard API's create and edit enforce. Pure, standard library only.
from write_modes import write_mode_refusal

logger = Logger(service="agentic_data_ingestion")

# p64 R6.6 / R7 — the key on the pipeline record that carries the quality
# opt-ins. A `quality_config` sub-object PEER to `approval_config`, per design C7,
# and deliberately NOT a key inside `target_config`: `generate_code` splats
# source_config and target_config wholesale into the render context, so a flag
# there would become a template variable with no backend change and no
# validation, and a data-quality setting is not target configuration.
#
# The key names moved to `shared/quality_verdict.py` in p64 2c.1 and are
# IMPORTED above rather than declared here, because 2c.1 made the sub-object
# WRITABLE from `dashboard_api`'s `create_pipeline` — so writer and reader now sit
# in two Lambdas that can only be compared through these strings
# (`LGR-eee8ac8-5`). While 2b.4 was the only party that touched them a local
# declaration was honest; it stopped being so the moment there was a second party.
# The names are re-exported by this import, so `code_assembly.QUALITY_CONFIG_KEY`
# still resolves for existing readers.

# Ordered template-selection rules: (source_type, target_type, filename).
# Evaluated in order; the first matching rule wins. "*" is a wildcard that
# matches any value in the corresponding position.
#
# JDBC source rule precedes S3_UPLOAD rules so that a JDBC pipeline routes
# to the JDBC source template regardless of target type. The
# ("S3_UPLOAD", "rds_database", ...) entry restores deterministic codegen
# for the P14 RDS Database target — see design.md "P14 Baseline" (Task 9.4).
TEMPLATE_RULES: list[tuple[str, str, str]] = [
    ("JDBC", "*", "glue_job_jdbc_source.py.j2"),
    ("S3_UPLOAD", "s3_tables", "glue_job_s3_tables.py.j2"),
    ("S3_UPLOAD", "glue_catalog", "glue_job_glue_catalog.py.j2"),
    ("S3_UPLOAD", "jdbc", "glue_job_jdbc.py.j2"),
    ("S3_UPLOAD", "rds_database", "glue_job_jdbc.py.j2"),  # P14 codegen fix
    ("S3_UPLOAD", "s3_parquet", "glue_job_s3_parquet.py.j2"),
]


def _select_template(
    source_type: str,
    target_type: str,
    rules: list[tuple[str, str, str]] | None = None,
) -> str | None:
    """Return the PySpark template filename for a (source_type, target_type) pair.

    Iterates ``rules`` (defaults to :data:`TEMPLATE_RULES`) in declared order
    and returns the filename of the first matching rule. A rule matches when
    its ``source_type`` equals the input or is ``"*"``, AND its
    ``target_type`` equals the input or is ``"*"``. Returns ``None`` when no
    rule matches.

    The wildcard semantics let a single rule cover all target types for a
    given source (e.g., ``("JDBC", "*", ...)``) or all sources for a given
    target. Order matters: place more specific rules before broader ones.
    """
    if rules is None:
        rules = TEMPLATE_RULES
    for rule_source, rule_target, filename in rules:
        source_match = rule_source == "*" or rule_source == source_type
        target_match = rule_target == "*" or rule_target == target_type
        if source_match and target_match:
            return filename
    return None


def resolve_data_quality_enabled(pipeline_metadata: dict | None, template_name: str | None) -> bool:
    """Whether the rendered job carries a DQ block, for THIS pipeline (p64 R6.6).

    THE ONE AUTHORITY for that question. ``generate_code`` calls it to set the
    ``data_quality_enabled`` render variable, and ``create_code``'s handler calls
    it again to record the same answer on the state row, from which
    ``shared/quality_verdict.derive_quality_verdict`` reports it. Two callers, one
    derivation — deliberately, because a second derivation is how a renderer and a
    reporter come to disagree about whether a job gates its write, which is
    exactly the divergence R6.6 exists to prevent (``LGR-eee8ac8-6``).

    WHY A GATE EXISTS AT ALL. Before p64, ``glue_job_s3_tables.py.j2`` was the
    only template with an ``EvaluateDataQuality`` block. R6.1/R6.2 put that block
    in all five, which — ungated — would have given every existing pipeline of
    every other target type row-level splitting, a quarantine write and a DQ
    verdict it had never had, without anyone asking for it. R6.6 forbids that:
    "the verdict mode must not silently start reporting ``enforced`` for pipelines
    whose behaviour did not change."

    WHY THE DEFAULT IS PER-TEMPLATE AND NOT SIMPLY ``False``. "No existing
    pipeline's behaviour changes" has two directions, and a flat ``False`` default
    only satisfies one of them: it would have REMOVED the DQ block from every
    existing S3 Tables pipeline, moving that pipeline's verdict from ``enforced``
    to ``advisory`` and dropping a quarantine mechanism it has had since P28. That
    is the same silent reclassification in the other direction. So the default is
    the posture each template had before p64 — :data:`DQ_DEFAULT_ON_TEMPLATES` —
    and the explicit setting overrides it either way.

    Args:
        pipeline_metadata: The pipeline record. ``quality_config`` is read from
            it as a top-level peer of ``approval_config`` (design C7), NOT from
            ``target_config`` — see :data:`QUALITY_CONFIG_KEY`. p64 2c owns
            writing and validating it; until then the key is absent and every
            pipeline takes the default.
        template_name: The template ``_select_template`` chose. ``None`` or ``""``
            is the full-AI fallback path, which renders no template and therefore
            has no DQ block to enable — so it is always ``False``, whatever the
            pipeline asked for. Every template name ``_select_template`` CAN
            return does include the partial: that is pinned by
            ``tests/unit/templates/test_data_quality_all_targets.py``'s
            ``test_the_block_is_included_twice_and_never_copied``, which asserts
            exactly two include sites in each of the five, so "non-empty name
            implies DQ-capable" is asserted rather than assumed and no second
            inventory of template names is kept here.

    Returns:
        ``True`` when the render must emit the DQ block for this pipeline.

    Only an explicit ``bool`` in ``quality_config`` overrides the default. A
    string, ``None`` or a missing key all fall through — this reads a record that
    2c has not yet started writing, and truthiness on an unvalidated value would
    make ``"false"`` mean enabled. Validation belongs at the API boundary with
    ``_validate_approval_config``'s peers (R10.4), not here.
    """
    if not template_name:
        return False
    quality_config = (pipeline_metadata or {}).get(QUALITY_CONFIG_KEY) or {}
    if isinstance(quality_config, dict):
        explicit = quality_config.get(DATA_QUALITY_ENABLED_KEY)
        if isinstance(explicit, bool):
            return explicit
    return template_name in DQ_DEFAULT_ON_TEMPLATES


#: The sink context for the dedup columns' ``validate_identifier`` call.
#: Distinguishable from every other sink string in this module on purpose — p55
#: task 6.7 pinned that the existing sinks pass different strings, so a refusal
#: names which sink refused. This one reaches ``dropDuplicates([...])`` in the
#: rendered job (p64 2c.3 emits it).
_DEDUP_COLUMN_SINK: Final[str] = "dedup column list (quality_config.dedup_columns)"


def resolve_quality_config(pipeline_metadata: dict | None) -> dict:
    """The Quality_Config as the RENDER context should see it (p64 R7.1/R7.2, C7).

    Threaded into ``template_vars`` under :data:`QUALITY_CONFIG_KEY`,
    unconditionally, at ``generate_code``'s top level — **not** routed through the
    ``source_config``/``target_config`` splat. Design C7 rejects that route
    explicitly: the splat would make a flag a template variable with no backend
    change and no validation, and ``target_config`` is a free-form
    ``Record<string, string>``, so it is a place to hide an unvalidated boolean.
    ``tests/unit/templates/test_dq_template_vars_contract.py`` asserts the
    unconditional half mechanically.

    **This NORMALISES; it does not validate.** Refusal happens at the API boundary
    in ``pipelines_crud._validate_quality_config`` (R10.4), which 400s a malformed
    submission before anything is persisted. By the time a record reaches here it
    has either passed that validator or predates it, so the job of this function is
    to hand the template a shape it can read without a ``default()`` filter: every
    recognised key present, every value the declared type. A record that predates
    2c.1 has no ``quality_config`` at all and takes the defaults, which are off.

    **Every key is present in the returned dict, and that is the point.** The
    render environment uses ``StrictUndefined``, and a jinja2 dict access to a
    missing key yields an ``Undefined`` whose ``__bool__`` raises — so
    ``{% if quality_config.dedup_enabled %}`` would fail the render for a pipeline
    that simply never set it. Filling the keys here is what lets 2c.3's template
    read them plainly, instead of reaching for ``| default(...)``, which do-NOT 6
    forbids for a reason (``_partials/_load_counts.py.j2``'s header: two jinja2
    copies in one process make the ``missing`` sentinel comparison fail, and the
    filter hands the foreign sentinel straight through).

    **``data_quality_enabled`` is deliberately NOT in the returned dict.** It has
    ONE authority — :func:`resolve_data_quality_enabled` — because its default is
    *per template* rather than a flat ``False``, and R6.6 requires both directions
    of "no existing pipeline's behaviour changes": a flat default would strip the DQ
    block from every existing S3 Tables pipeline. That resolver needs
    ``template_name``, which this function does not take, and copying its answer in
    here would be the second derivation ``LGR-eee8ac8-6`` was reopened to prevent.
    The render context therefore carries the gate as its own scalar
    ``data_quality_enabled`` and the dedup settings under ``quality_config``. If a
    later reader wants them in one place, move the gate INTO this dict by calling
    the resolver — do not recompute it.

    **``dedup_columns`` reaches a generated-code sink, so it is validated here
    too.** Each entry goes through ``validate_identifier`` and an offender fails
    code generation loudly with ``IdentifierValidationError`` naming it (R12.2;
    do-NOT 3 — the validator itself is untouched, only called). This is defence in
    depth behind the API refusal, and it is the same posture the two existing sinks
    in this module take. It is cheap to satisfy in practice: p55 R11.3's rename
    projection means every target column name downstream has already passed the
    same allowlist, so a legitimate dedup column cannot fail this.

    **Ordering.** The caller invokes this in the ``template_vars`` block, which is
    AFTER the ``merge_key`` sink and after ``_generate_dqdl_rules``' per-field call.
    That ordering is load-bearing in the same way p37 R2.7's is: an input that trips
    one of those must still fail with *that* sink's error naming *that* identifier.
    Do not hoist this ahead of them.

    Args:
        pipeline_metadata: The pipeline record. ``quality_config`` is read from it
            as a top-level peer of ``approval_config``, never from
            ``target_config``.

    Returns:
        ``{"dedup_enabled": bool, "dedup_columns": list[str],
        "nonprintable_check_enabled": bool, "cast_null_check_enabled": bool}`` —
        always every key. p64 2d.2 added the third and 2e.1 the fourth; the "every
        recognised key present" rule above is what makes adding one a one-line
        change here rather than a template change too.

    Raises:
        IdentifierValidationError: If a ``dedup_columns`` entry is not a valid
            identifier. Not caught here; ``generate_code``'s caller reports it.
    """
    submitted = (pipeline_metadata or {}).get(QUALITY_CONFIG_KEY)
    if not isinstance(submitted, dict):
        # Covers absent, None, and a scalar left by a writer without the
        # validator in front of it. All three mean "nothing was asked for".
        submitted = {}

    # Only an explicit `bool` opts in, for the same reason the DQ gate above
    # takes that line: truthiness on an unvalidated value would make the string
    # `"false"` mean enabled.
    dedup_enabled_raw = submitted.get(DEDUP_ENABLED_KEY)
    dedup_enabled = dedup_enabled_raw if isinstance(dedup_enabled_raw, bool) else False

    # A non-list, or a list carrying a non-string, degrades to "all mapped target
    # columns" rather than raising — this is the shape half, which the boundary
    # validator already refuses, and a malformed container here means the columns
    # were never usable. The IDENTITY half below does raise, because a
    # well-formed list of hostile strings is the injection case and must not be
    # silently narrowed to something safe-looking.
    columns_raw = submitted.get(DEDUP_COLUMNS_KEY)
    dedup_columns: list[str] = []
    if isinstance(columns_raw, (list, tuple)) and all(isinstance(entry, str) for entry in columns_raw):
        dedup_columns = validate_identifiers(list(columns_raw), _DEDUP_COLUMN_SINK)

    # R8.1's character check, normalised exactly like `dedup_enabled` above and
    # for the same reason: only an explicit `bool` opts in, so a submitted
    # `"false"` cannot mean enabled. This one carries NO identifier sink — the
    # rendered check derives its column list from `df_transformed.dtypes` at
    # runtime, so nothing from the pipeline record reaches the generated code and
    # there is no name here for `validate_identifier` to guard. See
    # `_partials/_nonprintable_check.py.j2`'s header.
    nonprintable_raw = submitted.get(NONPRINTABLE_CHECK_ENABLED_KEY)
    nonprintable_enabled = nonprintable_raw if isinstance(nonprintable_raw, bool) else False

    # R9.1's type-cast failure count, normalised exactly like the two flags above
    # and for the same reason. It carries NO identifier sink either: the rendered
    # count derives its column list from `_target_schema` and
    # `df_transformed.columns` at runtime, mirroring the auto-cast loop it sits in
    # front of, so nothing from the pipeline record reaches the generated code.
    #
    # This key is normalised for ALL FIVE templates even though only the two S3
    # Tables arms render anything for it. That is deliberate: the resolver's job is
    # the render context's shape, and making it template-aware would put a second
    # derivation of "which templates have an auto-cast block" here, beside the one
    # the include sites already express — the mistake `LGR-eee8ac8-6` was reopened
    # to prevent, and the reason `data_quality_enabled` is NOT in this dict. The
    # cost is a render variable four templates read and three ignore, which is
    # already true of the two above. See `_partials/_cast_null_check.py.j2`'s header
    # for the reach argument and for why an unmeasured run reports no entry rather
    # than zero.
    cast_null_raw = submitted.get(CAST_NULL_CHECK_ENABLED_KEY)
    cast_null_enabled = cast_null_raw if isinstance(cast_null_raw, bool) else False

    return {
        DEDUP_ENABLED_KEY: dedup_enabled,
        DEDUP_COLUMNS_KEY: dedup_columns,
        NONPRINTABLE_CHECK_ENABLED_KEY: nonprintable_enabled,
        CAST_NULL_CHECK_ENABLED_KEY: cast_null_enabled,
    }


# Resolve pyspark_templates directory (Lambda layer or local)
_PYSPARK_TEMPLATE_DIRS = [
    os.path.join("/opt/python", "pyspark_templates"),  # Lambda layer
    os.path.join(os.path.dirname(__file__), "..", "..", "pyspark_templates"),  # Local dev
    "/app/pyspark_templates",  # AgentCore container
]


def _get_pyspark_jinja_env() -> jinja2.Environment:
    """Return a Jinja2 Environment that loads from pyspark_templates/."""
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(_PYSPARK_TEMPLATE_DIRS),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


# P28 R11.2: Uniqueness rule inference uses whole-name (word-boundary)
# matching via re.fullmatch — never substring containment. The old
# `"key" in field_lower` check added Uniqueness to any field merely
# CONTAINING a keyword ("monkey" matched "key", "wide_identity" matched
# "_id"), producing false uniqueness constraints that quarantine/fail
# legitimate duplicate values. A field name must match one of these
# patterns in FULL (case-insensitive) to be inferred as a unique key.
_UNIQUENESS_NAME_PATTERN = re.compile(r"pk_.*|.*_id|id|uuid|guid|email")


def _uniqueness_hint_fields(target_config: dict | None) -> set[str]:
    """Return schema-hint field names (lowercased) that imply uniqueness.

    Hint sources available in this system today (P28 R11.2 investigation):

    - ``target_config.merge_key`` — the user-configured MERGE INTO key from
      the pipeline wizard. A merge key is by definition the record identity
      for upserts, so it is a strong uniqueness hint regardless of its name
      shape. Only an EXPLICITLY present merge_key counts: generate_code
      fabricates a template-var default of "id", and that fabricated default
      must not manufacture a Uniqueness rule.
    - Mapping records carry {source_field, target_field, transformation,
      confidence} only — no primary_key flags exist on mappings or on the
      JSON-schema-style target schema (verified against schema_mapping's
      record shape). If primary-key metadata is added later, surface it here.
    """
    if not target_config:
        return set()
    merge_key = target_config.get("merge_key")
    if isinstance(merge_key, str) and merge_key.strip():
        return {merge_key.strip().lower()}
    return set()


def _has_usable_source_field(mapping: Any) -> bool:
    """Does one Field_Mapping_Entry name a source column?

    The explicit ``isinstance(str)`` + non-empty ``.strip()`` test, hoisted out
    of ``_generate_dqdl_rules`` (p37 C7) so this module has **one** definition of
    "unsourced" and the two things derived from it — the DQDL skip (R2.1/R2.2)
    and the transform prompt's annotation (R3.5, C12) — cannot disagree about
    which entries are unsourced. Truthiness is deliberately NOT used: a mapping
    record is parsed JSON, so the value can be ``None``, ``""``, ``"   "`` or a
    non-string.

    The sibling of ``schema_mapping.lambda_function._has_usable_source_field``,
    which carries the same predicate for the mapping-side readers (C9/C10). The
    two live in different Lambdas and cannot import each other; the shared
    definition is the docstring in both plus the tests that pin the four unusable
    shapes.
    """
    if not isinstance(mapping, dict):
        return False
    source_field = mapping.get("source_field")
    return isinstance(source_field, str) and bool(source_field.strip())


def _unmapped_target_field_name(entry: Any) -> str:
    """Name of one ``unmapped_target_fields`` entry, tolerating both shapes.

    A behaviour-for-behaviour copy of
    ``schema_mapping.lambda_function._unmapped_target_field_name``, for the same
    reason the two ``_has_usable_source_field`` copies exist: the two Lambdas
    cannot import each other, and the two readers of that list must agree about
    which names it supplies. ``tests/unit/lambdas/schema_mapping/
    test_unsourced_definition_parity.py`` is the only mechanical link between
    them.

    Two producers write the list and they disagree on the element shape.
    ``schema_mapping.generate_simple_mapping`` writes dicts — ``{field,
    default_value, reason}`` — and so does the Bedrock prompt's declared response
    shape, but a Bedrock response is model output parsed as returned, so a bare
    string is reachable. Both answer here; anything else names nothing.

    ``default_value`` is deliberately NOT read. The prompt annotation says
    "null/default" and never names a value (``LGR-b2f79ef-1`` is why the wording
    is load-bearing: a missing target column fails the ``schema_compliance``
    gate, so the model must be told to produce the column, not to skip it).
    Rendering the recorded default would change the annotation text, which is
    pinned byte-for-byte by ``test_transform_prompt_unsourced_annotation.py``.
    """
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        name = entry.get("field", "")
        return name.strip() if isinstance(name, str) else ""
    return ""


def _unsourced_target_field_names(
    field_mappings: list[dict] | None,
    unmapped_target_fields: list | None = None,
) -> list[str]:
    """Target field names that no source column supplies, from BOTH lists.

    p37 R3.5 (design C12): the transform prompt annotates these so the model is
    told a target field has no source instead of being left to improvise one
    from an unrelated column. Order-preserving and de-duplicated, across both
    lists — ``unmapped_target_fields`` first, then ``field_mappings``, matching
    the declared read order of the sibling reader named below.

    Reads **both** lists, which is the whole of this function's contract:

    1. ``unmapped_target_fields`` — where ``schema_mapping``'s name-similarity
       fallback already puts an unmatched target field, and where p37 C9's
       ``_normalise_mapping_structure`` MOVES an entry the Bedrock path claimed
       without a source. After C9, this is where an unsourced field normally
       lives.
    2. every ``field_mappings`` entry carrying a ``target_field`` but no usable
       ``source_field`` — the pre-C9 placement, which stays reachable forever
       because C9 normalises at generation time and cannot reach a record
       already written. That is the ``phone`` shape observed on pipeline
       newtest-ab2a08a0 / run run-2026-08-09-15-38-19-5c5f5f8a.

    This is the definition ``schema_mapping.unsourced_target_field_names``
    carries under ``LGR-9e76249-7`` (**template / ratified**), which records why
    reading only one list is wrong: it makes the caller order-dependent on C9.
    This copy read only list 2 and its docstring argued the omission was safe
    because such a field "carries no rule and the model is not told to produce it
    from anything". **That reasoning was false** — the target-schema block in
    ``transform_generation.j2`` tells the model to produce every field the target
    schema declares, unconditionally — so the annotation was withheld from
    exactly the fields that need it most, and the model invented a source. The
    two readers now name the same set for the same record.

    Derived from the same two lists ``generate_code`` hands
    ``_generate_dqdl_rules`` and the prompt — ``_extract_field_mappings`` and
    ``_extract_unmapped_target_fields`` over one ``mapping_result`` — through the
    SAME ``_has_usable_source_field`` predicate. The branch A coupling therefore
    still holds, over the union of the two lists rather than over ``mappings``
    alone: a name from list 1 is annotated AND carries no Completeness rule,
    because it never enters ``_generate_dqdl_rules``' loop at all. Teaching that
    function about list 1 would emit ``Completeness >= 0.95`` on a column the
    transform sets to a constant — the p37 R2.1/R2.2 defect the skip exists to
    prevent.

    Names only what the two lists supply, and never invents one: an entry naming
    no target field contributes nothing rather than an empty column name.
    """
    names: list[str] = []
    for entry in unmapped_target_fields or []:
        name = _unmapped_target_field_name(entry)
        if name and name not in names:
            names.append(name)
    for mapping in field_mappings or []:
        if not isinstance(mapping, dict):
            continue
        target_field = mapping.get("target_field")
        if not isinstance(target_field, str) or not target_field.strip():
            continue
        if _has_usable_source_field(mapping):
            continue
        name = target_field.strip()
        if name not in names:
            names.append(name)
    return names


def _declared_source_fields(source_schema: dict | None) -> dict:
    """The source columns the extracted schema declares.

    A behaviour-for-behaviour copy of
    ``test_code.code_interpreter_runner.declared_source_fields`` — the read the
    pandas test frame is built from. The two live in different Lambdas and cannot
    import each other; ``tests/unit/lambdas/create_code/
    test_declared_source_fields_parity.py`` is the only mechanical link between
    them, the same arrangement the two ``_has_usable_source_field`` copies use.

    ``properties`` wins whenever the KEY is present, even when its value is
    empty; only an absent ``properties`` falls through to ``fields``. That is the
    behaviour p33 requirement 3.14 pins, so it is reproduced rather than tidied.
    Do NOT rewrite as ``schema.get("properties") or schema.get("fields", {})`` —
    that inverts the rule for ``{"properties": {}, "fields": {...}}``.

    Moving the original into ``shared/`` (which is bundled into the Lambda layer,
    so it would work) was rejected: it relocates a function p33 3.14 pins next to
    the frame builder it exists to agree with, and widens this change well past
    the fix it belongs to.
    """
    schema = source_schema or {}
    fields: dict = schema.get("properties", schema.get("fields", {}))
    return fields


# The declared-column list travels in the StepFailure message and details, which
# reach the SFN Cause, the errors table and the dashboard. Bounded for the reason
# ``_bound_stderr`` bounds stderr one Lambda over — a diagnostic must not be the
# thing that makes a write fail (cf. LGR-b2f79ef-12, which reasons about the same
# per-column growth against the 400 KB item limit).
_MAX_NAMES_IN_FAILURE = 50


def _bounded_names(names: list[str]) -> list[str]:
    """``names`` truncated to ``_MAX_NAMES_IN_FAILURE``, with the count of the rest."""
    if len(names) <= _MAX_NAMES_IN_FAILURE:
        return list(names)
    return [*names[:_MAX_NAMES_IN_FAILURE], f"... and {len(names) - _MAX_NAMES_IN_FAILURE} more"]


def _validate_mapped_source_fields(field_mappings: list[dict] | None, source_schema: dict | None) -> None:
    """Fail code generation when a mapping names a source column that is not declared.

    The transform prompt is never shown the source schema (deliberate, and pinned
    — see ``transform_generation.generate_transform_with_ai``'s docstring, p33
    design R4), so ``mappings[*].source_field`` is the model's only notion of a
    legal source column. Nothing validated those names, and a mapping naming a
    column the source does not have produced a transform reading
    ``df_transformed["email"]`` against a test frame built strictly from
    ``declared_source_fields`` — observed as ``KeyError: 'email'`` in the pandas
    proxy.

    **Loud, not recovered.** Two softer shapes were rejected. Reclassifying the
    entry as unsourced would take the run GREEN with a silently-null column and
    no Completeness rule, turning a human's approval of ``email → email`` into
    nothing. Annotating it but keeping the Completeness rule so DQDL fails costs
    the one-predicate coupling ``_unsourced_target_field_names`` carries (the
    annotated set IS the Completeness-skipped set). Failing here generates no bad code at all, which is
    the same stance ``generate_transform_with_ai`` already takes: its docstring
    records that the silent ``df_transformed = df`` fallback was removed because
    it "shipped untransformed data under a green pipeline" (P28 R3.3).

    **An empty declared set never fails.** ``schema_extract`` legitimately writes
    ``{"type": "object", "properties": {}}`` for a source it parsed and found no
    records in, the handler omits ``source_schema`` entirely when the
    ``schema_extract`` state row is missing, and an array-shaped legacy schema
    declares its columns under ``items`` where this read finds none. Validating
    against an empty set would fail every one of those pipelines.

    **Entries with no usable source_field are not this check's business.**
    ``_has_usable_source_field`` already classifies ``None`` / ``""`` / ``"   "``
    / non-string as unsourced and p37 C12 annotates them; this applies only to an
    entry whose ``source_field`` IS a usable string the schema does not declare.

    **Membership is exact.** No case-folding, no trimming, no prefix logic — a
    near-miss is precisely the defect being caught, and trimming would let
    ``df["email "]`` reach the proxy and reproduce the original ``KeyError`` one
    space over.

    ORDERING IS LOAD-BEARING (p37 R2.7, the same rule the DQDL skip carries): the
    caller runs this AFTER both existing identifier sinks — ``merge_key`` and
    ``_generate_dqdl_rules``' per-field ``validate_identifier`` — so an input that
    fails one of those still fails with its ``IdentifierValidationError``, naming
    the identifier. Do not move this ahead of them.

    Raises:
        StepFailure: naming every offending source column, the target field each
            was mapped to, and the columns the source actually declares.
            ``StepFailure`` is this Lambda's loud-degradation carrier (P28 R3.3).
    """
    declared = _declared_source_fields(source_schema)
    if not isinstance(declared, dict) or not declared:
        return

    offenders: list[tuple[str, str]] = []
    seen: set[str] = set()
    for mapping in field_mappings or []:
        if not _has_usable_source_field(mapping):
            continue
        source_field = mapping["source_field"]
        if source_field in declared or source_field in seen:
            continue
        seen.add(source_field)
        target_field = mapping.get("target_field")
        offenders.append((source_field, target_field if isinstance(target_field, str) else ""))

    if not offenders:
        return

    undeclared = [source_field for source_field, _ in offenders]
    declared_names = list(declared)
    rendered = ", ".join(f"{source_field!r} (mapped to target {target!r})" for source_field, target in offenders)
    raise StepFailure(
        "create_code",
        f"Field mapping names {len(offenders)} source column(s) the source schema does not "
        f"declare: {rendered}. The source schema declares "
        f"{_bounded_names(declared_names)}. Generating code from this mapping would produce a "
        "transform that reads a column the source does not have, which fails at test time with "
        "a KeyError. Fix the mapping's source_field, or re-extract the source schema if the "
        "column really does exist in the data.",
        details={
            "undeclared_source_fields": _bounded_names(undeclared),
            "undeclared_mappings": [
                {"source_field": source_field, "target_field": target}
                for source_field, target in offenders[:_MAX_NAMES_IN_FAILURE]
            ],
            "declared_source_fields": _bounded_names(declared_names),
            "declared_source_field_count": len(declared_names),
        },
    )


# ── p55 W4 — the Rename_Manifest projection (R11.1/R11.2, design W4-D1/W4-D2) ──
#
# A Non_Conforming_Column_Name is read from the source by its ORIGINAL name and
# written to the target by its APPROVED name. The bridge is one explicit
# projection emitted immediately after the read, ahead of the transform, the
# DQDL rules and the MERGE INTO:
#
#     df = df.withColumnRenamed("first name", "first_name")
#
# Why this shape and not another (W4-D1):
#
#   * ``withColumnRenamed`` takes STRING arguments, not SQL identifiers, so an
#     original name never enters a ``spark.sql`` statement or a DQDL rule
#     string. The Identifier_Allowlist is neither weakened nor bypassed at
#     either sink (do-NOT 2), and ``validate_identifier`` is not touched
#     (do-NOT 3 — it has 42 callers).
#   * Renaming FIRST is what makes "everything downstream sees only approved
#     names" structural rather than a promise: the DQDL rule fields, the
#     merge_key and the target write are all fed post-rename values that have
#     already passed ``validate_identifier`` unmodified (R11.3). A name that
#     fails it still fails loudly, naming the offending identifier (R11.4).
#   * ``mappings[*].source_field`` keeps naming the ORIGINAL column, and this
#     module translates through the manifest when it emits the transform. That
#     is what keeps ``LGR-a419843-2`` passing verbatim: its declared-source-field
#     check compares against ``schema_extract``'s output, i.e. original names, so
#     ``_declared_source_fields`` / ``_validate_mapped_source_fields`` are
#     UNTOUCHED and still receive the untranslated mappings.
#
# Rejected alternatives, recorded so they are not re-reached for:
#   * Renaming at extraction, so ``schema_extract`` emits sanitized names. The
#     extracted schema would then describe columns the file does not have, and
#     the Schema_Hash (``LGR-a419843-1``) would be computed over sanitized
#     names — so changing the sanitization rule would silently invalidate every
#     stored mapping.
#   * A Glue job argument renamed at runtime. It puts attacker-controlled text
#     into ``DefaultArguments``, the surface ``LGR-819980f-1`` (**invariant**)
#     exists to keep clean, and the rename would not be visible in the code a
#     human approves.
#   * ``df.toDF(*approved_names)``. Shorter and position-dependent, so a source
#     whose column order changes between runs renames the wrong columns
#     SILENTLY. ``withColumnRenamed`` is name-keyed.

# Cap on the length of an original column name emitted into the generated job
# (W4-D2). The NUMBER mirrors ``identifier_utils._DISPLAY_LIMIT``: 255
# (``MAX_IDENTIFIER_LENGTH``) plus the same 45 characters of slack, for the same
# stated purpose — the artifact "stays log-friendly even for megabyte-sized
# garbage", and here it is a script a human reviews as well as a log line.
#
# The MECHANISM is deliberately NOT mirrored. ``_display`` truncates, which is
# safe there because the value is only being DISPLAYED. Here the literal SELECTS
# a column, and ``withColumnRenamed`` is a silent no-op on a name no column
# carries — so a truncated literal would skip the rename without a word and
# leave the original name to reach the DQDL/MERGE sink. Over the cap this
# refuses LOUDLY instead (do-NOT 1: nothing is sanitized silently).
#
# That also reproduces the asymmetry p55 task 6.7 pinned in ``_display``: the
# truncating branch in ``_bounded_original_name`` below is only ever reached by a
# value that has ALREADY failed this check.
_MAX_ORIGINAL_NAME_CHARS: Final[int] = 300

# The sink context for the projection's own ``validate_identifier`` call. It is
# distinguishable from BOTH merge_key contexts on purpose — p55 task 6.7 pinned
# that the two existing sinks pass different strings
# (``job_builders.py``'s "--merge_key Glue job argument" and this module's
# "target_config.merge_key"), and a third sink inherits that discipline rather
# than reusing a neighbour's wording.
_RENAME_TARGET_SINK = "column rename projection target (Rename_Manifest.target_name)"

# The sink context for `target_config.partition_columns`, which the templates
# splice into `partitionBy(...)` / `partitionedBy(...)` / `repartition(...)` as
# an argument list. Distinct from the three contexts above for the reason given
# there.
_PARTITION_COLUMNS_SINK = "partition columns (target_config.partition_columns)"

# S3's bucket-naming rule: 3-63 characters of lowercase letters, digits, `.` and
# `-`, starting and ending with a letter or digit. Used as an allowlist, so a
# value that passes cannot close a string literal or open an f-string field.
_S3_BUCKET_NAME: Final[re.Pattern[str]] = re.compile(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]")


def _partition_columns_argument(configured: object) -> str | None:
    """The partition columns as the argument list the templates splice in, or ``None``.

    The templates emit ``partitionBy({{ partition_columns }})`` with the value
    unescaped, so whatever is bound here is Python source in the job. Until
    2026-10-10 the stored value was bound as-is: a configured
    ``__import__("os").getcwd()`` rendered as a call, and the documented
    ``"region,dt"`` shape rendered as two bare names, a ``NameError`` at run
    time. Each name now passes the Identifier_Allowlist through
    ``parse_identifier_list`` and is emitted as a string literal, giving
    ``"region", "dt"``: the form the template tests have always rendered.
    ``None`` when no column is configured, which the templates' ``{% if %}``
    renders as no partitioning, as before.

    Raises:
        IdentifierValidationError: naming the entry, for any name that is not
            an identifier or a value that is neither a list nor a string.
    """
    names = parse_identifier_list(configured, _PARTITION_COLUMNS_SINK)
    if not names:
        return None
    return ", ".join(_python_string_literal(name) for name in names)


def _checkpoint_bucket(config: dict[str, Any]) -> str:
    """The checkpoint bucket name, validated, when checkpointing is enabled; else ``""``.

    ``checkpoint_enabled`` and ``checkpoint_bucket`` reach the render only
    through the source/target config merge (nothing in this Lambda sets them),
    and four templates emit the bucket inside an f-string,
    ``f"s3://{{ checkpoint_bucket }}/checkpoints/..."``. Unvalidated, a value
    holding a quote or an ``{...}`` field became code in the job. *config* is
    the merged render context, so the truthiness test here is the same one the
    templates' ``{% if checkpoint_enabled %}`` applies.

    Raises:
        ValueError: when checkpointing is enabled and the bucket is missing or
            is not a valid S3 bucket name.
    """
    if not config.get("checkpoint_enabled"):
        return ""
    bucket = config.get("checkpoint_bucket")
    if not isinstance(bucket, str) or not _S3_BUCKET_NAME.fullmatch(bucket):
        shown = repr(bucket)[:100]
        raise ValueError(
            f"checkpoint_bucket {shown} is not a valid S3 bucket name; checkpointing is enabled, so the job "
            "needs one (3-63 characters of lowercase letters, digits, '.' and '-')"
        )
    return bucket


# The projection's header comment, emitted once when at least one column is
# renamed. Fixed length, so the growth in the emitted block is exactly one line
# per renamed column and nothing else.
#
# It deliberately names no token this repository's template fences count on the
# RENDERED output — no ``spark.read``, ``df.cache()``, ``df.unpersist()``,
# ``.count()``, ``df.write`` or ``SELECT``. ``LGR-b46a5bb-4`` (template /
# ratified) is why that matters: substring assertions over rendered text forbid
# documenting the fence in the emitted text, and the first draft of the p53
# sample block broke five such assertions exactly this way.
_RENAME_BLOCK_HEADER: Final[tuple[str, ...]] = (
    "# p55 W4: rename non-conforming source columns to their approved targets.",
    "# Original names arrive as escaped string literals, never as SQL identifiers;",
    "# approved names passed the Identifier_Allowlist before this line was emitted.",
)


def _python_string_literal(value: str) -> str:
    """``value`` as a pure-ASCII, double-quoted Python string literal (W4-D2).

    ``json.dumps`` does the escaping — quotes, backslashes and control
    characters — and the double-quoted form keeps the emitted line's quoting
    consistent with the rest of the generated script. *Rejected: ``repr()``* for
    exactly that reason: it switches to single quotes as soon as the value
    contains a double quote, and it emits raw non-ASCII.

    **``ensure_ascii=False`` plus explicit escaping, NOT ``ensure_ascii=True``**,
    and the difference is a correctness one rather than a preference. Design
    W4-D2 asks for a literal "Python accepts for any input"; ``ensure_ascii=True``
    is not that, because JSON escapes a non-BMP character as a UTF-16 SURROGATE
    PAIR and a Python string literal reads ``"\\ud83d\\ude42"`` as two lone
    surrogates rather than as ``"🙂"``. Measured, not feared: the round trip fails
    for ``🙂`` and for ``U+10FFFF``. A spreadsheet header with an emoji in it is
    not exotic, and a literal that does not round-trip names no column — so
    ``withColumnRenamed`` would quietly do nothing and the original name would go
    on to the SQL sink.

    So this keeps ``ensure_ascii=True``'s stated PROPERTY — the emitted literal is
    pure ASCII for any input, escaping quotes, backslashes, newlines and
    non-ASCII — and gets it by escaping each non-ASCII character to ``\\uXXXX`` or
    ``\\UXXXXXXXX`` itself. That is safe to do after ``json.dumps`` precisely
    because every escape sequence JSON produces is pure ASCII, so no character
    this loop rewrites can be part of one. Post-processing the
    ``ensure_ascii=True`` output with a regex was rejected for the mirror-image
    reason: ``\\\\ud83d`` (an escaped backslash followed by literal text) is
    indistinguishable from an escape without counting preceding backslashes.
    """
    return "".join(
        character if character.isascii() else _unicode_escape(character)
        for character in json.dumps(value, ensure_ascii=False)
    )


def _unicode_escape(character: str) -> str:
    """One non-ASCII character as the Python escape Python reads back unchanged."""
    code_point = ord(character)
    return f"\\u{code_point:04x}" if code_point <= 0xFFFF else f"\\U{code_point:08X}"


def _bounded_original_name(name: str) -> str:
    """Log-safe rendering of an over-long original name for a failure message.

    Mirrors ``identifier_utils._display``'s shape — ``repr`` so control
    characters are visible rather than mangling the line, plus the true length
    when truncated. Only reachable for a value that already failed the
    ``_MAX_ORIGINAL_NAME_CHARS`` check, so this never truncates a name that is
    about to be emitted.
    """
    if len(name) <= _MAX_ORIGINAL_NAME_CHARS:
        return repr(name)
    return f"{name[:_MAX_ORIGINAL_NAME_CHARS]!r}… ({len(name)} chars)"


def _rename_pairs(rename_manifest: dict | None) -> list[tuple[str, str]]:
    """``(original_name, target_name)`` pairs from the manifest, IN MANIFEST ORDER.

    The stored manifest is the authority at code-generation time and is used
    **verbatim or not at all** — never recomputed here (design risk R5.7). The
    order is load-bearing and is preserved: R10.4 gives the collision suffix to
    the second and subsequent colliding name *in source-column order*, so a
    reordering would change which column gets which name. Nothing on this path
    sorts, re-keys or de-duplicates.

    A **conforming** column is not a manifest entry (``derive_rename_manifest``
    emits one entry per distinct NON-conforming column), so it yields no pair and
    therefore no projection line — it passes through the read unchanged.

    ``None``, a manifest with no ``renames``, and an empty ``renames`` all return
    ``[]``, which makes the whole feature inert and the generated job
    byte-identical to its pre-p55 form. That is the state every run is in until
    the manifest reaches this Lambda.

    A **structurally invalid** manifest raises instead of returning ``[]``.
    Returning ``[]`` would silently skip every rename and hand the original names
    to the DQDL/MERGE sink, which is do-NOT 1's failure mode wearing a different
    hat.

    Raises:
        StepFailure: if ``renames`` is not a list, an entry is not a mapping with
            two string names, or two entries name the same original with
            different targets (both could not be honoured).
    """
    if not rename_manifest:
        return []
    if not isinstance(rename_manifest, dict):
        raise StepFailure(
            "create_code",
            f"Rename_Manifest is a {type(rename_manifest).__name__}, not an object. The manifest is "
            "read verbatim at code generation and cannot be repaired here.",
        )
    renames = rename_manifest.get("renames")
    if renames is None:
        return []
    if not isinstance(renames, list):
        raise StepFailure(
            "create_code",
            f"Rename_Manifest 'renames' is a {type(renames).__name__}, not a list. Expected an ordered "
            "list of {original_name, target_name} entries.",
            details={"manifest_id": rename_manifest.get("manifest_id")},
        )

    pairs: list[tuple[str, str]] = []
    by_original: dict[str, str] = {}
    for index, entry in enumerate(renames):
        if not isinstance(entry, dict):
            raise StepFailure(
                "create_code",
                f"Rename_Manifest entry at index {index} is a {type(entry).__name__}, not an object with "
                "'original_name' and 'target_name'.",
                details={"manifest_id": rename_manifest.get("manifest_id"), "entry_index": index},
            )
        original_name = entry.get("original_name")
        target_name = entry.get("target_name")
        if not isinstance(original_name, str) or not isinstance(target_name, str):
            raise StepFailure(
                "create_code",
                f"Rename_Manifest entry at index {index} carries original_name of type "
                f"{type(original_name).__name__} and target_name of type {type(target_name).__name__}; "
                "both must be strings.",
                details={"manifest_id": rename_manifest.get("manifest_id"), "entry_index": index},
            )
        previous = by_original.get(original_name)
        if previous is not None and previous != target_name:
            raise StepFailure(
                "create_code",
                f"Rename_Manifest names source column {_bounded_original_name(original_name)} twice, "
                f"with different targets {previous!r} and {target_name!r}. Only one rename per column can "
                "be applied, so this manifest cannot be honoured.",
                details={"manifest_id": rename_manifest.get("manifest_id"), "entry_index": index},
            )
        if previous == target_name:
            continue
        by_original[original_name] = target_name
        pairs.append((original_name, target_name))
    return pairs


def _rename_lookup(rename_pairs: list[tuple[str, str]]) -> dict[str, str]:
    """``original_name`` → ``target_name``.

    Idempotent as a substitution: a target name satisfies the
    Identifier_Allowlist and every original in the manifest fails it, so a target
    can never itself be a key. Applying the lookup twice is therefore the same as
    applying it once, which is what lets the unsourced-target-field list be
    translated after ``_unsourced_target_field_names`` has already seen
    translated mappings.
    """
    return dict(rename_pairs)


def _renamed_field_mappings(field_mappings: list[dict] | None, lookup: dict[str, str]) -> list[dict]:
    """``field_mappings`` with ``source_field``/``target_field`` translated through the manifest.

    Returns a list of SHALLOW COPIES. The caller's list is never mutated, because
    ``_validate_mapped_source_fields`` must keep receiving the ORIGINAL names —
    ``LGR-a419843-2``'s declared-source-field check compares them against
    ``schema_extract``'s output, which is original names, and translating them
    would make that refusal validate against a fiction.

    ``target_field`` is translated as well as ``source_field``. That is not a
    namespace confusion: in the S3-upload case W4 exists for, the target columns
    ARE the source columns, so a non-conforming header appears on both sides of
    the mapping entry and the DQDL rule field has to become the approved name for
    R11.3 to hold. Where the target schema is a separate uploaded artifact its
    names are not manifest originals, so no translation happens.

    An empty ``lookup`` returns the entries unchanged (copied), so a run with no
    manifest produces byte-identical downstream inputs.
    """
    if not lookup:
        return list(field_mappings or [])
    translated: list[dict] = []
    for mapping in field_mappings or []:
        if not isinstance(mapping, dict):
            # Tolerated rather than refused, matching every other reader in this
            # module (`_has_usable_source_field`, `_unsourced_target_field_names`):
            # a mapping record is parsed JSON and this module never assumes shape.
            translated.append(mapping)
            continue
        entry = dict(mapping)
        for key in ("source_field", "target_field"):
            value = entry.get(key)
            if isinstance(value, str) and value in lookup:
                entry[key] = lookup[value]
        translated.append(entry)
    return translated


def _renamed_names(names: list[str], lookup: dict[str, str]) -> list[str]:
    """``names`` translated through the manifest, order and duplicates preserved."""
    if not lookup:
        return list(names)
    return [lookup.get(name, name) for name in names]


def _rename_projection_block(rename_pairs: list[tuple[str, str]]) -> str:
    """The rename projection as Python source, or ``""`` when nothing is renamed.

    One ``df = df.withColumnRenamed(<original>, <target>)`` line per pair, in
    manifest order, under a fixed three-line comment header. **A conforming
    column contributes no pair and therefore no line**, so a source whose headers
    all satisfy the Identifier_Allowlist renders exactly the pre-p55 script.

    **Both names are emitted as escaped string literals, never interpolated**
    (do-NOT 17, W4-D2) — see ``_python_string_literal``, which also records why
    ``json.dumps(..., ensure_ascii=True)`` alone is not enough. A CSV header is
    attacker-controlled text and W4 CREATES this sink; nothing guarded it before,
    because it did not exist.

    *Rejected: an f-string with the raw name.* That IS the injection.

    The target is passed through ``validate_identifier`` before it is emitted.
    It is not a SQL identifier at *this* sink, but it becomes one downstream —
    validating here is what makes R11.3's "everything downstream sees only names
    that passed the allowlist unmodified" true at the point of emission rather
    than three sinks later. The validator is used exactly as documented, inline
    and unmodified (do-NOT 3).

    Raises:
        StepFailure: if an original name exceeds ``_MAX_ORIGINAL_NAME_CHARS``,
            naming the offending column. See that constant for why this refuses
            rather than truncating.
        IdentifierValidationError: if a target name fails the
            Identifier_Allowlist, naming the identifier and this sink (R11.4).
            Reachable only from a manifest that bypassed
            ``derive_rename_manifest``'s closing invariant check — which is
            precisely the case worth failing on.
    """
    if not rename_pairs:
        return ""
    lines: list[str] = list(_RENAME_BLOCK_HEADER)
    for original_name, target_name in rename_pairs:
        if len(original_name) > _MAX_ORIGINAL_NAME_CHARS:
            raise StepFailure(
                "create_code",
                f"Source column name {_bounded_original_name(original_name)} is "
                f"{len(original_name)} characters, over the {_MAX_ORIGINAL_NAME_CHARS}-character limit for "
                "a name emitted into the generated job. It is refused rather than truncated: a truncated "
                "name matches no column, so the rename would silently not happen and the original name "
                "would reach the SQL and data-quality sinks. Rename the column at the source.",
                details={
                    "original_name_length": len(original_name),
                    "max_original_name_chars": _MAX_ORIGINAL_NAME_CHARS,
                    "target_name": target_name,
                },
            )
        validate_identifier(target_name, _RENAME_TARGET_SINK)
        original_literal = _python_string_literal(original_name)
        target_literal = _python_string_literal(target_name)
        lines.append(f"df = df.withColumnRenamed({original_literal}, {target_literal})")
    return "\n".join(lines)


def _generate_dqdl_rules(field_mappings: list[dict], unique_hint_fields: set[str] | None = None) -> str:
    """Generate a DQDL ruleset string from field mappings.

    Produces rules for:
    - RowCount > 0 (always)
    - Completeness for each target field
    - Uniqueness for fields whose FULL name matches the key-like allowlist
      (word-boundary matching — "monkey" does NOT match "key") or that are
      named by a schema hint (``unique_hint_fields``, e.g. the configured
      merge_key), which wins regardless of name shape.

    Rules are only emitted for fields present in the mappings — a hint
    naming an unmapped field produces no rule (a DQDL rule on a missing
    column would error the evaluation, not protect it).

    p37 R2.1/R2.2: the same principle covers an Unsourced_Target_Field — a
    mapping entry carrying a target_field but no usable source_field. No
    Completeness and no Uniqueness rule is emitted for one. See the comment
    at the skip for why, and for why it sits after identifier validation.
    """
    if not field_mappings:
        return ""

    hints = {h.lower() for h in (unique_hint_fields or set())}
    rules: list[str] = ["RowCount > 0"]

    for mapping in field_mappings:
        target_field = mapping.get("target_field", "")
        if not target_field:
            continue

        # P28 R12.1: the field name is interpolated into DQDL rule strings
        # that the template embeds in a triple-quoted block — enforce the
        # Identifier_Allowlist at rule assembly so a hostile CSV header can
        # never become code. Fails loudly naming the identifier (R12.2);
        # there is no sanitize-and-continue.
        validate_identifier(target_field, "DQDL rule field")

        # p37 R2.1/R2.2: an Unsourced_Target_Field gets NO rule. A mapping
        # entry with a target_field and no usable source_field can only be
        # satisfied by a constant — the AI transform sets the column to
        # lit(None) because it was told to produce the column — so
        # `Completeness >= 0.95` is a rule this same code generation makes
        # impossible to satisfy: every row fails it and the inline DQ split
        # quarantines the whole batch. Observed on pipeline newtest-ab2a08a0,
        # run run-2026-08-09-15-38-19-5c5f5f8a: target field `phone` had no
        # source, 10/11 rules passed, 5 rows quarantined, 0 rows loaded, and
        # the run still reported SUCCEEDED.
        #
        # "Usable" is `_has_usable_source_field` — an explicit str/strip test,
        # not truthiness on an arbitrary object: a mapping record is parsed
        # JSON, so the value can be None, "", "   " or a non-string. p37 C12's
        # `_unsourced_target_field_names` reads the same predicate, so the
        # prompt annotation and this skip cannot disagree.
        #
        # ORDERING IS LOAD-BEARING (p37 R2.7): this skip is deliberately
        # AFTER validate_identifier above. That call is P28 R12.1's injection
        # sink — the field name is interpolated into a triple-quoted block in
        # the rendered job — so an unsourced field with a hostile name must
        # STILL fail code generation naming the identifier. Do not restructure
        # into "skip first, validate later".
        if not _has_usable_source_field(mapping):
            logger.info(f"No DQDL rule for unsourced target field: {target_field}")
            continue

        # Completeness check for all mapped fields
        rules.append(f'Completeness "{target_field}" >= 0.95')

        # Uniqueness: schema hint (merge_key/primary key) wins regardless of
        # name shape; otherwise whole-name allowlist match (P28 R11.2).
        field_lower = target_field.lower()
        if field_lower in hints or _UNIQUENESS_NAME_PATTERN.fullmatch(field_lower):
            rules.append(f'Uniqueness "{target_field}" >= 0.95')

    dqdl = "Rules = [\n    " + ",\n    ".join(rules) + "\n]"
    return dqdl


# ── p37 R2.3 — the contradiction check (design C8) ───────────────────
#
# A DQDL rule that asserts a column is populated, on a column the transform
# block sets to a constant, is a rule the same code generation makes
# impossible to satisfy. R2.1/R2.2 (the skip in _generate_dqdl_rules above)
# is the deterministic half and covers the observed defect; this is defence
# in depth for the case the AI transform nulls a column that DOES have a
# source. It RECORDS and FEEDS BACK. It never blocks — see generate_code's
# caller in create_code/lambda_function.py.
#
# NOT a substring search for `lit(None)`. Attempt 2 of the live run produced
# `coalesce(lit(None).cast('string'), lit('unknown'))`, which contains no
# `lit(None)` token any naive matcher would accept as constant-null and still
# reads no source column. `LGR-b46a5bb-4` (template / ratified) records the
# same lesson for the thread-antipattern detector: substring matching forbids
# documenting the fence and fails on legitimate text. So this parses, using
# the same `ast` mechanism `validate_pyspark_antipatterns` uses
# (transform_generation.py:182-243).

# The DQDL rule kinds that assert a column carries values. Both are emitted
# by _generate_dqdl_rules above; RowCount is a frame-level rule and names no
# column, so it cannot contradict a per-column transform.
_DQDL_COLUMN_RULE_ENTRY = re.compile(r'(Completeness|Uniqueness)\s+"([^"]+)"')

# Calls whose presence anywhere in an expression means it reads a column.
# `col` is the rule the design states. `column` is its documented alias in
# pyspark.sql.functions. `expr` is included deliberately: its argument is a
# SQL string this check does not parse, so `expr("mobile")` reads a column
# that no AST edge expresses — counting it as constant-null would be a FALSE
# POSITIVE recorded where approvers read, which for a non-blocking heuristic
# is worse than the false negative of missing `expr("'unknown'")`.
_COLUMN_READING_CALLS = frozenset({"col", "column", "expr"})


def _expression_reads_a_column(expr: ast.AST) -> bool:
    """True when this expression subtree references a DataFrame column.

    The classification rule (p37 design C8): an expression is constant-null
    when its subtree contains **no** column reference at all. So
    ``coalesce(lit(None).cast('string'), lit('unknown'))`` reads nothing and
    is constant; ``when(col("x").isNull(), lit(None)).otherwise(col("x"))``
    reads ``x`` and is NOT — it is a legitimate null-handling expression and
    flagging it would make the check cry wolf on correct code.

    A ``df["field"]`` subscript counts as a column read for the same
    false-positive reason ``expr`` does.
    """
    for node in ast.walk(expr):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
            if name in _COLUMN_READING_CALLS:
                return True
        elif isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            # df["mobile"] / row["mobile"] — a column read with no col() call.
            return True
    return False


def _string_literal(node: ast.AST) -> str | None:
    """Return the value of a string-literal AST node, else None."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _constant_null_columns(transform_block: str) -> set[str]:
    """Return the column names the transform block assigns a constant.

    Recognises the two shapes that name a target column in generated PySpark:

    - ``<frame>.withColumn("<name>", <expr>)`` — classify ``<expr>``.
    - ``<expr>.alias("<name>")`` — classify the receiver ``<expr>``.

    A column is reported only when it is assigned at least once and **none**
    of its assignments read a column. A column written twice, once from a
    source and once from a constant, is therefore not reported: this check
    feeds a prompt and a record an approver reads, so it is deliberately
    biased against false positives.

    Raises ``SyntaxError`` for an unparseable block; the caller skips on it,
    the same arm ``validate_pyspark_antipatterns`` uses
    (``transform_generation.py:240-241``).
    """
    tree = ast.parse(transform_block)

    reads_by_column: dict[str, list[bool]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        attr = node.func.attr
        if attr == "withColumn" and len(node.args) >= 2:
            name = _string_literal(node.args[0])
            if name is not None:
                reads_by_column.setdefault(name, []).append(_expression_reads_a_column(node.args[1]))
        elif attr == "alias" and len(node.args) == 1:
            name = _string_literal(node.args[0])
            if name is not None:
                reads_by_column.setdefault(name, []).append(_expression_reads_a_column(node.func.value))

    return {name for name, reads in reads_by_column.items() if reads and not any(reads)}


def _dqdl_column_rules(dqdl_rules: str | None) -> list[tuple[str, str]]:
    """Return ``[(rule_kind, field_name)]`` for the column rules in a ruleset.

    Same bracket-scan shape ``shared/quality_verdict.py::count_dqdl_rules``
    uses (``:108-131``) rather than a second parser: find the outer brackets,
    split the body on commas, read each entry. A field name that reached a
    rule has passed ``validate_identifier``, so it carries no comma and no
    quote. Anything that is not the emitted shape yields no rules rather than
    raising — this is a reporting annotation, never a reason to fail a step.
    """
    if not dqdl_rules:
        return []
    open_bracket = dqdl_rules.find("[")
    close_bracket = dqdl_rules.rfind("]")
    if open_bracket == -1 or close_bracket <= open_bracket:
        return []
    body = dqdl_rules[open_bracket + 1 : close_bracket]
    found: list[tuple[str, str]] = []
    for entry in body.split(","):
        match = _DQDL_COLUMN_RULE_ENTRY.search(entry.strip())
        if match:
            found.append((match.group(1), match.group(2)))
    return found


def detect_rule_contradictions(transform_block: str, dqdl_rules: str | None) -> list[dict[str, str]]:
    """Findings for DQDL rules the transform block makes unsatisfiable (R2.3).

    Returns ``[{column, rule, reason}]`` — empty when there is nothing to
    check, when the block does not parse, or when no rule names a
    constant-null column. NEVER raises for the checked code's sake: a
    contradiction is recorded (R2.4) and fed into one regeneration (R2.5),
    and the requirements deliberately say record and feed back, not block.
    """
    column_rules = _dqdl_column_rules(dqdl_rules)
    if not column_rules or not transform_block:
        return []

    try:
        constant_null = _constant_null_columns(transform_block)
    except SyntaxError:
        # Same skip as validate_pyspark_antipatterns:240 — an unparseable
        # transform block is reported by validate_generated_code, which is
        # the gate that owns that failure. This check stays silent about it
        # rather than raising a second, differently-classified error
        # (do-NOT 9, LGR-78ce0b7-13, template / ratified).
        logger.info("Contradiction check skipped: transform block is not parseable Python")
        return []

    if not constant_null:
        return []

    findings: list[dict[str, str]] = []
    for rule_kind, field in column_rules:
        if field not in constant_null:
            continue
        findings.append(
            {
                "column": field,
                "rule": f'{rule_kind} "{field}"',
                "reason": (
                    f'The transform assigns "{field}" an expression that reads no source column, so every '
                    f'row carries a constant null, and the generated rule {rule_kind} "{field}" can never '
                    f'pass. Read "{field}" from a source column, or leave the column out of the transform.'
                ),
            }
        )
    return findings


# ── p49 R1.1/R1.2 — the date-format check (design C1) ─────────────────
#
# The observed defect: the transform emitted
# `to_date(col("signup_date"), "yyyy-MM-dd")` for MM/dd/yyyy data, Spark
# returned NULL for all five values, the Completeness rule failed and every
# row was quarantined. The run reported SUCCEEDED, correctly
# (`LGR-9e76249-1`, template / ratified), so nothing upstream of the DQ rule
# saw it.
#
# The sibling check above does NOT catch this and cannot: it classifies an
# expression as constant-null when its subtree reads NO column
# (`LGR-9e76249-5`), and `to_date(col("signup_date"), …)` reads one. That
# expression is *runtime*-null, a different property of the same node.
#
# Same three constraints as the sibling, for the same recorded reasons:
#
# - **AST, not substring matching.** `LGR-9e76249-5` (template / ratified)
#   recorded substring matching failing in BOTH directions on real inputs
#   from a live run.
# - **Never blocks.** `LGR-9e76249-4` (template / ratified) rejected making
#   the sibling blocking: "a false gate on code generation is worse than a
#   missed finding." This function can only ever return findings or none.
# - **Biased against false positives.** A finding is recorded where approvers
#   read and feeds a regeneration prompt, so every case this checker cannot
#   settle with certainty is skipped rather than guessed at. That bias is why
#   only DIRECT column reads are considered (see `_direct_column_read`) and
#   why an unrecognised pattern letter aborts the whole translation.
#
# Samples are NOT available here by default — `generate_code` receives
# `pipeline_metadata` + `mapping_result`, and all five `schema_extract`
# extractors emit `{"type": …}` with the values discarded. They arrive
# through the injected `sample_reader`, which `lambda_function.py` binds to
# its own `s3_client` and to the `json_s3_uri` already sitting on the
# `schema_extract` row it already reads (p49 task 1.1). `schema_extract` was
# NOT changed to store values: `LGR-0739ab8-1` (template / ratified)
# measures that Lambda at 247.86 MiB against a 250 MiB limit (do-NOT 9).

# The two Spark date-parsing functions that take an explicit format. Both are
# imported by every scaffold. Only `to_date` has an observed failure behind
# it; `to_timestamp` is covered by analogy (design O4).
_DATE_PARSE_CALLS = frozenset({"to_date", "to_timestamp"})

# Spark/Java datetime pattern letters → `strptime` directives.
#
# Deliberately PARTIAL. Any letter absent from this table aborts the
# translation and the call is skipped with no finding (design §Error
# Handling, "Format string unparseable by the checker: an unknown pattern is
# not evidence of a mismatch"). Timezone letters (`X`, `Z`, `z`, `V`, `O`),
# era `G`, quarter `Q`, week `w`, and the standalone forms `L`/`c`/`e` are
# therefore all absent ON PURPOSE — guessing at them risks a FALSE finding,
# which for a non-blocking heuristic recorded where approvers read is the
# expensive direction. Do not extend this table without a value to test
# against.
_SPARK_PATTERN_TOKENS: dict[str, str] = {
    "yyyy": "%Y",
    "yy": "%y",
    "y": "%Y",
    "MMMM": "%B",
    "MMM": "%b",
    "MM": "%m",
    "M": "%m",
    "dd": "%d",
    "d": "%d",
    "DDD": "%j",
    "EEEE": "%A",
    "EEE": "%a",
    "HH": "%H",
    "H": "%H",
    "hh": "%I",
    "h": "%I",
    "mm": "%M",
    "m": "%M",
    "ss": "%S",
    "s": "%S",
    "SSSSSS": "%f",
    "SSS": "%f",
    "a": "%p",
}

# Longest-token-first, so at a position starting "MMM" the scan cannot match
# the shorter "MM" and leave a stray "M" behind. Derived rather than written
# out so the two cannot drift.
_SPARK_PATTERN_SCAN: tuple[tuple[str, str], ...] = tuple(
    sorted(_SPARK_PATTERN_TOKENS.items(), key=lambda item: len(item[0]), reverse=True)
)

# A finding carries sample values, which are USER DATA travelling into a log
# line and (task 2.2) a state row an approver reads. Bounded for the reason
# `_bound_stderr` bounds stderr one Lambda over and `_MAX_NAMES_IN_FAILURE`
# bounds the declared-column list above — a diagnostic must never be the
# thing that makes a write fail (cf. `LGR-b2f79ef-12`).
_MAX_SAMPLE_VALUES_IN_FINDING = 3
_MAX_SAMPLE_VALUE_CHARS = 40

# Day and month pattern letters, longest-first so a month NAME (`MMM`/`MMMM`)
# is matched whole and never read as `MM` plus a stray `M`. Used only by
# :func:`_day_month_swapped`: the p49 C3 ambiguity family is day/month ORDER
# and deliberately nothing else.
_DAY_MONTH_TOKEN = re.compile(r"MMMM|MMM|MM|M|dd|d")


def _strptime_format(spark_format: str) -> str | None:
    """Translate a Spark datetime pattern to a ``strptime`` format, else None.

    ``None`` means "this checker does not understand the pattern", which the
    caller treats as no finding. Returning ``None`` is always safe; returning
    a wrong translation is not, because it would make correct code look like
    a mismatch.

    Handles quoted literals (``yyyy-MM-dd'T'HH:mm:ss`` → ``%Y-%m-%dT%H:%M:%S``,
    and ``''`` → a literal quote), passes non-alphabetic separators through
    unchanged, and aborts on:

    - any ``%``, which would collide with the directives being built;
    - a bare digit, which no emitted pattern carries and which would silently
      become a literal that never matches;
    - an unterminated quote;
    - any alphabetic character not in :data:`_SPARK_PATTERN_TOKENS`;
    - a translation carrying no year directive — see the comment at that
      check for the forward-compatibility reason.
    """
    if not isinstance(spark_format, str) or not spark_format.strip():
        return None
    if "%" in spark_format:
        return None

    out: list[str] = []
    index = 0
    length = len(spark_format)
    while index < length:
        char = spark_format[index]
        if char == "'":
            if index + 1 < length and spark_format[index + 1] == "'":
                out.append("'")
                index += 2
                continue
            closing = spark_format.find("'", index + 1)
            if closing == -1:
                return None
            out.append(spark_format[index + 1 : closing])
            index = closing + 1
            continue
        if char.isalpha():
            for token, directive in _SPARK_PATTERN_SCAN:
                if spark_format.startswith(token, index):
                    out.append(directive)
                    index += len(token)
                    break
            else:
                return None
            continue
        if char.isdigit():
            return None
        out.append(char)
        index += 1

    translated = "".join(out)
    if not translated:
        return None
    # A pattern with no year is refused, and this one is about forward
    # compatibility rather than ambiguity. CPython deprecated parsing a
    # day-of-month without a year (cpython#70647): today `strptime` warns and
    # parses, but from 3.15 it may RAISE — and `_value_parses` catches that as
    # "did not parse", which would turn a correct yearless format into zero
    # parses and a FALSE finding. Refusing is always safe; the caller records
    # nothing. Time-only patterns (`HH:mm:ss`) are refused by the same rule,
    # deliberately: this check exists for date layouts.
    if "%Y" not in translated and "%y" not in translated:
        return None
    return translated


def _parsed_datetime(value: str, strptime_format: str) -> datetime | None:
    """``value`` parsed under ``strptime_format``, or ``None``. Never raises.

    The single place the parse is attempted, so the exception clause that
    makes it total lives once. :func:`_value_parses` asks only *whether* it
    parsed; :func:`_format_ambiguity` (p49 C3) needs the resulting datetime,
    because two formats that both parse a value are only ambiguous when they
    disagree about what date it is.

    ``re.error`` is caught alongside the two obvious classes, and not as a
    hypothetical. ``strptime`` compiles the format into a regex carrying one
    NAMED group per directive, so a format that repeats a directive raises
    rather than returning ``None``: ``y:y``, which :func:`_strptime_format`
    translates happily to ``%Y:%Y``, gives
    ``re.PatternError("redefinition of group name 'Y'")``. That class derives
    from ``Exception``, **not** ``ValueError``, so it escaped this clause and
    propagated out of ``generate_code`` — a heuristic annotation failing the
    step, which is what ``LGR-9e76249-4`` (**template / ratified**) and p49 CP2
    forbid. Found by ``test_the_pattern_translation_is_total``'s generator
    rather than by review, and pinned as an example by
    ``test_a_format_repeating_a_directive_does_not_raise``.

    Absorbed here rather than refused in the translation, because "did not
    parse" is the honest answer for a format no parser can compile, and
    refusing it there would need the translator to reason about directive
    uniqueness for no gain.
    """
    try:
        return datetime.strptime(value, strptime_format)
    except (ValueError, TypeError, re.error):
        return None


def _value_parses(value: str, strptime_format: str) -> bool:
    """True when ``value`` parses under ``strptime_format``.

    ``strptime`` is stricter than Spark's LEGACY parser in one direction — it
    rejects trailing content a lenient Spark parse would absorb. That is a
    known false-positive source, and it is why a finding needs **zero**
    parses across the whole sample rather than one failure: a column whose
    values genuinely carry the emitted layout will parse at least one of its
    ten sampled values.

    Totality — including the ``re.error`` case — is :func:`_parsed_datetime`'s,
    which is where the reason for each caught class is written down.
    """
    return _parsed_datetime(value, strptime_format) is not None


def _day_month_swapped(spark_format: str) -> str | None:
    """``spark_format`` with its day and month letters exchanged, else ``None``.

    ``MM/dd/yyyy`` → ``dd/MM/yyyy``; ``yyyy-MM-dd`` → ``yyyy-dd-MM``. The
    result is a real Spark pattern, not a ``strptime`` one, because it is named
    to a human and to the regeneration model — both of whom write Spark. Its
    separators and letter widths are the emitted pattern's own, so the
    alternative reads as the same layout with two components read the other way
    round rather than as a differently-formatted guess.

    **Day/month order is the ONLY ambiguity family this checker considers**, and
    the narrowness is the point. It is the family R5.1 names (``05/12/2024`` is
    valid as both) and the one a wider candidate set would drown: an
    alternative no source is ever in produces a finding an approver learns to
    dismiss, which is design O3's approval-fatigue risk with none of the safety.

    ``None`` — no alternative, so no ambiguity — for:

    - a pattern carrying a quoted literal. A literal can contain ``M`` or ``d``
      (``yyyy'day'MM``) and a textual exchange would corrupt it. Refusing costs
      nothing: a quoted literal is a timestamp separator (``'T'``), not part of
      a day/month ordering question.
    - anything other than exactly one day letter and exactly one month letter.
      Two of either is not an ordering ambiguity, and zero of either has no
      second reading.
    - a month NAME (``MMM``/``MMMM``). ``15 Jan 2024`` cannot be read the other
      way round, so there is nothing to settle.
    """
    if not isinstance(spark_format, str) or "'" in spark_format:
        return None
    matches = list(_DAY_MONTH_TOKEN.finditer(spark_format))
    months = [match for match in matches if match.group().startswith("M")]
    days = [match for match in matches if match.group().startswith("d")]
    if len(months) != 1 or len(days) != 1:
        return None
    if months[0].group() in ("MMM", "MMMM"):
        return None
    first, second = sorted((months[0], days[0]), key=lambda match: match.start())
    return (
        spark_format[: first.start()]
        + second.group()
        + spark_format[first.end() : second.start()]
        + first.group()
        + spark_format[second.end() :]
    )


def _swapped_day_month_directives(strptime_format: str) -> str | None:
    """``strptime_format`` with its ``%m`` and ``%d`` exchanged, else ``None``.

    The independent second opinion :func:`_format_ambiguity` checks
    :func:`_day_month_swapped` against. Both compute the same exchange, one on
    the Spark pattern and one on the translation, and an ambiguity is only
    reported when they agree — so a token-table asymmetry, or an exchange that
    landed on text rather than on a directive, makes the check inert instead of
    making it wrong.

    ``None`` unless each directive appears exactly once, which is the same
    precondition :func:`_day_month_swapped` enforces one representation up.
    """
    if strptime_format.count("%m") != 1 or strptime_format.count("%d") != 1:
        return None
    low, high = sorted((strptime_format.index("%m"), strptime_format.index("%d")))
    return (
        strptime_format[:low]
        + strptime_format[high : high + 2]
        + strptime_format[low + 2 : high]
        + strptime_format[low : low + 2]
        + strptime_format[high + 2 :]
    )


def _direct_column_read(node: ast.AST) -> str | None:
    """The column name when ``node`` reads one column DIRECTLY, else None.

    Accepts exactly ``col("x")``, ``column("x")`` and ``<frame>["x"]``.

    Anything wrapped is deliberately refused, and this is the load-bearing
    half of the false-positive bias. ``to_date(regexp_replace(col("d"), "/",
    "-"), "yyyy-MM-dd")`` is CORRECT code over MM/dd/yyyy data — the wrapper
    rewrites the layout before the parse — so testing the raw sampled values
    against the emitted format would report a mismatch that does not exist.
    The checker cannot know which wrappers preserve the layout, so it checks
    none of them. Same reason ``expr`` counts as a column read for the
    sibling check: the argument is a string this module does not parse.
    """
    if isinstance(node, ast.Call):
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name in ("col", "column") and len(node.args) == 1:
            return _string_literal(node.args[0])
        return None
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
        return _string_literal(node.slice)
    return None


def _date_format_calls(transform_block: str) -> list[tuple[str, str, str]]:
    """``[(function, source_column, spark_format)]``, de-duplicated in order.

    One entry per distinct ``(source_column, spark_format)`` pair: the same
    column parsed the same way twice is one finding, not two.

    Raises ``SyntaxError`` for an unparseable block — the same contract
    ``_constant_null_columns`` has, and the caller takes the same skip. Note
    that ``ast.parse`` can also emit a ``SyntaxWarning`` WITHOUT raising (an
    invalid escape sequence in a string literal, which AI-generated regex
    arguments do produce). A warning does not raise, so the walk below is
    unaffected; do not widen the caller's ``except`` on account of it.
    """
    tree = ast.parse(transform_block)

    found: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or len(node.args) < 2:
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name not in _DATE_PARSE_CALLS:
            continue
        source_column = _direct_column_read(node.args[0])
        spark_format = _string_literal(node.args[1])
        if source_column is None or spark_format is None:
            continue
        key = (source_column, spark_format)
        if key in seen:
            continue
        seen.add(key)
        found.append((name, source_column, spark_format))
    return found


def _rendered_samples(values: list[str]) -> list[str]:
    """``values`` bounded for a finding — count and per-value length."""
    return [
        value if len(value) <= _MAX_SAMPLE_VALUE_CHARS else value[:_MAX_SAMPLE_VALUE_CHARS] + "…"
        for value in values[:_MAX_SAMPLE_VALUES_IN_FINDING]
    ]


def _rendered_datetime(parsed: datetime) -> str:
    """A parsed value for a human: the date alone unless a time is carried."""
    return parsed.date().isoformat() if parsed.time() == datetime.min.time() else parsed.isoformat(sep=" ")


def _format_ambiguity(
    function_name: str,
    source_column: str,
    spark_format: str,
    strptime_format: str,
    values: list[str],
) -> dict[str, str] | None:
    """The p49 C3 finding: the samples do not settle the emitted format (R5.1).

    Returns ``{column, function, assumed_format, alternative_format, samples,
    reason}`` or ``None``. **Never raises** (CP2). Called only from the branch
    where the emitted format was NOT refuted, because that is precisely where
    "not refuted" has been mistaken for "settled": ``05/12/2024`` parses under
    MM/dd/yyyy and dd/MM/yyyy alike, and picking one silently is forbidden
    (do-NOT 8, R5.1).

    **The key is ``assumed_format``, not ``format``.** The sibling mismatch
    finding calls the emitted pattern ``format`` because there it is the thing
    being refuted; here it is a reading the data does not confirm, and a
    consumer skimming keys must not be able to read it as settled. R5.2 is why
    that matters: a wrong date passes every rule in place, whereas the observed
    null was caught by a Completeness rule.

    Three conditions, all required, and each one is what keeps this from crying
    wolf:

    1. **An alternative exists and both representations agree it does** —
       :func:`_day_month_swapped` on the Spark pattern and
       :func:`_swapped_day_month_directives` on its translation.
    2. **Every sampled value parses under BOTH.** One value the alternative
       cannot read is the data refuting it, and a refuted alternative is not an
       ambiguity — which is exactly why ``01/15/2024`` in the observed run's
       sample would have settled the format if the emitted one had been
       MM/dd/yyyy. A value the *emitted* format cannot read is also declined
       here: the emitted format is then partly refuted, which is a different
       (and messier) condition than an ambiguity, and the sibling mismatch arm
       already owns the fully-refuted case.
    3. **At least one value resolves to a DIFFERENT date under the two.** An
       ambiguity that cannot change any loaded value is not worth an approver's
       attention — ``2024-01-01`` reads the same either way round. This is the
       one condition O3's fatigue concern is answered by rather than argued
       with.
    """
    alternative_spark = _day_month_swapped(spark_format)
    if alternative_spark is None:
        return None
    alternative_strptime = _strptime_format(alternative_spark)
    if alternative_strptime is None or alternative_strptime != _swapped_day_month_directives(strptime_format):
        return None

    diverging: list[str] = []
    example: tuple[str, str, str] | None = None
    for value in values:
        assumed = _parsed_datetime(value, strptime_format)
        alternative = _parsed_datetime(value, alternative_strptime)
        if assumed is None or alternative is None:
            return None
        if assumed != alternative:
            diverging.append(value)
            if example is None:
                example = (value, _rendered_datetime(assumed), _rendered_datetime(alternative))
    if example is None:
        return None

    rendered = _rendered_samples(diverging)
    value, as_assumed, as_alternative = example
    return {
        "column": source_column,
        "function": function_name,
        "assumed_format": spark_format,
        "alternative_format": alternative_spark,
        "samples": ", ".join(rendered),
        "reason": (
            f'The transform parses "{source_column}" with '
            f'{function_name}(col("{source_column}"), "{spark_format}"), and the sampled source '
            f'values are equally consistent with "{alternative_spark}" — {len(diverging)} of the '
            f"{len(values)} sampled value(s) resolve to a DIFFERENT date under the two. For example "
            f'{value!r} is {as_assumed} read as "{spark_format}" and {as_alternative} read as '
            f'"{alternative_spark}". The samples cannot settle which layout the source uses, so '
            f'"{spark_format}" is an ASSUMPTION here and not a format derived from the data. A '
            f"wrong date passes every data-quality rule in place — unlike a null, which the "
            f"Completeness rule catches — so confirm the source layout with whoever owns the data. "
            f"Recorded, not blocking: no format is asserted as settled and nothing was picked on "
            f"the checker's behalf."
        ),
    }


def _sampled_source_values(sample_reader: Callable[[], dict[str, list[str]]] | None) -> dict[str, list[str]]:
    """The injected reader's result, or ``{}``. Never raises (p49 CP2).

    The reader ``lambda_function`` injects already returns ``{}`` rather than
    raising on any failure — the shape ``read_sample_data:135-137`` uses. This
    wrapper does not trust that, because the guarantee that matters is
    ``generate_code``'s: a telemetry read must never turn a generation that
    would have succeeded into a failure. An ``AccessDenied``, a ``NoSuchKey``,
    throttling or a decode error all land here, get logged, and leave the
    check inert (design §Error Handling, final row).
    """
    if sample_reader is None:
        return {}
    try:
        samples = sample_reader()
    except Exception as exc:
        logger.warning(
            f"Source sample read failed; the date-format check is inert for this generation: "
            f"{type(exc).__name__}: {exc}"
        )
        return {}
    if not isinstance(samples, dict):
        return {}
    return samples


def detect_date_format_mismatches(
    transform_block: str,
    sample_reader: Callable[[], dict[str, list[str]]] | None,
    ambiguities: list[dict[str, str]] | None = None,
) -> list[dict[str, str]]:
    """Findings for date formats that parse NONE of their column's samples (R1.1).

    Returns ``[{column, function, format, samples, reason}]`` — empty when
    there is nothing to check. **NEVER raises** (p49 CP2, `LGR-9e76249-4`):
    every unsettleable input is inert rather than an error, because this is a
    heuristic annotation on AI output and a false gate on code generation is
    worse than a missed finding.

    ``sample_reader`` is the injected zero-argument read (p49 C1, do-NOT 10),
    taken as a callable rather than a dict so the AST scan runs FIRST and the
    read happens only when the transform block actually emits a formatted date
    parse. A pipeline with no date handling therefore spends no S3 request at
    all, and the read cost is paid exactly where the finding is possible.

    ``ambiguities`` (p49 R5.1, design C3) is an OUT parameter carrying the
    **other** question about the same node: not "is the emitted format
    refuted" but "do the samples settle it". Passing a list opts in;
    :func:`_format_ambiguity` describes what lands in it.

    An out parameter rather than a second return value, and rather than a
    second public function, for three reasons that all point the same way. The
    return type is a list this module's tests and ``generate_code`` assert
    directly, so widening it is a breaking change for a non-blocking
    annotation — the argument ``generate_code``'s own docstring already makes
    for ``contradictions``. A peer function would repeat the AST scan and, worse,
    call ``sample_reader`` a **second** time, doubling the S3 request the
    callable form exists to make conditional. And the two outcomes are computed
    from the same loop over the same calls against the same samples: the
    ambiguity branch is literally the ``continue`` the not-refuted case already
    took, which is where "not refuted" and "settled" stop being the same thing.

    Inert, with no finding of either kind, in every one of these cases:

    - the transform block is empty or not parseable Python — the arm
      ``validate_pyspark_antipatterns:242`` and the sibling check both take.
      An unparseable block is ``validate_generated_code``'s failure to
      report, not this annotation's.
    - no ``to_date``/``to_timestamp`` call reads a column directly with a
      literal format — see :func:`_direct_column_read`. The reader is not
      called at all in this case.
    - the reader is ``None``, raises, or returns nothing — the fetch failed,
      the ``json_s3_uri`` was absent, or the URI was the
      ``jdbc://pipeline/{id}`` sentinel. :func:`_sampled_source_values` owns
      that absorption and its logging.
    - the column the format is applied to is absent from the sample, or
      sampled to zero non-empty values. A column with no observed values is
      no evidence of anything.
    - the format is not one :func:`_strptime_format` understands.
    - at least one sampled value parses. Note the asymmetry with R5.1: this
      says nothing about WHICH format is right, only that the emitted one is
      not refuted. ``05/12/2024`` parses under both MM/dd/yyyy and
      dd/MM/yyyy, and settling that ambiguity is C3's job, not this
      **mismatch** arm's — neither may ever silently pick (do-NOT 8). That
      case now records an entry in ``ambiguities`` instead of nothing, which
      is the whole of C3; it remains no mismatch finding.
    """
    if not transform_block:
        return []

    try:
        calls = _date_format_calls(transform_block)
    except SyntaxError:
        logger.info("Date-format check skipped: transform block is not parseable Python")
        return []

    if not calls:
        return []

    samples = _sampled_source_values(sample_reader)
    if not samples:
        return []

    findings: list[dict[str, str]] = []
    for function_name, source_column, spark_format in calls:
        values = [value for value in samples.get(source_column) or [] if isinstance(value, str) and value.strip()]
        if not values:
            continue
        strptime_format = _strptime_format(spark_format)
        if strptime_format is None:
            logger.info(
                f"Date-format check skipped for {source_column}: "
                f"format {spark_format!r} is not one this checker understands"
            )
            continue
        if any(_value_parses(value, strptime_format) for value in values):
            # NOT REFUTED — which is not the same as settled, and p49 C3 is the
            # difference. This `continue` used to end the story for a format the
            # samples happened to accept, so `05/12/2024` under `yyyy-MM-dd`
            # would have been silently taken as correct on the strength of data
            # that says nothing of the kind. The ambiguity arm runs here, in the
            # branch that already decided not to record a mismatch, so the two
            # outcomes stay mutually exclusive by construction (R5.1).
            if ambiguities is not None:
                ambiguity = _format_ambiguity(function_name, source_column, spark_format, strptime_format, values)
                if ambiguity is not None:
                    ambiguities.append(ambiguity)
            continue

        rendered = _rendered_samples(values)
        findings.append(
            {
                "column": source_column,
                "function": function_name,
                "format": spark_format,
                "samples": ", ".join(rendered),
                "reason": (
                    f'The transform parses "{source_column}" with '
                    f'{function_name}(col("{source_column}"), "{spark_format}"), and NONE of the '
                    f"{len(values)} sampled source value(s) match that format — for example "
                    f"{', '.join(repr(value) for value in rendered)}. Spark returns NULL for a "
                    f"format mismatch rather than raising, so every row would carry a null in this "
                    f"column and its Completeness rule would fail after the Glue run. Use a format "
                    f"that matches the sampled values, and do not assume a layout the samples do "
                    f"not show."
                ),
            }
        )
    return findings


def generate_code(
    pipeline_metadata: dict,
    mapping_result: dict,
    regeneration_feedback: dict | None = None,
    *,
    generate_transform: Callable[..., str],
    quarantine_bucket: str,
    trace_plugin: Any = None,
    contradictions: list[dict[str, str]] | None = None,
    sample_reader: Callable[[], dict[str, list[str]]] | None = None,
    date_format_findings: list[dict[str, str]] | None = None,
    date_format_ambiguities: list[dict[str, str]] | None = None,
    rename_manifest: dict | None = None,
) -> str:
    """Assemble a PySpark script using a Jinja2 template + AI-generated transform block.

    1. Select template by (source_type, target_type) via TEMPLATE_RULES
    2. Generate transform block via AI (regeneration feedback, when present,
       is embedded in the prompt — P28 R13.3)
    3. Render Jinja2 template with transform block + source_config + target_config

    ``generate_transform`` is the AI transform generator (lambda_function
    passes its ``generate_transform_with_ai`` binding so existing patches on
    lambda_function keep intercepting). ``quarantine_bucket`` is the bucket
    the rendered job writes DQ-failing rows to (P28 R11.1). ``trace_plugin``
    is threaded through to the transform generator for observability.

    ``contradictions`` (p37 R2.3, design C8) is an OUT parameter: when a list
    is passed, this function extends it with ``detect_rule_contradictions``'
    findings for the transform block and ruleset it just produced. It is an
    out parameter rather than a second return value because the return type
    is a ``str`` that two callers and roughly a dozen tests assert directly
    (``lambda_function.generate_code``'s shim at ``:213``, and
    ``test_dq_quarantine_render.py`` / ``test_dqdl_rule_inference.py`` /
    ``test_identifier_validation_sinks.py`` / ``test_jdbc_source_target_type_injection.py``
    among others) — widening it would be a breaking change for a
    non-blocking annotation. Omitting the parameter skips the check
    entirely, which is what makes it free for every caller that does not
    want it.

    ``date_format_findings`` (p49 R1.1, design C1) is a SECOND out parameter
    with the same contract, filled by ``detect_date_format_mismatches``. It is
    kept separate from ``contradictions`` rather than merged into it: the two
    findings are different kinds — one says a rule can never pass, the other
    says a parse will null a column — and the record an approver reads names
    them separately (p49 task 2.2 owns that wiring).

    ``date_format_ambiguities`` (p49 R5.1, design C3) is a THIRD, for the same
    reason once more: a format the samples do not settle is a different fact
    again from a format they refute, and the two are mutually exclusive per
    node. It is threaded to the same call — the check computes both from one
    scan and one read — and it carries a different field set
    (``assumed_format``/``alternative_format`` where the mismatch carries
    ``format``), so a merged list would be one no consumer could read without
    discriminating on shape.

    ``sample_reader`` (p49 C1, do-NOT 10) is the INJECTED source-sample read.
    It is a zero-argument callable already bound to both the S3 client and the
    run's source URI, so this module needs no AWS client and no S3-URI
    vocabulary of its own — the same shape ``generate_transform`` and
    ``quarantine_bucket`` already use. It is called at most once per
    invocation, and only when ``date_format_findings`` is passed AND the
    transform block actually emits a formatted date parse, so a generation
    with no date handling spends no S3 request. ``None`` — or a reader that
    returns nothing, or raises — leaves the check inert.

    ``rename_manifest`` (p55 R11.1/R11.2, design W4-D1) is the approved
    Rename_Manifest for this run, INJECTED by the caller for the same reason
    ``generate_transform``, ``quarantine_bucket`` and ``sample_reader`` are: this
    module owns no AWS client and reads nothing (``LGR-1e135d2-2``, template /
    ratified — and ``test_code_assembly_still_owns_no_aws_client`` asserts it).
    The handler is what reads the stored manifest off the mapping record and
    threads it through ``lambda_function.generate_code``'s shim.

    The manifest is used **verbatim or not at all** and is never recomputed here
    (design risk R5.7): it was derived from the source schema whose Schema_Hash
    keys reuse, so a fresh derivation could disagree with what a human approved.

    ``None`` — which is every run until the manifest reaches this Lambda — makes
    the whole feature inert: no projection is emitted, no mapping name is
    translated, and the rendered script is byte-identical to its pre-p55 form.
    """
    target_config = pipeline_metadata.get("target_config", {})
    source_config = pipeline_metadata.get("source_config", {})
    source_type = pipeline_metadata.get("source_type", "S3_UPLOAD")
    raw_type = target_config.get("type", target_config.get("target_type", "")).lower()

    # Normalise legacy target type names
    type_map = {
        "new_s3_table_bucket": "s3_tables",
        "existing_s3_table_bucket": "s3_tables",
        "s3_tables": "s3_tables",
        "glue_catalog": "glue_catalog",
        "jdbc": "jdbc",
        "rds_database": "rds_database",  # P14 fix: ensure RDS targets resolve via TEMPLATE_RULES
        "s3_parquet": "s3_parquet",
    }
    target_type = type_map.get(raw_type, raw_type)

    template_name = _select_template(source_type, target_type)
    if not template_name:
        raise ValueError(
            f"Unknown (source_type, target_type): ({source_type}, {raw_type}) (normalised target: {target_type})"
        )

    # The write mode this render would bake in, checked against what the
    # target's writer implements (`shared/write_modes.py`), before any prompt is
    # built. Create and edit refuse an unsupported pair at the API; this catches
    # a record stored before they did. Until 2026-10-10 a stored `merge` on a
    # JDBC, S3 Parquet or Glue Catalog target rendered append, overwrite and a
    # plain catalog write respectively, and the run reported success. That
    # silent fall-through was deliberate (P19 Decision 4, "the runtime safety
    # net") for JDBC; it is reversed because on S3 Parquet the same fall-through
    # replaced all existing data on every run. Same default as the render below.
    resolved_write_mode = target_config.get("write_mode", "append")
    write_mode_error = write_mode_refusal(
        target_config.get("type", target_config.get("target_type")), resolved_write_mode
    )
    if write_mode_error:
        raise StepFailure(
            "create_code",
            f"{write_mode_error}. Edit the pipeline and choose one of those write modes; nothing was "
            "generated, so no data was written.",
            details={"target_type": raw_type, "write_mode": str(resolved_write_mode)},
        )

    # Build field mappings list for the AI prompt (see _extract_field_mappings
    # for the mapping-record storage variants it resolves).
    field_mappings = _extract_field_mappings(mapping_result)
    logger.info(f"Field mappings for AI transform: {len(field_mappings)} mappings found")

    # p55 R11.1/R11.2 (design W4-D1): the approved Rename_Manifest, read verbatim.
    #
    # `field_mappings` above KEEPS the original source column names and is what
    # `_validate_mapped_source_fields` is given below — `LGR-a419843-2`'s check
    # compares against `schema_extract`'s output, which is original names, so
    # translating them would make that refusal validate against a fiction. The
    # translated copy is what feeds the DQDL rules, the unsourced annotation and
    # the transform prompt, because the projection has already renamed the frame
    # by the time any of that code runs in the job (R11.3).
    #
    # Both lists are empty and every translation is the identity when no manifest
    # is supplied, which is every run until one reaches this Lambda.
    rename_pairs = _rename_pairs(rename_manifest)
    rename_lookup = _rename_lookup(rename_pairs)
    renamed_field_mappings = _renamed_field_mappings(field_mappings, rename_lookup)
    if rename_pairs:
        logger.info(f"Rename_Manifest applies {len(rename_pairs)} column rename(s) after the source read")

    source_schema = pipeline_metadata.get("source_schema", {})
    # p33 task 4.7 / requirement 2.5: the resolved target schema, read here
    # next to `source_schema` and off the same `pipeline_metadata` dict the
    # handler assembles. It is rendered into the transform prompt (unlike
    # `source_schema`, which is accepted and dropped — see
    # `transform_generation.generate_transform_with_ai`'s docstring for why
    # that asymmetry is deliberate rather than an oversight; p33 design R4).
    #
    # `{}` when absent, matching `source_schema` above: the handler omits the
    # key only for a pipeline that has no target schema on record (requirement
    # 2.6), and the template renders no target block for an empty dict, so the
    # prompt is byte-identical to its pre-p33 form for those pipelines.
    target_schema = pipeline_metadata.get("target_schema", {})

    # p37 R3.5 (design C12): every target field no source supplies — the ones the
    # mappings claim without naming a source, AND the ones the record carries in
    # `unmapped_target_fields`. Both lists are resolved off the SAME
    # `mapping_result` by sibling extractors that agree on the three storage
    # variants, so a field is annotated whichever list it happens to live in.
    #
    # Reading only `field_mappings` was the branch B defect: p37 C9's normaliser
    # MOVES a source-less entry into `unmapped_target_fields`, so after C9 the
    # normal placement for an unsourced field was the one this call could not see,
    # and the template declared it mandatory with no `— NO SOURCE` annotation.
    #
    # The Completeness coupling is unaffected: a name reaching this list from
    # `unmapped_target_fields` is not in `field_mappings`, so it never enters
    # `_generate_dqdl_rules` below and carries no Completeness rule — which is
    # what the coupling asserts. Do NOT feed the unmapped list to
    # `_generate_dqdl_rules` to "restore symmetry"; that emits
    # `Completeness >= 0.95` on an all-null column and quarantines the batch
    # (p37 R2.1/R2.2, observed on run run-2026-08-09-15-38-19-5c5f5f8a).
    #
    # It is threaded THROUGH `generate_transform` (lambda_function's shim), not
    # around it: the shim is the only seam the tests patch, so shortcutting past
    # it would look harmless and would not be. Same reason `target_schema` is
    # threaded that way (LGR-b2f79ef-10, template / ratified).
    #
    # p55 R11.3: computed from the RENAMED mappings, and the resulting names are
    # translated too. Both halves are needed to keep the one-predicate coupling
    # this function's docstring describes: the annotated set must stay equal to
    # the Completeness-skipped set, and `_generate_dqdl_rules` below is now given
    # the renamed mappings. `_renamed_names` is safe to apply on top of names that
    # already came from renamed mappings because the lookup is idempotent — a
    # target satisfies the Identifier_Allowlist and every original fails it, so a
    # target is never itself a key.
    unsourced_target_fields = _renamed_names(
        _unsourced_target_field_names(renamed_field_mappings, _extract_unmapped_target_fields(mapping_result)),
        rename_lookup,
    )
    if unsourced_target_fields:
        logger.info(f"Target fields with no source, annotated in the transform prompt: {unsourced_target_fields}")

    # ------------------------------------------------------------------
    # Everything that can refuse this mapping runs HERE, before a prompt is
    # built or a Bedrock call is spent, so no code is generated from an input
    # already known to be bad.
    #
    # THE ORDER OF THESE THREE IS LOAD-BEARING and preserves the pre-existing
    # precedence exactly (p37 R2.7): the two identifier sinks come first, so an
    # input that trips one still fails with its IdentifierValidationError naming
    # the identifier, and the new source-field check is last. The two hoisted
    # results are assigned into `template_vars` at their original sites below —
    # only the point of EVALUATION moved, and only forward of the AI call.
    # ------------------------------------------------------------------
    #
    # P28 R12.1: merge_key is f-string'd into the MERGE INTO statement by the
    # rendered job — validate so a hostile pipeline config fails code generation
    # naming the identifier (R12.2). The rendered job re-checks the runtime
    # --merge_key argument at the SQL sink itself.
    validated_merge_key = validate_identifier(
        target_config.get("merge_key", "id"), "MERGE statement merge_key (target_config.merge_key)"
    )

    # DQDL rules from field mappings for inline data quality evaluation.
    # Uniqueness inference is word-boundary + merge_key schema hint (P28 R11.2)
    # — see _generate_dqdl_rules/_uniqueness_hint_fields. This is also P28
    # R12.1's per-field injection sink, which is why it precedes the check below.
    #
    # p55 R11.3: the RENAMED mappings, so a rule field is the approved name the
    # projection has already put on the frame. Nothing here is relaxed — the
    # per-field `validate_identifier` call is untouched, and a target name that
    # fails the allowlist still fails code generation naming the identifier
    # (R11.4, do-NOTs 1–3).
    dqdl_rules = _generate_dqdl_rules(renamed_field_mappings, unique_hint_fields=_uniqueness_hint_fields(target_config))

    # p55 R11.1/R11.2: the projection itself, and this module's THIRD identifier
    # sink. It sits here — after the two pre-existing sinks and BEFORE the
    # source-field check — so the documented precedence is preserved exactly: an
    # input that trips merge_key or a DQDL rule field still fails with that
    # sink's IdentifierValidationError, and the undeclared-source-field refusal
    # stays last. Do not move it ahead of the two above or behind the one below.
    rename_projection_block = _rename_projection_block(rename_pairs)

    # Two configured values the templates emit as Python source, refused here
    # with the other inputs rather than at the render. The partition columns
    # are this module's fourth identifier sink, placed after the three above so
    # their precedence is unchanged; see `_partition_columns_argument` and
    # `_checkpoint_bucket`. The checkpoint pair arrives through the
    # source/target merge below, so it is read from the same merge here.
    partition_columns_argument = _partition_columns_argument(target_config.get("partition_columns"))
    checkpoint_bucket = _checkpoint_bucket({**source_config, **target_config})

    # A mapping naming a source column `source_schema` does not declare fails
    # loudly here rather than producing a transform that reads a column the test
    # frame cannot contain. Inert when the declared set is empty — see
    # `_validate_mapped_source_fields` for why that is required, not lenient.
    _validate_mapped_source_fields(field_mappings, source_schema)

    # Generate the transform block via AI (reviewer feedback embedded on
    # regeneration — P28 R13.3)
    # p55 R11.2: the RENAMED mappings. The projection runs before the transform
    # in the emitted job, so the frame the transform reads carries approved
    # names — a prompt naming the originals would produce a transform reading
    # columns that no longer exist. `source_schema` is still the ORIGINAL
    # extracted schema and is still accepted-and-dropped by the generator (p33
    # design R4, `LGR-1e135d2-8`); nothing about that asymmetry changes here.
    transform_block = generate_transform(
        mappings=renamed_field_mappings,
        source_schema=source_schema,
        target_schema=target_schema,
        regeneration_feedback=regeneration_feedback,
        unsourced_target_fields=unsourced_target_fields,
        trace_plugin=trace_plugin,
    )

    # Threat model T-01: the block is model output whose prompt carries
    # operator- and uploader-controlled text, and it is rendered verbatim into
    # a job that runs with the pipeline's Glue role. Enforce the shape the
    # prompt asks for BEFORE the render, so an injected import, `spark._jvm`
    # reach or `df.write` never reaches a stored script. This is the only
    # `generate_transform` call and this function holds the only render, so
    # the in-process regeneration in the handler passes through here too.
    # Raises UnsafeTransformBlockError (a ValueError); the handler wraps it as
    # StepFailure("create_code", ...). See transform_safety's docstring for
    # why it fails the step rather than feeding the regeneration.
    validate_transform_block_safety(transform_block)

    # Determine source format (default json)
    source_format = target_config.get("source_format", "json")

    # p55 R4.1: the Source_Delimiter the four templates' csv read passes to
    # `.option("sep", …)`. Derived HERE, beside `source_format`, and assigned
    # into `template_vars` below beside it — that is what makes "no render path
    # omits it" structural rather than a promise: this function holds the ONLY
    # `template.render()` call in the Lambda (at the foot of this function; it was
    # cited as `:1541` before p55 task 6.5 inserted the rename projection above,
    # which is why the line number is no longer quoted here), so a key set here is
    # a key every render has.
    #
    # `source_object_uri` is the URI of the object the Glue job will READ — the
    # handler takes it off the `schema_extract` row's `readable_source_uri`,
    # falling back to `json_s3_uri` for a pre-p55 row (p55 W3, task 4.3). It is
    # the same object the Glue job's `--source_path` points at, so the extension
    # read here is the one the job will actually read: the original upload for
    # every Native_Format_Set member, and the converted `.csv` for an
    # Excel_Workbook. It is not the bounded Sample_Artifact.
    # `delimiter_for_key` is total and defaults to a comma (R4.6), so an absent
    # URI, a JDBC sentinel, or a folder prefix with no extension all render
    # exactly the comma this line hardcoded before p55.
    source_delimiter = delimiter_for_key(pipeline_metadata.get("source_object_uri") or "")

    # Build template context: merge source_config BEFORE target_config so the
    # JDBC source template can read source-side keys (engine, jdbc_url, etc.)
    # while still letting target_config win on key collisions (preserves the
    # P14 behaviour where target keys are authoritative for the writer).
    template_vars: dict = {}
    template_vars.update(source_config)
    template_vars.update(target_config)
    # `engine` must come from source_config, and the merge above takes it from
    # target_config. Re-assert it, the same way `write_mode` below is re-asserted
    # from target_config — after a wholesale merge of two dicts that share key
    # names, the only reliable binding is an explicit one.
    #
    # `engine` is the ONLY key read at Jinja2 RENDER time that exists in both
    # configs. The other four collisions between a JDBC source_config and a JDBC
    # target_config — `jdbc_url`, `jdbc_driver`, `jdbc_table`, `secret_arn` — are
    # read at RUN time out of `args[...]` (Glue job arguments, which
    # `job_arguments.py` builds with explicit source_/target_ prefixes), never as
    # `{{ ... }}`, so the merge order cannot reach them. Verified by grep: zero
    # `{{ }}` uses of any of the four across `pyspark_templates/**`.
    #
    # All three `{{ engine }}` sites are in `glue_job_jdbc_source.py.j2` and all
    # three are source-side reads — `quote_identifier` for the schema and table
    # (`:166-167`) and `build_watermark_predicate` for the incremental bound
    # (`:192`). No target template references `engine` at all, so binding it to
    # the source costs the write path nothing.
    #
    # What this cost when it was wrong: a MySQL source feeding an
    # Aurora-PostgreSQL target rendered
    # `quote_identifier("aurora-postgresql", ...)`, producing
    # `SELECT * FROM "appdb"."customers" WHERE 1=0`. MySQL rejects double-quoted
    # identifiers unless `ANSI_QUOTES` is set, so every run of such a pipeline
    # died with "You have an error in your SQL syntax" — after
    # `create_glue_pipeline` had reported success. The engines have to DIFFER for
    # it to show, which is why a same-engine pipeline (and every fixture whose
    # target_config omits `engine`) looks fine.
    if source_config.get("engine"):
        template_vars["engine"] = source_config["engine"]
    # The JDBC source template reads `initial_watermark`, and the record stores
    # the seed under `watermark_initial_value` (what the wizard writes). The
    # merge above therefore never bound it, so every first incremental run read
    # the whole table whatever seed the user entered. Bound explicitly, from the
    # resolver the API validates with, so the validated value is the rendered
    # one. `""` means "no seed": the template then reads the whole table on the
    # first run, as documented.
    initial_watermark = initial_watermark_entry(source_config)
    template_vars["initial_watermark"] = initial_watermark[1] if initial_watermark else ""
    template_vars["transform_block"] = transform_block
    template_vars["source_format"] = source_format
    # p55 R4.1 — set unconditionally, beside `source_format`. The templates
    # reference it only inside their `{% if source_format == 'csv' %}` branch,
    # but `_get_pyspark_jinja_env` uses `StrictUndefined`, so supplying it on
    # every render is what keeps that branch from raising rather than relying on
    # a `| default(…)` filter in four places.
    template_vars["source_delimiter"] = source_delimiter
    # p55 R11.1/R11.2 — set UNCONDITIONALLY, for the reason above: this function
    # holds the only `template.render()` call in the Lambda, so a key set here is
    # a key every render has, and a conditional assignment under
    # `StrictUndefined` would raise rather than emit nothing. `""` when no column
    # is renamed, which the templates' `is defined and` guard renders as nothing.
    template_vars["column_rename_block"] = rename_projection_block
    # Validated and quoted above, before the AI call. Bound after the merge so
    # the stored value can never reach the template directly.
    template_vars["partition_columns"] = partition_columns_argument
    template_vars["checkpoint_bucket"] = checkpoint_bucket
    template_vars["write_mode"] = target_config.get("write_mode", "append")
    # Validated above, with the rest of the refusals, before the AI call.
    template_vars["merge_key"] = validated_merge_key
    # Inject the resolved target_type into the render context. The PySpark
    # templates branch on an UPPERCASE `target_type` literal (e.g.
    # `{% if target_type == 'JDBC' %}`) to choose the getResolvedOptions
    # target keys and the write block. The persisted target_config carries
    # the type under the key `type` (not `target_type`), so without this the
    # template's `target_type | default('UNKNOWN')` fell through to
    # 'UNKNOWN' — emitting NO write block and producing a job that reads +
    # transforms but silently writes nothing. Uppercased to match the
    # template literals and the TEMPLATE_RULES-normalised values
    # (jdbc → JDBC, rds_database → RDS_DATABASE, etc.).
    template_vars["target_type"] = target_type.upper()

    # Inject the target-side Secrets Manager ARN (when the persisted
    # target_config carries one — i.e. the target Glue connection is
    # secret-backed). When present, the JDBC write partial fetches the
    # target credentials from Secrets Manager AT RUNTIME instead of reading
    # plaintext `target_jdbc_user`/`target_jdbc_password` Glue job arguments
    # (which are visible in the console, CloudTrail, and job-run history).
    # When absent (inline-credential connections), the template falls back to
    # the plaintext-arg path. Mirrors the source-side credential handling.
    template_vars["target_secret_arn"] = target_config.get("secret_arn") or None

    # Generated above, with the rest of the refusals, before the AI call.
    template_vars["dqdl_rules"] = dqdl_rules

    # p64 R6.6 — set UNCONDITIONALLY, at this function's top level, beside
    # `dqdl_rules` for the same reason `source_delimiter` and `column_rename_block`
    # are: this function holds the only `template.render()` call in the Lambda, so
    # a key set here is a key every render has. Required by R6.5 / do-NOT 6 and
    # asserted mechanically by
    # `tests/unit/templates/test_dq_template_vars_contract.py`, which reads the
    # DQ partial's whole variable surface and fails if any member of it is missing
    # from this block, assigned inside an `if`, or routed through the
    # source_config/target_config splat.
    #
    # NOT read through `| default(...)` in the template. Unlike `dqdl_rules`, this
    # variable is interpolated by a bare `{% if %}` with no `is defined` guard, so
    # an unset value fails the render loudly under StrictUndefined rather than
    # silently dropping the DQ body — which is the correct direction, and the
    # reason the guard is upstream here rather than in the partial.
    template_vars["data_quality_enabled"] = resolve_data_quality_enabled(pipeline_metadata, template_name)

    # p64 R7.1/R7.2 (design C7) — the dedup settings, set UNCONDITIONALLY beside
    # the gate above and for the same reasons. Threaded EXPLICITLY rather than
    # arriving through `template_vars.update(target_config)`: C7 rejects that route
    # by name, because a `target_config` key becomes a template variable with zero
    # backend change and zero validation, and `target_config` is free-form.
    #
    # `resolve_quality_config` returns every recognised key on every call, so
    # 2c.3's template can read `quality_config.dedup_enabled` plainly. Under
    # `StrictUndefined` a jinja2 dict access to an ABSENT key yields an `Undefined`
    # whose `__bool__` raises, so filling the keys upstream is what makes the
    # template readable without `| default(...)` — which do-NOT 6 forbids.
    #
    # This is the LAST identifier sink in the function, deliberately: `merge_key`
    # (above) and `_generate_dqdl_rules`' per-field call run first, so an input that
    # trips either still fails with that sink's own error naming that identifier
    # (the p37 R2.7 ordering rule). Do not hoist this.
    #
    # The subscript is a STRING LITERAL, not `QUALITY_CONFIG_KEY`, and that is not
    # an oversight. Two reasons, and the second is the load-bearing one. (a) These
    # are two different contracts that happen to share a spelling: the constant is
    # the RECORD ATTRIBUTE name, agreed with `dashboard_api`'s writer across a
    # Lambda boundary; this is the RENDER VARIABLE name, agreed with the template.
    # (b) `test_dq_template_vars_contract.py`'s AST reader matches
    # `template_vars["<literal>"]` only — `_template_vars_target` requires an
    # `ast.Constant` slice — so a constant here would make this assignment INVISIBLE
    # to the fence, and 2c.3's template read of `quality_config` would then fail
    # R6.5's check with the variable actually set. Keep it a literal.
    template_vars["quality_config"] = resolve_quality_config(pipeline_metadata)

    # p37 R2.3 (design C8): both halves of the contradiction are in this
    # function's hands and nowhere else — the AI transform block above and
    # the ruleset just built — so the check needs no new read and no second
    # AI call. Only the transform block is parsed, not the rendered script:
    # the templates' own `withColumn(field.name, ...)` casts take a
    # non-literal column name and are not the AI's improvisation.
    if contradictions is not None:
        contradictions.extend(detect_rule_contradictions(transform_block, template_vars["dqdl_rules"]))

    # p49 R1.1/R1.2 (design C1): the emitted date format tested against the
    # source column's real values. Same block, same AST mechanism and the same
    # non-blocking contract as the check above — this one can only ADD a
    # finding, never fail the step (`LGR-9e76249-4`, template / ratified).
    #
    # The samples are NOT in this function's inputs; they arrive through the
    # injected `sample_reader`, which `detect_date_format_mismatches` calls
    # only after finding a formatted date parse to check. Only the transform
    # block is parsed, not the rendered script — for the same reason the check
    # above gives: the templates' own casts are not the AI's improvisation.
    #
    # p49 R5.1 (design C3): `date_format_ambiguities` is filled by the SAME
    # call. One scan, one sample read, two questions about each date parse —
    # is the emitted format refuted, and do the samples settle it. C3 is
    # recorded and NOT gated; see the handler for why that arm was taken.
    if date_format_findings is not None or date_format_ambiguities is not None:
        mismatches = detect_date_format_mismatches(transform_block, sample_reader, date_format_ambiguities)
        if date_format_findings is not None:
            date_format_findings.extend(mismatches)

    # Quarantine bucket for DQ-failing rows (P28 R11.1). The generated-code
    # bucket is used because the Glue job role already has s3:PutObject on
    # adi-generated-code-* (iam_roles.py) and an S3 Tables target bucket is a
    # table bucket that cannot accept loose parquet objects. The rendered job
    # writes failing rows to s3://<bucket>/quarantine/<run_id>/.
    template_vars["quarantine_bucket"] = quarantine_bucket

    # Render the Jinja2 template
    env = _get_pyspark_jinja_env()
    template = env.get_template(template_name)
    final_code: str = template.render(**template_vars).lstrip("\n")

    return final_code
