# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: the wizard's Approval_Config wire keys are the keys the readers read.

Three artifacts have to agree on four attribute names and one unit, and none of
them can import the others:

- ``dashboard-react/src/pages/CreatePipelinePage.tsx`` —
  ``buildApprovalConfigRequest`` is the **single normalisation authority** for the
  Approval_Config's wire spelling. What it emits is what
  ``POST /pipelines/provision`` carries.
- ``lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines_crud.py`` —
  ``_submitted_approval_config`` persists a submitted map **verbatim** (p64 task
  2a.1), and ``_default_approval_config`` seeds the map when the key is absent. So
  the wire spelling *is* the stored spelling; nothing translates in between.
- the readers — ``schema_mapping/lambda_function.py`` for
  ``schema_mapping_enabled`` and ``always_approve_schema_changes``, and
  ``test_code.resolve_code_approval_setting`` for ``code_generation_enabled``.

When they disagree, the four controls on the wizard's step 5 do nothing while
appearing to have been saved. That is exactly what happened up to p64: the wizard
posted its own camelCase state (``schemaMappingEnabled``, …) against snake_case
readers, so the operator's toggles were persisted under keys no reader ever asked
for and every gate silently took its own default.

**Why this file exists rather than an assertion inside either side.** The
reconciliation is a *cross-language* agreement, so a test on one side can only ever
check that side against itself — which is how the old tripwire
(``test_create_pipeline_approval_config.py::TestKnownCasingGap``, now
``TestReconciledCasing``) came to pin the gap without being able to catch its fix:
it fed a camelCase payload to the Python handler, so it read the same before and
after a client-side change. And
``tests/contracts/api_responses/test_create_pipeline_wizard_contract.py``
transcribes the payload, so it cannot notice the wizard drifting away from it
either.

Why the root of ``tests/contracts/`` rather than ``api_responses/``: the subject is
neither a response body nor an event payload but a request-key vocabulary shared
across a language boundary — the same reason
``test_approval_window_cross_language.py`` sits here. The root ``conftest.py``
applies ``@pytest.mark.contract`` to everything under ``tests/contracts/``, so it
still runs in the contracts layer of ``make test-fast``.

Why parse the sources instead of restating the four names: a second list that has to
agree is the drift this exists to prevent. The technique is this repo's existing one
(``test_approval_window_cross_language.py``, ``tests/cdk/stacks/test_asl_state_names_pinned.py``):
read the literals out of the producer and assert against them, so changing one side
alone fails here.

Validates: p64 Requirement 10.1 (a submitted Approval_Config is honoured);
``.kiro/specs/p63-inert-controls-and-gate-integrity`` R1.2 (the reconciliation holds
in **one** direction, with no second normalisation authority).
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_CREATE_PIPELINE_PAGE = _PROJECT_ROOT / "dashboard-react" / "src" / "pages" / "CreatePipelinePage.tsx"
_WIZARD_TYPES = _PROJECT_ROOT / "dashboard-react" / "src" / "pages" / "wizard" / "types.ts"
_PIPELINES_CRUD = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_dashboard_api" / "handlers" / "pipelines_crud.py"
_SCHEMA_MAPPING = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_schema_mapping" / "lambda_function.py"
_TEST_CODE = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_test_code" / "lambda_function.py"
#: The other writer of the Approval_Config map, and the reader of the one key no
#: writer sets. Both are p64 task 2a.3's subject — see
#: :class:`TestTheInfrastructureGateHasNoProducer`.
_APPROVAL_CONFIG_LAMBDA = _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_approval_config" / "lambda_function.py"
_CREATE_INFRASTRUCTURE = (
    _PROJECT_ROOT / "lambda" / "agentic_data_ingestion_create_infrastructure" / "lambda_function.py"
)

sys.path.insert(0, os.path.join(str(_PROJECT_ROOT), "shared"))

from config import CONFIDENCE_AUTO_APPROVE_THRESHOLD

