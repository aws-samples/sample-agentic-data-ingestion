# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p34-code-approval-gate-honors-config, Property 1: Only an explicit boolean false disables the gate
"""Property 1 — only an explicit boolean ``false`` disables the code gate.

*For any* pipeline record, ``resolve_code_approval_setting`` returns
``(False, "pipeline_record")`` **if and only if** ``approval_config`` is a dict
whose ``code_generation_enabled`` is the JSON boolean ``false``; for every other
record — key absent, ``approval_config`` absent or not a dict, or the value
being any non-boolean JSON value including ``"false"``, ``"true"``, ``0``, ``1``,
``null``, a list or a dict — it returns ``True``, and a WARNING-or-above record
naming the raw value is emitted for the non-boolean case.

**Validates: Requirements 1.2, 1.4, 1.5, 7.1, 7.4**

Why this file exists
--------------------
This property is one of amended R8.2's two relocation targets. It carries the
**enabled, disabled, absent and malformed** branches of
``create_code.determine_approval_requirement``, which design decision D2 deletes
(``LGR-f077f1b-1``, **template / ratified**: ``test_code`` owns the
code-generation Approval_Policy and ``create_code``'s decision is deleted rather
than renamed). Those four branches are pinned today at
``tests/unit/lambdas/create_code/test_no_keyword_self_approval.py:112``, ``:134``
and ``:136``; they must be provable in their new home before anything is
deleted, which is why this task precedes the deletion.

Expectations derive from the **requirements**, not from running the current code
— there is no current code to run, and R1.2/R1.4 are fail-closed statements the
defect this spec fixes has never satisfied:

* R1.2 — the setting absent from the record evaluates as ``true``;
* R1.4 — a value that is neither JSON ``true`` nor JSON ``false`` evaluates as
  ``true``;
* R1.5 — that rejected raw value is logged at WARNING level or above;
* R7.1 — no migration and no per-pipeline backfill, so a record written before
  this change (no ``approval_config`` at all) has to resolve on its own;
* R7.4 — the evaluated value **and its source** are both reported, which is why
  the returned tuple's second element is asserted, not only the boolean.

Why the malformed case is reachable
-----------------------------------
``dashboard_api/handlers/pipelines_crud.py:1157`` passes the request-body value
straight into the nested document-path update, unvalidated. The dashboard's own
form cannot produce a non-boolean (``PipelineFormModal.tsx:103-106`` binds a
checkbox), but a direct API caller can — so R1.4/R1.5's branch is a live input
shape, not a defensive nicety. ``test_edit_pipeline_approval_config_boolean.py``
guards the write side; this property covers the read side.

Why identity, not truthiness
----------------------------
``"false"`` is a **truthy** string and ``0`` is a **falsy** int, so a truthiness
test on the raw value inverts the user's setting in both directions: it would
disable the gate for a caller who sent ``0`` meaning "unset", and enable it for
one who sent the string ``"false"`` meaning off. Both non-boolean values must
land on the same fail-closed answer, and the generators below produce both
spellings explicitly for that reason. ``Decimal`` is generated alongside ``int``
because the DynamoDB resource deserialises JSON numbers to ``Decimal`` — the
record this function receives is the resource's output (``:167-168``), so a bare
``int`` is not the only shape a numeric setting can arrive in.

Ledger provenance
-----------------
No record governs this gate; ``requirements.md`` §"Ledger provenance for this
area" is the record, and ``LGR-f077f1b-1`` records the ownership decision.
``LGR-78ce0b7-13`` (**template / ratified**) is why the function under test has
no ``try``/``except`` of its own and takes the already-read record rather than a
``pipeline_id`` — a clause here would swallow the classified exceptions
``get_pipeline_metadata`` (``:131``) and the handler's missing-record guard
(``:1216-1222``) raise, silently reverting p33 R2.8. ``LGR-78ce0b7-8``
(**template / ratified**) is why a missing record arrives as ``None`` and fails
the step instead of reaching this function as ``{}``.
``LGR-3e45e65-1`` (**template / ratified**) is why this file does not re-pin
``AWS_REGION``: ``tests/unit/conftest.py`` assigns it before any test module in
this directory is imported.
"""

