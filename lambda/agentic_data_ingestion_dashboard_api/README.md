# Agentic Data Ingestion Dashboard API - Refactored Structure

## Overview

The dashboard API Lambda function has been refactored from a single large file (2482 lines) into a modular structure with separate handlers for each endpoint group.

## File Structure

```
agentic_data_ingestion_dashboard_api/
├── lambda_function.py          # Main entry point (simplified)
├── router.py                   # Request routing logic
├── lambda_function_original.py # Backup of original monolithic file
├── handlers/
│   ├── __init__.py
│   ├── common.py              # Shared utilities and AWS clients
│   ├── pipelines.py           # Pipeline management endpoints
│   ├── files.py               # File management endpoints
│   ├── stats.py               # Statistics endpoints
│   ├── approvals.py           # Approval management endpoints
│   └── health.py              # Health check and utility endpoints
└── README.md                  # This file
```

## Handler Modules

### `handlers/common.py`
- Shared AWS clients (DynamoDB, S3)
- Environment variables
- Common utilities (`create_response`, `handle_error`, `DecimalEncoder`)

### `handlers/pipelines.py`
- `get_pipelines()` - List all pipelines
- `get_pipeline_details(pipeline_key)` - Get detailed pipeline info
- `create_pipeline(body)` - Create new pipeline
- `edit_pipeline(pipeline_id, body)` - Update pipeline metadata
- `archive_pipeline(pipeline_id)` - Archive pipeline
- `restore_pipeline(pipeline_id)` - Restore archived pipeline

### `handlers/files.py`
- `get_files(query_params)` - List files with optional filtering
- `get_file_details(pipeline_key, file_key)` - Get detailed file execution info

### `handlers/stats.py`
- `get_stats()` - Get aggregated dashboard statistics

### `handlers/approvals.py`
- `get_approvals(query_params)` - List approval requests
- `get_pending_approvals()` - List pending approvals
- `approve_request(approval_id, body)` - Approve request
- `reject_request(approval_id, body)` - Reject request

### `handlers/health.py`
- `get_health()` - Health check endpoint
- `get_notifications(query_params)` - Notifications (placeholder)
- `get_updates(query_params)` - Updates (placeholder)

### `handlers/glue_jobs.py`
- `get_glue_job_details(pipeline_id)` - Get Glue job details for a pipeline
  - Returns job name, ARN, status, worker configuration
  - Returns last run timestamp and success rate
  - Returns estimated cost per run
  - Queries AWS Glue API for job run history

## Router Logic

The `router.py` module handles all request routing based on HTTP method and path patterns:

- `/api/pipelines` → pipelines handler
- `/api/files` → files handler
- `/api/stats` → stats handler
- `/api/approvals` → approvals handler
- `/api/health` → health handler

## Benefits of Refactoring

1. **Maintainability**: Each handler focuses on a specific domain
2. **Readability**: Smaller, focused files are easier to understand
3. **Testability**: Individual handlers can be tested in isolation
4. **Scalability**: New endpoints can be added without modifying existing handlers
5. **Code Reuse**: Common utilities are centralized in `common.py`

## Migration Notes

- Original file backed up as `lambda_function_original.py`
- All existing endpoints maintain the same API contracts
- No breaking changes to external interfaces
- Environment variables and AWS clients remain the same

## API Gateway Integration

### Resource-Level Catch-All Pattern

The Lambda function works with an API Gateway configuration that uses resource-level catch-all routing:

```
/api
  ├── pipelines/{proxy+}    → All pipeline endpoints
  ├── files/{proxy+}        → All file endpoints
  ├── approvals/{proxy+}    → All approval endpoints
  ├── schemas/{proxy+}      → All schema endpoints
  ├── runs/{proxy+}         → All run endpoints
  ├── code/{proxy+}         → All code endpoints
  ├── tests/{proxy+}        → All test endpoints
  ├── stats                 → Statistics endpoint
  ├── health                → Health check endpoint
  ├── notifications         → Notifications endpoint
  └── updates               → Updates endpoint
```

### Router Architecture

The `router.py` module implements resource-based delegation:

1. **Extract resource** from path: `/api/pipelines/my-id/edit` → `resource='pipelines'`
2. **Extract subpath**: `subpath='my-id/edit'`
3. **Delegate to resource router**: `pipelines_router(event, subpath)`
4. **Resource router** handles specific logic for that domain

### Benefits

- **Simplified API Gateway**: 63% fewer CloudFormation lines (1610 → 600)
- **Faster Development**: New sub-endpoints require zero infrastructure changes
- **Better Organization**: Clear separation between resource domains
- **Flexible IAM**: Can apply different policies per resource group
- **Improved Monitoring**: CloudWatch metrics per resource

### Adding New Endpoints

**New Sub-Endpoint** (e.g., `/api/pipelines/{id}/clone`):
- Update `pipelines_router()` in `router.py`
- Add handler function in `handlers/pipelines.py`
- Deploy Lambda code only (no CloudFormation changes)

**New Top-Level Resource** (e.g., `/api/schemas`):
- Add resource to `cloudformation/api-gateway-v2.yaml`
- Create `handlers/schemas.py`
- Add `schemas_router()` to `router.py`
- Deploy CloudFormation + Lambda

See `cloudformation/API_GATEWAY_V2_MIGRATION.md` for detailed migration guide.

## Future Enhancements

Consider extracting additional handler modules for:
- Schema management endpoints (partially implemented)
- Code generation endpoints
- Test result endpoints
- S3 content endpoints
- Bedrock model endpoints

These endpoints are currently missing from the refactored structure and would need to be added based on the original implementation.