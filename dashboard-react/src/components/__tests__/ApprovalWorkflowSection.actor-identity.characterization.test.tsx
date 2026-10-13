// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Characterization tests for `PendingApprovalCard` and the two identity
 * projections in `ApprovalWorkflowSection` — p50 task 1.1, landing BEFORE C4.
 *
 * Why this file exists (design O3): codegraph reports **no covering tests** for
 * `PendingApprovalCard` (`ApprovalWorkflowSection.tsx:341`), and C4 changes both
 * that component's mapping-count line (`:161`, reached via
 * `renderTypeSpecificContent`) and the two identity sites R2.4 names —
 * `:625` ("Decided by", on the completed card) and `:665`
 * (`data-testid="audit-actor"`, in the audit trail).
 *
 * **Every expectation here is derived from `requirements.md` R2, not from
 * current rendered output.** That distinction is load-bearing: today both sites
 * render the raw Cognito `sub` (an opaque UUID), which is the defect C4 fixes.
 * `LGR-c58f276-2` (**template / ratified**) records that UUID as a knowingly
 * accepted consequence and names this spec as its `revisit_when`. So this file
 * deliberately does **NOT** assert "an approval carrying an email renders the
 * sub" anywhere — that assertion would pin the bug and make C4 read as a
 * regression instead of the recorded follow-up.
 *
 * What it pins instead is the contract that must hold both before and after C4:
 *
 *  - **CP5 / R2.3** — the identity display never renders blank. Email if
 *    present, else the `sub`, else a stated placeholder. Asserted at BOTH sites
 *    (R2.4), because `LGR-c58f276-2` names both and fixing one leaves the audit
 *    trail showing a UUID.
 *  - **R2.4** — both projections exist for a decided approval.
 *  - **R1.5 / do-NOT 7** — an absent mapping count reads as not-established,
 *    never as `0`. `ApprovalWorkflowSection.tsx:161` is already correct on this
 *    (`?? '—'`); pinning it protects it through task 4.2, which has to fix the
 *    sibling site `ApprovalsPage.tsx:39` where the same value is `?? 0`.
 *  - `PendingApprovalCard`'s structure — the card, its title fallback, and its
 *    approve/reject affordances.
 *
 * A note on the placeholder arm: the two sites use *different* placeholders
 * today ('—' at `:625`, 'System' at `:665`). Both satisfy R2.3's "rather than
 * rendering blank", so the assertions below require a non-empty stated value
 * without pinning either literal — task 4.1 is free to unify them, and O6's
 * `system_timeout` approver has to render legibly there too.
 *
 * **One CP5 gap found while writing this and deliberately left open for task
 * 4.1:** both sites guard the identity with `||`, which is falsy-based, so a
 * whitespace-only `decided_by` renders as whitespace — blank to a reader. The
 * CP5 property below records why it does not generate that shape (not
 * producible today) and that closing it means a trim-aware guard, which is a
 * production change task 1.1 must not make.
 *
 * Requirements: R2.1, R2.3, R2.4, R1.5 — Design: O3, CP5, CP2
 */

import { describe, it, expect, afterEach } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within, screen } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail } from '../../types';

/**
 * `Approval` carries no email field yet — C3 writes `approved_by_email` to the
 * approvals row and C3's read half projects it beside `decided_by`
 * (`pipelines_runs.py:1049`). Declaring it here keeps these tests forward
 * compatible: the shapes below already exercise the arm C4 will implement,
 * without asserting what the component does with it today.
 */
type ApprovalWithEmail = Approval & { decided_by_email?: string };

/** A real Cognito `sub` from the reported record — opaque by design (R3.1). */
const SUB = '583123b0-60b1-7056-51a3-bd7ba804c325';
const EMAIL = 'approver@example.com';

const RUN_DETAILS: RunDetail = {
  run_id: 'run-2026-08-24-13-48-26-4fccdf03',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2026-08-24T13:48:26Z',
} as RunDetail;

function approval(overrides: Partial<ApprovalWithEmail> = {}): ApprovalWithEmail {
  return {
    approval_id: 'ap-1',
    pipeline_key: 'test-pipeline',
    approval_type: 'schema_mapping',
    status: 'approved',
    created_at: '2026-08-24T13:48:26Z',
    decided_at: '2026-08-24T14:02:11Z',
    ...overrides,
  };
}

function renderSection(approvals: ApprovalWithEmail[]) {
  return render(
    <ApprovalWorkflowSection approvals={approvals as Approval[]} runDetails={RUN_DETAILS} />,
  );
}

