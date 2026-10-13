// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Alert from '@cloudscape-design/components/alert';
import Button from '@cloudscape-design/components/button';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Spinner from '@cloudscape-design/components/spinner';
import {
  colorTextStatusSuccess,
  colorTextStatusError,
  colorTextStatusInfo,
  colorTextStatusWarning,
  colorTextBodyDefault,
  colorTextBodySecondary,
  colorBorderStatusSuccess,
  colorBorderStatusError,
  colorBorderStatusInfo,
  colorBorderDividerDefault,
  colorBackgroundStatusSuccess,
  colorBackgroundStatusError,
  colorBackgroundStatusInfo,
  colorBackgroundContainerContent,
} from '@cloudscape-design/design-tokens';
import { useTestResults } from '../hooks';

export interface TestResultsViewerProps {
  codeVersionId: string;
  testStep?: string;
}

/** Determine if a status string represents a passing test */
function isPassing(status: string): boolean {
  const s = (status ?? '').toLowerCase();
  return s === 'passed' || s === 'pass' || s === 'success' || s === 'succeeded';
}

/** Try to format output as indented JSON; return original if not valid JSON */
function formatOutput(output: string): string {
  try {
    const parsed = JSON.parse(output);
    return JSON.stringify(parsed, null, 2);
  } catch {
    return output;
  }
}

