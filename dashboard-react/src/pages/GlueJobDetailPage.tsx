// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useQuery } from '@tanstack/react-query';
import Header from '@cloudscape-design/components/header';
import Container from '@cloudscape-design/components/container';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Table from '@cloudscape-design/components/table';
import Tabs from '@cloudscape-design/components/tabs';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import Spinner from '@cloudscape-design/components/spinner';
import Select from '@cloudscape-design/components/select';
import Modal from '@cloudscape-design/components/modal';
import Popover from '@cloudscape-design/components/popover';
import { useGlueJob, useRunGlueJob, useJobVersionHistory, useRollbackGlueJob } from '../hooks';
import { useToast } from '../contexts/ToastContext';
import { apiClient } from '../services/api-client';
import { formatRelativeTime, statusType } from '../utils/formatting';
import { colorBackgroundContainerContent, colorTextBodyDefault, colorBorderDividerDefault } from '@cloudscape-design/design-tokens';
import GlueJobLogViewer from '../components/GlueJobLogViewer';
import type { GlueJobRun } from '../types';

export default function GlueJobDetailPage() {
  const { id: pipelineId, jobName } = useParams<{ id: string; jobName: string }>();
  const navigate = useNavigate();
  const { showToast } = useToast();
  const { data: glueJob, isLoading, isError } = useGlueJob(jobName);
  const runGlueJob = useRunGlueJob();

  const [activeTabId, setActiveTabId] = useState('overview');
  const [runStatusFilter, setRunStatusFilter] = useState<string>('all');
  const [selectedScriptVersion, setSelectedScriptVersion] = useState<string | null>(null);

  // Rollback state
  const [showRollbackModal, setShowRollbackModal] = useState(false);
  const rollbackMutation = useRollbackGlueJob(pipelineId ?? '');
  const { data: versionHistory, isLoading: versionsLoading } = useJobVersionHistory(
    pipelineId ?? '',
    showRollbackModal && !!pipelineId,
  );

  const scriptVersions = glueJob?.script_versions ?? [];
  const canRollback = scriptVersions.length > 1;

  // Determine which version to fetch script for
  const activeVersion = selectedScriptVersion
    ?? (glueJob?.script_versions?.length
      ? (typeof glueJob.script_versions[0] === 'string'
        ? glueJob.script_versions[0]
        : glueJob.script_versions[0]?.version?.toString() ?? null)
      : null);

  // Fetch script content on-demand when Code tab is active
  const { data: scriptData, isLoading: scriptLoading } = useQuery({
    queryKey: ['glue-script', pipelineId, activeVersion],
    queryFn: async () => {
      const params = activeVersion ? `?version=${encodeURIComponent(activeVersion)}` : '';
      return apiClient.get<{ script_content: string; script_location: string }>(`/pipelines/${encodeURIComponent(pipelineId!)}/glue-job/script${params}`);
    },
    enabled: !!pipelineId && activeTabId === 'code',
    staleTime: 60_000,
    retry: false,
  });

  const scriptContent = scriptData?.script_content ?? glueJob?.script_content ?? '';

  const handleRunJob = async () => {
    if (!jobName) return;
    try {
      await runGlueJob.mutateAsync(jobName);
      showToast('Glue job started successfully', 'success');
    } catch {
      showToast('Failed to start Glue job', 'error');
    }
  };

  const handleDownloadScript = () => {
    if (!scriptContent) return;
    const blob = new Blob([scriptContent], { type: 'text/plain' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${jobName || 'script'}.py`;
    a.click();
    URL.revokeObjectURL(url);
  };

  const handleConfirmRollback = async () => {
    try {
      await rollbackMutation.mutateAsync();
      setShowRollbackModal(false);
      showToast('Rollback completed successfully', 'success');
    } catch (err: any) {
      showToast(err?.message ?? 'Rollback failed', 'error');
    }
  };

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
        <Box variant="p" color="text-body-secondary">Loading Glue job details...</Box>
      </Box>
    );
  }

  if (isError || !glueJob) {
    return (
      <Container>
        <Box textAlign="center" padding="l">
          <StatusIndicator type="error">Failed to load Glue job details</StatusIndicator>
          <Box padding={{ top: 's' }}>
            <Button onClick={() => navigate(pipelineId ? `/pipelines/${pipelineId}` : '/')}>
              Back to Pipeline
            </Button>
          </Box>
        </Box>
      </Container>
    );
  }

  const filteredRuns = runStatusFilter === 'all'
    ? (glueJob.runs ?? [])
    : (glueJob.runs ?? []).filter((r) => r.status?.toLowerCase() === runStatusFilter.toLowerCase());

  const recentRuns = (glueJob.runs ?? []).slice(0, 5);

  return (
    <SpaceBetween size="l">
      <Button variant="link" iconName="arrow-left" onClick={() => navigate(pipelineId ? `/pipelines/${pipelineId}` : '/')}>
        Back to Pipeline
      </Button>

      <Header
        variant="h1"
        actions={
          <SpaceBetween direction="horizontal" size="xs">
            <Button onClick={handleRunJob} loading={runGlueJob.isPending}>
              Run Job
            </Button>
            <Button iconName="download" onClick={handleDownloadScript} disabled={!scriptContent}>
              Download Script
            </Button>
          </SpaceBetween>
        }
      >
        Glue Job: {glueJob.job_name}
      </Header>

      <div data-testid="glue-job-detail">
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => setActiveTabId(detail.activeTabId)}
          tabs={[
            {
              id: 'overview',
              label: 'Overview',
              content: (
                <div data-testid="tab-overview">
                  <SpaceBetween size="l">
                    <Container header={<Header variant="h3">Job Metadata</Header>}>
                      <ColumnLayout columns={3} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Job Name</Box>
                          <Box data-testid="job-name">{glueJob.job_name}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Status</Box>
                          <StatusIndicator type={statusType(glueJob.status)} data-testid="job-status">
                            {glueJob.status}
                          </StatusIndicator>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Created</Box>
                          <Box>{formatRelativeTime(glueJob.created_at)}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Last Modified</Box>
                          <Box>{formatRelativeTime(glueJob.last_modified)}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Script Location</Box>
                          <Box>{glueJob.script_location}</Box>
                        </div>
                      </ColumnLayout>
                    </Container>

                    <Container header={<Header variant="h3">Worker Configuration</Header>}>
                      <ColumnLayout columns={3} variant="text-grid">
                        <div>
                          <Box variant="awsui-key-label">Worker Type</Box>
                          <Box data-testid="worker-type">{glueJob.worker_type}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Number of Workers</Box>
                          <Box data-testid="num-workers">{glueJob.number_of_workers}</Box>
                        </div>
                        <div>
                          <Box variant="awsui-key-label">Timeout (min)</Box>
                          <Box data-testid="timeout">{glueJob.timeout}</Box>
                        </div>
                      </ColumnLayout>
                    </Container>

                    {recentRuns.length > 0 && (
                      <Container header={<Header variant="h3">Recent Runs</Header>}>
                        <Table
                          items={recentRuns}
                          columnDefinitions={[
                            {
                              id: 'status', header: 'Status',
                              cell: (item: GlueJobRun) => (
                                <StatusIndicator type={statusType(item.status)}>{item.status}</StatusIndicator>
                              ),
                            },
                            { id: 'started', header: 'Started', cell: (item: GlueJobRun) => formatRelativeTime(item.started_at) },
                            {
                              id: 'duration', header: 'Duration',
                              cell: (item: GlueJobRun) => item.duration_seconds != null ? `${item.duration_seconds}s` : '—',
                            },
                          ]}
                          variant="embedded"
                        />
                      </Container>
                    )}
                  </SpaceBetween>
                </div>
              ),
            },
            {
              id: 'runs',
              label: 'Runs',
              content: (
                <div data-testid="tab-runs">
                  <SpaceBetween size="m">
                    <Select
                      selectedOption={{ value: runStatusFilter, label: runStatusFilter === 'all' ? 'All Statuses' : runStatusFilter }}
                      onChange={({ detail }) => setRunStatusFilter(detail.selectedOption.value ?? 'all')}
                      options={[
                        { value: 'all', label: 'All Statuses' },
                        { value: 'succeeded', label: 'Succeeded' },
                        { value: 'failed', label: 'Failed' },
                        { value: 'running', label: 'Running' },
                      ]}
                    />
                    <Table
                      items={filteredRuns}
                      columnDefinitions={[
                        {
                          id: 'run-id', header: 'Run ID', width: 200,
                          cell: (item: GlueJobRun) => item.run_id,
                        },
                        {
                          id: 'status', header: 'Status', width: 120,
                          cell: (item: GlueJobRun) => (
                            <StatusIndicator type={statusType(item.status)}>{item.status}</StatusIndicator>
                          ),
                        },
                        { id: 'started', header: 'Started', width: 150, cell: (item: GlueJobRun) => formatRelativeTime(item.started_at) },
                        {
                          id: 'duration', header: 'Duration', width: 100,
                          cell: (item: GlueJobRun) => item.duration_seconds != null ? `${item.duration_seconds}s` : '—',
                        },
                        {
                          id: 'error', header: 'Error',
                          cell: (item: GlueJobRun) => (
                            <Box color={item.error_message ? 'text-status-error' : 'text-body-secondary'}>
                              {item.error_message || '—'}
                            </Box>
                          ),
                        },
                      ]}
                      empty={<Box textAlign="center" color="text-body-secondary">No runs found</Box>}
                    />
                  </SpaceBetween>
                </div>
              ),
            },
            {
              id: 'configuration',
              label: 'Configuration',
              content: (
                <div data-testid="tab-configuration">
                  <SpaceBetween size="m">
                    <Container
                      header={
                        <Header
                          variant="h3"
                          description={
                            <span data-testid="config-read-only-note">
                              Read-only. These values are read from the deployed Glue job definition
                              and cannot be edited here.
                            </span>
                          }
                        >
                          Job Parameters
                        </Header>
                      }
                    >
                      <SpaceBetween size="s">
                        <ColumnLayout columns={2} variant="text-grid">
                          <div>
                            <Box variant="awsui-key-label">Worker Type</Box>
                            <Box data-testid="config-worker-type">{glueJob.worker_type}</Box>
                          </div>
                          <div>
                            <Box variant="awsui-key-label">Number of Workers</Box>
                            <Box data-testid="config-num-workers">{glueJob.number_of_workers}</Box>
                          </div>
                          <div>
                            <Box variant="awsui-key-label">Timeout</Box>
                            <Box data-testid="config-timeout">{glueJob.timeout} min</Box>
                          </div>
                          <div>
                            <Box variant="awsui-key-label">Script Location</Box>
                            <Box data-testid="config-script-location">{glueJob.script_location}</Box>
                          </div>
                        </ColumnLayout>

                        {Object.keys(glueJob.configuration ?? {}).length > 0 && (
                          <div>
                            <Box variant="awsui-key-label">Custom Parameters</Box>
                            <SpaceBetween size="xs">
                              {Object.entries(glueJob.configuration ?? {}).map(([key, value]) => (
                                <div key={key} data-testid="config-param" style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
                                  <Badge>{key}</Badge>
                                  <Box>{value}</Box>
                                </div>
                              ))}
                            </SpaceBetween>
                          </div>
                        )}
                      </SpaceBetween>
                    </Container>
                  </SpaceBetween>
                </div>
              ),
            },
            {
              id: 'code',
              label: 'Code',
              content: (
                <div data-testid="tab-code">
                  <SpaceBetween size="m">
                    {glueJob.script_versions && glueJob.script_versions.length > 0 && (
                      <div style={{ display: 'flex', gap: 8, alignItems: 'flex-start' }}>
                        <div style={{ flex: 1 }}>
                          <Select
                            selectedOption={
                              activeVersion
                                ? (() => {
                                    const v = glueJob.script_versions.find((sv: any) =>
                                      (typeof sv === 'string' ? sv : sv?.version?.toString()) === activeVersion
                                    );
                                    const ver = typeof v === 'object' ? v?.version : v;
                                    const ts = typeof v === 'object' && v?.timestamp
                                      ? new Date(v.timestamp).toLocaleDateString()
                                      : '';
                                    const isLatest = v !== undefined && glueJob.script_versions.indexOf(v) === 0;
                                    return {
                                      value: activeVersion,
                                      label: `Version ${ver ?? activeVersion}${ts ? ` — ${ts}` : ''}${isLatest ? ' (latest)' : ''}`,
                                    };
                                  })()
                                : null
                            }
                            onChange={({ detail }) => setSelectedScriptVersion(detail.selectedOption.value ?? null)}
                            options={glueJob.script_versions.map((v: any, i: number) => {
                              const ver = typeof v === 'string' ? v : v?.version ?? v?.code_version_id ?? '';
                              const verLabel = typeof v === 'object' && v?.version ? `Version ${v.version}` : `${ver}`;
                              const ts = typeof v === 'object' && v?.timestamp
                                ? ` — ${new Date(v.timestamp).toLocaleDateString()}`
                                : '';
                              return {
                                value: typeof v === 'string' ? v : v?.version?.toString() ?? v?.code_version_id ?? '',
                                label: `${verLabel}${ts}${i === 0 ? ' (latest)' : ''}`,
                              };
                            })}
                            placeholder="Select script version"
                            data-testid="script-version-select"
                          />
                        </div>
                        {canRollback ? (
                          <Button
                            data-testid="rollback-button"
                            onClick={() => setShowRollbackModal(true)}
                            loading={rollbackMutation.isPending}
                            disabled={rollbackMutation.isPending}
                          >
                            Rollback
                          </Button>
                        ) : (
                          <Popover
                            dismissButton={false}
                            position="top"
                            size="small"
                            triggerType="custom"
                            content="No previous version available"
                          >
                            <Button
                              data-testid="rollback-button"
                              disabled
                            >
                              Rollback
                            </Button>
                          </Popover>
                        )}
                      </div>
                    )}
                    <Container header={<Header variant="h3">Job Script</Header>}>
                      {scriptContent ? (
                        <pre
                          data-testid="script-content"
                          style={{
                            background: colorBackgroundContainerContent,
                            color: colorTextBodyDefault,
                            border: `1px solid ${colorBorderDividerDefault}`,
                            padding: 16,
                            borderRadius: 4,
                            fontSize: 13,
                            overflow: 'auto',
                            maxHeight: 500,
                            whiteSpace: 'pre-wrap',
                          }}
                        >
                          {scriptContent}
                        </pre>
                      ) : scriptLoading ? (
                        <Box textAlign="center" padding="l"><Spinner /> Loading script...</Box>
                      ) : (
                        <Box color="text-body-secondary">No script content available</Box>
                      )}
                    </Container>
                    <Button iconName="download" onClick={handleDownloadScript} disabled={!scriptContent}>
                      Download Script
                    </Button>
                  </SpaceBetween>

                  <Modal
                    visible={showRollbackModal}
                    onDismiss={() => setShowRollbackModal(false)}
                    header="Confirm Rollback"
                    data-testid="rollback-modal"
                    footer={
                      <Box float="right">
                        <SpaceBetween direction="horizontal" size="xs">
                          <Button
                            variant="link"
                            onClick={() => setShowRollbackModal(false)}
                            data-testid="rollback-cancel"
                          >
                            Cancel
                          </Button>
                          <Button
                            variant="primary"
                            onClick={handleConfirmRollback}
                            loading={rollbackMutation.isPending}
                            data-testid="rollback-confirm"
                          >
                            Confirm Rollback
                          </Button>
                        </SpaceBetween>
                      </Box>
                    }
                  >
                    {versionsLoading ? (
                      <Box textAlign="center" padding="l"><Spinner /> Loading version history...</Box>
                    ) : (
                      <SpaceBetween size="m">
                        <Box>
                          <Box variant="awsui-key-label">Current Version</Box>
                          <Box data-testid="current-version-info">
                            {versionHistory?.versions?.[0]
                              ? `Version ${versionHistory.versions[0].version} — ${new Date(versionHistory.versions[0].timestamp).toLocaleString()}`
                              : `Version ${typeof scriptVersions[0] === 'object' ? scriptVersions[0]?.version : scriptVersions[0]}`}
                          </Box>
                        </Box>
                        <Box>
                          <Box variant="awsui-key-label">Rollback To</Box>
                          <Box data-testid="previous-version-info">
                            {versionHistory?.versions?.[1]
                              ? `Version ${versionHistory.versions[1].version} — ${new Date(versionHistory.versions[1].timestamp).toLocaleString()}`
                              : `Version ${typeof scriptVersions[1] === 'object' ? scriptVersions[1]?.version : scriptVersions[1]}`}
                          </Box>
                        </Box>
                      </SpaceBetween>
                    )}
                  </Modal>
                </div>
              ),
            },
            {
              id: 'logs',
              label: 'Logs',
              content: (
                <div data-testid="tab-logs">
                  <GlueJobLogViewer pipelineId={pipelineId!} runs={glueJob.runs ?? []} />
                </div>
              ),
            },
          ]}
        />
      </div>
    </SpaceBetween>
  );
}
