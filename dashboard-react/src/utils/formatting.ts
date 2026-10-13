// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Pure utility functions for the AgenticDataIngestion React dashboard.
 * These functions have no side effects and are ideal for property-based testing.
 */

import type { Pipeline, PipelineFile, Approval } from '../types';

const TERMINAL_STATUSES = new Set(['succeeded', 'completed', 'failed', 'error', 'stopped', 'timed_out', 'aborted', 'cancelled']);

/**
 * Run statuses that can still change by themselves, so a view of the run keeps
 * polling. CANCELLING belongs here: the run's Glue job is stopping and the run
 * becomes CANCELLED when it has stopped, which can take minutes. Note it is not
 * a cancellable status; the Cancel buttons gate on their own list.
 */
const ACTIVE_RUN_STATUSES = new Set(['RUNNING', 'IN_PROGRESS', 'WAITING_APPROVAL', 'CANCELLING']);

/** True when a run (not a step) is still in flight. Exact, upper-case, as the API returns it. */
export function isActiveRunStatus(status: string | undefined | null): boolean {
  return ACTIVE_RUN_STATUSES.has(status ?? '');
}

/**
 * True when a run/execution/step status is terminal (no further transitions
 * expected). Case-insensitive. This is the single source of truth for
 * "is this done?" checks — reused by adaptive polling (P29 R11.3) so it never
 * grows a second, divergent terminal-status list.
 */
export function isTerminalStatus(status: string | undefined | null): boolean {
  return TERMINAL_STATUSES.has((status ?? '').toLowerCase());
}

/**
 * If the overall run/execution status is terminal but a step still shows RUNNING,
 * override the step status to match the overall status.
 * This handles a backend quirk where the last step's status isn't always updated.
 */
export function resolveStepStatus(stepStatus: string, overallStatus: string): string {
  if (stepStatus.toLowerCase() === 'running' && TERMINAL_STATUSES.has(overallStatus.toLowerCase())) {
    const overall = overallStatus.toLowerCase();
    if (overall === 'failed' || overall === 'error') return 'failed';
    // A step that was running when the run was cancelled did not succeed.
    if (overall === 'cancelled') return 'cancelled';
    return 'success';
  }
  return stepStatus;
}

export type CloudscapeStatusType = 'success' | 'error' | 'warning' | 'info' | 'in-progress';

/**
 * Maps a pipeline/run/step status string to a Cloudscape StatusIndicator type.
 * Case-insensitive. Returns 'info' for unknown statuses.
 */
export function statusType(status: string): CloudscapeStatusType {
  switch (status?.toLowerCase()) {
    case 'completed': case 'active': case 'succeeded': case 'ready': case 'approved':
      return 'success';
    case 'failed': case 'error': case 'stopped': case 'rejected': case 'expired': case 'cancelled':
      return 'error';
    case 'running': case 'in_progress': case 'in-progress': case 'cancelling':
      return 'in-progress';
    case 'waiting_approval': case 'pending': case 'approval_required':
      return 'warning';
    case 'aborted': case 'timed_out':
      return 'warning';
    default:
      return 'info';
  }
}



/**
 * Returns a confidence level string based on a numeric confidence value.
 * - "high" if confidence >= 0.8
 * - "medium" if confidence >= 0.6
 * - "low" if confidence < 0.6
 *
 * Matches the vanilla JS `getConfidenceColor` thresholds.
 */
export function getConfidenceLevel(confidence: number): 'high' | 'medium' | 'low' {
  if (confidence >= 0.8) return 'high';
  if (confidence >= 0.6) return 'medium';
  return 'low';
}

/**
 * Computes the most recent activity date from pipelines and files,
 * excluding archived pipelines. Returns an ISO date string or '—' if no data.
 */
export function computeLastActivity(
  pipelines: Pipeline[],
  files: PipelineFile[],
): string {
  const activePipelines = pipelines.filter(
    (p) => p.status?.toLowerCase() !== 'archived',
  );

  const dates: Date[] = [];

  for (const p of activePipelines) {
    if (p.updated_at) {
      const d = new Date(p.updated_at);
      if (!isNaN(d.getTime())) dates.push(d);
    }
    if (p.created_at) {
      const d = new Date(p.created_at);
      if (!isNaN(d.getTime())) dates.push(d);
    }
  }

  // Only include files that belong to active pipelines
  const activePipelineKeys = new Set(activePipelines.map((p) => p.pipeline_key));
  for (const f of files) {
    if (activePipelineKeys.has(f.pipeline_key) && f.processed_at) {
      const d = new Date(f.processed_at);
      if (!isNaN(d.getTime())) dates.push(d);
    }
  }

  if (dates.length === 0) return '—';

  const maxDate = new Date(Math.max(...dates.map((d) => d.getTime())));
  return maxDate.toISOString();
}

