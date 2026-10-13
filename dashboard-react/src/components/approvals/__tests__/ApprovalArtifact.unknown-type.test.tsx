// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Component tests for `ApprovalArtifact`'s unrecognised-type branch
 * (p30 task 2.8, R14.5).
 *
 * **Validates: Requirements 14.6**
 *
 * This branch used to return `null`, which rendered a blank space directly above
 * live approve/reject buttons — reading as "nothing to approve" when the truth
 * is "this UI cannot show you what you are approving". R14.5 replaced it with a
 * warning `Alert`.
 *
 * `ApprovalArtifact.test.tsx` keeps the pre-existing no-crash / no-wrong-artifact
 * guarantee for this branch and deliberately stops at `container.textContent`
 * not being empty. The R14.5 assertions — the alert itself, the raw
 * `approval_type` in the output, the absent-type wording, and the message
 * content — live here.
 *
 * **The fixtures are live backend types, not invented strings.** The switch
 * handles `code_generation`, `schema_mapping` and `test_failure`; the backend
 * also emits `infrastructure`
 * (`create_infrastructure/lambda_function.py:447`) and `first_run`
 * (`create_quality_checks/lambda_function.py:195`). So this branch is reached in
 * production today and these tests are a regression guard on real behaviour
 * rather than on a hypothetical. `infrastructure` is the fully-blank case;
 * `ApprovalsPage`'s own `ApprovalTypeDetails` does handle `first_run`
 * (`ApprovalsPage.tsx:81`), so for that type the modal is not entirely empty —
 * but this component still cannot render its artifact.
 */
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import ApprovalArtifact from '../ApprovalArtifact';
import type { Approval } from '../../../types';

// Same minimal factory the sibling file uses — the reused CodeViewer /
// MappingViewer must not reach the network. Neither hook is called on the
// unknown branch; the mock keeps the module graph inert either way.
vi.mock('../../../hooks', () => ({
  useCodeVersion: () => ({ data: undefined, isLoading: false, isError: false }),
  useBedrockModels: () => ({ data: [], isLoading: false, isError: false }),
}));

function makeApproval(approvalType: string | undefined): Approval {
  return {
    approval_id: 'appr-1',
    pipeline_key: 'pipe-1',
    approval_type: approvalType as unknown as string,
    status: 'pending',
    created_at: '2026-06-01T10:00:00Z',
    details: {},
  };
}

describe('ApprovalArtifact — unrecognised approval type says so (R14.5)', () => {
  it('renders the warning alert inside the unknown-artifact wrapper', () => {
    render(<ApprovalArtifact approval={makeApproval('infrastructure')} />);

    const wrapper = screen.getByTestId('approval-artifact-unknown');
    const alert = screen.getByTestId('approval-artifact-unknown-type');
    expect(wrapper).toContainElement(alert);
    expect(alert.textContent).toContain('Cannot display what is being approved');
  });

  it('names the unrecognised approval_type verbatim so it can be quoted in a report', () => {
    render(<ApprovalArtifact approval={makeApproval('infrastructure')} />);

    expect(screen.getByTestId('approval-artifact-unknown-type').textContent).toContain(
      'approval type "infrastructure"',
    );
  });

  it('does the same for first_run, the other live unhandled backend type', () => {
    render(<ApprovalArtifact approval={makeApproval('first_run')} />);

    expect(screen.getByTestId('approval-artifact-unknown-type').textContent).toContain(
      'approval type "first_run"',
    );
  });

  it('reports the type as the backend sent it, not lower-cased for matching', () => {
    render(<ApprovalArtifact approval={makeApproval('Infrastructure_V2')} />);

    const text = screen.getByTestId('approval-artifact-unknown-type').textContent ?? '';
    expect(text).toContain('approval type "Infrastructure_V2"');
    expect(text).not.toContain('infrastructure_v2');
  });

  it('states that the buttons below are live and advises rejecting', () => {
    render(<ApprovalArtifact approval={makeApproval('infrastructure')} />);

    const text = screen.getByTestId('approval-artifact-unknown-type').textContent ?? '';
    // An empty preview must not be read as "nothing to approve".
    expect(text).toContain('empty preview does not mean there is nothing to approve');
    expect(text).toContain('still live');
    expect(text).toContain('Reject unless you can confirm');
    expect(text).toContain('report the approval type named above');
  });
});

describe('ApprovalArtifact — absent approval type (R14.5)', () => {
  it('names the approval itself rather than an empty quoted type', () => {
    render(<ApprovalArtifact approval={makeApproval(undefined)} />);

    const text = screen.getByTestId('approval-artifact-unknown-type').textContent ?? '';
    expect(text).toContain('this approval, which records no approval type');
    expect(text).not.toContain('approval type ""');
  });

  it('asks the user to report the approval, not a type it does not have', () => {
    render(<ApprovalArtifact approval={makeApproval(undefined)} />);

    const text = screen.getByTestId('approval-artifact-unknown-type').textContent ?? '';
    expect(text).toContain('report this approval');
    expect(text).not.toContain('report the approval type named above');
  });

  it('treats an empty-string type the same as an absent one', () => {
    render(<ApprovalArtifact approval={makeApproval('')} />);

    expect(screen.getByTestId('approval-artifact-unknown-type').textContent).toContain(
      'this approval, which records no approval type',
    );
  });
});

describe('ApprovalArtifact — unknown type is reported once per distinct type (R14.5)', () => {
  it('warns on first render and not on re-render of the same type', () => {
    // `reportedUnknownTypes` is module-scoped, so this uses a type string no
    // other test in this file renders — the dedupe is then observable without
    // depending on test order.
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    try {
      const approval = makeApproval('dedupe_probe_type');
      const { unmount } = render(<ApprovalArtifact approval={approval} />);
      expect(warn).toHaveBeenCalledTimes(1);
      expect(warn.mock.calls[0][1]).toEqual({ approval_type: 'dedupe_probe_type' });

      unmount();
      render(<ApprovalArtifact approval={approval} />);
      expect(warn).toHaveBeenCalledTimes(1);
    } finally {
      warn.mockRestore();
    }
  });
});
