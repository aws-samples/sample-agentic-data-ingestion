# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: ``validate_approval_request``'s three sanitization rules.

CHARACTERIZATION PLUS ONE CORRECTION
------------------------------------
``validate_approval_request`` gates **all four** approval types — every
create-mode event the state machine sends passes through it — and before this
file it had no unit coverage at all. The only assertions on it anywhere were four
cases in ``tests/integration/approval_flow/test_approval_system.py``
(``:73-121``): valid event, missing required field, invalid approval type, and
``.strip()``. **None of them asserted the length cap**, in either direction, so
the cap's behaviour was unpinned in the layer that runs on every change.

Most of what is below is therefore characterization — the behaviour that was
already there, written down. One assertion is not, and it is the reason the file
exists.

THE DEFECT, AND WHY TRUNCATION IS THE WRONG ANSWER FOR AN ID
------------------------------------------------------------
The function applied ``.strip()[:500]`` to every string value except
``task_token``. That is right for free text and wrong for an identifier, and the
difference is not stylistic:

* A comment clipped at 500 characters is the same fact, shorter.
* An id clipped at 500 characters is a **different id** — well-formed,
  plausible-looking, and resolving to nothing.

``mapping_id`` made it concrete. p54 R1.1 put it on the reuse arm's event;
``create_approval_request`` receives the sanitized dict and hands it to
``get_approval_data``, whose reuse arm passes the value to
``_reused_mapping_payload``. A clipped ``mapping_id`` misses in the mappings
table, and the arm renders an "unreadable" approval payload with **no indication
that a lossy transform upstream is why**. ``pipeline_id`` and ``run_id`` carry the
same hazard with a worse consequence: they are the approval record's own key
components, so a clipped one files the approval against a run that does not
exist.

Expectations here derive from that requirement — *an id that cannot round-trip
must not be silently substituted for a valid one* — and **not** from the previous
output, which is where the bug lived.

WHY REFUSING IS NOT A NEW ERROR CONTRACT
----------------------------------------
``LGR-86698d4-2`` (template / ratified) draws the raise-vs-return line by
**origin**, not by exception type: ``validate_approval_request`` sits
deliberately OUTSIDE ``lambda_handler``'s inner ``try``, so its ``ValueError``
stays a 400 for a malformed event (R3.5), while anything escaping
``create_approval_request`` fails the invocation. An id longer than 500
characters is a malformed event — no producer emits one, ``schema_compare``
writes a UUID-shaped ``mapping_id`` — so it joins the two malformed-event cases
already on this path rather than opening a third. That placement is asserted
below, because it is what makes the refusal correct rather than merely loud.

NOT REACHABLE TODAY
-------------------
No id in this system approaches 500 characters, so nothing here fixes an
observed failure. It is a latent-correctness fence, and the tests are what stop a
later reader "simplifying" the three rules back into one.