/**
 * Sorts pipelines by the given criterion.
 * - "name": alphabetical by pipeline_name (case-insensitive)
 * - "activity": most recent updated_at first
 * - "files": most files first (descending file_count)
 *
 * Returns a new sorted array (does not mutate the input).
 */
export function sortPipelines(
  pipelines: Pipeline[],
  sortBy: 'name' | 'activity' | 'files',
): Pipeline[] {
  const sorted = [...pipelines];

  switch (sortBy) {
    case 'name':
      sorted.sort((a, b) =>
        (a.pipeline_name ?? '').localeCompare(b.pipeline_name ?? '', undefined, {
          sensitivity: 'base',
        }),
      );
      break;

    case 'activity':
      sorted.sort((a, b) => {
        const dateA = new Date(a.updated_at ?? 0).getTime();
        const dateB = new Date(b.updated_at ?? 0).getTime();
        return dateB - dateA; // most recent first
      });
      break;

    case 'files':
      sorted.sort((a, b) => (b.file_count ?? 0) - (a.file_count ?? 0));
      break;
  }

  return sorted;
}

/**
 * Computes approval performance metrics from an array of approvals.
 * Returns average response time (ms) and approval rate (0–1).
 */
export function computeApprovalMetrics(approvals: Approval[]): {
  averageResponseTime: number;
  approvalRate: number;
} {
  if (approvals.length === 0) {
    return { averageResponseTime: 0, approvalRate: 0 };
  }

  const withResponseTime = approvals.filter(
    (a) => a.response_time_ms != null,
  );
  const averageResponseTime =
    withResponseTime.length > 0
      ? withResponseTime.reduce((sum, a) => sum + (a.response_time_ms ?? 0), 0) /
        withResponseTime.length
      : 0;

  const approvedCount = approvals.filter(
    (a) => a.status?.toLowerCase() === 'approved',
  ).length;
  const approvalRate = approvedCount / approvals.length;

  return { averageResponseTime, approvalRate };
}

/**
 * Maps a step execution status to an emoji icon.
 * Matches the vanilla JS `getStatusIcon` implementation.
 */
export function getStatusIcon(status: string): string {
  const iconMap: Record<string, string> = {
    completed: '✅',
    success: '✅',
    succeeded: '✅',
    failed: '❌',
    error: '❌',
    aborted: '⛔',
    cancelled: '🚫',
    cancelling: '⏹️',
    timed_out: '⏰',
    running: '🔄',
    pending: '⏳',
    waiting_approval: '🔔',
    skipped: '⏭️',
  };
  return iconMap[status.toLowerCase()] || '⚙️';
}

/**
 * Maps a step execution status to a Cloudscape color token.
 */
export function getStatusColor(status: string): string {
  const colorMap: Record<string, string> = {
    completed: 'text-status-success',
    success: 'text-status-success',
    succeeded: 'text-status-success',
    failed: 'text-status-error',
    error: 'text-status-error',
    running: 'text-status-info',
    pending: 'text-status-inactive',
    skipped: 'text-status-inactive',
  };
  return colorMap[status.toLowerCase()] || 'text-status-inactive';
}

/**
 * Formats a timestamp as both absolute and relative time.
 * Example: "Mar 16, 2026 19:48 (14d ago)"
 */
export function formatTimestamp(date: string | Date): string {
  let parsed: Date;

  if (typeof date === 'string') {
    const hasTimezone = /[Zz]$/.test(date) || /[+-]\d{2}:\d{2}$/.test(date);
    const utcStr = hasTimezone ? date : date + 'Z';
    parsed = new Date(utcStr);
  } else if (date instanceof Date) {
    parsed = date;
  } else {
    return 'Unknown';
  }

  if (isNaN(parsed.getTime())) return 'Unknown';

  const absolute = parsed.toLocaleDateString(undefined, {
    month: 'short', day: 'numeric', year: 'numeric',
  }) + ' ' + parsed.toLocaleTimeString(undefined, {
    hour: '2-digit', minute: '2-digit',
  });

  const relative = formatRelativeTime(parsed);
  return `${absolute} (${relative})`;
}

