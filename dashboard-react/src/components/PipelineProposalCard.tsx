// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Table from '@cloudscape-design/components/table';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import type { PipelineProposal } from '../hooks/use-chat-agent';

export interface PipelineProposalCardProps {
  proposal: PipelineProposal;
  onCreatePipeline?: (proposal: PipelineProposal) => void;
  isCreating?: boolean;
}

export default function PipelineProposalCard({ proposal, onCreatePipeline, isCreating }: PipelineProposalCardProps) {
  return (
    <Container
      header={
        <Header
          variant="h3"
          actions={
            onCreatePipeline && (
              <Button variant="primary" onClick={() => onCreatePipeline(proposal)} loading={isCreating}>
                Create Pipeline
              </Button>
            )
          }
        >
          Pipeline Proposal: {proposal.pipeline_name}
        </Header>
      }
      data-testid="pipeline-proposal-card"
    >
      <SpaceBetween size="m">
        <ColumnLayout columns={2} variant="text-grid">
          <div>
            <Box variant="awsui-key-label">Source</Box>
            <Box>{proposal.source.type} — {proposal.source.location}</Box>
            <Box variant="small" color="text-body-secondary">Format: {proposal.source.format}</Box>
          </div>
          <div>
            <Box variant="awsui-key-label">Target</Box>
            <Box>{proposal.target.type} — {proposal.target.table_name}</Box>
            {proposal.target.partitioning.length > 0 && (
              <Box variant="small" color="text-body-secondary">
                Partitioned by: {proposal.target.partitioning.join(', ')}
              </Box>
            )}
          </div>
        </ColumnLayout>

        <Table
          header={<Header variant="h3">Schema Fields</Header>}
          columnDefinitions={[
            { id: 'name', header: 'Field Name', cell: (item) => item.name },
            { id: 'type', header: 'Type', cell: (item) => item.type },
          ]}
          items={proposal.schema.fields}
          variant="embedded"
        />

        <Table
          header={<Header variant="h3">Mappings</Header>}
          columnDefinitions={[
            { id: 'source', header: 'Source Field', cell: (item) => item.source },
            { id: 'target', header: 'Target Field', cell: (item) => item.target },
            { id: 'confidence', header: 'Confidence', cell: (item) => `${(item.confidence * 100).toFixed(0)}%` },
          ]}
          items={proposal.mappings}
          variant="embedded"
        />
      </SpaceBetween>
    </Container>
  );
}
