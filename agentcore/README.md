# AgentCore Runtime Containers

## Overview

Four AgentCore Runtime containers replace the Lambda-based agent execution for pipeline steps 5-8. Five additional agent containers (Phase 13) provide autonomous capabilities. Each container runs a `BedrockAgentCoreApp` HTTP server.

## Container Structure

```
agentcore/
├── create_code/              # Step 5: Code Generation
│   ├── main.py               # BedrockAgentCoreApp entrypoint
│   ├── Dockerfile            # Python 3.12-slim + dependencies
│   └── requirements.txt      # bedrock-agentcore, strands-agents, jinja2, etc.
├── test_code/                # Step 6: Code Testing
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── create_glue_pipeline/     # Step 8: Glue Pipeline Creation
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── create_infrastructure/    # Step 7: Infrastructure Provisioning
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── self_healing_agent/       # P13: Auto-diagnose and fix failed Glue jobs
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── complex_mapping_agent/    # P13: Evidence-based schema mapping with sample data
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── data_quality_agent/       # P13: Post-run quality checks via Athena
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── cost_optimization_agent/  # P13: Weekly DPU utilization analysis
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
└── nl_pipeline_builder/      # P13: Natural language pipeline creation
    ├── main.py
    ├── Dockerfile
    └── requirements.txt
```

## How It Works

### Proxy Pattern

```
Step Functions → LambdaInvoke(proxy) → HTTP POST → AgentCore Runtime (warm container)
                     │                                      │
                     │  USE_AGENTCORE_RUNTIME=true           │  @app.entrypoint
                     │  → agentcore_proxy.invoke_runtime()   │  → handler(payload)
                     │                                      │
                     │  USE_AGENTCORE_RUNTIME=false          │
                     └─ → original inline handler            │
```

When `USE_AGENTCORE_RUNTIME=true` (default), the Lambda forwards the payload to the AgentCore Runtime endpoint via SigV4-signed HTTP POST (`shared/agentcore_proxy.py`). When `false`, the Lambda executes the original inline handler code.

### Container Entrypoint

Each `main.py` follows this pattern:

```python
from bedrock_agentcore.runtime import BedrockAgentCoreApp

app = BedrockAgentCoreApp()

# MCP client initialized ONCE at startup (reused across invocations)
agent, mcp_client = initialize_strands_agent_with_mcp(system_prompt=SYSTEM_PROMPT)

@app.entrypoint
def handler(payload):
    # Same logic as the Lambda handler, but:
    # - agent and mcp_client are reused (not created per invocation)
    # - No 15-minute timeout constraint
    # - Container stays warm between invocations
    return result

if __name__ == "__main__":
    app.run()
```

### Benefits Over Lambda

| Aspect | Lambda | AgentCore Runtime |
|--------|--------|-------------------|
| Cold start | ~3s (150MB Strands layer) | ~0s (warm container) |
| Timeout | 900s (15 min max) | Unlimited |
| MCP connection | New per invocation | Reused across invocations |
| Memory | 2048MB fixed | Configurable |

## Local Development

### Build a container

```bash
docker build -t agentic-data-ingestion-create-code:local agentcore/create_code/
```

### Run locally

```bash
docker run -p 8080:8080 \
  -e AWS_REGION=us-west-2 \
  -e AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID \
  -e AWS_SECRET_ACCESS_KEY=$AWS_SECRET_ACCESS_KEY \
  agentic-data-ingestion-create-code:local
```

### Test with curl

```bash
# Health check
curl http://localhost:8080/ping

# Invoke with a test payload
curl -X POST http://localhost:8080/invoke \
  -H "Content-Type: application/json" \
  -d '{
    "run_id": "test-run-001",
    "pipeline_id": "test-pipeline",
    "regeneration_attempt": 0
  }'
```

## Deployment

### Build and push to ECR

```bash
# Authenticate to ECR
aws ecr get-login-password --region us-west-2 | \
  docker login --username AWS --password-stdin $ACCOUNT.dkr.ecr.us-west-2.amazonaws.com

# Build and push each container
for agent in create_code test_code create_glue_pipeline create_infrastructure self_healing_agent complex_mapping_agent data_quality_agent cost_optimization_agent nl_pipeline_builder; do
  docker build -t agentic-data-ingestion-$agent:latest agentcore/$agent/
  docker tag agentic-data-ingestion-$agent:latest $ACCOUNT.dkr.ecr.us-west-2.amazonaws.com/agentic-data-ingestion-agentcore:$agent-dev
  docker push $ACCOUNT.dkr.ecr.us-west-2.amazonaws.com/agentic-data-ingestion-agentcore:$agent-dev
done
```

### Deploy via CDK

The `agentcore-stack.ts` CDK stack creates AgentCore Runtime resources for each container. Deploy with:

```bash
cd deploy-cdk
npx cdk deploy AgenticDataIngestion-AgentCore-dev --require-approval never
```

### Enable/disable Runtime routing

`USE_AGENTCORE_RUNTIME` defaults to `true` in `compute-stack.ts`. To disable (run agents inline in Lambda):

```bash
# Disable for one Lambda at a time
aws lambda update-function-configuration \
  --function-name agentic_data_ingestion_create_code \
  --environment "Variables={USE_AGENTCORE_RUNTIME=false}" \
  --region us-west-2
```

**Important**: Code changes to agent Lambdas require deploying BOTH the Compute AND AgentCore stacks:
```bash
cd deploy-cdk
npx cdk deploy AgenticDataIngestion-Compute-dev AgenticDataIngestion-AgentCore-dev --require-approval never
```

## Shared Dependencies

All containers share these modules (copied from `shared/` and `lambda/` during Docker build):

| Module | Purpose |
|--------|---------|
| `strands_mcp_factory.py` | MCP client initialization |
| `env_utils.py` | Environment variable helpers |
| `prompt_template_loader.py` | Jinja2 prompt templates |
| `artifact_schema.py` | Output data schema |
| `metrics_facade.py` | CloudWatch metrics |
| `eventbridge_utils.py` | EventBridge publishers |
| `agent_trace_plugin.py` | P13a: Strands hook for model/tool call tracing |
| `audit_utils.py` | P13: Agent audit trail writer |
| `mapping_memory.py` | P12: AgentCore Memory client for mapping history |

## Tests

```bash
# Container structure and Dockerfile validation
python -m pytest tests/unit/agentcore/ -v

# Proxy function tests
python -m pytest tests/unit/shared/test_agentcore_proxy.py -v
```
