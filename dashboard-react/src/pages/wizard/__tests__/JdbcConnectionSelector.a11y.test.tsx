// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * JdbcConnectionSelector accessibility tests — two radio groups, two group labels.
 *
 * Validates: Requirements 10.6, 10.7
 *
 * P31 R10.2 recorded the shipped defect: the connection-mode `<Tiles>` had no
 * label of its own and sat directly under the step's "Source Type" tiles, so
 * the two rendered as one continuous 2×3 grid with two filled tiles — a state
 * a radio group cannot be in. R10.6 requires the two groups to be exposed as
 * two radio groups with distinct accessible labels; R10.7 requires a test so
 * the regression cannot return silently.
 *
 * These assertions are deliberately made against the **accessibility tree**
 * (role + accessible name), not the DOM shape. Cloudscape's `Tiles` renders
 * `role="radiogroup"` and reads `aria-labelledby` from `FormFieldContext`
 * (`@cloudscape-design/components/tiles/internal.js`), so the group only has an
 * accessible name while it is wrapped in a `<FormField label=…>`. Removing that
 * wrapper — the exact R10.2 regression — still renders the tiles, so a
 * `data-testid` or CSS-class assertion would pass through it. A role/name query
 * fails.
 *
 * Both render paths are covered because R10.5 scopes the fix to *wherever* the
 * shared selector is rendered: `WizardStepSource` (source) and `JdbcTargetForm`
 * (target). `JdbcSourceForm` has no test file of its own, so the source path is
 * exercised through the step that owns the neighbouring "Source Type" group —
 * which is the only place the two-groups-as-one defect is observable.
 *
 * NOTE: automated role and accessible-name checks are not a substitute for
 * testing with a real screen reader. They prove the group boundaries and names
 * exist in the accessibility tree; they do not prove the announcement is
 * comprehensible.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { WizardStepSource } from '../WizardStepSource';
import { JdbcTargetForm } from '../JdbcTargetForm';
import { INITIAL_WIZARD_STATE } from '../types';
import type { WizardState } from '../types';
import SourceConfigDisplay from '../../../components/SourceConfigDisplay';
import type { Pipeline } from '../../../types';

vi.mock('../../../hooks', async () => {
  const actual = await vi.importActual<Record<string, unknown>>('../../../hooks');
  return {
    ...actual,
    useRdsInstances: () => ({ data: [], isLoading: false }),
    useRdsClusters: () => ({ data: [], isLoading: false }),
    useRdsGlueConnections: () => ({ data: [], isLoading: false }),
    useRdsSecrets: () => ({ data: [], isLoading: false }),
    useJdbcDetectSchemaStart: () => ({ mutate: vi.fn(), isPending: false }),
    useJdbcDetectSchemaStatus: () => ({ data: undefined, isError: false, error: null }),
  };
});

/** The three connection-mode tile labels, in the order the component renders
 *  them. Order is load-bearing: P20 Req 1.1 pins `manual` as the third tile and
 *  `select_rds` leads because it is the default mode
 *  (`config._connection_mode ?? 'select_rds'`). Nothing else in the suite pins
 *  the order — every other tile assertion queries by label text. */
const CONNECTION_MODE_LABELS = [
  'Select RDS Instance',
  'Use Glue Connection',
  'External or self-managed database',
] as const;

/** `_connection_mode` value → tile label. The same three strings are held by
 *  `components/SourceConfigDisplay.tsx` (read-back of a saved pipeline) and by
 *  `JdbcConnectionSelector.mode-switch.prop.test.tsx`. The last describe block
 *  asserts the wizard and the read-back agree. */
const MODE_TO_LABEL: Record<string, string> = {
  select_rds: 'Select RDS Instance',
  glue_connection: 'Use Glue Connection',
  manual: 'External or self-managed database',
};

function renderSourceStep(overrides: Partial<WizardState> = {}) {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    sourceType: 'jdbc',
    ...overrides,
  };
  return render(
    <QueryClientProvider client={queryClient}>
      <WizardStepSource state={state} onChange={vi.fn()} />
    </QueryClientProvider>,
  );
}

function renderTargetForm(overrides: Partial<WizardState> = {}) {
  const state: WizardState = {
    ...INITIAL_WIZARD_STATE,
    targetType: 'jdbc',
    ...overrides,
  };
  return render(<JdbcTargetForm state={state} onChange={vi.fn()} />);
}

