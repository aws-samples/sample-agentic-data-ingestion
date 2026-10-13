# Shared Utilities

Reusable Python modules for Agentic Data Ingestion Lambda functions providing standardized monitoring, metrics, and integration capabilities.

## Modules

### cloudwatch_metrics.py

CloudWatch custom metrics publisher for pipeline monitoring.

**Classes:**
- `Agentic Data IngestionMetrics` - Core metrics publisher with batching and timing decorators
- `ApprovalMetrics` - Approval workflow metrics (created, processed, timeout)
- `PipelineMetrics` - Pipeline execution metrics (stage duration, throughput, error rate)
- `DashboardMetrics` - API performance metrics (response time, success rate)
- `CostMetrics` - Cost tracking for Lambda, DynamoDB, and MCP operations

**Usage:**
```python
from shared.cloudwatch_metrics import metrics, PipelineMetrics

# Time an operation
with metrics.timer('MyOperation', {'Stage': 'Extract'}):
    do_work()

# Record pipeline stage
PipelineMetrics.record_stage_execution(
    stage_name='schema_extract',
    pipeline_id='pipe-123',
    status='completed',
    duration_ms=1500
)
```

### eventbridge_publisher.py

EventBridge event publishing for pipeline state changes and approvals.

**Functions:**
- `publish_pipeline_state_change()` - Pipeline stage status updates
- `publish_approval_status_change()` - Approval workflow events
- `publish_mcp_operation_event()` - MCP operation performance events

**Usage:**
```python
from shared.eventbridge_publisher import publish_pipeline_state_change

publish_pipeline_state_change(
    pipeline_id='pipe-123',
    execution_id='exec-456',
    stage='agentic_data_ingestion_schema_extract',
    status='completed',
    metrics={'records_processed': 1000}
)
```

### glue_metrics.py

Specialized CloudWatch metrics for Glue job monitoring.

**Functions:**
- `publish_glue_job_metrics()` - Job execution metrics (records/sec, cost, DPU hours)
- `publish_mcp_recommendation_accuracy()` - MCP recommendation tracking
- `publish_glue_job_creation_metrics()` - Job creation success/failure
- `publish_glue_job_state_change()` - Job state transitions
- `calculate_dpu_hours()` - DPU calculation by worker type

**Usage:**
```python
from shared.glue_metrics import publish_glue_job_metrics

publish_glue_job_metrics(
    pipeline_id='pipe-123',
    job_name='agentic-data-ingestion-etl-job',
    records_processed=100000,
    execution_duration_seconds=120,
    estimated_cost=0.88,
    worker_type='G.1X',
    num_workers=2
)
```

### mcp_integration.py

MCP client with circuit breaker patterns and automatic fallback to boto3.

**Classes:**
- `CircuitBreaker` - Circuit breaker implementation (CLOSED/OPEN/HALF_OPEN states)
- `MCPClient` - MCP client with caching, cost tracking, and fallback mechanisms

**Key Features:**
- Circuit breakers for different operation types (aws_operations, code_generation, optimization)
- Response caching with configurable TTL
- Daily cost threshold tracking
- Automatic fallback to boto3 when MCP fails

**Usage:**
```python
from shared.mcp_integration import mcp_client, with_mcp_fallback

# Direct usage
result = mcp_client.optimize_glue_job('my-job', data_size='large')

# Decorator for automatic fallback
@with_mcp_fallback('aws_operations')
def my_operation():
    # MCP operation with automatic fallback
    pass
```

### mcp_logger.py

Comprehensive MCP tool call logging and metrics tracking.

**Classes:**
- `MCPToolCall` - Single tool call with timing and result tracking
- `MCPSession` - Session tracking for all tool calls in a Lambda invocation
- `MCPToolCallLogger` - Context manager for automatic timing and error handling

**Usage:**
```python
from shared.mcp_logger import MCPSession, MCPToolCallLogger

session = MCPSession(
    session_id='sess-123',
    pipeline_id='pipe-456',
    context='GlueJobCreation'
)

with MCPToolCallLogger(session, 'analyze_data', {'volume_gb': 10}) as tool_call:
    result = mcp_client.call_tool('analyze_data')
    tool_call.complete(success=True, result=result)

session.publish_metrics()
```

### notification_service.py

Multi-channel notification service (SNS, Slack, Dashboard, CloudWatch).

**Classes:**
- `NotificationService` - Centralized notification publisher
- `NotificationPriority` - LOW, MEDIUM, HIGH, CRITICAL
- `NotificationChannel` - SNS, SLACK, DASHBOARD, CLOUDWATCH

**Convenience Functions:**
- `send_approval_notification()` - Approval required alerts
- `send_approval_timeout_alert()` - Timeout warnings (CRITICAL)
- `send_pipeline_failure_alert()` - Pipeline failure notifications
- `send_performance_degradation_alert()` - Performance warnings

