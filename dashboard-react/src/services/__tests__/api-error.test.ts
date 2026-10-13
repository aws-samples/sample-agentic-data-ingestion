// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect } from 'vitest';
import { ApiError, redactErrorDetail } from '../api-client';

describe('ApiError', () => {
  it('is an instance of Error', () => {
    const err = new ApiError(400, { message: 'bad request' });
    expect(err).toBeInstanceOf(Error);
    expect(err).toBeInstanceOf(ApiError);
  });

  it('has correct status and body properties', () => {
    const body = { error: 'not found', detail: 'resource missing' };
    const err = new ApiError(404, body);
    expect(err.status).toBe(404);
    expect(err.body).toBe(body);
    expect(err.name).toBe('ApiError');
  });

  it('extracts message from body.message', () => {
    const err = new ApiError(500, { message: 'Internal server error' });
    expect(err.message).toBe('Internal server error');
  });

  it('extracts message from body.error (dashboard handler shape)', () => {
    // Most dashboard handlers return `{"error": "..."}` via
    // shared/response_utils.create_response. Surfacing that string (rather
    // than a generic "API error 400") is what lets the create-pipeline
    // wizard toast show the real validation reason.
    const err = new ApiError(400, { error: 'jdbc_table is required' });
    expect(err.message).toBe('jdbc_table is required');
  });

  it('prefers body.error over the generic fallback', () => {
    const err = new ApiError(403, { error: 'forbidden' });
    expect(err.message).toBe('forbidden');
  });

  it('prefers body.message when both message and error are present', () => {
    const err = new ApiError(400, { message: 'msg wins', error: 'err loses' });
    expect(err.message).toBe('msg wins');
  });

  it('falls back to generic message for non-object body', () => {
    const err = new ApiError(502, 'gateway timeout');
    expect(err.message).toBe('API error 502');
  });

  it('falls back to generic message for null body', () => {
    const err = new ApiError(500, null);
    expect(err.message).toBe('API error 500');
  });

  it('falls back to generic message when neither key is a string', () => {
    const err = new ApiError(400, { error: 123, message: { nested: true } });
    expect(err.message).toBe('API error 400');
  });
});

/**
 * `body.details` composition.
 *
 * `handle_error` (`shared/response_utils.py:94`) returns `{error, details}`,
 * where `error` is a fixed sentence the handler chose and `details` is
 * `str(exception)` — the actual AWS error code and message. Reading only `error`
 * is why a tester saw "Failed to create discovery crawler" while the cause sat
 * unread in the same response body.
 */
describe('ApiError — details composition', () => {
  it('appends body.details to the handler sentence', () => {
    const err = new ApiError(500, {
      error: 'Failed to create discovery crawler',
      details:
        'An error occurred (AccessDeniedException) when calling the CreateCrawler operation',
    });
    expect(err.message).toBe(
      'Failed to create discovery crawler (An error occurred (AccessDeniedException) ' +
        'when calling the CreateCrawler operation)',
    );
  });

  it('uses details alone when neither error nor message is present', () => {
    const err = new ApiError(500, { details: 'InternalServiceException' });
    expect(err.message).toBe('InternalServiceException');
  });

  it('does not duplicate details when it equals the chosen sentence', () => {
    const err = new ApiError(500, { error: 'same text', details: 'same text' });
    expect(err.message).toBe('same text');
  });

  it('ignores a blank or non-string details', () => {
    expect(new ApiError(500, { error: 'e', details: '   ' }).message).toBe('e');
    expect(new ApiError(500, { error: 'e', details: 42 }).message).toBe('e');
  });

  it('leaves body untouched so callers reading body.error keep the raw sentence', () => {
    // `utils/schema-delete-failure.ts` reads `body.error` directly rather than
    // `err.message`, and must keep getting the handler's own sentence verbatim.
    const body = { error: 'Failed to delete schema', details: 'AccessDenied' };
    const err = new ApiError(500, body);
    expect(err.body).toBe(body);
    expect((err.body as Record<string, unknown>).error).toBe('Failed to delete schema');
  });
});

/**
 * Redaction. Promoting `details` to a user-visible surface widens what an
 * operator can see, and would widen what a credential can escape through if it
 * went unfiltered. `details` is produced at every `handle_error` call site, none
 * of which was written with an operator-visible `details` in mind.
 */
describe('redactErrorDetail', () => {
  it('replaces a ConnectionProperties password value', () => {
    const out = redactErrorDetail('Invalid params: {"PASSWORD": "hunter2", "HOST": "db"}');
    expect(out).not.toContain('hunter2');
    expect(out).toContain('***');
    // The diagnostic survives.
    expect(out).toContain('HOST');
  });

  it('replaces USERNAME and SECRET_ID values', () => {
    const out = redactErrorDetail('USERNAME=admin SECRET_ID=some/secret/path');
    expect(out).not.toContain('admin');
    expect(out).not.toContain('some/secret/path');
  });

  it('drops a secret ARN name but keeps region and account', () => {
    const out = redactErrorDetail(
      'arn:aws:secretsmanager:us-west-2:123456789012:secret:db-creds-AbCdEf not found',
    );
    expect(out).not.toContain('db-creds-AbCdEf');
    // Region is the live diagnostic for a wrong-region Glue connection.
    expect(out).toContain('us-west-2');
    expect(out).toContain('123456789012');
  });

  it('replaces presigned-URL signing parameters only', () => {
    const out = redactErrorDetail(
      'https://b.s3.amazonaws.com/k?X-Amz-Credential=AKIAIOSFODNN7/x' +
        '&X-Amz-Signature=abc123def&X-Amz-Expires=900',
    );
    expect(out).not.toContain('abc123def');
    expect(out).not.toContain('AKIAIOSFODNN7');
    expect(out).toContain('X-Amz-Expires=900');
  });

  it('leaves an ordinary AWS error message unchanged', () => {
    const message =
      'An error occurred (AccessDeniedException) when calling the CreateCrawler ' +
      'operation: User is not authorized to perform: iam:PassRole';
    expect(redactErrorDetail(message)).toBe(message);
  });

  it('redacts through the ApiError constructor, not just standalone', () => {
    const err = new ApiError(500, {
      error: 'Failed to create Glue Connection',
      details: 'Invalid params: {"PASSWORD": "hunter2"}',
    });
    expect(err.message).not.toContain('hunter2');
  });
});
