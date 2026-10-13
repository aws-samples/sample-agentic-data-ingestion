// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useRef, useEffect, useCallback } from 'react';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import Input from '@cloudscape-design/components/input';
import Spinner from '@cloudscape-design/components/spinner';
import { colorBackgroundContainerContent, colorBackgroundItemSelected, colorTextBodyDefault } from '@cloudscape-design/design-tokens';
import { useChatAgent } from '../hooks/use-chat-agent';
import type { PipelineProposal } from '../hooks/use-chat-agent';
import PipelineProposalCard from '../components/PipelineProposalCard';
import { apiClient } from '../services/api-client';

export default function ChatPage() {
  const { messages, sendMessage, isLoading } = useChatAgent();
  const [input, setInput] = useState('');
  const [isCreating, setIsCreating] = useState(false);
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  const handleSend = useCallback(() => {
    const trimmed = input.trim();
    if (!trimmed || isLoading) return;
    setInput('');
    sendMessage(trimmed);
  }, [input, isLoading, sendMessage]);

  const handleCreatePipeline = useCallback(async (proposal: PipelineProposal) => {
    setIsCreating(true);
    try {
      await apiClient.post('/pipelines', {
        pipeline_name: proposal.pipeline_name,
        source: proposal.source,
        target: proposal.target,
        schema: proposal.schema,
        mappings: proposal.mappings,
      });
    } finally {
      setIsCreating(false);
    }
  }, []);

  return (
    <SpaceBetween size="l">
      <Header variant="h1">Chat — Pipeline Builder</Header>

      <Container>
        <div style={{ minHeight: 400, maxHeight: 600, overflowY: 'auto', padding: '8px 0' }}>
          <SpaceBetween size="s">
            {messages.length === 0 && (
              <Box color="text-body-secondary" textAlign="center" padding="l">
                Describe a pipeline in plain English. For example: "Load CSV files from s3://my-bucket/data/ into a customers table partitioned by region."
              </Box>
            )}
            {messages.map((msg, i) => (
              <div key={i} style={{ textAlign: msg.role === 'user' ? 'right' : 'left' }}>
                <Box
                  variant="small"
                  color="text-body-secondary"
                  margin={{ bottom: 'xxs' }}
                >
                  {msg.role === 'user' ? 'You' : 'Agent'}
                </Box>
                <div
                  style={{
                    display: 'inline-block',
                    maxWidth: '80%',
                    padding: '8px 12px',
                    borderRadius: 8,
                    background: msg.role === 'user' ? colorBackgroundItemSelected : colorBackgroundContainerContent,
                    color: colorTextBodyDefault,
                    textAlign: 'left',
                    whiteSpace: 'pre-wrap',
                  }}
                >
                  {msg.content}
                </div>
                {msg.proposal && (
                  <div style={{ marginTop: 8 }}>
                    <PipelineProposalCard
                      proposal={msg.proposal}
                      onCreatePipeline={handleCreatePipeline}
                      isCreating={isCreating}
                    />
                  </div>
                )}
              </div>
            ))}
            {isLoading && (
              <Box textAlign="left">
                <Spinner size="normal" /> Thinking…
              </Box>
            )}
          </SpaceBetween>
          <div ref={bottomRef} />
        </div>
      </Container>

      <div style={{ display: 'flex', gap: 8 }}>
        <div style={{ flex: 1 }}>
          <Input
            value={input}
            onChange={({ detail }) => setInput(detail.value)}
            onKeyDown={({ detail }) => { if (detail.key === 'Enter') handleSend(); }}
            placeholder="Describe your pipeline…"
            disabled={isLoading}
          />
        </div>
        <Button variant="primary" onClick={handleSend} loading={isLoading}>
          Send
        </Button>
      </div>
    </SpaceBetween>
  );
}
