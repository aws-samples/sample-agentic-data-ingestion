// Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
// SPDX-License-Identifier: MIT-0
import * as cdk from 'aws-cdk-lib';
import * as wafv2 from 'aws-cdk-lib/aws-wafv2';
import { Construct } from 'constructs';

export interface EdgeWafStackProps extends cdk.StackProps {
  environment: string;
}

/**
 * Dedicated us-east-1 stack for the CLOUDFRONT-scope WAF web ACL.
 *
 * A `CLOUDFRONT`-scope WebACL MUST be created in us-east-1 (CloudFront is a
 * global service whose WAF associations live in us-east-1), so it cannot be
 * created inline in the Hosting stack which deploys to the application region
 * (e.g. us-west-2). The Hosting stack consumes `webAclArn` via cross-region
 * references (`crossRegionReferences: true` on both stacks).
 */
export class EdgeWafStack extends cdk.Stack {
  /** ARN of the CLOUDFRONT-scope WAF web ACL, for the CloudFront distribution. */
  public readonly webAclArn: string;

  constructor(scope: Construct, id: string, props: EdgeWafStackProps) {
    super(scope, id, props);

    const { environment } = props;

    const webAcl = new wafv2.CfnWebACL(this, 'CloudFrontWebAcl', {
      scope: 'CLOUDFRONT',
      defaultAction: { allow: {} },
      name: `agentic-data-ingestion-${environment}-cloudfront-waf`,
      description: 'WAF for CloudFront distribution - managed rules + rate limiting',
      visibilityConfig: {
        cloudWatchMetricsEnabled: true,
        metricName: `agentic-data-ingestion-${environment}-cloudfront-waf`,
        sampledRequestsEnabled: true,
      },
      rules: [
        // AWS Managed Common Rule Set
        {
          name: 'AWSManagedRulesCommonRuleSet',
          priority: 1,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesCommonRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesCommonRuleSet',
            sampledRequestsEnabled: true,
          },
        },
        // AWS Managed Known Bad Inputs Rule Set
        {
          name: 'AWSManagedRulesKnownBadInputsRuleSet',
          priority: 2,
          overrideAction: { none: {} },
          statement: {
            managedRuleGroupStatement: {
              vendorName: 'AWS',
              name: 'AWSManagedRulesKnownBadInputsRuleSet',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'AWSManagedRulesKnownBadInputsRuleSet',
            sampledRequestsEnabled: true,
          },
        },
        // Rate-based rule: 2000 requests per 5 minutes per IP
        {
          name: 'RateLimitPerIP',
          priority: 3,
          action: { block: {} },
          statement: {
            rateBasedStatement: {
              limit: 2000,
              aggregateKeyType: 'IP',
            },
          },
          visibilityConfig: {
            cloudWatchMetricsEnabled: true,
            metricName: 'RateLimitPerIP',
            sampledRequestsEnabled: true,
          },
        },
      ],
    });

    this.webAclArn = webAcl.attrArn;

    // ── CfnOutput ──────────────────────────────────────────────────
    new cdk.CfnOutput(this, 'CloudFrontWebAclArn', {
      value: this.webAclArn,
      exportName: `${this.stackName}-CloudFrontWebAclArn`,
      description: 'ARN of the CLOUDFRONT-scope WAF web ACL (us-east-1)',
    });
  }
}
