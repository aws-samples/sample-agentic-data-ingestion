// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useCallback } from 'react';
import { apiClient } from '../services/api-client';

export interface PipelineProposal {
  pipeline_name: string;
  source: { type: string; location: string; format: string };
  target: { type: string; table_name: string; partitioning: string[] };
  schema: { fields: Array<{ name: string; type: string }> };
  mappings: Array<{ source: string; target: string; confidence: number }>;
}

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
  timestamp: number;
  proposal?: PipelineProposal;
}

interface ChatResponse {
  response?: string;
  reasoning_steps?: string[];
  proposal?: PipelineProposal;
}

export function useChatAgent() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  const sendMessage = useCallback(async (message: string) => {
    setMessages(prev => [...prev, { role: 'user', content: message, timestamp: Date.now() }]);
    setIsLoading(true);
    try {
      const response = await apiClient.post<ChatResponse>('/chat', { message });
      const assistantMsg: ChatMessage = {
        role: 'assistant',
        content: response.reasoning_steps?.join('\n') || response.response || '',
        timestamp: Date.now(),
        proposal: response.proposal,
      };
      setMessages(prev => [...prev, assistantMsg]);
    } catch {
      setMessages(prev => [
        ...prev,
        { role: 'assistant', content: 'Error: Failed to get response', timestamp: Date.now() },
      ]);
    } finally {
      setIsLoading(false);
    }
  }, []);

  return { messages, sendMessage, isLoading };
}
