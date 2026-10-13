// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The shared Quality_Config controls, rendered by BOTH editors of a pipeline.
 *
 * Validates: Requirements R6.6, R7.1, R7.2, R7.5.
 *
 * These assertions used to live in `wizard/__tests__/WizardStepModel.quality.test.tsx`
 * against the inline section. That file is kept as-is and still passes — it now
 * covers the wizard's *integration* of this component, which is worth keeping
 * separately: it is what would catch the section being dropped from the Model step.
 * This file covers the component itself, so the modal's copy of the same controls
 * is covered by construction rather than by a second transcription of these cases.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import QualitySettingsFields, { hydrateQualityConfig } from '../QualitySettingsFields';
import { INITIAL_WIZARD_STATE } from '../../pages/wizard/types';
import type { QualityConfig } from '../../pages/wizard/types';

function renderFields(value: QualityConfig | undefined = INITIAL_WIZARD_STATE.qualityConfig) {
  const onChange = vi.fn();
  render(<QualitySettingsFields value={value} onChange={onChange} />);
  return { onChange };
}

/** The `QualityConfig` the component asked its host to store. */
function written(onChange: ReturnType<typeof vi.fn>): QualityConfig {
  return onChange.mock.calls.at(-1)?.[0] as QualityConfig;
}

describe('QualitySettingsFields — the controls', () => {
  it('renders the section header and its controls', () => {
    renderFields({ dedupEnabled: true, dedupColumns: [] });

    expect(screen.getByText('Data Quality')).toBeInTheDocument();
    expect(screen.getByTestId('data-quality-select')).toBeInTheDocument();
    expect(screen.getByTestId('dedup-enabled-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('dedup-columns-input')).toBeInTheDocument();
  });

  it('hides the column field until dedup is switched on', () => {
    // R7.1's "default to off" made visible: the columns only mean anything once
    // dedup is on, and a field that cannot affect anything is an inert control.
    renderFields();

    expect(screen.queryByTestId('dedup-columns-input')).not.toBeInTheDocument();
  });

  it('parses the column field into a list', () => {
    const { onChange } = renderFields({ dedupEnabled: true, dedupColumns: [] });

    fireEvent.change(screen.getByTestId('dedup-columns-input').querySelector('input')!, {
      target: { value: 'patient_id, visit_date' },
    });

    expect(written(onChange).dedupColumns).toEqual(['patient_id', 'visit_date']);
  });

  it('shows the offending column name inline rather than filtering it away', () => {
    // `_validate_quality_config` refuses an offending entry with a 400 and does not
    // drop it, so the operator has to be told which name and why — on the control
    // that owns the field, not as a failed Create or a failed Save.
    renderFields({ dedupEnabled: true, dedupColumns: ['ok', 'not-ok'] });

    expect(screen.getByText(/not-ok/)).toBeInTheDocument();
  });

  it('does not complain about an empty column list', () => {
    // R7.2: empty MEANS every mapped target column, and is the default.
    renderFields({ dedupEnabled: true, dedupColumns: [] });

    expect(screen.queryByText(/not a usable column name/)).not.toBeInTheDocument();
  });

  it('survives an undefined value (R7.5)', () => {
    // `useWizardDraft.readDraft` is a bare `JSON.parse(raw) as T` and
    // `handleRestoreDraft` replaces state rather than merging into it, so a draft
    // saved before `qualityConfig` existed restores it as `undefined` despite the
    // type. Reading `.dedupEnabled` off that would throw on render — a blank
    // section, not a degraded one.
    expect(() => renderFields(undefined)).not.toThrow();
    expect(screen.getByTestId('dedup-enabled-toggle')).toBeInTheDocument();
  });
});

describe('QualitySettingsFields — the data-quality gate is three-state', () => {
  it('shows "Template default" when nobody has expressed an opinion', () => {
    renderFields();

    // Not "Off". The default is per template — on for S3 Table Buckets, off for the
    // other four — so rendering it as "Off" would state the opposite of the truth
    // for an S3 Tables pipeline.
    expect(screen.getByTestId('data-quality-select').textContent).toContain('Template default');
  });

  it('reflects an explicit opt-in as On, not as the default', () => {
    renderFields({ dataQualityEnabled: true, dedupEnabled: false, dedupColumns: [] });

    const shown = screen.getByTestId('data-quality-select').textContent ?? '';
    expect(shown).not.toContain('Template default');
    expect(shown).toContain('On');
  });

  it('reflects an explicit opt-out as Off, not as the default', () => {
    // The state a Toggle could not tell apart from "no opinion", and the one whose
    // conflation with absence changes a pipeline's verdict.
    renderFields({ dataQualityEnabled: false, dedupEnabled: false, dedupColumns: [] });

    const shown = screen.getByTestId('data-quality-select').textContent ?? '';
    expect(shown).not.toContain('Template default');
    expect(shown).toContain('Off');
  });

  it('leaves the gate absent when its neighbour is edited', () => {
    const { onChange } = renderFields();

    fireEvent.click(screen.getByTestId('dedup-enabled-toggle').querySelector('input')!);

    const out = written(onChange);
    expect(out.dedupEnabled).toBe(true);
    // The two settings are independent, and the gate's absent state must survive an
    // edit to its neighbour — otherwise flipping dedup would opt an S3 Tables
    // pipeline out of data quality as a side effect.
    expect('dataQualityEnabled' in out).toBe(false);
  });
});

describe('hydrateQualityConfig', () => {
  it('leaves data_quality_enabled ABSENT when the record does not carry it', () => {
    // The assertion the whole module turns on. `_default_quality_config` seeds
    // `dedup_enabled` and `dedup_columns` and deliberately no gate, so this is the
    // shape of a normal record — and `'dataQualityEnabled' in out === false` is what
    // makes `buildQualityConfigRequest` omit the key on the way back out.
    const out = hydrateQualityConfig({ dedup_enabled: false, dedup_columns: [] });

    expect('dataQualityEnabled' in out).toBe(false);
    expect(out.dedupEnabled).toBe(false);
    expect(out.dedupColumns).toEqual([]);
  });

  it('reads an explicit false as false, not as absence', () => {
    const out = hydrateQualityConfig({ data_quality_enabled: false });

    expect('dataQualityEnabled' in out).toBe(true);
    expect(out.dataQualityEnabled).toBe(false);
  });

  it('reads an explicit true as true', () => {
    expect(hydrateQualityConfig({ data_quality_enabled: true }).dataQualityEnabled).toBe(true);
  });

  it('treats a non-boolean gate as absent, matching resolve_data_quality_enabled', () => {
    // A record carrying one predates `_validate_quality_config`; falling back to the
    // template default is what it would have got at the time.
    for (const bad of ['true', 1, null, {}]) {
      const out = hydrateQualityConfig({ data_quality_enabled: bad });
      expect('dataQualityEnabled' in out).toBe(false);
    }
  });

  it('falls back to the documented defaults for a missing sub-object', () => {
    const out = hydrateQualityConfig(undefined);

    expect('dataQualityEnabled' in out).toBe(false);
    expect(out.dedupEnabled).toBe(INITIAL_WIZARD_STATE.qualityConfig.dedupEnabled);
    expect(out.dedupColumns).toEqual([]);
  });

  it('falls back for a JSON null dedup_enabled rather than reading it as truthy', () => {
    expect(hydrateQualityConfig({ dedup_enabled: null }).dedupEnabled).toBe(false);
  });

  it('drops non-string column entries and copies the list', () => {
    const dedup_columns = ['a', 3, 'b'];
    const out = hydrateQualityConfig({ dedup_columns });

    expect(out.dedupColumns).toEqual(['a', 'b']);
    expect(out.dedupColumns).not.toBe(dedup_columns);
  });

  it('does not alias the initial state\'s column list', () => {
    // A shared array reference here would let one edit leak into every later
    // hydration in the session.
    const out = hydrateQualityConfig({});

    expect(out.dedupColumns).not.toBe(INITIAL_WIZARD_STATE.qualityConfig.dedupColumns);
  });
});
