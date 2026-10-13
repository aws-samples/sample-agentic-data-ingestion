// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as cognito from 'aws-cdk-lib/aws-cognito';
import { Construct } from 'constructs';
import * as ssm from 'aws-cdk-lib/aws-ssm';

export interface AuthStackProps extends cdk.StackProps {
  environment: string;
  /**
   * SSM parameter to write the e2e app client ID to, when that client is
   * deployed, so the WebSocket authorizer accepts its tokens (R-05).
   */
  e2eClientIdParameterName?: string;
}

export class AuthStack extends cdk.Stack {
  public readonly userPool: cognito.UserPool;
  public readonly userPoolDomain: cognito.UserPoolDomain;
  /** The e2e harness's app client; undefined unless `-c adi:deployE2eClient=true`. */
  public readonly e2eClient?: cognito.UserPoolClient;

  constructor(scope: Construct, id: string, props: AuthStackProps) {
    super(scope, id, props);

    const { environment } = props;

    // ── Cognito User Pool ──────────────────────────────────────────
    // MFA is REQUIRED by default (TOTP; SMS stays disabled), security review S3.
    // The one thing pool-level REQUIRED breaks is the e2e harness: its
    // USER_PASSWORD_AUTH sign-in gets an MFA_SETUP challenge. That harness's app
    // client only exists with `-c adi:deployE2eClient=true` (LGR-a65b594-3,
    // below), so MFA follows that flag: OPTIONAL exactly when the e2e client is
    // deployed, REQUIRED otherwise. It used to be OPTIONAL for every deployment
    // to keep a test client working that most deployments never create.
    //
    // `-c adi:requireMfa` overrides the derived value in either direction:
    // `true` forces REQUIRED (the e2e harness then cannot sign in), `false`
    // forces OPTIONAL (e.g. a throwaway demo that should skip TOTP enrollment).
    //
    // Changing this on an existing pool is an in-place update. Users who have
    // not enrolled TOTP are asked to at their next Hosted UI sign-in.
    const deployE2eClient =
      this.node.tryGetContext('adi:deployE2eClient') === 'true' ||
      this.node.tryGetContext('adi:deployE2eClient') === true;
    const requireMfaContext = this.node.tryGetContext('adi:requireMfa');
    const requireMfa =
      requireMfaContext === 'true' || requireMfaContext === true
        ? true
        : requireMfaContext === 'false' || requireMfaContext === false
          ? false
          : !deployE2eClient;

    this.userPool = new cognito.UserPool(this, 'UserPool', {
      userPoolName: `agentic-data-ingestion-${environment}-users`,
      selfSignUpEnabled: false,
      signInAliases: { email: true },
      autoVerify: { email: true },
      accountRecovery: cognito.AccountRecovery.EMAIL_ONLY,
      mfa: requireMfa ? cognito.Mfa.REQUIRED : cognito.Mfa.OPTIONAL,
      mfaSecondFactor: {
        sms: false,
        otp: true,
      },
      // Hardened per external security review: 16-char minimum and a 24-hour
      // temporary-password window (admin-created users must sign in and set a
      // permanent password within a day).
      passwordPolicy: {
        minLength: 16,
        requireLowercase: true,
        requireUppercase: true,
        requireDigits: true,
        requireSymbols: true,
        tempPasswordValidity: cdk.Duration.hours(24),
      },
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });

    // ── Hosted UI Domain (Cognito prefix) ──────────────────────────
    // The prefix embeds the account ID because Cognito domain prefixes are
    // globally unique across ALL AWS accounts — without it, two accounts
    // deploying the same environment name collide (P27 Req 5).
    // `this.account` is concrete: the bin app validates CDK_DEFAULT_ACCOUNT
    // via resolveEnv() before any stack is constructed (P27 Req 5.4/7.1).
    //
    // NOTE (upgrade from pre-P27 deployments): changing the prefix REPLACES
    // the UserPoolDomain. A user pool allows only one domain and CloudFormation
    // creates the new one before deleting the old, so updating an existing
    // environment fails unless the old domain is deleted first:
    //   aws cognito-idp delete-user-pool-domain \
    //     --domain agentic-data-ingestion-<env> --user-pool-id <id>
    // See README.md deployment notes. Fresh accounts are unaffected.
    const domainPrefix = `agentic-data-ingestion-${this.account}-${environment}`;
    if (domainPrefix.length > 63) {
      throw new Error(
        `Cognito domain prefix '${domainPrefix}' is ${domainPrefix.length} chars (max 63). ` +
        `Shorten the environment name '${environment}' (max ${63 - `agentic-data-ingestion-${this.account}-`.length} chars).`,
      );
    }
    this.userPoolDomain = this.userPool.addDomain('HostedUiDomain', {
      cognitoDomain: { domainPrefix },
    });

    // ── E2E test app client (programmatic token path) ──────────────
    // The dashboard app client (Hosting stack) only allows the Hosted UI
    // authorization-code flow, which requires a browser. The e2e harness
    // needs tokens programmatically, so this dedicated client enables
    // USER_PASSWORD_AUTH. Scope-wise it is no more privileged than the
    // dashboard client — the tokens it issues are ordinary user-pool JWTs
    // gated by the same 16-char password policy, and self-signup stays
    // disabled (test users are admin-created and deleted by the harness).
    //
    // Created only with `-c adi:deployE2eClient=true` (threat model R-05). A
    // password-auth client is attack surface that only the e2e suite needs, so
    // a deployment that never runs it should not carry one. Deploying without
    // the flag over a stack that has the client DELETES it; redeploy with the
    // flag before running tests/e2e (docs/TESTING.md). `deployE2eClient` is read
    // at the user pool above, because the same flag makes MFA OPTIONAL there.
    if (deployE2eClient) {
      this.e2eClient = new cognito.UserPoolClient(this, 'E2eTestClient', {
        userPool: this.userPool,
        userPoolClientName: `agentic-data-ingestion-${environment}-e2e`,
        generateSecret: false,
        authFlows: { userPassword: true },
        accessTokenValidity: cdk.Duration.hours(1),
        idTokenValidity: cdk.Duration.hours(1),
      });
      // The e2e WebSocket test connects with this client's ID token, so the
      // authorizer must accept it; it reads this parameter at run time.
      if (props.e2eClientIdParameterName) {
        new ssm.StringParameter(this, 'E2eClientIdParameter', {
          parameterName: props.e2eClientIdParameterName,
          stringValue: this.e2eClient.userPoolClientId,
          description: 'E2E app client ID, read by the WebSocket $connect authorizer',
        });
      }
    }

    // ── CfnOutputs ─────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'UserPoolId', {
      value: this.userPool.userPoolId,
      exportName: `${this.stackName}-UserPoolId`,
      description: 'Cognito User Pool ID',
    });

    new cdk.CfnOutput(this, 'UserPoolArn', {
      value: this.userPool.userPoolArn,
      exportName: `${this.stackName}-UserPoolArn`,
      description: 'Cognito User Pool ARN',
    });

    new cdk.CfnOutput(this, 'UserPoolProviderUrl', {
      value: this.userPool.userPoolProviderUrl,
      exportName: `${this.stackName}-UserPoolProviderUrl`,
      description: 'Cognito User Pool issuer URL',
    });

    new cdk.CfnOutput(this, 'HostedUiDomain', {
      value: this.userPoolDomain.baseUrl(),
      exportName: `${this.stackName}-HostedUiDomain`,
      description: 'Cognito Hosted UI domain URL',
    });

    if (this.e2eClient) {
      new cdk.CfnOutput(this, 'E2eTestClientId', {
        value: this.e2eClient.userPoolClientId,
        exportName: `${this.stackName}-E2eTestClientId`,
        description: 'App client (USER_PASSWORD_AUTH) used by the e2e test harness',
      });
    }
  }
}
