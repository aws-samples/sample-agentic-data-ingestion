# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for create_pipeline writing approval_config to DynamoDB (Task 3.7).

Validates: AC-20.4

p64 task 2a.1 extends this module for R10.1 / R10.2 — a **supplied**
``approval_config`` is persisted as supplied, and the seeded defaults apply
**only when the request omits the key**. The two original tests below cover the
omitted arm and are unchanged; everything from
``TestSuppliedApprovalConfigPersists`` down is new and **fails against unfixed
code**, which assigned the defaults unconditionally at ``pipelines_crud.py:2104``.

Expectations here are derived from R10.1/R10.2, not from what the handler
currently emits. Three arms, and the distinction between the second and third is
the whole requirement:

* key **omitted** → defaults
* key present and a **dict** → verbatim, including ``{}`` and including all-``False``
* key present and **not a dict** → **400** (p64 task 2a.2 / R10.4; see below)

p64 task 2a.2 then adds R10.4's boundary validation, and it changes two outcomes
this module previously asserted. Both are **amended in place with the reason at
the assertion** (``LGR-60c4985-6``) rather than deleted:

* ``TestNonDictSubmissionWritesNoAttribute`` expected 201-and-no-attribute for a
  non-dict container; it is now a 400. 2a.1's own docstring anticipated this and
  asked for the amendment.
* ``test_an_integer_is_left_alone`` submitted ``80``, which the ``[0, 1]`` range
  now refuses; the int-is-not-widened property is asserted on ``1`` instead.

