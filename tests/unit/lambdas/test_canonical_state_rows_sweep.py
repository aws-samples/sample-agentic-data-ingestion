# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Canonical step-name sweep over EVERY state-row-writing Lambda (P28 R8.5).

Task 14.2 requires that state records are written with canonical names on
success AND failure paths for all ten task-step Lambdas. Focused behavioral
tests cover individual writers (e.g. create_code failure, schema_mapping /
schema_extract / schema_compare failure records, run_pipeline token rows,
file_validation rows); this sweep closes the remaining surface by statically
analyzing every writer's source (AST — comments/strings can't false-positive):

1. Every state-row payload dict (a dict literal carrying ``step_name``
   alongside ``status``/``step_sequence``) resolves to the writer's OWN
   canonical step name — a writer can neither write a legacy-variant row nor
   mislabel a row as another step.
2. Each writer has BOTH its success-record and failure-record write sites
   (>= 2 payload dicts), so neither path can silently lose its record.
3. Every ``step_name`` value referenced anywhere in the file (including
   cross-step ``Key=`` reads) is spelled canonically.
4. The retired legacy state-row names ("CodeGeneration", "CodeTesting")
   appear in no writer as string constants — EXCEPT as CloudWatch
   ``stage_name=`` dimension values, which are a separate naming surface
   (Pipeline.StageExecution metric-history continuity) deliberately left
   untouched by the state-row rename.

Validates: P28 Requirements 8.2, 8.5
"""

import ast
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")))

from step_names import STEP_SEQUENCE, TASK_STEPS, StepName, from_any

LAMBDA_ROOT = Path(__file__).resolve().parents[3] / "lambda"

# TASK_STEPS canonical values are exactly the writer Lambdas' dir suffixes.
WRITER_SOURCES = {step: LAMBDA_ROOT / f"agentic_data_ingestion_{step}" / "lambda_function.py" for step in TASK_STEPS}

# Legacy state-row names retired by task 14.2 — must not appear as string
# constants in any writer (exact match; AST constants exclude comments).
RETIRED_ROW_NAMES = {"CodeGeneration", "CodeTesting"}


def _parse(step: StepName) -> ast.Module:
    return ast.parse(WRITER_SOURCES[step].read_text())


def _resolve_step_name_value(node: ast.expr) -> str | None:
    """Resolve a step_name value node: string literal or StepName.X attribute."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "StepName"
        and hasattr(StepName, node.attr)
    ):
        return str(getattr(StepName, node.attr))
    return None


def _dict_entries(node: ast.Dict) -> dict[str, ast.expr]:
    """String-keyed entries of a dict literal (dynamic keys skipped)."""
    entries: dict[str, ast.expr] = {}
    for key, value in zip(node.keys, node.values, strict=True):
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            entries[key.value] = value
    return entries


def _dict_assignments(tree: ast.Module) -> dict[str, ast.Dict]:
    """Name → dict literal, for state-row attribute dicts held in a variable."""
    assignments: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    assignments[target.id] = node.value
    return assignments


def _update_item_payloads(tree: ast.Module) -> list[dict[str, ast.expr]]:
    """State-ROW payloads assembled across an ``update_item`` call.

    A writer that SETs its row instead of ``put_item``-ing it splits the payload
    in two: the key attributes go in ``Key=`` (they cannot appear in a SET
    expression) and the rest go in the attribute dict. Neither half carries
    ``step_name`` AND ``status``/``step_sequence`` on its own, so the dict-literal
    detector above cannot see it — the payload has to be reassembled from the
    call.

    ``test_code`` writes this way on purpose: a full-row ``PutItem`` on the
    ``(run_id, test_code)`` item deleted the ``regeneration_feedback`` that
    ``store_regeneration_feedback`` had written to the same item earlier in the
    same invocation. The R8.5 obligation is unchanged — both the success and the
    failure path must still write a record — so the detector follows the write
    shape rather than the fence being relaxed.
    """
    assignments = _dict_assignments(tree)
    payloads = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        if node.func.attr != "update_item":
            continue
        key_dict = next((kw.value for kw in node.keywords if kw.arg == "Key" and isinstance(kw.value, ast.Dict)), None)
        if key_dict is None:
            continue

        entries = dict(_dict_entries(key_dict))
        # The SET attributes: a ``**builder(attrs)`` unpack, a ``**attrs``
        # unpack, or an inline dict — resolved through a local name if needed.
        for kw in node.keywords:
            if kw.arg is not None:
                continue
            attrs: ast.expr | None = kw.value
            if isinstance(attrs, ast.Call) and attrs.args:
                attrs = attrs.args[0]
            if isinstance(attrs, ast.Name):
                attrs = assignments.get(attrs.id)
            if isinstance(attrs, ast.Dict):
                entries.update(_dict_entries(attrs))

        if "step_name" in entries and ("status" in entries or "step_sequence" in entries):
            payloads.append(entries)
    return payloads


