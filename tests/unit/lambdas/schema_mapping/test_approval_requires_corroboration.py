# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""T-04 — a mapping is auto-approved only when something the model did not write
corroborates every entry.

**Function under test:** `determine_approval_requirement` in
`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py` (the name is not
unique in this repository; `test_this_is_the_schema_mapping_module` below asserts
which one this is).

The finding: every clause that can return `False` read `confidence` from the
model's own output and nothing range-checked it. One entry claiming `50` lifts the
average; `json.loads` accepts `NaN`, and `NaN < 0.85` is `False`; and text placed
in field names, sample values or the additional context can ask the model to
report high confidence. T-04 adds one clause that can only return `True`. It fires
when ANY entry in `mappings` fails either of:

(a) its `confidence` is a real number (not `bool`), finite, and `0 <= c <= 1`;
(b) it is corroborated deterministically — a usable `source_field` that, when the
    caller supplies the source schema's column names, is one of them; AND either
    the two names agree after lowercasing and removing non-alphanumerics, or the
    exact pair `source_field → target_field` is in the pipeline's operator-written
    `mapping_hints`.

**Expectations derive from that rule, not from running the function.** Every
`False` here is a case the rule admits and every `True` is one it rejects. Where a
`True` could also come from an earlier clause, the test asserts the T-04 log line
(`_T04_LOG`) to attribute it, and every call asserts `logger.error` was never
reached — the trailing `except Exception: return True` is the only `logger.error`
in the function, so a `True` manufactured by the swallow fails the test instead of
passing as a corroboration verdict (`LGR-b2f79ef-13`, template / ratified, is the
reasoning precedent on this module).

