// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for `buildQualityConfigRequest` — the Quality_Config transform
 * applied to the create/provision request body (p64 task 2c.2).
 *
 * Validates: Requirements R7.1, R7.2, R7.3, R7.5 (and R6.6, in the
 * `data_quality_enabled` group).
 *
 * **What this closes.** p64 task 2c.1 made `quality_config` a validated peer of
 * `approval_config` on the create path, and nothing sent it. R7.3 requires the
 * setting be usable **at pipeline creation**, not only by editing afterwards, so
 * without this the whole sub-object was write-only from the server's side and
 * unreachable from the wizard.
 *
 * **The load-bearing assertion is the `data_quality_enabled` omission.** The other
 * two keys are always sent because their default is unconditional; that key is sent
 * only when an operator actually picked a value, because absence means "use this
 * template's own default" — on for S3 Tables, off for the other four. Sending
 * `false` for an untouched control would opt every new S3 Tables pipeline out of a
 * DQ block it has had since P28 and move its verdict from `enforced` to `advisory`.
 *
 * These tests fail against the pre-2c.2 tree by construction: the function did not
 * exist and the submit body carried no `quality_config` at all.
 */
import { describe, it, expect } from 'vitest';
import { buildQualityConfigRequest } from '../CreatePipelinePage';
import { INITIAL_WIZARD_STATE } from '../wizard/types';
import type { QualityConfig } from '../wizard/types';

describe('buildQualityConfigRequest', () => {
  it('emits the two unconditional keys for an untouched wizard, and only those', () => {
    const out = buildQualityConfigRequest(INITIAL_WIZARD_STATE.qualityConfig);

    // Set equality, not containment. An extra key would be persisted verbatim by
    // `_submitted_quality_config` and read by nobody; a missing one would hand
    // that setting back to a default the record no longer states.
    expect(new Set(Object.keys(out))).toEqual(new Set(['dedup_enabled', 'dedup_columns']));
  });

  it('agrees with the reader defaults for an untouched wizard', () => {
    // The regression this change could most easily cause, and the one
    // `alwaysApproveSchemaChanges` actually caused one attribute over. What this
    // function emits is what lands on the record verbatim, so an initial value
    // disagreeing with its reader's fallback changes behaviour for every new
    // pipeline with nobody having asked. `resolve_quality_config` returns
    // `dedup_enabled=False` / `dedup_columns=[]` for a record with no
    // `quality_config` at all, and `_default_quality_config` seeds the same pair.
    const out = buildQualityConfigRequest(INITIAL_WIZARD_STATE.qualityConfig);

    expect(out.dedup_enabled).toBe(false);
    expect(out.dedup_columns).toEqual([]);
  });

  it('does not leak any camelCase key onto the wire', () => {
    const config: QualityConfig = {
      dataQualityEnabled: true,
      dedupEnabled: true,
      dedupColumns: ['patient_id'],
    };
    const out = buildQualityConfigRequest(config) as Record<string, unknown>;

    for (const stateKey of Object.keys(config)) {
      expect(out).not.toHaveProperty(stateKey);
    }
  });

  it('carries booleans as booleans, never as truthy stand-ins', () => {
    // `_validate_quality_config` refuses a non-boolean with a 400 — including `1`
    // and `0`, because `isinstance(1, bool)` is False. So a truthy stand-in here
    // would not be coerced, it would fail the create.
    const out = buildQualityConfigRequest({
      dataQualityEnabled: false,
      dedupEnabled: true,
      dedupColumns: [],
    });

    expect(typeof out.dedup_enabled).toBe('boolean');
    expect(typeof out.data_quality_enabled).toBe('boolean');
  });

  it('carries an operator-selected dedup opt-in and its columns', () => {
    const out = buildQualityConfigRequest({
      dedupEnabled: true,
      dedupColumns: ['patient_id', 'visit_date'],
    });

    expect(out.dedup_enabled).toBe(true);
    expect(out.dedup_columns).toEqual(['patient_id', 'visit_date']);
  });

  it('sends an offending column verbatim rather than filtering it', () => {
    // `_validate_quality_config` refuses the entry with a 400 and does NOT drop
    // it, for `LGR-4ea43f3-4`'s reason one value class over: a silently altered
    // value is well-formed, plausible and wrong, and a dedup key list quietly
    // missing a column deduplicates on the wrong identity. Pre-empting that
    // refusal by sanitising here would reintroduce exactly the failure the server
    // refuses to allow. The wizard's Model-step guard is what stops the submit
    // reaching the boundary in the normal case.
    const out = buildQualityConfigRequest({
      dedupEnabled: true,
      dedupColumns: ['ok', 'not-ok'],
    });

    expect(out.dedup_columns).toEqual(['ok', 'not-ok']);
  });

  it('copies the column list rather than aliasing wizard state', () => {
    const config: QualityConfig = { dedupEnabled: true, dedupColumns: ['a'] };
    const out = buildQualityConfigRequest(config);

    expect(out.dedup_columns).not.toBe(config.dedupColumns);
    out.dedup_columns.push('b');
    expect(config.dedupColumns).toEqual(['a']);
  });

  it('reads a draft-restored undefined defensively (R7.5)', () => {
    // `useWizardDraft.readDraft` is a bare `JSON.parse(raw) as T` and
    // `handleRestoreDraft` replaces state rather than merging into it, so a draft
    // saved before `qualityConfig` existed restores it as `undefined` despite the
    // type. Such a draft must submit the documented defaults — which is what it
    // would have got before the field existed — not throw at submit.
    const out = buildQualityConfigRequest(undefined);

    expect(out.dedup_enabled).toBe(false);
    expect(out.dedup_columns).toEqual([]);
    expect(new Set(Object.keys(out))).toEqual(new Set(['dedup_enabled', 'dedup_columns']));
  });
});