#: The three boolean gates, each with the reader that reads it. The threshold is
#: excluded because it has **no reader anywhere** — ``schema_mapping:1777`` and
#: ``:1902`` read ``CONFIDENCE_AUTO_APPROVE_THRESHOLD`` directly — and is covered by
#: its own unit test below instead. p63 R9 owns giving it one.
_BOOLEAN_GATES = (
    ("schema_mapping_enabled", _SCHEMA_MAPPING),
    ("always_approve_schema_changes", _SCHEMA_MAPPING),
    ("code_generation_enabled", _TEST_CODE),
)

_CONFIDENCE_KEY = "schema_mapping_confidence_threshold"


def _read(path: Path) -> str:
    if not path.is_file():
        pytest.fail(f"expected artifact is missing: {path}")
    return path.read_text(encoding="utf-8")


def _ts_block(source: str, declaration: str) -> str:
    """The text of a top-level TS declaration, from its opening to its close.

    Bounded by the first line that is exactly ``}`` at column 0 after the
    declaration, which is what Prettier guarantees for a top-level function or
    interface in this codebase. Deliberately not a brace counter: a wrong slice here
    should fail loudly rather than silently match a neighbouring declaration's keys.
    """
    start = source.find(declaration)
    if start == -1:
        pytest.fail(f"could not find {declaration!r} — has it been renamed or removed?")
    end = source.find("\n}\n", start)
    if end == -1:
        pytest.fail(f"could not find the end of {declaration!r}")
    return source[start:end]


def _py_block(source: str, declaration: str) -> str:
    """The text of a top-level Python ``def``, up to the next top-level construct.

    Python has no closing brace to anchor on, so the end is the next line that
    starts at column 0 with a construct that can only be top level.
    """
    start = source.find(declaration)
    if start == -1:
        pytest.fail(f"could not find {declaration!r} — has it been renamed or removed?")
    following = re.search(r"\n(?=(?:def |class |@|#: |#\s))", source[start + len(declaration) :])
    end = len(source) if following is None else start + len(declaration) + following.start()
    return source[start:end]


def _wire_keys_emitted_by_the_wizard() -> set[str]:
    """The snake_case keys ``buildApprovalConfigRequest`` puts on the wire."""
    body = _ts_block(
        _read(_CREATE_PIPELINE_PAGE),
        "export function buildApprovalConfigRequest(",
    )
    # Object-literal property names in the returned map. Anchored to
    # `snake_case:` so a camelCase property cannot match and be counted as fine.
    keys = set(re.findall(r"^ {4}([a-z][a-z0-9]*(?:_[a-z0-9]+)+):", body, re.MULTILINE))
    if not keys:
        pytest.fail("parsed no wire keys out of buildApprovalConfigRequest — the regex or the shape moved")
    return keys


class TestTheWizardSpellsTheAttributesTheReadersRead:
    """Key-set agreement across the language boundary.

    **Validates: p64 Requirement 10.1**
    """

    def test_every_wire_key_is_read_by_the_lambda_that_owns_it(self):
        """Each emitted key appears as a read at its reader, spelled identically."""
        emitted = _wire_keys_emitted_by_the_wizard()

        for key, reader_path in _BOOLEAN_GATES:
            assert key in emitted, (
                f"the wizard no longer sends {key!r}, but {reader_path.parent.name} still reads it — "
                "the control has gone inert again"
            )
            assert f'"{key}"' in _read(reader_path), (
                f"{reader_path.parent.name} no longer reads {key!r} — if it was renamed, the wizard "
                "and this test must move with it (and see p63 R2.4 on keeping a reader-side fallback)"
            )

    def test_the_wire_carries_no_key_that_nothing_consumes(self):
        """Set equality against the seeded map, so an extra key fails too.

        ``_submitted_approval_config`` persists verbatim, so an unrecognised key is
        stored and read by nobody — the same defect as the casing gap, narrower. The
        comparison is against ``_default_approval_config``'s own literal rather than
        a list written here, because that function is what a pipeline gets when the
        wizard sends nothing: the two maps describing one setting must have the same
        keys or the absent-input and present-input paths diverge.
        """
        seeded = set(
            re.findall(
                r'^ {8}"([a-z_]+)":',
                _py_block(_read(_PIPELINES_CRUD), "def _default_approval_config()"),
                re.MULTILINE,
            )
        )
        assert seeded, "parsed no keys out of _default_approval_config — the regex or the shape moved"

        assert _wire_keys_emitted_by_the_wizard() == seeded

    def test_no_camel_case_spelling_survives_on_the_wire(self):
        """The wizard's state field names must not reach the request body.

        The defect itself, stated as a fact about the producer. Each camelCase name
        is read out of ``wizard/types.ts``'s ``ApprovalConfig`` so this cannot pass
        by the interface being renamed.
        """
        interface = _ts_block(_read(_WIZARD_TYPES), "export interface ApprovalConfig {")
        camel_fields = set(re.findall(r"^ {2}([a-z]+[A-Z][A-Za-z]*):", interface, re.MULTILINE))
        assert len(camel_fields) == 4, f"expected 4 camelCase state fields, parsed {sorted(camel_fields)}"

        body = _ts_block(_read(_CREATE_PIPELINE_PAGE), "export function buildApprovalConfigRequest(")
        for field in camel_fields:
            # The camelCase name appears only as the READ side (`config.<field>`),
            # never as an emitted property (`<field>:`).
            assert not re.search(rf"^ +{re.escape(field)}:", body, re.MULTILINE), (
                f"{field!r} is being emitted onto the wire; no reader reads that spelling"
            )


