# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The persisted reasoning count equals the count the ruleset was derived from.

p37 task 9.2, requirement 3.4 (design C11). The subject is the hoist task 9.1
landed in `lambda/agentic_data_ingestion_create_code/lambda_function.py`:

- `resolved_field_mappings = _extract_field_mappings(mapping_item)` (`:676`);
- `_generate_dqdl_rules(resolved_field_mappings, ...)` (`:680-683`) — the
  ruleset the approval payload shows and the template bakes into the job;
- `- Field mappings: {len(resolved_field_mappings)}` inside `agent_reasoning`
  (`:770`).

All three verified by content, not by line number, in the assertions below.

**Why this file exists when task 1.2 already asserts `R3.4`.**
`test_extract_field_mappings_characterization.py` asserts `R3.4` against the
*expression*: it lifts the `- Field mappings: ` slot out of the source with
`ast` and evaluates it. That is the right shape for a characterization window
— it went red before 9.1 and green after — but it stops at the caller's
locals. `R3.4`'s defect mattered because the false zero was **persisted on the
state row and rendered to approvers** (`requirements.md` Correction 1: a run
generated from eight mappings reported `Field mappings: 0`), not because it
appeared in a log. So this file drives the whole handler and reads the count
back out of the row `pipeline_runs_table.put_item` writes, where an approver
would meet it.

**How the tie to the ruleset's derivation is made, and why it is not a
count comparison.** The reasoning string reports a number of
**Field_Mapping_Entries**; the ruleset contains a number of **rules**, and
after C7 (task 8.1) those two are deliberately different — an
Unsourced_Target_Field contributes an entry and no rule. Eight entries with
`phone` unsourced yield `1 RowCount + 7 Completeness + 2 Uniqueness` = 10
rules, where the live pre-C7 run rendered 11. Comparing 8 against 10 would be
comparing two different things.

Instead the tie is made through the *derivation*: the persisted ruleset is
required to be **byte-identical** to `_generate_dqdl_rules` re-run over the
list `_extract_field_mappings` resolves from the same record, and the reported
count is required to equal `len` of that same list. Two facts about one list,
so the reasoning string and the approval payload's ruleset cannot be reporting
different mapping views. `test_the_count_and_the_ruleset_move_together` closes
the remaining door directly: it feeds a record whose variants disagree, so a
hoist that reached the string but not the ruleset argument (or vice versa)
makes the two move independently and fails there rather than passing review.

**Module identity.** The `patch.dict("sys.modules", _mods)` import block below
evicts `code_assembly` and `mapping_resolution` from `sys.modules` on exit, so
a later plain `import code_assembly` in the same pytest process binds a
*second, distinct* module object whose globals the handler never reads. Every
reference here goes through `_mod` for that reason — the same caution
`test_rule_contradiction_check.py` records. `LGR-b7d5490-1` (**backfilled**)
is the `sys.modules.pop` + `sys.path.insert` pattern this directory shares,
and why: `lambda_function` is a name 23 Lambda dirs share.

**`artifact_schema` is deliberately NOT stubbed.** `create_output_data` is a
pure validator (`shared/artifact_schema.py`), and the persisted `summary` and
`approval` this file reads are what it produces — a MagicMock returning `{}`
would make the whole assertion vacuous.

