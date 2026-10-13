// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import { describe, it, expect, beforeAll } from 'vitest';
import * as cdk from 'aws-cdk-lib';
import { Template } from 'aws-cdk-lib/assertions';
import { FoundationStack } from '../lib/foundation-stack';
import { DataStack } from '../lib/data-stack';
import { AuthStack } from '../lib/auth-stack';
import { ComputeStack } from '../lib/compute-stack';

/**
 * `schema_extract` carries `PROCESSED_BUCKET`, and it is the same bucket the
 * other three functions get — closing p55 task 4.3's reported finding.
 *
 * WHY THIS EXISTS
 * ---------------
 * p55 added an Excel branch to `schema_extract`: it converts the workbook to CSV
 * and writes the result somewhere the Glue job can read. The destination must be
 * the PROCESSED bucket, never the source bucket, because the source bucket is
 * watched by the S3-upload EventBridge rule — a converted object landing there
 * re-triggers the pipeline on its own output, and it looks like a duplicate
 * upload rather than like a misconfiguration.
 *
 * `PROCESSED_BUCKET` was not set on this function. `compute-stack.ts` sets it on
 * `CodeGeneration`, `CodeTesting` and `DashboardApi`, and `schemaExtraction`'s
 * environment block carried only `STATE_TABLE`, `PIPELINES_TABLE` and
 * `AGENTIC_DATA_INGESTION_ENV`. p55 task 4.3 could not fix that — a
 * `compute-stack.ts` change was outside its file scope — so it handled the
 * absence in Python and reported the rest:
 *
 *   target_bucket = os.environ.get("PROCESSED_BUCKET")
 *       or bucket.replace("adi-source-files-", "adi-processed-files-", 1)
 *
 * That fallback **fails closed**, which is why this was a latent risk and not a
 * live defect: `str.replace` returns its input unchanged when the pattern does
 * not match, so a source bucket without the `adi-source-files-` prefix resolves
 * to itself, and `schema_extract/lambda_function.py` refuses rather than writing
 * into the watched bucket. But it made the Excel path depend on a
 * bucket-name-derivation coincidence holding for every future bucket name.
 *
 * THIS IS A BEHAVIOUR CHANGE AT DEPLOY, STATED RATHER THAN BURIED. Before, the
 * derivation branch was the live path for every conversion. After, the
 * environment variable is, and the derivation goes cold — it stays as the belt to
 * this braces and simply stops being exercised. The in-Python comment at
 * `schema_extract/lambda_function.py:1756-1766` still describes the derivation as
 * "the LIVE path for every conversion, not a fallback", which is now stale; that
 * file is owned elsewhere and the staleness is reported, not edited.
 *
 * WHY THE VALUE IS COMPARED AGAINST A SIBLING, NOT ASSERTED AS A LITERAL
 * ---------------------------------------------------------------------
 * The processed bucket lives in the Foundation stack, so `bucketName` renders as
 * a cross-stack `Fn::ImportValue` rather than a name. Spelling that export name
 * out here would pin a CDK-generated string, and it would pin it in a second
 * place — `LGR-87b8538-1`: "a literal in two files is how the next move
 * manufactures a misleading failure".
 *
 * So the property is EQUALITY WITH THE FUNCTIONS THAT ALREADY HAD IT. That is
 * both stronger and more stable: it catches the real hazard, which is not a
 * missing variable but a variable pointing at the wrong bucket — the source
 * bucket most of all, since `foundation.sourceBucket.bucketName` is one
 * autocomplete away from `foundation.processedBucket.bucketName` and the
 * consequence is the self-retriggering loop the Python guard exists to refuse.
 *
 * WHY THE SYNTHESIZED TEMPLATE, NOT THE SOURCE
 * --------------------------------------------
 * `LGR-1166391-1` (template / ratified) settled this for CDK-shape guards: the
 * template is what CloudFormation hands to the service, and a source-text scan
 * can pass on input that still fails at deploy. It matters more than usual here,
 * because the thing being compared is a resolved cross-stack reference — a text
 * scan can only see the two expressions are spelled differently, not that they
 * resolve to the same bucket. That record also notes `tests/cdk/` cannot render a
 * template at all (`aws-cdk-lib` is absent from `pyproject.toml`), so vitest is
 * the only suite in which this assertion can exist.
 *
 * NO IAM CHANGE IS NEEDED, AND THAT WAS VERIFIED RATHER THAN ASSUMED.
 * `schemaExtractRole` (`foundation-stack.ts:262-264`) grants `s3:GetObject`,
 * `s3:PutObject` and `s3:ListBucket` (S3 Select authorizes as GetObject) on
 * `[sourceBucket, sourceBucket/*, processedBucket, processedBucket/*]` in one
 * statement — `LGR-53765e8-1` records that one-statement-four-resources shape as
 * this file's local convention, citing this very role. The write this variable
 * points at is already permitted. That grant is asserted by
 * `tests/cdk/stacks/` IAM tests and is deliberately not restated here.
 */

