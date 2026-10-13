// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * The two identity projections after p50 C4 (task 4.1) — what they now RENDER,
 * as opposed to the contract task 1.1 pinned before the change.
 *
 * Split from `ApprovalWorkflowSection.actor-identity.characterization.test.tsx`
 * on purpose. That file's header states that it "deliberately does **NOT**
 * assert 'an approval carrying an email renders the sub' anywhere", and that
 * which of the two values wins "is exactly what C4 changes" — so it asserts only
 * that neither site goes blank. Folding the winner into it would contradict its
 * stated scope. It keeps the before-and-after contract (CP5, never blank, at
 * both sites); this file asserts C4's choice.
 *
 * Both sites, always, because `R2.4` requires both and `LGR-c58f276-2`
 * (**template / ratified**) names both — `:625` "Decided by" on the completed
 * card, and `:665` `data-testid="audit-actor"` in the audit trail. A test that
 * checked one would let the audit trail keep rendering a UUID, which is the
 * half-fix R2.4 exists to prevent.
 *
 * What is asserted, and where each expectation comes from:
 *
 *  - **R2.1 / R2.4** — an email present on the record wins at both sites. This
 *    closes `LGR-c58f276-2`'s `revisit_when` ("the UI projection is changed to
 *    read `actor_email`"), and per that record the field is now *load-bearing*.
 *  - **R2.3** — with no email, the `sub` is rendered rather than blank. This is
 *    not a legacy path: **every** approvals row written before task 3.1 is in
 *    it, and task 3.2 projects `decided_by_email` as always-present-and-`None`
 *    for those, which is what the fall-through is built on.
 *  - **R3.3 / do-NOT 3** — `decided_by` keeps carrying the `sub`; the email is
 *    additive. Asserted by rendering a record with both and confirming the
 *    `sub` is still the value used when the email is gone.
 *  - **O6** — a `system_timeout` actor reads as a system decision, not as a
 *    missing email and not as the no-identity placeholder.
 *  - **CP5** — the whitespace arm the 1.1 characterization file found and handed
 *    over: `.trim()` instead of a falsy guard, so `'   '` cannot render as three
 *    spaces.
 *
 * Requirements: R2.1, R2.3, R2.4, R3.3 — Design: C4, CP5, O6
 */

import { describe, it, expect, afterEach } from 'vitest';
import { render, cleanup, within } from '@testing-library/react';
import ApprovalWorkflowSection from '../ApprovalWorkflowSection';
import type { Approval, RunDetail } from '../../types';

/** The real Cognito `sub` from the reported record — opaque by design (R3.1). */
const SUB = '583123b0-60b1-7056-51a3-bd7ba804c325';
const EMAIL = 'approver@example.com';

const RUN_DETAILS: RunDetail = {
  run_id: 'run-2026-08-24-13-48-26-4fccdf03',
  pipeline_key: 'test-pipeline',
  status: 'RUNNING',
  started_at: '2026-08-24T13:48:26Z',
} as RunDetail;

