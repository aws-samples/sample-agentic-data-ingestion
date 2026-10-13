# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 3.1 — the Source_Field_Mappability_Ratio hard fail (R14.1–R14.4, R14.8).

**Subject, named by file and line because neither name is unique in this
repository:** `enforce_source_field_mappability` in
`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1815`, called
from `lambda_handler` (`:2182`) at `:2405` — after
`generate_mapping_with_bedrock` (`:2328`) and before `store_mapping_separately`
(`:2425`), `determine_approval_requirement` (`:2448`) and `store_mapping_result`
(`:2453`).

Those figures are the ones verified in this tree **after** this task's change.
The spec cites `:2111` / `:2181` / `:2204` for the three handler call sites and
all three were correct before the change; the +217 drift is this task's own
insertion (one import line plus the new function) and nothing else moved.
`determine_approval_requirement` moved `:1552` → `:1553` and its `> 0.2` clause
`:1650` → `:1651` for the same one-line reason — the new function is placed
*after* `create_approval_payload` deliberately, so the `:1553`–`:1798` block that
task 1.1 and the ledger reference shifts by exactly one line rather than by the
function's length.

WHAT THIS FILE OWNS, AND WHAT IT DELIBERATELY DOES NOT
------------------------------------------------------
Owned here: the refusal boundary (R14.1), the message's three contents (R14.2),
the no-change-above-the-threshold arm (R14.4), the denominator decision, and the
placement — that a refused run reaches neither the mapping write nor the
approval decision.

**Not** here, because tasks 3.2 and 3.3 own them and duplicating would pre-empt
their work: the three-way partition and the subset relationship as a property
(3.2, design Property 4), the zero-source-fields case (3.3, R14.5), the
assertion about the persisted state row carrying no `approval_required` (3.3,
R14.3) and the assertion that the raise is a `StepFailure` and **not** a
`TransientStepFailure` (3.3, R14.8). This file asserts the *class* of the raise
only as far as it needs to catch it.

THE DENOMINATOR, WHICH IS A DECISION AND NOT AN INHERITANCE
-----------------------------------------------------------
Two different quantities in this module are both spelled
`total_source_fields`, about a hundred lines apart, and both are read:
`determine_approval_requirement:1626` uses `len(mappings) + len(unmapped_source)`
and `create_approval_payload:1729` uses `len(source_schema["properties"])`. The
production function adopts the second — `requirements.md`'s glossary says "the
total count of source fields in **that run's source schema**", and R14.2 puts the
ratio in front of an operator who will compare it against what the dashboard
showed them, which is that same number.

That choice is pinned from both directions below, by two witnesses that give
opposite answers under the two denominators, so the decision cannot be quietly
reverted:
`test_the_denominator_is_the_declared_schema_when_the_model_under_reports` and
`test_the_denominator_is_the_declared_schema_when_the_model_names_undeclared_fields`.
Neither is a hypothetical shape — `mappings` on the Bedrock arm is model output
parsed as returned (`:806-826`).

Ledger state for this area: `why` on `schema_mapping/lambda_function.py:2181`
returns only commit `8f3f9e1b30` with `records: []` — a broad snapshot, so no
fence. The governing records are `LGR-9e76249-7` (**template / ratified**; BROAD,
80 files — background) for the clause ordering these clauses sit in, and
`LGR-60c4985-7` for the permanent-`StepFailure`-terminates-the-execution
mechanism this refusal reuses. `LGR-b2f79ef-13` is the reasoning precedent on
this same module for not letting a broad `except Exception` re-label a classified
exception — honoured here: the handler's own `except` re-raises a `StepFailure`
as-is (`:2694`).