const TEST_ACCOUNT = '111111111111';
const TEST_REGION = 'us-west-2';
const TEST_ENV = 'dev';

const SCHEMA_EXTRACT = 'agentic_data_ingestion_schema_extract';

/**
 * The functions that already carried `PROCESSED_BUCKET` before this change, and
 * therefore establish what the correct value looks like.
 *
 * A floor rather than an exact set: another function gaining the variable is not
 * a defect and must not fail this file. What must not happen is `schema_extract`
 * disagreeing with the established value.
 */
const ESTABLISHED_CARRIERS = [
  'agentic_data_ingestion_create_code',
  'agentic_data_ingestion_test_code',
];

const ENV_VAR = 'PROCESSED_BUCKET';

interface RenderedFunction {
  functionName: string;
  environment: Record<string, unknown>;
}

function synthesizeCompute(): Record<string, any> {
  // Bundling disabled, per `LGR-1166391-1` and every sibling file in this
  // directory: bundling is what exhausts the vitest worker, and asset content
  // cannot affect an `Environment.Variables` entry.
  const app = new cdk.App({ context: { 'aws:cdk:bundling-stacks': [] } });
  const env = { account: TEST_ACCOUNT, region: TEST_REGION };

  const foundation = new FoundationStack(app, `AgenticDataIngestion-Foundation-${TEST_ENV}`, {
    env,
    projectName: 'agentic-data-ingestion',
    environment: TEST_ENV,
  });
  const data = new DataStack(app, `AgenticDataIngestion-Data-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });
  const auth = new AuthStack(app, `AgenticDataIngestion-Auth-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
  });
  const compute = new ComputeStack(app, `AgenticDataIngestion-Compute-${TEST_ENV}`, {
    env,
    environment: TEST_ENV,
    projectName: 'agentic-data-ingestion',
    foundation,
    data,
    userPoolArn: auth.userPool.userPoolArn,
  });

  return Template.fromStack(compute).toJSON();
}

/** Every `AWS::Lambda::Function` that declares a `FunctionName`, with its env. */
function namedFunctions(template: Record<string, any>): RenderedFunction[] {
  const functions: RenderedFunction[] = [];
  for (const resource of Object.values(template.Resources ?? {}) as any[]) {
    if (resource.Type !== 'AWS::Lambda::Function') continue;
    const functionName = resource.Properties?.FunctionName;
    if (typeof functionName !== 'string') continue;
    functions.push({
      functionName,
      environment: resource.Properties?.Environment?.Variables ?? {},
    });
  }
  return functions;
}

