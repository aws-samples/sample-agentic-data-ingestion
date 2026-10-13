# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: uploaded schema format matches what schema_extract Lambda expects.

Validates: AC-12.1, AC-12.4

The SchemaUploadWizard produces a JSON Schema object via onSchemaReady():
  { properties: { field_name: { type, description } }, required: [...] }

The schema_extract Lambda (extract_json_schema) expects data with:
  - "properties" dict where each value has a "type" key
  - OR an array of objects (inferred from first item)

This test verifies the frontend output is compatible with the Lambda's
schema storage format (extracted_schema in DynamoDB).
"""

import json


def _build_uploaded_schema(fields):
    """Simulate the schema blob built by SchemaManagementPage.handleSchemaReady().

    This mirrors the frontend code:
        const schemaBlob = {
            properties: Object.fromEntries(
                fields.map(f => [f.name, { type: f.type, description: f.description }])
            ),
            required: fields.filter(f => !f.nullable).map(f => f.name),
        };
    """
    return {
        "properties": {f["name"]: {"type": f["type"], "description": f["description"]} for f in fields},
        "required": [f["name"] for f in fields if not f["nullable"]],
    }


class TestSchemaUploadContract:
    """Verify the schema format produced by the frontend matches what the
    schema_extract Lambda stores and downstream Lambdas consume."""

    def test_uploaded_schema_has_properties_dict(self):
        """The uploaded schema must have a 'properties' key with a dict value."""
        fields = [
            {"name": "id", "type": "integer", "nullable": False, "description": "Primary key"},
            {"name": "email", "type": "string", "nullable": True, "description": ""},
        ]
        schema = _build_uploaded_schema(fields)

        assert "properties" in schema
        assert isinstance(schema["properties"], dict)

    def test_each_property_has_type_key(self):
        """Each property in the schema must have a 'type' string — this is what
        schema_extract and schema_mapping Lambdas read."""
        fields = [
            {"name": "amount", "type": "number", "nullable": True, "description": "Dollar amount"},
            {"name": "active", "type": "boolean", "nullable": False, "description": ""},
        ]
        schema = _build_uploaded_schema(fields)

        for field_name, field_def in schema["properties"].items():
            assert "type" in field_def, f"Property '{field_name}' missing 'type'"
            assert isinstance(field_def["type"], str), f"Property '{field_name}' type must be string"

    def test_required_array_contains_non_nullable_fields(self):
        """The 'required' array lists fields where nullable=false."""
        fields = [
            {"name": "id", "type": "integer", "nullable": False, "description": ""},
            {"name": "name", "type": "string", "nullable": False, "description": ""},
            {"name": "notes", "type": "string", "nullable": True, "description": ""},
        ]
        schema = _build_uploaded_schema(fields)

        assert "required" in schema
        assert isinstance(schema["required"], list)
        assert "id" in schema["required"]
        assert "name" in schema["required"]
        assert "notes" not in schema["required"]

    def test_schema_is_valid_json_serializable(self):
        """The schema must be JSON-serializable (stored as extracted_schema in DynamoDB)."""
        fields = [
            {"name": "col_a", "type": "string", "nullable": True, "description": "Column A"},
        ]
        schema = _build_uploaded_schema(fields)

        serialized = json.dumps(schema)
        deserialized = json.loads(serialized)
        assert deserialized == schema

    def test_field_names_become_property_keys(self):
        """Field names from the preview table become top-level keys in properties."""
        fields = [
            {"name": "user_id", "type": "integer", "nullable": False, "description": ""},
            {"name": "created_at", "type": "string", "nullable": True, "description": "ISO timestamp"},
        ]
        schema = _build_uploaded_schema(fields)

        assert "user_id" in schema["properties"]
        assert "created_at" in schema["properties"]

    def test_description_preserved_in_property(self):
        """Field descriptions from the preview table are stored in each property."""
        fields = [
            {"name": "price", "type": "number", "nullable": True, "description": "Unit price in USD"},
        ]
        schema = _build_uploaded_schema(fields)

        assert schema["properties"]["price"]["description"] == "Unit price in USD"

    def test_empty_fields_produces_empty_properties(self):
        """An empty field list produces a valid schema with empty properties."""
        schema = _build_uploaded_schema([])

        assert schema == {"properties": {}, "required": []}

    def test_schema_compatible_with_extract_json_schema_output(self):
        """The uploaded schema structure matches what extract_json_schema() produces.

        extract_json_schema returns:
            { "type": "object", "properties": { "field": { "type": "..." } } }

        The uploaded schema has the same 'properties' structure, so downstream
        Lambdas (schema_compare, schema_mapping) can consume either format.
        """
        fields = [
            {"name": "id", "type": "integer", "nullable": False, "description": "PK"},
            {"name": "value", "type": "string", "nullable": True, "description": ""},
        ]
        uploaded = _build_uploaded_schema(fields)

        # Simulate what extract_json_schema would produce for similar data
        extracted = {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "value": {"type": "string"},
            },
        }

        # Both must have 'properties' with matching field names and types
        for field_name in extracted["properties"]:
            assert field_name in uploaded["properties"], f"Uploaded schema missing field '{field_name}'"
            assert uploaded["properties"][field_name]["type"] == extracted["properties"][field_name]["type"], (
                f"Type mismatch for '{field_name}'"
            )
