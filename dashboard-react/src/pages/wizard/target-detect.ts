// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import type { WizardStepProps } from './types';

/* ---------- canDetectSchema helper ---------- */

// Exported for property tests in __tests__/WizardStepTarget.jdbc-detect.test.tsx
// (P19 Task 7.5 / Property 1) and re-exported from `WizardStepTarget.tsx` so the
// existing import path (`../WizardStepTarget`) stays stable. Extracted from
// `WizardStepTarget.tsx` (P29 R11.5 decomposition) with no behavior change.
export function canDetectSchema(state: WizardStepProps['state']): boolean {
  switch (state.targetType) {
    case 's3_tables':
      // Only for existing mode — need both bucket and table selected.
      // "New" mode won't have table_bucket_arn set.
      return !!(state.targetConfig.table_bucket_arn && state.targetConfig.bucket_name && state.targetConfig.table_name && state.targetConfig.namespace);
    case 'glue_catalog':
      return !!(state.targetConfig.database_name && state.targetConfig.table_name);
    case 'jdbc': {
      // P19 Task 7.1 — Validates: Requirement 1.1.
      // Detect Schema is enabled iff the user has supplied an identifying
      // database + table AND has configured a connection — either by
      // selecting a Glue connection by name, OR by providing the full
      // `select_rds` tuple (`secret_arn` + `jdbc_url`).
      const tc = state.targetConfig;
      const haveDbAndTable = !!(tc.database_name && tc.table_name);
      const haveGlueConnection = !!tc.glue_connection_name;
      const haveSelectRds = !!(tc.secret_arn && tc.jdbc_url);
      return haveDbAndTable && (haveGlueConnection || haveSelectRds);
    }
    default:
      return false;
  }
}
