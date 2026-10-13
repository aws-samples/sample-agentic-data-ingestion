# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/response_utils.py.

Covers the shared Response_Helper contract (P21 Req 5):
- create_response / handle_error / DecimalEncoder are importable directly from
  shared/ (cross-Lambda availability, Req 5.1) — this test imports them via the
  sys.path-to-shared idiom that the Layer_Import_Fallback targets in local/test
  execution.
- create_response produces the standardized status code, CORS headers, and a
  Decimal-aware JSON body unchanged from the dashboard implementation (Req 5.9).
- handle_error returns a 500 with error/details keys.
"""

import json
import os
import sys
from decimal import Decimal

# Ensure shared modules are importable standalone (Layer_Import_Fallback target).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from response_utils import (
    DecimalEncoder,
    create_response,
    handle_error,
)


def test_create_response_status_code_and_default_headers():
    """create_response returns the given status code and the standard CORS headers."""
    response = create_response(200, {"ok": True})

    assert response["statusCode"] == 200
    headers = response["headers"]
    assert headers["Content-Type"] == "application/json"
    assert headers["Access-Control-Allow-Origin"] == "*"
    assert "Access-Control-Allow-Headers" in headers
    assert "Access-Control-Allow-Methods" in headers


def test_create_response_serializes_body_as_json():
    """The body is a JSON string parseable back to the original payload."""
    response = create_response(201, {"id": "abc", "count": 3})

    assert isinstance(response["body"], str)
    assert json.loads(response["body"]) == {"id": "abc", "count": 3}


def test_create_response_merges_custom_headers():
    """Custom headers are merged on top of the defaults."""
    response = create_response(200, {}, headers={"X-Custom": "value"})

    assert response["headers"]["X-Custom"] == "value"
    # Defaults are still present after the merge.
    assert response["headers"]["Content-Type"] == "application/json"


def test_create_response_encodes_decimal_in_body():
    """Decimal values in the body are encoded to float via DecimalEncoder."""
    response = create_response(200, {"amount": Decimal("12.50")})

    assert json.loads(response["body"]) == {"amount": 12.5}


def test_decimal_encoder_serializes_decimal_to_float():
    """DecimalEncoder converts Decimal to float; other types use default behavior."""
    assert json.dumps({"x": Decimal("1.25")}, cls=DecimalEncoder) == '{"x": 1.25}'


def test_decimal_encoder_rejects_unsupported_type():
    """Non-Decimal unsupported types still raise TypeError (default fallthrough)."""
    import pytest

    with pytest.raises(TypeError):
        json.dumps({"s": {1, 2, 3}}, cls=DecimalEncoder)


def test_handle_error_returns_500_with_error_and_details():
    """handle_error returns a 500 response carrying error + details keys."""
    response = handle_error(ValueError("boom"), message="Something failed")

    assert response["statusCode"] == 500
    body = json.loads(response["body"])
    assert body["error"] == "Something failed"
    assert body["details"] == "boom"


def test_handle_error_default_message():
    """handle_error defaults the user-facing message to a generic error string."""
    response = handle_error(RuntimeError("kaboom"))

    body = json.loads(response["body"])
    assert body["error"] == "Internal server error"
    assert body["details"] == "kaboom"
