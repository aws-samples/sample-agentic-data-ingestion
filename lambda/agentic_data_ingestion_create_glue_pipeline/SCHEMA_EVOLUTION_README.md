# Schema Evolution and Job Updates

This document describes the implementation of Task 10: Schema evolution and job updates for the Agentic Data Ingestion Glue job generation feature.

## Overview

The schema evolution system automatically handles schema changes by:
1. Classifying schema changes as compatible, breaking, or identical
2. Updating existing jobs for compatible changes
3. Creating new versioned jobs for breaking changes
4. Validating that jobs are not running before updates
5. Storing version history in DynamoDB
6. Providing rollback capability to previous configurations

## Architecture

### Modules

1. **schema_evolution.py** - Core schema evolution logic
   - `classify_schema_change()` - Determines if schema change is compatible or breaking
   - `check_job_running_status()` - Validates job is not running before updates
   - `update_existing_job()` - Updates job for compatible changes
   - `create_new_versioned_job()` - Creates versioned job for breaking changes
   - `store_job_version_history()` - Stores version history in DynamoDB
   - `rollback_job_to_previous_version()` - Restores previous job configuration

2. **job_update_handler.py** - Integration with job creation workflow
   - `should_use_schema_evolution()` - Determines if evolution should be used
   - `create_or_update_job_with_evolution()` - Main integration function
   - `get_existing_job_and_schema()` - Retrieves existing job and schema

3. **Dashboard API (glue_jobs.py)** - REST API endpoints
   - `POST /api/pipelines/{id}/glue-job/rollback` - Rollback to previous version
   - `GET /api/pipelines/{id}/glue-job/versions` - Get version history
   - `GET /api/pipelines/{id}/glue-job/validate-update` - Check if update allowed

## Schema Change Classification

### Compatible Changes (Update Existing Job)

Compatible changes allow updating the existing job without creating a new version:

- **Adding nullable fields** - New optional fields can be added
- **Widening data types** - Type changes that don't lose precision:
  - `int` → `bigint`, `long`, `double`, `decimal`
  - `float` → `double`, `decimal`
  - `date` → `timestamp`, `datetime`
  - `string` → `text`, `varchar`
- **Making fields nullable** - Changing non-nullable to nullable
- **Adding optional fields** - Fields with default values

### Breaking Changes (Create New Versioned Job)

Breaking changes require creating a new versioned job:

- **Removing fields** - Deleting existing fields
- **Incompatible type changes** - Type changes that could lose data:
  - `bigint` → `int`
  - `double` → `float`
  - `timestamp` → `date`
- **Making fields non-nullable** - Changing nullable to non-nullable
- **Renaming fields** - Field name changes (detected as remove + add)

### Identical (No Action)

No changes detected between schemas - no job update needed.

## Job Versioning

### Version Naming Convention

Jobs are versioned using the pattern: `agentic-data-ingestion-job-{pipeline_id}-v{version}`

Examples:
- Initial job: `agentic-data-ingestion-job-pipeline123` (no version suffix)
- Version 2: `agentic-data-ingestion-job-pipeline123-v2`
- Version 3: `agentic-data-ingestion-job-pipeline123-v3`

### Version History Storage

Version history is stored in the `agentic_data_ingestion_pipelines` DynamoDB table:

```python
{
    'pipeline_id': 'pipeline123',
    'current_job_version': 3,
    'job_version_history': [
        {
            'version': 1,
            'job_name': 'agentic-data-ingestion-job-pipeline123',
            'schema_change_type': 'initial',
            'created_at': '2025-01-15T10:00:00Z',
            'status': 'active',
            'differences': [],
            'job_config': {...}
        },
        {
            'version': 2,
            'job_name': 'agentic-data-ingestion-job-pipeline123-v2',
            'schema_change_type': 'breaking',
            'created_at': '2025-01-20T14:30:00Z',
            'status': 'active',
            'differences': [
                {
                    'type': 'field_removed',
                    'field': 'old_column',
                    'severity': 'breaking'
                }
            ],
            'job_config': {...}
        },
        {
            'version': 3,
            'job_name': 'agentic-data-ingestion-job-pipeline123-v3',
            'schema_change_type': 'compatible',
            'created_at': '2025-01-25T09:15:00Z',
            'status': 'active',
            'differences': [
                {
                    'type': 'field_added',
                    'field': 'new_column',
                    'severity': 'compatible',
                    'nullable': True
                }
            ],
            'job_config': {...}
        }
    ]
}
```

## Job Update Validation

### Running Job Check

Before any update or rollback, the system checks if the job is currently running:

