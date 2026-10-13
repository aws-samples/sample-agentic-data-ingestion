// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo, useCallback } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import TextFilter from '@cloudscape-design/components/text-filter';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Select from '@cloudscape-design/components/select';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import { usePipelines, useBulkArchive, useBulkDelete } from '../hooks';
import type { BulkResult } from '../hooks';
import { sortPipelines, statusType } from '../utils/formatting';
import type { Pipeline, PipelineFilters } from '../types';
import StatsBar from '../components/StatsBar';
import ViewToggle from '../components/ViewToggle';
import PipelineSortControls from '../components/PipelineSortControls';
import PipelineCard from '../components/PipelineCard';
import BulkActionBar from '../components/BulkActionBar';
import BulkConfirmModal from '../components/BulkConfirmModal';
import ListPagination from '../components/common/ListPagination';
import { useToast } from '../contexts/ToastContext';

const STATUS_OPTIONS = [
  { value: '', label: 'All statuses' },
  { value: 'active', label: 'Active' },
  { value: 'completed', label: 'Completed' },
  { value: 'error', label: 'Error' },
  { value: 'pending', label: 'Pending' },
];

function formatDate(iso: string): string {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}

const COLUMN_DEFINITIONS = [
  {
    id: 'name',
    header: 'Pipeline name',
    cell: (item: Pipeline) => (
      <Link to={`/pipelines/${item.pipeline_key}`}>
        {item.pipeline_name || item.pipeline_key}
      </Link>
    ),
    sortingField: 'pipeline_name',
  },
  {
    id: 'status',
    header: 'Status',
    cell: (item: Pipeline) => {
      const displayStatus = item.status === 'approval_required' ? 'Approval Required'
        : item.status === 'running' ? 'Running'
        : item.status;
      return (
        <StatusIndicator type={statusType(item.status)}>
          {displayStatus}
        </StatusIndicator>
      );
    },
    sortingField: 'status',
  },
  {
    id: 'source',
    header: 'Source schema',
    cell: (item: Pipeline) => item.source_schema || '—',
  },
  {
    id: 'target',
    header: 'Target schema',
    cell: (item: Pipeline) => item.target_schema || '—',
  },
  {
    id: 'updated',
    header: 'Last updated',
    cell: (item: Pipeline) => formatDate(item.updated_at),
    sortingField: 'updated_at',
  },
];

