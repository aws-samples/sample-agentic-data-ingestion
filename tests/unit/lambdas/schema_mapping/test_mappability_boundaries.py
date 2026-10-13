# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 3.3 — W5's three named boundaries (R14.3, R14.5, R14.8).

**Subject, named by file and line because neither name is unique in this
repository:** ``enforce_source_field_mappability`` in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1815``, called
once from ``lambda_handler`` (``:2182``) at ``:2405`` — after
``generate_mapping_with_bedrock`` (``:2328``) and before
``store_mapping_separately`` (``:2425``), ``determine_approval_requirement``
(``:2448``) and ``store_mapping_result`` (``:2453``). The handler's ``except``
re-raises an already-classified ``StepFailure`` as-is at ``:2694``. All figures
verified in this tree after task 3.1's change; the spec text cites the
pre-change ``:2111`` / ``:2181`` / ``:2204`` for the three handler call sites and
``:1552`` / ``:1650`` for ``determine_approval_requirement`` and its ``> 0.2``
clause, which are now ``:1553`` / ``:1651``.

WHY THESE THREE ARE EXAMPLES AND NOT PROPERTIES
-----------------------------------------------
Behaviour does not vary meaningfully with input in any of them. A zero-field
schema is one shape, not a space — ``0/0`` is undefined for every possible
mapping beside it. "No approval row was written" is a statement about *which
writes did not happen*, and generating a hundred refused runs asserts the same
absence a hundred times. And the raised exception's class is a constant of the
code path. Property 4's space — the (mapped, total) grid — is task 3.2's, in
``test_mappability_partition_prop.py``.

WHAT EACH TEST OBSERVES, AND WHAT MAKES IT NON-VACUOUS
------------------------------------------------------
1. **Zero source fields** (R14.5). The check returns ``None`` and raises
   nothing, and the pre-existing criteria still route the run to a human. Real
   rather than theoretical: p46's defect returned
   ``{"type": "object", "properties": {}}`` instead of raising, which is exactly
   how a zero-field schema arrives — so the fixture asserts it *is* that literal
   shape rather than merely being empty. Also driven through
   ``lambda_handler``, because "``0/0`` must not evaluate to a refusal" is a
   claim about the run, not only about the function.

   *Two honest corrections to the spec text, both about this clause.* First, its
   line: ``requirements.md`` and ``tasks.md`` both cite ``:1656`` for the "no
   mappings generated" clause. In this tree ``:1656`` is blank and ``:1657`` is
   the **target-side** ratio clause; the no-mappings clause is at ``:1663``, and
   was at ``:1662`` before 3.1's one-line insertion — so the cited figure was
   never that clause. Second, and the reason it matters: that clause is
   **behind** the confidence clause (``:1647``), and for a zero-mapping result
   ``avg_confidence`` is ``0.0``, so the confidence clause fires first and
   returns before the no-mappings clause is reached. The **outcome** R14.5
   requires is unchanged and is what is asserted; which clause delivers it is
   observed from the log rather than assumed, so the two cannot drift silently.

2. **No approval row written** (R14.3). Driven through the handler with
   ``store_mapping_separately``, ``determine_approval_requirement`` and
   ``store_mapping_result`` **left unpatched** — only the two DynamoDB table
   resources are mocks. That is the difference between this test and task 3.1's
   ``test_a_refused_run_never_reaches_the_mapping_write_or_the_approval_decision``,
   which asserts those three were *not called*. Here the real writers are in
   place, so a refusal moved one block later would genuinely write an
   ``approval_required`` state row and a mapping record, and this test reads the
   items actually handed to ``put_item`` to say it did not.

