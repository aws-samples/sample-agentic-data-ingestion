// SPDX-License-Identifier: MIT-0
/**
 * What a resource dropdown says when its list loaded and holds nothing.
 *
 * Passed as the third argument of `selectAsyncStatus`. Each statement says what
 * was searched, because "nothing found" is only useful when the reader can tell
 * whether the search covered the thing they expected. The scope stated here is
 * the backend's actual filter, read from its handler:
 *
 * - RDS instances and Aurora clusters: only status `available`
 *   (`dashboard_api/handlers/rds_resources.py`, `list_rds_instances`,
 *   `list_rds_clusters`).
 * - Secrets: only names matching `db|database|rds|jdbc|postgres|mysql|oracle|
 *   sqlserver`, case-insensitive (`SECRET_NAME_PATTERN`,
 *   `JDBC_CANDIDATE_PATTERN` in the same file). Change the word list here when
 *   that pattern changes.
 * - Glue connections: only `ConnectionType` JDBC, minus the connections this
 *   app creates for its own pipelines and schema detection
 *   (`list_glue_jdbc_connections`, `is_managed_glue_connection` in
 *   `shared/jdbc_helpers.py`).
 * - Bedrock models: only model ids containing `claude` or `amazon`
 *   (`handlers/bedrock_models.py`).
 *
 * Next steps are words, never a control. A Retry button on a successful empty
 * load would suggest a failure (LGR-2bcdd0d-3); where the form offers another
 * way forward, the statement names it.
 *
 * The Glue Data Catalog statements live in `glue-target-eligibility.ts`.
 */

const DATABASE_SECRET_WORDS = 'db, database, rds, jdbc, postgres, mysql, oracle or sqlserver';

/** "Select RDS Instance" mode, source and target. */
export const NO_RDS_DATABASES =
  'No available RDS instances or Aurora clusters were found in this account and Region. ' +
  'If the database is elsewhere, choose Use Glue Connection or External or self-managed database above.';

/** The secret picker in "Select RDS Instance" mode, which has no paste-an-ARN alternative. */
export const NO_RDS_SECRETS =
  'No secrets with a database-related name were found in this account and Region. ' +
  `This list shows only secrets whose name contains ${DATABASE_SECRET_WORDS}.`;

/** The secret picker in "External or self-managed database" mode, which has the paste toggle. */
export const NO_JDBC_CANDIDATE_SECRETS =
  'No secrets with a database-related name were found in this account and Region. ' +
  `This list shows only secrets whose name contains ${DATABASE_SECRET_WORDS}. ` +
  'To use another secret, turn on Paste ARN manually.';

/** "Use Glue Connection" mode. */
export const NO_GLUE_JDBC_CONNECTIONS =
  'No Glue JDBC connections were found in this account and Region. ' +
  'Connections this app creates for its own pipelines are not listed. ' +
  'Choose Select RDS Instance or External or self-managed database above instead.';

export const NO_VPCS = 'No VPCs were found in this account and Region.';

export function noSubnetsEmpty(vpcId: string): string {
  return `No subnets were found in ${vpcId}.`;
}

export function noSecurityGroupsEmpty(vpcId: string): string {
  return `No security groups were found in ${vpcId}.`;
}

export const NO_S3_TABLE_BUCKETS = 'No S3 table buckets were found in this account and Region.';

/** The wizard offers a "Create new" tile beside "Use existing"; the edit modal does not. */
export const NO_S3_TABLE_BUCKETS_WIZARD_NEXT_STEP = 'Choose Create new to define one.';

export function noS3TablesEmpty(bucket: string): string {
  return `No tables were found in ${bucket}.`;
}

export const NO_BEDROCK_MODELS = 'No Claude or Amazon models were found in this Region.';
