// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { useState, useEffect } from 'react';
import Modal from '@cloudscape-design/components/modal';
import Box from '@cloudscape-design/components/box';
import Button from '@cloudscape-design/components/button';
import SpaceBetween from '@cloudscape-design/components/space-between';
import FormField from '@cloudscape-design/components/form-field';
import Input from '@cloudscape-design/components/input';
import Select from '@cloudscape-design/components/select';
import Textarea from '@cloudscape-design/components/textarea';
import Spinner from '@cloudscape-design/components/spinner';
import type { Pipeline, Schema, TargetConfig } from '../types';
import { useBedrockModels, useCreatePipeline, useEditPipeline } from '../hooks';
import { selectAsyncStatus, selectFiltering } from './common/select-async-status';
import { NO_BEDROCK_MODELS } from '../utils/resource-empty-states';
import { useToast } from '../contexts/ToastContext';
import SchemaSourceSelector from './SchemaSourceSelector';
import TargetConfigForm, { type SchemaSourceType } from './TargetConfigForm';
import QualitySettingsFields, { hydrateQualityConfig } from './QualitySettingsFields';
import ApprovalGatesFields from './ApprovalGatesFields';
import { QUALITY_CONFIG_REQUEST_KEY, dedupColumnsError } from '../utils/quality-config';
import { targetSupportsMerge } from '../utils/write-modes';
// The single normalisation authorities for these two sub-objects' wire shapes, and
// the single record→state reader for the Approval_Config — all imported rather than
// reimplemented. `buildQualityConfigRequest` is what decides whether
// `data_quality_enabled` is sent at all; `buildApprovalConfigRequest` is what
// decides the casing, the confidence unit and (via its option) whether the
// reader-less threshold is sent at all. A second serialiser here is exactly how the
// two editors would come to disagree about either.
import {
  buildApprovalConfigRequest,
  buildQualityConfigRequest,
  hydrateApprovalConfig,
} from '../pages/CreatePipelinePage';
import { INITIAL_WIZARD_STATE } from '../pages/wizard/types';
import type { ApprovalConfig, QualityConfig } from '../pages/wizard/types';

const WRITE_MODE_OPTIONS = [
  { value: 'append', label: 'Append', description: 'Add new records, keep existing data' },
  { value: 'overwrite', label: 'Overwrite', description: 'Replace all existing data on each run' },
  { value: 'merge', label: 'Merge (Upsert)', description: 'Update matching rows, insert new ones (Iceberg only)' },
];

export interface PipelineFormModalProps {
  visible: boolean;
  onDismiss: () => void;
  pipeline?: Pipeline; // If provided, edit mode
}

