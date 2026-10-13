# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.4 — provenance for a run that reused a Rename_Manifest (R10.10).

**Functions under test, named by file because names in this repository are not
unique:** ``determine_approval_requirement``, ``store_mapping_result`` and
``lambda_handler`` in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py``.
``test_this_is_the_schema_mapping_module`` pins which module this file bound —
risk R5.8's discriminator, and ``create_approval_payload`` in this same file is
the standing reason it is worth asserting rather than trusting a path.

**What R10.10 requires and why it is not decoration.** ``LGR-c58f276-1``
(template / ratified) measured **438** rows in ``agentic_data_ingestion_audit_log``
carrying ``actor == "unknown"``, **107** of them ``approval_approved``. A reuse
path that records nothing recreates that gap deliberately: a run that skipped a
human gate must still be attributable to the human who made the decision it
inherited. So both identifiers are recorded — the reused manifest's
(``manifest_id``) and the approval's that produced it (``approval_id``) — in the
shape design §Data Models fixes:

    {"rename_manifest_reuse": {"manifest_id": str, "approval_id": str}}

**The plumbing decision this file also pins.** ``determine_approval_requirement``
returns a ``bool`` — that is what ``CheckSchemaMappingApproval`` reads — and 6.3
sited the reuse lookup *inside* it, behind the cheap non-conformity gate. So the
two identifiers exist in a function with nowhere to put them. The chosen route is
a **caller-owned sink**: one defaulted ``reuse_provenance`` parameter that the
reuse branch fills in place, created fresh per invocation in ``lambda_handler``.
Asserted here rather than merely described:

* ``test_the_return_type_is_still_a_bool_on_both_arms`` — the contract task 1.1's
  characterization pins is unchanged. A tuple return was rejected for that reason.
* ``test_the_sink_is_a_parameter_and_not_module_level_state`` — a warm Lambda
  container serves many invocations, so a module-level channel would attribute one
  run's row to a previous run's approval. A **false** attribution is strictly worse
  than a missing one; it is `LGR-c58f276-1`'s gap inverted into a lie.
* ``test_a_conforming_schema_records_no_provenance_and_issues_no_query`` — hoisting
  the lookup into the handler was rejected because keeping this gate would need the
  handler to re-derive non-conformity (a second authority for whether the clause
  fires), and dropping it would put a DynamoDB query on every run.

Nothing is smuggled out by exception: ``LGR-b2f79ef-13`` reasons against exactly
that on this module, and the sink is an ordinary parameter.

**Both records, and they answer different questions.** The state row says what
this run says about itself — one mutable row per run and step, read by the
approval path and the dashboard, and written in the *same* ``put_item`` as the
``approval_required`` the reuse produced, so the two cannot come to disagree. The
audit entry says who is answerable — append-only, one-year TTL, and the table
`LGR-c58f276-1` measured. R10.10's own subject is attributability, so a
state-row-only record would leave the measured surface untouched.

**Validates: Requirements 10.10**
"""

import inspect
import os
import sys
from unittest.mock import MagicMock, patch

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in the sibling files here).
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

# sys.modules.pop + sys.path.insert: `lambda_function` is a name 23 Lambda dirs
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)` (`LGR-b397ee2-2`, `LGR-c58f276-4`), so nothing here
# depends on sys.modules still pointing at this module at call time. No test
# module in this directory imports another; this file is self-contained.
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

#: A source column name that fails the Identifier_Allowlist (a space).
_NON_CONFORMING = "first name"

_PIPELINE = "p55-task-6-4"
_RUN_ID = "run-6-4"
_RUN_HASH = "src-hash-6-4"
_MANIFEST_ID = "mapping-hash-6-4"
_APPROVAL_ID = "approval-6-4"

#: The action name the audit entry carries, so a typo is visible in one place.
_AUDIT_ACTION = "rename_manifest_reuse"


# --- Fixtures -----------------------------------------------------------------


def _high_confidence_mapping(first_name_column: str = _NON_CONFORMING) -> dict:
    """A mapping no OTHER forcing clause fires on.

    Every source field mapped (neither ratio fires), confidence above
    ``CONFIDENCE_AUTO_APPROVE_THRESHOLD``, no fallback provenance, both target
    fields sourced. Without this the `False` results below would prove nothing.

    AMENDED for T-04, for the same reasons and in the same way as the sibling
    fixture in ``test_rename_manifest_reuse.py``: T-04 requires each entry's
    ``source_field`` to be a real source column whose name agrees with the
    target's, or an operator mapping hint. ``first_name_column`` lets a
    CONFORMING schema's mapping read the ``first_name`` column it actually has
    (``_first_name_column``), and ``cust_id → customer_id`` — a semantic match —
    is corroborated by a hint in ``_pipeline_metadata``.
    """
    return {
        "mappings": [
            {"source_field": first_name_column, "target_field": "first_name", "confidence": 0.97},
            {"source_field": "cust_id", "target_field": "customer_id", "confidence": 0.98},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _first_name_column(source_column_names) -> str:
    """The column a model's ``first_name`` entry reads for this source schema (T-04)."""
    if source_column_names is not None and "first_name" in source_column_names:
        return "first_name"
    return _NON_CONFORMING


