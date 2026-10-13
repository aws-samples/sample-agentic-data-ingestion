// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';

/**
 * Resolve the CDK deployment environment (account + region) from standard
 * environment variables, failing fast at synth with actionable messages.
 *
 * Rationale (P27, Requirement 7): without a concrete account, synth appears
 * to succeed for individual stacks but the EdgeWaf/Hosting cross-region
 * reference pair fails minutes into a deploy with an opaque error
 * ("cross stack references are only supported..."). This check converts that
 * into a 2-second synth failure that names the variable and the fix.
 *
 * The account must also be concrete (12-digit) before the Auth stack is
 * constructed: the Cognito Hosted UI domain prefix embeds the account ID and
 * must resolve to a plain string at synth (P27, Requirement 5.4).
 */
export function resolveEnv(
  env: NodeJS.ProcessEnv = process.env,
): Required<Pick<cdk.Environment, 'account' | 'region'>> {
  const account = env.CDK_DEFAULT_ACCOUNT;
  if (!account || !/^\d{12}$/.test(account)) {
    throw new Error(
      `CDK_DEFAULT_ACCOUNT is ${account ? `set to '${account}', which is not a 12-digit AWS account ID` : 'not set'}. ` +
      'Set it from your credentials, e.g.:\n' +
      '  export CDK_DEFAULT_ACCOUNT=$(aws sts get-caller-identity --query Account --output text)\n' +
      'or run ./deploy.sh, which resolves it automatically. ' +
      'See README.md § Setup → Configure AWS credentials.',
    );
  }

  const region = env.CDK_DEFAULT_REGION ?? env.AWS_REGION ?? 'us-west-2';
  if (!region) {
    throw new Error(
      'No AWS region resolved. Set CDK_DEFAULT_REGION (or AWS_REGION), e.g.:\n' +
      '  export CDK_DEFAULT_REGION=us-west-2\n' +
      'See README.md § Setup → Configure AWS credentials.',
    );
  }

  return { account, region };
}