def _state_row_payloads(tree: ast.Module) -> list[dict[str, ast.expr]]:
    """Dict literals that are state-ROW payloads: step_name + status or
    step_sequence (Key= lookup dicts carry only run_id + step_name), plus rows
    assembled across an ``update_item`` call (see ``_update_item_payloads``)."""
    payloads = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            entries = _dict_entries(node)
            if "step_name" in entries and ("status" in entries or "step_sequence" in entries):
                payloads.append(entries)
    return payloads + _update_item_payloads(tree)


def _all_step_name_values(tree: ast.Module) -> list[str]:
    """Every resolvable step_name value in the file (rows AND Key= reads)."""
    values = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            entries = _dict_entries(node)
            if "step_name" in entries:
                resolved = _resolve_step_name_value(entries["step_name"])
                if resolved is not None:
                    values.append(resolved)
    return values


@pytest.mark.parametrize("step", TASK_STEPS, ids=[str(s) for s in TASK_STEPS])
class TestCanonicalStateRowsSweep:
    def test_writer_source_exists(self, step):
        assert WRITER_SOURCES[step].is_file(), f"writer source missing for {step}"

    def test_every_state_row_payload_carries_the_writers_own_canonical_name(self, step):
        """Rows a Lambda writes are its own, spelled canonically (R8.2/R8.5)."""
        payloads = _state_row_payloads(_parse(step))
        assert payloads, f"{step}: no state-row payload dicts found — detector or writer changed"
        for entries in payloads:
            value = _resolve_step_name_value(entries["step_name"])
            assert value is not None, f"{step}: state-row step_name is not a literal/StepName reference"
            assert value == str(step), f"{step}: state row written with step_name {value!r}"
            # Canonical spelling, not merely a parseable variant
            assert str(from_any(value)) == value

    def test_success_and_failure_record_sites_both_present(self, step):
        """>= 2 payload sites: the success record AND the failure record."""
        payloads = _state_row_payloads(_parse(step))
        assert len(payloads) >= 2, (
            f"{step}: only {len(payloads)} state-row write site(s) — success and failure records both required"
        )

    def test_step_sequence_literals_match_shared_table(self, step):
        """Payloads carrying a literal step_sequence use the shared value."""
        for entries in _state_row_payloads(_parse(step)):
            seq_node = entries.get("step_sequence")
            if isinstance(seq_node, ast.Constant) and isinstance(seq_node.value, int):
                assert seq_node.value == STEP_SEQUENCE[step], (
                    f"{step}: literal step_sequence {seq_node.value} != shared {STEP_SEQUENCE[step]}"
                )

    def test_every_step_name_reference_is_canonical(self, step):
        """Cross-step Key= reads must also use canonical spellings."""
        for value in _all_step_name_values(_parse(step)):
            assert str(from_any(value)) == value, f"{step}: step_name reference {value!r} is not the canonical spelling"

    def test_no_retired_legacy_row_names_in_source(self, step):
        """'CodeGeneration'/'CodeTesting' string constants are gone —
        except as CloudWatch stage_name= dimension values (a separate
        naming surface kept for metric-history continuity)."""
        tree = _parse(step)
        cw_stage_constants = {
            id(kw.value)
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            for kw in node.keywords
            if kw.arg == "stage_name" and isinstance(kw.value, ast.Constant)
        }
        found = {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value in RETIRED_ROW_NAMES and id(node) not in cw_stage_constants
        }
        assert not found, f"{step}: retired legacy step-name literals present: {found}"


class TestSweepCoversAllWriters:
    def test_task_steps_is_the_full_writer_set(self):
        """The sweep parametrization covers all ten state-row writers."""
        assert len(TASK_STEPS) == 10
        assert {str(s) for s in TASK_STEPS} == {
            "file_validation",
            "schema_extract",
            "schema_compare",
            "schema_mapping",
            "create_code",
            "test_code",
            "create_infrastructure",
            "create_glue_pipeline",
            "create_quality_checks",
            "run_pipeline",
        }