class TestThereIsExactlyOneNormalisationAuthority:
    """p63 R1.2's one-direction requirement, pinned from both sides.

    **Validates: p63 R1.2**
    """

    def test_the_submit_site_serialises_rather_than_passing_state_through(self):
        """`approval_config:` at the submit site goes through the serialiser.

        This is the assertion that would have caught the original defect. A revert
        to `approval_config: state.approvalConfig` fails here.
        """
        source = _read(_CREATE_PIPELINE_PAGE)

        assert "approval_config: buildApprovalConfigRequest(state.approvalConfig)" in source
        assert "approval_config: state.approvalConfig" not in source

    def test_the_handler_adds_no_second_authority(self):
        """No server-side camelCase translation crept in beside the client's.

        Two spellings on the wire with nothing to arbitrate between them is what
        R1.2 forbids, and it would be worse than cosmetic here: ``schema_mapping``
        reads ``not schema_mapping_enabled`` by truthiness, not by identity, so a
        second accepted spelling is a second way for a non-boolean falsy value to
        disable a governance gate.
        """
        source = _read(_PIPELINES_CRUD)

        for camel in (
            "schemaMappingEnabled",
            "schemaMappingConfidenceThreshold",
            "codeGenerationEnabled",
            "alwaysApproveSchemaChanges",
        ):
            # A normaliser has to name the key it is translating FROM, and in Python
            # that means a string literal. Prose is unaffected: the handler's
            # docstrings discuss these names in double backticks, never in quotes.
            assert f'"{camel}"' not in source, (
                f"{camel!r} appears as a string literal in {_PIPELINES_CRUD.name} — that is a second "
                "normalisation authority, which p63 R1.2 forbids"
            )
            assert f"'{camel}'" not in source