describe('buildQualityConfigRequest and data_quality_enabled', () => {
  it('OMITS the key entirely when the operator did not set it', () => {
    // The assertion this task turns on. An existing S3 Tables pipeline has data
    // quality on by template default and stores no `data_quality_enabled`;
    // `_default_quality_config` deliberately seeds none either. Sending `false`
    // for an untouched control would write an explicit opt-OUT — removing the
    // row-level split and quarantine write that target type has had since P28, and
    // moving `shared/quality_verdict.py`'s answer from `enforced` to `advisory`
    // for a pipeline whose behaviour nobody chose to change. R6.6 forbids it in
    // both directions.
    //
    // `Object.keys` rather than a value check, because `{ data_quality_enabled:
    // undefined }` and an absent key are indistinguishable after
    // `JSON.stringify` but not to a reader of this object — and the point is that
    // the key is not there.
    const out = buildQualityConfigRequest({ dedupEnabled: false, dedupColumns: [] });

    expect(Object.keys(out)).not.toContain('data_quality_enabled');
    expect('data_quality_enabled' in out).toBe(false);
  });

  it('survives JSON serialisation without the key appearing', () => {
    // What actually goes on the wire, so `_validate_quality_config`'s
    // `KEY not in data` arm is the one that runs and the seeded per-template
    // default applies.
    const body = JSON.parse(
      JSON.stringify(buildQualityConfigRequest(INITIAL_WIZARD_STATE.qualityConfig)),
    );

    expect(Object.keys(body)).toEqual(['dedup_enabled', 'dedup_columns']);
  });

  it('sends true when the operator turned data quality ON', () => {
    const out = buildQualityConfigRequest({
      dataQualityEnabled: true,
      dedupEnabled: false,
      dedupColumns: [],
    });

    expect(out.data_quality_enabled).toBe(true);
  });

  it('sends false when the operator turned data quality OFF', () => {
    // The other half of the three-state contract: an explicit opt-out is a real
    // choice and must reach the record, or the control is inert. What must not
    // happen is `false` arriving for a control nobody touched.
    const out = buildQualityConfigRequest({
      dataQualityEnabled: false,
      dedupEnabled: false,
      dedupColumns: [],
    });

    expect(out.data_quality_enabled).toBe(false);
    expect(Object.keys(out)).toContain('data_quality_enabled');
  });

  it('distinguishes all three states from one another', () => {
    const on = buildQualityConfigRequest({
      dataQualityEnabled: true,
      dedupEnabled: false,
      dedupColumns: [],
    });
    const off = buildQualityConfigRequest({
      dataQualityEnabled: false,
      dedupEnabled: false,
      dedupColumns: [],
    });
    const unset = buildQualityConfigRequest({ dedupEnabled: false, dedupColumns: [] });

    expect(on.data_quality_enabled).toBe(true);
    expect(off.data_quality_enabled).toBe(false);
    expect('data_quality_enabled' in unset).toBe(false);

    // And the three request bodies are pairwise distinct, which is what makes the
    // state recoverable on the far side.
    const bodies = [on, off, unset].map((b) => JSON.stringify(b));
    expect(new Set(bodies).size).toBe(3);
  });
});

describe('INITIAL_WIZARD_STATE.qualityConfig', () => {
  it('leaves dataQualityEnabled absent, not false', () => {
    // Checked on the initializer itself as well as through the serialiser, because
    // this is where the equivalent approval-config bug lived:
    // `alwaysApproveSchemaChanges` was seeded `false` against readers defaulting to
    // `True`, which would have turned the schema-change approval gate off for every
    // untouched wizard. The same mistake here is seeding `false` for a key whose
    // default is per-template.
    expect('dataQualityEnabled' in INITIAL_WIZARD_STATE.qualityConfig).toBe(false);
    expect(INITIAL_WIZARD_STATE.qualityConfig.dataQualityEnabled).toBeUndefined();
  });

  it('defaults dedup to off with an empty column list (R7.1)', () => {
    expect(INITIAL_WIZARD_STATE.qualityConfig.dedupEnabled).toBe(false);
    expect(INITIAL_WIZARD_STATE.qualityConfig.dedupColumns).toEqual([]);
  });
});
