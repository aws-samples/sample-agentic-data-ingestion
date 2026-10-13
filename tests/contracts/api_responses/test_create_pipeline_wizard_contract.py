# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: POST body from wizard matches backend expected schema including approval_config.

Validates: AC-6.5
Ensures the wizard's submission payload is compatible with the backend create_pipeline handler.

p64 (authorised addition to task 2a) — ``approval_config`` below was **camelCase**
and is now **snake_case with a fractional threshold**, because the wizard's submit
site was changed to serialise rather than pass its own state through. p63 R1.4
called for exactly this edit and named the reason: as camelCase this was a
*passing* test pinning a payload the backend could not read.

**This file transcribes the payload, so on its own it cannot detect the wizard
drifting away from it** — every assertion here is about a literal defined in this
module. That weakness is p63 R1.4's underlying complaint and it is not fixed by
correcting the literal. The pin that does catch drift is
``tests/contracts/test_approval_config_casing_cross_language.py``, which reads the
keys out of ``CreatePipelinePage.tsx``'s serialiser and out of both Python reader
sites and asserts the two agree. Amend that file too if this shape changes.
"""

import json

# The wizard sends this shape from CreatePipelinePage.tsx handleSubmit
WIZARD_PAYLOAD = {
    "pipeline_name": "my-test-pipeline",
    "description": "A pipeline created via the wizard",
    "target_type": "glue_catalog",
    "target_config": {
        "database_name": "mydb",
        "table_name": "users",
    },
    "schema": [
        {"name": "user_id", "type": "bigint", "nullable": False, "description": "Primary key"},
        {"name": "email", "type": "string", "nullable": True, "description": "Email address"},
    ],
    "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
    # p64 — `buildApprovalConfigRequest(state.approvalConfig)`, not
    # `state.approvalConfig`. snake_case because that is what every reader reads,
    # and the threshold is the slider's percent DIVIDED BY 100 because the stored
    # attribute's unit is a fraction (`_default_approval_config` seeds
    # `Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))` = 0.85).
    #
    # `always_approve_schema_changes` keeps the name the readers use TODAY. p63 C4
    # proposes renaming it to `require_approval_on_schema_change`; that is a
    # separate change with a reader-side fallback, and this file is not the place
    # to half-adopt it.
    "approval_config": {
        "schema_mapping_enabled": True,
        "schema_mapping_confidence_threshold": 0.8,
        "code_generation_enabled": True,
        "always_approve_schema_changes": True,
    },
}

# The backend create_pipeline handler expects these required fields
REQUIRED_BACKEND_FIELDS = ["pipeline_name", "ai_model"]


class TestWizardPayloadContract:
    """Verify the wizard POST body matches what the backend expects."""

    def test_payload_has_required_fields(self):
        """Wizard payload must include all fields required by create_pipeline."""
        for field in REQUIRED_BACKEND_FIELDS:
            assert field in WIZARD_PAYLOAD, f"Missing required field: {field}"

    def test_pipeline_name_is_string(self):
        """pipeline_name must be a non-empty string."""
        assert isinstance(WIZARD_PAYLOAD["pipeline_name"], str)
        assert len(WIZARD_PAYLOAD["pipeline_name"]) > 0

    def test_ai_model_is_string(self):
        """ai_model must be a non-empty string."""
        assert isinstance(WIZARD_PAYLOAD["ai_model"], str)
        assert len(WIZARD_PAYLOAD["ai_model"]) > 0

    def test_description_is_optional_string(self):
        """description should be a string (optional)."""
        assert isinstance(WIZARD_PAYLOAD["description"], str)

    def test_target_type_is_valid(self):
        """target_type must be one of the supported types."""
        valid_types = {"s3_tables", "glue_catalog", "jdbc", "s3_parquet"}
        assert WIZARD_PAYLOAD["target_type"] in valid_types

    def test_target_config_is_dict(self):
        """target_config must be a dictionary."""
        assert isinstance(WIZARD_PAYLOAD["target_config"], dict)

    def test_schema_is_list_of_field_objects(self):
        """schema must be a list of objects with name, type, nullable, description."""
        schema = WIZARD_PAYLOAD["schema"]
        assert isinstance(schema, list)
        assert len(schema) > 0
        for field in schema:
            assert "name" in field
            assert "type" in field
            assert "nullable" in field
            assert "description" in field

    def test_approval_config_present(self):
        """approval_config must be present in the wizard payload."""
        assert "approval_config" in WIZARD_PAYLOAD

    def test_approval_config_has_required_keys(self):
        """approval_config must have all required keys.

        p64 — the four key names are **amended, not added to**: they were the
        wizard's camelCase state field names and are now the snake_case attributes
        `schema_mapping/lambda_function.py:1657,:1669` and
        `test_code.resolve_code_approval_setting` actually read. The old spelling
        was legal JSON that reached no reader, which is why this assertion passed
        while the toggles did nothing.
        """
        config = WIZARD_PAYLOAD["approval_config"]
        required_keys = [
            "schema_mapping_enabled",
            "schema_mapping_confidence_threshold",
            "code_generation_enabled",
            "always_approve_schema_changes",
        ]
        for key in required_keys:
            assert key in config, f"Missing approval_config key: {key}"

    def test_approval_config_types(self):
        """approval_config values must have correct types."""
        config = WIZARD_PAYLOAD["approval_config"]
        assert isinstance(config["schema_mapping_enabled"], bool)
        assert isinstance(config["code_generation_enabled"], bool)
        assert isinstance(config["always_approve_schema_changes"], bool)
        assert isinstance(config["schema_mapping_confidence_threshold"], (int, float))
        # p64 — the range is **amended from `0 <= v <= 100` to `0 <= v <= 1`**, and
        # that is the substantive half of this file's change rather than a
        # tightening. The wizard slider is a percent; the persisted attribute is a
        # fraction, set by its only writer (`_default_approval_config` seeds
        # `Decimal("0.85")`). Renaming the key and keeping the percent would have
        # put two units under one attribute name and made an 80% slider read as
        # "never auto-approve" to any fraction-comparing reader.
        #
        # Asserted as a closed interval on purpose: `<= 1` is a legal "require
        # perfect confidence", and `0` a legal "auto-approve anything". Whether a
        # per-pipeline value may sit BELOW the shared floor of 0.85 is p63 R9/D2's
        # decision (stricter-only, refused rather than clamped) and is deliberately
        # not asserted here — the wizard's own default of 80% is 0.8, which that
        # decision would refuse.
        assert 0 <= config["schema_mapping_confidence_threshold"] <= 1

    def test_payload_serializes_to_valid_json(self):
        """The full payload must be JSON-serializable."""
        serialized = json.dumps(WIZARD_PAYLOAD)
        deserialized = json.loads(serialized)
        assert deserialized == WIZARD_PAYLOAD

    def test_payload_without_approval_config_still_has_core_fields(self):
        """Even without approval_config, the core fields should be present."""
        minimal = {k: v for k, v in WIZARD_PAYLOAD.items() if k != "approval_config"}
        for field in REQUIRED_BACKEND_FIELDS:
            assert field in minimal
