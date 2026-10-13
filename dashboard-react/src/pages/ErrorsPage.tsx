// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo } from 'react';
import Header from '@cloudscape-design/components/header';
import Table from '@cloudscape-design/components/table';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Select from '@cloudscape-design/components/select';
import DateRangePicker from '@cloudscape-design/components/date-range-picker';
import Badge from '@cloudscape-design/components/badge';
import type { DateRangePickerProps } from '@cloudscape-design/components/date-range-picker';
import { useErrors } from '../hooks/use-errors';
import type { ErrorEntry } from '../hooks/use-errors';

const ERROR_TYPE_OPTIONS = [
  { label: 'All Types', value: '' },
  { label: 'Permission', value: 'permission' },
  { label: 'Timeout', value: 'timeout' },
  { label: 'Data Format', value: 'data_format' },
  { label: 'Code Error', value: 'code_error' },
  { label: 'Infrastructure', value: 'infrastructure' },
  { label: 'Throttling', value: 'throttling' },
  { label: 'Unknown', value: 'unknown' },
  { label: 'Schema Mismatch', value: 'schema_mismatch' },
  { label: 'Resource Quota', value: 'resource_quota' },
  { label: 'Data Quality', value: 'data_quality' },
  { label: 'Network', value: 'network' },
  { label: 'Authentication', value: 'authentication' },
];

const ERROR_TYPE_COLORS: Record<string, string> = {
  permission: 'red',
  timeout: 'red',
  data_format: 'blue',
  code_error: 'blue',
  infrastructure: 'red',
  throttling: 'grey',
  unknown: 'grey',
  schema_mismatch: 'blue',
  resource_quota: 'red',
  data_quality: 'blue',
  network: 'red',
  authentication: 'red',
};

function formatTimestamp(ts: string): string {
  // timestamp format: "2025-01-15T10:30:00+00:00#run-id"
  const isopart = ts.split('#')[0];
  try {
    return new Date(isopart).toLocaleString();
  } catch {
    return ts;
  }
}