export default function PipelineListPage() {
  const navigate = useNavigate();
  const { showToast } = useToast();
  const [filters, setFilters] = useState<PipelineFilters>({ page: 1, pageSize: 20 });
  const [filterText, setFilterText] = useState('');
  const [statusFilter, setStatusFilter] = useState(STATUS_OPTIONS[0]);
  const [view, setView] = useState<'table' | 'cards'>('table');
  const [currentSort, setCurrentSort] = useState<'name' | 'activity' | 'files'>('activity');
  const [selectedItems, setSelectedItems] = useState<Pipeline[]>([]);
  const [confirmAction, setConfirmAction] = useState<'archive' | 'delete' | null>(null);

  const bulkArchive = useBulkArchive();
  const bulkDelete = useBulkDelete();
  const isExecuting = bulkArchive.isExecuting || bulkDelete.isExecuting;
  const progress = bulkArchive.progress || bulkDelete.progress;

  const currentFilters: PipelineFilters = {
    ...filters,
    search: filterText || undefined,
    status: statusFilter.value || undefined,
  };

  const { data, isLoading, isError, refetch } = usePipelines(currentFilters);

  const pipelines = data?.pipelines ?? [];
  const totalPages = data?.total_pages ?? 1;

  // Async_State_Triple (R1.4): a load failure must render an error + retry, not
  // the "No pipelines found." empty copy. Shared between the table and card views
  // (only one is mounted at a time, so the retry test id stays unique).
  const errorEmptyNode = (
    <Box textAlign="center" color="inherit">
      <SpaceBetween size="s">
        <span>Failed to load pipelines. Check your API connection.</span>
        <Box>
          <Button data-testid="pipelines-retry" onClick={() => refetch()}>Retry</Button>
        </Box>
      </SpaceBetween>
    </Box>
  );
  const emptyNode = <Box textAlign="center" color="inherit">No pipelines found.</Box>;

  const sortedPipelines = useMemo(
    () => sortPipelines(pipelines, currentSort),
    [pipelines, currentSort],
  );

  const handleBulkComplete = useCallback((result: BulkResult, action: string) => {
    if (result.failed.length === 0) {
      showToast(`Successfully ${action}d ${result.succeeded.length} pipeline${result.succeeded.length !== 1 ? 's' : ''}.`, 'success');
    } else if (result.succeeded.length === 0) {
      showToast(`Failed to ${action} all ${result.failed.length} pipelines.`, 'error');
    } else {
      const failedNames = result.failed.map((f) => f.name).join(', ');
      showToast(
        `${action}d ${result.succeeded.length} pipeline${result.succeeded.length !== 1 ? 's' : ''}. Failed: ${failedNames}`,
        'warning',
      );
    }
    setSelectedItems([]);
  }, [showToast]);

  const handleConfirm = useCallback(async () => {
    if (!confirmAction) return;
    setConfirmAction(null);
    if (confirmAction === 'archive') {
      const result = await bulkArchive.execute(selectedItems);
      handleBulkComplete(result, 'archive');
    } else {
      const result = await bulkDelete.execute(selectedItems);
      handleBulkComplete(result, 'delete');
    }
  }, [confirmAction, selectedItems, bulkArchive, bulkDelete, handleBulkComplete]);

  return (
    <SpaceBetween size="l">
      <StatsBar />

      <SpaceBetween direction="horizontal" size="s">
        <ViewToggle view={view} onChange={setView} />
        <PipelineSortControls currentSort={currentSort} onSortChange={setCurrentSort} />
      </SpaceBetween>

      {view === 'table' && selectedItems.length > 0 && (
        <BulkActionBar
          selectedItems={selectedItems}
          onArchive={() => setConfirmAction('archive')}
          onDelete={() => setConfirmAction('delete')}
          progress={progress}
          isExecuting={isExecuting}
        />
      )}

      {view === 'table' ? (
        <Table
          columnDefinitions={COLUMN_DEFINITIONS}
          items={sortedPipelines}
          loading={isLoading}
          loadingText="Loading pipelines"
          selectionType="multi"
          selectedItems={selectedItems}
          onSelectionChange={({ detail }) => {
            setSelectedItems(detail.selectedItems);
          }}
          trackBy="pipeline_key"
          header={
            <Header
              variant="h1"
              counter={data ? `(${data.total_count})` : undefined}
              actions={
                <Button variant="primary" onClick={() => navigate('/pipelines/new')}>
                  Create Pipeline
                </Button>
              }
            >
              Pipelines
            </Header>
          }
          filter={
            <SpaceBetween direction="horizontal" size="xs">
              <TextFilter
                filteringText={filterText}
                filteringPlaceholder="Search pipelines"
                onChange={({ detail }) => setFilterText(detail.filteringText)}
                onDelayedChange={({ detail }) => {
                  setFilters((f) => ({ ...f, page: 1 }));
                  setFilterText(detail.filteringText);
                }}
              />
              <Select
                selectedOption={statusFilter}
                onChange={({ detail }) => {
                  setStatusFilter(detail.selectedOption as typeof statusFilter);
                  setFilters((f) => ({ ...f, page: 1 }));
                }}
                options={STATUS_OPTIONS}
                placeholder="Filter by status"
              />
            </SpaceBetween>
          }
          pagination={
            <ListPagination
              currentPageIndex={filters.page ?? 1}
              pagesCount={totalPages}
              onChange={(pageIndex) => setFilters((f) => ({ ...f, page: pageIndex }))}
            />
          }
          empty={isError ? errorEmptyNode : emptyNode}
        />
      ) : (
        <SpaceBetween size="l">
          <Header
            variant="h1"
            counter={data ? `(${data.total_count})` : undefined}
            actions={
              <Button variant="primary" onClick={() => navigate('/pipelines/new')}>
                Create Pipeline
              </Button>
            }
          >
            Pipelines
          </Header>

          <SpaceBetween direction="horizontal" size="xs">
            <TextFilter
              filteringText={filterText}
              filteringPlaceholder="Search pipelines"
              onChange={({ detail }) => setFilterText(detail.filteringText)}
              onDelayedChange={({ detail }) => {
                setFilters((f) => ({ ...f, page: 1 }));
                setFilterText(detail.filteringText);
              }}
            />
            <Select
              selectedOption={statusFilter}
              onChange={({ detail }) => {
                setStatusFilter(detail.selectedOption as typeof statusFilter);
                setFilters((f) => ({ ...f, page: 1 }));
              }}
              options={STATUS_OPTIONS}
              placeholder="Filter by status"
            />
          </SpaceBetween>

          {isLoading ? (
            <Box textAlign="center">Loading pipelines...</Box>
          ) : sortedPipelines.length === 0 ? (
            isError ? errorEmptyNode : emptyNode
          ) : (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(340px, 1fr))', gap: '16px' }}>
              {sortedPipelines.map((p) => (
                <PipelineCard key={p.pipeline_key} pipeline={p} />
              ))}
            </div>
          )}

          <ListPagination
            currentPageIndex={filters.page ?? 1}
            pagesCount={totalPages}
            onChange={(pageIndex) => setFilters((f) => ({ ...f, page: pageIndex }))}
          />
        </SpaceBetween>
      )}

      <BulkConfirmModal
        visible={confirmAction !== null}
        action={confirmAction ?? 'archive'}
        pipelines={selectedItems}
        onConfirm={handleConfirm}
        onDismiss={() => setConfirmAction(null)}
      />
    </SpaceBetween>
  );
}
