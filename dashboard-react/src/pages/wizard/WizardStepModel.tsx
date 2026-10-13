// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import FormField from '@cloudscape-design/components/form-field';
import Select from '@cloudscape-design/components/select';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import Slider from '@cloudscape-design/components/slider';
import ColumnLayout from '@cloudscape-design/components/column-layout';
import Box from '@cloudscape-design/components/box';
import { useBedrockModels } from '../../hooks';
import { selectAsyncStatus, selectFiltering } from '../../components/common/select-async-status';
import { NO_BEDROCK_MODELS } from '../../utils/resource-empty-states';
// p64 R7 / R6.6 — the data-quality controls, rendered from the SAME component
// `PipelineFormModal`'s edit branch renders. The section used to live inline
// here; it was extracted so the two editors of one pipeline cannot grow
// divergent subsets of one setting the way `approval_config` did (four controls
// here, two there). The three-state Select, the absent-key spelling of
// "default", and the inline dedup-column error all moved with it unchanged —
// see that module's header.
import QualitySettingsFields from '../../components/QualitySettingsFields';
// The three boolean approval gates, shared with `PipelineFormModal`'s edit branch
// for the reason the comment at the render site gives. The Confidence Threshold
// Slider stays in this file — see that comment.
import ApprovalGatesFields from '../../components/ApprovalGatesFields';
import type { WizardStepProps } from './types';

/** Cost/quality hints per model family. */
const MODEL_HINTS: Record<string, { cost: string; quality: string }> = {
  'claude-sonnet-4': { cost: 'High', quality: 'Excellent' },
  'claude-3-5-sonnet': { cost: 'High', quality: 'Excellent' },
  'claude-3-haiku': { cost: 'Low', quality: 'Good' },
  'nova-pro': { cost: 'Medium', quality: 'Very Good' },
  'nova-lite': { cost: 'Low', quality: 'Good' },
  'titan-text': { cost: 'Low', quality: 'Basic' },
};

function getModelHint(modelId: string): { cost: string; quality: string } {
  for (const [key, hint] of Object.entries(MODEL_HINTS)) {
    if (modelId.includes(key)) return hint;
  }
  return { cost: 'Unknown', quality: 'Unknown' };
}

export function WizardStepModel({ state, onChange }: WizardStepProps) {
  const modelsQuery = useBedrockModels();
  const models = modelsQuery.data;

  const modelOptions = (models ?? []).map((m) => {
    const hint = getModelHint(m.model_id);
    return {
      value: m.model_id,
      label: m.model_name,
      description: `Cost: ${hint.cost} · Quality: ${hint.quality}`,
    };
  });

  const selectedHint = state.aiModel ? getModelHint(state.aiModel) : null;

  return (
    <Container header={<Header variant="h2">AI Model &amp; Options</Header>}>
      <SpaceBetween size="l">
        <FormField label="AI Model" description="Select the model for schema mapping and code generation">
          <Select
            selectedOption={state.aiModel ? modelOptions.find((o) => o.value === state.aiModel) ?? null : null}
            onChange={({ detail }) => onChange({ ...state, aiModel: detail.selectedOption.value! })}
            options={modelOptions}
            placeholder="Select an AI model"
            data-testid="ai-model-select"
            {...selectAsyncStatus(modelsQuery, 'models', NO_BEDROCK_MODELS)}
            {...selectFiltering('models', modelOptions.length)}
          />
        </FormField>

        {selectedHint && (
          <ColumnLayout columns={2}>
            <Box>
              <Box variant="awsui-key-label">Estimated Cost</Box>
              <Box>{selectedHint.cost}</Box>
            </Box>
            <Box>
              <Box variant="awsui-key-label">Output Quality</Box>
              <Box>{selectedHint.quality}</Box>
            </Box>
          </ColumnLayout>
        )}

        {/* The three boolean approval gates, rendered from the SAME component
            `PipelineFormModal`'s edit branch renders. They used to be spelled
            inline here — the modal spelled two of them independently, which is how
            an operator editing through the modal ended up unable to see or change
            `always_approve_schema_changes` at all. The three `data-testid`s moved
            with the toggles unchanged, so this step's existing tests address the
            same controls.

            The Confidence Threshold Slider below is deliberately NOT part of that
            component and stays here: `schema_mapping_confidence_threshold` has no
            reader anywhere in `lambda/`, `shared/` or `mcp/`, so a second control
            for it would be a second inert control, and on the modal's EDIT path it
            would additionally write a threshold nobody chose onto a record that
            carried none. See `ApprovalGatesFields`' header and
            `buildApprovalConfigRequest`'s `includeConfidenceThreshold`. */}
        <ApprovalGatesFields
          value={state.approvalConfig}
          onChange={(next) => onChange({ ...state, approvalConfig: next })}
        />

        <FormField
          label="Confidence Threshold"
          description={`Minimum confidence for auto-approval: ${state.approvalConfig.schemaMappingConfidenceThreshold}%`}
        >
          <Slider
            value={state.approvalConfig.schemaMappingConfidenceThreshold}
            onChange={({ detail }) =>
              onChange({
                ...state,
                approvalConfig: { ...state.approvalConfig, schemaMappingConfidenceThreshold: detail.value },
              })
            }
            min={0}
            max={100}
            step={5}
            data-testid="confidence-slider"
          />
        </FormField>

        {/* p64 R7 / R6.6 — the per-pipeline data-quality settings, rendered HERE
            because R7.3 requires them settable at pipeline creation rather than
            only by editing afterwards. They ride to the backend as a
            `quality_config` peer of `approval_config`, never inside
            `target_config` (design C7).

            The controls themselves are `QualitySettingsFields`, shared with
            `PipelineFormModal`'s edit branch. The step's own contribution is the
            navigation guard in `CreatePipelinePage` (`isModelStepNextDisabled`),
            which reads `dedupColumnsError` off `state.qualityConfig` — the
            component reports the error inline, the host decides what it gates. */}
        <QualitySettingsFields
          value={state.qualityConfig}
          onChange={(next) => onChange({ ...state, qualityConfig: next })}
        />
      </SpaceBetween>
    </Container>
  );
}