export default function ErrorsPage() {
  const [errorTypeFilter, setErrorTypeFilter] = useState({ label: 'All Types', value: '' });
  const [dateRange, setDateRange] = useState<DateRangePickerProps.Value | null>({
    type: 'relative',
    amount: 7,
    unit: 'day',
    key: 'last-7-days',
  });

  const dateParams = useMemo(() => {
    if (!dateRange) return {};
    if (dateRange.type === 'relative') {
      const end = new Date();
      const start = new Date();
      const amount = dateRange.amount;
      const unit = dateRange.unit;
      if (unit === 'day') start.setDate(start.getDate() - amount);
      else if (unit === 'week') start.setDate(start.getDate() - amount * 7);
      else if (unit === 'month') start.setMonth(start.getMonth() - amount);
      return {
        start_date: start.toISOString().split('T')[0],
        end_date: end.toISOString().split('T')[0],
      };
    }
    return {
      start_date: dateRange.startDate.split('T')[0],
      end_date: dateRange.endDate.split('T')[0],
    };
  }, [dateRange]);

  const filters = useMemo(() => ({
    error_type: errorTypeFilter.value || undefined,
  }), [errorTypeFilter]);

  // Async_State_Triple (p29 R1, p30 R13.4): loading, error and genuinely-empty are
  // three distinct states. A failed fetch must not fall through to "No errors
  // found" — the most misleading output this page can produce, because the reader
  // concludes the fleet is healthy exactly when the dashboard cannot tell.
  // `loading`/`loadingText` on the Table cover the first state; the error state
  // swaps the Table's empty slot for a message plus Retry, matching
  // PipelineListPage and ApprovalsPage.
  const { data, isLoading, isError, refetch } = useErrors(dateParams, filters);
  const errors = data?.errors ?? [];

  return (
    <SpaceBetween size="l">
      <Header
        variant="h1"
        description="Recent pipeline failures across all pipelines"
        counter={data ? `(${errors.length})` : undefined}
      >
        Errors
      </Header>

      <SpaceBetween size="s" direction="horizontal">
        <DateRangePicker
          value={dateRange}
          onChange={({ detail }) => setDateRange(detail.value)}
          isValidRange={() => ({ valid: true })}
          relativeOptions={[
            { key: 'last-1-day', amount: 1, unit: 'day', type: 'relative' },
            { key: 'last-7-days', amount: 7, unit: 'day', type: 'relative' },
            { key: 'last-30-days', amount: 30, unit: 'day', type: 'relative' },
          ]}
          placeholder="Filter by date range"
          i18nStrings={{
            todayAriaLabel: 'Today',
            nextMonthAriaLabel: 'Next month',
            previousMonthAriaLabel: 'Previous month',
            customRelativeRangeDurationLabel: 'Duration',
            customRelativeRangeDurationPlaceholder: 'Enter duration',
            customRelativeRangeOptionLabel: 'Custom range',
            customRelativeRangeOptionDescription: 'Set a custom range in the past',
            customRelativeRangeUnitLabel: 'Unit of time',
            formatRelativeRange: (e) => `Last ${e.amount} ${e.unit}${e.amount > 1 ? 's' : ''}`,
            formatUnit: (unit, value) => `${value} ${unit}${value > 1 ? 's' : ''}`,
            dateTimeConstraintText: '',
            relativeModeTitle: 'Relative range',
            absoluteModeTitle: 'Absolute range',
            relativeRangeSelectionHeading: 'Choose a range',
            startDateLabel: 'Start date',
            endDateLabel: 'End date',
            startTimeLabel: 'Start time',
            endTimeLabel: 'End time',
            clearButtonLabel: 'Clear',
            cancelButtonLabel: 'Cancel',
            applyButtonLabel: 'Apply',
          }}
        />
        <Select
          selectedOption={errorTypeFilter}
          onChange={({ detail }) => setErrorTypeFilter(detail.selectedOption as typeof errorTypeFilter)}
          options={ERROR_TYPE_OPTIONS}
          placeholder="Filter by error type"
        />
      </SpaceBetween>

      <Table<ErrorEntry>
        loading={isLoading}
        loadingText="Loading errors..."
        items={errors}
        empty={
          isError ? (
            <Box textAlign="center" color="inherit" padding="l">
              <SpaceBetween size="s">
                <span>Failed to load errors. Check your API connection.</span>
                <Box>
                  <Button data-testid="errors-retry" onClick={() => refetch()}>Retry</Button>
                </Box>
              </SpaceBetween>
            </Box>
          ) : (
            <Box textAlign="center" color="inherit" padding="l">
              <b>No errors found</b>
              <Box variant="p" color="inherit">No pipeline errors in the selected date range.</Box>
            </Box>
          )
        }
        columnDefinitions={[
          {
            id: 'timestamp',
            header: 'Date',
            cell: (item: ErrorEntry) => formatTimestamp(item.timestamp),
            sortingField: 'timestamp',
            width: 200,
          },
          {
            id: 'pipeline_id',
            header: 'Pipeline',
            cell: (item: ErrorEntry) => item.pipeline_id,
            sortingField: 'pipeline_id',
          },
          {
            id: 'step_name',
            header: 'Step',
            cell: (item: ErrorEntry) => item.step_name,
            sortingField: 'step_name',
          },
          {
            id: 'error_type',
            header: 'Error Type',
            cell: (item: ErrorEntry) => (
              <Badge color={ERROR_TYPE_COLORS[item.error_type] as 'red' | 'blue' | 'grey' ?? 'grey'}>
                {item.error_type}
              </Badge>
            ),
            sortingField: 'error_type',
          },
          {
            id: 'error_message',
            header: 'Message',
            cell: (item: ErrorEntry) => (
              <Box variant="code" fontSize="body-s">
                {(item.error_message || '').substring(0, 200)}
                {(item.error_message || '').length > 200 ? '...' : ''}
              </Box>
            ),
            maxWidth: 400,
          },
        ]}
        sortingDisabled={false}
        variant="full-page"
        stickyHeader
      />
    </SpaceBetween>
  );
}