/**
 * The identity `:625` renders. That line has no test id, so it is read off the
 * completed card's text: `Decided by: {identity} at {time}`. Returning the
 * segment rather than a boolean lets the never-blank assertions see whitespace.
 */
function decidedByIdentity(container: HTMLElement): string {
  const card = within(container).getByTestId('completed-approval');
  const match = /Decided by:(.*?)\bat\b/s.exec(card.textContent ?? '');
  expect(match, 'the completed card must carry a "Decided by:" projection (R2.4)').not.toBeNull();
  return (match?.[1] ?? '').trim();
}

/** The identity `:665` renders, via its stable test id. */
function auditActorIdentities(container: HTMLElement): string[] {
  return within(container)
    .getAllByTestId('audit-actor')
    .map((el) => (el.textContent ?? '').trim());
}

afterEach(() => {
  cleanup();
});

// ---------------------------------------------------------------------------
// CP5 / R2.3 / R2.4 — the identity display, at both sites
// ---------------------------------------------------------------------------

describe('ApprovalWorkflowSection identity projections (R2.3, R2.4, CP5)', () => {
  it('renders both projections for a decided approval — R2.4 names both sites', () => {
    const { container } = renderSection([approval({ decided_by: SUB })]);

    // `:625`, on the completed card.
    expect(decidedByIdentity(container)).not.toHaveLength(0);
    // `:665`, in the audit trail.
    expect(auditActorIdentities(container)).toHaveLength(1);
  });

  it('falls back to the sub rather than blank when that is the only identity available (R2.3)', () => {
    // This is R2.3's middle arm and the state EVERY row written before C3 is in:
    // no email reached the approvals row, so the sub is all there is. "An opaque
    // identity is worse than an email and better than nothing."
    const { container } = renderSection([approval({ decided_by: SUB })]);

    expect(decidedByIdentity(container)).toBe(SUB);
    expect(auditActorIdentities(container)).toEqual([SUB]);
  });

  it('renders a stated placeholder, never an empty string, when no identity is available (R2.3)', () => {
    // R2.3's last arm. The two sites use different placeholders today; the
    // contract is that neither renders blank, not that they agree — task 4.1 may
    // unify them.
    const { container } = renderSection([approval({ decided_by: undefined })]);

    expect(decidedByIdentity(container)).not.toHaveLength(0);
    for (const actor of auditActorIdentities(container)) {
      expect(actor).not.toHaveLength(0);
    }
  });

  it('attributes a system_timeout approver rather than reporting it as absent (O6)', () => {
    // `approval_timeout_handler` writes `approver="system_timeout"` (`:343`,
    // `:399`). Design O6 flags this as unverified against the display. What is
    // pinned is that such a row is ATTRIBUTED — it must not fall through to the
    // no-identity placeholder, because the decision did have an actor. The exact
    // wording is task 4.1's to choose ("legibly rather than as a missing email").
    const noIdentity = renderSection([approval({ decided_by: undefined })]);
    const placeholder = decidedByIdentity(noIdentity.container);
    cleanup();

    const { container } = renderSection([approval({ decided_by: 'system_timeout' })]);
    const identity = decidedByIdentity(container);

    expect(identity).not.toHaveLength(0);
    expect(identity).not.toBe(placeholder);
    for (const actor of auditActorIdentities(container)) {
      expect(actor).not.toHaveLength(0);
    }
  });

  it('never renders blank at either site, for any combination of identity fields (CP5)', () => {
    // CP5 as a property. The `decided_by_email` arm is generated so the shape C4
    // introduces is already covered here; what is asserted is only that neither
    // site goes blank — NOT which of the two values wins, because that is
    // exactly what C4 changes.
    //
    // The generator is constrained to the shapes the PRODUCER can actually
    // write. `decided_by` is synthesised on the read path as
    // `approved_by or rejected_by` (`pipelines_runs.py:1049`) from a Cognito
    // `sub` claim or the timeout handler's literal `system_timeout`; and
    // `LGR-c58f276-2` records that the identity details are "omitted when empty
    // rather than written blank, so an audit row never asserts an identity it
    // does not have". So absent, empty, a real sub and `system_timeout` are the
    // reachable space.
    //
    // **A whitespace-only identity is deliberately NOT generated, and it is a
    // real gap.** Both sites guard with `||`, which is falsy-based, so a
    // `decided_by` of "   " is truthy and renders as three spaces — blank to a
    // reader, and a CP5 violation. It is excluded here because it is not
    // producible today and because closing it means changing the guard, which is
    // task 4.1's change and not a characterization task's. Task 4.1 should make
    // the guard trim-aware; this comment is the handoff.
    const identityArb = fc.oneof(
      fc.constant(undefined),
      fc.constant(''),
      fc.constant(SUB),
      fc.constant('system_timeout'),
    );

    fc.assert(
      fc.property(
        identityArb,
        fc.oneof(fc.constant(undefined), fc.constant(''), fc.constant(EMAIL)),
        // `:625` renders on the COMPLETED card only, so the status arbitrary is
        // over decided statuses. The audit trail covers pending separately.
        fc.constantFrom('approved', 'rejected'),
        (decided_by, decided_by_email, status) => {
          cleanup();
          const { container } = renderSection([
            approval({ decided_by, decided_by_email, status }),
          ]);

          expect(decidedByIdentity(container)).not.toHaveLength(0);
          for (const actor of auditActorIdentities(container)) {
            expect(actor).not.toHaveLength(0);
          }
        },
      ),
      { numRuns: 100 },
    );
  });

  it('never renders a blank audit actor for a pending approval either (CP5)', () => {
    // The audit trail maps over ALL approvals, so a pending row — which has no
    // `decided_by` by construction — reaches `:665` too.
    const { container } = renderSection([
      approval({ approval_id: 'ap-pending', status: 'pending', decided_at: undefined }),
    ]);

    for (const actor of auditActorIdentities(container)) {
      expect(actor).not.toHaveLength(0);
    }
  });
});

