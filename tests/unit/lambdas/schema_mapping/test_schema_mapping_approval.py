# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for determine_approval_requirement with schema_change_action (Task 3.6).

Validates: AC-20.2, AC-20.5
"""

import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

# Set region so boto3 doesn't fail at import time
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")

# Add lambda directory to import path
_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)

# Stub out Lambda-layer dependencies (these modules only exist in the Lambda layer)
# Using direct sys.modules assignment since tests use @patch('lambda_function.xxx')
# decorators that need the module to remain importable
sys.modules.setdefault(
    "eventbridge_utils", MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock()))
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock())

sys.modules.pop("lambda_function", None)
# Re-insert to ensure our directory is at position 0 even if other test files added paths
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _schema_mapping_module
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

determine_approval_requirement = _schema_mapping_module.determine_approval_requirement


@pytest.fixture(autouse=True)
def _ensure_correct_lambda_module():
    """Ensure sys.modules['lambda_function'] points to schema_mapping's module.

    Other test files may replace sys.modules['lambda_function'] with their own
    Lambda's module. The @patch('lambda_function.xxx') decorators resolve the
    module from sys.modules at test execution time, so we must restore it.
    """
    sys.modules["lambda_function"] = _schema_mapping_module
    yield


# --- Helpers ---


def _high_confidence_mapping():
    """Mapping result with high confidence and no unmapped fields.

    AMENDED for T-04. The entries used to be keyed ``source``/``target``, which
    no reader in the module recognises — the decision only ever read their
    ``confidence``. T-04 makes every entry also need deterministic corroboration
    (a usable ``source_field`` whose name agrees with its ``target_field``), so
    the keys are now the real ones. The names are unchanged and already
    identical on both sides, so this is still the "nothing is wrong with this
    mapping" fixture every test below reads it as.
    """
    return {
        "mappings": [
            {"source_field": "id", "target_field": "id", "confidence": 0.95},
            {"source_field": "name", "target_field": "name", "confidence": 0.92},
            {"source_field": "email", "target_field": "email", "confidence": 0.98},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
    }


def _pipeline_with_approval_config(always_approve=True):
    """Pipeline metadata with approval_config."""
    return {
        "pipeline_id": "test-pipeline",
        "approval_config": {
            "schema_mapping_enabled": True,
            "schema_mapping_confidence_threshold": Decimal("0.85"),
            "code_generation_enabled": True,
            "always_approve_schema_changes": always_approve,
        },
    }


# --- Tests: schema change actions force approval ---


@patch("lambda_function.get_pipeline_metadata")
def test_create_new_pipeline_forces_approval(mock_get_meta):
    """schema_change_action='create_new_pipeline' returns True regardless of confidence."""
    mock_get_meta.return_value = _pipeline_with_approval_config(always_approve=True)
    result = determine_approval_requirement(
        "test-pipeline", _high_confidence_mapping(), schema_change_action="create_new_pipeline"
    )
    assert result is True


@patch("lambda_function.get_pipeline_metadata")
def test_update_existing_pipeline_forces_approval(mock_get_meta):
    """schema_change_action='update_existing_pipeline' returns True."""
    mock_get_meta.return_value = _pipeline_with_approval_config(always_approve=True)
    result = determine_approval_requirement(
        "test-pipeline", _high_confidence_mapping(), schema_change_action="update_existing_pipeline"
    )
    assert result is True


@patch("lambda_function.get_pipeline_metadata")
def test_remap_required_forces_approval(mock_get_meta):
    """schema_change_action='remap_required' returns True."""
    mock_get_meta.return_value = _pipeline_with_approval_config(always_approve=True)
    result = determine_approval_requirement(
        "test-pipeline", _high_confidence_mapping(), schema_change_action="remap_required"
    )
    assert result is True


@patch("lambda_function.get_pipeline_metadata")
def test_schema_change_respects_always_approve_false(mock_get_meta):
    """When always_approve_schema_changes=False, schema change does NOT force approval."""
    mock_get_meta.return_value = _pipeline_with_approval_config(always_approve=False)
    # Falls through to confidence check — high confidence → no approval
    result = determine_approval_requirement(
        "test-pipeline", _high_confidence_mapping(), schema_change_action="create_new_pipeline"
    )
    assert result is False


# --- Tests: existing confidence-based logic still works ---


@patch("lambda_function.get_pipeline_metadata")
def test_no_schema_change_high_confidence_no_approval(mock_get_meta):
    """schema_change_action=None with high confidence returns False (AC-20.5)."""
    mock_get_meta.return_value = _pipeline_with_approval_config()
    result = determine_approval_requirement("test-pipeline", _high_confidence_mapping(), schema_change_action=None)
    assert result is False


@patch("lambda_function.get_pipeline_metadata")
def test_use_existing_pipeline_high_confidence_no_approval(mock_get_meta):
    """schema_change_action='use_existing_pipeline' with high confidence returns False."""
    mock_get_meta.return_value = _pipeline_with_approval_config()
    result = determine_approval_requirement(
        "test-pipeline", _high_confidence_mapping(), schema_change_action="use_existing_pipeline"
    )
    assert result is False


@patch("lambda_function.get_pipeline_metadata")
def test_low_confidence_requires_approval(mock_get_meta):
    """Low confidence mapping still triggers approval even without schema change."""
    mock_get_meta.return_value = _pipeline_with_approval_config()
    low_confidence_mapping = {
        "mappings": [
            {"source": "id", "target": "identifier", "confidence": 0.4},
        ],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
    }
    result = determine_approval_requirement("test-pipeline", low_confidence_mapping, schema_change_action=None)
    assert result is True


@patch("lambda_function.get_pipeline_metadata")
def test_no_pipeline_metadata_defaults_to_approval(mock_get_meta):
    """When pipeline metadata is missing, default to requiring approval."""
    mock_get_meta.return_value = None
    result = determine_approval_requirement(
        "test-pipeline", _high_confidence_mapping(), schema_change_action="create_new_pipeline"
    )
    assert result is True


# ---------------------------------------------------------------------------
# p37 task 11.2 — C10 at the observed run's exact shape (R3.2, R3.3, R3.6)
#
# Everything below is an INSERTION. No assertion above this line is amended:
# the `determine_approval_requirement` calls above pass three positional
# arguments, so C10's optional fourth parameter (`target_schema`) defaults to
# `None` and its forcing clause is inert for every one of them. That is R3.6
# stated structurally rather than asserted.
#
# The observed run: pipeline `newtest-ab2a08a0`, run
# `run-2026-08-09-15-38-19-5c5f5f8a`. Eight target fields, `phone` with no
# source. The arithmetic below is MEASURED against the module, not estimated,
# and it is asserted here rather than described in a comment so that a change
# to either existing quality rule shows up as a failure in these tests.
#
#   | Shape                                    | unmapped-target ratio | avg_confidence |
#   |------------------------------------------|-----------------------|----------------|
#   | pre-C9 (`phone` inside `mappings`)       | 0/8   = 0.000         | 0.90625        |
#   | post-C9 (`phone` in unmapped_target)     | 1/8   = 0.125         | 0.95           |
#
# BOTH are asserted. The pre-C9 shape is today's live shape and is what makes
# the clause correct before task 11.3 lands. The post-C9 shape at 12.5% with a
# RAISED average confidence is precisely the auto-approve hole C9-alone would
# open — 12.5% is below the existing `> 0.2` rule, and 0.95 is above the 0.85
# auto-approve threshold, so both pre-existing quality rules point at
# auto-approve for exactly the mapping that loaded zero rows. These assertions
# are the proof that no intermediate state of the C10-before-C9 ordering made
# the observed bug MORE likely to pass unreviewed.
# ---------------------------------------------------------------------------

_OBSERVED_SOURCED_FIELDS = (
    "customer_id",
    "first_name",
    "last_name",
    "email",
    "address",
    "city",
    "signup_date",
)

# The eighth target field, the one the source cannot supply.
_OBSERVED_UNSOURCED_FIELD = "phone"

_OBSERVED_TARGET_PROPERTIES = {f: {"type": "string"} for f in (*_OBSERVED_SOURCED_FIELDS, _OBSERVED_UNSOURCED_FIELD)}


def _observed_target_schema(required=("customer_id", "email", "phone")):
    """The run's target schema. `required` is parameterised for the risk-6 cases."""
    schema = {"type": "object", "properties": dict(_OBSERVED_TARGET_PROPERTIES)}
    if required is not None:
        schema["required"] = list(required)
    return schema


