# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/decimal_utils.py (P28 R19.2 — single float↔Decimal converter).

Validates: Requirements 19.2 (float↔Decimal converters live in one shared
module, replacing the local copies). Covers the exact semantics the replaced
copies had:

- float→Decimal uses string round-trip (``0.1`` → ``Decimal("0.1")``, never
  the binary-float artifact ``Decimal(0.1)``)
- nested dict/list structures are walked recursively
- non-float scalars pass through unchanged — including ``bool``, which is an
  ``int`` subclass (NOT a ``float`` subclass) and was never converted by any
  of the replaced copies
- the reverse converter (Decimal→float) mirrors the walk
- round-trip float→Decimal→float restores the original payload
"""

import os
import sys
from decimal import Decimal

from hypothesis import given
from hypothesis import strategies as st

# Add shared directory to path (same convention as test_identifier_utils.py)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

from decimal_utils import convert_decimals_to_float, convert_floats_to_decimal


class TestConvertFloatsToDecimal:
    """float→Decimal for DynamoDB writes — Decimal(str(f)) semantics."""

    def test_float_uses_exact_string_semantics_not_binary_artifact(self):
        result = convert_floats_to_decimal(0.1)
        assert result == Decimal("0.1")
        # The binary-float constructor would produce a different value
        # (0.1000000000000000055511151231257827021181583404541015625).
        # Deliberate float-literal Decimal here — it IS the artifact we
        # assert against.
        assert result != Decimal(0.1)  # noqa: RUF032

    def test_nested_dict_and_list_converted_recursively(self):
        payload = {
            "confidence": 0.85,
            "mappings": [{"score": 0.5}, {"score": 1.25}],
            "meta": {"depth": {"ratio": 2.5, "counts": [1.0, 2.0]}},
        }
        result = convert_floats_to_decimal(payload)
        assert result == {
            "confidence": Decimal("0.85"),
            "mappings": [{"score": Decimal("0.5")}, {"score": Decimal("1.25")}],
            "meta": {"depth": {"ratio": Decimal("2.5"), "counts": [Decimal("1.0"), Decimal("2.0")]}},
        }

    def test_non_float_scalars_pass_through(self):
        for value in (42, "text", None, Decimal("3.14")):
            assert convert_floats_to_decimal(value) == value

    def test_bool_is_not_converted(self):
        # bool subclasses int, not float — the replaced copies never touched
        # booleans and DynamoDB stores them natively.
        assert convert_floats_to_decimal(True) is True
        assert convert_floats_to_decimal(False) is False
        assert convert_floats_to_decimal({"flag": True}) == {"flag": True}

    def test_dict_keys_untouched(self):
        # Only values are converted; float keys (however unusual) stay keys.
        result = convert_floats_to_decimal({"rate": 0.5})
        assert list(result.keys()) == ["rate"]

    def test_empty_containers(self):
        assert convert_floats_to_decimal({}) == {}
        assert convert_floats_to_decimal([]) == []


class TestConvertDecimalsToFloat:
    """Decimal→float for JSON serialization — the reverse walk."""

    def test_decimal_becomes_float(self):
        assert convert_decimals_to_float(Decimal("0.1")) == 0.1
        assert isinstance(convert_decimals_to_float(Decimal("2")), float)

    def test_nested_dict_and_list_converted_recursively(self):
        payload = {
            "score": Decimal("0.85"),
            "items": [Decimal("1.5"), {"inner": Decimal("2.25")}],
        }
        result = convert_decimals_to_float(payload)
        assert result == {"score": 0.85, "items": [1.5, {"inner": 2.25}]}
        assert isinstance(result["score"], float)
        assert isinstance(result["items"][0], float)
        assert isinstance(result["items"][1]["inner"], float)

    def test_non_decimal_scalars_pass_through(self):
        for value in (42, "text", None, True, 1.5):
            assert convert_decimals_to_float(value) == value


class TestRoundTrip:
    """float→Decimal→float restores the original payload."""

    def test_example_round_trip(self):
        payload = {"a": 0.1, "b": [1.25, {"c": 3.5}], "d": "keep", "e": 7, "f": None, "g": True}
        assert convert_decimals_to_float(convert_floats_to_decimal(payload)) == payload

    # Smart generator: JSON-ish payloads of finite floats, ints, strings,
    # bools, and None nested in dicts/lists — the shapes these converters
    # actually receive (DynamoDB items / EventBridge details). NaN/inf are
    # excluded: Decimal(str(nan)) raises and DynamoDB rejects them anyway.
    _scalars = st.one_of(
        st.floats(allow_nan=False, allow_infinity=False),
        st.integers(),
        st.text(max_size=20),
        st.booleans(),
        st.none(),
    )
    _payloads = st.recursive(
        _scalars,
        lambda children: st.one_of(
            st.lists(children, max_size=4),
            st.dictionaries(st.text(max_size=10), children, max_size=4),
        ),
        max_leaves=25,
    )

    @given(payload=_payloads)
    def test_round_trip_property(self, payload):
        """**Validates: Requirements 19.2**

        For any JSON-ish payload, float→Decimal→float is the identity:
        ``Decimal(str(f))`` back through ``float()`` restores every float
        exactly (str round-trips floats), and all other values (bool
        included) pass through both walks untouched.
        """
        assert convert_decimals_to_float(convert_floats_to_decimal(payload)) == payload