```python
is_running, run_id = check_job_running_status(job_name)

if is_running:
    return {
        'error': 'job_running',
        'message': f'Cannot update job - currently running (run ID: {run_id})',
        'run_id': run_id
    }
```

Job states that block updates:
- `RUNNING` - Job is actively executing
- `STOPPING` - Job is in the process of stopping

Job states that allow updates:
- `STOPPED` - Job has stopped
- `SUCCEEDED` - Job completed successfully
- `FAILED` - Job failed
- `TIMEOUT` - Job timed out

### Update Queue (Future Enhancement)

Currently, if a job is running, the update is rejected with a 409 Conflict error. A future enhancement could implement an update queue:

```python
# Future: Queue update if job is running
if is_running:
    queue_update(pipeline_id, new_config)
    return {
        'status': 'queued',
        'message': 'Update queued - will apply when job completes'
    }
```

## Rollback Capability

### Backup Storage

Before any job update, the current configuration is backed up:

```python
backup_config = backup_current_job_config(job_name)
store_job_backup(pipeline_id, backup_config, pipelines_table)
```

Backup is stored in DynamoDB:

```python
{
    'pipeline_id': 'pipeline123',
    'job_backup': {
        'Name': 'agentic-data-ingestion-job-pipeline123',
        'Role': 'arn:aws:iam::...',
        'Command': {...},
        'DefaultArguments': {...},
        'WorkerType': 'G.2X',
        'NumberOfWorkers': 5,
        'backed_up_at': '2025-01-25T09:15:00Z'
    },
    'job_backup_timestamp': '2025-01-25T09:15:00Z'
}
```

### Rollback Process

1. **Validate job not running** - Check current job status
2. **Retrieve backup** - Get backup configuration from DynamoDB
3. **Restore configuration** - Update Glue job with backup config
4. **Update metadata** - Track rollback in pipeline metadata

```python
result = rollback_job_to_previous_version(pipeline_id, pipelines_table)

if result['success']:
    # Job successfully rolled back
    print(f"Rolled back to {result['backup_timestamp']}")
```

### Rollback Tracking

Rollbacks are tracked in pipeline metadata:

```python
{
    'glue_job_metadata': {
        'rolled_back_at': '2025-01-25T15:30:00Z',
        'rollback_count': 2
    }
}
```

## API Endpoints

### Rollback Job

**Endpoint:** `POST /api/pipelines/{pipeline_id}/glue-job/rollback`

**Request:**
```bash
curl -X POST https://api.example.com/api/pipelines/pipeline123/glue-job/rollback
```

**Response (Success):**
```json
{
    "success": true,
    "job_name": "agentic-data-ingestion-job-pipeline123",
    "action": "rolled_back",
    "backup_timestamp": "2025-01-25T09:15:00Z",
    "rolled_back_at": "2025-01-25T15:30:00Z",
    "message": "Successfully rolled back job to previous configuration"
}
```

**Response (Job Running):**
```json
{
    "statusCode": 409,
    "error": "job_running",
    "message": "Cannot rollback - job is currently RUNNING",
    "run_id": "jr_abc123",
    "job_name": "agentic-data-ingestion-job-pipeline123"
}
```

**Response (No Backup):**
```json
{
    "statusCode": 404,
    "error": "no_backup_found",
    "message": "No backup configuration found for pipeline pipeline123",
    "hint": "Backup is created automatically when job is updated"
}
```

### Get Version History

**Endpoint:** `GET /api/pipelines/{pipeline_id}/glue-job/versions`

**Request:**
```bash
curl https://api.example.com/api/pipelines/pipeline123/glue-job/versions
```

**Response:**
```json
{
    "pipeline_id": "pipeline123",
    "current_version": 3,
    "current_job_name": "agentic-data-ingestion-job-pipeline123-v3",
    "total_versions": 3,
    "has_backup": true,
    "backup_timestamp": "2025-01-25T09:15:00Z",
    "rollback_count": 1,
    "version_history": [
        {
            "version": 3,
            "job_name": "agentic-data-ingestion-job-pipeline123-v3",
            "schema_change_type": "compatible",
            "created_at": "2025-01-25T09:15:00Z",
            "status": "active",
            "is_current": true,
            "differences_count": 1,
            "differences": [
                {
                    "type": "field_added",
                    "field": "new_column",
                    "severity": "compatible",
                    "nullable": true
                }
            ]
        },
        {
            "version": 2,
            "job_name": "agentic-data-ingestion-job-pipeline123-v2",
            "schema_change_type": "breaking",
            "created_at": "2025-01-20T14:30:00Z",
            "status": "active",
            "is_current": false,
            "differences_count": 1,
            "differences": [
                {
                    "type": "field_removed",
                    "field": "old_column",
                    "severity": "breaking"
                }
            ]
        }
    ]
}
```

