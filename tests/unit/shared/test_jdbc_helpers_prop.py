# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for ``shared/jdbc_helpers.py`` validation helpers.

This file complements the example-based tests in ``test_jdbc_helpers.py``
with universal-property assertions over Hypothesis-generated inputs.

**Property 2: Identifier validation is fail-closed**

``validate_identifier`` is the single source of truth for SQL identifier
validation across AgenticDataIngestion (wizard-time and Glue job runtime — see
Requirements 3.5 and 7.5). The function is **total**: it must never raise
``TypeError`` or return anything other than ``None``; it either accepts
the input (returns ``None``) or rejects it with ``ValueError``. The
allowed shape is ``^[A-Za-z_][A-Za-z0-9_]*$``.

This property test exercises the function across three input families:

* **2a — Conforming strings**: drawn from the regex itself, must always
  be accepted.
* **2b — Non-conforming strings**: drawn from arbitrary Unicode text and
  filtered to those that do NOT match the regex, must always be
  rejected.
* **2c — Non-string types**: integers, floats, lists, dicts, and
  ``None``, must always be rejected (defence-in-depth — the function
  is called from PySpark templates where any payload shape is possible).

**Validates: Requirements 3.5, 7.5**
"""

from __future__ import annotations

import os
import re
import sys

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

# Make shared modules importable without relying on the Lambda layer mount.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

from jdbc_helpers import validate_identifier

# The validation regex — kept in sync with ``WATERMARK_COLUMN_RE`` in
# ``jdbc_helpers.py`` and Requirements 3.5 / 7.5. We use ``fullmatch`` to
# match the semantics of ``re.match(...)`` against the anchored pattern
# ``^...$``.
_IDENT_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"
_IDENT_RE = re.compile(_IDENT_PATTERN)


# ---------------------------------------------------------------------------
# Property 2a — conforming strings are accepted
# ---------------------------------------------------------------------------


class TestIdentifierAcceptsConforming:
    """Strings matching ``^[A-Za-z_][A-Za-z0-9_]*$`` MUST be accepted.

    **Validates: Requirements 3.5, 7.5**
    """

    @given(name=st.from_regex(_IDENT_PATTERN, fullmatch=True))
    @settings(max_examples=200)
    def test_accepts_any_conforming_identifier(self, name: str):
        # validate_identifier returns None on success; any exception is a
        # failure of the property.
        assert validate_identifier(name) is None


# ---------------------------------------------------------------------------
# Property 2b — non-conforming strings are rejected
# ---------------------------------------------------------------------------


class TestIdentifierRejectsNonConforming:
    """Strings NOT matching the regex MUST raise ``ValueError``.

    **Validates: Requirements 3.5, 7.5**
    """

    @given(
        name=st.text().filter(lambda s: not _IDENT_RE.fullmatch(s)),
    )
    @settings(max_examples=200)
    def test_rejects_any_non_conforming_string(self, name: str):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_identifier(name)


# ---------------------------------------------------------------------------
# Property 2c — non-string types are rejected (fail-closed defence-in-depth)
# ---------------------------------------------------------------------------


class TestIdentifierRejectsNonStringTypes:
    """Non-string inputs MUST raise ``ValueError``, never ``TypeError``.

    The PySpark template runtime calls ``validate_identifier`` on values
    pulled from job-argument JSON, so any Python type can in principle
    reach the function. Per Requirement 7.5, the helper must fail closed
    on any non-string payload.

    **Validates: Requirements 3.5, 7.5**
    """

    @given(
        value=st.one_of(
            st.integers(),
            st.floats(allow_nan=True, allow_infinity=True),
            st.booleans(),
            st.none(),
            st.binary(),
            st.lists(st.text()),
            st.dictionaries(st.text(), st.text()),
            st.tuples(st.text()),
        )
    )
    @settings(max_examples=200)
    def test_rejects_any_non_string_value(self, value):
        with pytest.raises(ValueError, match="Invalid SQL identifier"):
            validate_identifier(value)  # type: ignore[arg-type]


# ===========================================================================
# Property 3 — Watermark value validation is type-correct (fail-closed)
# ===========================================================================
#
# ``validate_watermark_value`` is the second half of the wizard-time and
# job-runtime SQL-injection guard (see Requirement 7.5). For each known
# ``watermark_type`` it accepts only strings matching the type-specific regex;
# all other inputs — including values that conform to the OTHER type's regex,
# and any unknown ``watermark_type`` — must raise ``ValueError``.
#
# The properties below exercise both directions of acceptance/rejection across
# the two known types, plus a defence-in-depth check that any unknown
# ``watermark_type`` is rejected.
#
# **Validates: Requirements 7.5**

from jdbc_helpers import validate_watermark_value

# Source-of-truth regexes — kept aligned with ``WATERMARK_NUMERIC_RE`` and
# ``WATERMARK_TIMESTAMP_RE`` in ``jdbc_helpers.py`` and Requirement 7.5.
_NUMERIC_PATTERN = r"^-?\d+(\.\d+)?$"
_TIMESTAMP_PATTERN = (
    r"^\d{4}-\d{2}-\d{2}"
    r"([T ]\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:?\d{2})?)?$"
)
_NUMERIC_RE = re.compile(_NUMERIC_PATTERN)
_TIMESTAMP_RE = re.compile(_TIMESTAMP_PATTERN)


# ---------------------------------------------------------------------------
# Property 3a — numeric watermark accepts conforming numeric strings
# ---------------------------------------------------------------------------


class TestNumericWatermarkAcceptsConforming:
    """Strings matching the numeric regex MUST be accepted as ``"numeric"``.

    **Validates: Requirements 7.5**
    """

    @given(value=st.from_regex(_NUMERIC_PATTERN, fullmatch=True))
    @settings(max_examples=200)
    def test_accepts_any_conforming_numeric_string(self, value: str):
        assert validate_watermark_value(value, "numeric") is None


# ---------------------------------------------------------------------------
# Property 3b — numeric watermark rejects timestamp-shaped strings
# ---------------------------------------------------------------------------


class TestNumericWatermarkRejectsTimestamp:
    """Strings matching the timestamp regex MUST be rejected as ``"numeric"``.

    Timestamps generally cannot also satisfy the numeric regex (they contain
    ``-``/``:``/``T`` etc.), but the strategy filters out any defensive
    cross-matches just in case.

    **Validates: Requirements 7.5**
    """

    @given(
        value=st.from_regex(_TIMESTAMP_PATTERN, fullmatch=True).filter(lambda s: not _NUMERIC_RE.fullmatch(s)),
    )
    @settings(max_examples=200)
    def test_rejects_any_timestamp_shaped_string(self, value: str):
        with pytest.raises(ValueError, match="Invalid numeric watermark value"):
            validate_watermark_value(value, "numeric")


# ---------------------------------------------------------------------------
# Property 3c — timestamp watermark accepts conforming timestamp strings
# ---------------------------------------------------------------------------


class TestTimestampWatermarkAcceptsConforming:
    """Strings matching the timestamp regex MUST be accepted as ``"timestamp"``.

    **Validates: Requirements 7.5**
    """

    @given(value=st.from_regex(_TIMESTAMP_PATTERN, fullmatch=True))
    @settings(max_examples=200)
    def test_accepts_any_conforming_timestamp_string(self, value: str):
        assert validate_watermark_value(value, "timestamp") is None


# ---------------------------------------------------------------------------
# Property 3d — timestamp watermark rejects numeric-shaped strings
# ---------------------------------------------------------------------------


class TestTimestampWatermarkRejectsNumeric:
    """Strings matching the numeric regex MUST be rejected as ``"timestamp"``.

    The strategy filters out any value that also matches the timestamp regex,
    although in practice no numeric string can — timestamps require at least
    a four-digit year, two-digit month, and two-digit day separated by ``-``.

    **Validates: Requirements 7.5**
    """

    @given(
        value=st.from_regex(_NUMERIC_PATTERN, fullmatch=True).filter(lambda s: not _TIMESTAMP_RE.fullmatch(s)),
    )
    @settings(max_examples=200)
    def test_rejects_any_numeric_shaped_string(self, value: str):
        with pytest.raises(ValueError, match="Invalid timestamp watermark value"):
            validate_watermark_value(value, "timestamp")


# ---------------------------------------------------------------------------
# Property 3e — unknown ``watermark_type`` is always rejected
# ---------------------------------------------------------------------------


class TestUnknownWatermarkTypeRejected:
    """Any ``watermark_type`` other than the two known values MUST be rejected.

    This is the defence-in-depth guard against new watermark types being added
    in one place (e.g. the wizard schema) without the corresponding regex
    being added here. Rejecting unknown types ensures we fail closed rather
    than silently accept arbitrary strings.

    **Validates: Requirements 7.5**
    """

    @given(
        watermark_type=st.text().filter(lambda s: s not in {"numeric", "timestamp"}),
        value=st.text(),
    )
    @settings(max_examples=200)
    def test_rejects_any_unknown_watermark_type(self, watermark_type: str, value: str):
        with pytest.raises(ValueError, match="Unknown watermark_type"):
            validate_watermark_value(value, watermark_type)