Validates: p54 Requirement 1.1 (the reuse arm's ``mapping_id`` must resolve).
"""

import importlib.util
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

_LAMBDA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_approval_handler")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

for _p in (_LAMBDA_DIR, _SHARED_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_REQUIRED_ENV = {
    "AGENTIC_DATA_INGESTION_ENV": "local",
    "APPROVALS_TABLE": "test-approvals",
    "STATE_TABLE": "test-state",
    "TASK_TOKENS_TABLE": "test-task-tokens",
    "MAPPINGS_TABLE": "test-schema-mappings",
}

#: Every approval type this function admits. Spelled out rather than imported so
#: a silent widening of the accepted set shows up here as an untested member.
ALL_APPROVAL_TYPES = ("schema_mapping", "code_generation", "first_run", "test_failure")


def _load_handler(alias: str):
    """Import the approval handler fresh, with AWS clients mocked out.

    Same idiom as ``test_timeout_behavior_unadvertised.py`` in this directory:
    several Lambda dirs contain a module named ``lambda_function``, so this one
    is loaded from its path under a unique alias.
    """
    for name, value in _REQUIRED_ENV.items():
        os.environ.setdefault(name, value)

    with patch("boto3.resource") as mock_resource, patch("boto3.client"):
        mock_resource.return_value.Table.side_effect = lambda _name: MagicMock()
        sys.modules.pop(alias, None)
        spec = importlib.util.spec_from_file_location(alias, os.path.join(_LAMBDA_DIR, "lambda_function.py"))
        module = importlib.util.module_from_spec(spec)
        sys.modules[alias] = module
        spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def handler():
    return _load_handler("ah_validate_approval_request_sanitization")


def _event(**overrides) -> dict:
    """A minimal valid create-mode event, with overrides applied."""
    base = {"approval_type": "schema_mapping", "pipeline_id": "pipe-1", "run_id": "run-1"}
    base.update(overrides)
    return base


class TestTheCapItselfIsUnchangedForFreeText:
    """The 500-character cap exists for a reason that still applies."""

    def test_a_long_free_text_field_is_still_truncated(self, handler):
        """Characterization. The cap is NOT weakened for free text.

        This is the assertion that stops the id fix turning into "remove the
        cap". A comment clipped at 500 characters is still the same comment.
        """
        result = handler.validate_approval_request(_event(comment="x" * 900))

        assert len(result["comment"]) == handler._MAX_FREE_TEXT_LENGTH
        assert result["comment"] == "x" * handler._MAX_FREE_TEXT_LENGTH

    def test_free_text_at_the_cap_is_untouched(self, handler):
        """The boundary is inclusive: exactly 500 characters is not over."""
        result = handler.validate_approval_request(_event(comment="x" * handler._MAX_FREE_TEXT_LENGTH))

        assert len(result["comment"]) == handler._MAX_FREE_TEXT_LENGTH

    def test_whitespace_is_stripped_before_the_cap_is_applied(self, handler):
        """Characterization, and the ordering matters.

        Stripping first means padding cannot consume budget an over-length value
        would otherwise be refused for. The integration suite asserts stripping;
        it does not assert that stripping happens *before* the measurement.
        """
        padded = "  " + ("x" * handler._MAX_FREE_TEXT_LENGTH) + "  "
        result = handler.validate_approval_request(_event(comment=padded))

        assert result["comment"] == "x" * handler._MAX_FREE_TEXT_LENGTH

    def test_non_string_values_pass_through_untouched(self, handler):
        """Characterization. Only ``str`` values are sanitized."""
        payload = {"nested": ["a", "b"], "count": 7}
        result = handler.validate_approval_request(_event(test_result=payload, retry_count=3))

        assert result["test_result"] is payload
        assert result["retry_count"] == 3


class TestTheTaskTokenExemptionSurvives:
    """A token is longer than the cap and every character of it matters."""

    def test_a_task_token_over_the_cap_is_neither_clipped_nor_refused(self, handler):
        """Characterization, and the one field that must stay UNBOUNDED.

        Step Functions tokens run ~600-700 characters, so the cap would clip
        every one of them. A clipped token cannot be sent back, so the gate it
        belongs to stalls for its full ``TimeoutSeconds`` — the failure mode
        ``LGR-86698d4-3`` proposes an invariant against. It is exempt from the
        cap ENTIRELY, which is why it cannot join the identifier set: refusing at
        500 would refuse every real token.
        """
        token = "t" * 700
        result = handler.validate_approval_request(_event(task_token="  " + token + "  "))

        assert result["task_token"] == token

    def test_task_token_is_not_in_the_identifier_set(self, handler):
        """The two exemptions are different rules and must not be merged."""
        assert "task_token" in handler._UNTRUNCATED_FIELDS
        assert "task_token" not in handler._EXACT_IDENTITY_FIELDS


class TestAnIdentifierIsRefusedRatherThanTruncated:
    """The correction. Derived from the requirement, not from prior output."""

    @pytest.mark.parametrize("field", sorted({"pipeline_id", "run_id", "mapping_id"}))
    def test_an_over_length_identifier_raises(self, handler, field):
        """An id that cannot round-trip must not be silently substituted.

        Asserted for all three members rather than for ``mapping_id`` alone:
        ``pipeline_id`` and ``run_id`` are the approval record's key components,
        so a clipped one files the approval against a run that does not exist.
        """
        with pytest.raises(ValueError) as exc:
            handler.validate_approval_request(_event(**{field: "m" * 501}))

        message = str(exc.value)
        assert field in message, "the refusal must name the offending field"
        assert "501" in message, "the refusal must state the length observed"

    @pytest.mark.parametrize("field", sorted({"pipeline_id", "run_id", "mapping_id"}))
    def test_an_identifier_at_the_cap_is_accepted_verbatim(self, handler, field):
        """The boundary is inclusive, and an accepted id is byte-identical.

        The complement of the test above: the fence refuses only what it cannot
        represent, and what it accepts it does not alter.
        """
        value = "m" * handler._MAX_FREE_TEXT_LENGTH
        result = handler.validate_approval_request(_event(**{field: value}))

        assert result[field] == value

    def test_a_realistic_mapping_id_round_trips(self, handler):
        """The shape ``schema_compare`` actually writes, unchanged end to end."""
        mapping_id = "a5a00ebac4a903530e6a785af8295005"
        result = handler.validate_approval_request(_event(mapping_id=mapping_id))

        assert result["mapping_id"] == mapping_id

    def test_no_identifier_is_ever_shortened(self, handler):
        """The property, stated over the whole set rather than per field.

        Either the value survives intact or the call raises. There is no third
        outcome in which a shorter id is returned — which is exactly the outcome
        that produced an unresolvable ``mapping_id``.
        """
        for field in sorted(handler._EXACT_IDENTITY_FIELDS):
            for length in (1, 36, handler._MAX_FREE_TEXT_LENGTH, handler._MAX_FREE_TEXT_LENGTH + 1):
                value = "z" * length
                try:
                    result = handler.validate_approval_request(_event(**{field: value}))
                except ValueError:
                    assert length > handler._MAX_FREE_TEXT_LENGTH, (
                        f"{field} of length {length} is representable and must not be refused"
                    )
                    continue
                assert result[field] == value, f"{field} of length {length} was altered"


class TestTheRefusalKeepsItsRatifiedPlacement:
    """``LGR-86698d4-2``: a malformed event is a 400, by ORIGIN not by type."""

    def test_validate_is_called_outside_the_inner_try(self, handler):
        """The new ValueError must reach the 400 clause, not the failure clause.

        Asserted through ``lambda_handler`` rather than by reading the source, so
        it measures the contract the dashboard and the state machine observe. A
        create-mode event carrying an over-length id returns 400 and — the load-
        bearing half — ``create_approval_request`` is never reached, which is what
        makes the origin rule hold.
        """
        with patch.object(handler, "create_approval_request") as create:
            response = handler.lambda_handler(_event(mapping_id="m" * 501), MagicMock())

        assert response["statusCode"] == 400
        create.assert_not_called()

    @pytest.mark.parametrize("approval_type", ALL_APPROVAL_TYPES)
    def test_the_rules_apply_to_every_approval_type(self, handler, approval_type):
        """This function gates all four types, so nothing here is type-specific."""
        with pytest.raises(ValueError):
            handler.validate_approval_request(_event(approval_type=approval_type, run_id="r" * 501))