**Validates: Requirements 14.1, 14.2, 14.3, 14.4, 14.8**
"""

import ast
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
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)` rather than `@patch("lambda_function.…")`
# (`LGR-b397ee2-2`, `LGR-c58f276-4`).
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

#: Read from the module under test rather than restated. `shared/config.py` is
#: its single source (P28 R18.2) and `tests/unit/shared/test_config.py` owns its
#: value; `tests/unit/shared/test_config_mappability_nesting.py` (task 1.4) owns
#: its relationship to the `> 0.2` clause.
_THRESHOLD = _mod.MAPPABILITY_HARD_FAIL_THRESHOLD

# Non-vacuity guard: every boundary below is arithmetic on this number.
assert 0 < _THRESHOLD < 1, f"a refusal threshold of {_THRESHOLD} would make this whole file vacuous"


# --- Fixtures -----------------------------------------------------------------


def _schema(field_count: int, prefix: str = "src") -> dict:
    """A normalised source schema declaring *field_count* fields.

    `lambda_handler` runs `normalize_schema_format` before the check, so the
    shape reaching production always carries `properties`.
    """
    return {"type": "object", "properties": {f"{prefix}_{i}": {"type": "string"} for i in range(field_count)}}


def _mapping_result(mapped: int, *, prefix: str = "src", unmapped_source: list[str] | None = None) -> dict:
    """A mapping result mapping the first *mapped* fields of `_schema`'s output.

    Every entry carries both a usable `source_field` and a `target_field`, which
    is what makes it count towards the numerator.
    """
    return {
        "mappings": [
            {"source_field": f"{prefix}_{i}", "target_field": f"tgt_{i}", "confidence": 1.0, "transformation": "direct"}
            for i in range(mapped)
        ],
        "unmapped_source_fields": [] if unmapped_source is None else unmapped_source,
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _ratio(mapped: int, declared: int) -> float | None:
    """Call the subject with a clean logger and return its answer."""
    with patch.object(_mod, "logger"):
        return _mod.enforce_source_field_mappability(_mapping_result(mapped), _schema(declared))


def _refusal(mapping_result: dict, source_schema: dict) -> _mod.StepFailure:
    """Call the subject expecting a refusal; return the raised failure."""
    with patch.object(_mod, "logger"), pytest.raises(_mod.StepFailure) as caught:
        _mod.enforce_source_field_mappability(mapping_result, source_schema, run_id="r-p55", pipeline_id="p-p55")
    return caught.value


# --- R14.1: the refusal boundary ----------------------------------------------


def test_a_ratio_below_the_threshold_is_refused():
    """R14.1. One of four source fields mapped is a ratio of 0.25."""
    declared, mapped = 4, 1
    assert mapped / declared < _THRESHOLD, "the fixture must sit below the threshold or this proves nothing"

    failure = _refusal(_mapping_result(mapped), _schema(declared))

    assert isinstance(failure, _mod.StepFailure)
    assert failure.step_name == "schema_mapping"


def test_a_ratio_exactly_at_the_threshold_is_refused():
    """R14.1's boundary: "at or below", so equality refuses.

    The requirement is `<=`, not `<`, and the two differ on exactly this input.
    Without this arm a drift to a strict `<` would leave every run sitting
    precisely on the threshold routed to a human instead of refused, and nothing
    would say so. Two of four mapped is 0.5 with no floating-point slack.
    """
    declared = 2
    mapped = 1
    assert mapped / declared == _THRESHOLD, "this arm only means something when the ratio IS the threshold"

    failure = _refusal(_mapping_result(mapped), _schema(declared))

    assert isinstance(failure, _mod.StepFailure)


def test_a_ratio_just_above_the_threshold_is_not_refused_and_nothing_else_changes():
    """R14.4: above the threshold the existing clauses decide, unchanged.

    The subject returns the ratio and raises nothing. Returning rather than
    deciding is the point — the function has no opinion about approval, so it
    cannot narrow or widen what `determine_approval_requirement` does with the
    same run.
    """
    declared = 10
    mapped = int(_THRESHOLD * declared) + 1
    assert mapped / declared > _THRESHOLD, "the fixture must sit above the threshold"

    assert _ratio(mapped, declared) == pytest.approx(mapped / declared)


def test_a_fully_mapped_run_is_not_refused():
    """The control the two no-refusal arms are read against.

    Every declared field mapped is a ratio of 1.0. If this refused, the arms
    above would be measuring some unrelated early return rather than the
    threshold comparison.
    """
    assert _ratio(6, 6) == pytest.approx(1.0)


# --- R14.2: the refusal is self-diagnosing ------------------------------------


def test_the_refusal_names_the_ratio_the_threshold_and_the_unmapped_source_fields():
    """R14.2, all three contents, in the message a human reads.

    This is not decoration. The population of live runs at or below the
    threshold is **unmeasured** (design risk R5.6), so a self-diagnosing refusal
    is the whole mitigation: if the first refusals name routine schemas, the
    threshold is wrong and moving it is a product decision to be ratified rather
    than tuned. A message missing any one of the three cannot support that
    judgement, so all three are asserted separately.
    """
    declared, mapped = 4, 1
    failure = _refusal(_mapping_result(mapped), _schema(declared))
    message = failure.message

    # 1. the ratio
    assert f"{mapped / declared:.3f}" in message, f"the ratio is not in the message: {message}"
    # 2. the threshold
    assert str(_THRESHOLD) in message, f"the threshold is not in the message: {message}"
    # 3. the source field names it could not map — every one of them
    for i in range(mapped, declared):
        assert f"src_{i}" in message, f"unmapped source field src_{i} is not named: {message}"
    # …and not the ones it could, which is what makes the list informative.
    assert "src_0" not in message.split("Unmapped source field(s):")[1]


def test_the_refusal_details_carry_the_same_three_facts_structurally():
    """The message is for a human; `details` is for anything reading the failure.

    `StepFailure` serialises `details` into `str(e)` and the Step Functions
    `Cause`, so the ratio and the threshold are recoverable without parsing
    prose. Asserted because the dashboard-facing record writes the *message*
    (`:2696`) while the state machine carries the *serialization*, and a fact
    present in only one of the two is a fact one reader cannot see.
    """
    failure = _refusal(_mapping_result(1), _schema(4))

    assert failure.details["mappability_ratio"] == pytest.approx(0.25)
    assert failure.details["mappability_hard_fail_threshold"] == _THRESHOLD
    assert failure.details["mapped_source_fields"] == 1
    assert failure.details["total_source_fields"] == 4
    assert failure.details["unmapped_source_fields"] == ["src_1", "src_2", "src_3"]
    assert failure.details["run_id"] == "r-p55"
    assert failure.details["pipeline_id"] == "p-p55"


def test_a_wide_source_schema_bounds_the_named_fields_and_says_how_many_more():
    """R14.2 with a bound, because the message travels in the SFN `Cause`.

    The `Cause` is capped at 32,768 characters and the same text lands in the
    failed state row's `error_message`, so a 512-column schema must not put its
    whole column list in either — the same bound-what-reaches-a-record reasoning
    `LGR-b2f79ef-12` applied to the test frame. The *count* is never lost, only
    the tail of the list, and the message says so explicitly.
    """
    cap = _mod._MAX_REPORTED_UNMAPPED_SOURCE_FIELDS
    declared = cap + 10
    failure = _refusal(_mapping_result(0), _schema(declared))

    assert f"and {declared - cap} more" in failure.message
    assert len(failure.details["unmapped_source_fields"]) == cap
    assert failure.details["unmapped_source_field_count"] == declared


# --- The denominator decision -------------------------------------------------


def test_the_denominator_is_the_declared_schema_when_the_model_under_reports():
    """Witness 1: the two denominators disagree and the declared one wins.

    Four `mappings` entries, an **empty** `unmapped_source_fields`, and a source
    schema declaring ten fields. Under `determine_approval_requirement`'s
    denominator (`len(mappings) + len(unmapped_source)` = 4) the mapped ratio is
    1.0 and nothing would fire. Under the declared schema (10) it is 0.4 and the
    run is refused. A refusal here is therefore only possible on the declared
    basis.
    """
    declared = 10
    result = _mapping_result(4, unmapped_source=[])
    assert len(result["mappings"]) / (len(result["mappings"]) + len(result["unmapped_source_fields"])) == 1.0

    failure = _refusal(result, _schema(declared))

    assert "0.400" in failure.message
    assert failure.details["total_source_fields"] == declared


def test_the_denominator_is_the_declared_schema_when_the_model_names_undeclared_fields():
    """Witness 2: the same decision from the opposite direction.

    Four declared fields, all four mapped, and an `unmapped_source_fields` list
    naming four columns the schema does not declare. On the model's own
    accounting the mapped ratio is 4/8 = 0.5 — exactly at the threshold, so that
    basis would **refuse**. On the declared schema it is 1.0 and there is nothing
    to refuse. Together with witness 1 this pins the choice from both sides: a
    revert to the other denominator fails one test or the other whichever way it
    goes.
    """
    declared = 4
    result = _mapping_result(declared, unmapped_source=[f"ghost_{i}" for i in range(4)])
    model_basis = len(result["mappings"]) / (len(result["mappings"]) + len(result["unmapped_source_fields"]))
    assert model_basis <= _THRESHOLD, "witness 2 only discriminates when the model basis would refuse"

    with patch.object(_mod, "logger"):
        assert _mod.enforce_source_field_mappability(result, _schema(declared)) == pytest.approx(1.0)


def test_a_mapping_entry_naming_no_target_field_does_not_count_as_mapped():
    """The glossary says "maps to **at least one target field**".

    An entry carrying a source column but no target maps nothing, so it must not
    lift the numerator. This is the shape `_normalise_mapping_structure` leaves
    in `mappings` on purpose (it only moves entries that name a target and no
    source), so it is reachable rather than theoretical.
    """
    declared = 4
    result = _mapping_result(1)
    result["mappings"].append({"source_field": "src_1", "target_field": "   ", "confidence": 1.0})
    result["mappings"].append({"source_field": "src_2", "confidence": 1.0})

    failure = _refusal(result, _schema(declared))

    assert failure.details["mapped_source_fields"] == 1
    assert "src_1" in failure.message
    assert "src_2" in failure.message


def test_a_mapping_entry_naming_an_undeclared_source_column_does_not_count_as_mapped():
    """The consequence of the declared basis, pinned rather than left implicit.

    A `source_field` the schema does not declare contributes nothing, which errs
    toward refusal. The production docstring states this and states the
    mitigation: the message names every declared field left unaccounted for, so
    a refusal caused by an invented name reads as exactly that.
    """
    declared = 4
    result = _mapping_result(1)
    result["mappings"].append({"source_field": "not_in_the_schema", "target_field": "tgt_9", "confidence": 1.0})

    failure = _refusal(result, _schema(declared))

    assert failure.details["mapped_source_fields"] == 1
    assert "not_in_the_schema" not in failure.message


def test_a_mappings_value_that_is_not_a_list_is_read_as_nothing_mapped():
    """`mappings` is model output parsed as returned, so it may not be a list.

    The subject must answer rather than raise a `TypeError` — an unclassified
    exception here would reach the handler's `except` and be re-wrapped, losing
    the refusal's own message.
    """
    failure = _refusal({"mappings": "not-a-list"}, _schema(4))

    assert failure.details["mapped_source_fields"] == 0


# --- Placement: R14.3's and R14.8's mechanism ---------------------------------

_PATCH_TARGETS = (
    "get_source_schema",
    "get_pipeline_metadata",
    "get_target_schema",
    "normalize_schema_format",
    "get_ai_model_for_pipeline",
    "generate_mapping_id",
    "store_mapping_separately",
    "determine_approval_requirement",
    "store_mapping_result",
    "publish_pipeline_state_change",
    "write_agent_audit_entry",
    "AgentTracePlugin",
    "read_source_samples",
)


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    The same helper shape `test_sample_uri_preference_prop.py:224` uses to drive
    this handler.
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


def _run_handler(mapped: int, declared: int) -> dict:
    """One `lambda_handler` run over a mapping of *mapped* of *declared* fields.

    Returns the mocks so the caller can ask what was and was not reached. The
    handler's own failure path is left unpatched apart from `table` and the
    publisher, so a refusal really does travel through it.
    """
    managers = {name: patch.object(_mod, name) for name in _PATCH_TARGETS}
    managers["table"] = patch.object(
        _mod,
        "table",
        MagicMock(
            get_item=MagicMock(
                return_value={"Item": {"run_id": "r-p55", "step_name": "schema_extract", "original_pipeline_id": "p-1"}}
            )
        ),
    )
    managers["memory_client"] = patch.object(_mod, "memory_client", None)
    managers["generate_mapping_with_bedrock"] = patch.object(
        _mod, "generate_mapping_with_bedrock", return_value=_mapping_result(mapped)
    )

    with _nested(managers) as mocks:
        mocks["get_source_schema"].return_value = _schema(declared)
        mocks["get_pipeline_metadata"].return_value = {
            "pipeline_id": "p-1",
            "approval_config": {"schema_mapping_enabled": True, "schema_mapping_confidence_threshold": Decimal("0.85")},
        }
        mocks["get_target_schema"].return_value = (_schema(declared, prefix="tgt"), "tgt-hash")
        mocks["normalize_schema_format"].side_effect = lambda schema: schema
        mocks["get_ai_model_for_pipeline"].return_value = "us.test-model"
        mocks["generate_mapping_id"].return_value = "map-p55"
        mocks["determine_approval_requirement"].return_value = False
        mocks["read_source_samples"].return_value = {}
        mocks["AgentTracePlugin"].return_value = MagicMock()

        event = {"run_id": "r-p55", "pipeline_id": "p-1", "source_schema_hash": "src-hash", "additional_context": ""}
        try:
            mocks["_result"] = _mod.lambda_handler(event, None)
        except _mod.StepFailure as raised:
            mocks["_raised"] = raised

    return mocks


def test_a_refused_run_never_reaches_the_mapping_write_or_the_approval_decision():
    """R14.3 and "no orphan mapping record", from the placement rather than from a flag.

    A refused run must leave nothing behind. Because the raise happens before
    `store_mapping_separately`, no row is written to the mappings table; because
    it happens before `store_mapping_result`, no `schema_mapping` state row
    carrying `approval_required` is written and the approval record is never
    created. Both are asserted as *calls that did not happen*, which is what this
    task's change is responsible for. Task 3.3 owns the complementary assertion
    about the persisted row's contents.
    """
    mocks = _run_handler(mapped=1, declared=4)

    assert "_raised" in mocks, "the handler completed; the refusal never fired and this test is vacuous"
    assert isinstance(mocks["_raised"], _mod.StepFailure)
    assert "mappability ratio" in mocks["_raised"].message

    mocks["store_mapping_separately"].assert_not_called()
    mocks["determine_approval_requirement"].assert_not_called()
    mocks["store_mapping_result"].assert_not_called()


def test_the_model_call_and_its_audit_entry_still_happen_on_a_refused_run():
    """The refusal is after the Converse call, and that is deliberate.

    A pre-Bedrock name/type proxy is not an upper bound on what the model can map
    (`cust_nm → customer_name`), so gating on one produces false refusals — worse
    than the one cheap call it saves (design W5-D1). The audit entry is written
    before the check for the same honesty reason: the call happened and it cost
    what it cost.
    """
    mocks = _run_handler(mapped=1, declared=4)

    mocks["generate_mapping_with_bedrock"].assert_called_once()
    mocks["write_agent_audit_entry"].assert_called_once()


def test_an_above_threshold_run_still_reaches_the_mapping_write_unchanged():
    """R14.4, at the handler level — the control without which the test above proves nothing.

    Same harness, same fixtures, only the ratio differs. If a run above the
    threshold also stopped short of `store_mapping_separately`, the refusal test
    would be measuring a broken harness rather than the check.
    """
    declared = 10
    mapped = int(_THRESHOLD * declared) + 1
    mocks = _run_handler(mapped=mapped, declared=declared)

    assert "_raised" not in mocks, f"an above-threshold run was refused: {mocks.get('_raised')}"
    assert mocks["_result"]["statusCode"] == 200
    mocks["store_mapping_separately"].assert_called_once()
    mocks["determine_approval_requirement"].assert_called_once()
    mocks["store_mapping_result"].assert_called_once()


# --- The call site's position, read structurally ------------------------------


def _handler_call_order() -> list[str]:
    """The order of the four calls that matter, inside `lambda_handler`.

    Read as a syntax tree rather than by searching the source text.
    `LGR-b46a5bb-4` (**template / ratified**) is the record: substring matching
    over source also forbids *documenting* the fence, and failed against a
    docstring that named the construct on purpose. `lambda_handler`'s body
    discusses these call sites in comments, so it has exactly that hazard.
    """
    source_path = os.path.join(_schema_mapping_dir, "lambda_function.py")
    with open(source_path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=source_path)

    handlers = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == "lambda_handler"]
    assert len(handlers) == 1, f"expected exactly one `def lambda_handler`, found {len(handlers)}"

    watched = {
        "generate_mapping_with_bedrock",
        "enforce_source_field_mappability",
        "store_mapping_separately",
        "determine_approval_requirement",
        "store_mapping_result",
    }
    seen: list[str] = []
    for node in ast.walk(handlers[0]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in watched:
            seen.append((node.lineno, node.func.id))
    return [name for _, name in sorted(seen)]


def test_the_check_is_called_after_the_model_call_and_before_the_mapping_write():
    """W5-D2's placement, pinned as the ordering it is.

    Every property this task claims follows from the position and from nothing
    else: after `generate_mapping_with_bedrock` so the ratio is computed on a
    real mapping, before `store_mapping_separately` so a refused run leaves no
    orphan mapping record, and before `store_mapping_result` so no
    `approval_required` row is written (R14.3). A later edit that moved the call
    one block down would keep every behavioural test above passing on the
    refusal path while quietly writing the mapping record first.
    """
    order = _handler_call_order()

    assert order.count("enforce_source_field_mappability") == 1, f"expected exactly one call site, got {order}"
    check = order.index("enforce_source_field_mappability")

    assert order.index("generate_mapping_with_bedrock") < check
    assert check < order.index("store_mapping_separately")
    assert check < order.index("determine_approval_requirement")
    assert check < order.index("store_mapping_result")