The `schema_mapping_enabled=False` opt-out is asserted to still win: an operator's
explicit configuration keeps the authority over this quality clause that it has
over every other one.
"""

import inspect
import json
import math
import os
import sys
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

# sys.modules.pop + sys.path.insert: `lambda_function` is a name many Lambda dirs
# share, so it cross-contaminates between test files. Every patch below is
# `patch.object(_mod, …)`, so nothing here depends on sys.modules still pointing
# at this module at call time.
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

_THRESHOLD = _mod.CONFIDENCE_AUTO_APPROVE_THRESHOLD

#: The T-04 clause's own log text. Present only when T-04 is the clause that
#: returned, so it attributes a `True` that an earlier clause could also explain.
_T04_LOG = "lack a valid confidence or deterministic corroboration"

_PIPELINE = "t04-pipeline"


# --- Fixtures -----------------------------------------------------------------


def _pipeline(*, enabled: bool = True, hints: dict | None = None) -> dict:
    record: dict = {
        "pipeline_id": _PIPELINE,
        "approval_config": {"schema_mapping_enabled": enabled, "always_approve_schema_changes": True},
    }
    if hints is not None:
        record["mapping_hints"] = hints
    return record


def _entry(source, target, confidence=0.95) -> dict:
    return {"source_field": source, "target_field": target, "confidence": confidence}


def _result(*entries: dict) -> dict:
    """Nothing unmapped on either side and a model id as provenance.

    So the fallback clause, both ratio clauses and the no-mappings clause have
    nothing to fire on; whether the average-confidence clause can fire is decided
    per test and stated there.
    """
    return {
        "mappings": list(entries),
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _with_honest_neighbours(entry: dict, count: int = 9) -> dict:
    """*entry* beside *count* exact-name entries at confidence 1.0.

    Nine neighbours at 1.0 keep the average at or above the threshold for every
    single bad entry this file draws whose confidence is finite and `>= -0.5`
    ((9 - 0.5) / 10 = 0.85), so the average clause cannot be what fires and the
    T-04 log line is the attribution.
    """
    neighbours = [_entry(f"col_{i}", f"col_{i}", 1.0) for i in range(count)]
    return _result(*neighbours, entry)


def _decide(
    mapping_result: dict,
    *,
    source_column_names: list[str] | None = None,
    pipeline: dict | None = None,
) -> tuple[bool, list[str]]:
    """The decision plus every `logger.info` message it emitted."""
    with (
        patch.object(_mod, "get_pipeline_metadata") as mock_get_meta,
        patch.object(_mod, "logger") as mock_logger,
    ):
        mock_get_meta.return_value = _pipeline() if pipeline is None else pipeline
        decision = _mod.determine_approval_requirement(
            _PIPELINE, mapping_result, None, source_column_names=source_column_names
        )
        assert mock_get_meta.called, "the approval configuration was never read — an early return, not a clause"
        mock_logger.error.assert_not_called()
        messages = [str(call.args[0]) for call in mock_logger.info.call_args_list if call.args]
    return decision, messages


def _forced_by_t04(messages: list[str]) -> bool:
    return any(_T04_LOG in message for message in messages)


# --- Which module this is ------------------------------------------------------


def test_this_is_the_schema_mapping_module():
    assert _mod.__file__.endswith("agentic_data_ingestion_schema_mapping/lambda_function.py"), _mod.__file__
    assert "source_column_names" in inspect.signature(_mod.determine_approval_requirement).parameters


# --- The control ---------------------------------------------------------------


def test_a_corroborated_high_confidence_mapping_still_auto_approves():
    """Non-vacuity: without this every `True` below could be an unconditional `True`."""
    decision, messages = _decide(
        _result(_entry("customer_id", "customer_id", 0.97), _entry("email", "email", 0.99)),
        source_column_names=["customer_id", "email"],
    )

    assert decision is False
    assert not _forced_by_t04(messages)


# --- (a) the confidence must be a probability ---------------------------------


@pytest.mark.parametrize("confidence", [1.0001, 1.5, 50, 10**6, -0.1, -0.5])
def test_an_out_of_range_confidence_requires_a_human(confidence):
    """One entry outside [0, 1] forces a human, whichever way it is out of range.

    `50` and `10**6` LIFT the average — that is the bypass; `-0.1` and `-0.5`
    stay inside what nine 1.0 neighbours absorb, so the average clause does not
    fire and the attribution is T-04's.
    """
    mapping_result = _with_honest_neighbours(_entry("target_col", "target_col", confidence))
    average = sum(m["confidence"] for m in mapping_result["mappings"]) / len(mapping_result["mappings"])
    assert average >= _THRESHOLD, "the average clause would fire first and this would prove nothing about T-04"

    decision, messages = _decide(mapping_result)

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_a_nan_confidence_parsed_from_model_json_requires_a_human():
    """The real path: `json.loads` accepts the bare `NaN` token a model can emit.

    The premise is asserted, not described: the average becomes NaN and the
    low-confidence comparison is `False`, so before T-04 this auto-approved.
    """
    body = '{"mappings": [{"source_field": "id", "target_field": "id", "confidence": NaN}]}'
    mapping_result = dict(_result(), mappings=json.loads(body)["mappings"])

    confidence = mapping_result["mappings"][0]["confidence"]
    assert math.isnan(confidence)
    assert not (confidence < _THRESHOLD), "NaN compares False — the hole T-04 closes"

    decision, messages = _decide(mapping_result)

    assert decision is True
    assert _forced_by_t04(messages), messages


@pytest.mark.parametrize("token", ["Infinity", "-Infinity"])
def test_an_infinite_confidence_requires_a_human(token):
    """`json.loads` accepts both infinities. `+inf` lifts the average; `-inf` sinks it.

    Only `+inf` reaches T-04 (`-inf` is already caught by the average clause), so
    the attribution is asserted for `+inf` alone and the decision for both.
    """
    body = f'{{"mappings": [{{"source_field": "id", "target_field": "id", "confidence": {token}}}]}}'
    mapping_result = dict(_result(), mappings=json.loads(body)["mappings"])
    assert math.isinf(mapping_result["mappings"][0]["confidence"])

    decision, messages = _decide(mapping_result)

    assert decision is True
    if token == "Infinity":
        assert _forced_by_t04(messages), messages


@pytest.mark.parametrize("confidence", [True, False])
def test_a_boolean_confidence_requires_a_human(confidence):
    """`bool` is an `int` subclass, so `True` would otherwise count as a perfect 1."""
    decision, messages = _decide(_with_honest_neighbours(_entry("target_col", "target_col", confidence)))

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_a_missing_confidence_requires_a_human():
    """Rule (a) asks for a real number; an absent key is not one.

    The average clause reads an absent confidence as 0.5, which nine 1.0
    neighbours absorb (0.95), so before T-04 this auto-approved.
    """
    mapping_result = _with_honest_neighbours({"source_field": "target_col", "target_field": "target_col"})

    decision, messages = _decide(mapping_result)

    assert decision is True
    assert _forced_by_t04(messages), messages


@pytest.mark.parametrize("confidence", [0, 0.0, _THRESHOLD, 1, 1.0])
def test_the_closed_unit_interval_is_accepted_at_both_ends(confidence):
    """`0 <= c <= 1` is inclusive, and an `int` is a real number.

    The neighbours carry the average; this pins only that T-04 does not reject
    an in-range value.
    """
    decision, messages = _decide(_with_honest_neighbours(_entry("target_col", "target_col", confidence)))

    assert decision is False
    assert not _forced_by_t04(messages)


# --- (b) the pairing must be corroborated -------------------------------------


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("cust_nm", "customer_name"),
        ("fname", "first_name"),
        ("id", "user_id"),
        ("email", "email_address"),
        ("dob", "date_of_birth"),
    ],
)
def test_a_semantic_match_goes_to_a_human_however_confident_the_model_is(source, target):
    """The deliberate consequence: a semantic pairing is the judgement injected text
    can steer, so model confidence alone no longer carries it past a human."""
    decision, messages = _decide(
        _result(_entry(source, target, 0.99)),
        source_column_names=[source],
    )

    assert decision is True
    assert _forced_by_t04(messages), messages
    assert target in next(m for m in messages if _T04_LOG in m)


def test_a_source_field_that_is_not_a_source_column_requires_a_human():
    """Names agreeing is not enough when the source column does not exist."""
    decision, messages = _decide(
        _result(_entry("customer_id", "customer_id", 0.99)),
        source_column_names=["cust_id", "email"],
    )

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_column_membership_is_exact_even_where_the_names_normalise_alike():
    """`ID` is not the column `id`: the generated job would read a column that is not there."""
    decision, messages = _decide(_result(_entry("ID", "id", 0.99)), source_column_names=["id"])

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_column_membership_is_not_checked_when_the_caller_supplies_no_columns():
    """`None` means no source schema in hand; the name rule still applies."""
    decision, messages = _decide(_result(_entry("customer_id", "customer_id", 0.99)), source_column_names=None)

    assert decision is False
    assert not _forced_by_t04(messages)


@pytest.mark.parametrize(
    "source_field",
    [None, "", "   ", 7, ["customer_id"]],
    ids=["None", "empty", "whitespace", "int", "list"],
)
def test_an_unusable_source_field_is_never_corroborated(source_field):
    """No usable source, nothing to corroborate — the `_has_usable_source_field` test."""
    decision, messages = _decide(_with_honest_neighbours(_entry(source_field, "customer_id", 1.0)))

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_an_absent_source_field_is_never_corroborated():
    decision, messages = _decide(_with_honest_neighbours({"target_field": "customer_id", "confidence": 1.0}))

    assert decision is True
    assert _forced_by_t04(messages), messages


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("email", "email"),
        ("first_name", "firstName"),
        ("First-Name", "first_name"),
        ("EMAIL", "email"),
        ("e_mail", "email"),
        ("Customer ID", "customer_id"),
    ],
)
def test_an_exact_or_normalised_name_match_is_corroborated(source, target):
    """Lowercased, non-alphanumerics removed: these pairs name the same field."""
    decision, messages = _decide(_result(_entry(source, target, 0.99)))

    assert decision is False
    assert not _forced_by_t04(messages)


def test_names_that_both_normalise_to_nothing_do_not_agree():
    """`__` and `--` share no letter or digit; an empty match is no match."""
    decision, messages = _decide(_result(_entry("__", "--", 0.99)))

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_normalisation_does_not_strip_non_ascii_letters_into_a_match():
    """An ASCII-only normaliser would turn `電話id` into `id` and call it a match."""
    decision, messages = _decide(_result(_entry("電話id", "id", 0.99)))

    assert decision is True
    assert _forced_by_t04(messages), messages


# --- (b) the operator's mapping hints -----------------------------------------


def test_an_operator_mapping_hint_corroborates_exactly_its_own_pair():
    """A hint is a human having already named this pairing."""
    decision, messages = _decide(
        _result(_entry("fname", "first_name", 0.99)),
        source_column_names=["fname"],
        pipeline=_pipeline(hints={"fname": "first_name"}),
    )

    assert decision is False
    assert not _forced_by_t04(messages)


def test_a_hint_for_a_different_target_does_not_corroborate():
    decision, messages = _decide(
        _result(_entry("fname", "first_name", 0.99)),
        source_column_names=["fname"],
        pipeline=_pipeline(hints={"fname": "last_name"}),
    )

    assert decision is True
    assert _forced_by_t04(messages), messages


def test_a_hint_does_not_excuse_a_source_field_that_is_not_a_column():
    """The hint corroborates the PAIRING; the column still has to exist."""
    decision, messages = _decide(
        _result(_entry("fname", "first_name", 0.99)),
        source_column_names=["first"],
        pipeline=_pipeline(hints={"fname": "first_name"}),
    )

    assert decision is True
    assert _forced_by_t04(messages), messages


@pytest.mark.parametrize("hints", ["fname=first_name", ["fname", "first_name"], None])
def test_hints_that_are_not_a_dict_corroborate_nothing(hints):
    pipeline = _pipeline()
    pipeline["mapping_hints"] = hints

    decision, messages = _decide(_result(_entry("fname", "first_name", 0.99)), pipeline=pipeline)

    assert decision is True
    assert _forced_by_t04(messages), messages


# --- What the clause does not change ------------------------------------------


def test_schema_mapping_enabled_false_still_auto_approves():
    """Operator authority: the opt-out returns before every quality clause, T-04 included.

    The mapping fails both (a) and (b), so if T-04 were placed above the
    opt-out this would be `True`.
    """
    mapping_result = _result(_entry("cust_nm", "customer_name", 50), _entry("id", "id", float("nan")))

    decision, messages = _decide(mapping_result, pipeline=_pipeline(enabled=False))

    assert decision is False
    assert not _forced_by_t04(messages)


def test_a_corroborated_mapping_below_the_threshold_still_requires_a_human():
    """T-04 only adds `True`: corroboration never rescues a low average."""
    decision, messages = _decide(_result(_entry("email", "email", 0.4)))

    assert decision is True
    assert not _forced_by_t04(messages), "the average clause fires first and logs its own reason"


def test_the_log_names_the_offending_target_fields_and_at_most_ten():
    """Twelve uncorroborated entries: the first ten are named, the rest are counted."""
    entries = [_entry(f"src_{i}", f"tgt_{i}", 0.99) for i in range(12)]

    decision, messages = _decide(_result(*entries))

    assert decision is True
    (line,) = [m for m in messages if _T04_LOG in m]
    assert "12 mapping(s)" in line
    for i in range(10):
        assert f"'tgt_{i}'" in line
    assert "'tgt_10'" not in line
    assert "'tgt_11'" not in line