export default function PipelineFormModal({ visible, onDismiss, pipeline }: PipelineFormModalProps) {
  const isEdit = !!pipeline;
  const { showToast } = useToast();
  const modelsQuery = useBedrockModels();
  const models = modelsQuery.data;
  const createMutation = useCreatePipeline();
  const editMutation = useEditPipeline();

  const [name, setName] = useState('');
  const [selectedSchemaId, setSelectedSchemaId] = useState('');
  const [schemaSourceType, setSchemaSourceType] = useState<SchemaSourceType>('uploaded');
  const [aiModel, setAiModel] = useState('');
  const [description, setDescription] = useState('');
  const [targetConfig, setTargetConfig] = useState<Partial<TargetConfig>>({});
  const [submitting, setSubmitting] = useState(false);
  // One `ApprovalConfig` rather than the two loose booleans this modal used to
  // hold. The two booleans were the divergence: they were the only approval state
  // the modal had, so the modal could only ever render and submit two of the four
  // controls the wizard renders, and `always_approve_schema_changes` — a governance
  // gate with a live reader at `schema_mapping/lambda_function.py:1657` — was
  // invisible here with nothing indicating it existed.
  const [approvalConfig, setApprovalConfig] = useState<ApprovalConfig>(
    INITIAL_WIZARD_STATE.approvalConfig,
  );
  const [qualityConfig, setQualityConfig] = useState<QualityConfig>(
    INITIAL_WIZARD_STATE.qualityConfig,
  );

  // Pre-populate in edit mode
  useEffect(() => {
    if (pipeline && visible) {
      setName(pipeline.pipeline_name || '');
      setSelectedSchemaId(pipeline.target_schema || '');
      setAiModel(pipeline.ai_model || pipeline.model_id || '');
      setDescription(pipeline.pipeline_description || '');
      if (pipeline.target_config) setTargetConfig(pipeline.target_config);
      // Hydrated through `hydrateApprovalConfig` — the same reader the wizard's
      // `hydrateWizardStateFromPipeline` uses — and NEVER with `?? false` on
      // `always_approve_schema_changes`. `true` there means approval IS REQUIRED
      // (`schema_mapping/lambda_function.py:1657` reads
      // `approval_config.get("always_approve_schema_changes", True)`), so a record
      // that stores no such key is a record whose schema-change gate is ON. Reading
      // that absence as `false` and saving would turn the gate off for a pipeline
      // nobody asked to change, with nothing reporting it — a reduction in human
      // oversight delivered as a side effect of editing a description.
      //
      // Called unconditionally, replacing the `if (pipeline.approval_config)` guard
      // that used to sit here. That guard was itself a bug of the same family: a
      // record carrying no `approval_config` left whatever the previously-opened
      // pipeline's toggles happened to be in state, and Save then wrote those onto
      // this pipeline. The hydrate resets to the documented defaults instead, which
      // is what `hydrateQualityConfig` below already did for the same reason
      // (`LGR-919254d-2`).
      setApprovalConfig(hydrateApprovalConfig(pipeline.approval_config));
      // p64 R7.4 — hydrated through `hydrateQualityConfig`, the same reader the
      // wizard's `hydrateWizardStateFromPipeline` uses, and NEVER with `?? false`
      // on `data_quality_enabled`. An existing S3 Tables pipeline has data quality
      // on by template default and stores no such key, so reading absence as
      // `false` and saving would write an explicit opt-OUT: the row-level split and
      // quarantine write that target type has had since P28 would stop being
      // generated, and the pipeline's verdict would move from `enforced` to
      // `advisory` on its first edit through this modal, with nothing reporting it.
      //
      // Called unconditionally — no `if (pipeline.quality_config)` guard, unlike
      // the `approval_config` block above it. A pipeline whose record carries no
      // `quality_config` at all must still reset the controls to the documented
      // defaults, or a previously-opened pipeline's settings would linger in state
      // and be saved onto this one.
      setQualityConfig(hydrateQualityConfig(pipeline.quality_config));
    } else if (!visible) {
      // Reset on close
      setName('');
      setSelectedSchemaId('');
      setAiModel('');
      setDescription('');
      setTargetConfig({});
      setApprovalConfig(INITIAL_WIZARD_STATE.approvalConfig);
      setQualityConfig(INITIAL_WIZARD_STATE.qualityConfig);
      setSubmitting(false);
    }
  }, [pipeline, visible]);

  // p64 R7.2 — the save gate on the dedup column names, the modal's counterpart
  // of the wizard's Model-step navigation guard.
  //
  // Blocks only on a name the Identifier_Allowlist refuses, never on emptiness: an
  // empty list means every mapped target column and is the default. The gate is
  // not what enforces the allowlist — `_validate_quality_config` 400s at the
  // boundary — it is what puts the refusal on the control that owns the field
  // instead of surfacing it as a failed Save with the offending name three layers
  // away. `QualitySettingsFields` already renders the same message inline.
  //
  // Gated on `dedupEnabled` because that is the only branch that renders the
  // control: a stored pipeline could otherwise carry bad columns with dedup off
  // and block Save with no visible field to correct — a dead end, and unreachable
  // for a keyboard or screen-reader user in particular.
  const dedupError = qualityConfig.dedupEnabled
    ? dedupColumnsError(qualityConfig.dedupColumns)
    : undefined;

  const modelOptions = (models ?? []).map((m) => ({
    value: m.model_id,
    label: m.model_name,
  }));

  function handleSchemaSelect(schemaId: string, schemaData: Schema) {
    setSelectedSchemaId(schemaId);
    // Determine source type from schema data
    const src = schemaData.source_type;
    if (src === 'glue_data_catalog') setSchemaSourceType('glue_data_catalog');
    else if (src === 's3_table_bucket') setSchemaSourceType('s3_table_bucket');
    else setSchemaSourceType('uploaded');
  }

  // The write modes this pipeline's target implements, and whether the stored
  // one is among them. See `utils/write-modes.ts`.
  const writeModeOptions = targetSupportsMerge(targetConfig.type)
    ? WRITE_MODE_OPTIONS
    : WRITE_MODE_OPTIONS.filter((o) => o.value !== 'merge');
  const writeModeUnsupported =
    targetConfig.write_mode !== undefined && !writeModeOptions.some((o) => o.value === targetConfig.write_mode);

  async function handleSubmit() {
    if (!aiModel) {
      showToast('AI model is required', 'error');
      return;
    }

    // The same refusal `edit_pipeline` would return as a 400, made here so the
    // reason sits on the control. Only in edit mode: the create branch of this
    // modal renders no write-mode control.
    if (isEdit && writeModeUnsupported) {
      showToast('Choose a write mode this target supports before saving.', 'error');
      return;
    }

    // p64 R7.2 — refuse a dedup column list the boundary would refuse, rather than
    // submitting it and reporting a 400. The inline `errorText` on the control
    // already names the offending column and why; this is the half that stops the
    // request. Checked before `setSubmitting(true)` so the modal does not flash its
    // full-screen "Updating..." overlay for a submit that never happens.
    if (isEdit && dedupError) {
      showToast(dedupError, 'error');
      return;
    }

    setSubmitting(true);
    try {
      if (isEdit) {
        // p64 R7 / R7.5 — this modal is the second editor of one pipeline object,
        // and it now carries the **same** Quality_Config controls the 6-step
        // wizard's Model step does, from one shared component
        // (`QualitySettingsFields`).
        //
        // **Both earlier blocks on doing this are gone.** `edit_pipeline` HAS a
        // `quality_config` branch (`handlers/pipelines_crud.py:3037`), merging
        // **per key** exactly as its `approval_config` sibling at `:2974` does
        // (`LGR-7bea749-1`), so submitting one key cannot erase the others — and
        // specifically cannot erase an explicit `data_quality_enabled`. A control
        // here is therefore not inert, which is what the previous revision of this
        // comment recorded as the technical block, noting "Add the handler branch
        // first, then the control". That ordering is satisfied.
        //
        // The second block was a product question — which editor owns the setting —
        // left open by `LGR-7bea749-6`, which rejected putting a control in one
        // editor (making the other's hydration dead code) and in both independently
        // (inviting two divergent spellings). **One shared component is the third
        // answer**, and it is the answer precisely because `approval_config` is the
        // worked example of the alternative: the wizard has four approval controls,
        // this modal has two, and an operator here cannot see or change
        // `always_approve_schema_changes` or `schema_mapping_confidence_threshold`.
        // Adding the quality settings as a third arbitrary subset would have
        // repeated that.
        //
        // What this path does NOT do, verified rather than assumed: it cannot reset
        // a setting it does not name. The handler merges each sub-object **per key**
        // into the existing map (`approval_config.{name} = {value}` at `:2952`,
        // after an `if_not_exists` to create the map), so an attribute the body
        // never mentions is untouched. That is what makes the deliberate omission
        // of `schema_mapping_confidence_threshold` below a genuine "leave it alone"
        // rather than a silent reset.
        await editMutation.mutateAsync({
          id: pipeline!.pipeline_key,
          ai_model: aiModel,
          description: description || undefined,
          target_config: targetConfig as Record<string, unknown>,
          // Serialised through the one authority, not assembled here. This used to
          // be a hand-built object with two snake_case keys, which is what let the
          // two editors drift into two subsets: a hand-built body is a second
          // policy on which keys reach the record, and p63 R1.2 permits exactly one
          // (p64 2a.1b placed it at the wizard's request boundary). It is also what
          // converts the confidence percent to the stored fraction — which matters
          // here precisely because this call asks it NOT to send that key.
          //
          // `includeConfidenceThreshold: false` is the one deliberate asymmetry
          // with the wizard's create path, and it is load-bearing rather than
          // laziness. `schema_mapping_confidence_threshold` has no reader anywhere
          // in `lambda/`, `shared/` or `mcp/`; the wizard seeds it at `80` (= 0.8)
          // while `_default_approval_config` seeds `Decimal("0.85")`. Sending it
          // from here would write `0.8` onto a record that carried no threshold, or
          // over a stored 0.85, resolving a knowing disagreement that **p63 R9/D2
          // owns** (stricter-only, refused rather than clamped) as a side effect of
          // an unrelated edit — and for a control the operator never saw, since
          // `ApprovalGatesFields` deliberately renders no Slider. Do NOT drop this
          // option to make the two editors look symmetrical.
          approval_config: buildApprovalConfigRequest(approvalConfig, {
            includeConfidenceThreshold: false,
          }),
          // Serialised through the one authority, not passed through.
          // `qualityConfig` is camelCase and every reader reads snake_case, and
          // `buildQualityConfigRequest` is also what decides whether
          // `data_quality_enabled` is sent **at all** — it omits the key for a
          // pipeline nobody has expressed an opinion about, which is what keeps this
          // save from writing an explicit opt-OUT onto a template default. Do NOT
          // replace this with `qualityConfig`, and do not assemble the object here.
          [QUALITY_CONFIG_REQUEST_KEY]: buildQualityConfigRequest(qualityConfig),
        });
        showToast('Pipeline updated successfully', 'success');
      } else {
        if (!name.trim()) {
          showToast('Pipeline name is required', 'error');
          setSubmitting(false);
          return;
        }
        if (!selectedSchemaId) {
          showToast('Target schema is required', 'error');
          setSubmitting(false);
          return;
        }
        // p64 R7.1 — no `quality_config` here either, and that is correct rather
        // than an omission. `create_pipeline` seeds `_default_quality_config()`
        // when the key is absent, which is dedup **off** with an empty column list
        // and — deliberately — no `data_quality_enabled`, so the pipeline takes its
        // template's own DQ posture. That is precisely R7.1's "default to off so no
        // existing pipeline's behaviour changes". Sending `data_quality_enabled:
        // false` from here to look thorough would opt every pipeline created
        // through this modal OUT of a DQ block, which R6.6 forbids.
        await createMutation.mutateAsync({
          pipeline_name: name.trim(),
          target_schema: selectedSchemaId,
          ai_model: aiModel,
          description: description || undefined,
          target_config: targetConfig as Record<string, unknown>,
        });
        showToast('Pipeline created successfully', 'success');
      }
      onDismiss();
    } catch {
      showToast(`Failed to ${isEdit ? 'update' : 'create'} pipeline`, 'error');
    } finally {
      setSubmitting(false);
    }
  }

  // Full-screen loading overlay during submission
  if (submitting) {
    return (
      <Modal visible header={isEdit ? 'Updating Pipeline...' : 'Creating Pipeline...'} onDismiss={() => {}}>
        <Box textAlign="center" padding="xxl">
          <SpaceBetween size="m" alignItems="center">
            <Spinner size="large" />
            <Box variant="p" color="text-body-secondary">
              {isEdit ? 'Updating pipeline configuration...' : 'Provisioning new pipeline...'}
            </Box>
          </SpaceBetween>
        </Box>
      </Modal>
    );
  }

  return (
    <Modal
      visible={visible}
      onDismiss={onDismiss}
      header={isEdit ? 'Edit Pipeline' : 'Create Pipeline'}
      size="large"
      footer={
        <Box float="right">
          <SpaceBetween direction="horizontal" size="xs">
            <Button variant="link" onClick={onDismiss}>Cancel</Button>
            <Button variant="primary" onClick={handleSubmit}>
              {isEdit ? 'Save Changes' : 'Create Pipeline'}
            </Button>
          </SpaceBetween>
        </Box>
      }
    >
      <SpaceBetween size="l">
        <FormField label="Pipeline Name">
          {isEdit ? (
            <Box>{name}</Box>
          ) : (
            <Input
              value={name}
              onChange={({ detail }) => setName(detail.value)}
              placeholder="Enter pipeline name"
            />
          )}
        </FormField>

        {!isEdit && (
          <FormField label="Target Schema" description="Select a schema source and choose a schema">
            <SchemaSourceSelector onSchemaSelect={handleSchemaSelect} />
          </FormField>
        )}

        <FormField label="AI Model">
          <Select
            selectedOption={aiModel ? modelOptions.find((o) => o.value === aiModel) ?? { value: aiModel, label: aiModel } : null}
            onChange={({ detail }) => setAiModel(detail.selectedOption.value!)}
            options={modelOptions}
            placeholder="Select an AI model"
            {...selectAsyncStatus(modelsQuery, 'models', NO_BEDROCK_MODELS)}
            {...selectFiltering('models', modelOptions.length)}
          />
        </FormField>

        <FormField label="Description" description="Optional">
          <Textarea
            value={description}
            onChange={({ detail }) => setDescription(detail.value)}
            placeholder="Describe this pipeline"
            rows={3}
          />
        </FormField>

        {!isEdit && (
          <FormField label="Target Configuration">
            <TargetConfigForm
              schemaSourceType={schemaSourceType}
              value={targetConfig}
              onChange={setTargetConfig}
            />
          </FormField>
        )}

        {isEdit && (
          <>
            {/* Only the modes this target's writer implements. Merge is S3 Tables
                only; the server refuses it for every other target at edit and at
                code generation (`shared/write_modes.py`). A pipeline stored with a
                mode its target cannot run (a legacy JDBC `merge`) shows no
                selection and says why, rather than displaying a different option
                while still submitting the stored one. */}
            <FormField
              label="Write Mode"
              description="How new data is written to the target"
              errorText={
                writeModeUnsupported
                  ? `'${targetConfig.write_mode}' is not supported for this target. Choose ${writeModeOptions
                      .map((o) => o.label)
                      .join(' or ')} before saving.`
                  : undefined
              }
            >
              <Select
                selectedOption={
                  writeModeUnsupported
                    ? null
                    : (writeModeOptions.find((o) => o.value === (targetConfig.write_mode ?? 'append')) ?? writeModeOptions[0])
                }
                placeholder="Choose a write mode"
                onChange={({ detail }) => {
                  setTargetConfig({
                    ...targetConfig,
                    write_mode: detail.selectedOption.value!,
                    ...(detail.selectedOption.value !== 'merge' ? { merge_key: undefined } : {}),
                  });
                }}
                options={writeModeOptions}
                data-testid="edit-write-mode-select"
              />
            </FormField>
            {targetConfig.write_mode === 'merge' && (
              <FormField label="Merge Key" description="Column to match for upsert">
                <Input
                  value={(targetConfig as Record<string, string>).merge_key ?? ''}
                  onChange={({ detail }) => setTargetConfig({ ...targetConfig, merge_key: detail.value } as Partial<TargetConfig>)}
                  placeholder="e.g. customer_id"
                />
              </FormField>
            )}
            {/* The three boolean approval gates the wizard's Model step renders,
                from one component, so the two editors of this object cannot drift
                into two subsets of one setting again. This is the change: the modal
                used to render two of them inline, so
                `always_approve_schema_changes` was invisible here.

                Not four. `schema_mapping_confidence_threshold` has no control in
                either the component or this branch — see the component's header,
                and the `includeConfidenceThreshold: false` at the submit site,
                which is the write-side half of the same decision. */}
            <ApprovalGatesFields value={approvalConfig} onChange={setApprovalConfig} />
            {/* p64 R7.4 — the same controls the wizard's Model step renders, from
                one component, so the two editors of this object cannot drift into
                two subsets of one setting. Rendered only in the edit branch: the
                create branch here deliberately sends no `quality_config` at all so
                `create_pipeline` seeds `_default_quality_config()`, which is dedup
                off and — deliberately — no `data_quality_enabled`, leaving the new
                pipeline on its template's own DQ posture (see the create branch's
                own comment). Offering the controls there would only be correct
                alongside sending them, and R7.3 already places pipeline-creation
                quality settings in the wizard. */}
            <QualitySettingsFields value={qualityConfig} onChange={setQualityConfig} />
          </>
        )}
      </SpaceBetween>
    </Modal>
  );
}