import logging
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import event, given, settings
from hypothesis import strategies as st

# Account id is not pinned by tests/unit/conftest.py (only the region is —
# LGR-3e45e65-1), and env_utils is stubbed below in any case.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation. Same set
# as the sibling files in this directory; the module-level cache pop is the
# house pattern for lambda_function cross-contamination (several Lambda dirs
# expose a module of that name).
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# Log capture — real records off the real logger, no logger substitution
# ---------------------------------------------------------------------------


class _RecordCollector(logging.Handler):
    """Collects the records the module's own logger actually emits.

    Attached to the underlying stdlib logger rather than replacing
    ``_mod.logger``, so what is asserted is a real ``LogRecord`` at a real
    level. ``caplog`` is not used because the Powertools ``Logger`` does not
    propagate to the root logger by default, which would make an absent record
    indistinguishable from a suppressed one — and R1.5 is precisely an assertion
    about a record existing at a level.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.NOTSET)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def reset(self) -> None:
        self.records.clear()

    def at_warning_or_above(self) -> list[logging.LogRecord]:
        return [r for r in self.records if r.levelno >= logging.WARNING]


@pytest.fixture(scope="module")
def logs():
    """Attach a collector to the module logger for the duration of this module.

    Module-scoped on purpose: a function-scoped fixture requested by a
    ``@given`` test trips Hypothesis' ``function_scoped_fixture`` health check,
    because the fixture would be set up once and then shared silently across
    every example. The collector is reset explicitly at the top of each example
    instead, which is the same isolation without the ambiguity.
    """
    stdlib_logger = logging.getLogger(_mod.logger.name)
    collector = _RecordCollector()
    original_level = stdlib_logger.level
    stdlib_logger.addHandler(collector)
    # WARNING must not be filtered out before it reaches the handler.
    stdlib_logger.setLevel(logging.DEBUG)
    try:
        yield collector
    finally:
        stdlib_logger.removeHandler(collector)
        stdlib_logger.setLevel(original_level)


# ---------------------------------------------------------------------------
# Generators — the non-boolean space is enumerated, not sampled by accident
# ---------------------------------------------------------------------------

#: The values R1.4 names, spelled out. ``"false"`` / ``0`` are the two that a
#: truthiness test gets wrong in opposite directions; ``Decimal`` is the shape a
#: JSON number actually arrives in through the DynamoDB resource.
_EXPLICIT_NON_BOOLEANS: list[object] = [
    "false",
    "true",
    "False",
    "True",
    "",
    0,
    1,
    -1,
    Decimal("0"),
    Decimal("1"),
    None,
    [],
    [False],
    ["false"],
    {},
    {"enabled": False},
]


def _non_boolean_values() -> st.SearchStrategy[object]:
    """Any JSON-ish value that is not the boolean ``True`` or ``False``.

    The explicit list carries R1.4's named cases; the generated arm keeps the
    property quantified over the wider space rather than only over the examples
    the requirement happened to name.
    """
    generated = st.one_of(
        st.none(),
        st.text(max_size=8),
        st.integers(min_value=-3, max_value=3),
        st.floats(allow_nan=False, allow_infinity=False, min_value=-2, max_value=2),
        st.integers(min_value=-3, max_value=3).map(Decimal),
        st.lists(st.one_of(st.booleans(), st.text(max_size=4)), max_size=3),
        st.dictionaries(st.text(min_size=1, max_size=4), st.booleans(), max_size=2),
    )
    return st.one_of(st.sampled_from(_EXPLICIT_NON_BOOLEANS), generated)


#: Sibling keys a real Pipeline_Record carries alongside the code setting. Their
#: presence must not change the answer, so they are generated rather than fixed.
def _other_approval_keys() -> st.SearchStrategy[dict]:
    return st.dictionaries(
        st.sampled_from(["schema_mapping_enabled", "always_approve_schema_changes", "test_failure_enabled"]),
        st.one_of(st.booleans(), st.none(), st.text(max_size=4)),
        max_size=3,
    )


#: Shapes ``approval_config`` can take that are not a dict — the branch R1.2
#: covers alongside the key simply being absent.
def _non_dict_approval_configs() -> st.SearchStrategy[object]:
    return st.one_of(
        st.none(),
        st.text(max_size=6),
        st.integers(min_value=-2, max_value=2),
        st.booleans(),
        st.lists(st.text(max_size=3), max_size=2),
    )


#: A pre-change Pipeline_Record shape (R7.1/R7.2): plausible surrounding
#: attributes, and nothing about approvals at all.
def _record_padding() -> st.SearchStrategy[dict]:
    return st.fixed_dictionaries(
        {},
        optional={
            "pipeline_id": st.text(min_size=1, max_size=8),
            "pipeline_name": st.text(min_size=1, max_size=8),
            "target_schema_s3_key": st.text(min_size=1, max_size=8),
            "status": st.sampled_from(["active", "created", "paused"]),
        },
    )


#: Discriminates the four record classes the property partitions the space into.
_CLASS_EXPLICIT_FALSE = "explicit_boolean_false"
_CLASS_EXPLICIT_TRUE = "explicit_boolean_true"
_CLASS_ABSENT = "setting_absent"
_CLASS_MALFORMED = "setting_non_boolean"


@st.composite
def _records(draw, *, only: str | None = None):
    """A pipeline record plus the class it belongs to and its raw setting value.

    ``only`` restricts the draw to one class, so a property whose antecedent is
    a single class exercises its assertion on every example rather than on the
    fraction that happens to land there.
    """
    record_class = only or draw(
        st.sampled_from([_CLASS_EXPLICIT_FALSE, _CLASS_EXPLICIT_TRUE, _CLASS_ABSENT, _CLASS_MALFORMED])
    )
    record = dict(draw(_record_padding()))
    raw: object = None
    has_raw = False

    if record_class == _CLASS_ABSENT:
        shape = draw(st.sampled_from(["no_approval_config", "config_not_a_dict", "key_missing"]))
        if shape == "config_not_a_dict":
            record["approval_config"] = draw(_non_dict_approval_configs())
        elif shape == "key_missing":
            record["approval_config"] = dict(draw(_other_approval_keys()))
        # "no_approval_config" leaves the key off entirely — the pre-change
        # record shape R7.1/R7.2 describe.
        event(f"absent shape: {shape}")
    else:
        if record_class == _CLASS_EXPLICIT_FALSE:
            raw = False
        elif record_class == _CLASS_EXPLICIT_TRUE:
            raw = True
        else:
            raw = draw(_non_boolean_values())
        has_raw = True
        approval_config = dict(draw(_other_approval_keys()))
        approval_config["code_generation_enabled"] = raw
        record["approval_config"] = approval_config

    return record, record_class, raw, has_raw


def _expected(record_class: str) -> tuple[bool, str]:
    """R1.2 / R1.4 / R7.4's answer for a record class, from the requirements."""
    if record_class == _CLASS_EXPLICIT_FALSE:
        return False, "pipeline_record"
    if record_class == _CLASS_EXPLICIT_TRUE:
        return True, "pipeline_record"
    # Absent (R1.2) and non-boolean (R1.4) both fail closed to True, and both
    # report the default as the source: the value did not come from the record.
    return True, "default"


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------