/** Accessible names of the radios inside a group, in DOM order.
 *
 *  DOM order matters and `getByRole(..., { name })` cannot express it, so the
 *  name is resolved here. The `accessibleName` resolver is cross-checked
 *  against Testing Library's own computation in the first membership test. */
function radioNames(group: HTMLElement): string[] {
  return within(group).getAllByRole('radio').map(accessibleName);
}

// ─── Source render path (R10.6) ─────────────────────────────────

describe('JdbcConnectionSelector a11y — source path exposes two labelled radio groups (R10.6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('exposes Source Type and Connection Method as two distinct radio groups', () => {
    renderSourceStep();

    const sourceType = screen.getByRole('radiogroup', { name: 'Source Type' });
    const connectionMethod = screen.getByRole('radiogroup', { name: 'Connection Method' });

    // Two groups, not one. If the FormField wrapper is removed from the
    // connection-mode Tiles, the second query throws — the R10.2 regression.
    expect(sourceType).not.toBe(connectionMethod);
    expect(sourceType).not.toContainElement(connectionMethod);
    expect(connectionMethod).not.toContainElement(sourceType);
  });

  it('gives every radio group on the step a unique, non-empty accessible name', () => {
    renderSourceStep();

    // The JDBC source step renders three tile groups: Source Type (the step),
    // Connection Method (the shared selector), Read Mode (JdbcSourceForm).
    // An unlabelled group is the defect class R10.6 exists to close, so assert
    // it for all of them rather than only the two named above.
    const names = screen.getAllByRole('radiogroup').map(accessibleName);

    expect(names).toContain('Source Type');
    expect(names).toContain('Connection Method');
    for (const name of names) {
      expect(name.trim()).not.toBe('');
    }
    expect(new Set(names).size).toBe(names.length);
  });

  it('keeps the connection-mode options out of the Source Type group', () => {
    renderSourceStep();

    const sourceType = screen.getByRole('radiogroup', { name: 'Source Type' });
    const connectionMethod = screen.getByRole('radiogroup', { name: 'Connection Method' });

    // As shipped, the two groups read as one five-option list. Membership is
    // the assertion that pins the separation: each option belongs to exactly
    // one group.
    expect(radioNames(sourceType)).toEqual(['S3 Upload', 'JDBC Database']);
    expect(radioNames(connectionMethod)).toEqual([...CONNECTION_MODE_LABELS]);

    // Cross-check the local name resolver against Testing Library's own
    // accessible-name computation, and assert the negative directly: the
    // connection-mode options are not reachable inside the Source Type group.
    for (const label of CONNECTION_MODE_LABELS) {
      expect(within(connectionMethod).getByRole('radio', { name: label })).toBeInTheDocument();
      expect(within(sourceType).queryByRole('radio', { name: label })).toBeNull();
    }
  });

  it('shows exactly one selection per group, not two in one group', () => {
    renderSourceStep();

    const sourceType = screen.getByRole('radiogroup', { name: 'Source Type' });
    const connectionMethod = screen.getByRole('radiogroup', { name: 'Connection Method' });

    // R10.2's visible symptom: "JDBC Database" and "External or self-managed
    // database" both filled and blue in what looked like one group. Two checked
    // radios in one radiogroup is not a state a radio group can be in.
    const checked = (group: HTMLElement) =>
      within(group)
        .getAllByRole('radio')
        .filter((r) => (r as HTMLInputElement).checked);

    expect(checked(sourceType)).toHaveLength(1);
    expect(checked(connectionMethod)).toHaveLength(1);
  });

  it('renders the Database Connection heading that separates the group visually', () => {
    renderSourceStep();
    // R10.1's visual separation is a Container with an h3 header. Asserted by
    // role so it is the heading in the accessibility tree, not just the text.
    expect(
      screen.getByRole('heading', { name: 'Database Connection' }),
    ).toBeInTheDocument();
  });

  it('lists the three modes in the pinned order — select_rds first, manual third', () => {
    renderSourceStep();
    const connectionMethod = screen.getByRole('radiogroup', { name: 'Connection Method' });
    // P20 Req 1.1 pins `manual` third; `select_rds` leads because it is the
    // default mode. Every other tile assertion in the suite queries by label,
    // so a reorder would otherwise pass unnoticed.
    expect(radioNames(connectionMethod)).toEqual([...CONNECTION_MODE_LABELS]);
  });
});

