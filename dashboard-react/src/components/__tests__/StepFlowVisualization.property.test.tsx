// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Property-based tests for StepFlowVisualization
 * Uses fast-check with Vitest and @testing-library/react. Minimum 100 iterations.
 *
 * Feature: react-dashboard-parity
 * - Property 11: Step flow renders all steps with correct status
 * - Property 12: Step metadata rendering
 *
 * Feature: p38-retry-run-identity
 * - Property 9: A related-run link renders only where the id resolves elsewhere
 */

import { describe, it, expect } from 'vitest';
import * as fc from 'fast-check';
import { render, cleanup, within } from '@testing-library/react';
import StepFlowVisualization, { linkable } from '../StepFlowVisualization';
import type { StepDetail, MCPOperation } from '../../types';
import { getStatusIcon } from '../../utils/formatting';

// ---------------------------------------------------------------------------
// Arbitraries
// ---------------------------------------------------------------------------

const stepStatusArb = fc.constantFrom('completed', 'success', 'failed', 'running', 'pending', 'skipped', 'error');

const mcpOperationArb: fc.Arbitrary<MCPOperation> = fc.record({
  operation: fc.constantFrom('read_file', 'write_file', 'list_tools', 'execute_tool'),
  status: fc.constantFrom('completed', 'success', 'failed', 'pending'),
  duration_ms: fc.option(fc.integer({ min: 1, max: 5000 }), { nil: undefined }),
});

const stepDetailArb: fc.Arbitrary<StepDetail> = fc.record({
  step_name: fc.constantFrom(
    'file_validation', 'schema_extract', 'schema_mapping',
    'code_generation', 'code_testing', 'first_run',
  ),
  status: stepStatusArb,
  started_at: fc.option(
    fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
    { nil: undefined },
  ),
  completed_at: fc.option(
    fc.date({ min: new Date('2020-01-01'), max: new Date('2026-01-01') }).map((d) => d.toISOString()),
    { nil: undefined },
  ),
  error_message: fc.option(fc.constant('Something went wrong'), { nil: undefined }),
  duration_ms: fc.option(fc.integer({ min: 1, max: 60000 }), { nil: undefined }),
  throughput: fc.option(fc.integer({ min: 1, max: 10000 }), { nil: undefined }),
  mcp_latency_ms: fc.option(fc.integer({ min: 1, max: 5000 }), { nil: undefined }),
  mcp_operations: fc.option(fc.array(mcpOperationArb, { minLength: 0, maxLength: 4 }), { nil: undefined }),
  logs: fc.option(
    fc.array(fc.constantFrom('INFO: Processing started', 'DEBUG: Step complete', 'WARN: Slow query'), { minLength: 0, maxLength: 5 }),
    { nil: undefined },
  ),
  error_details: fc.option(fc.constant('Detailed error info'), { nil: undefined }),
  troubleshooting: fc.option(
    fc.array(fc.constant('Check network connectivity'), { minLength: 0, maxLength: 3 }),
    { nil: undefined },
  ),
});

// Generate unique step names to avoid duplicate keys
function uniqueStepsArb(minLen: number, maxLen: number): fc.Arbitrary<StepDetail[]> {
  const allStepNames = [
    'file_validation', 'schema_extract', 'schema_mapping',
    'code_generation', 'code_testing', 'first_run',
  ];
  return fc.integer({ min: minLen, max: Math.min(maxLen, allStepNames.length) }).chain((count) => {
    return fc.shuffledSubarray(allStepNames, { minLength: count, maxLength: count }).chain((names) => {
      return fc.tuple(
        ...names.map((name) =>
          stepDetailArb.map((step) => ({ ...step, step_name: name })),
        ),
      );
    });
  });
}

