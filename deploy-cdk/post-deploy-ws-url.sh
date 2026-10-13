#!/bin/bash
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
# Post-deploy: fetch WebSocket API endpoint from CloudFormation and write to dashboard .env
#
# Usage: ./post-deploy-ws-url.sh [environment]
#   environment: dev (default), staging, prod

ENV="${1:-dev}"
STACK_NAME="AgenticDataIngestion-Compute-${ENV}"
REGION="${AWS_REGION:-us-west-2}"
DASHBOARD_ENV="../dashboard-react/.env"

echo "Fetching WebSocket endpoint from stack: ${STACK_NAME}..."

WS_URL=$(aws cloudformation describe-stacks \
  --stack-name "$STACK_NAME" \
  --region "$REGION" \
  --query "Stacks[0].Outputs[?contains(OutputKey,'WsApiEndpoint')].OutputValue" \
  --output text 2>/dev/null)

if [ -z "$WS_URL" ] || [ "$WS_URL" = "None" ]; then
  echo "⚠️  Could not find WebSocket endpoint in stack ${STACK_NAME}"
  echo "   Make sure you've deployed: npx cdk deploy ${STACK_NAME} --require-approval never"
  exit 1
fi

echo "Found WebSocket endpoint: ${WS_URL}"

# Update or create the dashboard .env
if [ -f "$DASHBOARD_ENV" ]; then
  if grep -q "VITE_WS_URL=" "$DASHBOARD_ENV"; then
    sed -i '' "s|VITE_WS_URL=.*|VITE_WS_URL=${WS_URL}|" "$DASHBOARD_ENV"
    echo "✅ Updated VITE_WS_URL in ${DASHBOARD_ENV}"
  else
    echo "VITE_WS_URL=${WS_URL}" >> "$DASHBOARD_ENV"
    echo "✅ Added VITE_WS_URL to ${DASHBOARD_ENV}"
  fi
else
  echo "VITE_WS_URL=${WS_URL}" > "$DASHBOARD_ENV"
  echo "✅ Created ${DASHBOARD_ENV} with VITE_WS_URL"
fi

echo ""
echo "Restart the Vite dev server to pick up the new URL."