### Validate Update Allowed

**Endpoint:** `GET /api/pipelines/{pipeline_id}/glue-job/validate-update`

**Request:**
```bash
curl https://api.example.com/api/pipelines/pipeline123/glue-job/validate-update
```

**Response (Update Allowed):**
```json
{
    "update_allowed": true,
    "job_name": "agentic-data-ingestion-job-pipeline123",
    "message": "Job is not running, update allowed"
}
```

**Response (Update Blocked):**
```json
{
    "update_allowed": false,
    "reason": "job_running",
    "message": "Job agentic-data-ingestion-job-pipeline123 is currently running",
    "running_jobs": [
        {
            "run_id": "jr_abc123",
            "status": "RUNNING",
            "started_on": "2025-01-25T15:00:00Z"
        }
    ],
    "job_name": "agentic-data-ingestion-job-pipeline123"
}
```

## Integration with Job Creation

The schema evolution logic is integrated into the job creation workflow:

```python
# In lambda_handler or job creation function
result = create_or_update_job_with_evolution(
    pipeline_id=pipeline_id,
    target_type=target_type,
    target_schema=new_schema,
    generated_code_s3_path=code_s3_uri,
    s3_table_bucket=s3_table_bucket,
    worker_recommendation=recommendation,
    target_config=target_config,
    test_results=test_results,
    run_id=run_id,
    pipelines_table=pipelines_table,
    create_glue_job_func=create_glue_job
)

if result.get('schema_evolution_used'):
    logger.info(f"Schema evolution applied: {result['action']}")
    logger.info(f"Change type: {result['change_type']}")
else:
    logger.info("New job created (no evolution)")
```

## Error Handling

### Job Running Error

```python
{
    'statusCode': 409,
    'error': 'job_running',
    'message': 'Cannot update job - currently running',
    'run_id': 'jr_abc123'
}
```

**Resolution:** Wait for job to complete, then retry update.

### No Backup Found

```python
{
    'statusCode': 404,
    'error': 'no_backup_found',
    'message': 'No backup configuration found'
}
```

**Resolution:** Cannot rollback without backup. Backup is created automatically on first update.

### Schema Evolution Failed

```python
{
    'success': False,
    'schema_evolution_used': False,
    'schema_evolution_error': 'Error message'
}
```

**Resolution:** System falls back to creating new job. Check logs for details.

## Testing

### Unit Tests

Test schema classification:
```python
def test_compatible_schema_change():
    old_schema = {'fields': [{'name': 'id', 'type': 'int'}]}
    new_schema = {'fields': [
        {'name': 'id', 'type': 'int'},
        {'name': 'name', 'type': 'string', 'nullable': True}
    ]}
    
    change_type, diffs = classify_schema_change(old_schema, new_schema)
    assert change_type == SchemaChangeType.COMPATIBLE
```

### Integration Tests

Test job update workflow:
```python
def test_update_job_with_compatible_change():
    # Create initial job
    job1 = create_glue_job(pipeline_id, schema_v1, ...)
    
    # Update with compatible schema
    result = create_or_update_job_with_evolution(
        pipeline_id, schema_v2, ...
    )
    
    assert result['action'] == 'updated'
    assert result['change_type'] == 'compatible'
```

## Monitoring

### CloudWatch Metrics

Track schema evolution events:
- `SchemaEvolutionUsed` - Count of evolution operations
- `CompatibleSchemaChanges` - Count of compatible updates
- `BreakingSchemaChanges` - Count of breaking changes requiring new versions
- `JobRollbacks` - Count of rollback operations
- `JobUpdateBlocked` - Count of updates blocked by running jobs

### CloudWatch Logs

Log schema evolution decisions:
```
[INFO] Schema change classified as: compatible
[INFO] Found 2 differences: field_added, type_changed
[INFO] Updating existing job: agentic-data-ingestion-job-pipeline123
[INFO] Stored version history for version 2
```

## Future Enhancements

1. **Update Queue** - Queue updates when job is running, apply when complete
2. **Automatic Rollback** - Auto-rollback on job failure after update
3. **Schema Migration Scripts** - Generate data migration scripts for breaking changes
4. **Version Comparison** - Compare any two versions, not just consecutive
5. **Scheduled Updates** - Schedule schema updates for maintenance windows
6. **Approval Workflow** - Require approval for breaking changes
7. **Canary Deployments** - Test new version with subset of data before full rollout

## References

- Task 10 Requirements: `.kiro/specs/glue-job-generation/requirements.md` (Requirement 7)
- Design Document: `.kiro/specs/glue-job-generation/design.md`
- Implementation Guide: `agentic_data_ingestion/glue_generation.md`
