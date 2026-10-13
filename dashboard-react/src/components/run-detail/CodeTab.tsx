// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import CodeViewer from '../CodeViewer';
import TestResultsViewer from '../TestResultsViewer';

interface CodeTabProps {
  codeVersionId: string;
}

/**
 * Code tab content extracted from RunDetailPage (P29 R11.5).
 * Renders the generated-code viewer and its test results, or the empty copy.
 */
export default function CodeTab({ codeVersionId }: CodeTabProps) {
  return (
    <SpaceBetween size="l">
      {codeVersionId ? (
        <>
          <CodeViewer codeVersionId={codeVersionId} />
          <TestResultsViewer codeVersionId={codeVersionId} />
        </>
      ) : (
        <Box textAlign="center" color="text-body-secondary" padding="l">
          No code was generated in this run.
        </Box>
      )}
    </SpaceBetween>
  );
}
