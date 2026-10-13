// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Table from '@cloudscape-design/components/table';
import Modal from '@cloudscape-design/components/modal';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import { useArchivedPipelines, useRestorePipeline, useDeletePipeline } from '../hooks';
import { useToast } from '../contexts/ToastContext';
import { formatRelativeTime } from '../utils/formatting';
import type { Pipeline } from '../types';

export default function ArchivedPipelinesPage() {
  const { showToast } = useToast();
  const { data: pipelines, isLoading, isError } = useArchivedPipelines();
  const restoreMutation = useRestorePipeline();
  const deleteMutation = useDeletePipeline();

  // Double confirmation state for delete
  const [deleteTarget, setDeleteTarget] = useState<Pipeline | null>(null);
  const [deleteStep, setDeleteStep] = useState<1 | 2>(1);

  const handleRestore = async (pipeline: Pipeline) => {
    try {
      await restoreMutation.mutateAsync(pipeline.pipeline_key);
      showToast(`Pipeline "${pipeline.pipeline_name}" restored successfully`, 'success');
    } catch {
      showToast(`Failed to restore pipeline "${pipeline.pipeline_name}"`, 'error');
    }
  };

  const handleDeleteStart = (pipeline: Pipeline) => {
    setDeleteTarget(pipeline);
    setDeleteStep(1);
  };

  const handleDeleteConfirmFirst = () => {
    setDeleteStep(2);
  };

  const handleDeleteConfirmFinal = async () => {
    if (!deleteTarget) return;
    try {
      await deleteMutation.mutateAsync(deleteTarget.pipeline_key);
      showToast(`Pipeline "${deleteTarget.pipeline_name}" permanently deleted`, 'success');
    } catch {
      showToast(`Failed to delete pipeline "${deleteTarget.pipeline_name}"`, 'error');
    } finally {
      setDeleteTarget(null);
      setDeleteStep(1);
    }
  };

  const handleDeleteCancel = () => {
    setDeleteTarget(null);
    setDeleteStep(1);
  };

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" color="text-body-secondary">Loading archived pipelines...</Box>
      </Box>
    );
  }

  if (isError) {
    return (
      <Container>
        <Box textAlign="center" padding="l">
          <StatusIndicator type="error">Failed to load archived pipelines</StatusIndicator>
        </Box>
      </Container>
    );
  }

  return (
    <SpaceBetween size="l">
      <Header variant="h1">Archived Pipelines</Header>

      <div data-testid="archived-pipelines-list">
        <Table
          items={pipelines ?? []}
          columnDefinitions={[
            {
              id: 'name',
              header: 'Pipeline Name',
              cell: (item: Pipeline) => item.pipeline_name || item.pipeline_key,
            },
            {
              id: 'status',
              header: 'Status',
              cell: (item: Pipeline) => (
                <StatusIndicator type="stopped">{item.status}</StatusIndicator>
              ),
            },
            {
              id: 'updated_at',
              header: 'Archived',
              cell: (item: Pipeline) => formatRelativeTime(item.updated_at),
            },
            {
              id: 'actions',
              header: 'Actions',
              cell: (item: Pipeline) => (
                <SpaceBetween direction="horizontal" size="xs">
                  <Button
                    data-testid="restore-button"
                    onClick={() => handleRestore(item)}
                    loading={restoreMutation.isPending}
                  >
                    Restore
                  </Button>
                  <Button
                    data-testid="delete-button"
                    variant="link"
                    onClick={() => handleDeleteStart(item)}
                    loading={deleteMutation.isPending}
                  >
                    Delete
                  </Button>
                </SpaceBetween>
              ),
            },
          ]}
          empty={
            <Box textAlign="center" color="text-body-secondary" padding="l">
              No archived pipelines
            </Box>
          }
          trackBy={(item) => item.pipeline_key ?? item.pipeline_name ?? ''}
        />
      </div>

      {/* Double confirmation delete modal */}
      <Modal
        visible={!!deleteTarget}
        onDismiss={handleDeleteCancel}
        header={deleteStep === 1 ? 'Confirm Delete' : 'Final Confirmation'}
        footer={
          <Box float="right">
            <SpaceBetween direction="horizontal" size="xs">
              <Button variant="link" onClick={handleDeleteCancel}>Cancel</Button>
              {deleteStep === 1 ? (
                <Button variant="primary" onClick={handleDeleteConfirmFirst}>
                  Continue
                </Button>
              ) : (
                <Button
                  variant="primary"
                  onClick={handleDeleteConfirmFinal}
                  loading={deleteMutation.isPending}
                  data-testid="final-delete-button"
                >
                  Permanently Delete
                </Button>
              )}
            </SpaceBetween>
          </Box>
        }
      >
        {deleteStep === 1 ? (
          <Box>
            Are you sure you want to delete pipeline{' '}
            <strong>{deleteTarget?.pipeline_name}</strong>? This action cannot be undone.
          </Box>
        ) : (
          <Box>
            <StatusIndicator type="warning">
              This will permanently delete pipeline{' '}
              <strong>{deleteTarget?.pipeline_name}</strong> and all associated data.
              This action is irreversible.
            </StatusIndicator>
          </Box>
        )}
      </Modal>
    </SpaceBetween>
  );
}