**Usage:**
```python
from shared.notification_service import send_approval_notification

send_approval_notification(
    approval_type='schema_mapping',
    pipeline_id='pipe-123',
    approval_id='appr-456',
    dashboard_url='https://dashboard.example.com/approvals/appr-456'
)
```

### performance_tracker.py

Performance tracking and optimization metrics for KPIs.

**Classes:**
- `PerformanceTracker` - Main tracking class with DynamoDB storage
- `ETLSetupMetrics` - ETL setup time tracking
- `GlueJobCostMetrics` - Glue job cost tracking
- `DashboardMetrics` - Dashboard creation metrics
- `ErrorMetrics` - Error occurrence and resolution tracking

**Key Features:**
- Baseline comparisons (30-min ETL setup, $0.44/DPU-hour)
- Target tracking (60% setup time reduction, 20% cost reduction)
- Automatic optimization report generation

**Usage:**
```python
from shared.performance_tracker import track_etl_start, track_etl_complete

metrics = track_etl_start('pipe-123')
# ... do ETL work ...
track_etl_complete(metrics, success=True)

# Generate optimization report
from shared.performance_tracker import generate_optimization_report
report = generate_optimization_report()
```

## CloudWatch Namespaces

| Module | Namespace |
|--------|-----------|
| cloudwatch_metrics.py | `Agentic Data Ingestion/Pipeline` |
| glue_metrics.py | `Agentic Data Ingestion/Pipeline` |
| mcp_logger.py | `Agentic Data Ingestion/MCP` |
| notification_service.py | `Agentic Data Ingestion/Alerts` |
| performance_tracker.py | `Agentic Data Ingestion/Performance` |

## boto3 Client Initialization Convention

Each AWS service client is created **once per module and then cached**, rather than
by calling `boto3.client(...)` per invocation. That avoids duplicate client creation
and repeated connection setup on a hot Lambda path. Use one client per AWS service
per module, and match the module's existing region-resolution style (e.g. pass
`region_name=AWS_REGION` only if sibling clients in the same module do).

**Create it on first use, not at import.** `boto3.client()` resolves a region
eagerly, so a module-scope call makes the module unimportable wherever AWS is not
configured — it raises `NoRegionError` before any test body runs. Prefer a cached
accessor:

```python
_glue_client_cache: Any = None


def _glue_client() -> Any:
    global _glue_client_cache
    if _glue_client_cache is None:
        _glue_client_cache = boto3.client("glue")
    return _glue_client_cache
```

This keeps the one-client-per-module property while leaving the module importable
and the client patchable — a test injects a fake by replacing the accessor. The
worked examples are `_glue_client` in `jdbc_helpers.py` and the pair in
`lambda/agentic_data_ingestion_create_glue_pipeline/job_arguments.py`, whose comment
records what an import-time client cost: it took out a whole `tests/cdk/` module at
collection time, so that suite passed only where a developer's region happened to be
set.

## Module Size Guideline

Target a maximum of **800 physical source lines per module** (measured as the
total line count of the file). Modules that grow past this become hard to
navigate and review, so prefer splitting them along functional seams.

The two primary hotspot files identified in the P21 code review are decomposed to
meet this limit using the **façade pattern** — the original module is kept as a
thin re-export shim so existing imports keep resolving, while the implementation
moves into focused sibling modules:

- `lambda/agentic_data_ingestion_dashboard_api/handlers/pipelines.py` → split into
  `pipelines_*.py` modules (JDBC, logs, CRUD, runs, artifacts, mappings), routed
  through the existing `router.py`.
- `lambda/agentic_data_ingestion_create_glue_pipeline/lambda_function.py` → split into
  `job_arguments.py`, `connections.py`, `iam_roles.py`, `workers.py`, and
  `artifacts.py`, with `lambda_function.py` kept as the orchestration entrypoint.

These two primary files are **required** to land at or below 800 lines after
decomposition. For secondary hotspots (`jdbc_resources.py`,
`schema_mapping/lambda_function.py`, `glue_jobs.py`,
`create_code/lambda_function.py`) the same guideline is **recommended but not
required**.

The limit is enforced by `tests/unit/test_module_sizes.py`.

## Environment Variables

Required environment variables for shared modules:

```bash
AWS_REGION=us-east-1
ENVIRONMENT=dev
EVENT_BUS_NAME=agentic-data-ingestion-pipeline-events-dev
AGENTCORE_ENDPOINT=https://...
AGENT_ID=agent-xxx
AGENT_ALIAS_ID=alias-xxx
MCP_COST_THRESHOLD=100.0
SLACK_WEBHOOK_URL=https://hooks.slack.com/...
CRITICAL_ALERTS_TOPIC_ARN=arn:aws:sns:...
APPROVAL_NOTIFICATIONS_TOPIC_ARN=arn:aws:sns:...
```

## Last Updated

February 2, 2026 | Version 2.1