Arm 3 is still live in ``_submitted_approval_config`` as a backstop the handler no
longer reaches, so it is pinned directly at the function in
``TestArmThreeSurvivesAsABackstop``.
"""

import json
import os
import sys
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

# Stub out Lambda-layer dependencies before importing
sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (MagicMock(), MagicMock(), MagicMock())
_prev_env_utils = sys.modules.get("env_utils")
sys.modules["env_utils"] = MagicMock()
sys.modules["env_utils"].require_env = lambda name, default=None: default or name

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add handler package to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# P28 R18.2: the stored default must mirror the enforced gate — assert against
# the single config source so a divergent literal can never sneak back in.
try:
    from handlers.pipelines import create_pipeline

    # p64 2a.2: imported directly because `create_pipeline` no longer reaches arm 3 —
    # the boundary refusal 400s a non-dict first — so the branch has to be pinned at
    # the function. See `TestArmThreeSurvivesAsABackstop`.
    from handlers.pipelines_crud import _submitted_approval_config

    from config import CONFIDENCE_AUTO_APPROVE_THRESHOLD

    # p64 2a.1: the property test compares against the SAME shared converter the
    # handler applies, so the test cannot drift from the conversion under test.
    from decimal_utils import convert_floats_to_decimal
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

VALID_BODY = json.dumps(
    {
        "pipeline_name": "Test Pipeline",
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "description": "A test pipeline",
    }
)


@patch("handlers.pipelines_crud.boto3")
@patch("handlers.pipelines_crud.pipelines_table")
def test_create_pipeline_writes_approval_config(mock_table, mock_boto3):
    """create_pipeline writes approval_config with expected keys and values."""
    # Mock the Lambda invoke for provision_pipeline
    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: json.dumps({"status": "ok"}).encode())}
    mock_boto3.client.return_value = mock_lambda

    # Mock S3 get_object to raise so we skip schema loading (falls to except)
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_lambda
        if svc == "lambda"
        else MagicMock(
            get_object=MagicMock(side_effect=Exception("skip")),
            get_caller_identity=MagicMock(return_value={"Account": "123456789012"}),
        )
    )

    response = create_pipeline(VALID_BODY)

    assert response["statusCode"] == 201

    # Verify put_item was called
    mock_table.put_item.assert_called_once()
    pipeline_item = mock_table.put_item.call_args[1]["Item"]

    # Verify approval_config exists and has correct structure
    assert "approval_config" in pipeline_item
    config = pipeline_item["approval_config"]

    assert config["schema_mapping_enabled"] is True
    # The stored default mirrors the enforced gate (shared/config.py, P28 R18.2)
    assert config["schema_mapping_confidence_threshold"] == Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))
    assert config["code_generation_enabled"] is True
    assert config["always_approve_schema_changes"] is True


@patch("handlers.pipelines_crud.boto3")
@patch("handlers.pipelines_crud.pipelines_table")
def test_approval_config_threshold_is_decimal(mock_table, mock_boto3):
    """schema_mapping_confidence_threshold is a Decimal (DynamoDB-compatible)."""
    mock_lambda = MagicMock()
    mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: json.dumps({"status": "ok"}).encode())}
    mock_boto3.client.return_value = mock_lambda
    mock_boto3.client.side_effect = lambda svc, **kw: (
        mock_lambda
        if svc == "lambda"
        else MagicMock(
            get_object=MagicMock(side_effect=Exception("skip")),
            get_caller_identity=MagicMock(return_value={"Account": "123456789012"}),
        )
    )

    create_pipeline(VALID_BODY)

    pipeline_item = mock_table.put_item.call_args[1]["Item"]
    threshold = pipeline_item["approval_config"]["schema_mapping_confidence_threshold"]
    assert isinstance(threshold, Decimal)


# ---------------------------------------------------------------------------
# p64 task 2a.1 — R10.1 / R10.2
# ---------------------------------------------------------------------------


def _body(**overrides):
    """The valid create body, with `approval_config` added only when asked for.

    ``approval_config=...`` is passed through :func:`dict.update`, so passing it
    explicitly as ``{}`` produces a body that **has** the key with an empty
    value — which is the arm-2 case that must not re-default. Omitting the
    keyword entirely produces a body with no key at all: arm 1.
    """
    payload = {
        "pipeline_name": "Test Pipeline",
        "target_schema": "uploaded_test_schema",
        "ai_model": "anthropic.claude-3-haiku-20240307-v1:0",
        "description": "A test pipeline",
    }
    payload.update(overrides)
    return json.dumps(payload)


def _persisted_item(body):
    """Run create_pipeline against mocks and return the item handed to put_item.

    Mirrors the two original tests' wiring: the Lambda client is stubbed for
    provisioning and S3 ``get_object`` raises so the schema-loading branch falls
    to its ``except`` — which still returns 201 by ``LGR-cad8032-2``.
    """
    with (
        patch("handlers.pipelines_crud.boto3") as mock_boto3,
        patch("handlers.pipelines_crud.pipelines_table") as mock_table,
    ):
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: json.dumps({"status": "ok"}).encode())}
        mock_boto3.client.side_effect = lambda svc, **kw: (
            mock_lambda
            if svc == "lambda"
            else MagicMock(
                get_object=MagicMock(side_effect=Exception("skip")),
                get_caller_identity=MagicMock(return_value={"Account": "123456789012"}),
            )
        )

        response = create_pipeline(body)
        assert response["statusCode"] == 201, response

        mock_table.put_item.assert_called_once()
        return mock_table.put_item.call_args[1]["Item"]


def _refusal(body):
    """Run create_pipeline against the same mocks and return (response, table mock).

    p64 task 2a.2. The sibling of :func:`_persisted_item` for the arm that does not
    reach ``put_item``. It asserts **no** status code of its own, so a test can
    state the 400 itself rather than inherit it from a helper — and it hands back
    the table mock so the caller can assert nothing was written, which is the
    substantive half of the guarantee. A 400 that still created the record would
    pass a status-code-only assertion.
    """
    with (
        patch("handlers.pipelines_crud.boto3") as mock_boto3,
        patch("handlers.pipelines_crud.pipelines_table") as mock_table,
    ):
        mock_lambda = MagicMock()
        mock_lambda.invoke.return_value = {"Payload": MagicMock(read=lambda: json.dumps({"status": "ok"}).encode())}
        mock_boto3.client.side_effect = lambda svc, **kw: (
            mock_lambda
            if svc == "lambda"
            else MagicMock(
                get_object=MagicMock(side_effect=Exception("skip")),
                get_caller_identity=MagicMock(return_value={"Account": "123456789012"}),
            )
        )

        return create_pipeline(body), mock_table


class TestOmittedApprovalConfigGetsDefaults:
    """Arm 1 — the key's ABSENCE is what selects the defaults.

    **Validates: Requirements 10.2**
    """

    def test_absent_key_seeds_all_four_defaults(self):
        """A body with no approval_config gets the full seeded map.

        R10.2's compatibility guarantee: a client that predates the field is
        unaffected. This is the behaviour the two original tests above already
        pin; asserted here as a whole-map equality so a *silent addition* to the
        seed is caught too.
        """
        config = _persisted_item(_body())["approval_config"]

        assert config == {
            "schema_mapping_enabled": True,
            "schema_mapping_confidence_threshold": Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD)),
            "code_generation_enabled": True,
            "always_approve_schema_changes": True,
        }

    def test_the_default_map_is_a_fresh_object_per_call(self):
        """Two creates do not share one mutable default map.

        ``_default_approval_config`` is a function returning a literal rather
        than a module constant precisely so this holds — the result is handed to
        ``put_item`` as a nested map and a shared dict is the kind of thing a
        later caller edits in place.
        """
        first = _persisted_item(_body())["approval_config"]
        second = _persisted_item(_body())["approval_config"]

        assert first == second
        assert first is not second


class TestSuppliedApprovalConfigPersists:
    """Arm 2 — a submitted dict is persisted verbatim, never re-defaulted.

    **Validates: Requirements 10.1**
    """

    def test_supplied_values_persist_unchanged(self):
        """The submitted map is what lands on the record.

        Fails against unfixed code, which discarded the submission and wrote the
        seeded defaults regardless.

        The threshold is submitted as a JSON number and expected back as a
        ``Decimal`` — a request body cannot carry a ``Decimal``, so ``float`` on
        the way in and ``Decimal`` on the way out is the real contract, not a
        looseness in the test.
        """
        config = _persisted_item(
            _body(
                approval_config={
                    "schema_mapping_enabled": False,
                    "code_generation_enabled": False,
                    "always_approve_schema_changes": False,
                    "schema_mapping_confidence_threshold": 0.99,
                }
            )
        )["approval_config"]

        assert config == {
            "schema_mapping_enabled": False,
            "code_generation_enabled": False,
            "always_approve_schema_changes": False,
            "schema_mapping_confidence_threshold": Decimal("0.99"),
        }

    def test_explicit_false_is_not_upgraded_to_the_default_true(self):
        """`False` is an explicit choice, not an absent value.

        The load-bearing case. All four seeded defaults are ``True``, so a
        truthiness-based read of the submission would flip every one of these
        back on — the operator's toggles would appear saved and change nothing.
        """
        submitted = {
            "schema_mapping_enabled": False,
            "code_generation_enabled": False,
            "always_approve_schema_changes": False,
        }

        config = _persisted_item(_body(approval_config=dict(submitted)))["approval_config"]

        assert config["schema_mapping_enabled"] is False
        assert config["code_generation_enabled"] is False
        assert config["always_approve_schema_changes"] is False
        # No default leaked in alongside them.
        assert "schema_mapping_confidence_threshold" not in config

    def test_explicit_empty_map_is_persisted_and_does_not_re_default(self):
        """`approval_config: {}` means "no overrides", NOT "give me the defaults".

        The omit-versus-explicitly-empty distinction, and the case this kind of
        code usually gets wrong: ``if data.get("approval_config"):`` reads ``{}``
        as absence and substitutes the defaults, exactly as ``${VAR:-default}``
        substitutes on empty as well as unset.
        """
        config = _persisted_item(_body(approval_config={}))["approval_config"]

        assert config == {}

    def test_a_partial_submission_is_not_topped_up_with_defaults(self):
        """One submitted key does not drag the other three in behind it.

        R10.1 is about persistence, so the record states what the caller sent.
        The absent keys resolve at each reader's own fallback — the same
        fallbacks the seeded defaults duplicate — so this is fail-closed.
        """
        config = _persisted_item(_body(approval_config={"schema_mapping_enabled": False}))["approval_config"]

        assert config == {"schema_mapping_enabled": False}

    def test_unknown_keys_are_persisted_rather_than_filtered(self):
        """An unrecognised key is kept, not silently dropped.

        Filtering to a known-key allowlist would be *validation*, which p64 task
        2a.2 (R10.4) owns and this task must not pre-empt. Dropping a key here
        would also be the silent-discard defect R10.1 exists to remove, just
        narrower.
        """
        config = _persisted_item(_body(approval_config={"future_setting": "on"}))["approval_config"]

        assert config == {"future_setting": "on"}

    def test_a_json_float_is_stored_as_decimal(self):
        """A fractional JSON number survives as `Decimal`, not `float`.

        Serialisation, not validation: boto3 rejects ``float`` outright, so
        persisting verbatim without the shared converter would raise ``TypeError``
        on ``put_item``. Reachable via the clone path — ``hydrateWizardStateFromPipeline``
        reads a stored ``Decimal("0.85")`` back into the slider, which resubmits
        it as a JSON float.

        The submitted value is deliberately **not** ``0.85``: that is the seeded
        constant's own value, so the assertion would pass against unfixed code by
        coincidence and discriminate nothing.
        """
        config = _persisted_item(_body(approval_config={"schema_mapping_confidence_threshold": 0.42}))[
            "approval_config"
        ]

        threshold = config["schema_mapping_confidence_threshold"]
        assert isinstance(threshold, Decimal)
        # Decimal(str(f)) semantics — not the binary-float artifact
        # Decimal(0.42) == Decimal('0.42000000000000001554312234475219...').
        assert threshold == Decimal("0.42")

    def test_an_integer_is_left_alone(self):
        """`int` is already DynamoDB-safe and is not widened to Decimal.

        Guards the converter's own contract from the create path: turning a whole
        number into ``Decimal`` here would change the stored type.

        **AMENDED by p64 task 2a.2** (``LGR-60c4985-6``: an amendment stays legible
        as a deliberate reversal). The submitted value was ``80`` — the wizard
        slider's default *percent*, chosen when nothing bounded the range. R10.4
        now bounds the threshold to the fraction ``[0, 1]``, so ``80`` is a **400**
        and is asserted as one in ``TestThresholdOutOfRangeIsRefused`` below. The
        int-is-not-widened property is unchanged and is asserted here on ``1``,
        which is the largest legal integer the range admits. ``0`` and ``1`` are the
        only two, which is worth knowing: this assertion no longer covers "the
        common case" because after 2a.1b's unit conversion there is no common
        integer case — the wizard emits a fraction.
        """
        config = _persisted_item(_body(approval_config={"schema_mapping_confidence_threshold": 1}))["approval_config"]

        threshold = config["schema_mapping_confidence_threshold"]
        assert threshold == 1
        assert isinstance(threshold, int)


class TestNonDictSubmissionWritesNoAttribute:
    """Arm 3 — a wrong container type is refused at the boundary with a 400.

    **Validates: Requirements 10.1, 10.4**

    **AMENDED by p64 task 2a.2, not deleted** (``LGR-60c4985-6``: an amendment must
    stay legible as a deliberate reversal). 2a.1 wrote this class to assert that a
    non-dict ``approval_config`` persisted nothing and returned **201**, and left an
    explicit instruction that 2a.2 should amend it in place if R10.4 turned that
    into a 400. It did, and the class name is kept because what it now pins is the
    same fact from the other side: **no attribute is written, and the request no
    longer succeeds either.**

    The original reasoning is unchanged and still load-bearing. Every reader does
    ``record.get("approval_config", {}).get(key, default)``, so a stored scalar
    makes the *reader* raise ``AttributeError`` several Lambdas away; and writing
    the defaults instead would record four settings the caller never asked for. A
    400 satisfies both — nothing is written at all, because no record is created.

    **Why the posture changed.** ``LGR-cad8032-2`` still governs what it governs: an
    *unverifiable claim about external state* (does the S3 object this id names
    exist?) is answered by writing no attribute rather than by refusing. A
    container that is a string is not an unverifiable claim; it is wrong on its
    face, at the boundary, with no external read. R10.4 requires validation there,
    and the create path already refuses a bad ``schema_source`` and an unknown
    ``source_type`` the same way. Arm 3 survives in
    ``_submitted_approval_config`` as a backstop — pinned directly by
    ``TestArmThreeSurvivesAsABackstop`` below, since the handler no longer reaches
    it.
    """

    @pytest.mark.parametrize(
        "submitted",
        ["yes", 1, 0, True, False, None, [], ["schema_mapping_enabled"], [{"a": 1}]],
        ids=["str", "int", "zero", "bool-true", "bool-false", "null", "empty-list", "list", "list-of-dict"],
    )
    def test_non_dict_submission_is_refused(self, submitted):
        """Every non-dict container is a 400, and no record is written.

        The parameter set is 2a.1's, unchanged, so the amendment is a change of
        expected outcome over exactly the same inputs rather than a new test with a
        convenient set. ``0`` and ``False`` are the two that a truth test would
        have read as absence; they are refused like the rest.

        Fails against unfixed code, which returned 201 for all nine.
        """
        response, mock_table = _refusal(_body(approval_config=submitted))

        assert response["statusCode"] == 400
        # Nothing was written, which is the half of arm 3's original guarantee
        # that survives verbatim: no scalar reaches the record for a reader to
        # trip over.
        mock_table.put_item.assert_not_called()

    def test_the_refusal_names_the_field_and_what_was_wrong(self):
        """The error says which key was malformed and what it should have been.

        A 400 whose body says only "invalid request" moves the diagnosis to
        CloudWatch. The type name is included because the realistic cause is a
        client sending the whole object as a JSON string.
        """
        response, _ = _refusal(_body(approval_config="yes"))

        error = json.loads(response["body"])["error"]
        assert "approval_config" in error
        assert "object" in error
        assert "str" in error


class TestReconciledCasing:
    """A camelCase payload is persisted **as sent** — unrecognised, not translated.

    **Validates: Requirements 10.1**

    **AMENDED, not deleted** (``LGR-60c4985-6``: an amendment must stay legible as
    a deliberate reversal). This class was ``TestKnownCasingGap`` and asserted the
    inert-toggle behaviour as a fact about the tree: the wizard posted
    ``approval_config: state.approvalConfig`` in **camelCase**
    (``wizard/types.ts:10-15``) while every reader reads **snake_case**, so
    persisting verbatim landed four keys nothing read.

    **The gap is now closed, on the client.** p64 placed the single normalisation
    authority that p63 R1.2 reserved a decision for at the wizard's request
    boundary: ``buildApprovalConfigRequest`` (``CreatePipelinePage.tsx``) emits the
    readers' four snake_case keys. Nothing on this side changed, which is the
    point — ``_submitted_approval_config`` still persists verbatim and must keep
    doing so, because a server-side normaliser would make a second wire spelling
    legal forever (R1.2 forbids exactly that).

    **Note what this class could NOT have caught, since that is why it is being
    re-purposed rather than kept.** Its three assertions fed a camelCase payload
    straight to the Python handler, so they hold identically before and after a
    *client-side* fix — the tripwire only ever covered the arm where the server
    normalises. The client half is pinned by
    ``tests/contracts/test_approval_config_casing_cross_language.py``, which reads
    the TypeScript boundary and the Python readers and asserts they agree.

    What the three assertions now state, unchanged in mechanism: an
    **unreconciled** client — an old bundle, or any third-party caller — still has
    its payload recorded exactly as sent, and still lands on every reader's
    fail-closed default. That is worth keeping pinned: it is the compatibility
    behaviour, and it is what makes a stale deploy safe rather than silently
    permissive.
    """

    #: What a client that has NOT been reconciled sends. Retained under its
    #: original name because that is what it is: the wizard's old payload.
    WIZARD_PAYLOAD = {
        "schemaMappingEnabled": False,
        "schemaMappingConfidenceThreshold": 80,
        "codeGenerationEnabled": False,
        "alwaysApproveSchemaChanges": False,
    }

    def test_camel_case_keys_are_persisted_verbatim(self):
        """An unreconciled client's map lands unchanged — no key is translated.

        Unchanged assertion, changed subject. Before p64's reconciliation this was
        "today the wizard's own payload lands unchanged"; the wizard no longer
        sends this shape, so it now covers a stale bundle or a direct caller. The
        behaviour it pins is deliberate: translating here is the second
        normalisation authority R1.2 forbids.
        """
        config = _persisted_item(_body(approval_config=dict(self.WIZARD_PAYLOAD)))["approval_config"]

        assert config == self.WIZARD_PAYLOAD

    def test_no_snake_case_key_is_produced(self):
        """The server still translates nothing.

        This used to be the statement of the gap. It is now the statement of the
        **do-NOT**: the reconciliation happens on the client, so a snake_case key
        appearing here out of a camelCase submission would mean a second authority
        had been added on this side. Fails if anyone adds one.
        """
        config = _persisted_item(_body(approval_config=dict(self.WIZARD_PAYLOAD)))["approval_config"]

        for reader_key in (
            "schema_mapping_enabled",
            "schema_mapping_confidence_threshold",
            "code_generation_enabled",
            "always_approve_schema_changes",
        ):
            assert reader_key not in config

    def test_an_unreconciled_payload_is_still_fail_closed(self):
        """A stale client's `False`s do not reach a reader, so every gate stays on.

        Renamed from ``test_the_gates_are_still_fail_closed``: "still" described a
        gap that no longer exists. Both halves are asserted so the test
        discriminates — the camelCase keys ARE on the record carrying ``False``,
        and the reader expressions resolve to ``True`` regardless.

        This is the compatibility guarantee for a deploy where the bundle is behind
        the Lambda (a measured property of this project, not a hypothetical —
        ``LGR-c88d326-4``). The gates fail closed in that window.
        """
        config = _persisted_item(_body(approval_config=dict(self.WIZARD_PAYLOAD)))["approval_config"]

        # What the record claims.
        assert config["schemaMappingEnabled"] is False
        assert config["alwaysApproveSchemaChanges"] is False

        # What the readers see — the expressions verbatim from
        # schema_mapping/lambda_function.py:1669 and :1657.
        assert config.get("schema_mapping_enabled", True) is True
        assert config.get("always_approve_schema_changes", True) is True


class TestReconciledPayloadMakesTheTogglesLive:
    """The wire shape the wizard now sends reaches the readers as sent.

    **Validates: Requirements 10.1**

    The other half of p64's reconciliation, from this side. ``TestReconciledCasing``
    above proves the server translates nothing; this proves that when the client
    sends the readers' own spelling, an operator's ``False`` survives persistence
    and lands in the reader's expression as ``False`` — which is what makes the
    step-5 toggles operable rather than decorative.

    **The reader expressions here are transcribed, and that is a real limitation**
    stated rather than hidden. Importing ``schema_mapping`` or ``test_code`` into a
    ``dashboard_api`` unit test means a second module named ``lambda_function`` in
    this process plus the agent-layer stub set, which is why the sibling suites keep
    an autouse ``_ensure_correct_module`` fixture. The transcription is pinned to
    the real readers by
    ``tests/contracts/test_approval_config_casing_cross_language.py``, which parses
    both reader sites; without that file these assertions would only be testing
    themselves.
    """

    #: The four keys and units ``buildApprovalConfigRequest`` emits
    #: (``CreatePipelinePage.tsx``). The threshold is a **fraction** — the wizard
    #: slider's percent divided by 100 — because that is the unit the only writer
    #: of this attribute uses (``_default_approval_config`` seeds
    #: ``Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))``).
    WIRE_PAYLOAD_ALL_OFF = {
        "schema_mapping_enabled": False,
        "schema_mapping_confidence_threshold": 0.8,
        "code_generation_enabled": False,
        "always_approve_schema_changes": False,
    }

    def test_a_submitted_false_reaches_each_reader_expression_as_false(self):
        """`False` in, `False` out of every reader's own `.get(key, True)`.

        The whole point of the reconciliation. Fails against the unreconciled
        client not because the handler differs — it does not — but because that
        client never produced these keys, so this payload had no producer.
        """
        config = _persisted_item(_body(approval_config=dict(self.WIRE_PAYLOAD_ALL_OFF)))["approval_config"]

        # schema_mapping/lambda_function.py:1669 — the Governance_Switch.
        assert config.get("schema_mapping_enabled", True) is False
        # schema_mapping/lambda_function.py:1657 — the schema-change clause.
        assert config.get("always_approve_schema_changes", True) is False
        # test_code.resolve_code_approval_setting — identity-checked, so what
        # matters is that the stored value is the JSON boolean and not a coercion
        # of it. `is False` asserts exactly what that reader tests for.
        assert config["code_generation_enabled"] is False

    def test_a_submitted_true_is_stored_as_the_boolean_the_readers_require(self):
        """`True` survives as `True`, not as a truthy stand-in.

        ``resolve_code_approval_setting`` accepts only ``raw is True`` /
        ``raw is False`` and fails closed on anything else, so a value that merely
        *compares* equal is not enough — ``1 == True`` in Python but ``1 is True``
        is ``False``. This pins the stored type, not just its truthiness.
        """
        config = _persisted_item(
            _body(
                approval_config={
                    "schema_mapping_enabled": True,
                    "code_generation_enabled": True,
                    "always_approve_schema_changes": True,
                }
            )
        )["approval_config"]

        for key in ("schema_mapping_enabled", "code_generation_enabled", "always_approve_schema_changes"):
            assert config[key] is True

    def test_the_reader_default_still_applies_when_a_key_is_absent(self):
        """An absent key is a reader default, and every one of those is `True`.

        R10.2's compatibility arm, asserted at the *reader* rather than at the
        handler: a partial submission persists partially (``TestSupplied…`` above),
        and what a pipeline then gets for the missing keys is each reader's own
        fallback. Both directions in one record, so the test cannot pass by
        everything defaulting.
        """
        config = _persisted_item(_body(approval_config={"schema_mapping_enabled": False}))["approval_config"]

        # Submitted → honoured.
        assert config.get("schema_mapping_enabled", True) is False
        # Absent → the reader's own fail-closed default, which is approval ON.
        assert "always_approve_schema_changes" not in config
        assert config.get("always_approve_schema_changes", True) is True
        assert "code_generation_enabled" not in config
        # resolve_code_approval_setting's absent-key arm, transcribed: it returns
        # (True, _SETTING_FROM_DEFAULT) when the key is not in the map.
        assert config.get("code_generation_enabled", True) is True

    def test_the_confidence_threshold_persists_as_a_fraction(self):
        """The slider's percent arrives already converted, and stays a `Decimal`.

        The unit is the trap a key-only rename would have activated: the wizard
        slider is ``min={0} max={100} step={5}`` and renders ``%``, while this
        attribute's unit is the fraction ``_default_approval_config`` seeds. Had
        the reconciliation renamed the key and left the unit, an 80% slider would
        have persisted ``80`` under a key any fraction-comparing reader reads as
        "never auto-approve". ``buildApprovalConfigRequest`` divides by 100;
        ``confidenceFractionToPercent`` is its inverse on the hydrate path.

        The attribute has **no reader** anywhere in ``lambda/``, ``shared/`` or
        ``mcp/`` today — ``schema_mapping:1777`` and ``:1902`` read
        ``CONFIDENCE_AUTO_APPROVE_THRESHOLD`` directly — so this pins the unit
        before a reader exists rather than after. p63 R9 owns giving it one.
        """
        config = _persisted_item(_body(approval_config=dict(self.WIRE_PAYLOAD_ALL_OFF)))["approval_config"]

        threshold = config["schema_mapping_confidence_threshold"]
        assert isinstance(threshold, Decimal)
        assert threshold == Decimal("0.8")
        # In the same units as the constant it will eventually be compared against.
        assert threshold < Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))
        assert threshold <= 1


class TestSubmittedConfigPersistsUnchangedProperty:
    """Property — persistence adds no key and changes no value but float→Decimal.

    **Validates: Requirements 10.1, 10.2**
    """

    # JSON-ish scalars: the shapes a request body can actually carry. NaN and inf
    # are excluded because `Decimal(str(nan))` raises and DynamoDB rejects them
    # anyway — the same exclusion `tests/unit/shared/test_decimal_utils.py` makes.
    _scalars = st.one_of(
        st.booleans(),
        st.integers(min_value=-(10**6), max_value=10**6),
        st.floats(allow_nan=False, allow_infinity=False, allow_subnormal=False, width=32),
        st.text(max_size=16),
        st.none(),
    )

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(submitted=st.dictionaries(st.text(min_size=1, max_size=16), _scalars, max_size=5))
    def test_any_submitted_dict_persists_with_no_key_added_or_dropped(self, submitted):
        """For any dict, the stored map is exactly `convert_floats_to_decimal(dict)`.

        One property covering the whole of arm 2: the empty map, all-``False``
        maps, partial maps and unknown keys are all instances of it, and *no*
        default is ever merged in. Compared against the shared converter rather
        than against a hand-rolled expectation so the test cannot drift from the
        conversion the handler actually applies.
        """
        config = _persisted_item(_body(approval_config=dict(submitted)))["approval_config"]

        assert config == convert_floats_to_decimal(submitted)
        assert set(config) == set(submitted)


# ---------------------------------------------------------------------------
# p64 task 2a.2 — R10.4, values validated at the API boundary
# ---------------------------------------------------------------------------
#
# Everything below **fails against unfixed code**, which accepted every value
# unconditionally: 2a.1 persisted a submitted dict verbatim and validated no
# value at all, by design, leaving the refusal to this task.
#
# Expectations are derived from R10.4 and from the two reader sites, not from
# what the handler emitted before.


class TestGateValuesMustBeBooleans:
    """The three gates accept only JSON `true` / `false`.

    **Validates: Requirements 10.4**

    **Fail-closed at the reader is not enough, and that is why this is a 400.**
    Only one of the three readers is identity-checked:

    * ``test_code.resolve_code_approval_setting`` tests ``raw is True or raw is
      False`` and treats anything else as absent — ``code_generation_enabled``
      alone would fail closed (``LGR-69ca78b-1``'s read-side counterpart).
    * ``schema_mapping/lambda_function.py:1669`` reads the value then tests
      ``if not schema_mapping_enabled:`` — **truthiness**.
    * ``:1657`` is ``if approval_config.get("always_approve_schema_changes",
      True):`` — **truthiness**.

    So a submitted ``0``, ``""``, ``[]`` or ``None`` under either of the last two
    turns a governance gate **off**, with no boolean check anywhere between the
    request and the decision. p64 task 2a.1b named exactly this hazard when it
    refused to accept a second wire spelling; the boundary is the only place it
    closes, because the readers are several Lambdas away and two of them cannot
    tell ``0`` from ``False``.
    """

    GATE_KEYS = ("schema_mapping_enabled", "code_generation_enabled", "always_approve_schema_changes")

    @pytest.mark.parametrize("key", GATE_KEYS)
    @pytest.mark.parametrize(
        "value",
        [0, 1, "", "false", "true", "yes", None, [], {}, 0.0],
        ids=["zero", "one", "empty-str", "str-false", "str-true", "str-yes", "null", "list", "dict", "float-zero"],
    )
    def test_a_non_boolean_gate_value_is_refused(self, key, value):
        """Neither truthy nor falsy non-booleans are accepted for any of the three.

        ``"false"`` and ``0`` are the two that matter most and they fail in opposite
        directions: ``"false"`` is a **truthy string**, so a reader would treat a
        caller's "off" as "on"; ``0`` is a falsy int, so it would silently satisfy
        a reader that only asks whether the value is falsy. ``LGR-69ca78b-1``
        rejected a value-only check on precisely this pair — it "inverts the
        owner's setting in both directions".
        """
        response, mock_table = _refusal(_body(approval_config={key: value}))

        assert response["statusCode"] == 400
        mock_table.put_item.assert_not_called()

    @pytest.mark.parametrize("key", GATE_KEYS)
    def test_the_refusal_names_the_key_and_the_accepted_values(self, key):
        """The message says which gate, and that it wants true or false."""
        response, _ = _refusal(_body(approval_config={key: "yes"}))

        error = json.loads(response["body"])["error"]
        assert key in error
        assert "true or false" in error

    @pytest.mark.parametrize("key", GATE_KEYS)
    @pytest.mark.parametrize("value", [True, False], ids=["true", "false"])
    def test_a_real_boolean_is_accepted_for_every_gate(self, key, value):
        """The discriminating half — validation refuses bad values, not all values.

        Asserted per key and for both booleans so a validator that accidentally
        rejected ``False`` (the value an operator sets when turning a gate off, and
        the one a truth test would drop) could not pass this class.
        """
        config = _persisted_item(_body(approval_config={key: value}))["approval_config"]

        assert config[key] is value


class TestThresholdIsAFractionInRange:
    """`schema_mapping_confidence_threshold` must be a number in `[0, 1]`.

    **Validates: Requirements 10.4**

    **Refused, never clamped.** ``.kiro/specs/p63-inert-controls-and-gate-integrity``
    R9.3 requires a 400 naming the bound and explicitly rejects clamping, because a
    control that displays 70% while the system enforces 85% manufactures a new inert
    control — the defect class p63 exists to remove. p64 task 2a.1b already declined
    to clamp on the hydrate path for the same reason, and ``LGR-4ea43f3-4`` is the
    same posture for identifiers: refused over the cap, never truncated, because a
    silently altered value is well-formed, plausible and wrong.
    """

    KEY = "schema_mapping_confidence_threshold"

    @pytest.mark.parametrize(
        "value",
        [80, 100, 1.0001, 1.5, -0.1, -1, 1e9],
        ids=["percent-80", "percent-100", "just-over", "one-point-five", "just-under", "negative", "huge"],
    )
    def test_an_out_of_range_threshold_is_refused(self, value):
        """Outside `[0, 1]` is a 400, in both directions.

        ``80`` is the case with a real producer behind it: it is the wizard
        slider's default *percent*, which is what an unreconciled client sends
        before ``buildApprovalConfigRequest``'s divide-by-100. Under a percent
        reading it means a permissive gate; under the fraction reading this
        attribute is stored in, it means "never auto-approve". Refusing is what
        stops the two readings sharing one attribute.
        """
        response, mock_table = _refusal(_body(approval_config={self.KEY: value}))

        assert response["statusCode"] == 400
        mock_table.put_item.assert_not_called()

    def test_the_refusal_is_not_a_clamp(self):
        """No record is written carrying a corrected value.

        The assertion that discriminates refusal from clamping. A `max()`/`min()`
        coercion would also return a 2xx, so asserting only the status code would
        not distinguish the two; asserting that nothing was written does.
        """
        response, mock_table = _refusal(_body(approval_config={self.KEY: 80}))

        assert response["statusCode"] == 400
        mock_table.put_item.assert_not_called()

    def test_the_refusal_names_the_bound_and_the_unit(self):
        """The message carries 0, 1, and the word fraction.

        p63 R9.3's requirement is a refusal that *names the bound*; the unit is
        included because a submitted ``80`` is almost always an unconverted percent
        and "must be between 0 and 1" alone does not say so.
        """
        response, _ = _refusal(_body(approval_config={self.KEY: 80}))

        error = json.loads(response["body"])["error"]
        assert self.KEY in error
        assert "0" in error and "1" in error
        assert "fraction" in error
        assert "percent" in error

    @pytest.mark.parametrize(
        "value",
        ["0.8", "", None, [], {}, True, False, float("nan"), float("inf")],
        ids=["numeric-str", "empty-str", "null", "list", "dict", "bool-true", "bool-false", "nan", "inf"],
    )
    def test_a_non_numeric_threshold_is_refused(self, value):
        """A number is required, and `bool` does not count as one.

        ``True`` is the trap: ``bool`` is an ``int`` subclass, so ``isinstance(True,
        int)`` is ``True`` and ``0 <= True <= 1`` holds. A validator that checked
        the range without excluding ``bool`` first would accept ``True`` as the
        number 1 — a fully-permissive gate submitted by a client that meant to
        send a toggle. ``NaN`` and infinity are refused by the range comparison
        rather than by a check of their own, which is why they are listed here
        with the non-numerics they behave like.
        """
        response, mock_table = _refusal(_body(approval_config={self.KEY: value}))

        assert response["statusCode"] == 400
        mock_table.put_item.assert_not_called()

    @pytest.mark.parametrize(
        "value",
        [0, 0.0, 0.5, 0.85, 0.99, 1, 1.0],
        ids=["zero", "zero-float", "half", "gate", "high", "one", "one-float"],
    )
    def test_an_in_range_threshold_is_accepted(self, value):
        """Both endpoints are inclusive, and the shared gate's own value passes.

        ``0`` and ``1`` are asserted because an exclusive bound is the easy mistake
        and it would refuse ``1`` — "always require approval", a legitimate setting
        an operator may well pick. ``0.85`` is here because it is the value
        ``_default_approval_config`` seeds, so a range that refused it would refuse
        the handler's own default.
        """
        config = _persisted_item(_body(approval_config={self.KEY: value}))["approval_config"]

        assert config[self.KEY] == Decimal(str(value)) or config[self.KEY] == value

    @settings(max_examples=10, deadline=None, suppress_health_check=[HealthCheck.too_slow])
    @given(
        value=st.floats(
            min_value=-1000, max_value=1000, allow_nan=False, allow_infinity=False, allow_subnormal=False, width=32
        )
    )
    def test_the_accepted_set_is_exactly_the_range(self, value):
        """Property — acceptance and `0 <= v <= 1` are the same predicate.

        **Validates: Requirements 10.4**

        The parametrized cases above are examples either side of the two bounds;
        this states that there is no third behaviour anywhere in between — no band
        that is coerced, no band that is accepted and rewritten. Expressed as the
        range test itself rather than as a list of expected outcomes, so the test
        cannot drift from the requirement by being updated alongside the code.
        """
        in_range = 0 <= value <= 1
        body = _body(approval_config={self.KEY: value})

        if in_range:
            config = _persisted_item(body)["approval_config"]
            # Accepted, and stored as submitted (as a Decimal, since boto3 refuses
            # float) rather than adjusted.
            assert config[self.KEY] == Decimal(str(value))
        else:
            response, mock_table = _refusal(body)
            assert response["statusCode"] == 400
            mock_table.put_item.assert_not_called()


class TestTheP63FloorIsNotPreEmptedHere:
    """The range is `[0, 1]`; the shared constant is NOT used as a floor.

    **Validates: Requirements 10.4**

    A deliberate omission, pinned so it reads as one. ``.kiro/specs/p63-inert-controls-and-gate-integrity``
    R9/D2 make ``CONFIDENCE_AUTO_APPROVE_THRESHOLD`` a **floor** — a per-pipeline
    value below it is refused with a 400 naming the floor — and R10.4 could be read
    as asking for that here. It deliberately does not, for a concrete reason p64
    task 2a.1b recorded as a deferred disagreement: **the wizard's own default
    slider position is 80% → ``0.8``, which is below the 0.85 floor.** Adding the
    comparison in this task would 400 every untouched wizard submission. p63 R9.4
    is the other half of that change — raise the slider's minimum to the floor —
    and the two have to land together.

    Harmless meanwhile because the attribute has **no reader** anywhere in
    ``lambda/``, ``shared/`` or ``mcp/``: ``schema_mapping:1777`` and ``:1902`` read
    the constant directly.
    """

    KEY = "schema_mapping_confidence_threshold"

    def test_the_wizard_default_position_is_accepted(self):
        """`0.8` — below the shared gate — persists rather than 400ing.

        The load-bearing assertion of this class. It will **fail deliberately**
        when p63 R9's stricter-only resolver lands, and that failure is the signal
        to move the slider's minimum (R9.4) in the same change, not to relax the
        resolver.
        """
        config = _persisted_item(_body(approval_config={self.KEY: 0.8}))["approval_config"]

        assert config[self.KEY] == Decimal("0.8")
        # The disagreement, asserted rather than described, so the class cannot
        # quietly stop being about it.
        assert config[self.KEY] < Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))

    def test_a_value_far_below_the_gate_is_still_in_range(self):
        """`0.01` is a legal fraction today, and that is the whole point.

        Stated separately from ``0.8`` because ``0.8`` is close enough to the
        constant to look like a rounding question. This one is not: an operator can
        currently record a threshold an order of magnitude more permissive than the
        platform's, and only p63 R9 closes that.
        """
        config = _persisted_item(_body(approval_config={self.KEY: 0.01}))["approval_config"]

        assert config[self.KEY] == Decimal("0.01")


class TestUnrecognisedKeysAreStillNotValidated:
    """Validation covers the four keys with a meaning; others pass through.

    **Validates: Requirements 10.4**

    R10.4 asks for **value** validation, and rejecting unknown keys is a different
    obligation. Keeping them unvalidated preserves
    ``test_unknown_keys_are_persisted_rather_than_filtered`` above and keeps a newer
    client compatible with an older Lambda — deployment drift is a measured property
    of this project rather than a hypothesis (``LGR-c88d326-4``). An allowlist would
    also have to be amended by p64 task 2a.3 the moment
    ``infrastructure_approval_enabled`` gains a producer, and a stale Lambda would
    then start refusing a current wizard.
    """

    def test_an_unknown_key_with_a_non_boolean_value_is_accepted(self):
        """A future setting is not refused for failing this task's rules."""
        config = _persisted_item(_body(approval_config={"future_setting": "on"}))["approval_config"]

        assert config == {"future_setting": "on"}

    def test_a_camel_case_gate_name_is_not_validated_as_a_gate(self):
        """An unreconciled client's payload still lands, unrecognised.

        The compatibility guarantee ``TestReconciledCasing`` pins, re-asserted from
        the validation side: these names are not the readers' keys, so they are not
        this validator's business either. Validating them would be the second
        normalisation authority p63 R1.2 forbids — recognising a camelCase spelling
        well enough to reject its value is recognising it — and
        ``tests/contracts/test_approval_config_casing_cross_language.py::test_the_handler_adds_no_second_authority``
        fails if one appears as a string literal in the handler.

        The threshold here is ``80``, which would be refused under the readers' key
        and is accepted under this one. That is the discriminating detail.
        """
        response = _persisted_item(
            _body(
                approval_config={
                    "schemaMappingEnabled": 0,
                    "schemaMappingConfidenceThreshold": 80,
                }
            )
        )["approval_config"]

        assert response == {"schemaMappingEnabled": 0, "schemaMappingConfidenceThreshold": 80}


class TestArmThreeSurvivesAsABackstop:
    """`_submitted_approval_config` still writes no attribute for a non-dict.

    **Validates: Requirements 10.1**

    ``create_pipeline`` no longer reaches this arm — the boundary refusal 400s a
    non-dict several hundred lines earlier — so it is pinned here directly, at the
    function. Without this, amending ``TestNonDictSubmissionWritesNoAttribute`` to
    expect a 400 would have silently dropped the only coverage of a branch that is
    still live for any other caller, and 2a.1's docstring names it as the shape p64
    tasks 2c/2d copy for ``quality_config``.
    """

    @pytest.mark.parametrize(
        "submitted",
        ["yes", 1, 0, True, False, None, [], ["schema_mapping_enabled"]],
        ids=["str", "int", "zero", "bool-true", "bool-false", "null", "empty-list", "list"],
    )
    def test_a_non_dict_still_returns_none(self, submitted):
        """`None` means "write no attribute", which is fail-closed.

        Every reader's own fallback is the same value the seeded defaults carry, so
        an absent attribute selects exactly the gates the defaults would.
        """
        assert _submitted_approval_config({"approval_config": submitted}) is None

    def test_an_absent_key_still_seeds_the_defaults(self):
        """Arm 1 at the function, so all three arms are pinned here, not just one."""
        assert _submitted_approval_config({}) == {
            "schema_mapping_enabled": True,
            "schema_mapping_confidence_threshold": Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD)),
            "code_generation_enabled": True,
            "always_approve_schema_changes": True,
        }

    def test_a_dict_still_persists_verbatim(self):
        """Arm 2 at the function. The validator is a separate concern from this one.

        ``_submitted_approval_config`` interprets and ``_validate_approval_config``
        refuses; neither does the other's job. This asserts the interpretation is
        unchanged by 2a.2 — including for a value the validator would refuse, which
        is what proves the two are separate rather than one function in two names.
        """
        assert _submitted_approval_config({"approval_config": {"schema_mapping_enabled": 0}}) == {
            "schema_mapping_enabled": 0
        }