@settings(max_examples=300, deadline=None)
@given(case=_records())
def test_only_an_explicit_boolean_false_disables_the_gate(logs, case):
    """Property 1 over the whole record space — the iff, and R7.4's source.

    Validates: Requirements 1.2, 1.4, 1.5, 7.1, 7.4
    """
    record, record_class, raw, has_raw = case
    logs.reset()
    event(f"record class: {record_class}")

    setting, source = _mod.resolve_code_approval_setting(record)
    expected_setting, expected_source = _expected(record_class)

    # `is` on the result too: a returned 0/1 would satisfy `==` and then flow
    # into the Approval_Policy's disjunction as the opposite decision.
    assert setting is expected_setting, (
        f"expected {expected_setting!r} for a {record_class} record, got {setting!r} "
        f"(approval_config={record.get('approval_config')!r}) — R1.2/R1.4"
    )
    assert source == expected_source, (
        f"expected source {expected_source!r} for a {record_class} record, got {source!r} — R7.4"
    )

    # The disabling answer is reachable ONLY from an explicit boolean false.
    assert (setting is False) == (record_class == _CLASS_EXPLICIT_FALSE), (
        f"a {record_class} record produced setting={setting!r}: the gate is disabled by something "
        "other than the JSON boolean false — R1.4"
    )
    assert ((setting, source) == (False, "pipeline_record")) == (record_class == _CLASS_EXPLICIT_FALSE)

    warnings = logs.at_warning_or_above()
    if record_class == _CLASS_MALFORMED:
        assert warnings, (
            f"no WARNING-or-above record for the rejected raw value {raw!r} (type={type(raw).__name__}) — R1.5"
        )
    else:
        assert not warnings, (
            f"a {record_class} record logged at WARNING or above ({[r.getMessage() for r in warnings]}) — "
            "R1.5 covers the rejected non-boolean value only; warning on a well-formed record would make "
            "the malformed signal unreadable"
        )
    assert has_raw == (record_class != _CLASS_ABSENT)


