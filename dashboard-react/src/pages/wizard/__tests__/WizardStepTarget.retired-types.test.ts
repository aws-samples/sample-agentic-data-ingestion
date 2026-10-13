// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
//
// The Glue Catalog target is no longer offered for a new pipeline:
// `create_pipeline` refuses `GLUE_CATALOG` with a 400, so the wizard must not
// list it. It is still listed while it is the current value, so a state
// hydrated from an existing Glue Catalog pipeline renders its selection.
import { describe, it, expect, vi } from 'vitest';

vi.mock('../../../services/api-client', () => ({
  apiClient: { get: vi.fn() },
}));

import { offeredTargetTypeOptions } from '../WizardStepTarget';

const values = (current: string) => offeredTargetTypeOptions(current).map((o) => o.value);

describe('offeredTargetTypeOptions', () => {
  it.each(['s3_tables', 'jdbc', 's3_parquet', ''])(
    'does not list glue_catalog when the current target type is %j',
    (current) => {
      expect(values(current)).toEqual(['s3_tables', 'jdbc', 's3_parquet']);
    },
  );

  it('lists glue_catalog, in its original position, while it is the current target type', () => {
    expect(values('glue_catalog')).toEqual(['s3_tables', 'glue_catalog', 'jdbc', 's3_parquet']);
  });
});