/** Lightweight markdown-to-HTML for test reports */
function renderMarkdown(text: string): string {
  let html = text
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
  // Headers: # ## ### ####
  html = html.replace(/^#### (.+)$/gm, '<h4 style="margin:12px 0 4px;font-size:14px">$1</h4>');
  html = html.replace(/^### (.+)$/gm, '<h3 style="margin:16px 0 6px;font-size:16px">$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2 style="margin:20px 0 8px;font-size:18px">$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h1 style="margin:24px 0 10px;font-size:22px">$1</h1>');
  // Bold: **text**
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  // Horizontal rules: ---
  html = html.replace(/^---+$/gm, `<hr style="margin:12px 0;border:none;border-top:1px solid ${colorBorderDividerDefault}"/>`);
  // List items: - text
  html = html.replace(/^- (.+)$/gm, '<li style="margin:2px 0;margin-left:20px">$1</li>');
  // Numbered items: 1. text
  html = html.replace(/^\d+\. (.+)$/gm, '<li style="margin:2px 0;margin-left:20px;list-style:decimal">$1</li>');
  // Newlines to <br> (but not after block elements)
  html = html.replace(/\n/g, '<br/>');
  // Clean up double <br/> after block elements
  html = html.replace(/(<\/h[1-4]>)<br\/>/g, '$1');
  html = html.replace(/(<hr[^>]*\/>)<br\/>/g, '$1');
  html = html.replace(/(<\/li>)<br\/>/g, '$1');
  return html;
}

/**
 * Severity tokens that do not fail the review gate (p43 R4.3).
 *
 * `review_verdict.NON_BLOCKING_SEVERITIES` is the source of truth for this set;
 * this is a display-side mirror of it across the language boundary. A token
 * outside the set blocks, so a novel severity is labelled as blocking here for
 * the same reason the gate blocks on it. Drift shows up as a display lie rather
 * than a gate defect, which is what the cross-language contract pin guards.
 */
const NON_BLOCKING_SEVERITIES = new Set(['minor']);

/**
 * Format the AI reviewer's static-analysis verdict — embedded as a raw JSON
 * object inside the report markdown (under the "# Original Static Analysis
 * Report" heading) — into readable markdown at render time.
 *
 * Pure and defensive: the input `text` is returned unchanged when no verdict
 * JSON candidate is found, when the candidate isn't valid JSON, or when the
 * parsed object lacks both a `verdict` and a `categories` key. On a successful
 * match the raw JSON substring is spliced out and replaced with markdown that
 * only uses constructs the existing `renderMarkdown` supports (`###`,
 * `**bold**`, `- ` list items, plain text, emoji).
 */
export function formatStaticAnalysisVerdict(text: string): string {
  if (!text) return text;

  // Locate the reviewer verdict JSON: the first `{` (with optional whitespace)
  // immediately followed by the "verdict" key.
  const match = text.match(/\{\s*"verdict"/);
  if (!match || match.index === undefined) return text;

  const start = match.index;

  // Walk from `start` tracking brace depth, correctly skipping over string
  // literals (respecting `\` escapes) to find the matching closing `}`.
  let depth = 0;
  let inString = false;
  let escaped = false;
  let end = -1;
  for (let i = start; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escaped) {
        escaped = false;
      } else if (ch === '\\') {
        escaped = true;
      } else if (ch === '"') {
        inString = false;
      }
      continue;
    }
    if (ch === '"') {
      inString = true;
    } else if (ch === '{') {
      depth++;
    } else if (ch === '}') {
      depth--;
      if (depth === 0) {
        end = i;
        break;
      }
    }
  }

  if (end === -1) return text; // no matching closing brace — leave unchanged

  const jsonStr = text.slice(start, end + 1);

  let parsed: unknown;
  try {
    parsed = JSON.parse(jsonStr);
  } catch {
    return text; // malformed/partial JSON — defensive, leave unchanged
  }

  if (typeof parsed !== 'object' || parsed === null) return text;
  const obj = parsed as {
    verdict?: unknown;
    issues?: unknown;
    categories?: unknown;
  };
  if (!('verdict' in obj) && !('categories' in obj)) return text;

  const lines: string[] = [];

  // Verdict
  const verdictStr = typeof obj.verdict === 'string' ? obj.verdict : '';
  const verdictPass = verdictStr.toLowerCase() === 'pass';
  lines.push(`**Verdict:** ${verdictPass ? '✅ Pass' : '❌ Fail'}`);

  // Issues
  const issues = Array.isArray(obj.issues) ? obj.issues : [];
  if (issues.length === 0) {
    lines.push('');
    lines.push('No issues reported.');
  } else {
    lines.push('');
    lines.push(`### Issues (${issues.length})`);
    for (const issue of issues) {
      if (typeof issue !== 'object' || issue === null) continue;
      const it = issue as {
        severity?: unknown;
        category?: unknown;
        description?: unknown;
        attribution?: unknown;
      };
      const severity = typeof it.severity === 'string' ? it.severity : '';
      const category = typeof it.category === 'string' ? it.category : '';
      const description = typeof it.description === 'string' ? it.description : '';
      // Blocking status, stated in words (p43 R4.3). Colour or an icon alone
      // fails WCAG 1.4.1 and makes the status invisible, so no new emoji or
      // styling is introduced here — the status is a phrase in the bullet text.
      //
      // Every phrase describes what the issue does *by itself*, never what the
      // run's outcome was: the unattributed rule's antecedent is a property of
      // the whole issue list, so a flat "did not block" would be a claim this
      // loop cannot support. The unattributed case therefore states the rule
      // rather than resolving it per run — resolving it would be a second
      // implementation of the gate, in another language, that no test could see
      // drift.
      const attribution = typeof it.attribution === 'string' ? it.attribution.trim().toLowerCase() : '';
      const reviewerAttributed =
        attribution === 'transform_block' || attribution === 'template_scaffolding';
      let blocking: string;
      if (!reviewerAttributed) {
        blocking = 'no reviewer attribution — does not block a pass verdict, blocks a fail verdict';
      } else if (attribution === 'template_scaffolding') {
        blocking = 'scaffolding — does not fail the gate by itself';
      } else if (NON_BLOCKING_SEVERITIES.has(severity.trim().toLowerCase())) {
        blocking = 'reported at non-blocking severity — did not fail the gate';
      } else {
        blocking = 'blocked the gate';
      }
      let bullet = `- **${severity} · ${category}** — ${description}`;
      // The raw attribution token is still rendered verbatim: an unrecognised
      // token is shown as the reviewer wrote it *and* labelled as not being a
      // recognised attribution. What was said and what the gate did with it are
      // two facts, and the bullet carries both.
      bullet +=
        typeof it.attribution === 'string' && it.attribution
          ? ` _(${it.attribution} · ${blocking})_`
          : ` _(${blocking})_`;
      lines.push(bullet);
    }
  }

  // Category checks
  if (obj.categories && typeof obj.categories === 'object') {
    lines.push('');
    lines.push('### Category checks');
    for (const [name, value] of Object.entries(obj.categories as Record<string, unknown>)) {
      const v = typeof value === 'string' ? value.toLowerCase() : '';
      const ok = v === 'pass' || v === 'passed';
      lines.push(`- ${ok ? '✅' : '❌'} ${name}`);
    }
  }

  const replacement = lines.join('\n');
  return text.slice(0, start) + replacement + text.slice(end + 1);
}

export default function TestResultsViewer({ codeVersionId }: TestResultsViewerProps) {
  // Async_State_Triple (p29 R1, p30 R14.4): loading, error and genuinely-empty
  // are three distinct states. The error state carries a Retry affordance wired
  // to the query's own refetch, matching StatsBar and the AI Traces tab — a
  // read-only "Failed to load" left the reader with no way forward but a full
  // page reload. The `!data` case is split out of the error branch on purpose:
  // a query that is neither loading nor failed but has no data yet (disabled or
  // paused) is empty, not broken, and must not be reported as a failure.
  const { data, isLoading, isError, refetch } = useTestResults(codeVersionId);

  if (isLoading) {
    return (
      <div data-testid="test-results-viewer">
        <Box textAlign="center" padding="l">
          <Spinner size="large" />
          <Box variant="p" color="text-body-secondary">Loading test results...</Box>
        </Box>
      </div>
    );
  }

  if (isError) {
    return (
      <div data-testid="test-results-viewer">
        <Container>
          <Alert
            type="error"
            data-testid="test-results-error"
            action={<Button data-testid="test-results-retry" onClick={() => refetch()}>Retry</Button>}
          >
            Failed to load test results.
          </Alert>
        </Container>
      </div>
    );
  }

  if (!data) {
    return (
      <div data-testid="test-results-viewer">
        <Container>
          <Box textAlign="center" color="text-body-secondary" padding="l" data-testid="test-results-empty">
            No test results available.
          </Box>
        </Container>
      </div>
    );
  }

  const passing = isPassing(data.test_status);

  // Try to extract structured test_results for the report cards
  let testResults: { tests_passed?: number; tests_failed?: number; tests_run?: number; execution_time_ms?: number; coverage_percent?: number } | null = null;
  try {
    // The output contains "Full results:\n{...}" — extract the JSON part
    const jsonMatch = data.test_output?.match(/Full results:\n([\s\S]+)$/);
    if (jsonMatch) testResults = JSON.parse(jsonMatch[1]);
  } catch { /* not structured */ }

  return (
    <div data-testid="test-results-viewer">
      <Container header={<Header variant="h3">Test Report</Header>}>
        <SpaceBetween size="m">
          {/* Status badge */}
          <div>
            <Box variant="awsui-key-label">Status</Box>
            <span
              data-testid="test-status"
              className={passing ? 'test-status-pass' : 'test-status-fail'}
              style={{ color: passing ? colorTextStatusSuccess : colorTextStatusError, fontWeight: 'bold', fontSize: '16px' }}
            >
              {passing ? '✅' : '❌'} {data.test_status?.toUpperCase()}
            </span>
          </div>

          {/* Executed at */}
          {data.executed_at && (
            <div>
              <Box variant="awsui-key-label">Executed</Box>
              <Box>{new Date(data.executed_at).toLocaleString()}</Box>
            </div>
          )}

          {/* Stats cards */}
          {testResults && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: '12px' }}>
              <div style={{ padding: '16px', borderRadius: '8px', background: colorBackgroundStatusSuccess, border: `1px solid ${colorBorderStatusSuccess}`, textAlign: 'center' }}>
                <div style={{ fontSize: '28px', fontWeight: 'bold', color: colorTextStatusSuccess }}>{testResults.tests_passed ?? 0}</div>
                <div style={{ fontSize: '13px', color: colorTextBodySecondary }}>Passed</div>
              </div>
              <div style={{ padding: '16px', borderRadius: '8px', background: colorBackgroundStatusError, border: `1px solid ${colorBorderStatusError}`, textAlign: 'center' }}>
                <div style={{ fontSize: '28px', fontWeight: 'bold', color: colorTextStatusError }}>{testResults.tests_failed ?? 0}</div>
                <div style={{ fontSize: '13px', color: colorTextBodySecondary }}>Failed</div>
              </div>
              <div style={{ padding: '16px', borderRadius: '8px', background: colorBackgroundStatusInfo, border: `1px solid ${colorBorderStatusInfo}`, textAlign: 'center' }}>
                <div style={{ fontSize: '28px', fontWeight: 'bold', color: colorTextStatusInfo }}>{testResults.tests_run ?? 0}</div>
                <div style={{ fontSize: '13px', color: colorTextBodySecondary }}>Total</div>
              </div>
              <div style={{ padding: '16px', borderRadius: '8px', background: colorBackgroundContainerContent, border: `1px solid ${colorBorderDividerDefault}`, textAlign: 'center' }}>
                <div style={{ fontSize: '28px', fontWeight: 'bold', color: colorTextBodyDefault }}>{((testResults.execution_time_ms ?? 0) / 1000).toFixed(1)}s</div>
                <div style={{ fontSize: '13px', color: colorTextBodySecondary }}>Duration</div>
              </div>
              {(testResults.coverage_percent ?? 0) > 0 && (
                <div style={{ padding: '16px', borderRadius: '8px', background: colorBackgroundContainerContent, border: `1px solid ${colorBorderDividerDefault}`, textAlign: 'center' }}>
                  <div style={{ fontSize: '28px', fontWeight: 'bold', color: colorTextBodyDefault }}>{testResults.coverage_percent}%</div>
                  <div style={{ fontSize: '13px', color: colorTextBodySecondary }}>Coverage</div>
                </div>
              )}
            </div>
          )}

          {/* Pass rate bar */}
          {testResults && (testResults.tests_run ?? 0) > 0 && (
            <div>
              <Box variant="awsui-key-label">Pass Rate</Box>
              <div style={{ display: 'flex', alignItems: 'center', gap: '8px' }}>
                <div style={{ flex: 1, height: '8px', borderRadius: '4px', background: colorBorderDividerDefault, overflow: 'hidden' }}>
                  <div style={{
                    width: `${((testResults.tests_passed ?? 0) / (testResults.tests_run ?? 1)) * 100}%`,
                    height: '100%',
                    background: passing ? colorTextStatusSuccess : colorTextStatusWarning,
                    borderRadius: '4px',
                  }} />
                </div>
                <Box variant="small">{Math.round(((testResults.tests_passed ?? 0) / (testResults.tests_run ?? 1)) * 100)}%</Box>
              </div>
            </div>
          )}

          {/* Formatted test report */}
          {data.test_output && (
            <div>
              <Box variant="awsui-key-label">Test Review</Box>
              <div
                data-testid="test-output"
                style={{
                  padding: '16px',
                  borderRadius: '8px',
                  border: `1px solid ${colorBorderDividerDefault}`,
                  lineHeight: '1.6',
                  fontSize: '14px',
                  maxHeight: '600px',
                  overflow: 'auto',
                }}
                dangerouslySetInnerHTML={{ __html: renderMarkdown(formatStaticAnalysisVerdict(data.test_output)) }}
              />
            </div>
          )}

          {/* Error details */}
          {data.error_details && (
            <div>
              <Box variant="awsui-key-label">Error Details</Box>
              <pre
                data-testid="error-details"
                style={{
                  backgroundColor: colorBackgroundStatusError,
                  color: colorTextStatusError,
                  padding: '12px',
                  borderRadius: '4px',
                  overflow: 'auto',
                  maxHeight: '300px',
                  fontSize: '13px',
                  lineHeight: '1.4',
                  fontFamily: 'monospace',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {data.error_details}
              </pre>
            </div>
          )}
        </SpaceBetween>
      </Container>
    </div>
  );
}

/**
 * Pure rendering component for testing — renders a TestResult directly
 * without the hook dependency.
 */
export function TestResultsDisplay({ testResult }: { testResult: { test_status: string; test_output: string; error_details?: string } }) {
  const passing = isPassing(testResult.test_status);

  return (
    <div data-testid="test-results-viewer">
      <SpaceBetween size="m">
        <span
          data-testid="test-status"
          className={passing ? 'test-status-pass' : 'test-status-fail'}
          style={{ color: passing ? colorTextStatusSuccess : colorTextStatusError, fontWeight: 'bold' }}
        >
          {testResult.test_status}
        </span>

        {testResult.test_output && (
          <pre data-testid="test-output" style={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap' }}>
            {formatOutput(testResult.test_output)}
          </pre>
        )}

        {testResult.error_details && (
          <pre data-testid="error-details" style={{ fontFamily: 'monospace', whiteSpace: 'pre-wrap', color: colorTextStatusError }}>
            {testResult.error_details}
          </pre>
        )}
      </SpaceBetween>
    </div>
  );
}