/**
 * Converts an ISO date string to a human-readable relative time string.
 * Matches the vanilla JS `Formatters.formatRelativeTime` behavior.
 */
export function formatRelativeTime(date: string | Date): string {
  let parsed: Date;

  if (typeof date === 'string') {
    // Handle various timezone formats: Z, +00:00, or no timezone
    const hasTimezone = /[Zz]$/.test(date) || /[+-]\d{2}:\d{2}$/.test(date);
    const utcStr = hasTimezone ? date : date + 'Z';
    parsed = new Date(utcStr);
  } else if (date instanceof Date) {
    parsed = date;
  } else {
    return 'Unknown';
  }

  if (isNaN(parsed.getTime())) return 'Unknown';

  const now = new Date();
  const diffMs = now.getTime() - parsed.getTime();

  if (diffMs < 0) return 'Just now';

  const diffMinutes = Math.floor(diffMs / (1000 * 60));
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffDays > 0) return `${diffDays}d ago`;
  if (diffHours > 0) return `${diffHours}h ago`;
  if (diffMinutes > 0) return `${diffMinutes}m ago`;
  return 'Just now';
}

/**
 * Converts a snake_case step name to a human-readable Title Case name.
 * Uses a known mapping for common step names, falling back to generic conversion.
 * Matches the vanilla JS `Formatters.formatStepName` behavior.
 */
export function formatStepName(stepName: string): string {
  const stepNames: Record<string, string> = {
    file_validation: 'File Validation',
    schema_extract: 'Schema Extraction',
    schema_comparison: 'Schema Comparison',
    schema_mapping: 'Schema Mapping',
    code_generation: 'Code Generation',
    code_testing: 'Code Testing',
    first_run: 'First Run',
    // P28 task 14.2: canonical snake_case step names on new runs keep the
    // established display labels (labels unchanged, only row keys renamed).
    create_code: 'Code Generation',
    test_code: 'Code Testing',
  };

  return (
    stepNames[stepName] ??
    stepName
      .replace(/_/g, ' ')
      .replace(/\b\w/g, (l) => l.toUpperCase())
  );
}

/**
 * The honest-provenance marker a fallback schema mapping is stamped with instead
 * of a Bedrock model id.
 *
 * P28 R3.1 writes this onto both `generated_by` and `ai_model_used` so that a
 * mapping produced by substring matching can never claim a model it did not use
 * (`lambda/agentic_data_ingestion_schema_mapping/lambda_function.py`,
 * `FALLBACK_GENERATED_BY`). The value is therefore a **cross-language literal**:
 * it is declared once in Python and once here, and nothing in either language
 * can import the other.
 *
 * It is exported so `tests/contracts/test_fallback_model_sentinel_cross_language.py`
 * can parse it out by name and hold it against the Python declaration. Do not
 * inline it — a literal buried in a comparison is invisible to that pin, which is
 * the drift `LGR-1e135d2-12` exists to prevent.
 */
export const FALLBACK_MODEL_SENTINEL = 'fallback_substring_match';

/**
 * Every honest-provenance marker begins with this prefix.
 *
 * `ApprovalArtifact` already classifies fallback provenance this way
 * (`generatedBy.startsWith('fallback_')`, `ApprovalArtifact.tsx:865`). Reusing
 * the same rule means a marker added to the backend later cannot silently fall
 * back to the truncation branch and render as corruption.
 */
const FALLBACK_MARKER_PREFIX = 'fallback_';

/**
 * What a fallback mapping shows where a model name would otherwise go.
 *
 * Says plainly that no model was involved rather than naming one. Deliberately
 * not phrased as an error: the state is expected and the stored value is correct,
 * so the wording sits in the same family as the fuller statement on the approval
 * surface ("Fallback mapping — generated by substring matching, not AI",
 * `ApprovalArtifact.tsx:872`) without repeating its detail in a narrow slot.
 */
export const FALLBACK_MODEL_DISPLAY = 'Fallback — no model used';

