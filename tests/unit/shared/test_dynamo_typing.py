# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/dynamo_typing.py typed item accessors (P22 Req 2.2).

Validates that the accessors narrow DynamoDB item values correctly and are
behavior-preserving relative to ``item.get(key, default)``: present-and-correct-type
returns the value, absent/wrong-type returns the typed default, and nothing raises.
"""

import os
import sys
from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

# Ensure shared modules are importable standalone (Layer_Import_Fallback target).
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

from dynamo_typing import item_dict, item_int, item_list, item_str


class TestItemDict:
    def test_present_dict_returned(self):
        item = {"meta": {"a": 1}}
        assert item_dict(item, "meta") == {"a": 1}

    def test_absent_returns_empty_dict(self):
        assert item_dict({}, "missing") == {}

    def test_absent_returns_custom_default(self):
        assert item_dict({}, "missing", default={"x": 1}) == {"x": 1}

    @pytest.mark.parametrize("wrong", ["str", 5, Decimal("1"), ["a"], None, True])
    def test_wrong_type_returns_default(self, wrong):
        assert item_dict({"k": wrong}, "k") == {}


class TestItemStr:
    def test_present_str_returned(self):
        assert item_str({"name": "hello"}, "name") == "hello"

    def test_absent_returns_default(self):
        assert item_str({}, "missing") == ""
        assert item_str({}, "missing", default="fallback") == "fallback"

    @pytest.mark.parametrize("wrong", [5, Decimal("1"), {"a": 1}, ["a"], None, True])
    def test_wrong_type_returns_default(self, wrong):
        # Numbers are intentionally NOT coerced to str.
        assert item_str({"k": wrong}, "k") == ""


class TestItemInt:
    def test_present_int_returned(self):
        assert item_int({"n": 42}, "n") == 42

    def test_decimal_coerced(self):
        assert item_int({"n": Decimal("7")}, "n") == 7

    def test_numeric_string_coerced(self):
        assert item_int({"n": "13"}, "n") == 13

    def test_absent_returns_default(self):
        assert item_int({}, "missing") == 0
        assert item_int({}, "missing", default=-1) == -1

    def test_bool_treated_as_non_int(self):
        # DynamoDB stores booleans separately; True must not become 1.
        assert item_int({"flag": True}, "flag", default=99) == 99

    @pytest.mark.parametrize("wrong", ["not-a-number", {"a": 1}, ["a"], None])
    def test_non_numeric_returns_default(self, wrong):
        assert item_int({"k": wrong}, "k", default=5) == 5


class TestItemList:
    def test_present_list_returned(self):
        assert item_list({"xs": [1, 2, 3]}, "xs") == [1, 2, 3]

    def test_absent_returns_empty_list(self):
        assert item_list({}, "missing") == []

    def test_absent_returns_custom_default(self):
        assert item_list({}, "missing", default=[9]) == [9]

    @pytest.mark.parametrize("wrong", ["str", 5, Decimal("1"), {"a": 1}, None, True])
    def test_wrong_type_returns_default(self, wrong):
        assert item_list({"k": wrong}, "k") == []


class TestNeverRaises:
    """Property: accessors never raise and always return the declared type."""

    _values = st.one_of(
        st.none(),
        st.booleans(),
        st.integers(),
        st.text(),
        st.lists(st.integers()),
        st.dictionaries(st.text(), st.integers()),
        st.decimals(allow_nan=False, allow_infinity=False),
    )

    @given(value=_values)
    def test_item_dict_total(self, value):
        assert isinstance(item_dict({"k": value}, "k"), dict)

    @given(value=_values)
    def test_item_str_total(self, value):
        assert isinstance(item_str({"k": value}, "k"), str)

    @given(value=_values)
    def test_item_int_total(self, value):
        assert isinstance(item_int({"k": value}, "k"), int)
        # bool is a subclass of int; ensure we never leak a bool through item_int
        assert not isinstance(item_int({"k": value}, "k"), bool)

    @given(value=_values)
    def test_item_list_total(self, value):
        assert isinstance(item_list({"k": value}, "k"), list)