// ---------------------------------------------------------------------------
// PendingApprovalCard — structure, and the mapping-count three-state
// ---------------------------------------------------------------------------

describe('PendingApprovalCard characterization (design O3)', () => {
  function pending(details?: Record<string, unknown>): ApprovalWithEmail {
    return approval({
      approval_id: 'ap-pending',
      status: 'pending',
      decided_at: undefined,
      details,
    });
  }

  it('renders the card with its approve and reject affordances', () => {
    renderSection([pending()]);

    const card = screen.getByTestId('pending-approval');
    expect(within(card).getByTestId('approve-button')).toBeTruthy();
    expect(within(card).getByTestId('reject-button')).toBeTruthy();
  });

  it('titles the card from gate_name when present, else the approval type', () => {
    const withGate = renderSection([pending()]);
    expect(
      within(withGate.container).getByTestId('pending-approval').textContent,
    ).toContain('schema_mapping');
    cleanup();

    renderSection([approval({ ...pending(), gate_name: 'Schema Review' })]);
    expect(screen.getByTestId('pending-approval').textContent).toContain('Schema Review');
  });

  it('reports an absent mapping count as not-established, never as 0 (R1.5, do-NOT 7)', () => {
    // The state this spec was reported from: `mapping_count` is not a key at all,
    // because `get_approval_data`'s `schema_mapping` branch reads nine keys off
    // the state row's top level where the producer writes nothing
    // (`LGR-86698d4-1`). This site is already correct — it renders '—' — and that
    // is what must survive task 4.2, which fixes the sibling `ApprovalsPage.tsx:39`
    // where the same value collapses to `0`. Zero mappings and an unreadable
    // payload are different facts (R1.5).
    renderSection([pending({ generated_by: 'us.anthropic.claude-sonnet-4-6' })]);

    const content = screen.getByTestId('type-content-schema-mapping').textContent ?? '';
    expect(content).toContain('Mapping count:');
    expect(content).not.toMatch(/Mapping count:\s*0\b/);
  });

  it('distinguishes a readable count of zero from an unreadable one (CP2)', () => {
    const absent = renderSection([pending({})]);
    const absentText =
      absent.container.querySelector('[data-testid="type-content-schema-mapping"]')?.textContent ??
      '';
    cleanup();

    renderSection([pending({ mapping_count: 0 })]);
    const zeroText = screen.getByTestId('type-content-schema-mapping').textContent ?? '';

    expect(zeroText).toMatch(/Mapping count:\s*0\b/);
    expect(zeroText).not.toBe(absentText);
  });

  it('renders a populated mapping count — the state C1 makes reachable (R1.1)', () => {
    renderSection([pending({ mapping_count: 7 })]);

    expect(screen.getByTestId('type-content-schema-mapping').textContent).toMatch(
      /Mapping count:\s*7\b/,
    );
  });

  it('renders the card for every pending approval and none for decided ones', () => {
    renderSection([
      pending(),
      approval({ approval_id: 'ap-2', status: 'pending', decided_at: undefined }),
      approval({ approval_id: 'ap-3', status: 'approved', decided_by: SUB }),
    ]);

    expect(screen.getAllByTestId('pending-approval')).toHaveLength(2);
    expect(screen.getAllByTestId('completed-approval')).toHaveLength(1);
  });
});
