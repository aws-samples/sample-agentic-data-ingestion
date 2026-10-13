// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';

// jsdom doesn't implement scrollIntoView
Element.prototype.scrollIntoView = vi.fn();

// Mock the chat agent hook — returns value is controlled per test
const mockSendMessage = vi.fn();
let mockMessages: Array<{ role: 'user' | 'assistant'; content: string; timestamp: number }> = [];
let mockIsLoading = false;

vi.mock('../../hooks/use-chat-agent', () => ({
  useChatAgent: () => ({
    messages: mockMessages,
    sendMessage: mockSendMessage,
    isLoading: mockIsLoading,
  }),
}));

// Mock the api-client
vi.mock('../../services/api-client', () => ({
  apiClient: { post: vi.fn(), get: vi.fn() },
}));

// Mock PipelineProposalCard to keep test focused
vi.mock('../../components/PipelineProposalCard', () => ({
  default: () => <div data-testid="proposal-card">Proposal</div>,
}));

import ChatPage from '../ChatPage';

function renderPage() {
  return render(
    <MemoryRouter>
      <ChatPage />
    </MemoryRouter>,
  );
}

beforeEach(() => {
  mockMessages = [];
  mockIsLoading = false;
  mockSendMessage.mockClear();
});

describe('ChatPage', () => {
  it('renders the header', () => {
    renderPage();
    expect(screen.getByText('Chat — Pipeline Builder')).toBeInTheDocument();
  });

  it('renders the input field and Send button', () => {
    renderPage();
    expect(screen.getByPlaceholderText('Describe your pipeline…')).toBeInTheDocument();
    expect(screen.getByText('Send')).toBeInTheDocument();
  });

  it('shows empty state message when no messages', () => {
    renderPage();
    expect(screen.getByText(/Describe a pipeline in plain English/)).toBeInTheDocument();
  });

  it('renders user and assistant messages', () => {
    mockMessages = [
      { role: 'user', content: 'Load CSV data', timestamp: 1 },
      { role: 'assistant', content: 'Scanning files...', timestamp: 2 },
    ];
    renderPage();
    expect(screen.getByText('Load CSV data')).toBeInTheDocument();
    expect(screen.getByText('Scanning files...')).toBeInTheDocument();
  });

  it('shows loading spinner when isLoading', () => {
    mockIsLoading = true;
    renderPage();
    expect(screen.getByText('Thinking…')).toBeInTheDocument();
  });
});
