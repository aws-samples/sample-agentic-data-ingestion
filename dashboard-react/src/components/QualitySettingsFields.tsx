// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import FormField from '@cloudscape-design/components/form-field';
import Select from '@cloudscape-design/components/select';
import Input from '@cloudscape-design/components/input';
import Toggle from '@cloudscape-design/components/toggle';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
// p64 R7 / R6.6 — the dedup-column rule, its text form and the three-state
// reader, from one pure module no `vi.mock` factory replaces (the same reason
// `worksheet-selection.ts` holds the Selected_Worksheet's gate). Imported, never
// transcribed: `LGR-eee8ac8-5`.
import {
  DATA_QUALITY_ENABLED_REQUEST_KEY,
  DEDUP_COLUMNS_REQUEST_KEY,
  DEDUP_ENABLED_REQUEST_KEY,
  dedupColumnsError,
  formatDedupColumns,
  parseDedupColumns,
  readDataQualityEnabled,
} from '../utils/quality-config';
import { INITIAL_WIZARD_STATE } from '../pages/wizard/types';
import type { QualityConfig } from '../pages/wizard/types';

/**
 * p64 R6.6 — the data-quality gate is a three-option Select, **not a Toggle**.
 *
 * A Toggle has two positions and the setting has three states: on, off, and "use
 * this template's own default". The third is not a placeholder for "off" — it is
 * on for an S3 Tables target and off for the other four, which is the posture
 * every template had before p64 (`shared/quality_verdict.py`'s
 * `DQ_DEFAULT_ON_TEMPLATES`). Rendering it as a Toggle would force the editor to
 * pick one of the two positions for a pipeline nobody has expressed an opinion
 * about, and picking `off` writes an explicit opt-OUT that strips a DQ block an
 * existing S3 Tables pipeline has had since P28.
 *
 * So `'default'` is a first-class option with its own label. `LGR-4854b3d-1` is
 * the same rule one value class over: a three-state value gets a control that can
 * express three states, rather than a two-state control and a lossy mapping.
 */
const DQ_MODE_OPTIONS = [
  {
    value: 'default',
    label: 'Template default',
    description: 'On for S3 Table Buckets, off for other targets — what this target type does today',
  },
  { value: 'on', label: 'On', description: 'Run the data-quality checks and quarantine failing rows' },
  { value: 'off', label: 'Off', description: 'Skip the data-quality checks for this pipeline' },
] as const;

/** Which of the three options a `boolean | undefined` corresponds to. */
function dqModeValue(enabled: boolean | undefined): 'default' | 'on' | 'off' {
  if (enabled === true) return 'on';
  if (enabled === false) return 'off';
  return 'default';
}

/**
 * Apply a picked option, spelling "default" as an **absent key** rather than an
 * explicit `undefined`.
 *
 * `_submitted_quality_config` and `_default_quality_config` both distinguish
 * absence with a membership test (`key not in data`), so the editor's own state
 * uses the same spelling — `'dataQualityEnabled' in config` is then a reliable
 * answer to "has anyone expressed an opinion", and `buildQualityConfigRequest`
 * has one rule to follow rather than two.
 */
export function withDqMode(config: QualityConfig, mode: string): QualityConfig {
  const { dataQualityEnabled: _dropped, ...rest } = config;
  if (mode === 'on') return { ...rest, dataQualityEnabled: true };
  if (mode === 'off') return { ...rest, dataQualityEnabled: false };
  return rest;
}

/**
 * A persisted `quality_config` as either editor's caller has it.
 *
 * Members are `unknown` rather than the concrete types `Pipeline.quality_config`
 * declares, because a record predating `_validate_quality_config` could hold a
 * non-boolean under any of them and this is the reader that has to survive that
 * (`LGR-7bea749-5` documents the same residual on the concrete type). The
 * concrete shape is assignable to this one, so both call sites pass their own
 * type unchanged.
 */
export interface PersistedQualityConfig {
  data_quality_enabled?: unknown;
  dedup_enabled?: unknown;
  dedup_columns?: unknown;
}

/**
 * Record → editor state. The read-side counterpart of
 * `buildQualityConfigRequest`, and like it a **single** authority: both editors
 * call this rather than each spelling the three reads themselves.
 *
 * Three separate reads because the three keys have three different absence
 * semantics, and collapsing them would break the one that matters.
 */
