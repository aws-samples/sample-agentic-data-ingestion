// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useParams, useNavigate, useLocation, Outlet } from 'react-router-dom';
import Tabs from '@cloudscape-design/components/tabs';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Spinner from '@cloudscape-design/components/spinner';
import Box from '@cloudscape-design/components/box';
import StatusIndicator from '@cloudscape-design/components/status-indicator';
import { usePipelineDetail } from '../hooks';
import { PipelineProvider } from '../contexts/PipelineContext';

const TAB_ROUTES = [
  { id: 'overview', label: 'Overview', href: '' },
  { id: 'schema', label: 'Schema', href: '/schema' },
  { id: 'code', label: 'Code', href: '/code' },
  { id: 'executions', label: 'Executions', href: '/executions' },
  { id: 'files', label: 'Files', href: '/files' },
  { id: 'mapping-memory', label: 'Mapping Memory', href: '/mapping-memory' },
];

export default function PipelineLayout() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const location = useLocation();
  const { data: pipeline, isLoading, isError, refetch } = usePipelineDetail(id ?? '');

  // Derive active tab from URL suffix
  const basePath = `/pipelines/${id}`;
  const suffix = location.pathname.replace(basePath, '') || '';
  const activeTabId = TAB_ROUTES.find((t) => t.href === suffix)?.id ?? 'overview';

  if (isLoading) {
    return (
      <Box textAlign="center" padding="xxl">
        <Spinner size="large" />
      </Box>
    );
  }

  if (isError || !pipeline) {
    return <StatusIndicator type="error">Failed to load pipeline</StatusIndicator>;
  }

  return (
    <PipelineProvider value={{ pipeline, isLoading, isError, refetch }}>
      <SpaceBetween size="l">
        <Button variant="link" iconName="arrow-left" onClick={() => navigate('/')}>
          Back to Pipelines
        </Button>
        <Header variant="h1">{pipeline.pipeline_name || id}</Header>
        <Tabs
          activeTabId={activeTabId}
          onChange={({ detail }) => {
            const tab = TAB_ROUTES.find((t) => t.id === detail.activeTabId);
            if (tab) navigate(`${basePath}${tab.href}`);
          }}
          tabs={TAB_ROUTES.map((t) => ({ id: t.id, label: t.label }))}
        />
        <Outlet />
      </SpaceBetween>
    </PipelineProvider>
  );
}
