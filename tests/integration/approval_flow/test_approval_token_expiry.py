# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Approval token expiry integration test.

Creates an approval → waits for TTL expiry → attempts approve → verifies
error response.

Requires DynamoDB TTL simulation which moto doesn't support — future work.

Validates: Requirements 17.4
"""

import pytest


@pytest.mark.skip(reason="Requires DynamoDB TTL simulation — moto doesn't support TTL expiry. Future work.")
def test_expired_approval_returns_error():
    """Create approval → wait for TTL → approve → verify error.

    Full flow:
    1. Create approval with short TTL
    2. Wait for TTL to expire (moto doesn't auto-delete TTL items)
    3. Attempt to approve
    4. Verify error response indicating expired token
    """
    pass