function approval(overrides: Partial<Approval> = {}): Approval {
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

function renderSection(approvals: Approval[]) {
  return render(<ApprovalWorkflowSection approvals={approvals} runDetails={RUN_DETAILS} />);
}

/**
 * The identity rendered at `:625`. That line has no test id, so it is read off
 * the completed card's text: `Decided by: {identity} at {time}`. Same reader as
 * the 1.1 characterization file, deliberately — returning the raw segment lets a
 * whitespace-only render be seen rather than swallowed.
 */
function decidedByIdentity(container: HTMLElement): string {
  const card = within(container).getByTestId('completed-approval');
  const match = /Decided by:(.*?)\bat\b/s.exec(card.textContent ?? '');
  expect(match, 'the completed card must carry a "Decided by:" projection (R2.4)').not.toBeNull();
  return (match?.[1] ?? '').trim();
}

/** The identity rendered at `:665`, via its stable test id. */
function auditActorIdentities(container: HTMLElement): string[] {
  return within(container)
    .getAllByTestId('audit-actor')
    .map((el) => (el.textContent ?? '').trim());
}

/** Both sites at once — every assertion in this file is about both (R2.4). */
function bothSites(container: HTMLElement): string[] {
  return [decidedByIdentity(container), ...auditActorIdentities(container)];
}

afterEach(() => {
  cleanup();
});

describe('the deciding identity prefers the email (p50 C4, R2.1, R2.4)', () => {
  it('renders the email at both sites when the record carries one', () => {
    const { container } = renderSection([
      approval({ decided_by: SUB, decided_by_email: EMAIL }),
    ]);

    for (const identity of bothSites(container)) {
      expect(identity).toBe(EMAIL);
      // The defect this task closes: an opaque UUID where an email was expected.
      expect(identity).not.toContain(SUB);
    }
  });

  it('renders the email even when it is the only identity on the record', () => {
    // Not producible by today's writers — task 3.1 only ever adds the email
    // beside an actor — but the preference must not depend on the sub existing.
    const { container } = renderSection([approval({ decided_by_email: EMAIL })]);

    for (const identity of bothSites(container)) {
      expect(identity).toBe(EMAIL);
    }
  });
});

describe('the deciding identity falls back to the sub, never to blank (R2.3, CP5)', () => {
  it('renders the sub at both sites when no email reached the row', () => {
    // The state of EVERY approvals row written before task 3.1. Task 3.2
    // projects `decided_by_email` as always present and `None` for those, so
    // this is the live path, not a legacy one.
    const { container } = renderSection([approval({ decided_by: SUB })]);

    for (const identity of bothSites(container)) {
      expect(identity).toBe(SUB);
    }
  });

  it('renders the sub when the projected email is null on the wire (task 3.2 shape)', () => {
    // `a["decided_by_email"] = a.get("approved_by_email") or a.get("rejected_by_email")`
    // — always present, `None` when neither stored attribute exists. JSON puts
    // that on the wire as `null`, which the declared optional type does not
    // describe, so the cast states what the producer actually sends.
    const { container } = renderSection([
      approval({ decided_by: SUB, decided_by_email: null as unknown as undefined }),
    ]);

    for (const identity of bothSites(container)) {
      expect(identity).toBe(SUB);
    }
  });

  it('keeps the sub as the identity when the email is absent — the email is additive (R3.3)', () => {
    // do-NOT 3: `approved_by` still carries the `sub` and nothing replaced it.
    // With the email gone the sub is unchanged and still what renders.
    const withEmail = renderSection([approval({ decided_by: SUB, decided_by_email: EMAIL })]);
    expect(decidedByIdentity(withEmail.container)).toBe(EMAIL);
    cleanup();

    const { container } = renderSection([approval({ decided_by: SUB })]);
    for (const identity of bothSites(container)) {
      expect(identity).toBe(SUB);
    }
  });

  it('renders one stated placeholder at both sites when there is no identity at all', () => {
    // The two sites used to disagree ('—' and 'System'); C4 unifies them. The
    // audit trail's old 'System' was the worse of the two: it asserted a system
    // actor for any row with no identity, including every pending one.
    const { container } = renderSection([approval({ decided_by: undefined })]);

    const identities = bothSites(container);
    for (const identity of identities) {
      expect(identity).not.toHaveLength(0);
      expect(identity).toBe(identities[0]);
    }
  });

  it('does not render a whitespace-only identity as whitespace (CP5)', () => {
    // The gap task 1.1 found and handed over: both sites guarded with `||`, so
    // '   ' was truthy and rendered as three spaces — blank to a reader. Not
    // reachable from today's producers, which is why 1.1 recorded it instead of
    // pinning it. `.trim()` closes it.
    const empty = renderSection([approval({ decided_by: undefined })]);
    const placeholder = decidedByIdentity(empty.container);
    cleanup();

    const { container } = renderSection([
      approval({ decided_by: '   ', decided_by_email: '  ' }),
    ]);

    for (const identity of bothSites(container)) {
      expect(identity).toBe(placeholder);
    }
  });
});

describe('a system_timeout decision is attributed legibly (O6)', () => {
  it('names the system and the reason at both sites', () => {
    // `approval_timeout_handler` writes this literal as the deciding actor
    // (`:327` auto-approve, `:383` auto-reject).
    const { container } = renderSection([approval({ decided_by: 'system_timeout' })]);

    for (const identity of bothSites(container)) {
      expect(identity).toBe('System (approval timeout)');
      // Not the raw literal, and not a bare word that reads as a username.
      expect(identity).not.toBe('system_timeout');
    }
  });

  it('does not fall through to the no-identity placeholder — the decision had an actor', () => {
    const empty = renderSection([approval({ decided_by: undefined })]);
    const placeholder = decidedByIdentity(empty.container);
    cleanup();

    const { container } = renderSection([approval({ decided_by: 'system_timeout' })]);

    for (const identity of bothSites(container)) {
      expect(identity).not.toBe(placeholder);
    }
  });
});