def _target_schema() -> dict:
    return {
        "type": "object",
        "properties": {"first_name": {"type": "string"}, "customer_id": {"type": "string"}},
        "required": ["customer_id"],
    }


def _source_schema(*column_names: str) -> dict:
    return {"type": "object", "properties": {name: {"type": "string"} for name in column_names}}


def _pipeline_metadata() -> dict:
    # `mapping_hints` AMENDED for T-04 — see `_high_confidence_mapping`.
    return {
        "pipeline_id": _PIPELINE,
        "approval_config": {"schema_mapping_enabled": True, "always_approve_schema_changes": True},
        "mapping_hints": {"cust_id": "customer_id"},
    }


def _reusable() -> dict:
    """What `find_reusable_rename_manifest` returns — both identifiers, no second read."""
    return {
        "manifest_id": _MANIFEST_ID,
        "source_schema_hash": _RUN_HASH,
        "approval_id": _APPROVAL_ID,
        "renames": [{"original_name": _NON_CONFORMING, "target_name": "first_name"}],
    }


def _row(*, manifest: dict | None = None) -> dict:
    """A `schema_mapping` state row shaped like `store_mapping_result` writes one."""
    return {
        "run_id": "run-older",
        "step_name": "schema_mapping",
        "status": "success",
        "timestamp": "2026-08-27T10:00:00+00:00",
        "mapping_id": _MANIFEST_ID,
        "source_schema_hash": _RUN_HASH,
        "pipeline_id": _PIPELINE,
        "approval_id": _APPROVAL_ID,
        "output_data": {
            "approval": {
                "rename_manifest": manifest
                if manifest is not None
                else {
                    "manifest_id": _MANIFEST_ID,
                    "source_schema_hash": _RUN_HASH,
                    "approval_id": None,
                    "renames": [{"original_name": _NON_CONFORMING, "target_name": "first_name"}],
                }
            }
        },
    }


def _decide(
    *,
    source_column_names: list[str] | None,
    reusable: dict | None,
    sink: dict | None,
) -> bool:
    """Call `determine_approval_requirement` with the lookup stubbed."""
    with (
        patch.object(_mod, "get_pipeline_metadata", return_value=_pipeline_metadata()),
        patch.object(_mod, "find_reusable_rename_manifest", return_value=reusable) as lookup,
    ):
        decision = _mod.determine_approval_requirement(
            _PIPELINE,
            _high_confidence_mapping(_first_name_column(source_column_names)),
            None,
            _target_schema(),
            source_column_names=source_column_names,
            run_schema_hash=_RUN_HASH,
            reuse_provenance=sink,
        )
    _decide.last_lookup = lookup  # type: ignore[attr-defined]
    return decision


# --- Which module this is ------------------------------------------------------


def test_this_is_the_schema_mapping_module():
    """Risk R5.8's discriminator, asserted rather than inferred from a filename."""
    assert _mod.__file__.endswith("agentic_data_ingestion_schema_mapping/lambda_function.py"), _mod.__file__
    assert list(inspect.signature(_mod.create_approval_payload).parameters) == [
        "source_schema",
        "target_schema",
        "mapping_result",
        "source_schema_hash",
        "target_schema_hash",
    ], "this is not schema_mapping's module; create_code's create_approval_payload takes 8 parameters"


# --- The plumbing: a caller-owned sink, not a return type and not module state ---