class TestTheConfidenceUnitAgrees:
    """The threshold is a fraction on the wire and on the record, never a percent.

    **Validates: p64 Requirement 10.1**

    The trap a key-only rename would have activated. The wizard's slider is a
    percent (``min={0} max={100} step={5}``, rendered with ``%``); the persisted
    attribute's unit is set by its only writer, ``_default_approval_config``, which
    seeds ``Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))``. Renaming the key and
    leaving the unit would have persisted ``80`` under a key that any
    fraction-comparing reader reads as "never auto-approve".
    """

    def test_the_seeded_default_is_in_fraction_units(self):
        """The unit's own definition: the shared constant is a fraction."""
        assert 0 < CONFIDENCE_AUTO_APPROVE_THRESHOLD <= 1

    def test_the_wizard_divides_the_percent_before_sending_it(self):
        """The conversion exists and is a division by 100, in one place."""
        source = _read(_CREATE_PIPELINE_PAGE)

        converter = _ts_block(source, "export function percentToConfidenceFraction(")
        assert "percent / 100" in converter

        emitter = _ts_block(source, "export function buildApprovalConfigRequest(")
        assert re.search(
            rf"{_CONFIDENCE_KEY}: percentToConfidenceFraction\(",
            emitter,
        ), f"{_CONFIDENCE_KEY} is being emitted without the unit conversion"

    def test_the_slider_is_still_a_percent(self):
        """If the slider's domain ever becomes 0-1, the division becomes wrong.

        The conversion is only correct while the two units differ, so the premise is
        asserted rather than assumed — a future change to a 0-1 slider must land
        here and delete the division, not silently double-convert.
        """
        model_step = _PROJECT_ROOT / "dashboard-react" / "src" / "pages" / "wizard" / "WizardStepModel.tsx"
        source = _read(model_step)

        assert "min={0}" in source
        assert "max={100}" in source
        # And the wizard's own initial value is in that domain, not in 0-1.
        initial = re.search(r"schemaMappingConfidenceThreshold: (\d+)", _read(_WIZARD_TYPES))
        assert initial is not None, "could not read the wizard's default threshold"
        assert 1 < int(initial.group(1)) <= 100


class TestAbsenceStillFailsClosed:
    """A key the wizard does not send must still route to a human.

    **Validates: p64 Requirement 10.2**

    Making the controls live is a reduction in oversight when — and only when — an
    operator asks for it. The absent case must be unaffected, which is what makes a
    stale bundle or a pre-p64 record safe.
    """

    @pytest.mark.parametrize("key", ["schema_mapping_enabled", "always_approve_schema_changes"])
    def test_the_schema_mapping_reads_default_to_true(self, key):
        """Both `schema_mapping` reads spell their own fallback as `True`."""
        source = _read(_SCHEMA_MAPPING)

        assert re.search(rf'\.get\(\s*"{key}",\s*True\s*\)', source), (
            f"{key}'s read in schema_mapping no longer defaults to True — an absent key would "
            "now skip a governance gate"
        )

    def test_the_code_gate_defaults_to_true_and_checks_identity(self):
        """`resolve_code_approval_setting`'s absent and non-boolean arms both fail closed.

        ``LGR-69ca78b-1``'s read-side counterpart: only an explicit boolean ``False``
        disables. Identity rather than truthiness is the load-bearing half —
        ``"false"`` is a truthy string and ``0`` a falsy int, so a truthiness test
        would invert the operator's setting in both directions.
        """
        source = _read(_TEST_CODE)
        body = _py_block(source, "def resolve_code_approval_setting(")

        assert '"code_generation_enabled" not in approval_config' in body
        assert "return True, _SETTING_FROM_DEFAULT" in body
        assert "if raw is True or raw is False:" in body


_INFRASTRUCTURE_GATE_KEY = "infrastructure_approval_enabled"


