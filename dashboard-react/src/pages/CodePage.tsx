// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState } from 'react';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Box from '@cloudscape-design/components/box';
import Badge from '@cloudscape-design/components/badge';
import Button from '@cloudscape-design/components/button';
import Select from '@cloudscape-design/components/select';
import { usePipelineContext } from '../contexts/PipelineContext';
import { useCodeVersion } from '../hooks';
import ClickableArea from '../components/common/ClickableArea';
import CodeViewer from '../components/CodeViewer';
import TestResultsViewer from '../components/TestResultsViewer';
import AgentOutputDiff from '../components/AgentOutputDiff';

export default function CodePage() {
  const { pipeline } = usePipelineContext();
  const schemaHistory = pipeline.schema_history ?? [];

  const [selectedVersionIdx, setSelectedVersionIdx] = useState(schemaHistory.length - 1);
  const [selectedRunIndex, setSelectedRunIndex] = useState(0);
  const [showDiff, setShowDiff] = useState(false);

  const activeVersion = schemaHistory[selectedVersionIdx] ?? schemaHistory[schemaHistory.length - 1];
  const activeRuns = activeVersion?.runs ?? [];
  const activeRun = activeRuns[selectedRunIndex] ?? activeRuns[0];
  const activeCodeVersionId = activeRun?.code_version_id;

  // Find previous code version for diff comparison:
  // 1. Next run within the same schema version (if multiple runs)
  // 2. First run of the previous schema version (if single run or first run selected)
  const previousRun = activeRuns[selectedRunIndex + 1]
    ?? (selectedRunIndex === 0 && selectedVersionIdx > 0
      ? schemaHistory[selectedVersionIdx - 1]?.runs?.[0]
      : undefined);
  const previousCodeVersionId = previousRun?.code_version_id;

  const { data: currentCode } = useCodeVersion(showDiff ? activeCodeVersionId : undefined);
  const { data: previousCode } = useCodeVersion(showDiff ? previousCodeVersionId : undefined);

  const canShowDiff = !!activeCodeVersionId && !!previousCodeVersionId && activeCodeVersionId !== previousCodeVersionId;

  return (
    <SpaceBetween size="l">
      {/* Version selector + run badges */}
      <SpaceBetween direction="horizontal" size="m">
        {schemaHistory.length > 1 && (
          <Select
            selectedOption={{
              value: String(selectedVersionIdx),
              label: `Schema v${activeVersion?.version ?? selectedVersionIdx + 1}`,
            }}
            onChange={({ detail }) => {
              setSelectedVersionIdx(Number(detail.selectedOption.value));
              setSelectedRunIndex(0);
              setShowDiff(false);
            }}
            options={schemaHistory.map((v, idx) => ({
              value: String(idx),
              label: `Schema v${v.version}`,
              description: v.created ? `Created ${v.created}` : undefined,
            }))}
            data-testid="version-selector"
          />
        )}
        {activeRuns.length > 1 && (
          <SpaceBetween direction="horizontal" size="xxs">
            {activeRuns.map((run, idx) => (
              <ClickableArea
                as="span"
                key={run.run_id}
                ariaLabel={`View run ${activeRuns.length - idx}`}
                onClick={() => { setSelectedRunIndex(idx); setShowDiff(false); }}
              >
                <Badge color={selectedRunIndex === idx ? 'blue' : 'grey'}>
                  Run {activeRuns.length - idx}{run.test_status ? ` (${run.test_status})` : ''}
                </Badge>
              </ClickableArea>
            ))}
          </SpaceBetween>
        )}
        {canShowDiff && (
          <Button
            variant={showDiff ? 'primary' : 'normal'}
            onClick={() => setShowDiff(!showDiff)}
            data-testid="toggle-diff-view"
          >
            {showDiff ? 'Hide Diff' : 'Compare with Previous'}
          </Button>
        )}
      </SpaceBetween>

      {/* Diff view */}
      {showDiff && currentCode && previousCode && (
        <AgentOutputDiff
          labelA="Previous Version"
          labelB="Current Version"
          contentA={(previousCode as unknown as Record<string, unknown>).code_content as string ?? previousCode.code ?? ''}
          contentB={(currentCode as unknown as Record<string, unknown>).code_content as string ?? currentCode.code ?? ''}
          contentType="code"
        />
      )}

      {/* Code viewer */}
      {activeCodeVersionId ? (
        <SpaceBetween size="l">
          <CodeViewer codeVersionId={activeCodeVersionId} schemaVersion={activeVersion?.version} />
          <TestResultsViewer codeVersionId={activeCodeVersionId} />
        </SpaceBetween>
      ) : (
        <Box color="text-body-secondary">No code version available for the latest schema version.</Box>
      )}
    </SpaceBetween>
  );
}
