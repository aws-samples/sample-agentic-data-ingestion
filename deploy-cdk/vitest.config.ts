// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { defineConfig } from 'vitest/config';

export default defineConfig({
  test: {
    include: ['test/**/*.test.ts'],
    exclude: ['cdk.out/**', 'node_modules/**'],
    // vitest 4 enforces this on long synchronous CDK synths; vitest 2 did not (slowest passing test ~22 s).
    testTimeout: 60000,
    // The same limit for beforeAll/afterAll, which default to 10 s. Many files
    // synthesize their stacks in a beforeAll; four of them (agentcore-runtime-roles,
    // hosting-auth, p28b-approval-config-cloudwatch, p64-source-bucket-cors)
    // passed alone in 6.5 s but timed out at 10 s in a full run, contended by
    // the files that pip-bundle the Powertools layer.
    hookTimeout: 60000,
  },
});