export function hydrateQualityConfig(
  persisted: PersistedQualityConfig | undefined,
): QualityConfig {
  const raw = persisted ?? {};
  // `dataQualityEnabled` stays `boolean | undefined`. `readDataQualityEnabled`
  // returns `undefined` for an absent key AND for a non-boolean, and this code
  // must NOT turn either into `false`: an existing S3 Tables pipeline has DQ on by
  // template default with nothing stored, so hydrating `false` and re-submitting
  // would write an explicit opt-OUT and move that pipeline's verdict from
  // `enforced` to `advisory` on its first edit.
  const dataQualityEnabled = readDataQualityEnabled(raw[DATA_QUALITY_ENABLED_REQUEST_KEY]);
  const rawDedupColumns = raw[DEDUP_COLUMNS_REQUEST_KEY];
  return {
    // Spread-with-membership rather than `dataQualityEnabled: undefined`, so
    // `'dataQualityEnabled' in config` distinguishes "no opinion" from "set to
    // something" — the same membership test the server's own writer uses.
    ...(dataQualityEnabled === undefined ? {} : { dataQualityEnabled }),
    // `dedup_enabled` DOES fall back to a flat default, and that is correct rather
    // than inconsistent: dedup is off on every target type, so `false` is what a
    // record with no `quality_config` selects at
    // `code_assembly.resolve_quality_config` anyway. `typeof` rather than `??` so a
    // JSON `null` falls back too.
    dedupEnabled:
      typeof raw[DEDUP_ENABLED_REQUEST_KEY] === 'boolean'
        ? (raw[DEDUP_ENABLED_REQUEST_KEY] as boolean)
        : INITIAL_WIZARD_STATE.qualityConfig.dedupEnabled,
    // Non-string entries are dropped here — not as sanitisation of a value bound
    // for the wire, but because editor state is typed `string[]` and a number in
    // this position could only have come from a record written before
    // `_validate_quality_config` existed. A legitimate entry is a string by
    // construction: every one that reaches a record has passed the
    // Identifier_Allowlist at the boundary.
    dedupColumns: Array.isArray(rawDedupColumns)
      ? rawDedupColumns.filter((c): c is string => typeof c === 'string')
      : [...INITIAL_WIZARD_STATE.qualityConfig.dedupColumns],
  };
}

export interface QualitySettingsFieldsProps {
  /**
   * The current value. Accepts `undefined` because R7.5 requires every consumer
   * to read defensively: `useWizardDraft.readDraft` is a bare
   * `JSON.parse(raw) as T` and `handleRestoreDraft` replaces state rather than
   * merging into it, so a draft saved before `qualityConfig` existed restores it
   * as `undefined` despite the type.
   */
  value: QualityConfig | undefined;
  onChange: (next: QualityConfig) => void;
}

/**
 * p64 R7 / R6.6 — the per-pipeline data-quality settings, as **one** set of
 * controls rendered by both editors of the pipeline object.
 *
 * ## Why this is a shared component and not a section in each editor
 *
 * There are two editors of one pipeline: the 6-step wizard's Model step and
 * `PipelineFormModal`'s `isEdit` branch. `approval_config` is the worked example
 * of what happens when each grows its own section — the wizard has four approval
 * controls, the modal has two, and an operator editing through the modal cannot
 * see or change `always_approve_schema_changes` or
 * `schema_mapping_confidence_threshold`, with nothing indicating they exist. That
 * divergence is not a rendering detail: the settings it hides are governance
 * gates.
 *
 * `LGR-7bea749-6` deliberately left "which editor owns quality settings" open,
 * having rejected adding a control to one editor (which makes the other's
 * hydration dead code) and to both independently (which invites two divergent
 * spellings). This component is the third answer that record did not consider:
 * both editors, one set of fields, no subset to diverge.
 *
 * ## What it renders, and why each control is shaped the way it is
 *
 * The DQ gate is a three-option `Select` (see `DQ_MODE_OPTIONS`), the dedup
 * opt-in is a `Toggle`, and the column list appears only once dedup is on —
 * a field that cannot affect anything is an inert control. The column list
 * reports `dedupColumnsError` inline on the field that owns it, rather than
 * letting `_validate_quality_config`'s 400 surface three layers away.
 *
 * The component is purely presentational: it holds no state, performs no
 * submission, and does not know which editor is rendering it. The *gate* on an
 * invalid column list belongs to each host — the wizard blocks step navigation,
 * the modal blocks save — because only the host knows what it is gating.
 */
export default function QualitySettingsFields({ value, onChange }: QualitySettingsFieldsProps) {
  // R7.5 — falling back to the initial value means the controls render at the
  // documented defaults instead of throwing on `.dedupEnabled` of `undefined`.
  const quality = value ?? INITIAL_WIZARD_STATE.qualityConfig;
  const dedupError = dedupColumnsError(quality.dedupColumns);

  return (
    <SpaceBetween size="m">
      <Header variant="h3">Data Quality</Header>

      <FormField
        label="Data quality checks"
        description="Leave on the template default unless this pipeline needs a different posture."
      >
        <Select
          selectedOption={
            DQ_MODE_OPTIONS.find((o) => o.value === dqModeValue(quality.dataQualityEnabled)) ??
            DQ_MODE_OPTIONS[0]
          }
          onChange={({ detail }) =>
            onChange(withDqMode(quality, detail.selectedOption.value ?? 'default'))
          }
          options={[...DQ_MODE_OPTIONS]}
          data-testid="data-quality-select"
        />
      </FormField>

      <Toggle
        checked={quality.dedupEnabled}
        onChange={({ detail }) => onChange({ ...quality, dedupEnabled: detail.checked })}
        data-testid="dedup-enabled-toggle"
      >
        Drop duplicate rows
      </Toggle>

      {quality.dedupEnabled && (
        <FormField
          label="Duplicate-identifying columns"
          description="Comma-separated. Leave empty to treat every mapped target column as part of the key."
          errorText={dedupError}
        >
          <Input
            value={formatDedupColumns(quality.dedupColumns)}
            onChange={({ detail }) =>
              onChange({ ...quality, dedupColumns: parseDedupColumns(detail.value) })
            }
            placeholder="e.g. patient_id, visit_date"
            data-testid="dedup-columns-input"
          />
        </FormField>
      )}
    </SpaceBetween>
  );
}