class TestTheInfrastructureGateHasNoProducer:
    """``infrastructure_approval_enabled`` is a recorded seam, not a live setting.

    **Validates: Requirements 10.3**

    p64 task 2a.3 took R10.3's **second** arm — record it as a producer-less seam
    following ``LGR-5d68eeb-3`` — so the deliverable is documentation at the guard
    (``create_infrastructure/lambda_function.py:606``) plus this class, which is
    what stops a producer landing silently and making that documentation false.

    The asymmetry is the whole assertion: the **reader must stay** and the
    **writers must not gain the key**. It is deliberately a source scan rather than
    a behavioural test, because the thing being pinned is the *absence* of a code
    path, and absence is not observable by calling anything.

    Sibling of ``TestTheWizardSpellsTheAttributesTheReadersRead`` inverted: that
    class asserts every wire key has a reader, this one asserts the one key with a
    reader has no writer. Same defect class as the casing gap — a setting and its
    consumer failing to meet — approached from the other end.

    **Why a producer is not simply missing work.** ``:937`` of that Lambda reads
    ``if overall_status != "success" and not approval_required: raise``, a
    deliberate fail-loud. A truthy value here inverts it and returns 200 for failed
    provisioning, and no Step Functions Choice consumes
    ``$.create_infrastructure_result.approval_required``, so nothing downstream
    reviews the result. If one of these assertions fails, the fix is not to relax
    it — read the guard's docstring and answer the product question it names.
    """

    def test_the_reader_still_exists(self):
        """Delete the reader and the seam is gone, so the record must go with it.

        Pinned because the rest of this class asserts absences: if the reader were
        removed, every other test here would pass vacuously while the documented
        posture had stopped being true.
        """
        source = _read(_CREATE_INFRASTRUCTURE)
        assert f'"{_INFRASTRUCTURE_GATE_KEY}"' in source, (
            f"create_infrastructure no longer reads {_INFRASTRUCTURE_GATE_KEY!r} — if the gate was "
            "removed outright, delete the seam record at determine_infrastructure_approval_requirement "
            "and this class with it, rather than leaving a record describing a reader that is gone"
        )
        assert f'.get("{_INFRASTRUCTURE_GATE_KEY}", False)' in source, (
            f"{_INFRASTRUCTURE_GATE_KEY!r}'s read no longer defaults to False — an absent key must "
            "select 'no approval required', which is what makes today's every-execution posture safe"
        )

    def test_the_seeded_map_does_not_carry_it(self):
        """``_default_approval_config`` is the absent-input writer.

        Parsed out of the function rather than compared against a list written
        here, for the reason the module docstring gives. Scoped to the ``def``
        block on purpose: the key *is* named in ``_validate_approval_config``'s
        docstring a few hundred lines away, and a whole-file scan would read that
        prose as a producer.
        """
        seeded = _py_block(_read(_PIPELINES_CRUD), "def _default_approval_config()")

        assert _INFRASTRUCTURE_GATE_KEY not in seeded, (
            f"_default_approval_config now seeds {_INFRASTRUCTURE_GATE_KEY!r}, so the gate has a "
            "producer and is no longer the seam p64 R10.3 recorded. Note this also breaks "
            "test_the_wire_carries_no_key_that_nothing_consumes unless the wizard emits it too"
        )

    def test_the_wizard_does_not_emit_it(self):
        """No UI path sets it, so an operator cannot turn it on by accident."""
        assert _INFRASTRUCTURE_GATE_KEY not in _wire_keys_emitted_by_the_wizard(), (
            f"the wizard now emits {_INFRASTRUCTURE_GATE_KEY!r} — that is a producer, and it routes "
            "to a gate no Step Functions Choice consumes"
        )

    def test_the_update_endpoint_does_not_set_it(self):
        """The third writer of the map, and the only one that can amend a live pipeline.

        ``update_pipeline_approval_config`` requires three other gates and writes
        the submitted map verbatim, so it is the most likely place a producer
        appears without anyone touching the create path.
        """
        assert _INFRASTRUCTURE_GATE_KEY not in _read(_APPROVAL_CONFIG_LAMBDA), (
            f"the approval_config Lambda now references {_INFRASTRUCTURE_GATE_KEY!r}; if it can be "
            "set on an existing pipeline the seam is live, and the guard's fail-loud at "
            "create_infrastructure:937 becomes reachable-off"
        )

    def test_no_step_functions_choice_consumes_this_steps_approval_flag(self):
        """The seam has no destination, which is the stronger half of the record.

        ``$.create_infrastructure_result`` is written (``orchestration-stack.ts``)
        and never branched on, unlike ``$.schema_mapping_result.approval_required``
        and ``$.test_code_result.approval_required``. Asserted here rather than in
        ``tests/cdk/`` because the claim being protected is a cross-component one:
        a Python reader whose answer nothing consumes.

        Deliberately **not** an assertion about the two Choices that do exist —
        ``LGR-69ca78b-1`` is a standing invariant and owns those; this only says a
        third one has not appeared for this step.
        """
        asl = _read(_PROJECT_ROOT / "deploy-cdk" / "lib" / "orchestration-stack.ts")

        assert "$.create_infrastructure_result.approval_required" not in asl, (
            "a Choice now reads $.create_infrastructure_result.approval_required — the seam has "
            "gained a destination, so re-read the guard's docstring: the fail-loud at "
            "create_infrastructure:937 and this new branch now both claim the error path"
        )
