// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Textarea from '@cloudscape-design/components/textarea';
import SpaceBetween from '@cloudscape-design/components/space-between';
import Container from '@cloudscape-design/components/container';
import Header from '@cloudscape-design/components/header';
import type { WizardStepProps } from './types';

const NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9_-]*$/;

/**
 * P29 R5.1 — the single source of truth for Step 1 name validation.
 *
 * Returns the inline field-error string (or `undefined` when valid) covering
 * the required, format, and duplicate-name cases. Duplicate detection is
 * case-insensitive and matches against the names of pipelines that already
 * exist (supplied by the container's `usePipelines` query). Kept as a pure,
 * exported function so both this step component and the container's
 * Next-gating predicate share one implementation and it can be unit-tested
 * directly.
 */
export function validatePipelineName(
  value: string,
  existingNames: readonly string[] = [],
): string | undefined {
  const trimmed = value.trim();
  if (!trimmed) return 'Pipeline name is required';
  if (value.length < 3) return 'Name must be at least 3 characters';
  if (value.length > 64) return 'Name must be 64 characters or fewer';
  if (!NAME_RE.test(value))
    return 'Name must start with a letter or number and contain only letters, numbers, hyphens, or underscores';
  const normalized = trimmed.toLowerCase();
  if (existingNames.some((n) => n.trim().toLowerCase() === normalized))
    return `A pipeline named "${trimmed}" already exists`;
  return undefined;
}

export interface WizardStepNameProps extends WizardStepProps {
  /** Names of existing pipelines, used for case-insensitive duplicate detection. */
  existingNames?: readonly string[];
  /**
   * When `true`, surface the "required" error even for an empty field. The
   * container flips this on after the user clicks Next on an empty/invalid
   * name so the error appears inline at this step (P29 R5.1) rather than only
   * as a submit-time toast on the review step.
   */
  showRequiredError?: boolean;
}

export function WizardStepName({
  state,
  onChange,
  existingNames = [],
  showRequiredError = false,
}: WizardStepNameProps) {
  // Show the error live once the user has typed anything; force it onto an
  // empty field only after a Next attempt (so we don't nag before interaction).
  const nameError =
    state.pipelineName || showRequiredError
      ? validatePipelineName(state.pipelineName, existingNames)
      : undefined;

  return (
    <Container header={<Header variant="h2">Name &amp; Description</Header>}>
      <SpaceBetween size="l">
        <FormField
          label="Pipeline Name"
          description="A unique name for this pipeline"
          errorText={nameError}
          constraintText="3-64 characters. Letters, numbers, hyphens, and underscores only."
        >
          <Input
            value={state.pipelineName}
            onChange={({ detail }) => onChange({ ...state, pipelineName: detail.value })}
            placeholder="my-etl-pipeline"
            data-testid="pipeline-name-input"
          />
        </FormField>

        <FormField
          label="Description"
          description="Optional description of what this pipeline does"
        >
          <Textarea
            value={state.description}
            onChange={({ detail }) => onChange({ ...state, description: detail.value })}
            placeholder="Describe this pipeline's purpose"
            rows={3}
            data-testid="pipeline-description-input"
          />
        </FormField>
      </SpaceBetween>
    </Container>
  );
}