// ---------------------------------------------------------------------------
// Property 11: Step flow renders all steps with correct status
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 11: Step flow renders all steps with correct status', () => {
  /**
   * **Validates: Requirements 9.1, 9.2**
   *
   * For any list of N StepDetail objects, the StepFlowVisualization SHALL
   * render exactly N step cards, each with the correct status icon and color.
   */
  it('renders exactly N step cards with correct status icons for N steps', () => {
    fc.assert(
      fc.property(uniqueStepsArb(1, 6), (steps) => {
        cleanup();
        const { container } = render(
          <StepFlowVisualization steps={steps} />,
        );

        const flow = within(container).getByTestId('step-flow-visualization');
        const stepCards = within(flow).getAllByTestId('step-card');

        // Exactly N step cards
        expect(stepCards).toHaveLength(steps.length);

        // Each card has correct status icon
        for (let i = 0; i < steps.length; i++) {
          const card = stepCards[i];
          const statusIcon = within(card).getByTestId('step-status-icon');
          expect(statusIcon.textContent).toBe(getStatusIcon(steps[i].status));

          // Status text is present
          const statusEl = within(card).getByTestId('step-status');
          expect(statusEl.textContent).toContain(steps[i].status);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});

// ---------------------------------------------------------------------------
// Property 12: Step metadata rendering
// ---------------------------------------------------------------------------

describe('Feature: react-dashboard-parity, Property 12: Step metadata rendering', () => {
  /**
   * **Validates: Requirements 9.3, 9.4, 9.6**
   *
   * For any StepDetail with performance metrics, MCP operations, or logs,
   * the StepFlowVisualization SHALL include the corresponding values in output.
   */
  it('renders duration, throughput, MCP latency when present', () => {
    // Generate steps that always have metrics
    const stepWithMetricsArb: fc.Arbitrary<StepDetail> = fc.record({
      step_name: fc.constant('schema_extract'),
      status: stepStatusArb,
      duration_ms: fc.integer({ min: 1, max: 60000 }),
      throughput: fc.integer({ min: 1, max: 10000 }),
      mcp_latency_ms: fc.integer({ min: 1, max: 5000 }),
    });

    fc.assert(
      fc.property(stepWithMetricsArb, (step) => {
        cleanup();
        const { container } = render(
          <StepFlowVisualization steps={[step]} />,
        );

        const flow = within(container).getByTestId('step-flow-visualization');
        const metrics = within(flow).getByTestId('step-metrics');
        const text = metrics.textContent ?? '';

        expect(text).toContain(`${step.duration_ms}ms`);
        expect(text).toContain(`${step.throughput}/s`);
        expect(text).toContain(`${step.mcp_latency_ms}ms`);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('renders MCP operations list when present', () => {
    const stepWithOpsArb: fc.Arbitrary<StepDetail> = fc.record({
      step_name: fc.constant('code_generation'),
      status: fc.constant('completed'),
      mcp_operations: fc.array(mcpOperationArb, { minLength: 1, maxLength: 4 }),
    });

    fc.assert(
      fc.property(stepWithOpsArb, (step) => {
        cleanup();
        const { container } = render(
          <StepFlowVisualization steps={[step]} />,
        );

        const flow = within(container).getByTestId('step-flow-visualization');
        const opsSection = within(flow).getByTestId('step-mcp-operations');
        const opEls = within(opsSection).getAllByTestId('mcp-operation');

        expect(opEls).toHaveLength(step.mcp_operations!.length);

        for (const op of step.mcp_operations!) {
          expect(opsSection.textContent).toContain(op.operation);
          expect(opsSection.textContent).toContain(op.status);
        }

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  it('renders logs section when logs are present', () => {
    const stepWithLogsArb: fc.Arbitrary<StepDetail> = fc.record({
      step_name: fc.constant('file_validation'),
      status: fc.constant('completed'),
      logs: fc.array(
        fc.constantFrom('INFO: Start', 'DEBUG: Processing', 'WARN: Slow'),
        { minLength: 1, maxLength: 5 },
      ),
    });

    fc.assert(
      fc.property(stepWithLogsArb, (step) => {
        cleanup();
        const { container } = render(
          <StepFlowVisualization steps={[step]} />,
        );

        const flow = within(container).getByTestId('step-flow-visualization');
        // Logs section header should be present
        const logsSection = within(flow).getByTestId('step-logs-section');
        expect(logsSection).toBeTruthy();
        expect(logsSection.textContent).toContain(`Logs (${step.logs!.length})`);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });
});
// ---------------------------------------------------------------------------
// Feature: p38-retry-run-identity, Property 9: A related-run link renders only
// where the id resolves elsewhere
// ---------------------------------------------------------------------------

/**
 * Id-pair arbitrary for Property 9.
 *
 * The three shapes R5.3 distinguishes are drawn as first-class cases rather than
 * left to chance: a shared id (the continuation model's own shape, where
 * `retry_source_run_id === run_id`), an empty/absent related id, and two ids that
 * genuinely differ. `examples` pins each shape so a run can never miss one.
 */
const runIdArb = fc.oneof(
  fc.constantFrom(
    'run-2026-08-09-15-38-19-5c5f5f8a',
    'run-original-1',
    'retry-run-2026-08-09-15-38-19-5c5f5f8a-537b2303',
  ),
  fc.string({ minLength: 1, maxLength: 24 }).map((s) => `run-${s}`),
);

type IdPair = { current: string; related?: string };

const idPairArb: fc.Arbitrary<IdPair> = fc.oneof(
  // One shared id — the continuation model's shape. Never linkable.
  { arbitrary: runIdArb.map((id): IdPair => ({ current: id, related: id })), weight: 2 },
  // Empty string or entirely absent related id. Never linkable.
  {
    arbitrary: fc
      .tuple(runIdArb, fc.constantFrom<string | undefined>('', undefined))
      .map(([current, related]): IdPair => ({ current, related })),
    weight: 2,
  },
  // Two independently drawn ids — usually different, occasionally equal.
  {
    arbitrary: fc.tuple(runIdArb, runIdArb).map(([current, related]): IdPair => ({ current, related })),
    weight: 3,
  },
);

const idPairExamples: [IdPair][] = [
  [{ current: 'run-original-1', related: 'run-original-1' }], // equal ids
  [{ current: 'run-original-1', related: '' }], // empty related
  [{ current: 'run-original-1', related: undefined }], // absent related
  [{ current: 'run-original-1', related: 'run-original-2' }], // resolvable elsewhere
];

/** A step that reaches the skipped-step branch: `trigger="retry"` + status `skipped`. */
const skippedStep: StepDetail = { step_name: 'file_validation', status: 'skipped' };

describe('Feature: p38-retry-run-identity, Property 9: A related-run link renders only where the id resolves elsewhere', () => {
  /**
   * **Validates: Requirements 5.3**
   *
   * For any pair of run ids (current, related), a related-run link renders
   * exactly when `related` is non-empty and differs from `current`, at both the
   * retried-banner site and the skipped-step source-link site.
   *
   * The two arms are asymmetric on purpose. The skipped-step site is asserted by
   * render. The banner site's only candidate id is the run's own under one shared
   * identity, so "renders exactly when they differ" is satisfied there by never
   * rendering — that arm asserts no related-run link renders at all. The banner's
   * example coverage lives in task 8.5.
   */

  // Skipped-step site — asserted by render.
  it('renders the source link exactly when linkable, and the note otherwise', () => {
    // Non-vacuity counters: the branch is only reachable with trigger="retry" AND
    // a skipped step, so a property that rendered nothing would pass trivially.
    // Both counters must end above zero, which proves both arms were exercised.
    let linkRenders = 0;
    let noteRenders = 0;

    fc.assert(
      fc.property(idPairArb, ({ current, related }) => {
        cleanup();
        const { container } = render(
          <StepFlowVisualization
            steps={[skippedStep]}
            trigger="retry"
            retrySourceRunId={related}
            runId={current}
            pipelineId="pipeline-1"
          />,
        );

        const flow = within(container).getByTestId('step-flow-visualization');
        const link = within(flow).queryByTestId('skipped-step-source-link');
        const note = within(flow).queryByTestId('skipped-step-source-note');

        // The branch is reachable on every draw: exactly one of the two renders.
        expect(Boolean(link) !== Boolean(note)).toBe(true);

        // The render agrees with the real predicate — this is why `linkable` is
        // imported rather than replicated.
        expect(Boolean(link)).toBe(linkable(related, current));

        // ...and the predicate agrees with R5.3 stated independently of its
        // implementation: non-empty, and different from the current run.
        expect(linkable(related, current)).toBe(
          related !== undefined && related.length > 0 && related !== current,
        );

        if (link) {
          linkRenders += 1;
          expect(link.textContent).toContain(related!);
          expect(link.getAttribute('href')).toBe(`/pipelines/pipeline-1/runs/${related}`);
        } else {
          noteRenders += 1;
          expect(note!.textContent).toContain("Result from this run's earlier attempt");
        }

        cleanup();
      }),
      { numRuns: 100, examples: idPairExamples },
    );

    expect(linkRenders).toBeGreaterThan(0);
    expect(noteRenders).toBeGreaterThan(0);
  });

  // Banner site — asserts that NO related-run link renders at all.
  it('renders no related-run link anywhere when one id is shared by both ends', () => {
    fc.assert(
      fc.property(runIdArb, (sharedId) => {
        cleanup();

        // The banner's only candidate id is the run's own, so the predicate is
        // false for every id — there is no draw on which a banner link is correct.
        expect(linkable(sharedId, sharedId)).toBe(false);

        const { container } = render(
          <StepFlowVisualization
            steps={[skippedStep]}
            trigger="retry"
            retrySourceRunId={sharedId}
            runId={sharedId}
            pipelineId="pipeline-1"
          />,
        );

        const flow = within(container).getByTestId('step-flow-visualization');
        expect(within(flow).queryByTestId('skipped-step-source-link')).toBeNull();

        // No anchor pointing at a run page: a related-run link is exactly that.
        const runLinks = Array.from(container.querySelectorAll('a[href]')).filter((a) =>
          (a.getAttribute('href') ?? '').includes('/runs/'),
        );
        expect(runLinks).toHaveLength(0);

        cleanup();
      }),
      { numRuns: 100 },
    );
  });

  /**
   * Deliberate, not incidental: §4.4's ternary is literal, so an entirely absent
   * `retrySourceRunId` renders the note rather than nothing. Property 9 permits
   * that — the link renders exactly when the id is non-empty and different — and
   * this case is asserted explicitly so a reader knows it was intended.
   */
  it('renders the note, not nothing, when retrySourceRunId is absent entirely', () => {
    cleanup();
    const { container } = render(
      <StepFlowVisualization steps={[skippedStep]} trigger="retry" runId="run-original-1" pipelineId="pipeline-1" />,
    );

    const flow = within(container).getByTestId('step-flow-visualization');
    expect(within(flow).queryByTestId('skipped-step-source-link')).toBeNull();
    const note = within(flow).getByTestId('skipped-step-source-note');
    expect(note.textContent).toContain("Result from this run's earlier attempt");
    cleanup();
  });
});