// ─── Target render path (R10.5 + R10.6) ─────────────────────────

describe('JdbcConnectionSelector a11y — target path inherits the labelled group (R10.5, R10.6)', () => {
  beforeEach(() => vi.clearAllMocks());

  it('exposes Connection Method and Write Mode as two distinct radio groups', () => {
    renderTargetForm();

    const connectionMethod = screen.getByRole('radiogroup', { name: 'Connection Method' });
    const writeMode = screen.getByRole('radiogroup', { name: 'Write Mode' });

    expect(connectionMethod).not.toBe(writeMode);
    expect(connectionMethod).not.toContainElement(writeMode);
    expect(radioNames(connectionMethod)).toEqual([...CONNECTION_MODE_LABELS]);
    expect(radioNames(writeMode)).toEqual(['Overwrite', 'Append']);
  });

  it('gives every radio group on the target form a unique, non-empty accessible name', () => {
    renderTargetForm();
    const names = screen.getAllByRole('radiogroup').map(accessibleName);
    expect(names).toContain('Connection Method');
    for (const name of names) {
      expect(name.trim()).not.toBe('');
    }
    expect(new Set(names).size).toBe(names.length);
  });

  it('renders the Database Connection heading on the target side too', () => {
    renderTargetForm();
    expect(
      screen.getByRole('heading', { name: 'Database Connection' }),
    ).toBeInTheDocument();
  });
});

// ─── Two-sided label contract ───────────────────────────────────

describe('connection-mode labels agree between the wizard and the saved-pipeline read-back', () => {
  beforeEach(() => vi.clearAllMocks());

  it('SourceConfigDisplay renders the same label the wizard tile carries, for every mode', () => {
    // The three label strings live in three places: the selector's tiles,
    // `SourceConfigDisplay`'s `_connection_mode` map, and the mode-switch
    // property test. Changing one desyncs the wizard from what the user is
    // shown after saving. Asserted by rendering both rather than by comparing
    // constants, so neither side is its own oracle.
    const { unmount } = renderSourceStep();
    const wizardLabels = radioNames(
      screen.getByRole('radiogroup', { name: 'Connection Method' }),
    );
    unmount();

    expect(wizardLabels).toEqual([...CONNECTION_MODE_LABELS]);

    for (const [mode, expectedLabel] of Object.entries(MODE_TO_LABEL)) {
      const view = render(<SourceConfigDisplay pipeline={jdbcPipeline(mode)} />);
      expect(view.getByTestId('source-connection-mode').textContent).toBe(expectedLabel);
      expect(wizardLabels).toContain(expectedLabel);
      view.unmount();
    }
  });
});

// ─── Helpers ────────────────────────────────────────────────────

/** Resolve an element's accessible name from the two mechanisms Cloudscape
 *  uses: `aria-label`, and `aria-labelledby` (FormField → Tiles group label;
 *  AbstractSwitch → radio label span). Returns '' when the element has neither
 *  — which for a radiogroup is exactly the defect R10.6 closes. */
function accessibleName(el: HTMLElement): string {
  const direct = el.getAttribute('aria-label');
  if (direct) return direct.trim();
  const ids = el.getAttribute('aria-labelledby');
  if (!ids) return '';
  return ids
    .split(/\s+/)
    .map((id) => document.getElementById(id)?.textContent ?? '')
    .join(' ')
    .trim();
}

function jdbcPipeline(connectionMode: string): Pipeline {
  return {
    pipeline_key: 'a11y-pipe',
    pipeline_name: 'A11y Pipeline',
    status: 'active',
    source_schema: 'src',
    target_schema: 'tgt',
    created_at: '2024-01-01T00:00:00Z',
    updated_at: '2024-01-02T00:00:00Z',
    source_type: 'JDBC',
    source_config: {
      _connection_mode: connectionMode,
      engine: 'postgres',
      jdbc_url: 'jdbc:postgresql://host:5432/db',
      jdbc_driver: 'org.postgresql.Driver',
    },
  };
}
