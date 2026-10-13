// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Unit tests for the `hydrateWizardStateFromPipeline` utility (P19 Task 11.2).
 *
 * Validates: P19 Requirements 6.8, 8.4, 9.2, 9.3, 9.4.
 *
 * Persisted pipelines loaded into the wizard for editing must hydrate the
 * new P19 wizard-state fields correctly:
 *   - `detectedTargetSchema` from the persisted schema_fields (Decision 1)
 *   - `detectedSourceSchema` left null (source crawler hasn't run this session)
 *   - `schemaSourceExplicitlySet = true` so the Step 3 pre-selection effect
 *     does not override the user's previously-persisted choice (Req 9.4)
 *   - `schemaSource` mapped verbatim from the persisted record, with missing
 *     values defaulting to `'detected'` (Req 9.2 backward compatibility)
 *   - `target_config.write_mode == 'merge'` preserved verbatim so the Req 6.8
 *     info alert renders (Decision 4 — no silent backfill)
 */
import { describe, it, expect } from 'vitest';
import { buildQualityConfigRequest, hydrateWizardStateFromPipeline } from '../CreatePipelinePage';
import { INITIAL_WIZARD_STATE } from '../wizard/types';

describe('hydrateWizardStateFromPipeline', () => {
  it('hydrates schema_source verbatim for valid values', () => {
    for (const src of ['detected', 'upload', 'existing', 'mirror_source'] as const) {
      const result = hydrateWizardStateFromPipeline({
        pipeline_name: 'test',
        target_config: { type: 'JDBC' },
        schema_source: src,
      });
      expect(result.schemaSource).toBe(src);
    }
  });

  it('defaults missing schema_source to "detected" (Req 9.2)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'legacy-p17',
      target_config: { type: 'JDBC' },
      // no schema_source field — pre-P19 record
    });
    expect(result.schemaSource).toBe('detected');
  });

  it('rejects unknown schema_source values and falls back to "detected"', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'test',
      target_config: { type: 'JDBC' },
      schema_source: 'something_else',
    });
    expect(result.schemaSource).toBe('detected');
  });

  it('sets schemaSourceExplicitlySet = true so the pre-selection effect does not override (Req 9.4)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'test',
      target_config: { type: 'JDBC' },
      schema_source: 'mirror_source',
    });
    expect(result.schemaSourceExplicitlySet).toBe(true);
  });

  it('hydrates detectedTargetSchema from persisted schema_fields (wizard shape)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'test',
      target_config: { type: 'JDBC' },
      schema_fields: [
        { name: 'id', type: 'int', nullable: false, description: 'PK' },
        { name: 'email', type: 'string', nullable: true, description: '' },
      ],
    });
    expect(result.detectedTargetSchema).toEqual([
      { name: 'id', type: 'int', nullable: false, description: 'PK' },
      { name: 'email', type: 'string', nullable: true, description: '' },
    ]);
    expect(result.schemaFields).toEqual(result.detectedTargetSchema);
  });

  it('hydrates detectedTargetSchema from persisted Schema.fields shape (field_name/field_type)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'test',
      target_config: { type: 'JDBC' },
      schema_fields: [
        { field_name: 'id', field_type: 'bigint', context: 'primary key' },
        { field_name: 'name', field_type: 'string' },
      ],
    });
    expect(result.detectedTargetSchema).toEqual([
      { name: 'id', type: 'bigint', nullable: true, description: 'primary key' },
      { name: 'name', type: 'string', nullable: true, description: '' },
    ]);
  });

  it('leaves detectedTargetSchema null when persisted schema_fields is empty / missing', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'test',
      target_config: { type: 'JDBC' },
    });
    expect(result.detectedTargetSchema).toBeNull();
    expect(result.schemaFields).toEqual([]);
  });

  it('always leaves detectedSourceSchema null on edit-load', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'test',
      target_config: { type: 'JDBC' },
      schema_source: 'mirror_source',
      schema_fields: [{ name: 'id', type: 'int', nullable: false, description: '' }],
    });
    expect(result.detectedSourceSchema).toBeNull();
  });

  it('preserves target_config.write_mode == "merge" verbatim (Req 6.8, Decision 4)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'legacy-merge',
      target_config: {
        type: 'JDBC',
        jdbc_url: 'jdbc:postgresql://h/db',
        write_mode: 'merge',
      },
    });
    expect(result.targetType).toBe('jdbc');
    expect(result.targetConfig.write_mode).toBe('merge');
  });

  it('migrates legacy RDS_DATABASE -> jdbc target type (delegates to migrateLegacyTargetConfig)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'legacy-p14',
      target_config: { type: 'RDS_DATABASE', jdbc_url: 'jdbc:postgresql://h/db' },
    });
    expect(result.targetType).toBe('jdbc');
    expect(result.targetConfig.type).toBe('JDBC');
  });

  it('handles JDBC source pipelines (P17)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'jdbc-source',
      target_config: { type: 'GLUE_CATALOG', database: 'd', table: 't' },
      source_type: 'JDBC',
      source_config: { jdbc_url: 'jdbc:postgresql://h/d', jdbc_table: 'd.t' },
      schedule: 'rate(1 hour)',
    });
    expect(result.sourceType).toBe('jdbc');
    expect(result.sourceConfig.jdbc_url).toBe('jdbc:postgresql://h/d');
    expect(result.schedule).toBe('rate(1 hour)');
  });

  it('defaults sourceType to s3_upload when source_type is missing (pre-P17)', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'pre-p17',
      target_config: { type: 'GLUE_CATALOG' },
    });
    expect(result.sourceType).toBe('s3_upload');
    expect(result.sourceConfig).toEqual({});
  });

  it('hydrates pipeline_name, description, ai_model, target_schema, approval_config', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'My Pipeline',
      pipeline_description: 'A test pipeline',
      ai_model: 'anthropic.claude-3-haiku-20240307-v1:0',
      target_schema: 'schema-123',
      target_config: { type: 'JDBC' },
      approval_config: {
        schema_mapping_enabled: false,
        // p64 — **amended from `95`**, deliberately, not to make a test pass. The
        // persisted attribute's unit is a fraction: its only writer,
        // `pipelines_crud._default_approval_config`, seeds
        // `Decimal(str(CONFIDENCE_AUTO_APPROVE_THRESHOLD))` = 0.85. `95` was never a
        // legal value on a record, and reading it across into a 0–100 slider
        // happened to look right only because nothing produced the real thing.
        schema_mapping_confidence_threshold: 0.95,
        code_generation_enabled: false,
        always_approve_schema_changes: true,
      },
    });
    expect(result.pipelineName).toBe('My Pipeline');
    expect(result.description).toBe('A test pipeline');
    expect(result.aiModel).toBe('anthropic.claude-3-haiku-20240307-v1:0');
    expect(result.existingSchemaId).toBe('schema-123');
    expect(result.approvalConfig.schemaMappingEnabled).toBe(false);
    // Fraction on the record → percent in wizard state. The expected number is
    // unchanged; what changed is that it is now a conversion rather than a copy.
    expect(result.approvalConfig.schemaMappingConfidenceThreshold).toBe(95);
    expect(result.approvalConfig.codeGenerationEnabled).toBe(false);
    expect(result.approvalConfig.alwaysApproveSchemaChanges).toBe(true);
  });

  it('converts a seeded 0.85 threshold to 85%, not 0.85% (p64)', () => {
    // The case the unit mismatch actually broke, and the reason the conversion is
    // not cosmetic. Every pipeline created without an `approval_config` carries
    // the seeded `Decimal("0.85")`; before p64 that hydrated the slider to `0.85`,
    // so an operator opening the wizard on such a record saw a 0.85% confidence
    // gate and would resubmit it.
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'seeded-defaults',
      target_config: { type: 'JDBC' },
      approval_config: { schema_mapping_confidence_threshold: 0.85 },
    });
    expect(result.approvalConfig.schemaMappingConfidenceThreshold).toBe(85);
  });

  it('fails closed on the three boolean gates when they are absent or non-boolean', () => {
    // The shared reader (`hydrateApprovalConfig`) pinned from the wizard's side as
    // well as the modal's — both editors call it, so both should catch a regression
    // in it.
    //
    // `always_approve_schema_changes: true` means approval IS REQUIRED, and
    // `schema_mapping/lambda_function.py:1657` spells its own fallback `True`, so a
    // record storing no such key is a record whose gate is ON. All three readers
    // additionally test by TRUTHINESS rather than identity (`:1669` is
    // `not schema_mapping_enabled`), so a non-boolean must present as the gate being
    // on too — otherwise the control displays the opposite of what is enforced.
    const absent = hydrateWizardStateFromPipeline({ target_config: { type: 'JDBC' } });
    expect(absent.approvalConfig.alwaysApproveSchemaChanges).toBe(true);
    expect(absent.approvalConfig.schemaMappingEnabled).toBe(true);
    expect(absent.approvalConfig.codeGenerationEnabled).toBe(true);

    const nonBoolean = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      approval_config: {
        always_approve_schema_changes: 'false' as unknown as boolean,
        schema_mapping_enabled: 0 as unknown as boolean,
        code_generation_enabled: null as unknown as boolean,
      },
    });
    expect(nonBoolean.approvalConfig.alwaysApproveSchemaChanges).toBe(true);
    expect(nonBoolean.approvalConfig.schemaMappingEnabled).toBe(true);
    expect(nonBoolean.approvalConfig.codeGenerationEnabled).toBe(true);
  });

  it('falls back to the wizard default when the threshold is absent or not a number (p64)', () => {
    const absent = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      approval_config: { schema_mapping_enabled: false },
    });
    expect(absent.approvalConfig.schemaMappingConfidenceThreshold).toBe(80);

    // A JSON `null` on the record must fall back too. `Math.round(null * 100)` is
    // `0`, which would present a never-approve gate as approve-everything — the
    // guard broken in the worst direction.
    const nulled = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      approval_config: {
        schema_mapping_confidence_threshold: null as unknown as number,
      },
    });
    expect(nulled.approvalConfig.schemaMappingConfidenceThreshold).toBe(80);
  });

  it('falls back to model_id when ai_model is missing', () => {
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      model_id: 'amazon.nova-pro-v1:0',
    });
    expect(result.aiModel).toBe('amazon.nova-pro-v1:0');
  });

  it('falls back to description when pipeline_description is missing', () => {
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      description: 'fallback description',
    });
    expect(result.description).toBe('fallback description');
  });

  it('handles entirely empty persisted record (defensive)', () => {
    const result = hydrateWizardStateFromPipeline({});
    expect(result.targetType).toBe('s3_tables');
    expect(result.schemaSource).toBe('detected');
    expect(result.schemaSourceExplicitlySet).toBe(true);
    expect(result.detectedSourceSchema).toBeNull();
    expect(result.detectedTargetSchema).toBeNull();
    expect(result.schemaFields).toEqual([]);
  });
});