3. **Permanent, so codegen is unreachable** (R14.8). ``type(raised) is
   StepFailure`` rather than ``isinstance`` — ``TransientStepFailure`` **is** a
   subclass of ``StepFailure`` (``shared/step_errors.py:143``), so an
   ``isinstance`` check would pass for the transient class too and assert
   nothing. The class **name** is what Step Functions reports as ``Error`` and
   what ``ErrorEquals`` matches (``step_errors.py``'s module docstring), and
   ``stepRetry`` matches only ``TransientStepFailure`` and Lambda service errors
   (``orchestration-stack.ts:219-224``), so a plain ``StepFailure`` terminates
   the execution instead of retrying into code generation. Permanent is also
   correct on the merits: an incompatible schema pair does not start being
   compatible on attempt two. The handler-propagated failure is checked for
   ``details["mappability_ratio"]`` as well, because a re-wrap by the handler's
   broad ``except`` would replace ``details`` with ``{"run_id", "pipeline_id"}``
   — the no-relabel concern ``LGR-b2f79ef-13`` records for this same module.

Ledger status
-------------
``why`` on the subject's line reports ``uncommitted_line`` (3.1's function is not
committed yet), so no record governs it and the file's recent history is shown
instead. ``LGR-60c4985-7`` (**template / ratified**) is the record for the
permanent-``StepFailure``-terminates-the-execution mechanism test 3 asserts —
the same mechanism an unsupported extension uses. ``LGR-9e76249-7``
(**template / ratified**; BROAD, 80 files — background) governs the clause
ordering test 1 observes. ``LGR-b2f79ef-13`` is the reasoning precedent against
letting a broad ``except`` re-label a classified exception. Patching is
``patch.object(_mod, …)`` throughout, per ``LGR-b397ee2-2`` and
``LGR-c58f276-4``.

