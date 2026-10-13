// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Button from '@cloudscape-design/components/button';
import Tabs from '@cloudscape-design/components/tabs';
import {
  colorBackgroundContainerContent,
  colorTextBodyDefault,
  colorBorderDividerDefault,
} from '@cloudscape-design/design-tokens';

interface PromptResponseModalProps {
  visible: boolean;
  onDismiss: () => void;
  promptPreview: string;
  responsePreview: string;
  modelId?: string;
}

export default function PromptResponseModal({
  visible,
  onDismiss,
  promptPreview,
  responsePreview,
  modelId,
}: PromptResponseModalProps) {
  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={modelId ? `Model Call — ${modelId}` : 'Model Call Details'}
      size="max"
      footer={
        <Box float="right">
          <Button variant="primary" onClick={onDismiss}>Close</Button>
        </Box>
      }
    >
      <SpaceBetween size="m">
        <Tabs
          tabs={[
            {
              id: 'prompt',
              label: 'Prompt',
              content: (
                <pre
                  data-testid="prompt-content"
                  style={{
                    background: colorBackgroundContainerContent,
                    color: colorTextBodyDefault,
                    border: `1px solid ${colorBorderDividerDefault}`,
                    padding: 12,
                    borderRadius: 4,
                    fontSize: 13,
                    maxHeight: 'calc(80vh - 200px)',
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {promptPreview || 'No prompt data available'}
                </pre>
              ),
            },
            {
              id: 'response',
              label: 'Response',
              content: (
                <pre
                  data-testid="response-content"
                  style={{
                    background: colorBackgroundContainerContent,
                    color: colorTextBodyDefault,
                    border: `1px solid ${colorBorderDividerDefault}`,
                    padding: 12,
                    borderRadius: 4,
                    fontSize: 13,
                    maxHeight: 'calc(80vh - 200px)',
                    overflow: 'auto',
                    whiteSpace: 'pre-wrap',
                    wordBreak: 'break-word',
                  }}
                >
                  {responsePreview || 'No response data available'}
                </pre>
              ),
            },
          ]}
        />
      </SpaceBetween>
    </Modal>
  );
}