def _observed_source_schema():
    return {"type": "object", "properties": {f: {"type": "string"} for f in _OBSERVED_SOURCED_FIELDS}}


def _seven_sourced_mappings():
    """The seven target fields the source did supply, each at 0.95 confidence."""
    return [
        {"source_field": f, "target_field": f, "confidence": 0.95, "transformation": "direct"}
        for f in _OBSERVED_SOURCED_FIELDS
    ]


def _observed_pre_c9_mapping(phone_entry=None):
    """Today's live shape: `phone` sits INSIDE `mappings` carrying no source.

    Eight `mappings` entries, `unmapped_target_fields` empty. `phone` at 0.6
    drags the average to 0.90625 — still above the 0.85 auto-approve threshold.
    """
    if phone_entry is None:
        # `source_field` ABSENT, which is what an unvalidated Bedrock response
        # produces when the model simply omits the key (`:796-808`).
        phone_entry = {"target_field": _OBSERVED_UNSOURCED_FIELD, "confidence": 0.6, "transformation": "direct"}
    return {
        "mappings": [*_seven_sourced_mappings(), phone_entry],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _observed_post_c9_mapping():
    """The shape C9's normaliser produces: `phone` moved to `unmapped_target_fields`.

    Seven `mappings` at 0.95 — so dropping the low-confidence entry RAISES the
    average to 0.95 — and one unmapped target field, i.e. 12.5%.
    """
    return {
        "mappings": _seven_sourced_mappings(),
        "unmapped_source_fields": [],
        "unmapped_target_fields": [
            {"field": _OBSERVED_UNSOURCED_FIELD, "default_value": None, "reason": "No matching source field"}
        ],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }


def _unmapped_target_ratio(mapping_result):
    """The ratio the existing `> 0.2` rule computes (`lambda_function.py:1132`)."""
    mappings = mapping_result["mappings"]
    unmapped_target = mapping_result["unmapped_target_fields"]
    return len(unmapped_target) / (len(mappings) + len(unmapped_target))


def _avg_confidence(mapping_result):
    """The average the existing confidence rule computes (`:1092`)."""
    mappings = mapping_result["mappings"]
    return sum(m.get("confidence", 0.5) for m in mappings) / len(mappings)


# --- R3.3: the observed run's exact arithmetic, in both shapes ---------------


@patch("lambda_function.get_pipeline_metadata")
def test_pre_c9_shape_both_existing_quality_rules_point_at_auto_approve(mock_get_meta):
    """Today's live shape: 0.000 unmapped-target ratio at 0.90625 confidence.

    This is the premise the forcing clause exists to overturn, asserted rather
    than assumed. With `phone` inside `mappings` there are ZERO unmapped target
    fields, so the `> 0.2` rule sees 0.000 — it is not merely under the
    threshold, the defect is invisible to it entirely.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    mapping_result = _observed_pre_c9_mapping()

    assert _unmapped_target_ratio(mapping_result) == 0.0
    assert _avg_confidence(mapping_result) == 0.90625
    assert _avg_confidence(mapping_result) > _schema_mapping_module.CONFIDENCE_AUTO_APPROVE_THRESHOLD

    # No target schema in hand ⇒ the C10 clause is inert. This used to return
    # the pre-C10 answer, `False` — the auto-approve this spec exists to stop.
    #
    # EXPECTED VALUE CHANGED for T-04, and the premise above is unaffected: the
    # three assertions before this one still show both pre-existing quality
    # rules (the 20% ratio and the average confidence) pointing at auto-approve.
    # What changed is that a later clause now also reads `mappings`: the `phone`
    # entry carries no `source_field`, so nothing deterministic corroborates it,
    # and T-04 sends it to a human even with no target schema in hand. So the
    # same hole C10 closes for a *required* field is now closed for this shape
    # by a second, independent clause — which is the direction this test's
    # docstring wants the answer to move, not away from it.
    assert determine_approval_requirement("test-pipeline", mapping_result, None) is True


@patch("lambda_function.get_pipeline_metadata")
def test_pre_c9_shape_required_unsourced_field_forces_a_human(mock_get_meta):
    """R3.3 at the live shape — the clause reads `mappings`, not just the unmapped list.

    Correct BEFORE task 11.3 lands, which is the whole reason C10 goes first.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    mapping_result = _observed_pre_c9_mapping()

    result = determine_approval_requirement("test-pipeline", mapping_result, None, _observed_target_schema())
    assert result is True


@patch("lambda_function.get_pipeline_metadata")
def test_post_c9_shape_forces_a_human_at_12_5_percent_and_high_confidence(mock_get_meta):
    """R3.3 at 1-of-8 — the auto-approve hole C9 alone would open.

    12.5% is below the existing `> 0.2` rule and 0.95 is above the 0.85
    threshold, so **both** pre-existing quality rules auto-approve. Only the
    C10 clause forces the human. Green at task 11.1 and it must STAY green
    through 11.3.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    mapping_result = _observed_post_c9_mapping()

    ratio = _unmapped_target_ratio(mapping_result)
    assert ratio == 0.125
    assert ratio <= 0.2, "12.5% must be UNDER the existing 20% rule or this test proves nothing"
    assert _avg_confidence(mapping_result) == 0.95
    assert _avg_confidence(mapping_result) > _schema_mapping_module.CONFIDENCE_AUTO_APPROVE_THRESHOLD

    # Without the schema: the hole. With it: a human.
    assert determine_approval_requirement("test-pipeline", mapping_result, None) is False
    assert determine_approval_requirement("test-pipeline", mapping_result, None, _observed_target_schema()) is True


@patch("lambda_function.get_pipeline_metadata")
def test_normalisation_raises_the_average_confidence_it_is_measured_against(mock_get_meta):
    """C9's side effect, asserted: removing the low-confidence entry RAISES the average.

    0.90625 → 0.95. This is why C9-first would make the observed run *more*
    likely to auto-approve, and it is measured here rather than argued.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    assert _avg_confidence(_observed_post_c9_mapping()) > _avg_confidence(_observed_pre_c9_mapping())


# --- R3.3: the clause is `required`-gated, the naming is not (risk 6) --------


@patch("lambda_function.get_pipeline_metadata")
def test_unsourced_target_field_outside_required_is_named_but_not_forced(mock_get_meta):
    """Risk 6: only the FORCING clause is `required`-gated.

    `phone` is unsourced but the schema does not declare it required, so it is
    named in the reader's output while the decision stays auto-approve.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    mapping_result = _observed_post_c9_mapping()
    schema = _observed_target_schema(required=["customer_id", "email"])

    assert _schema_mapping_module.unsourced_target_field_names(mapping_result, schema) == ["phone"]
    assert determine_approval_requirement("test-pipeline", mapping_result, None, schema) is False


@patch("lambda_function.get_pipeline_metadata")
def test_target_schema_with_no_required_array_at_all_does_not_force(mock_get_meta):
    """Risk 6's live-unverified case: an uploaded schema that populates no `required`.

    The clause is inert, nothing fails, and the field is still named. That is
    the state the design's fallback (widen the clause to all unsourced fields)
    would address, and this test is what would show it had been widened.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    mapping_result = _observed_post_c9_mapping()
    schema = _observed_target_schema(required=None)

    assert "required" not in schema
    assert _schema_mapping_module.unsourced_target_field_names(mapping_result, schema) == ["phone"]
    assert determine_approval_requirement("test-pipeline", mapping_result, None, schema) is False


@patch("lambda_function.get_pipeline_metadata")
def test_nothing_unsourced_leaves_the_decision_unchanged(mock_get_meta):
    """Every target field has a source ⇒ the clause adds nothing (still `False`)."""
    mock_get_meta.return_value = _pipeline_with_approval_config()
    fully_mapped = {
        "mappings": _seven_sourced_mappings(),
        "unmapped_source_fields": [],
        "unmapped_target_fields": [],
    }
    schema = {
        "type": "object",
        "properties": {f: {"type": "string"} for f in _OBSERVED_SOURCED_FIELDS},
        "required": list(_OBSERVED_SOURCED_FIELDS),
    }

    assert _schema_mapping_module.unsourced_target_field_names(fully_mapped, schema) == []
    assert determine_approval_requirement("test-pipeline", fully_mapped, None, schema) is False
    assert determine_approval_requirement("test-pipeline", fully_mapped, None, None) is False


# --- R3.6 / robustness: the reader is total ---------------------------------


@pytest.mark.parametrize(
    ("label", "phone_entry"),
    [
        ("source_field absent", {"target_field": "phone", "confidence": 0.6}),
        ("source_field None", {"source_field": None, "target_field": "phone", "confidence": 0.6}),
        ("source_field empty string", {"source_field": "", "target_field": "phone", "confidence": 0.6}),
        ("source_field whitespace", {"source_field": "   ", "target_field": "phone", "confidence": 0.6}),
        ("source_field non-string", {"source_field": 7, "target_field": "phone", "confidence": 0.6}),
    ],
)
@patch("lambda_function.get_pipeline_metadata")
def test_every_unusable_source_field_form_is_treated_as_unsourced(mock_get_meta, label, phone_entry):
    """ "Usable" is `isinstance(str)` and non-empty after `.strip()`, not truthiness.

    A mapping record is parsed JSON round-tripped through DynamoDB, so all five
    forms are reachable. The same test `code_assembly._generate_dqdl_rules`
    applies for the same judgement.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()
    mapping_result = _observed_pre_c9_mapping(phone_entry=phone_entry)

    assert _schema_mapping_module.unsourced_target_field_names(mapping_result, _observed_target_schema()) == [
        "phone"
    ], label
    assert determine_approval_requirement("test-pipeline", mapping_result, None, _observed_target_schema()) is True


_DEGENERATE_MAPPING_RESULTS = [
    ("empty dict", {}),
    ("both lists None", {"mappings": None, "unmapped_target_fields": None}),
    ("non-dict mapping elements", {"mappings": [None, 3, "x"], "unmapped_target_fields": [None, 7, ["a"]]}),
    ("bare-string and dict mix in unmapped list", {"unmapped_target_fields": [None, 7, ["a"], {"field": 3}]}),
    ("target_field None or blank", {"mappings": [{"target_field": None}, {"target_field": "   "}]}),
]


@pytest.mark.parametrize(("label", "mapping_result"), _DEGENERATE_MAPPING_RESULTS)
def test_unsourced_reader_is_total_on_degenerate_input(label, mapping_result):
    """The new single reader never raises — a mapping record is parsed JSON.

    Asserted against the reader directly rather than through the decision,
    because the decision has pre-existing crash sites of its own (see the next
    test) and conflating the two would let a regression in either hide behind
    the other.
    """
    assert _schema_mapping_module.unsourced_target_field_names(mapping_result, _observed_target_schema()) == [], label
    assert _schema_mapping_module.unsourced_target_field_names(mapping_result) == [], label
    assert _schema_mapping_module.unsourced_target_field_names(mapping_result, {}) == [], label


# Two of the degenerate shapes are excluded from the decision-level test below,
# and the exclusions ARE the finding rather than a convenience. Both crash in
# PRE-EXISTING arithmetic that runs before C10's clause at `:1115`, so both land
# in the trailing swallow:
#
#   "non-dict mapping elements"  → `:1092`  `m.get("confidence", 0.5)` on `None`
#                                  ⇒ AttributeError
#   "both lists None"            → `:1101`  `len(mappings)` where `mappings` is
#                                  `None` (the key is present holding `None`, so
#                                  `.get(..., [])` returns `None`)
#                                  ⇒ TypeError: object of type 'NoneType' has no len()
#
# C10's own reader handles both — `unsourced_target_field_names` guards with
# `or []` and with an `isinstance` test, which the test above asserts directly.
# Neither crash is C10's doing and neither is fixed here: this is a test task,
# and papering over them with a passing assertion is exactly what would hide
# them. Reported rather than absorbed.
_PRE_EXISTING_ARITHMETIC_CRASHES = ("non-dict mapping elements", "both lists None")


@pytest.mark.parametrize(
    ("label", "mapping_result"),
    [case for case in _DEGENERATE_MAPPING_RESULTS if case[0] not in _PRE_EXISTING_ARITHMETIC_CRASHES],
)
@patch("lambda_function.get_pipeline_metadata")
def test_degenerate_input_does_not_reach_the_error_swallow(mock_get_meta, label, mapping_result):
    """A provenance annotation must never be the reason a decision blows up.

    `logger.error` is asserted absent because the trailing
    `except Exception: return True` (`:1147-1150`) is the only `logger.error` in
    the function — so a `True` produced by the swallow fails this test rather
    than passing as the documented outcome (`LGR-78ce0b7-13`, template /
    ratified).

    See `_PRE_EXISTING_ARITHMETIC_CRASHES` above for the two shapes this does
    not cover and why.
    """
    mock_get_meta.return_value = _pipeline_with_approval_config()

    with patch.object(_schema_mapping_module, "logger") as mock_logger:
        determine_approval_requirement("test-pipeline", mapping_result, None, _observed_target_schema())
    mock_logger.error.assert_not_called()


def test_required_unsourced_fields_are_named_first():
    """Ordering (risk 6): required first, then the rest, order-preserving within each."""
    mapping_result = {
        "mappings": _seven_sourced_mappings()[:5],
        "unmapped_source_fields": [],
        "unmapped_target_fields": ["nickname", {"field": "phone"}, {"field": "email"}],
    }
    schema = _observed_target_schema(required=["phone", "email"])

    assert _schema_mapping_module.unsourced_target_field_names(mapping_result, schema) == [
        "phone",
        "email",
        "nickname",
    ]


def test_both_unmapped_target_field_element_shapes_are_read():
    """A bare string and a `{field: ...}` dict are both named, and deduped once.

    Two producers write this list and do not agree on the element shape; a
    Bedrock response is model output parsed as returned, so a bare string is
    reachable (`:740-746`).
    """
    mapping_result = {
        "mappings": [],
        "unmapped_source_fields": [],
        "unmapped_target_fields": ["phone", {"field": "  phone  "}, {"field": "email"}],
    }

    assert _schema_mapping_module.unsourced_target_field_names(mapping_result) == ["phone", "email"]


# --- R3.2: `create_approval_payload` names the fields -----------------------
#
# `create_approval_payload` is a CONFIRMED zero-coverage finding, not a
# CodeGraph false negative: searching `tests/` for the symbol returns hits only
# for the same-named `create_code` function. These are its first tests. C10 adds
# a new field, so this is a plain unit test and not a characterization test —
# there is nothing to preserve.
#
# KNOWN GAP, recorded here rather than asserted: on the `source_field`-ABSENT
# variant of the pre-C9 shape the function does not reach the payload dict at
# all. `field_mapping_details` indexes `m["source_field"]` directly (`:1192`),
# which raises `KeyError` and is caught by the function's own blanket
# `except Exception` (`:1258`), so the return degrades to
# `{approval_type, error, generated_by}` and `target_required` never reaches an
# approver. That indexing is PRE-EXISTING and is not C10's doing — but its
# consequence for R3.2 is, so it is named. It is deliberately NOT pinned as an
# expectation: `LGR-78ce0b7-13` (template / ratified) is the standing reason not
# to enshrine a blanket-except outcome as intended behaviour. The variants below
# carry an explicit `source_field: None`, which is the shape that reaches the
# payload.


def test_approval_payload_names_the_required_unsourced_target_fields():
    """R3.2: `unmapped_fields.target_required` NAMES the fields, not counts them."""
    payload = _schema_mapping_module.create_approval_payload(
        _observed_source_schema(),
        _observed_target_schema(),
        _observed_post_c9_mapping(),
        "source-hash",
        "target-hash",
    )

    assert payload["unmapped_fields"]["target_required"] == ["phone"]
    assert payload["unmapped_fields"]["target_unsourced"] == ["phone"]


def test_approval_payload_target_unsourced_names_all_of_them_required_first():
    """Risk 6 on the payload: name them all, rank the required ones first."""
    mapping_result = {
        "mappings": _seven_sourced_mappings()[:5],
        "unmapped_source_fields": [],
        "unmapped_target_fields": [{"field": "nickname"}, {"field": "phone"}],
        "generated_by": "us.anthropic.claude-sonnet-4-20250514-v1:0",
    }
    payload = _schema_mapping_module.create_approval_payload(
        _observed_source_schema(),
        _observed_target_schema(required=["phone"]),
        mapping_result,
        "source-hash",
        "target-hash",
    )

    assert payload["unmapped_fields"]["target_unsourced"] == ["phone", "nickname"]
    assert payload["unmapped_fields"]["target_required"] == ["phone"]


def test_approval_payload_requires_review_term_turns_the_live_shapes_false_into_true():
    """11.1's correction to the design's premise, asserted directly.

    The design premised `requires_review` as "already `True` because
    `len(unmapped_target) > 0`". That holds only AFTER C9. On the pre-C9 shape
    `unmapped_target` is EMPTY and the average confidence is 0.90625, so every
    pre-existing term is False and the flag evaluated **False** while the
    routing decision now returns True. The new term is what closes that
    contradiction, and its effect is isolated here by varying only whether the
    schema declares the field required.
    """
    mapping_result = _observed_pre_c9_mapping(
        phone_entry={"source_field": None, "target_field": "phone", "confidence": 0.6}
    )

    without_required = _schema_mapping_module.create_approval_payload(
        _observed_source_schema(),
        _observed_target_schema(required=None),
        mapping_result,
        "source-hash",
        "target-hash",
    )
    with_required = _schema_mapping_module.create_approval_payload(
        _observed_source_schema(),
        _observed_target_schema(),
        mapping_result,
        "source-hash",
        "target-hash",
    )

    # Every pre-existing term is False on this shape — shown, not asserted about.
    assert without_required["unmapped_fields"]["source"] == []
    assert without_required["unmapped_fields"]["target"] == []
    assert without_required["mapping_statistics"]["average_confidence"] == 0.906
    assert without_required["requires_review"] is False

    # One term, from the same single reader, flips it.
    assert with_required["unmapped_fields"]["target_required"] == ["phone"]
    assert with_required["requires_review"] is True


def test_approval_payload_requires_review_already_true_on_the_post_c9_shape():
    """After C9 the design's premise is correct, and the new term cannot reverse it.

    `len(unmapped_target) > 0` fires on its own here, so the flag is `True`
    whether or not the schema declares the field required. The term can only
    turn False into True, never the reverse.
    """
    mapping_result = _observed_post_c9_mapping()
    for schema in (_observed_target_schema(), _observed_target_schema(required=None)):
        payload = _schema_mapping_module.create_approval_payload(
            _observed_source_schema(), schema, mapping_result, "source-hash", "target-hash"
        )
        assert payload["requires_review"] is True
