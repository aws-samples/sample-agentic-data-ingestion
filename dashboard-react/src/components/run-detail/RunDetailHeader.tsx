// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Select from '@cloudscape-design/components/select';
import { sameStep } from '../../utils/step-names';

interface RetryStepOption {
  label: string;
  value: string;
}

interface RunDetailHeaderProps {
  runId: string;
  status: string;
  isActive: boolean;
  prevRunId?: string;
  nextRunId?: string;
  retryStepOptions: RetryStepOption[];
  selectedRetryStep?: string;
  retryPending: boolean;
  cancelPending: boolean;
  onBack: () => void;
  onPrev: () => void;
  onNext: () => void;
  onSelectRetryStep: (value?: string) => void;
  onRetry: () => void;
  onCancel: () => void;
  onRefresh: () => void;
}

/**
 * Navigation bar + page header (title + FAILED-run retry controls, cancel,
 * refresh) extracted from RunDetailPage (P29 R11.5). Behavior-preserving:
 * navigation and mutation side effects stay owned by the parent via callbacks.
 */
export default function RunDetailHeader({
  runId,
  status,
  isActive,
  prevRunId,
  nextRunId,
  retryStepOptions,
  selectedRetryStep,
  retryPending,
  cancelPending,
  onBack,
  onPrev,
  onNext,
  onSelectRetryStep,
  onRetry,
  onCancel,
  onRefresh,
}: RunDetailHeaderProps) {
  return (
    <>
      {/* Navigation bar */}
      <SpaceBetween direction="horizontal" size="xs">
        <Button variant="link" iconName="arrow-left" onClick={onBack}>
          Back to Pipeline
        </Button>
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            {/* p48 R10.2 / task 5.2 — "Previous" means OLDER and "Next" means
                NEWER; the ids are resolved server-side from `RunsByTimeIndex`
                (`use-run-neighbours.ts`), so these work at any depth in the
                history rather than dying at a page edge.

                The unavailable state is carried in WORDS as well as by the
                greyed-out control — `LGR-99050e6-1` (template / ratified) records
                that colour alone fails WCAG 1.4.1, and a disabled button that
                says only "Previous Run" does not state WHY it cannot be pressed.
                `ariaLabel` rather than visible copy: two sentences in a nav bar
                would be noise on every run detail page, and the fact is
                programmatically determinable and announced where it is needed. */}
            <Button
              iconName="angle-left"
              disabled={!prevRunId}
              ariaLabel={prevRunId ? 'Previous Run, the run before this one' : 'Previous Run, unavailable: this is the oldest run of this pipeline'}
              onClick={onPrev}
            >
              Previous Run
            </Button>
            <Button
              iconAlign="right"
              iconName="angle-right"
              disabled={!nextRunId}
              ariaLabel={nextRunId ? 'Next Run, the run after this one' : 'Next Run, unavailable: this is the newest run of this pipeline'}
              onClick={onNext}
            >
              Next Run
            </Button>
          </SpaceBetween>
        </Box>
      </SpaceBetween>

      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            {status === 'FAILED' && (
              <>
                <Select
                  selectedOption={
                    // The default selection is the failed step's RAW name from
                    // the run (snake_case on new runs) while option values are
                    // SFN state ids — match in canonical space (P28 task 14.2).
                    (() => {
                      const match = selectedRetryStep
                        ? retryStepOptions.find((o) => sameStep(o.value, selectedRetryStep))
                        : undefined;
                      return match ? { label: match.label, value: match.value } : null;
                    })()
                  }
                  onChange={({ detail }) => onSelectRetryStep(detail.selectedOption.value ?? undefined)}
                  options={retryStepOptions.map((o) => ({ label: o.label, value: o.value }))}
                  disabled={retryPending}
                  placeholder="Select retry step"
                  data-testid="retry-step-select"
                />
                <Button
                  onClick={onRetry}
                  loading={retryPending}
                  data-testid="retry-button"
                >
                  Retry from Step
                </Button>
              </>
            )}
            {isActive && (
              <Button onClick={onCancel} loading={cancelPending}>
                Cancel Run
              </Button>
            )}
            <Button iconName="refresh" onClick={onRefresh}>Refresh</Button>
          </SpaceBetween>
        }
      >
        Run: {runId}
      </Header>
    </>
  );
}