/**
 * p64 R7.4 / R6.6 — the Quality_Config's edit-flow round trip.
 *
 * R7.4's requirement is that `hydrateWizardStateFromPipeline` read the setting "or
 * an edit session silently resets it". The `data_quality_enabled` group below is
 * the sharp end: the reset that matters is not losing the value, it is *inventing*
 * one. An S3 Tables pipeline has data quality on by template default and carries no
 * stored key, so hydrating that as `false` and re-submitting writes an explicit
 * opt-OUT — removing the row-level split and quarantine write P28 gave that target
 * type, and moving `shared/quality_verdict.py`'s answer from `enforced` to
 * `advisory` — with no error anywhere. R6.6 forbids exactly this.
 */
describe('hydrateWizardStateFromPipeline and the Quality_Config', () => {
  it('takes the wizard defaults for a record with no quality_config at all', () => {
    // A pre-2c.1 record. `resolve_quality_config` reads such a record as dedup off
    // with an empty column list, so hydrating to the same pair means an edit
    // session re-submits what the pipeline already had.
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'pre-2c1',
      target_config: { type: 'NEW_S3_TABLE_BUCKET' },
    });

    expect(result.qualityConfig.dedupEnabled).toBe(false);
    expect(result.qualityConfig.dedupColumns).toEqual([]);
    expect('dataQualityEnabled' in result.qualityConfig).toBe(false);
  });

  it('hydrates a stored dedup opt-in and its columns verbatim', () => {
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 'dedup-on',
      target_config: { type: 'NEW_S3_TABLE_BUCKET' },
      quality_config: {
        dedup_enabled: true,
        dedup_columns: ['patient_id', 'visit_date'],
      },
    });

    expect(result.qualityConfig.dedupEnabled).toBe(true);
    expect(result.qualityConfig.dedupColumns).toEqual(['patient_id', 'visit_date']);
  });

  it('round-trips through buildQualityConfigRequest without changing the record', () => {
    // The property R7.4 actually wants: hydrate then re-submit is a no-op. Asserted
    // over all three `data_quality_enabled` states, because the middle one
    // (`false`) and the absent one are the pair a lazy reader collapses.
    for (const stored of [
      { dedup_enabled: true, dedup_columns: ['id'] },
      { data_quality_enabled: true, dedup_enabled: false, dedup_columns: [] },
      { data_quality_enabled: false, dedup_enabled: true, dedup_columns: ['a', 'b'] },
    ]) {
      const hydrated = hydrateWizardStateFromPipeline({
        target_config: { type: 'NEW_S3_TABLE_BUCKET' },
        quality_config: stored,
      });
      const resubmitted = buildQualityConfigRequest(hydrated.qualityConfig);

      expect(resubmitted).toEqual(stored);
    }
  });

  it('does NOT read an absent data_quality_enabled as false (R6.6)', () => {
    // The trap. `_default_quality_config` writes `dedup_enabled` and
    // `dedup_columns` and deliberately omits `data_quality_enabled`, so this is the
    // shape of every pipeline created since 2c.1 — the common case, not an edge.
    const result = hydrateWizardStateFromPipeline({
      pipeline_name: 's3-tables-seeded',
      target_config: { type: 'NEW_S3_TABLE_BUCKET' },
      quality_config: { dedup_enabled: false, dedup_columns: [] },
    });

    expect(result.qualityConfig.dataQualityEnabled).toBeUndefined();
    expect(result.qualityConfig.dataQualityEnabled).not.toBe(false);
    expect('dataQualityEnabled' in result.qualityConfig).toBe(false);
  });

  it('does not resurrect the key when re-submitting a pipeline that never had it', () => {
    // The consequence spelled out end to end, because "hydrates as undefined" is
    // only useful if the omission survives to the wire. If it did not, the first
    // edit of an S3 Tables pipeline would flip its DQ posture.
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'NEW_S3_TABLE_BUCKET' },
      quality_config: { dedup_enabled: false, dedup_columns: [] },
    });
    const body = JSON.parse(JSON.stringify(buildQualityConfigRequest(result.qualityConfig)));

    expect(Object.keys(body)).not.toContain('data_quality_enabled');
  });

  it('preserves an explicit opt-out so the other direction is not lost either', () => {
    // The mirror image, and the reason the wizard carries the key at all rather
    // than simply never sending it. `_submitted_quality_config` persists what
    // arrives **verbatim** with no merge, so omitting a key that WAS stored erases
    // it — a pipeline explicitly opted out of data quality would silently return to
    // its template default on the next save.
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'NEW_S3_TABLE_BUCKET' },
      quality_config: { data_quality_enabled: false, dedup_enabled: false, dedup_columns: [] },
    });

    expect(result.qualityConfig.dataQualityEnabled).toBe(false);
    expect(buildQualityConfigRequest(result.qualityConfig).data_quality_enabled).toBe(false);
  });

  it('preserves an explicit opt-in', () => {
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'GLUE_CATALOG' },
      quality_config: { data_quality_enabled: true, dedup_enabled: false, dedup_columns: [] },
    });

    expect(result.qualityConfig.dataQualityEnabled).toBe(true);
  });

  it('falls back to the template default for a non-boolean gate', () => {
    // A record predating `_validate_quality_config`'s 400. `resolve_data_quality_
    // enabled` checks `isinstance(explicit, bool)` and otherwise takes the
    // per-template default, so that is what the operator would see today — and
    // `"false"` in particular must not read as `false`, or the wizard would show an
    // opt-out that the running job never honoured.
    for (const raw of ['false', 'true', 0, 1, null] as unknown[]) {
      const result = hydrateWizardStateFromPipeline({
        target_config: { type: 'NEW_S3_TABLE_BUCKET' },
        quality_config: { data_quality_enabled: raw, dedup_enabled: false, dedup_columns: [] },
      });
      expect(result.qualityConfig.dataQualityEnabled).toBeUndefined();
    }
  });

  it('falls back to dedup off for a non-boolean dedup_enabled', () => {
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      quality_config: { dedup_enabled: 'yes', dedup_columns: [] },
    });

    expect(result.qualityConfig.dedupEnabled).toBe(false);
  });

  it('tolerates a dedup_columns that is not a list', () => {
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      quality_config: { dedup_enabled: true, dedup_columns: 'patient_id' },
    });

    expect(result.qualityConfig.dedupColumns).toEqual([]);
  });

  it('does not alias INITIAL_WIZARD_STATE.qualityConfig.dedupColumns', () => {
    // The hydrated state is handed straight to `setState` and then edited in place
    // by the Model step's controls, so sharing the initializer's array would let one
    // edit session's columns leak into the next.
    const result = hydrateWizardStateFromPipeline({ target_config: { type: 'JDBC' } });

    expect(result.qualityConfig.dedupColumns).not.toBe(
      INITIAL_WIZARD_STATE.qualityConfig.dedupColumns,
    );
  });

  it('drops a non-string column entry rather than typing around it', () => {
    const result = hydrateWizardStateFromPipeline({
      target_config: { type: 'JDBC' },
      quality_config: { dedup_enabled: true, dedup_columns: ['ok', 7, null] },
    });

    expect(result.qualityConfig.dedupColumns).toEqual(['ok']);
  });
});
