// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
/**
 * Which write modes a target's writer implements, for the editors that offer
 * a choice.
 *
 * The server is the authority: `shared/write_modes.py` refuses an unsupported
 * pair at create, at edit and at code generation. This mirrors it so the Edit
 * Pipeline dialog never offers an option the server will refuse. If the two
 * ever drift, the cost is a 400 with a reason, not a wrong write.
 *
 * Only S3 Tables implements `merge` (`MERGE INTO` on `merge_key`). Before
 * 2026-10-10 the dialog offered "Merge (Upsert)" for every target, and a JDBC,
 * S3 Parquet or Glue Catalog pipeline switched to it ran append, overwrite or a
 * plain catalog write instead, reporting success.
 */

/** Persisted `target_config.type` spellings of an S3 Tables target, lower-cased. */
const S3_TABLES_TARGET_TYPES = new Set([
  's3_tables',
  'new_s3_table_bucket',
  'existing_s3_table_bucket',
  's3_table_bucket',
]);

/** Whether the writer for `targetType` implements `merge`. */
export function targetSupportsMerge(targetType: unknown): boolean {
  return typeof targetType === 'string' && S3_TABLES_TARGET_TYPES.has(targetType.trim().toLowerCase());
}