/**
 * Maps AWS Bedrock model IDs to human-readable display names.
 * Matches the vanilla JS `getModelDisplayName` implementation.
 *
 * Also renders the fallback provenance marker honestly. Before that case existed,
 * `'fallback_substring_match'` matched no model branch and split on `'.'` into a
 * single part, so it reached the trailing truncation and rendered as
 * `fallback_substring_m...` under a label reading "AI Model" — which looks like
 * data corruption while the stored value is in fact correct.
 */
export function getModelDisplayName(modelId: string | undefined | null): string {
  if (!modelId) return 'Unknown';

  // The marker Python actually writes today, held against its Python declaration
  // by tests/contracts/test_fallback_model_sentinel_cross_language.py.
  if (modelId === FALLBACK_MODEL_SENTINEL) return FALLBACK_MODEL_DISPLAY;

  // Any future sibling marker: still not a model, so it must not reach the
  // truncation branch below and read as corruption.
  if (modelId.startsWith(FALLBACK_MARKER_PREFIX)) return FALLBACK_MODEL_DISPLAY;

  // Claude 4 models (newest)
  if (modelId.includes('claude-4-sonnet')) return 'Claude 4 Sonnet';
  if (modelId.includes('claude-4-opus')) return 'Claude 4 Opus';
  if (modelId.includes('claude-4-haiku')) return 'Claude 4 Haiku';

  // Claude 4 and later: family first, then the version (claude-sonnet-4-6,
  // claude-opus-4-1-20250805-v1:0, claude-sonnet-5). The version is read from the
  // id rather than listed per model, because a list falls behind every release:
  // `claude-sonnet-4-6` used to match a generic `claude-sonnet-4` branch and
  // display as "Claude Sonnet 4". The minor is one or two digits and must not be
  // followed by another digit, so a date suffix (-20250514) is never read as one.
  const familyFirst = /claude-(sonnet|opus|haiku)-(\d+)(?:-(\d{1,2}))?(?!\d)/.exec(modelId);
  if (familyFirst) {
    const [, family, major, minor] = familyFirst;
    const name = family.charAt(0).toUpperCase() + family.slice(1);
    return `Claude ${name} ${minor ? `${major}.${minor}` : major}`;
  }

  // Claude 3.5 models (handle both dot and dash variants)
  if (modelId.includes('claude-3.5-sonnet') || modelId.includes('claude-3-5-sonnet'))
    return 'Claude 3.5 Sonnet';
  if (modelId.includes('claude-3.5-haiku') || modelId.includes('claude-3-5-haiku'))
    return 'Claude 3.5 Haiku';

  // Claude 3 models
  if (modelId.includes('claude-3-haiku')) return 'Claude 3 Haiku';
  if (modelId.includes('claude-3-sonnet')) return 'Claude 3 Sonnet';
  if (modelId.includes('claude-3-opus')) return 'Claude 3 Opus';

  // Other models
  if (modelId.includes('titan')) return 'Amazon Titan';
  if (modelId.includes('llama')) return 'Llama';
  if (modelId.includes('mistral')) return 'Mistral';
  if (modelId.includes('nova')) return 'Amazon Nova';

  // Fallback: extract model name from ID (e.g., "anthropic.claude-xxx" → "Claude Xxx")
  const parts = modelId.split('.');
  if (parts.length > 1) {
    return parts[1]
      .replace(/-/g, ' ')
      .replace(/\b\w/g, (l) => l.toUpperCase());
  }

  return modelId.substring(0, 20) + '...';
}

/**
 * Calculates the waiting time from a creation date to now (or a decided date),
 * returning a human-readable duration string like "2h 30m" or "1d 5h".
 * Matches the vanilla JS `Formatters.calculateWaitingTime` behavior.
 */
export function calculateWaitingTime(
  createdAt: string,
  decidedAt?: string | null,
): string {
  const endTime = decidedAt ? new Date(decidedAt) : new Date();
  const startTime = new Date(createdAt);
  const diffMs = endTime.getTime() - startTime.getTime();
  const diffMinutes = Math.floor(diffMs / (1000 * 60));
  const diffHours = Math.floor(diffMinutes / 60);
  const diffDays = Math.floor(diffHours / 24);

  if (diffDays > 0) return `${diffDays}d ${diffHours % 24}h`;
  if (diffHours > 0) return `${diffHours}h ${diffMinutes % 60}m`;
  return `${diffMinutes}m`;
}