@settings(max_examples=300, deadline=None)
@given(case=_records(only=_CLASS_MALFORMED))
def test_a_non_boolean_setting_is_logged_at_warning_naming_the_raw_value(logs, case):
    """R1.5, with the antecedent forced onto every example.

    The record must *name* the rejected value, so the assertion is on ``repr``:
    for a string setting, ``str()`` renders ``false`` — indistinguishable in the
    log from the JSON boolean the operator did not send — and the whole point of
    R1.5 is that an operator reading the line can tell which value was rejected.

    Validates: Requirements 1.4, 1.5
    """
    record, record_class, raw, _has_raw = case
    assert record_class == _CLASS_MALFORMED, "guard: `only` drew the wrong class"
    logs.reset()
    event(f"raw type: {type(raw).__name__}")

    setting, source = _mod.resolve_code_approval_setting(record)
    assert setting is True, f"a non-boolean setting {raw!r} did not fail closed — R1.4"
    assert source == "default", f"a rejected value must not be sourced to the record, got {source!r} — R7.4"

    warnings = logs.at_warning_or_above()
    assert len(warnings) == 1, f"expected exactly one WARNING-or-above record, got {len(warnings)} — R1.5"
    message = warnings[0].getMessage()
    assert repr(raw) in message, f"the WARNING does not name the rejected raw value {raw!r}: {message!r} — R1.5"
    assert type(raw).__name__ in message, (
        f"the WARNING does not name the rejected value's type ({type(raw).__name__}): {message!r} — R1.5. "
        "The type is what distinguishes the string 'false' from the boolean false for a reader."
    )


@settings(max_examples=200, deadline=None)
@given(case=_records(only=_CLASS_ABSENT))
def test_an_absent_setting_evaluates_as_true_with_no_migration(logs, case):
    """R1.2 and R7.1 — a pre-change record resolves on its own, silently.

    Every Pipeline_Record written before this change is in this class: nothing
    ever wrote ``approval_config.code_generation_enabled`` for them, and R7.1
    forbids a backfill. So this branch is not an edge case — it is the state of
    the whole table on the day the change ships.

    Validates: Requirements 1.2, 7.1, 7.4
    """
    record, record_class, _raw, has_raw = case
    assert record_class == _CLASS_ABSENT, "guard: `only` drew the wrong class"
    assert not has_raw
    logs.reset()

    setting, source = _mod.resolve_code_approval_setting(record)
    assert setting is True, f"an absent setting did not evaluate as true for {record!r} — R1.2"
    assert source == "default", f"an absent setting must report the default as its source, got {source!r} — R7.4"
    assert not logs.at_warning_or_above(), (
        "an absent setting is a normal pre-change record, not a rejected value — R1.5 does not apply to it"
    )


def test_the_source_sentinels_are_the_values_r74_logs():
    """The two sentinels design §4.1 names, pinned as the strings R7.4 reports.

    The properties above assert the string values directly, so this test is what
    keeps the module constants and those literals from drifting apart — the
    handler's R7.4 log line (task 2.4) reads the constants.

    Validates: Requirements 7.4
    """
    assert _mod._SETTING_FROM_RECORD == "pipeline_record"
    assert _mod._SETTING_FROM_DEFAULT == "default"
