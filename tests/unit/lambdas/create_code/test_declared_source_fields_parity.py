# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Parity: the two copies of the declared-source-columns read.

TWO COPIES, NO IMPORT BETWEEN THEM
----------------------------------
The rule ``schema.get("properties", schema.get("fields", {}))`` exists twice:

* ``test_code/code_interpreter_runner.declared_source_fields`` — the original.
  It is what the pandas test frame is built from, and its precedence behaviour is
  pinned by p33 requirement 3.14: "``properties`` wins whenever the KEY is
  present, even when its value is empty; only an absent ``properties`` falls
  through to ``fields``. That is the behaviour requirement 3.14 pins, so it is
  reproduced rather than tidied."
* ``create_code/code_assembly._declared_source_fields`` — the copy, added so
  ``generate_code`` can refuse a mapping that names a column the frame will not
  contain.

They live in different Lambdas and **cannot import each other**. This file is the
only mechanical link between them, following the pattern wave 1 built for the two
``_has_usable_source_field`` copies in
``tests/unit/lambdas/schema_mapping/test_unsourced_definition_parity.py``.

WHY A COPY RATHER THAN A MOVE TO ``shared/``
--------------------------------------------
``shared/`` is bundled into the Lambda layer, so relocating
``declared_source_fields`` there would work. It was rejected: p33 requirement
3.14 pins that function where it is, alongside the frame builder it exists to
agree with (its own docstring: "Shared by ``_build_test_data_code`` and the
pre-session frame guard so the two cannot drift"). Moving it relocates a pinned
function and widens this change well past a bugfix — and it would land in
``lambda/agentic_data_ingestion_test_code/code_interpreter_runner.py``, a file
p41 is mid-flight in. The copy plus this parity test is the same trade the repo
already made for ``_has_usable_source_field``.

IF THIS FILE GOES RED
---------------------
One copy was changed and the other was not. That is the failure this file exists
to catch. Fix by changing both, or by deleting the copy and delegating — never by
loosening the assertion.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_LAMBDA_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda"))
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
_CREATE_CODE_DIR = os.path.join(_LAMBDA_ROOT, "agentic_data_ingestion_create_code")
_TEST_CODE_DIR = os.path.join(_LAMBDA_ROOT, "agentic_data_ingestion_test_code")

sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)
sys.path.insert(0, _TEST_CODE_DIR)

# Neither module is imported through its handler: `code_assembly` owns no AWS
# clients, and `code_interpreter_runner` builds its `bedrock-agentcore` client
# inside a try/except that degrades to `self.client = None` (`:212-217`), so the
# module imports without credentials. `pyspark_to_pandas` is a sibling import it
# needs, satisfied by `_TEST_CODE_DIR` above.
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)

try:
    import code_assembly as _ca
    import code_interpreter_runner as _cir
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

_BOTH_COPIES = [
    pytest.param(_ca._declared_source_fields, id="create_code.code_assembly"),
    pytest.param(_cir.declared_source_fields, id="test_code.code_interpreter_runner"),
]

# Every shape that separates the real precedence rule from a plausible tidy-up.
# `{"properties": {}, "fields": {...}}` is THE case: `get(a, get(b))` answers
# `{}`, while `get(a) or get(b)` answers the `fields` map.
SCHEMAS = [
    pytest.param(None, id="none"),
    pytest.param({}, id="empty-dict"),
    pytest.param({"type": "object"}, id="no-keys"),
    pytest.param({"properties": {"id": {"type": "string"}}}, id="properties-only"),
    pytest.param({"fields": {"id": {"type": "string"}}}, id="fields-only"),
    pytest.param({"properties": {}}, id="properties-empty"),
    pytest.param({"fields": {}}, id="fields-empty"),
    pytest.param({"properties": {}, "fields": {"id": {"type": "string"}}}, id="empty-properties-wins"),
    pytest.param(
        {"properties": {"a": {"type": "string"}}, "fields": {"b": {"type": "string"}}},
        id="both-populated-properties-wins",
    ),
    pytest.param(
        {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}},
        id="array-shaped-reads-nothing",
    ),
    pytest.param({"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]}, id="jdbc-shape"),
]


@pytest.mark.parametrize("schema", SCHEMAS)
def test_both_copies_agree(schema):
    """The whole point of the file: same input, same answer, every shape."""
    assert _ca._declared_source_fields(schema) == _cir.declared_source_fields(schema)


@pytest.mark.parametrize("read", _BOTH_COPIES)
def test_both_copies_let_an_empty_properties_win_over_a_populated_fields(read):
    """p33 requirement 3.14, asserted on both copies rather than trusted.

    Stated separately from the agreement test so a change that breaks the rule in
    BOTH copies identically — which the agreement test would pass — still goes
    red here.
    """
    assert read({"properties": {}, "fields": {"id": {"type": "string"}}}) == {}


@pytest.mark.parametrize("read", _BOTH_COPIES)
def test_both_copies_fall_through_to_fields_only_when_properties_is_absent(read):
    assert set(read({"fields": {"id": {"type": "string"}}})) == {"id"}


@pytest.mark.parametrize("read", _BOTH_COPIES)
@pytest.mark.parametrize("schema", [None, {}, {"type": "object"}])
def test_both_copies_are_total_on_an_absent_schema(read, schema):
    assert read(schema) == {}