**Validates: Requirements 14.3, 14.5, 14.8**
"""

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

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
# share, so it cross-contaminates between test files. The fixtures below are
# deliberately the same SHAPES as `test_mappability_hard_fail.py`'s and are
# re-declared rather than imported — importing them would bind helpers closing
# over *that* module's `_mod`, a different object from this module's, and every
# patch here would then target the wrong one.
sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod

    #: The exception hierarchy, read from the same module object `_mod` imported it
    #: from. Asserted rather than assumed, because `type(...) is StepFailure` below
    #: would silently stop discriminating if these were two different classes.
    import step_errors as _step_errors
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

assert _step_errors.StepFailure is _mod.StepFailure, "the test and the module under test hold different StepFailures"

_THRESHOLD = _mod.MAPPABILITY_HARD_FAIL_THRESHOLD
assert 0 < _THRESHOLD < 1, f"a refusal threshold of {_THRESHOLD} would make this whole file vacuous"


# --- Fixtures (the shapes task 3.1 established) --------------------------------


def _schema(field_count: int, prefix: str = "src") -> dict:
    """A normalised source schema declaring *field_count* fields.

    At ``field_count == 0`` this is byte-for-byte the shape p46's defect
    produced — see `test_a_zero_field_schema_is_the_p46_defect_shape_exactly`.
    """
    return {"type": "object", "properties": {f"{prefix}_{i}": {"type": "string"} for i in range(field_count)}}


def _mapping_result(mapped: int, *, prefix: str = "src", unmapped_source: list[str] | None = None) -> dict:
    """A mapping result mapping the first *mapped* fields of `_schema`'s output."""
    return {
        "mappings": [
            {"source_field": f"{prefix}_{i}", "target_field": f"tgt_{i}", "confidence": 1.0, "transformation": "direct"}
            for i in range(mapped)
        ],
        "unmapped_source_fields": [] if unmapped_source is None else unmapped_source,
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _pipeline_metadata() -> dict:
    """A pipeline whose configuration reaches the quality clauses."""
    return {
        "pipeline_id": "p55-task-3-3",
        "approval_config": {
            "schema_mapping_enabled": True,
            "schema_mapping_confidence_threshold": Decimal(str(_mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD)),
            "always_approve_schema_changes": True,
        },
    }


def _decide_with_log(mapping_result: dict) -> tuple[bool, list[str]]:
    """The pre-existing criteria's verdict, plus the messages that explain it.

    The same two attribution guards task 1.1 uses: the configuration must have
    been read (so this is a clause and not an early return on missing metadata),
    and `logger.error` must never fire (so this is not the trailing
    `except Exception`'s `return True` being recorded as a clause).
    """
    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = _pipeline_metadata()
        decision = _mod.determine_approval_requirement("p55-task-3-3", mapping_result, None)
        assert mock_get_meta.called, "the approval configuration was never read — an early return, not a clause"
        mock_logger.error.assert_not_called()
        messages = [str(call.args[0]) if call.args else "" for call in mock_logger.info.call_args_list]
    return decision, messages


# --- The handler harness ------------------------------------------------------

#: Everything the handler needs stubbed that is NOT part of what a test observes.
#: Deliberately shorter than `test_mappability_hard_fail.py`'s `_PATCH_TARGETS`:
#: `store_mapping_separately`, `determine_approval_requirement` and
#: `store_mapping_result` are absent, so the REAL writers run when reached. That
#: is what lets `test_a_refused_run_persists_no_approval_required_row_and_no_mapping_record`
#: read the items handed to `put_item` rather than assert a call did not happen.
_PATCH_TARGETS = (
    "get_source_schema",
    "get_pipeline_metadata",
    "get_target_schema",
    "normalize_schema_format",
    "get_ai_model_for_pipeline",
    "generate_mapping_id",
    "publish_pipeline_state_change",
    "write_agent_audit_entry",
    "AgentTracePlugin",
    "read_source_samples",
)

#: The three persistence functions, patched only where a test needs the handler
#: to run past them (task 3.1 owns the assertions about them being uncalled).
_PERSISTENCE_TARGETS = ("store_mapping_separately", "determine_approval_requirement", "store_mapping_result")


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    The same helper shape `test_sample_uri_preference_prop.py:224` and
    `test_mappability_hard_fail.py` use to drive this handler.
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


def _run_handler(mapped: int, declared: int, *, patch_persistence: bool) -> dict:
    """One `lambda_handler` run over a mapping of *mapped* of *declared* fields.

    With ``patch_persistence=False`` the three real writers run if reached and
    the only things standing between them and DynamoDB are the two mocked table
    resources — which is the point: a refusal moved after them would show up as
    an item handed to `put_item`.
    """
    names = _PATCH_TARGETS + (_PERSISTENCE_TARGETS if patch_persistence else ())
    managers = {name: patch.object(_mod, name) for name in names}
    managers["table"] = patch.object(
        _mod,
        "table",
        MagicMock(
            get_item=MagicMock(
                return_value={"Item": {"run_id": "r-p55", "step_name": "schema_extract", "original_pipeline_id": "p-1"}}
            )
        ),
    )
    managers["mappings_table"] = patch.object(_mod, "mappings_table", MagicMock())
    managers["memory_client"] = patch.object(_mod, "memory_client", None)
    managers["generate_mapping_with_bedrock"] = patch.object(
        _mod, "generate_mapping_with_bedrock", return_value=_mapping_result(mapped)
    )

    with _nested(managers) as mocks:
        mocks["get_source_schema"].return_value = _schema(declared)
        mocks["get_pipeline_metadata"].return_value = _pipeline_metadata()
        mocks["get_target_schema"].return_value = (_schema(max(declared, 1), prefix="tgt"), "tgt-hash")
        mocks["normalize_schema_format"].side_effect = lambda schema: schema
        mocks["get_ai_model_for_pipeline"].return_value = "us.test-model"
        mocks["generate_mapping_id"].return_value = "map-p55"
        mocks["read_source_samples"].return_value = {}
        mocks["AgentTracePlugin"].return_value = MagicMock()
        if patch_persistence:
            mocks["determine_approval_requirement"].return_value = False

        event = {"run_id": "r-p55", "pipeline_id": "p-1", "source_schema_hash": "src-hash", "additional_context": ""}
        try:
            mocks["_result"] = _mod.lambda_handler(event, None)
        except _mod.StepFailure as raised:
            mocks["_raised"] = raised

    return mocks


def _put_items(table_mock: MagicMock) -> list[dict]:
    """Every `Item` handed to `put_item` on a mocked table resource, in order."""
    return [call.kwargs["Item"] for call in table_mock.put_item.call_args_list if "Item" in call.kwargs]


# --- R14.5: zero source fields ------------------------------------------------


def test_a_zero_field_source_schema_is_left_to_the_existing_clauses():
    """R14.5. The ratio is undefined, `0/0` does not refuse, and a human still decides.

    Three observations, because R14.5 has three parts. The check **returns
    `None`** rather than raising, so nothing about the refusal path is entered.
    The fixture **is** p46's defect shape, so this is the real arrival route and
    not a hypothetical one. And the pre-existing criteria **still require a
    human**, which is the "must keep doing so" half — a change that made a
    zero-field run auto-approve would satisfy "no refusal" while being the
    worse failure.
    """
    empty_schema = _schema(0)
    assert empty_schema == {"type": "object", "properties": {}}, (
        "this fixture is asserted to BE p46's defect output — a reader must not have to trust "
        "the docstring for the claim that this is how a zero-field schema arrives"
    )

    with patch.object(_mod, "logger"):
        ratio = _mod.enforce_source_field_mappability(
            _mapping_result(0), empty_schema, run_id="r-p55", pipeline_id="p-p55"
        )

    assert ratio is None, f"a zero-field schema must yield an undefined ratio, got {ratio!r}"

    decision, messages = _decide_with_log(_mapping_result(0))
    assert decision is True, "a zero-field run must still reach a human (R14.5)"

    # Which clause delivers it, observed rather than assumed. The spec text says
    # "no mappings generated" (cited as `:1656`, actually `:1663`); in this tree
    # the confidence clause (`:1647`) is ahead of it and `avg_confidence` is 0.0
    # for an empty `mappings`, so that one fires first. The outcome R14.5
    # requires is the same either way, and pinning WHICH means a reordering of
    # those two clauses cannot silently change the reason a zero-field run
    # reaches a human.
    assert any("low confidence" in message.lower() for message in messages), (
        f"expected the confidence clause to be what routes a zero-mapping run to a human; logs were {messages}"
    )


def test_a_zero_field_schema_with_mappings_present_still_yields_an_undefined_ratio():
    """R14.5's other arrival shape: the model named fields the schema does not declare.

    The denominator is the **declared** schema (task 3.1's decision), so a
    zero-field schema makes the ratio undefined no matter how many entries
    `mappings` carries. Without this arm, an implementation that fell back to
    the model's own accounting would compute 1.0 here — not a refusal, so the
    test above would still pass — and would then compute a *different* ratio
    from the one the refusal message and the dashboard show.
    """
    with patch.object(_mod, "logger"):
        assert _mod.enforce_source_field_mappability(_mapping_result(3), _schema(0)) is None


def test_a_zero_field_run_reaches_the_mapping_write_through_the_handler():
    """R14.5 at the handler level: `0/0` does not terminate the run.

    The function-level assertion above says the check returns `None`. This says
    the run then proceeds — the same end-to-end shape as an above-threshold run
    — because "must not evaluate to a refusal" is a claim about the execution,
    and a `None` the handler then treated as a failure would satisfy the former
    while violating the latter.
    """
    mocks = _run_handler(mapped=0, declared=0, patch_persistence=True)

    assert "_raised" not in mocks, f"a zero-field run was refused: {mocks.get('_raised')}"
    assert mocks["_result"]["statusCode"] == 200
    mocks["store_mapping_separately"].assert_called_once()
    mocks["determine_approval_requirement"].assert_called_once()
    mocks["store_mapping_result"].assert_called_once()


# --- R14.3: no approval row, no mapping record --------------------------------


def test_a_refused_run_persists_no_approval_required_row_and_no_mapping_record():
    """R14.3, read off the items actually written rather than off a call count.

    The real `store_mapping_separately`, `determine_approval_requirement` and
    `store_mapping_result` are in place; only the two table resources are mocks.
    So if the refusal were moved one block later, `mappings_table.put_item`
    would receive a mapping item and `table.put_item` would receive a state row
    carrying `approval_required` — and both are asserted absent. Task 3.1 owns
    the complementary assertion that those three functions are not called; this
    one is about what a reader of the table would find afterwards.
    """
    declared, mapped = 4, 1
    assert mapped / declared <= _THRESHOLD, "the fixture must sit at or below the threshold or this proves nothing"

    mocks = _run_handler(mapped=mapped, declared=declared, patch_persistence=False)

    # Non-vacuity: the run really did get as far as the refusal, and the failure
    # really is the refusal rather than some earlier harness breakage.
    assert "_raised" in mocks, "the handler completed; the refusal never fired and this test is vacuous"
    assert "mappability ratio" in mocks["_raised"].message
    mocks["generate_mapping_with_bedrock"].assert_called_once()

    state_items = _put_items(mocks["table"])
    assert len(state_items) == 1, (
        f"expected exactly one state row — the failure row written at `:2662` — got {len(state_items)}: "
        f"{[item.get('status') for item in state_items]}"
    )
    failure_row = state_items[0]
    assert failure_row["status"] == "failed"
    assert failure_row["step_name"] == _mod.StepName.SCHEMA_MAPPING
    assert "approval_required" not in failure_row, (
        f"a refused run wrote a state row carrying approval_required={failure_row.get('approval_required')!r} "
        f"— R14.3 requires the run to be failed WITHOUT creating a schema mapping approval"
    )
    assert "mapping_id" not in failure_row
    assert "output_data" not in failure_row, (
        "output_data is where `store_mapping_result` puts the approval payload; its presence means "
        "the approval payload was built for a run being refused"
    )

    # No orphan mapping record. `store_mapping_separately` reads before it
    # writes, so both halves are asserted — a read would mean the function was
    # entered at all.
    mocks["mappings_table"].put_item.assert_not_called()
    mocks["mappings_table"].get_item.assert_not_called()


# --- R14.8: permanent, so code generation is unreachable ----------------------


def test_the_refusal_is_a_plain_step_failure_and_not_a_transient_one():
    """R14.8's mechanism: the exception CLASS is what makes codegen unreachable.

    `stepRetry` matches only `TransientStepFailure` and Lambda service errors
    (`orchestration-stack.ts:219-224`), so a plain `StepFailure` terminates the
    execution rather than retrying into code generation. Asserted with
    `type(...) is` and on the class **name**, because the name is what Step
    Functions reports as `Error` and matches on — and because
    `TransientStepFailure` subclasses `StepFailure`, so `isinstance` would pass
    for the transient class and assert nothing. Permanent is also correct on the
    merits: an incompatible schema pair does not start being compatible on
    attempt two.
    """
    # The reason `isinstance` is not the check — stated as an assertion so a
    # future flattening of the hierarchy makes this test say so.
    assert issubclass(_step_errors.TransientStepFailure, _mod.StepFailure)

    with patch.object(_mod, "logger"), pytest.raises(_mod.StepFailure) as caught:
        _mod.enforce_source_field_mappability(_mapping_result(1), _schema(4), run_id="r-p55", pipeline_id="p-p55")
    raised = caught.value

    assert type(raised) is _mod.StepFailure, f"the refusal is a {type(raised).__name__}, not a plain StepFailure"
    assert not isinstance(raised, _step_errors.TransientStepFailure)
    assert type(raised).__name__ == "StepFailure", "the class name is what SFN ErrorEquals matches — do not rename"
    assert raised.error_type == "permanent"
    assert raised.to_dict()["error_type"] == "permanent", (
        "the serialization travels in the SFN Cause and is what RecordFailure parses; a permanent "
        "class carrying a transient error_type would be read as retryable downstream"
    )


def test_the_handler_propagates_the_refusal_without_re_labelling_it():
    """The other half of R14.8: the classification survives the handler's own `except`.

    A permanent class is only permanent if it reaches Step Functions as that
    class. The handler's broad `except Exception` re-raises an already-structured
    `StepFailure` as-is (`:2694`); had it re-wrapped, `details` would be
    replaced with `{"run_id", "pipeline_id"}` and the ratio would be gone —
    which is the exact no-relabel concern `LGR-b2f79ef-13` records for this
    module. So the propagated failure is checked for the refusal's own
    `details`, not merely for its class.
    """
    mocks = _run_handler(mapped=1, declared=4, patch_persistence=False)

    assert "_raised" in mocks, "the handler completed; nothing propagated and this test is vacuous"
    raised = mocks["_raised"]

    assert type(raised) is _mod.StepFailure
    assert not isinstance(raised, _step_errors.TransientStepFailure)
    assert raised.error_type == "permanent"
    assert raised.details["mappability_ratio"] == pytest.approx(0.25), (
        "the propagated failure lost the refusal's details, so the handler re-wrapped it instead of re-raising it as-is"
    )
    assert raised.details["mappability_hard_fail_threshold"] == _THRESHOLD
