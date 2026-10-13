# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Feature: p34-code-approval-gate-honors-config, relocated guard: P28 R13.1
"""P28 R13.1's guard, relocated: approval is skipped ONLY by pipeline configuration.

**The subject, stated once so it stays greppable:** approval is skipped *only* by
the pipeline's own approval configuration, and **never** by a keyword quality
score. Token presence cannot waive a human gate.

**Validates: Requirements 8.2, 8.4**

Where this moved from, and why
------------------------------
These two tests lived in
``tests/unit/lambdas/create_code/test_no_keyword_self_approval.py`` —
``test_template_boilerplate_does_not_self_approve`` (``:103``, calling the
decision at ``:112``) and ``test_approval_skipped_only_by_pipeline_configuration``
(``:130``, asserting ``:134`` disabled → ``False`` and ``:136`` disabled +
regeneration → ``True``). Both called
``create_code.determine_approval_requirement`` (``create_code:304-339``) directly.

``LGR-f077f1b-1`` (**template / ratified**) moves ownership of the
code-generation Approval_Policy to ``test_code`` and **deletes** that function
rather than renaming it, because a renamed field with no consumer is the very
objection requirement 3 exists to answer. ``LGR-f077f1b-2`` (**template /
ratified**) is the companion decision for this file: the deleted function's test
coverage **relocates onto its successors** rather than being characterized in
place first, and *the relocation must carry P28 R13.1's subject with it, because
``test_no_keyword_self_approval.py:130`` is that requirement's only guard.*

So this is a **relocation, not a deletion and not a rewrite**. The test names are
kept verbatim (``LGR-5906ec6-2`` and ``LGR-60c4985-6``, both **template /
ratified**: this repo encodes a deliberate hold as a test assertion, so changing
one must read as a deliberate reversal rather than as a regression). The subject
is untouched by p34 and must survive the move — which is why this file lands and
passes **before** the deletion, not after.

The decision surface it moved onto
----------------------------------
* ``resolve_code_approval_setting`` (``test_code/lambda_function.py:702-745``) —
  takes the already-read Pipeline_Record, returns
  ``(setting, "pipeline_record" | "default")``.
* ``determine_merged_approval_requirement`` (``:819-887``) — the Approval_Policy,
  the disjunction of ``pipeline_config``, ``quality_gate`` and ``regeneration``,
  returning the decision **and** the per-input map R3.5 records.

What stayed behind in ``create_code``, deliberately
---------------------------------------------------
The *code-text* half of P28 R13.1 is about functions p34 does not touch, so it
does not move:

* ``:139 test_quality_metrics_carry_no_keyword_score_or_gate_booleans`` —
  ``calculate_code_quality_metrics`` carries no ``quality_score`` /
  ``has_logging`` / ``has_error_handling``;
* ``:160 test_approval_payload_carries_no_fabricated_review_verdicts`` —
  ``create_approval_payload`` fabricates no token-derived verdicts;
* ``:174 test_keyword_score_machinery_deleted_from_source`` — a source-text grep
  over ``create_code/lambda_function.py``, unaffected by decision D2.

Those three keep ``_TEMPLATE_BOILERPLATE`` alive where it is still consumed. What
relocates here is the **decision**: which is exactly the half whose home decision
D2 removes.

Why this is not a third copy of Property 1 or Property 2
--------------------------------------------------------
``test_code_approval_setting_prop.py`` (Property 1) and
``test_merged_approval_policy_prop.py`` (Property 2) already cover the two
helpers' input spaces exhaustively. Neither *names* P28 R13.1's subject, and a
property over a generated space is not a greppable guard for a named
requirement. These two examples exist so that the subject — skipped only by
configuration, never by a keyword score — is provable by name in its new home.

Ledger provenance
-----------------
``LGR-f077f1b-1``, ``LGR-f077f1b-2`` (both **template / ratified**) as above.
``LGR-5906ec6-2`` / ``LGR-60c4985-6`` (**template / ratified**) supply the
legible-reversal convention. P28 R13.1 itself has **no** ledger record — searched;
it exists only as a spec requirement in ``.kiro/specs/p28-reliability`` — which is
precisely why ``test_no_keyword_self_approval.py:130`` being its only guard
matters. ``LGR-3e45e65-1`` (**template / ratified**) is why this file does not
re-pin ``AWS_REGION``: ``tests/unit/conftest.py`` assigns it before any test
module in this directory is imported.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Account id is not pinned by tests/unit/conftest.py (only the region is —
# LGR-3e45e65-1), and env_utils is stubbed below in any case.
os.environ.setdefault("AWS_ACCOUNT_ID", "123456789012")

_TEST_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _TEST_CODE_DIR)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy / AWS-touching imports so the module loads in isolation. Same set as
# the sibling files in this directory; the module-level cache pop is the house
# pattern for lambda_function cross-contamination (several Lambda dirs expose a
# module of that name).
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(initialize_strands_agent_with_mcp=None),
    "agent_trace_plugin": MagicMock(),
    "audit_utils": MagicMock(write_agent_audit_entry=None),
    "prompt_template_loader": MagicMock(load_prompt_template=None),
    "metrics_facade": MagicMock(PipelineMetrics=None),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "model_config": MagicMock(resolve_model_id=MagicMock(return_value="model-id")),
    "code_interpreter_runner": MagicMock(),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="123456789012"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
}

with patch.dict("sys.modules", _mods), patch("boto3.client", MagicMock()), patch("boto3.resource", MagicMock()):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    """Several Lambda dirs expose a module named ``lambda_function``."""
    sys.modules["lambda_function"] = _mod
    yield


# ---------------------------------------------------------------------------
# The two record shapes the relocated assertions turn on
# ---------------------------------------------------------------------------

#: Approvals enabled by the default (R1.2) — the shape of every Pipeline_Record
#: written before p34, and the shape the original test expressed as
#: ``get_pipeline_metadata`` returning ``{}``.
_RECORD_APPROVALS_ENABLED_BY_DEFAULT = {"pipeline_id": "pipe-1"}

#: The one and only skip: the pipeline's own configuration, as an explicit JSON
#: boolean ``false``. This is ``create_code:132``'s ``disabled`` record verbatim.
_RECORD_APPROVALS_DISABLED = {"pipeline_id": "pipe-1", "approval_config": {"code_generation_enabled": False}}


def _decide(record: dict, *, quality_requires_approval: bool, regeneration_attempt: int):
    """Resolve the setting from ``record``, then merge — the two helpers in order.

    Returns ``(decision, inputs, source)``. This is the same two-step the handler
    performs (task 2.4 owns the wiring); expressing it here as one helper is what
    makes the relocated assertions read like the single call they replace.
    """
    config_requires_approval, source = _mod.resolve_code_approval_setting(record)
    decision, inputs = _mod.determine_merged_approval_requirement(
        config_requires_approval=config_requires_approval,
        quality_requires_approval=quality_requires_approval,
        regeneration_attempt=regeneration_attempt,
    )
    return decision, inputs, source


# ---------------------------------------------------------------------------
# P28 R13.1 / R13.4 — token presence cannot waive a human gate
# ---------------------------------------------------------------------------


def test_template_boilerplate_does_not_self_approve():
    """**Relocated from ``create_code/test_no_keyword_self_approval.py:103``.**

    Subject, unchanged: approval is skipped only by pipeline configuration, and
    **never** by a keyword quality score.

    Under the deleted gate, rendered-template boilerplate — which always contains
    ``logger``, ``try:``, ``def`` and ``import boto3`` — scored 100 on those
    tokens alone, skipped human approval and set ``requires_review=False``. P28
    R13.1 deleted that bypass. What proves it here is the ``quality_gate`` axis:
    with the configuration enabled (the default, R1.2), the merged decision is
    ``True`` **whatever** a quality answer says, so no scorer — keyword-based or
    otherwise — can waive the gate.

    Note what the reversal is *not*: this is not a new claim. It is
    ``create_code:112``'s claim, re-expressed on the successors of the function
    decision D2 deletes (``LGR-f077f1b-2``, **template / ratified**).
    """
    for quality_requires_approval in (False, True):
        decision, inputs, source = _decide(
            _RECORD_APPROVALS_ENABLED_BY_DEFAULT,
            quality_requires_approval=quality_requires_approval,
            regeneration_attempt=0,
        )
        assert decision is True, (
            "boilerplate on a pipeline with approvals enabled must still require approval "
            f"(quality_requires_approval={quality_requires_approval})"
        )
        # R1.2: the setting is absent from the record, so it defaults to enabled.
        assert source == "default"
        assert inputs["pipeline_config"] is True

    # And the recorded reason (R3.5) is the configuration, not a quality score:
    # this is the relocation of the original test's second half, where
    # ``create_approval_payload``'s ``requires_review`` mirrored the honest
    # decision instead of being computed from the keyword score.
    _, clean_inputs, _ = _decide(
        _RECORD_APPROVALS_ENABLED_BY_DEFAULT, quality_requires_approval=False, regeneration_attempt=0
    )
    assert clean_inputs == {"pipeline_config": True, "quality_gate": False, "regeneration": False}


def test_approval_skipped_only_by_pipeline_configuration():
    """**Relocated from ``create_code/test_no_keyword_self_approval.py:130`` — P28
    R13.1's only guard.** The ONLY approval bypass is the pipeline's own approval
    config.

    Both behavioural facts from the original are preserved exactly:

    * ``create_code:134`` — approvals disabled, attempt 0 → not required;
    * ``create_code:136`` — approvals disabled **plus** regeneration → required.
      Regenerated code is reviewer-flagged code and always escalates, even then
      (R2.3, relocated verbatim in intent from ``create_code:331-334``).

    The "only" clause is what the ``inputs`` map makes explicit: the skip is
    attributed to ``pipeline_config``, and ``quality_gate`` contributed nothing —
    there is no keyword score anywhere in the decision.
    """
    # create_code:134 — disabled, no regeneration → approval NOT required.
    decision, inputs, source = _decide(
        _RECORD_APPROVALS_DISABLED, quality_requires_approval=False, regeneration_attempt=0
    )
    assert decision is False
    # The skip is the pipeline record's own explicit boolean false, and nothing else.
    assert source == "pipeline_record"
    assert inputs == {"pipeline_config": False, "quality_gate": False, "regeneration": False}

    # create_code:136 — regenerated (reviewer-flagged) code always escalates,
    # even with routine approvals switched off.
    decision, inputs, source = _decide(
        _RECORD_APPROVALS_DISABLED, quality_requires_approval=False, regeneration_attempt=1
    )
    assert decision is True
    assert source == "pipeline_record"
    assert inputs["regeneration"] is True
    assert inputs["pipeline_config"] is False
