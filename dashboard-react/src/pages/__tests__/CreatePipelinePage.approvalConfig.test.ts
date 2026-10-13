// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for `buildApprovalConfigRequest` — the Approval_Config transform
 * applied to the create/provision request body (p64, authorised addition to task
 * 2a).
 *
 * **What was broken.** `handleSubmit` posted `approval_config: state.approvalConfig`
 * verbatim, and that state is camelCase (`wizard/types.ts`), while every reader
 * reads snake_case: `schema_mapping/lambda_function.py:1669`
 * (`schema_mapping_enabled`, default `True`), `:1657`
 * (`always_approve_schema_changes`, default `True`) and
 * `test_code.resolve_code_approval_setting` (`code_generation_enabled`, only an
 * explicit boolean `False` disables). Since p64 task 2a.1 the handler persists a
 * submitted `approval_config` **verbatim**, so those four camelCase keys landed on
 * the record and nothing ever read them — step 5's toggles were decorative.
 *
 * **Where the fix lives, and why here.** p63 R1.2 requires exactly **one**
 * normalisation authority; p64 put it at this boundary. This side was chosen over
 * a server-side normaliser because it adds **no new accepted wire shape** — the
 * edit path's `PipelineFormModal.tsx:103-106` already submitted these exact
 * snake_case keys — whereas accepting camelCase server-side would legalise a second
 * spelling forever and, because `schema_mapping:1669` tests
 * `not schema_mapping_enabled` by truthiness rather than by identity, would newly
 * let a non-boolean falsy value disable a governance gate.
 *
 * These tests fail against the unfixed tree by construction: the function under
 * test did not exist, and the submit site passed state straight through.
 */
import { describe, it, expect } from 'vitest';
import {
  buildApprovalConfigRequest,
  confidenceFractionToPercent,
  percentToConfidenceFraction,
} from '../CreatePipelinePage';
import { INITIAL_WIZARD_STATE } from '../wizard/types';
import type { ApprovalConfig } from '../wizard/types';

const ALL_OFF: ApprovalConfig = {
  schemaMappingEnabled: false,
  schemaMappingConfidenceThreshold: 80,
  codeGenerationEnabled: false,
  alwaysApproveSchemaChanges: false,
};

describe('buildApprovalConfigRequest', () => {
  it('emits exactly the four keys the readers read', () => {
    const out = buildApprovalConfigRequest(INITIAL_WIZARD_STATE.approvalConfig);

    // Set equality, not containment: an extra key would be persisted verbatim by
    // `_submitted_approval_config` and read by nobody — the same defect class,
    // narrower. A missing key would silently hand that gate back to its default.
    expect(new Set(Object.keys(out))).toEqual(
      new Set([
        'schema_mapping_enabled',
        'schema_mapping_confidence_threshold',
        'code_generation_enabled',
        'always_approve_schema_changes',
      ]),
    );
  });

  it('does not leak any camelCase key onto the wire', () => {
    const out = buildApprovalConfigRequest(ALL_OFF) as Record<string, unknown>;

    for (const stateKey of Object.keys(ALL_OFF)) {
      expect(out).not.toHaveProperty(stateKey);
    }
  });

  it('emits all four for an omitted options argument, so the create path is unchanged', () => {
    // The default arm. `CreatePipelinePage`'s submit site calls this with one
    // argument and must keep sending four keys; the cross-language contract test
    // asserts key-set equality against `_default_approval_config`'s own literal.
    expect(Object.keys(buildApprovalConfigRequest(ALL_OFF))).toHaveLength(4);
    expect(Object.keys(buildApprovalConfigRequest(ALL_OFF, {}))).toHaveLength(4);
    expect(
      Object.keys(buildApprovalConfigRequest(ALL_OFF, { includeConfidenceThreshold: true })),
    ).toHaveLength(4);
  });
});

