// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useMemo, useCallback, useEffect } from 'react';
import { Routes, Route } from 'react-router-dom';
import AppLayout from '@cloudscape-design/components/app-layout';
import SideNavigation from '@cloudscape-design/components/side-navigation';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Toggle from '@cloudscape-design/components/toggle';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import { useNavigate, useLocation } from 'react-router-dom';
import { applyMode, Mode } from '@cloudscape-design/global-styles';
import { useAuth } from 'react-oidc-context';
import { AuthGuard, AuthCallback } from './auth';
import { apiClient } from './services/api-client';
import PipelineListPage from './pages/PipelineListPage';
import PipelineLayout from './pages/PipelineLayout';
import PipelineOverviewPage from './pages/PipelineOverviewPage';
import SchemaHistoryPage from './pages/SchemaHistoryPage';
import CodePage from './pages/CodePage';
import ExecutionsPage from './pages/ExecutionsPage';
import FilesPage from './pages/FilesPage';
import RunDetailPage from './pages/RunDetailPage';
import GlueJobDetailPage from './pages/GlueJobDetailPage';
import ApprovalsPage from './pages/ApprovalsPage';
import SchemaManagementPage from './pages/SchemaManagementPage';
import ArchivedPipelinesPage from './pages/ArchivedPipelinesPage';
import MetricsDashboardPage from './pages/MetricsDashboardPage';
import ErrorsPage from './pages/ErrorsPage';
import CreatePipelinePage from './pages/CreatePipelinePage';
import MappingMemoryPage from './pages/MappingMemoryPage';
import ChatPage from './pages/ChatPage';
import HelpModal from './components/HelpModal';
import NotificationBell from './components/NotificationBell';
import { ToastProvider, useToast } from './contexts/ToastContext';
import { useKeyboardShortcuts } from './hooks';
import { setGlobalToast } from './services/global-toast';

const NAV_ITEMS = [
  { type: 'link' as const, text: 'Pipelines', href: '/' },
  { type: 'link' as const, text: 'Approvals', href: '/approvals' },
  { type: 'link' as const, text: 'Schemas', href: '/schemas' },
  { type: 'link' as const, text: 'Archived', href: '/archived' },
  { type: 'link' as const, text: 'Metrics', href: '/metrics' },
  { type: 'link' as const, text: 'Errors', href: '/errors' },
  { type: 'link' as const, text: 'Help', href: '#help' },
];

/** Registers the ToastContext's showToast with the global toast bridge so
 *  QueryCache.onError (in main.tsx) can display toasts without React context. */
function GlobalToastBridge() {
  const { showToast } = useToast();
  useEffect(() => { setGlobalToast(showToast); }, [showToast]);
  return null;
}

/** Wires the OIDC ID token into the framework-agnostic ApiClient singleton
 *  so every outgoing fetch carries the Authorization header. The REST API's
 *  API Gateway Cognito authorizer validates the ID token (the token API Gateway
 *  accepts when no authorization scopes are configured on the methods), not the
 *  access token — sending the access token yields 401 UnauthorizedException. */
function ApiTokenBridge() {
  const auth = useAuth();
  useEffect(() => {
    apiClient.setTokenAccessor(() => auth.user?.id_token ?? null);
    // On a 401 (expired/invalid session) the ApiClient triggers this once to
    // re-authenticate via the Cognito Hosted UI, instead of surfacing a toast
    // per failed request.
    apiClient.setUnauthorizedHandler(() => {
      void auth.signinRedirect();
    });
  }, [auth]);
  return null;
}

export default function App() {
  const navigate = useNavigate();
  const location = useLocation();
  const auth = useAuth();
  const [helpVisible, setHelpVisible] = useState(false);
  const [darkMode, setDarkMode] = useState(() => localStorage.getItem('agentic-data-ingestion-theme') === 'dark');

  const toggleDarkMode = useCallback((checked: boolean) => {
    setDarkMode(checked);
    applyMode(checked ? Mode.Dark : Mode.Light);
    localStorage.setItem('agentic-data-ingestion-theme', checked ? 'dark' : 'light');
  }, []);

  const handleSignOut = useCallback(() => {
    auth.signoutRedirect();
  }, [auth]);

  // Keyboard shortcuts — stable reference via useMemo
  const shortcuts = useMemo(() => ({
    'mod+k': () => {
      // Focus the first search input on the page
      const searchInput = document.querySelector<HTMLInputElement>('input[type="search"], input[placeholder*="earch"]');
      if (searchInput) searchInput.focus();
    },
    '?': () => setHelpVisible(true),
  }), []);

  useKeyboardShortcuts(shortcuts);

  return (
    <ToastProvider>
    <GlobalToastBridge />
    <ApiTokenBridge />
    <Routes>
      {/* Public route — OIDC callback (no auth guard) */}
      <Route path="/auth/callback" element={<AuthCallback />} />

      {/* All other routes are protected */}
      <Route path="*" element={
        <AuthGuard>
          <AppLayout
            navigation={
              <div>
                <SideNavigation
                  header={{ text: 'Agentic Data Ingestion', href: '/' }}
                  activeHref={location.pathname}
                  items={NAV_ITEMS}
                  onFollow={(e) => {
                    e.preventDefault();
                    if (e.detail.href === '#help') {
                      setHelpVisible(true);
                    } else {
                      navigate(e.detail.href);
                    }
                  }}
                />
                <Box padding={{ horizontal: 'l', vertical: 's' }}>
                  <SpaceBetween size="s" direction="horizontal" alignItems="center">
                    <Toggle checked={darkMode} onChange={({ detail }) => toggleDarkMode(detail.checked)}>
                      Dark mode
                    </Toggle>
                    <NotificationBell />
                  </SpaceBetween>
                </Box>
                <Box padding={{ horizontal: 'l', vertical: 'xs' }}>
                  <Button variant="link" onClick={handleSignOut}>
                    Sign out
                  </Button>
                </Box>
              </div>
            }
            content={
              <Routes>
                <Route path="/" element={<PipelineListPage />} />
                <Route path="/pipelines/new" element={<CreatePipelinePage />} />
                <Route path="/pipelines/:id" element={<PipelineLayout />}>
                  <Route index element={<PipelineOverviewPage />} />
                  <Route path="schema" element={<SchemaHistoryPage />} />
                  <Route path="code" element={<CodePage />} />
                  <Route path="executions" element={<ExecutionsPage />} />
                  <Route path="files" element={<FilesPage />} />
                  <Route path="mapping-memory" element={<MappingMemoryPage />} />
                </Route>
                <Route path="/pipelines/:id/runs/:runId" element={<RunDetailPage />} />
                <Route path="/pipelines/:id/glue-job/:jobName" element={<GlueJobDetailPage />} />
                <Route path="/approvals" element={<ApprovalsPage />} />
                <Route path="/schemas" element={<SchemaManagementPage />} />
                <Route path="/archived" element={<ArchivedPipelinesPage />} />
                <Route path="/metrics" element={<MetricsDashboardPage />} />
                <Route path="/errors" element={<ErrorsPage />} />
                <Route path="/chat" element={<ChatPage />} />
              </Routes>
            }
            toolsHide
          />
          <HelpModal visible={helpVisible} onDismiss={() => setHelpVisible(false)} />
        </AuthGuard>
      } />
    </Routes>
    </ToastProvider>
  );
}
