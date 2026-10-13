// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Modal from '@cloudscape-design/components/modal';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Link from '@cloudscape-design/components/link';
import Header from '@cloudscape-design/components/header';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Button from '@cloudscape-design/components/button';

interface HelpModalProps {
  visible: boolean;
  onDismiss: () => void;
}

const NAV_LINKS = [
  { text: 'Pipelines', href: '/', description: 'View and manage all ETL pipelines' },
  { text: 'Approvals', href: '/approvals', description: 'Review pending approval requests' },
  { text: 'Schemas', href: '/schemas', description: 'Manage target schemas' },
  { text: 'Archived', href: '/archived', description: 'View and restore archived pipelines' },
  { text: 'Metrics', href: '/metrics', description: 'CloudWatch metrics dashboard' },
  { text: 'Errors', href: '/errors', description: 'Recent pipeline errors and classifications' },
];

const DOC_LINKS = [
  { text: 'AWS Glue Documentation', href: 'https://docs.aws.amazon.com/glue/latest/dg/what-is-glue.html' },
  { text: 'Bedrock AgentCore', href: 'https://docs.aws.amazon.com/bedrock/latest/userguide/agentcore.html' },
  { text: 'Cloudscape Design System', href: 'https://cloudscape.design/' },
  { text: 'Strands Agents SDK', href: 'https://strandsagents.com/' },
];

export default function HelpModal({ visible, onDismiss }: HelpModalProps) {
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header="Help & Documentation"
      size="large"
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>Close</Button>
        </Box>
      }
    >
      <SpaceBetween size="l">
        {/* Navigation Quick Links */}
        <div data-testid="help-doc-links">
          <Header variant="h3">Dashboard Pages</Header>
          <ColumnLayout columns={2}>
            {NAV_LINKS.map((link) => (
              <div key={link.text} data-testid="help-link" style={{ marginBottom: 8 }}>
                <div><Link href={link.href}>{link.text}</Link></div>
                <div style={{ fontSize: 12, color: 'var(--color-text-body-secondary)' }}>{link.description}</div>
              </div>
            ))}
          </ColumnLayout>
        </div>

        {/* Pipeline Features */}
        <div data-testid="help-feature-summary">
          <Header variant="h3">Building & Managing Pipelines</Header>
          <SpaceBetween size="xs">
            <Box variant="p">
              Create a pipeline with the guided <Link href="/pipelines/new">Create Pipeline</Link> wizard
              (Name &amp; Description, Source, Target Destination, Target Schema, AI Model &amp; Options, Review &amp; Create),
              or open the <Link href="/chat">Chat</Link> builder to describe a pipeline in plain English and create it from the proposal.
            </Box>
            <Box variant="p">
              Sources can be uploaded files (CSV, JSON, Parquet) or JDBC databases. Targets include S3 Tables (Iceberg),
              the Glue Data Catalog, JDBC databases, and S3 Parquet.
            </Box>
            <Box variant="p">
              Each pipeline has tabs for Overview, Schema history, Code versions, Executions, Files, and Mapping Memory.
              Open any run to see Step Flow, AI Traces, Approvals, Schema &amp; Mappings, Code, and Data Quality.
            </Box>
            <Box variant="p">
              From the pipeline overview you can upload files, edit settings, view data previews, and manage approvals inline.
              The Glue Job detail page shows job metadata, run history, script code with version rollback, configuration,
              and live CloudWatch log streaming.
            </Box>
          </SpaceBetween>
        </div>

        {/* Key Capabilities */}
        <div>
          <Header variant="h3">Key Capabilities</Header>
          <ul style={{ margin: 0, paddingLeft: 20 }}>
            <li>Natural-language pipeline builder (Chat) plus a guided 6-step creation wizard</li>
            <li>AI-driven schema mapping that reuses approved mappings via AgentCore Memory</li>
            <li>Multiple sources (file upload, JDBC) and targets (S3 Tables/Iceberg, JDBC, S3 Parquet)</li>
            <li>Automated PySpark code generation and testing</li>
            <li>Human-in-the-loop approval gates for schema mappings and generated code</li>
            <li>Self-healing auto-retry and retry-from-step for failed runs</li>
            <li>Automated data quality checks per run</li>
            <li>AI trace and token-usage observability for every run</li>
            <li>Real-time execution monitoring via WebSocket</li>
            <li>Glue job log streaming with keyword filtering</li>
            <li>Mapping regeneration with custom context and model selection</li>
            <li>Optional scheduled runs</li>
            <li>Cost tracking per run with an aggregate metrics dashboard</li>
            <li>Bulk operations and archive / restore for pipelines</li>
          </ul>
        </div>

        {/* Keyboard Shortcuts */}
        <div data-testid="help-getting-help">
          <Header variant="h3">Keyboard Shortcuts</Header>
          <SpaceBetween size="xs">
            <Box variant="p">
              <strong>Ctrl/Cmd+K</strong> — Focus search &nbsp;&nbsp;
              <strong>?</strong> — Open this help dialog
            </Box>
          </SpaceBetween>
        </div>

        {/* External Documentation */}
        <div>
          <Header variant="h3">External Documentation</Header>
          <ColumnLayout columns={2}>
            {DOC_LINKS.map((link) => (
              <div key={link.text} data-testid="help-link">
                <Link href={link.href} external>{link.text}</Link>
              </div>
            ))}
          </ColumnLayout>
        </div>
      </SpaceBetween>
    </Modal>
  );
}