**Provenance.** `why` on `create_code/lambda_function.py` returns
`LGR-69ca78b-*` (p34's approval-gate work) and the documented 634-file
snapshot `LGR-0176ccc-1`; `why` on `mapping_resolution.py` returns the
snapshot alone. Nothing in the ledger governs `agent_reasoning` as a
persisted, user-visible claim — `decisions(concept="logging")` returns five
records of which the only on-point one, `LGR-78ce0b7-8` (**template /
ratified**), is about a `logger.error` that stays because it is the only place
a distinction survives. So `R3.4` rests on a code read, which is what
`requirements.md` §Code-intelligence already recorded. `LGR-d3c81fc-1`
(**backfilled**) is the commit that added the `mapping_result`-JSON-string
variant this test exercises, "so AI generates actual transform code instead of
passthrough". `LGR-3e45e65-1` (**template / ratified**) owns the region pin;
the `setdefault` calls below are no-ops kept for parity with the siblings.

Nothing here modifies production code, and nothing here amends a pre-existing
assertion.

**Validates: Requirements 3.4**
"""

import json
import os
import re
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_create_code_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_create_code",
    )
)
_shared_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _create_code_dir)
sys.path.insert(0, _shared_dir)

_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "agentcore_client": MagicMock(),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "parameter_extractor": MagicMock(
        extract_required_parameters=MagicMock(
            return_value={"required_params": [], "optional_params": [], "parameter_defaults": {}}
        )
    ),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

# Reached through the handler module, never re-imported. See "Module identity".
_ca = _mod.code_assembly
_extract_field_mappings = _mod._extract_field_mappings


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


RUN_ID = "run-c11"
PIPELINE_ID = "pipe-c11"
MAPPING_ID = "map-c11"

_TEMPLATE_TARGET_CONFIG = {
    "type": "s3_tables",
    "bucket_name": "b",
    "namespace_name": "n",
    "table_name": "t",
}

# A benign transform. C8's contradiction check is not what this file is about,
# and a contradiction would trigger the in-process regeneration re-call and
# make the assertions here read two generations deep for no reason.
_TRANSFORM = 'df_transformed = df.withColumn("phone", col("mobile"))'

# The observed run's mapping, as `requirements.md` Correction 1 establishes it:
# eight target fields for source columns `cust_id, fname, lname, email, mobile,
# created_date, state, premium_member`, with `phone` carrying NO source column —
# the Unsourced_Target_Field whose Completeness rule quarantined every row.
_OBSERVED_MAPPINGS = [
    {"target_field": "cust_id", "source_field": "cust_id", "confidence": 0.95},
    {"target_field": "first_name", "source_field": "fname", "confidence": 0.8},
    {"target_field": "last_name", "source_field": "lname", "confidence": 0.8},
    {"target_field": "email", "source_field": "email", "confidence": 0.95},
    {"target_field": "phone", "source_field": "", "confidence": 0.1, "notes": "no source column"},
    {"target_field": "created_date", "source_field": "created_date", "confidence": 0.9},
    {"target_field": "state", "source_field": "state", "confidence": 0.9},
    {"target_field": "premium_member", "source_field": "premium_member", "confidence": 0.9},
]
_OBSERVED_COUNT = 8


def _mapping_record(**overrides) -> dict:
    """A mappings-table record in the shape `store_mapping_separately` writes.

    `mapping_result` as a JSON string is the variant `_extract_field_mappings`
    documents as the **current** schema_mapping output
    (`mapping_resolution.py:48-49`) and the one the pre-9.1 inline count
    missed. Both schema hashes are present because the handler raises a
    `StepFailure` naming the absent one otherwise (`lambda_function.py:521`).
    """
    record = {
        "mapping_id": MAPPING_ID,
        "mapping_result": json.dumps({"mappings": _OBSERVED_MAPPINGS}),
        "source_schema_hash": "src-hash-c11",
        "target_schema_hash": "tgt-hash-c11",
    }
    record.update(overrides)
    return record


def _pre_9_1_inline_count(mapping_item: dict) -> int:
    """The expression the reasoning string used BEFORE the hoist, verbatim.

    `create_code/lambda_function.py:665` as it read then:

        mapping_item.get("field_mappings",
            mapping_item.get("mapping_config", {}).get("field_mappings", []))

    Two of the three storage variants. Reproduced here so the false zero is a
    computed value in the negative control below rather than a literal `0` a
    reader has to take on trust.
    """
    return len(mapping_item.get("field_mappings", mapping_item.get("mapping_config", {}).get("field_mappings", [])))


# ── Handler harness ─────────────────────────────────────────────────────────
#
# Same shape as `test_rule_contradiction_check.py` (task 8.5): the AI and AWS
# boundaries are stubbed, `generate_code` is deliberately NOT patched — the
# real render must run for the ruleset to exist at all — and the persisted row
# is read out of `put_item.call_args`.


class _nested:
    """Enter a mapping of named context managers, exit them in reverse.

    `__enter__` returns the dict of values the managers *yielded* (the mocks),
    keyed by the same names: `patch.object(...)` is a `_patch` object, not a
    mock, and asserting on the manager instead of on what it yields fails with
    `AttributeError`.
    """

    def __init__(self, managers: dict):
        self._managers = managers

    def __enter__(self) -> dict:
        self._entered = []
        values = {}
        for name, manager in self._managers.items():
            values[name] = manager.__enter__()
            self._entered.append(manager)
        return values

    def __exit__(self, *exc_info):
        for manager in reversed(self._entered):
            manager.__exit__(*exc_info)
        return False


def _state_table():
    return MagicMock(
        get_item=MagicMock(return_value={"Item": {"extracted_schema": '{"properties": {}}'}}),
        query=MagicMock(return_value={"Items": []}),
        put_item=MagicMock(),
    )


def _handler_patches(state_table, mapping_record):
    return {
        "get_pipeline_metadata": patch.object(
            _mod, "get_pipeline_metadata", return_value={"target_config": _TEMPLATE_TARGET_CONFIG}
        ),
        "pipeline_runs_table": patch.object(_mod, "pipeline_runs_table", state_table),
        "mappings_table": patch.object(
            _mod,
            "mappings_table",
            MagicMock(get_item=MagicMock(return_value={"Item": dict(mapping_record)})),
        ),
        "generated_code_table": patch.object(_mod, "generated_code_table", MagicMock()),
        "s3_client": patch.object(_mod, "s3_client", MagicMock()),
        "publish_pipeline_state_change": patch.object(_mod, "publish_pipeline_state_change"),
        "publish_approval_status_change": patch.object(_mod, "publish_approval_status_change"),
        "generate_transform_with_ai": patch.object(_mod, "generate_transform_with_ai", return_value=_TRANSFORM),
        "validate_generated_code": patch.object(
            _mod, "validate_generated_code", return_value={"is_valid": True, "errors": [], "summary": {}}
        ),
        "validate_pyspark_antipatterns": patch.object(_mod, "validate_pyspark_antipatterns", return_value=[]),
    }


def _run_handler(mapping_record: dict) -> tuple[dict, dict]:
    """Drive `lambda_handler` and return `(result, the persisted state row)`."""
    state_table = _state_table()
    with _nested(_handler_patches(state_table, mapping_record)):
        result = _mod.lambda_handler(
            {"run_id": RUN_ID, "pipeline_id": PIPELINE_ID, "mapping_id": MAPPING_ID},
            None,
        )
    state_row = state_table.put_item.call_args.kwargs["Item"]
    # The row read below must be the `create_code` row, not some other write
    # that happened to land last. Asserted rather than assumed so a future
    # handler that adds a write is a diagnosable failure here.
    assert state_row["step_name"] == _mod.StepName.CREATE_CODE
    return result, state_row


def _persisted_reasoning(state_row: dict) -> str:
    """The reasoning string as an approver meets it.

    `output_data.approval` is `create_approval_payload`'s return
    (`lambda_function.py:337-352`), and `strands_agent_reasoning` is the field
    the code-approval surfaces render. This is the persisted claim `R3.4` is
    about — not a log line.
    """
    return state_row["output_data"]["approval"]["strands_agent_reasoning"]


def _reported_field_mapping_count(reasoning: str) -> int:
    match = re.search(r"^- Field mappings: (\d+)$", reasoning, flags=re.MULTILINE)
    assert match is not None, (
        "No '- Field mappings: <n>' line in the persisted reasoning string. R3.4 is about that "
        f"recorded count; if the line was renamed this test must follow it rather than be deleted. "
        f"Reasoning was:\n{reasoning}"
    )
    return int(match.group(1))


def _ruleset_derived_from(entries: list[dict]) -> str:
    """`_generate_dqdl_rules` over `entries`, with the handler's own hint set.

    The handler passes `_uniqueness_hint_fields(target_config)`
    (`lambda_function.py:682`); the same call is made here against the same
    `target_config` so the only thing that can differ between this ruleset and
    the persisted one is the mapping list.
    """
    return _ca._generate_dqdl_rules(entries, unique_hint_fields=_ca._uniqueness_hint_fields(_TEMPLATE_TARGET_CONFIG))


# ── Preconditions ───────────────────────────────────────────────────────────


def test_this_file_and_the_handler_share_one_code_assembly_module():
    """Precondition for the byte-identity tie below.

    A second `code_assembly` object would mean the recomputed ruleset came
    from different code than the persisted one, silently.
    """
    assert _ca is sys.modules["lambda_function"].code_assembly
    assert _extract_field_mappings.__module__ == "mapping_resolution"


# ── R3.4 at the persisted row ───────────────────────────────────────────────


def test_persisted_reasoning_count_equals_the_count_the_ruleset_was_derived_from():
    """R3.4: one mapping view feeds both the reported count and the ruleset.

    The `mapping_result`-JSON-string variant — the one that produced
    `Field mappings: 0` for run `run-2026-08-09-15-38-19-5c5f5f8a`. The
    reported count equals `len` of the list `_extract_field_mappings` resolves,
    and the persisted ruleset is byte-identical to `_generate_dqdl_rules` over
    that same list, so the two cannot be describing different mapping views.

    **Validates: Requirements 3.4**
    """
    record = _mapping_record()
    resolved = _extract_field_mappings(record)
    assert len(resolved) == _OBSERVED_COUNT, "fixture drift, not the R3.4 defect"

    _result, state_row = _run_handler(record)

    reasoning = _persisted_reasoning(state_row)
    reported = _reported_field_mapping_count(reasoning)
    persisted_ruleset = state_row["output_data"]["approval"]["dqdl_rules"]

    # Non-vacuity: a ruleset really was derived on this run, so byte-identity
    # below is a claim about content rather than about two empty strings.
    assert persisted_ruleset, "no ruleset was persisted, so there is no derivation to tie the count to"

    # The tie. Same list, two facts: its length is what the reasoning reports,
    # and it is what the persisted ruleset was computed from.
    assert reported == len(resolved) == _OBSERVED_COUNT, (
        f"the persisted reasoning reports {reported} field mappings for a record the generated code "
        f"was derived from {len(resolved)} of (R3.4). Reasoning was:\n{reasoning}"
    )
    assert persisted_ruleset == _ruleset_derived_from(resolved), (
        "the persisted ruleset is not what _generate_dqdl_rules produces over the list whose length "
        "the reasoning reports, so the two consumers of the hoist have diverged (design C11)"
    )


def test_the_entry_count_and_the_rule_count_are_deliberately_different():
    """8 entries, 10 rules — and the reasoning reports entries, per R3.4.

    Stated as an assertion rather than left to a reader's arithmetic, because
    "the reported count does not match the number of rules" is the first thing
    that looks like a bug here and is in fact C7 working: an
    Unsourced_Target_Field contributes a Field_Mapping_Entry and no rule
    (`code_assembly.py:197-200`). `1 RowCount + 7 Completeness` — `phone`
    skipped — `+ 2 Uniqueness` (`cust_id` matches `.*_id`, `email` matches
    `email`) = 10, where the live pre-C7 run rendered 11.

    **Validates: Requirements 3.4**
    """
    record = _mapping_record()
    _result, state_row = _run_handler(record)

    reasoning = _persisted_reasoning(state_row)
    persisted_ruleset = state_row["output_data"]["approval"]["dqdl_rules"]

    assert _reported_field_mapping_count(reasoning) == _OBSERVED_COUNT
    assert persisted_ruleset.count("Completeness ") == _OBSERVED_COUNT - 1
    assert 'Completeness "phone"' not in persisted_ruleset
    assert persisted_ruleset.count("Uniqueness ") == 2
    assert persisted_ruleset.count("RowCount") == 1


def test_the_false_zero_is_gone_from_the_persisted_row():
    """The negative control: the pre-9.1 expression still yields 0 here.

    Without this, the assertion above would also pass on a caller that
    happened to report the right number by some other route. The record is the
    exact variant Correction 1 describes — `mapping_result` as a JSON string
    and nothing else — so the two-variant inline count returns 0 while the
    persisted count reads 8.

    **Validates: Requirements 3.4**
    """
    record = _mapping_record()
    assert _pre_9_1_inline_count(record) == 0, "fixture no longer reproduces Correction 1's shape"

    _result, state_row = _run_handler(record)

    assert _reported_field_mapping_count(_persisted_reasoning(state_row)) == _OBSERVED_COUNT


def test_the_count_and_the_ruleset_move_together():
    """A record whose variants disagree: both consumers must follow variant 1.

    This is the assertion task 9.2 exists for beyond the count itself. If the
    hoist reached the reasoning string but not `_generate_dqdl_rules`' argument
    — or the reverse — the two read different mapping views, and only a record
    where the views *differ* can tell. `field_mappings` (3 entries) takes
    precedence over `mapping_result` (8), so a correct hoist reports 3 AND
    persists the 3-entry ruleset; a split one reports 3 and persists the
    8-entry ruleset, or vice versa.

    **Validates: Requirements 3.4**
    """
    variant_1 = [
        {"target_field": "cust_id", "source_field": "cust_id", "confidence": 0.95},
        {"target_field": "email", "source_field": "email", "confidence": 0.95},
        {"target_field": "state", "source_field": "state", "confidence": 0.9},
    ]
    record = _mapping_record(field_mappings=variant_1)

    resolved = _extract_field_mappings(record)
    assert resolved == variant_1, "precedence changed; this test's premise is variant 1 winning"

    _result, state_row = _run_handler(record)

    reported = _reported_field_mapping_count(_persisted_reasoning(state_row))
    persisted_ruleset = state_row["output_data"]["approval"]["dqdl_rules"]

    assert reported == len(variant_1) == 3
    assert persisted_ruleset == _ruleset_derived_from(variant_1)
    # And explicitly NOT the other variant's ruleset — the split-hoist failure
    # named in the docstring, asserted rather than inferred from equality above.
    assert persisted_ruleset != _ruleset_derived_from(_OBSERVED_MAPPINGS)


def test_a_record_with_no_resolvable_mappings_reports_an_honest_zero():
    """0 stays reachable when it is true, and the ruleset is empty with it.

    The fix is "report the resolved count", not "report eight". A record whose
    `mapping_result` carries an empty `mappings` list resolves to `[]`
    (`mapping_resolution.py:68`), `_generate_dqdl_rules` returns `""` for it
    (`code_assembly.py:158-159`), and both persisted facts agree on zero.

    **Validates: Requirements 3.4**
    """
    record = _mapping_record(mapping_result=json.dumps({"mappings": []}))
    assert _extract_field_mappings(record) == []

    _result, state_row = _run_handler(record)

    assert _reported_field_mapping_count(_persisted_reasoning(state_row)) == 0
    assert state_row["output_data"]["approval"]["dqdl_rules"] == ""
