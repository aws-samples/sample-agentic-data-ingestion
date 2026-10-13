// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { API_ENDPOINTS } from '../services/api-endpoints';

describe('API Endpoint Registry', () => {
  it('pipeline detail uses singular /pipeline/ path', () => {
    expect(API_ENDPOINTS.pipelines.list).toBe('/pipelines');
    expect(API_ENDPOINTS.pipelines.detail('my-pipe')).toBe('/pipeline/my-pipe');
    expect(API_ENDPOINTS.pipelines.provision).toBe('/pipelines/provision');
    expect(API_ENDPOINTS.pipelines.edit('p1')).toBe('/pipelines/p1/edit');
    expect(API_ENDPOINTS.pipelines.archive('p1')).toBe('/pipelines/p1/archive');
    expect(API_ENDPOINTS.pipelines.restore('p1')).toBe('/pipelines/p1/restore');
    expect(API_ENDPOINTS.pipelines.delete('p1')).toBe('/pipelines/p1/delete');
    expect(API_ENDPOINTS.pipelines.archived).toBe('/pipelines/archived');
  });

  it('run detail uses /runs/{id}/details', () => {
    expect(API_ENDPOINTS.runs.detail('r1')).toBe('/runs/r1/details');
  });

  it('code uses /code/ not /code-versions/', () => {
    expect(API_ENDPOINTS.code.detail('c1')).toBe('/code/c1');
    expect(API_ENDPOINTS.code.testResults('c1')).toBe('/tests/c1');
  });

  it('file detail uses /files/ path', () => {
    expect(API_ENDPOINTS.files.detail('p1', 'f1')).toBe('/files/p1/f1/details');
  });

  it('approvals includes status=pending', () => {
    expect(API_ENDPOINTS.approvals.list).toContain('status=pending');
  });

  it('glue job uses /pipelines/{id}/glue-job not /glue-jobs/', () => {
    expect(API_ENDPOINTS.glueJob.detail('p1')).toBe('/pipelines/p1/glue-job');
    expect(API_ENDPOINTS.glueJob.run('p1')).toBe('/pipelines/p1/glue-job/run');
  });

  it('rerun mapping uses /rerun-mapping not /regenerate-mappings', () => {
    expect(API_ENDPOINTS.rerunMapping).toBe('/rerun-mapping');
  });

  it('metrics stats and resources', () => {
    expect(API_ENDPOINTS.metrics.comprehensive).toBe('/metrics/comprehensive');
    expect(API_ENDPOINTS.stats).toBe('/stats');
    expect(API_ENDPOINTS.resources.bedrockModels).toBe('/bedrock-models');
    expect(API_ENDPOINTS.resources.s3Buckets).toBe('/resources/s3-tables/buckets');
    expect(API_ENDPOINTS.resources.glueDatabases).toBe('/resources/glue/databases');
    // REMOVED by p64 task 1d.2, recorded here rather than dropped silently, per
    // this repo's convention that a deliberate change to an assertion stays
    // legible as a change (`LGR-60c4985-6` / `LGR-9e76249-10`). The line was:
    //
    //     expect(API_ENDPOINTS.upload.presignedUrl).toBe('/upload-file');
    //
    // Both halves were wrong and each confirmed the other. `/upload-file` is not
    // the presign route — `POST /api/upload/presign` is — and this assertion was
    // the entry's ONLY reader anywhere in `src/`, so it pinned the constant to
    // itself and could not detect the constant disagreeing with the backend.
    // 1d.2 deleted the entry rather than correcting it, for the reasons recorded
    // at `services/api-endpoints.ts`'s Upload section; that makes this assertion
    // unreferenceable rather than merely wrong, so it goes with the entry.
    //
    // The live route is covered by an assertion that CAN detect drift:
    // `components/__tests__/FileUploadModal.test.tsx` asserts the request the
    // component actually issues, not the value of a constant.
  });

  it('encodes special characters in path params', () => {
    expect(API_ENDPOINTS.pipelines.detail('a b')).toBe('/pipeline/a%20b');
  });

  it('schemas and mapping hints', () => {
    expect(API_ENDPOINTS.schemas.list).toBe('/schemas');
    expect(API_ENDPOINTS.schemas.detail('s1')).toBe('/schemas/s1');
    expect(API_ENDPOINTS.schemas.upload).toBe('/schemas/upload');
    expect(API_ENDPOINTS.mappingHints.save('p1')).toContain('/pipelines/p1/mapping-hints');
  });
});
