# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test for S3 Object Created event format (EventBridge format).

Validates that the S3 notification event fixture matches the JSON Schema
and that missing required fields cause validation failure.

Task 8.2 — Requirement 5.1
"""

import copy
import json
from pathlib import Path

import jsonschema
import pytest
from tests.contracts.event_schemas.schemas import S3_OBJECT_CREATED_SCHEMA

FIXTURES_DIR = Path(__file__).resolve().parent.parent.parent / "fixtures"


def _load_fixture(name: str) -> dict:
    with open(FIXTURES_DIR / name, encoding="utf-8") as f:
        return json.load(f)


@pytest.fixture
def s3_event() -> dict:
    return _load_fixture("events/s3_object_created.json")


class TestS3NotificationContract:
    """S3 Object Created event matches the expected EventBridge schema."""

    def test_fixture_validates_against_schema(self, s3_event):
        jsonschema.validate(s3_event, S3_OBJECT_CREATED_SCHEMA)

    def test_detail_bucket_name_present(self, s3_event):
        assert "name" in s3_event["detail"]["bucket"]

    def test_detail_object_key_present(self, s3_event):
        assert "key" in s3_event["detail"]["object"]

    def test_detail_object_size_present(self, s3_event):
        assert "size" in s3_event["detail"]["object"]

    def test_missing_detail_fails(self, s3_event):
        event = copy.deepcopy(s3_event)
        del event["detail"]
        with pytest.raises(jsonschema.ValidationError, match="'detail'"):
            jsonschema.validate(event, S3_OBJECT_CREATED_SCHEMA)

    def test_missing_bucket_name_fails(self, s3_event):
        event = copy.deepcopy(s3_event)
        del event["detail"]["bucket"]["name"]
        with pytest.raises(jsonschema.ValidationError, match="'name'"):
            jsonschema.validate(event, S3_OBJECT_CREATED_SCHEMA)

    def test_missing_object_key_fails(self, s3_event):
        event = copy.deepcopy(s3_event)
        del event["detail"]["object"]["key"]
        with pytest.raises(jsonschema.ValidationError, match="'key'"):
            jsonschema.validate(event, S3_OBJECT_CREATED_SCHEMA)

    def test_missing_object_size_fails(self, s3_event):
        event = copy.deepcopy(s3_event)
        del event["detail"]["object"]["size"]
        with pytest.raises(jsonschema.ValidationError, match="'size'"):
            jsonschema.validate(event, S3_OBJECT_CREATED_SCHEMA)

    def test_wrong_source_fails(self, s3_event):
        event = copy.deepcopy(s3_event)
        event["source"] = "aws.ec2"
        with pytest.raises(jsonschema.ValidationError):
            jsonschema.validate(event, S3_OBJECT_CREATED_SCHEMA)
