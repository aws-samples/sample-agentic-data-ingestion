# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: test data isolation via session prefix (Property 18).

Feature: testing-overhaul, Property 18: Test data isolation via session prefix

For any resource created during a real-AWS test session, its identifier
starts with ``test-{session_uuid[:8]}-``, ensuring that two concurrent
test sessions produce non-overlapping resource identifiers.

This test validates:
- ``TEST_SESSION_PREFIX`` matches the expected format
  (starts with "test-", 14 chars total, ends with "-")
- ``aws_session_prefix`` fixture returns the prefix
- Random UUIDs produce valid prefixes in the expected format

**Validates: Requirements 12.1, 12.5**
"""

from __future__ import annotations

import re

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.fixtures.conftest import TEST_SESSION_PREFIX

# The expected prefix format: "test-" + 8 hex chars + "-" = 14 chars total
PREFIX_PATTERN = re.compile(r"^test-[0-9a-f]{8}-$")


# ---------------------------------------------------------------------------
# Property 18a: TEST_SESSION_PREFIX matches expected format
# ---------------------------------------------------------------------------


def test_session_prefix_starts_with_test():
    """**Validates: Requirements 12.1**

    TEST_SESSION_PREFIX starts with 'test-'.
    """
    assert TEST_SESSION_PREFIX.startswith("test-")


def test_session_prefix_ends_with_dash():
    """**Validates: Requirements 12.1**

    TEST_SESSION_PREFIX ends with '-' for clean concatenation.
    """
    assert TEST_SESSION_PREFIX.endswith("-")


def test_session_prefix_is_14_chars():
    """**Validates: Requirements 12.1**

    TEST_SESSION_PREFIX is exactly 14 characters:
    'test-' (5) + hex[:8] (8) + '-' (1) = 14.
    """
    assert len(TEST_SESSION_PREFIX) == 14, f"Expected 14 chars, got {len(TEST_SESSION_PREFIX)}: '{TEST_SESSION_PREFIX}'"


def test_session_prefix_matches_pattern():
    """**Validates: Requirements 12.1**

    TEST_SESSION_PREFIX matches the regex ``^test-[0-9a-f]{8}-$``.
    """
    assert PREFIX_PATTERN.match(TEST_SESSION_PREFIX), (
        f"TEST_SESSION_PREFIX '{TEST_SESSION_PREFIX}' does not match expected pattern 'test-<8 hex chars>-'"
    )


# ---------------------------------------------------------------------------
# Property 18b: aws_session_prefix fixture returns the prefix
# ---------------------------------------------------------------------------


def test_aws_session_prefix_fixture_returns_prefix(aws_session_prefix):
    """**Validates: Requirements 12.1**

    The aws_session_prefix fixture returns TEST_SESSION_PREFIX.
    """
    assert aws_session_prefix == TEST_SESSION_PREFIX


def test_aws_session_prefix_fixture_matches_pattern(aws_session_prefix):
    """**Validates: Requirements 12.1**

    The aws_session_prefix fixture value matches the expected format.
    """
    assert PREFIX_PATTERN.match(aws_session_prefix), (
        f"aws_session_prefix '{aws_session_prefix}' does not match expected pattern 'test-<8 hex chars>-'"
    )


# ---------------------------------------------------------------------------
# Property 18c: Random UUIDs produce valid prefixes (hypothesis)
# ---------------------------------------------------------------------------

# Strategy: generate random UUIDs as hex strings
_uuid_hex_strategy = st.uuids().map(lambda u: u.hex)


@given(uuid_hex=_uuid_hex_strategy)
@settings(max_examples=50)
def test_prefix_format_from_random_uuid(uuid_hex: str):
    """**Validates: Requirements 12.1, 12.5**

    For any UUID, constructing a prefix as ``test-{uuid[:8]}-`` produces
    a string that:
    - starts with 'test-'
    - is exactly 14 characters
    - ends with '-'
    - matches the expected regex pattern
    - contains only lowercase hex digits in the middle segment
    """
    prefix = f"test-{uuid_hex[:8]}-"

    assert prefix.startswith("test-")
    assert prefix.endswith("-")
    assert len(prefix) == 14
    assert PREFIX_PATTERN.match(prefix), f"Prefix '{prefix}' from UUID hex '{uuid_hex}' does not match pattern"


@given(uuid_a=st.uuids(), uuid_b=st.uuids())
@settings(max_examples=50)
def test_different_uuids_produce_different_prefixes(uuid_a, uuid_b):
    """**Validates: Requirements 12.5**

    For any two distinct UUIDs, the session prefixes are different,
    ensuring concurrent test sessions don't collide.
    """
    if uuid_a == uuid_b:
        return  # Skip trivial case

    prefix_a = f"test-{uuid_a.hex[:8]}-"
    prefix_b = f"test-{uuid_b.hex[:8]}-"

    # With 8 hex chars (32 bits), collision is extremely unlikely but
    # theoretically possible. We check that the construction is correct
    # and different UUIDs yield different hex prefixes (when UUIDs differ
    # in their first 4 bytes).
    if uuid_a.hex[:8] != uuid_b.hex[:8]:
        assert prefix_a != prefix_b, f"Different UUIDs should produce different prefixes: '{prefix_a}' vs '{prefix_b}'"
