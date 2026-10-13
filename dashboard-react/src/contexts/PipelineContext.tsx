// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { createContext, useContext } from 'react';
import type { Pipeline } from '../types';

export interface PipelineContextValue {
  pipeline: Pipeline;
  isLoading: boolean;
  isError: boolean;
  refetch: () => void;
}

const PipelineContext = createContext<PipelineContextValue | null>(null);

export function PipelineProvider({ children, value }: { children: React.ReactNode; value: PipelineContextValue }) {
  return <PipelineContext.Provider value={value}>{children}</PipelineContext.Provider>;
}

export function usePipelineContext(): PipelineContextValue {
  const ctx = useContext(PipelineContext);
  if (!ctx) throw new Error('usePipelineContext must be used within PipelineLayout');
  return ctx;
}
