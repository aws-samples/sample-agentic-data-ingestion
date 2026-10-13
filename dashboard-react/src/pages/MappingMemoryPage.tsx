// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo } from 'react';
import { useParams } from 'react-router-dom';
import Table from '@cloudscape-design/components/table';
import Header from '@cloudscape-design/components/header';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import { useMappingMemory, useDeleteMappingMemoryEntry } from '../hooks';
import { useToast } from '../contexts/ToastContext';
import type { MappingMemoryEntry } from '../types';

export default function MappingMemoryPage() {
  const { id: pipelineId } = useParams<{ id: string }>();
  const { data, isLoading, isError } = useMappingMemory(pipelineId ?? '');
  const deleteMutation = useDeleteMappingMemoryEntry(pipelineId ?? '');
  const { showToast } = useToast();

  const [deleteTarget, setDeleteTarget] = useState<MappingMemoryEntry | null>(null);
  const [selectedItems, setSelectedItems] = useState<MappingMemoryEntry[]>([]);
  const [bulkDeleteVisible, setBulkDeleteVisible] = useState(false);
  const [bulkDeleting, setBulkDeleting] = useState(false);

  const sortedEntries = useMemo(() => {
    const entries = data?.entries ?? [];
    return [...entries].sort((a, b) => b.confidence - a.confidence);
  }, [data?.entries]);

  const handleDelete = async () => {
    if (!deleteTarget) return;
    try {
      await deleteMutation.mutateAsync(deleteTarget.entry_id);
      showToast(
        `Deleted mapping: ${deleteTarget.source_field} → ${deleteTarget.target_field}`,
        'success',
      );
      setSelectedItems((prev) => prev.filter((i) => i.entry_id !== deleteTarget.entry_id));
    } catch (err) {
      showToast(err instanceof Error ? err.message : 'Failed to delete entry', 'error');
    } finally {
      setDeleteTarget(null);
    }
  };

  const handleBulkDelete = async () => {
    setBulkDeleting(true);
    let succeeded = 0;
    let failed = 0;
    for (const item of selectedItems) {
      try {
        await deleteMutation.mutateAsync(item.entry_id);
        succeeded++;
      } catch {
        failed++;
      }
    }
    setBulkDeleting(false);
    setBulkDeleteVisible(false);
    setSelectedItems([]);
    if (failed === 0) {
      showToast(`Deleted ${succeeded} mapping entries`, 'success');
    } else {
      showToast(`Deleted ${succeeded}, failed ${failed}`, 'error');
    }
  };

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (isError) {
    return <StatusIndicator type="error">Failed to load mapping memory</StatusIndicator>;
  }

  const memoryEnabled = (data as any)?.memory_enabled !== false;
  const disabledMessage = (data as any)?.message;

  return (
    <>
      {!memoryEnabled && (
        <Box padding={{ bottom: 's' }}>
          <StatusIndicator type="warning">
            {disabledMessage || 'Mapping memory is not configured for this deployment.'}
          </StatusIndicator>
        </Box>
      )}
      <Table
        header={
          <Header
            counter={`(${sortedEntries.length})`}
            actions={
              <Button
                disabled={selectedItems.length === 0}
                onClick={() => setBulkDeleteVisible(true)}
                data-testid="bulk-delete-button"
              >
                Delete selected ({selectedItems.length})
              </Button>
            }
          >
            Mapping Memory
          </Header>
        }
        items={sortedEntries}
        selectionType="multi"
        selectedItems={selectedItems}
        onSelectionChange={({ detail }) =>
          setSelectedItems(detail.selectedItems as MappingMemoryEntry[])
        }
        trackBy="entry_id"
        columnDefinitions={[
          {
            id: 'source_field',
            header: 'Source Field',
            cell: (item: MappingMemoryEntry) => item.source_field,
            sortingField: 'source_field',
          },
          {
            id: 'target_field',
            header: 'Target Field',
            cell: (item: MappingMemoryEntry) => item.target_field,
            sortingField: 'target_field',
          },
          {
            id: 'confidence',
            header: 'Confidence',
            cell: (item: MappingMemoryEntry) => (
              <Badge
                color={
                  item.confidence >= 80
                    ? 'green'
                    : item.confidence >= 50
                      ? 'blue'
                      : 'grey'
                }
              >
                {item.confidence}%
              </Badge>
            ),
            sortingField: 'confidence',
          },
          {
            id: 'approved',
            header: 'Status',
            cell: (item: MappingMemoryEntry) =>
              item.approved ? (
                <StatusIndicator type="success">Approved</StatusIndicator>
              ) : (
                <StatusIndicator type="pending">Pending</StatusIndicator>
              ),
          },
          {
            id: 'timestamp',
            header: 'Timestamp',
            cell: (item: MappingMemoryEntry) =>
              item.timestamp ? new Date(item.timestamp).toLocaleString() : '—',
            sortingField: 'timestamp',
          },
          {
            id: 'actions',
            header: 'Actions',
            cell: (item: MappingMemoryEntry) => (
              <Button
                variant="icon"
                iconName="remove"
                ariaLabel={`Delete mapping ${item.source_field} to ${item.target_field}`}
                onClick={() => setDeleteTarget(item)}
                data-testid={`delete-${item.entry_id}`}
              />
            ),
          },
        ]}
        empty={
          <Box textAlign="center" padding="l">
            <SpaceBetween size="s">
              <b>No mapping memory entries</b>
              <Box variant="p" color="text-body-secondary">
                No AI-learned mapping patterns exist for this pipeline yet. Mapping
                memory entries are created when the AI generates field mappings.
              </Box>
            </SpaceBetween>
          </Box>
        }
        data-testid="mapping-memory-table"
      />

      {/* Single delete confirmation */}
      <Modal
        visible={deleteTarget !== null}
        onDismiss={() => setDeleteTarget(null)}
        header="Delete mapping memory entry"
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setDeleteTarget(null)} data-testid="delete-cancel">
                Cancel
              </Button>
              <Button variant="primary" onClick={handleDelete} loading={deleteMutation.isPending} data-testid="delete-confirm">
                Delete
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        {deleteTarget && (
          <Box>
            Are you sure you want to delete the mapping from{' '}
            <b>{deleteTarget.source_field}</b> to <b>{deleteTarget.target_field}</b>?
            This will remove it from future mapping suggestions.
          </Box>
        )}
      </Modal>

      {/* Bulk delete confirmation */}
      <Modal
        visible={bulkDeleteVisible}
        onDismiss={() => setBulkDeleteVisible(false)}
        header={`Delete ${selectedItems.length} mapping entries`}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={() => setBulkDeleteVisible(false)}>
                Cancel
              </Button>
              <Button variant="primary" onClick={handleBulkDelete} loading={bulkDeleting} data-testid="bulk-delete-confirm">
                Delete {selectedItems.length} entries
              </Button>
            </SpaceBetween>
          </Box>
        }
      >
        <Box>
          Are you sure you want to delete {selectedItems.length} mapping memory entries?
          This will remove them from future mapping suggestions.
        </Box>
      </Modal>
    </>
  );
}