describe('ComputeStack — schema_extract PROCESSED_BUCKET', () => {
  let functions: RenderedFunction[];
  let schemaExtract: RenderedFunction | undefined;

  beforeAll(() => {
    functions = namedFunctions(synthesizeCompute());
    schemaExtract = functions.find((f) => f.functionName === SCHEMA_EXTRACT);
  }, 300_000);

  it('resolves schema_extract and the functions that establish the expected value', () => {
    // Without this every assertion below compares against `undefined` and either
    // fails confusingly or passes while asserting nothing.
    expect(schemaExtract, `no Lambda named '${SCHEMA_EXTRACT}' in the Compute template`).toBeDefined();

    const present = ESTABLISHED_CARRIERS.filter((name) =>
      functions.some((f) => f.functionName === name && ENV_VAR in f.environment),
    );
    expect(
      present,
      `these functions no longer carry ${ENV_VAR}, so there is nothing left to compare ` +
        `schema_extract's value against: ${ESTABLISHED_CARRIERS.filter((n) => !present.includes(n)).join(', ')}`,
    ).toEqual(ESTABLISHED_CARRIERS);
  });

  it(`sets ${ENV_VAR} on schema_extract`, () => {
    // The variable p55 task 4.3 reported as absent. Without it the Excel
    // conversion path resolves its destination by rewriting the source bucket's
    // name, which is correct for today's naming and is a coincidence rather than
    // a contract.
    expect(
      Object.keys(schemaExtract!.environment).sort(),
      `${ENV_VAR} missing from ${SCHEMA_EXTRACT}'s environment. The Excel branch then falls back ` +
        'to bucket.replace("adi-source-files-", "adi-processed-files-", 1) ' +
        '(schema_extract/lambda_function.py:1766). That fails closed — it refuses rather than ' +
        'writing into the source bucket the S3-upload EventBridge rule watches — so the symptom ' +
        'is a failed conversion, not a pipeline loop. It is still a coincidence holding up a ' +
        'write path.',
    ).toContain(ENV_VAR);
  });

  it(`points at the SAME bucket as the functions that already had ${ENV_VAR}`, () => {
    // THE assertion. Presence is the cheap half; agreement is the half that
    // catches the failure worth catching.
    //
    // Compared as resolved template values, so a cross-stack Fn::ImportValue is
    // matched structurally rather than by re-spelling a CDK-generated export
    // name in a second place (`LGR-87b8538-1`).
    const expected = functions.find((f) => f.functionName === ESTABLISHED_CARRIERS[0])!.environment[ENV_VAR];

    const disagreeing = functions
      .filter((f) => ENV_VAR in f.environment)
      .filter((f) => JSON.stringify(f.environment[ENV_VAR]) !== JSON.stringify(expected))
      .map((f) => `${f.functionName}: ${JSON.stringify(f.environment[ENV_VAR])}`)
      .sort();

    expect(
      disagreeing,
      `these functions disagree with ${ESTABLISHED_CARRIERS[0]} about what ${ENV_VAR} is: ` +
        `${disagreeing.join('; ')}.\nExpected ${JSON.stringify(expected)}. The hazard this ` +
        'catches is not a missing variable but a wrong one: ' +
        'foundation.sourceBucket.bucketName is one autocomplete away from ' +
        'foundation.processedBucket.bucketName, and pointing schema_extract at the SOURCE bucket ' +
        'makes the Excel conversion write into the bucket the S3-upload EventBridge rule ' +
        'watches — the pipeline then re-triggers on its own output, which reads as a duplicate ' +
        'upload rather than as a misconfiguration.',
    ).toEqual([]);
  });

  it(`does not point at the source bucket, named separately because it is the failure that matters`, () => {
    // The equality check above already covers this, but the failure MESSAGE is
    // the value here. A reader who has just pointed this at the wrong bucket
    // needs to be told what happens next, not that two JSON blobs differ.
    //
    // Compared against a real `SOURCE_BUCKET` value from this stack, so this
    // resolves the same cross-stack reference rather than matching on a name
    // substring — the value is an `Fn::ImportValue` whose export name is
    // CDK-generated. Exactly one Compute function publishes `SOURCE_BUCKET`
    // (`compute-stack.ts:1034`), which is enough to give the comparison a
    // reference; the assertion below is non-vacuous today and the floor makes
    // that a checked fact rather than an assumption.
    const sourceBucketValues = functions
      .filter((f) => 'SOURCE_BUCKET' in f.environment)
      .map((f) => JSON.stringify(f.environment.SOURCE_BUCKET));

    expect(
      sourceBucketValues.length,
      'no Compute function publishes SOURCE_BUCKET any more, so this check has nothing to ' +
        'compare against and would pass while asserting nothing. Either find another reference ' +
        'to the source bucket in this template, or delete this test and say why the equality ' +
        'check above is sufficient on its own.',
    ).toBeGreaterThanOrEqual(1);

    expect(
      sourceBucketValues,
      `${SCHEMA_EXTRACT}'s ${ENV_VAR} resolves to the SOURCE bucket. The Excel conversion would ` +
        'write its output into the bucket the S3-upload EventBridge rule watches, so the ' +
        'pipeline re-triggers on its own output. schema_extract/lambda_function.py refuses this ' +
        'at runtime (target_bucket == bucket), so the symptom is every Excel conversion failing ' +
        'with a message about the source bucket — not a loop.',
    ).not.toContain(JSON.stringify(schemaExtract!.environment[ENV_VAR]));
  });
});
