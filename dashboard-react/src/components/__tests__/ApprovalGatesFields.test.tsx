// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * `ApprovalGatesFields` — the approval-gate controls both editors of the pipeline
 * object render, and the exclusion of the one control that has no reader.
 *
 * The component is purely presentational, so these tests are about two things: that
 * all three gates are here (the divergence being closed), and that the fourth is
 * NOT (the divergence being closed *deliberately* rather than by half). The second
 * half is the one worth a test: a future reader looking at "the wizard has four,
 * this has three" will reasonably want to complete it, and the assertion below is
 * what tells them the gap is a decision.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import ApprovalGatesFields from '../ApprovalGatesFields';
import { INITIAL_WIZARD_STATE } from '../../pages/wizard/types';
import type { ApprovalConfig } from '../../pages/wizard/types';

/** Every gate off, so a control that ignores `value` fails rather than passes. */
const ALL_OFF: ApprovalConfig = {
  schemaMappingEnabled: false,
  schemaMappingConfidenceThreshold: 40,
  codeGenerationEnabled: false,
  alwaysApproveSchemaChanges: false,
};

function checkbox(testId: string): HTMLInputElement {
  return screen.getByTestId(testId).querySelector('input')!;
}

describe('ApprovalGatesFields — what it renders', () => {
  it('renders all three boolean gates under one header', () => {
    render(<ApprovalGatesFields value={INITIAL_WIZARD_STATE.approvalConfig} onChange={() => {}} />);

    expect(screen.getByText('Approval Gates')).toBeInTheDocument();
    expect(screen.getByTestId('schema-mapping-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('code-generation-toggle')).toBeInTheDocument();
    expect(screen.getByTestId('auto-approve-toggle')).toBeInTheDocument();
  });

  it('keeps the labels that match what the values mean', () => {
    // `alwaysApproveSchemaChanges: true` means approval IS REQUIRED. The old label
    // read "Auto-approve schema changes", the opposite, so the fail-closed default
    // displayed as "Auto-approve: Yes". Pinned here so the extraction cannot quietly
    // reintroduce it.
    render(<ApprovalGatesFields value={INITIAL_WIZARD_STATE.approvalConfig} onChange={() => {}} />);

    expect(screen.getByText('Require approval for schema mapping')).toBeInTheDocument();
    expect(screen.getByText('Require approval for code generation')).toBeInTheDocument();
    expect(screen.getByText('Require approval for schema changes')).toBeInTheDocument();
    expect(screen.queryByText(/Auto-approve/)).not.toBeInTheDocument();
  });

  it('keeps `auto-approve-toggle` as the third gate\u2019s test id through the extraction', () => {
    // p63 R2.9 territory. The field name, the wire key and this test id are all
    // deliberately unchanged; only the label was ever corrected, and only the file it
    // lives in moved. The wizard's pre-existing tests address it by this id.
    render(<ApprovalGatesFields value={INITIAL_WIZARD_STATE.approvalConfig} onChange={() => {}} />);

    expect(screen.getByTestId('auto-approve-toggle')).toBeInTheDocument();
  });

  it('renders NO control for the confidence threshold, on purpose', () => {
    // `schema_mapping_confidence_threshold` has no reader anywhere in `lambda/`,
    // `shared/` or `mcp/` — the auto-approval comparison reads
    // `CONFIDENCE_AUTO_APPROVE_THRESHOLD` from `shared/config.py` directly, and
    // `create_infrastructure/lambda_function.py:620` names the key to say "Not this
    // one." A control here would be a second inert control, and on the modal's edit
    // path it would write a threshold nobody chose onto a record that had none.
    //
    // A slider would be the natural way to add one, so the absence of a slider is
    // what is asserted. The wizard keeps its own, outside this component.
    render(<ApprovalGatesFields value={ALL_OFF} onChange={() => {}} />);

    expect(screen.queryByTestId('confidence-slider')).not.toBeInTheDocument();
    expect(screen.queryByRole('slider')).not.toBeInTheDocument();
    expect(screen.queryByText(/Confidence Threshold/)).not.toBeInTheDocument();
    // And the value it would display is nowhere on screen either.
    expect(screen.queryByText(/40/)).not.toBeInTheDocument();
  });
});

describe('ApprovalGatesFields — it reflects the value it is given', () => {
  it('shows each gate in its stored position', () => {
    render(<ApprovalGatesFields value={ALL_OFF} onChange={() => {}} />);

    expect(checkbox('schema-mapping-toggle').checked).toBe(false);
    expect(checkbox('code-generation-toggle').checked).toBe(false);
    expect(checkbox('auto-approve-toggle').checked).toBe(false);
  });

  it('shows the gates on when they are on', () => {
    render(<ApprovalGatesFields value={INITIAL_WIZARD_STATE.approvalConfig} onChange={() => {}} />);

    expect(checkbox('schema-mapping-toggle').checked).toBe(true);
    expect(checkbox('code-generation-toggle').checked).toBe(true);
    // `INITIAL_WIZARD_STATE` seeds this `true` because its reader defaults to `True`.
    // A `false` here would ship an explicit gate-off for every wizard-created
    // pipeline — `LGR-b993b94-1` is the record of that being caught.
    expect(checkbox('auto-approve-toggle').checked).toBe(true);
  });

  it('falls back to the documented defaults for an undefined value', () => {
    // A restored draft can make a state field `undefined` despite its type:
    // `readDraft` is a bare `JSON.parse(raw) as T` and `handleRestoreDraft` replaces
    // state rather than merging into it. Rendering must not throw, and must not show
    // a gate as off.
    render(<ApprovalGatesFields value={undefined} onChange={() => {}} />);

    expect(checkbox('auto-approve-toggle').checked).toBe(true);
    expect(checkbox('schema-mapping-toggle').checked).toBe(true);
    expect(checkbox('code-generation-toggle').checked).toBe(true);
  });
});

describe('ApprovalGatesFields — it reports changes as a whole config', () => {
  it('emits the full object with one field changed, preserving the threshold', () => {
    // The component renders no threshold control but must carry the value through:
    // the wizard's Slider sits outside this component and edits the same object, so
    // dropping the field here would reset the wizard's slider on every toggle click.
    const onChange = vi.fn();
    render(<ApprovalGatesFields value={ALL_OFF} onChange={onChange} />);

    fireEvent.click(checkbox('auto-approve-toggle'));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0]).toEqual({
      ...ALL_OFF,
      alwaysApproveSchemaChanges: true,
    });
  });

  it('changes only the gate that was clicked', () => {
    const onChange = vi.fn();
    render(<ApprovalGatesFields value={INITIAL_WIZARD_STATE.approvalConfig} onChange={onChange} />);

    fireEvent.click(checkbox('schema-mapping-toggle'));

    expect(onChange.mock.calls[0][0]).toEqual({
      ...INITIAL_WIZARD_STATE.approvalConfig,
      schemaMappingEnabled: false,
    });
  });

  it('emits a new object rather than mutating the one it was given', () => {
    const value: ApprovalConfig = { ...ALL_OFF };
    const onChange = vi.fn();
    render(<ApprovalGatesFields value={value} onChange={onChange} />);

    fireEvent.click(checkbox('code-generation-toggle'));

    expect(onChange.mock.calls[0][0]).not.toBe(value);
    expect(value.codeGenerationEnabled).toBe(false);
  });
});