describe('buildApprovalConfigRequest and the reader-less confidence threshold', () => {
  it('omits the threshold key entirely under includeConfidenceThreshold: false', () => {
    // `PipelineFormModal`'s edit path. Key-set absence rather than a value
    // assertion: `edit_pipeline` merges `approval_config` per key
    // (`LGR-7bea749-1`), so an omitted key leaves the record's value alone while a
    // present one writes it. `undefined` under the key would still be a written key
    // to `Object.keys` and to the merge, so the projection is deliberate.
    const out = buildApprovalConfigRequest(ALL_OFF, { includeConfidenceThreshold: false });

    expect(Object.keys(out)).not.toContain('schema_mapping_confidence_threshold');
    expect(new Set(Object.keys(out))).toEqual(
      new Set([
        'schema_mapping_enabled',
        'code_generation_enabled',
        'always_approve_schema_changes',
      ]),
    );
  });

  it('still carries the three gates that DO have readers', () => {
    // The omission is one key, not a reduced body. Dropping a gate here would make
    // that control inert in the modal, which is the defect being removed.
    const out = buildApprovalConfigRequest(INITIAL_WIZARD_STATE.approvalConfig, {
      includeConfidenceThreshold: false,
    });

    expect(out.schema_mapping_enabled).toBe(true);
    expect(out.code_generation_enabled).toBe(true);
    expect(out.always_approve_schema_changes).toBe(true);
  });

  it('omits the threshold regardless of the state value it was given', () => {
    // Not a "skip the default" special case: the key is excluded because it has no
    // reader, so a deliberately-set state value is excluded too. p63 R9 owns giving
    // it a reader, and that is when this option should be revisited.
    for (const percent of [0, 55, 85, 100]) {
      const out = buildApprovalConfigRequest(
        { ...ALL_OFF, schemaMappingConfidenceThreshold: percent },
        { includeConfidenceThreshold: false },
      );
      expect(Object.keys(out)).not.toContain('schema_mapping_confidence_threshold');
    }
  });

  it('does not mutate or alias the full map when projecting', () => {
    const config = { ...INITIAL_WIZARD_STATE.approvalConfig };
    const full = buildApprovalConfigRequest(config);
    const projected = buildApprovalConfigRequest(config, { includeConfidenceThreshold: false });

    expect(full.schema_mapping_confidence_threshold).toBe(0.8);
    expect(projected).not.toBe(full);
  });

  it('carries an operator-selected false through as false', () => {
    // The whole point. Every reader defaults to `True`, so before this a `false`
    // toggle was indistinguishable from an absent key.
    const out = buildApprovalConfigRequest(ALL_OFF);

    expect(out.schema_mapping_enabled).toBe(false);
    expect(out.code_generation_enabled).toBe(false);
    expect(out.always_approve_schema_changes).toBe(false);
  });

  it('carries booleans as booleans, never as truthy stand-ins', () => {
    // `resolve_code_approval_setting` checks `raw is True` / `raw is False` and
    // fails closed on anything else, so `0`/`1`/`'false'` would all resolve to
    // "approval required" regardless of intent.
    const out = buildApprovalConfigRequest(INITIAL_WIZARD_STATE.approvalConfig);

    expect(typeof out.schema_mapping_enabled).toBe('boolean');
    expect(typeof out.code_generation_enabled).toBe('boolean');
    expect(typeof out.always_approve_schema_changes).toBe('boolean');
  });

  it('converts the slider percent to the fraction the attribute is stored in', () => {
    expect(buildApprovalConfigRequest(ALL_OFF).schema_mapping_confidence_threshold).toBe(0.8);

    // And the value that reaches the wire is JSON-clean at slider granularity —
    // no `0.30000000000000004`, which would persist as a `Decimal` of the same
    // noise. Division by 100 is what buys this; multiplication would not.
    for (let percent = 0; percent <= 100; percent += 5) {
      const wire = buildApprovalConfigRequest({
        ...ALL_OFF,
        schemaMappingConfidenceThreshold: percent,
      }).schema_mapping_confidence_threshold;
      expect(JSON.parse(JSON.stringify(wire))).toBe(percent / 100);
      expect(String(wire).length).toBeLessThanOrEqual(4);
    }
  });

  it('sends the wizard default as 0.8 — below the shared floor, and left visible', () => {
    // Stated as a test rather than only as a comment, because it is a known
    // disagreement someone will otherwise "fix" by clamping. `shared/config.py`'s
    // `CONFIDENCE_AUTO_APPROVE_THRESHOLD` is 0.85 and the wizard's default slider
    // position is 80%. The attribute has no reader today, so nothing enforces
    // either number; p63 R9/D2 owns making the per-pipeline value a stricter-only
    // override, and it chose to REFUSE a loosening value rather than clamp it,
    // precisely so the displayed value and the enforced value cannot diverge.
    const out = buildApprovalConfigRequest(INITIAL_WIZARD_STATE.approvalConfig);
    expect(out.schema_mapping_confidence_threshold).toBe(0.8);
  });

  it('agrees with the reader defaults for an untouched wizard', () => {
    // The regression this change could most easily have caused. Until the casing
    // was reconciled, an untouched wizard's values reached no reader, so a
    // pipeline got each reader's own fallback — all three `True`. Now the wizard's
    // initial state IS what gets persisted, so any initial value that disagrees
    // with its reader default would change a gate for every new pipeline with
    // nobody having asked. `alwaysApproveSchemaChanges` was `false` and is now
    // `true` for exactly this reason (p63 R1.3 named the disagreement).
    const out = buildApprovalConfigRequest(INITIAL_WIZARD_STATE.approvalConfig);

    expect(out.schema_mapping_enabled).toBe(true);
    expect(out.code_generation_enabled).toBe(true);
    expect(out.always_approve_schema_changes).toBe(true);
  });
});

describe('the confidence unit conversion', () => {
  it('round-trips every value the slider can hold', () => {
    // `min={0} max={100} step={5}` (`WizardStepModel.tsx`), so this is the whole
    // domain rather than a sample.
    for (let percent = 0; percent <= 100; percent += 5) {
      expect(confidenceFractionToPercent(percentToConfidenceFraction(percent))).toBe(percent);
    }
  });

  it('rounds on the way back, because 55 % is not exact in IEEE-754', () => {
    // Guarding the reason `confidenceFractionToPercent` rounds at all, with a
    // witness the slider can actually hold: `0.55 * 100` is 55.00000000000001, so
    // an unrounded inverse feeds the Slider a value off its own step grid. 55 is
    // the ONLY multiple of 5 that does this — `0.8 * 100` is exactly 80, so
    // picking the default as the witness would have proved nothing (it did not:
    // this test asserted `0.8` first and failed). Off the step grid the problem is
    // ordinary: 0.07, 0.14, 0.28, 0.29 and 0.56–0.58 are all noisy, and a stored
    // value need not be a multiple of 5.
    expect(0.55 * 100).not.toBe(55);
    expect(confidenceFractionToPercent(0.55)).toBe(55);
    expect(Number.isInteger(confidenceFractionToPercent(0.55))).toBe(true);
    expect(confidenceFractionToPercent(0.07)).toBe(7);
  });

  it('does not clamp an out-of-range stored value', () => {
    // p63 D2 rejected silent clamping: a control that displays 70% while the
    // system enforces 85% is a new inert control. Range refusal belongs to the API
    // boundary (p64 task 2a.2 / R10.4), where it is visible. Showing 150 as 150
    // beats quietly redrawing it as 100.
    expect(confidenceFractionToPercent(1.5)).toBe(150);
    expect(confidenceFractionToPercent(-0.2)).toBe(-20);
  });
});
