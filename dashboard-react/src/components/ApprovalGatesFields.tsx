// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import Toggle from '@cloudscape-design/components/toggle';
import Header from '@cloudscape-design/components/header';
import SpaceBetween from '@cloudscape-design/components/space-between';
import { INITIAL_WIZARD_STATE } from '../pages/wizard/types';
import type { ApprovalConfig } from '../pages/wizard/types';

/**
 * NOTE ON SHAPE — this module holds the controls only, **not** the record→state
 * reader, which is where it deliberately differs from its sibling
 * `QualitySettingsFields` (`LGR-919254d-1` put `hydrateQualityConfig` beside those
 * controls).
 *
 * `hydrateApprovalConfig` lives in `pages/CreatePipelinePage.tsx` instead, and it
 * has to: the reader needs the percent↔fraction conversion, and
 * `percentToConfidenceFraction` / `confidenceFractionToPercent` cannot move out of
 * that file — `tests/contracts/test_approval_config_casing_cross_language.py` reads
 * them out of it by name from Python and asserts `buildApprovalConfigRequest` calls
 * one of them adjacent to the wire key. Importing the conversion *here* would make
 * `CreatePipelinePage` ↔ `ApprovalGatesFields` a genuine module cycle, since that
 * page imports the reader back. An asymmetry with the quality sibling is a smaller
 * price than a cycle, so the reader sits with the conversion and both editors
 * import it from there — the same direction `PipelineFormModal` already imports
 * `buildQualityConfigRequest`.
 */

export interface ApprovalGatesFieldsProps {
  /**
   * The current value. Accepts `undefined` for the same reason
   * `QualitySettingsFields` does: `useWizardDraft.readDraft` is a bare
   * `JSON.parse(raw) as T` and `handleRestoreDraft` replaces state rather than
   * merging into it, so a draft saved before a field existed can restore it as
   * `undefined` despite the type.
   */
  value: ApprovalConfig | undefined;
  onChange: (next: ApprovalConfig) => void;
}

/**
 * The pipeline's approval gates, as **one** set of controls rendered by both
 * editors of the pipeline object.
 *
 * ## Why this is a shared component
 *
 * There are two editors of one pipeline: the 6-step wizard's Model step and
 * `PipelineFormModal`'s `isEdit` branch. Before this component the wizard rendered
 * four approval controls and the modal rendered two, so an operator editing through
 * the modal could not see or change `always_approve_schema_changes` — a governance
 * gate with a live reader at `schema_mapping/lambda_function.py:1657` — and nothing
 * indicated it existed. `LGR-919254d-1` names that divergence as the worked example
 * of what happens when each editor grows its own section, and closes the same gap
 * for `quality_config`. This is that treatment applied to its own example.
 *
 * ## What it renders — three gates, and deliberately NOT the confidence threshold
 *
 * `schema_mapping_confidence_threshold` is **excluded on purpose. Do not "complete"
 * this component by adding a Slider for it.**
 *
 * That key has **no reader anywhere** in `lambda/`, `shared/` or `mcp/`. Its only
 * mentions are its own writer (`pipelines_crud._default_approval_config`, seeding
 * `Decimal("0.85")`), its boundary range check, and
 * `create_infrastructure/lambda_function.py:620`, which names it explicitly to say
 * "Not this one." The auto-approval comparison reads
 * `shared/config.py`'s `CONFIDENCE_AUTO_APPROVE_THRESHOLD` directly. The
 * cross-language contract test excludes it from `_BOOLEAN_GATES` for exactly this
 * reason and records that p63 R9 owns giving it a reader.
 *
 * So rendering it here would add a **second** control that changes nothing — the
 * defect class this whole line of work exists to remove — and it would do concrete
 * harm beyond that. The wizard seeds the threshold at `80` (= `0.8`) while
 * `_default_approval_config` seeds `Decimal("0.85")`; a record carrying neither
 * would hydrate to the wizard's `80` and the save would write `0.8` onto it,
 * resolving a knowing disagreement that p63 R9/D2 owns — stricter-only, refused
 * rather than clamped — in whichever direction this component happened to guess.
 * Writing a governance number nobody chose is worse than not offering the control.
 *
 * The **wizard keeps its Slider**, in `WizardStepModel.tsx` outside this component.
 * That is not an inconsistency left by accident: the wizard is establishing a new
 * record where no stored value can be overwritten, its `min={0} max={100}` domain
 * is pinned by contract test, and `buildApprovalConfigRequest` sends all four keys
 * on the create path today. Removing it would be a behaviour change with its own
 * argument to make.
 *
 * ## Shape
 *
 * Purely presentational: no state, no submission, no knowledge of which editor is
 * rendering it. It carries its own `Header` so the section is self-contained, with
 * the same cosmetic consequence `LGR-919254d-1` accepted — the host's
 * header-to-controls gap tightens from `l` to `m`.
 */
export default function ApprovalGatesFields({ value, onChange }: ApprovalGatesFieldsProps) {
  const approval = value ?? INITIAL_WIZARD_STATE.approvalConfig;

  return (
    <SpaceBetween size="m">
      <Header variant="h3">Approval Gates</Header>

      <Toggle
        checked={approval.schemaMappingEnabled}
        onChange={({ detail }) => onChange({ ...approval, schemaMappingEnabled: detail.checked })}
        data-testid="schema-mapping-toggle"
      >
        Require approval for schema mapping
      </Toggle>

      <Toggle
        checked={approval.codeGenerationEnabled}
        onChange={({ detail }) => onChange({ ...approval, codeGenerationEnabled: detail.checked })}
        data-testid="code-generation-toggle"
      >
        Require approval for code generation
      </Toggle>

      {/* LABEL ONLY. `alwaysApproveSchemaChanges: true` means approval IS
          REQUIRED — the reader is `schema_mapping/lambda_function.py:1657`,
          `approval_config.get("always_approve_schema_changes", True)`. The old
          label ("Auto-approve schema changes") said the opposite, so the
          fail-closed default displayed as "Auto-approve: Yes". Relabelled in
          `f3cab1a` to match the value and its two siblings above;
          `WizardStepReview`'s matching row was relabelled with it.

          DO NOT invert the stored polarity to suit the label, and do not rename
          the field, the `always_approve_schema_changes` wire key or the record
          attribute: p63 R2.9 records inversion as a standing do-NOT — every
          existing record carries the current spelling, so reusing the key with
          inverted meaning flips the gate on every pipeline in the account. The
          rename half is p63 R2/D3's; this is the relabel half only, which is why
          `data-testid` is unchanged too — including through this extraction, so
          the wizard's existing tests keep addressing the same control. */}
      <Toggle
        checked={approval.alwaysApproveSchemaChanges}
        onChange={({ detail }) =>
          onChange({ ...approval, alwaysApproveSchemaChanges: detail.checked })
        }
        data-testid="auto-approve-toggle"
      >
        Require approval for schema changes
      </Toggle>
    </SpaceBetween>
  );
}