def test_the_sink_is_a_parameter_and_not_module_level_state():
    """Why the provenance travels as a parameter, asserted rather than described.

    A module-level channel would survive a warm Lambda container into the NEXT
    invocation, so a run that reused nothing would inherit the previous run's
    identifiers — a **false** attribution, which is `LGR-c58f276-1`'s
    `actor == "unknown"` gap inverted into a lie rather than merely repeated.
    Two things pin the shape: the parameter exists and is defaulted, and no
    module-level name holds the provenance between calls.
    """
    parameter = inspect.signature(_mod.determine_approval_requirement).parameters["reuse_provenance"]
    assert parameter.default is None, "a mutable default would itself be shared module state"

    first: dict = {}
    _decide(source_column_names=["cust_id", _NON_CONFORMING], reusable=_reusable(), sink=first)
    assert first == {"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID}

    second: dict = {}
    _decide(source_column_names=["cust_id", "first_name"], reusable=None, sink=second)
    assert second == {}, "the second call inherited the first call's provenance"


def test_the_return_type_is_still_a_bool_on_both_arms():
    """The contract task 1.1's characterization pins, unchanged.

    Widening the return to `(bool, provenance)` was the obvious alternative and is
    rejected for exactly this: `CheckSchemaMappingApproval` reads the boolean, and
    1.1 asserts on it directly.
    """
    # AMENDED for T-04: the column list now includes `cust_id`, which the
    # fixture mapping reads. T-04 requires every mapped `source_field` to be a
    # column the source schema declares, so the old `[_NON_CONFORMING]` list
    # would have made `exempted` `True` for a reason unrelated to the sink. The
    # non-conforming column — the one both arms are about — is unchanged.
    forced = _decide(source_column_names=["cust_id", _NON_CONFORMING], reusable=None, sink={})
    exempted = _decide(source_column_names=["cust_id", _NON_CONFORMING], reusable=_reusable(), sink={})

    assert forced is True
    assert exempted is False


def test_the_clause_still_works_when_no_sink_is_supplied():
    """Every pre-6.4 caller passes nothing and must be unaffected.

    Task 1.1's characterization is one of them, and it is not modified by this task.
    """
    with (
        patch.object(_mod, "get_pipeline_metadata", return_value=_pipeline_metadata()),
        patch.object(_mod, "find_reusable_rename_manifest", return_value=_reusable()),
    ):
        decision = _mod.determine_approval_requirement(
            _PIPELINE,
            _high_confidence_mapping(),
            None,
            _target_schema(),
            source_column_names=["cust_id", _NON_CONFORMING],
            run_schema_hash=_RUN_HASH,
        )

    assert decision is False


# --- R10.10: both identifiers, and only where a manifest was actually used -------


def test_a_reused_manifest_records_both_identifiers():
    """R10.10. Both, and in the shape design §Data Models fixes.

    Unconditional on this path rather than defensive: 6.3 made a non-empty
    `approval_id` a client-side ELIGIBILITY condition, so a manifest that cannot
    name its approval is never reusable. If we are here, both identifiers exist.
    """
    sink: dict = {}
    decision = _decide(source_column_names=["cust_id", _NON_CONFORMING], reusable=_reusable(), sink=sink)

    assert decision is False
    assert sink == {"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID}


def test_the_recorded_identifiers_are_the_lookups_own_and_not_rederived():
    """No fabrication: the two values come from what the lookup returned.

    `LGR-c58f276-2` is the shape being followed — a supporting identifier is
    recorded beside the actor rather than invented from one. Here the manifest and
    approval ids are copied verbatim from the lookup, so a manifest belonging to a
    different approval cannot be attributed to this one.
    """
    other = dict(_reusable(), manifest_id="some-other-manifest", approval_id="some-other-approval")
    sink: dict = {}
    _decide(source_column_names=[_NON_CONFORMING], reusable=other, sink=sink)

    assert sink == {"manifest_id": "some-other-manifest", "approval_id": "some-other-approval"}


def test_a_run_routed_to_a_human_records_no_provenance():
    """R10.10 is a WHERE clause: nothing was reused, so nothing is attributed.

    This run asks a human, and that human's own decision is recorded by the
    approval path. Writing a provenance here would claim an inheritance that did
    not happen.
    """
    sink: dict = {}
    decision = _decide(source_column_names=["cust_id", _NON_CONFORMING], reusable=None, sink=sink)

    assert decision is True
    assert sink == {}


def test_a_conforming_schema_records_no_provenance_and_issues_no_query():
    """The cheap gate is why the lookup lives inside the clause at all.

    Hoisting it into `lambda_handler` was rejected because keeping this gate would
    make the handler a second authority for whether the clause fires, and dropping
    the gate would put a DynamoDB query on every run.
    """
    sink: dict = {}
    decision = _decide(source_column_names=["cust_id", "first_name", "total_2024"], reusable=None, sink=sink)

    assert decision is False
    assert sink == {}
    _decide.last_lookup.assert_not_called()  # type: ignore[attr-defined]


def test_the_clause_being_inert_records_no_provenance():
    """A caller supplying no column names reaches neither the lookup nor the sink."""
    sink: dict = {}
    decision = _decide(source_column_names=None, reusable=_reusable(), sink=sink)

    assert decision is False
    assert sink == {}
    _decide.last_lookup.assert_not_called()  # type: ignore[attr-defined]


# --- The state row -------------------------------------------------------------


def _stored_item(rename_manifest_reuse: dict | None) -> dict:
    """The `Item` `store_mapping_result` put, with its collaborators stubbed."""
    table = MagicMock()
    with (
        patch.object(_mod, "table", table),
        patch.object(_mod, "create_approval_payload", return_value={"approval_type": "schema_mapping"}),
        patch.object(_mod, "create_output_data", return_value={"artifact_type": "schema_mapping"}),
        patch.object(_mod, "convert_floats_to_decimal", side_effect=lambda value: value),
    ):
        _mod.store_mapping_result(
            _RUN_ID,
            _PIPELINE,
            _MANIFEST_ID,
            _high_confidence_mapping(),
            _RUN_HASH,
            _source_schema("cust_id", _NON_CONFORMING),
            _target_schema(),
            "tgt-hash",
            None,
            False,
            rename_manifest_reuse=rename_manifest_reuse,
        )
    return table.put_item.call_args.kwargs["Item"]


def test_the_state_row_carries_the_provenance_at_top_level_in_the_fixed_shape():
    """Design §Data Models' shape, at top level beside `mapping_id`.

    Top level and not inside `output_data["approval"]`, following the
    `generated_by` precedent in this same put (P28 R3.5): provenance a reader of
    the row can see without descending into a payload.
    """
    item = _stored_item({"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID})

    assert item["rename_manifest_reuse"] == {"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID}


def test_the_provenance_is_not_written_inside_the_approval_payload():
    """The design presents it as its own shape, so the approval payload is untouched.

    Task 1.2's characterization of `create_approval_payload` pins that payload's
    keys, and this task adds none of them.
    """
    item = _stored_item({"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID})

    assert "rename_manifest_reuse" not in item["output_data"]["approval"]
    assert "rename_manifest_reuse" not in item["output_data"]


def test_the_provenance_lands_in_the_same_write_as_the_decision_it_produced():
    """One `put_item`, so the reuse and its outcome cannot come to disagree."""
    item = _stored_item({"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID})

    assert item["approval_required"] is False
    assert "rename_manifest_reuse" in item


def test_a_run_that_reused_nothing_writes_no_key_at_all():
    """Absence is meaningful and must not be defaulted away.

    Writing `{}` or `None` on every non-reusing run would make the honest majority
    indistinguishable from a reuse whose provenance failed to arrive, so
    "this run inherited a decision" stays a presence test.
    """
    for nothing in (None, {}):
        item = _stored_item(nothing)
        assert "rename_manifest_reuse" not in item, nothing


# --- Handler wiring ------------------------------------------------------------

_PATCH_TARGETS = (
    "get_source_schema",
    "get_pipeline_metadata",
    "get_target_schema",
    "normalize_schema_format",
    "get_ai_model_for_pipeline",
    "generate_mapping_id",
    "enforce_source_field_mappability",
    "store_mapping_separately",
    "store_mapping_result",
    "publish_pipeline_state_change",
    "write_agent_audit_entry",
    "AgentTracePlugin",
    "read_source_samples",
)


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    The same helper shape the sibling handler-driving files in this directory use.
    """

    def __init__(self, managers: dict):
        self._managers = managers

    def __enter__(self) -> dict:
        self._entered: list = []
        values = {}
        for name, manager in self._managers.items():
            values[name] = manager.__enter__()
            self._entered.append(manager)
        return values

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


def _run_handler(
    *,
    source_columns: tuple[str, ...] = ("cust_id", _NON_CONFORMING),
    query_pages: list[dict] | None = None,
    force_remap: bool = False,
) -> dict:
    """One `lambda_handler` run with the REAL clause and the REAL lookup."""
    table = MagicMock()
    table.get_item.return_value = {
        "Item": {
            "run_id": _RUN_ID,
            "step_name": "schema_extract",
            "original_pipeline_id": _PIPELINE,
            "schema_hash": _RUN_HASH,
        }
    }
    table.query.side_effect = list(query_pages) if query_pages else [{"Items": []}]

    managers = {name: patch.object(_mod, name) for name in _PATCH_TARGETS}
    managers["table"] = patch.object(_mod, "table", table)
    managers["memory_client"] = patch.object(_mod, "memory_client", None)
    managers["generate_mapping_with_bedrock"] = patch.object(
        _mod, "generate_mapping_with_bedrock", return_value=_high_confidence_mapping(_first_name_column(source_columns))
    )

    with _nested(managers) as mocks:
        mocks["get_source_schema"].return_value = _source_schema(*source_columns)
        mocks["get_pipeline_metadata"].return_value = _pipeline_metadata()
        mocks["get_target_schema"].return_value = (_target_schema(), "tgt-hash")
        mocks["normalize_schema_format"].side_effect = lambda schema: schema
        mocks["get_ai_model_for_pipeline"].return_value = "us.test-model"
        mocks["generate_mapping_id"].return_value = _MANIFEST_ID
        mocks["read_source_samples"].return_value = {}
        mocks["AgentTracePlugin"].return_value = MagicMock()

        event = {
            "run_id": _RUN_ID,
            "pipeline_id": _PIPELINE,
            "source_schema_hash": _RUN_HASH,
            "additional_context": "",
        }
        if force_remap:
            event["force_remap"] = True
        mocks["_result"] = _mod.lambda_handler(event, None)
        mocks["_table"] = table

    return mocks


def _passed_provenance(mocks: dict):
    """What the handler handed `store_mapping_result` as `rename_manifest_reuse`.

    Read from kwargs, and the positional arity is asserted, because passing this
    by keyword is deliberate: an eleventh POSITIONAL argument would shift the index
    the sibling wired tests read `approval_required` off.
    """
    args, kwargs = mocks["store_mapping_result"].call_args
    assert len(args) == 10, f"store_mapping_result's positional arity changed: {args}"
    assert "rename_manifest_reuse" in kwargs, kwargs
    return kwargs["rename_manifest_reuse"]


def _audit_calls(mocks: dict) -> list:
    """Every `write_agent_audit_entry` call carrying this task's action."""
    return [
        call for call in mocks["write_agent_audit_entry"].call_args_list if call.kwargs.get("action") == _AUDIT_ACTION
    ]


def test_through_the_handler_a_reused_manifest_reaches_the_state_row():
    """The whole path: row hash → filter → eligible row → provenance on the row."""
    mocks = _run_handler(query_pages=[{"Items": [_row()]}])

    assert _passed_provenance(mocks) == {"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID}
    assert mocks["_table"].query.called


def test_through_the_handler_a_reused_manifest_writes_one_audit_entry():
    """The audit half. `LGR-c58f276-1`'s subject is the audit trail specifically.

    The actor stays `agent:schema_mapping` — honest, because the machine took this
    step — and `approval_id` in `details` is the pointer to the human who
    authorised it. Naming that human here would mean reading the approvals table
    and asserting an identity this code has not checked.
    """
    mocks = _run_handler(query_pages=[{"Items": [_row()]}])

    calls = _audit_calls(mocks)
    assert len(calls) == 1, mocks["write_agent_audit_entry"].call_args_list
    kwargs = calls[0].kwargs
    assert kwargs["agent_type"] == "schema_mapping"
    assert kwargs["resource_id"] == _RUN_ID
    assert kwargs["run_id"] == _RUN_ID
    assert kwargs["pipeline_id"] == _PIPELINE
    assert kwargs["details"] == {"rename_manifest_reuse": {"manifest_id": _MANIFEST_ID, "approval_id": _APPROVAL_ID}}


def test_the_audit_entry_and_the_state_row_carry_the_same_shape():
    """One shape in two records, so a reader never has to reconcile two conventions."""
    mocks = _run_handler(query_pages=[{"Items": [_row()]}])

    assert _audit_calls(mocks)[0].kwargs["details"]["rename_manifest_reuse"] == _passed_provenance(mocks)


def test_through_the_handler_a_run_sent_to_a_human_records_nothing():
    """No candidate row, so the clause forces a human and attributes nothing to one."""
    mocks = _run_handler(query_pages=[{"Items": []}])

    assert _passed_provenance(mocks) is None
    assert _audit_calls(mocks) == []


def test_through_the_handler_a_conforming_schema_records_nothing():
    """No non-conforming column means no lookup, no provenance and no audit entry."""
    mocks = _run_handler(source_columns=("cust_id", "first_name"))

    mocks["_table"].query.assert_not_called()
    assert _passed_provenance(mocks) is None
    assert _audit_calls(mocks) == []


def test_the_force_remap_arm_records_nothing():
    """That arm skips the decision entirely, so it reuses nothing to attribute.

    The sink is created before the branch so it exists on both arms — the shape
    `LGR-5c539a6-2` (standing invariant) established for a value that must be
    present on both arms of a result — and it stays empty here.
    """
    mocks = _run_handler(force_remap=True, query_pages=[{"Items": [_row()]}])

    mocks["_table"].query.assert_not_called()
    assert _passed_provenance(mocks) is None
    assert _audit_calls(mocks) == []
