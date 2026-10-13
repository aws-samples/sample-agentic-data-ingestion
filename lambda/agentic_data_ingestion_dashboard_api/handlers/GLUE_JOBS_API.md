# Glue Jobs API Documentation

## Overview

The Glue Jobs API provides endpoints for managing and monitoring AWS Glue ETL jobs created by Agentic Data Ingestion pipelines.

## Endpoints

### GET /api/pipelines/{pipeline_id}/glue-job

Get detailed information about the Glue job associated with a specific pipeline.

#### Request

```
GET /api/pipelines/my-pipeline-123/glue-job
```

#### Response (200 OK)

```json
{
  "pipeline_id": "my-pipeline-123",
  "job_name": "agentic-data-ingestion-job-my-pipeline-123",
  "job_arn": "arn:aws:glue:us-east-1:123456789012:job/agentic-data-ingestion-job-my-pipeline-123",
  "glue_version": "5.0",
  "status": "created",
  "created_at": "2025-01-15T10:30:00Z",
  
  "worker_type": "G.1X",
  "number_of_workers": 5,
  "timeout_minutes": 60,
  "estimated_duration_minutes": 30,
  
  "estimated_cost_per_run": 0.88,
  "estimated_monthly_cost": 26.40,
  "cost_per_gb": 0.044,
  "total_dpu": 5.0,
  "dpu_hours": 2.5,
  
  "last_run_timestamp": "2025-01-15T14:20:00Z",
  "last_run_status": "SUCCEEDED",
  "success_rate": 95.5,
  "total_runs": 22,
  "successful_runs": 21,
  "failed_runs": 1,
  "average_duration_minutes": 28.5,
  
  "mcp_enhanced": true,
  "recommendation_rationale": "Based on data volume of 5GB and medium complexity transformations, G.1X workers with 5 instances provide optimal cost-performance balance.",
  "recommendation_confidence": 0.92,
  "mcp_tools_used": ["analyze_data_characteristics", "calculate_optimal_workers", "estimate_glue_cost"],
  
  "script_location": "s3://adi-generated-code-{account}-{region}/scripts/my-pipeline-123.py",
  "role_arn": "arn:aws:iam::123456789012:role/AgenticDataIngestion-my-pipeline-123-ExecutionRole"
}
```

#### Response Fields

**Job Identification:**
- `pipeline_id` (string): Pipeline identifier
- `job_name` (string): AWS Glue job name
- `job_arn` (string): AWS Glue job ARN
- `glue_version` (string): Glue version (e.g., "5.0")
- `status` (string): Job status ("created", "active", "failed")
- `created_at` (string): ISO 8601 timestamp of job creation

**Worker Configuration:**
- `worker_type` (string): Glue worker type (G.025X, G.1X, G.2X, G.4X, G.8X, Z.2X)
- `number_of_workers` (integer): Number of workers allocated
- `timeout_minutes` (integer): Job timeout in minutes
- `estimated_duration_minutes` (integer): Expected job duration

**Cost Information:**
- `estimated_cost_per_run` (float): Estimated cost per job execution in USD
- `estimated_monthly_cost` (float): Estimated monthly cost (assuming daily runs)
- `cost_per_gb` (float): Cost per GB of data processed
- `total_dpu` (float): Total Data Processing Units
- `dpu_hours` (float): DPU-hours per run

**Job Run Statistics:**
- `last_run_timestamp` (string): ISO 8601 timestamp of last run
- `last_run_status` (string): Status of last run (SUCCEEDED, FAILED, RUNNING, etc.)
- `success_rate` (float): Percentage of successful runs (0-100)
- `total_runs` (integer): Total number of job executions
- `successful_runs` (integer): Number of successful executions
- `failed_runs` (integer): Number of failed executions
- `average_duration_minutes` (float): Average duration of completed runs

**MCP Recommendation Details:**
- `mcp_enhanced` (boolean): Whether MCP tools were used for configuration
- `recommendation_rationale` (string): Explanation of worker configuration choice
- `recommendation_confidence` (float): Confidence score (0.0-1.0)
- `mcp_tools_used` (array): List of MCP tools used for analysis

**Additional Metadata:**
- `script_location` (string): S3 URI of PySpark script
- `role_arn` (string): IAM role ARN for job execution

#### Error Responses

**404 Not Found - Pipeline Not Found:**
```json
{
  "error": "Pipeline not found: my-pipeline-123"
}
```

**404 Not Found - No Glue Job:**
```json
{
  "error": "No Glue job found for pipeline: my-pipeline-123",
  "message": "Glue job has not been created yet for this pipeline"
}
```

**500 Internal Server Error:**
```json
{
  "error": "Error fetching Glue job details for pipeline my-pipeline-123",
  "details": "..."
}
```

## Data Sources

The endpoint aggregates data from multiple sources:

1. **DynamoDB (agentic_data_ingestion_pipelines table):**
   - `glue_job_metadata`: Job name, ARN, version, script location, role ARN, status, created_at
   - `worker_configuration`: Worker type, number of workers, timeout, estimated duration
   - `cost_information`: Cost estimates and DPU calculations
   - `mcp_recommendation`: MCP rationale, confidence, tools used

2. **AWS Glue API (get_job_runs):**
   - Job run history (last 100 runs)
   - Run timestamps and statuses
   - Duration calculations
   - Success/failure statistics

## Usage Examples

### cURL

```bash
# Get Glue job details
curl -X GET https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job

# With authentication
curl -X GET https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job \
  -H "Authorization: Bearer ${TOKEN}"
```

### JavaScript (Dashboard)

```javascript
async function getGlueJobDetails(pipelineId) {
  const response = await fetch(`/api/pipelines/${pipelineId}/glue-job`);
  
  if (!response.ok) {
    if (response.status === 404) {
      const error = await response.json();
      console.log('No Glue job found:', error.message);
      return null;
    }
    throw new Error('Failed to fetch Glue job details');
  }
  
  const jobDetails = await response.json();
  console.log('Glue Job:', jobDetails.job_name);
  console.log('Success Rate:', jobDetails.success_rate + '%');
  console.log('Estimated Cost:', '$' + jobDetails.estimated_cost_per_run);
  
  return jobDetails;
}
```

### Python

```python
import requests

def get_glue_job_details(pipeline_id):
    url = f"https://api.agentic_data_ingestion.com/api/pipelines/{pipeline_id}/glue-job"
    response = requests.get(url)
    
    if response.status_code == 404:
        print(f"No Glue job found for pipeline {pipeline_id}")
        return None
    
    response.raise_for_status()
    job_details = response.json()
    
    print(f"Job Name: {job_details['job_name']}")
    print(f"Worker Type: {job_details['worker_type']}")
    print(f"Success Rate: {job_details['success_rate']}%")
    print(f"Cost per Run: ${job_details['estimated_cost_per_run']:.2f}")
    
    return job_details
```

## Implementation Notes

### Performance Considerations

- **DynamoDB Query**: Single GetItem operation (fast, ~10ms)
- **Glue API Call**: get_job_runs with MaxResults=100 (~200-500ms)
- **Total Response Time**: Typically 300-600ms
- **Caching**: Consider caching job run statistics for 5-10 minutes

### Error Handling

The endpoint gracefully handles:
- Missing pipeline records
- Pipelines without Glue jobs
- Glue API errors (returns empty statistics)
- DynamoDB errors (returns 500 with details)

### IAM Permissions Required

The Lambda execution role needs:
```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:GetItem",
    "glue:GetJobRuns"
  ],
  "Resource": [
    "arn:aws:dynamodb:*:*:table/agentic_data_ingestion_pipelines",
    "arn:aws:glue:*:*:job/agentic-data-ingestion-*"
  ]
}
```

---

### POST /api/pipelines/{pipeline_id}/glue-job/run

Start a Glue job execution for a specific pipeline.

#### Request

```
POST /api/pipelines/my-pipeline-123/glue-job/run
```

#### Response (200 OK)

```json
{
  "pipeline_id": "my-pipeline-123",
  "job_name": "agentic-data-ingestion-job-my-pipeline-123",
  "job_run_id": "jr_abc123def456",
  "status": "RUNNING",
  "started_at": "2025-01-15T15:30:00Z",
  "message": "Glue job execution started successfully",
  "cloudwatch_logs_url": "https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput/log-events/jr_abc123def456"
}
```

#### Response Fields

- `pipeline_id` (string): Pipeline identifier
- `job_name` (string): AWS Glue job name
- `job_run_id` (string): Unique identifier for this job execution
- `status` (string): Job run status (always "RUNNING" on successful start)
- `started_at` (string): ISO 8601 timestamp when the job was started
- `message` (string): Success message
- `cloudwatch_logs_url` (string): Direct link to CloudWatch logs for this run

#### Error Responses

**404 Not Found - Pipeline Not Found:**
```json
{
  "error": "Pipeline not found: my-pipeline-123"
}
```

**404 Not Found - No Glue Job:**
```json
{
  "error": "No Glue job found for pipeline: my-pipeline-123",
  "message": "Glue job has not been created yet for this pipeline. Complete pipeline provisioning first."
}
```

**404 Not Found - Job Deleted:**
```json
{
  "error": "Glue job does not exist in AWS: agentic-data-ingestion-job-my-pipeline-123",
  "message": "The job may have been deleted or not yet created. Check pipeline status."
}
```

**429 Too Many Requests - Concurrent Runs Limit:**
```json
{
  "error": "Concurrent runs limit exceeded",
  "message": "The job is already running. Wait for the current execution to complete before starting a new run.",
  "pipeline_id": "my-pipeline-123"
}
```

**400 Bad Request - Invalid Configuration:**
```json
{
  "error": "Invalid job configuration",
  "message": "Job configuration is invalid or incomplete",
  "pipeline_id": "my-pipeline-123"
}
```

**500 Internal Server Error:**
```json
{
  "error": "Error starting Glue job for pipeline my-pipeline-123",
  "details": "..."
}
```

#### Validation Steps

The endpoint performs the following validations before starting the job:

1. **Pipeline Exists**: Verifies the pipeline exists in DynamoDB
2. **Job Metadata Exists**: Checks that glue_job_metadata is present in pipeline record
3. **Job Exists in AWS**: Calls `glue:GetJob` to verify the job exists in AWS Glue
4. **Concurrent Runs**: AWS Glue enforces concurrent run limits (handled automatically)

#### Usage Examples

##### cURL

```bash
# Start a Glue job run
curl -X POST https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/run

# With authentication
curl -X POST https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/run \
  -H "Authorization: Bearer ${TOKEN}"
```

##### JavaScript (Dashboard)

```javascript
async function startGlueJob(pipelineId) {
  try {
    const response = await fetch(`/api/pipelines/${pipelineId}/glue-job/run`, {
      method: 'POST'
    });
    
    if (!response.ok) {
      if (response.status === 404) {
        const error = await response.json();
        alert(error.message || 'Glue job not found');
        return null;
      }
      if (response.status === 429) {
        const error = await response.json();
        alert('Job is already running. Please wait for it to complete.');
        return null;
      }
      throw new Error('Failed to start Glue job');
    }
    
    const result = await response.json();
    console.log('Job started:', result.job_run_id);
    console.log('View logs:', result.cloudwatch_logs_url);
    
    return result;
  } catch (error) {
    console.error('Error starting job:', error);
    throw error;
  }
}

// Usage in dashboard
document.getElementById('run-job-btn').addEventListener('click', async () => {
  const pipelineId = getCurrentPipelineId();
  const result = await startGlueJob(pipelineId);
  
  if (result) {
    showNotification(`Job started: ${result.job_run_id}`, 'success');
    // Optionally redirect to job monitoring page
    window.location.href = `/glue-jobs/${result.job_name}?run=${result.job_run_id}`;
  }
});
```

##### Python

```python
import requests

def start_glue_job(pipeline_id):
    url = f"https://api.agentic_data_ingestion.com/api/pipelines/{pipeline_id}/glue-job/run"
    response = requests.post(url)
    
    if response.status_code == 404:
        error = response.json()
        print(f"Error: {error.get('message', 'Job not found')}")
        return None
    
    if response.status_code == 429:
        print("Job is already running. Please wait.")
        return None
    
    response.raise_for_status()
    result = response.json()
    
    print(f"Job started successfully!")
    print(f"Job Run ID: {result['job_run_id']}")
    print(f"Status: {result['status']}")
    print(f"CloudWatch Logs: {result['cloudwatch_logs_url']}")
    
    return result

# Usage
job_run = start_glue_job('my-pipeline-123')
if job_run:
    print(f"Monitor job at: {job_run['cloudwatch_logs_url']}")
```

#### Implementation Notes

##### Validation Flow

1. Query DynamoDB for pipeline metadata
2. Extract `glue_job_metadata.glue_job_name`
3. Call `glue:GetJob` to verify job exists in AWS
4. Call `glue:StartJobRun` to start execution
5. Return job run ID and status

##### Concurrent Run Handling

AWS Glue enforces concurrent run limits per job (default: 1 concurrent run). If a job is already running:
- AWS Glue returns `ConcurrentRunsExceededException`
- Endpoint returns 429 status code
- Client should wait and retry

##### IAM Permissions Required

The Lambda execution role needs:
```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:GetItem",
    "glue:GetJob",
    "glue:StartJobRun"
  ],
  "Resource": [
    "arn:aws:dynamodb:*:*:table/agentic_data_ingestion_pipelines",
    "arn:aws:glue:*:*:job/agentic-data-ingestion-*"
  ]
}
```

##### CloudWatch Logs

Job execution logs are automatically sent to CloudWatch Logs:
- Log Group: `/aws-glue/jobs/output`
- Log Stream: `{job_run_id}`
- Direct URL provided in response for easy access

##### Performance

- **DynamoDB Query**: ~10ms
- **Glue GetJob**: ~100-200ms
- **Glue StartJobRun**: ~200-500ms
- **Total Response Time**: Typically 300-700ms

---

---

### GET /api/pipelines/{pipeline_id}/glue-job/runs

Get job run history for a specific pipeline with pagination support.

#### Request

```
GET /api/pipelines/my-pipeline-123/glue-job/runs?max_results=25&next_token=abc123
```

#### Query Parameters

- `max_results` (integer, optional): Maximum number of results to return
  - Default: 25
  - Range: 1-100
  - Values outside range are clamped automatically
  
- `next_token` (string, optional): Pagination token from previous response
  - Used to retrieve the next page of results
  - Omit for first page

#### Response (200 OK)

```json
{
  "pipeline_id": "my-pipeline-123",
  "job_name": "agentic-data-ingestion-job-my-pipeline-123",
  "runs": [
    {
      "run_id": "jr_abc123def456",
      "status": "SUCCEEDED",
      "started_at": "2025-01-15T14:20:00Z",
      "completed_at": "2025-01-15T14:48:30Z",
      "duration_seconds": 1710,
      "duration_minutes": 28.5,
      "error_message": "",
      "cloudwatch_logs_url": "https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput/log-events/jr_abc123def456",
      "dpu_seconds": 8550,
      "max_capacity": 0,
      "worker_type": "G.1X",
      "number_of_workers": 5,
      "cost_estimate": 1.045,
      "attempt": 0,
      "execution_time": 1710,
      "timeout": 3600
    },
    {
      "run_id": "jr_def456ghi789",
      "status": "FAILED",
      "started_at": "2025-01-14T10:15:00Z",
      "completed_at": "2025-01-14T10:22:45Z",
      "duration_seconds": 465,
      "duration_minutes": 7.75,
      "error_message": "java.lang.OutOfMemoryError: Java heap space",
      "cloudwatch_logs_url": "https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput/log-events/jr_def456ghi789",
      "dpu_seconds": 2325,
      "max_capacity": 0,
      "worker_type": "G.1X",
      "number_of_workers": 5,
      "cost_estimate": 0.284,
      "attempt": 0,
      "execution_time": 465,
      "timeout": 3600
    },
    {
      "run_id": "jr_ghi789jkl012",
      "status": "RUNNING",
      "started_at": "2025-01-15T15:30:00Z",
      "completed_at": "",
      "duration_seconds": 180,
      "duration_minutes": 3.0,
      "error_message": "",
      "cloudwatch_logs_url": "https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput/log-events/jr_ghi789jkl012",
      "dpu_seconds": 0,
      "max_capacity": 0,
      "worker_type": "G.1X",
      "number_of_workers": 5,
      "cost_estimate": 0.0,
      "attempt": 0,
      "execution_time": 0,
      "timeout": 3600
    }
  ],
  "total_returned": 3,
  "has_more": true,
  "next_token": "eyJsYXN0RXZhbHVhdGVkS2V5Ijp7InJ1bl9pZCI6ImpyX2doaTc4OWprbDAxMiJ9fQ=="
}
```

#### Response Fields

**Top-Level:**
- `pipeline_id` (string): Pipeline identifier
- `job_name` (string): AWS Glue job name
- `runs` (array): List of job run objects (see below)
- `total_returned` (integer): Number of runs in this response
- `has_more` (boolean): Whether more results are available
- `next_token` (string): Token for next page (null if no more results)

**Run Object:**
- `run_id` (string): Unique identifier for this job execution
- `status` (string): Job run status
  - `STARTING`: Job is initializing
  - `RUNNING`: Job is executing
  - `STOPPING`: Job is being stopped
  - `STOPPED`: Job was stopped by user
  - `SUCCEEDED`: Job completed successfully
  - `FAILED`: Job failed with error
  - `TIMEOUT`: Job exceeded timeout limit
  - `ERROR`: Job encountered an error
- `started_at` (string): ISO 8601 timestamp when job started
- `completed_at` (string): ISO 8601 timestamp when job completed (empty if still running)
- `duration_seconds` (integer): Total execution time in seconds
- `duration_minutes` (float): Total execution time in minutes (rounded to 2 decimals)
- `error_message` (string): Error message if job failed (empty if successful)
- `cloudwatch_logs_url` (string): Direct link to CloudWatch logs for this run
- `dpu_seconds` (integer): Total DPU-seconds consumed (0 for running jobs)
- `max_capacity` (float): Maximum DPU capacity (legacy, usually 0 for Glue 5.0)
- `worker_type` (string): Worker type used (G.025X, G.1X, G.2X, G.4X, G.8X, Z.2X)
- `number_of_workers` (integer): Number of workers allocated
- `cost_estimate` (float): Estimated cost in USD for this run (based on DPU-seconds)
- `attempt` (integer): Retry attempt number (0 for first attempt)
- `execution_time` (integer): Actual execution time in seconds
- `timeout` (integer): Job timeout setting in seconds

#### Pagination

The endpoint supports pagination for large result sets:

1. **First Request**: Omit `next_token` parameter
   ```
   GET /api/pipelines/my-pipeline-123/glue-job/runs?max_results=25
   ```

2. **Subsequent Requests**: Use `next_token` from previous response
   ```
   GET /api/pipelines/my-pipeline-123/glue-job/runs?max_results=25&next_token=eyJ...
   ```

3. **Last Page**: When `has_more` is `false`, no more results are available

#### Error Responses

**404 Not Found - Pipeline Not Found:**
```json
{
  "error": "Pipeline not found: my-pipeline-123"
}
```

**404 Not Found - No Glue Job:**
```json
{
  "error": "No Glue job found for pipeline: my-pipeline-123",
  "message": "Glue job has not been created yet for this pipeline"
}
```

**404 Not Found - Job Deleted:**
```json
{
  "error": "Glue job does not exist in AWS",
  "message": "The job may have been deleted. Check pipeline status."
}
```

**400 Bad Request - Invalid Parameters:**
```json
{
  "error": "Invalid request parameters",
  "message": "Invalid NextToken value"
}
```

**500 Internal Server Error:**
```json
{
  "error": "Error fetching Glue job runs for pipeline my-pipeline-123",
  "details": "..."
}
```

#### Usage Examples

##### cURL

```bash
# Get first page of job runs
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs?max_results=25"

# Get next page using token
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs?max_results=25&next_token=eyJ..."

# Get more results per page
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs?max_results=50"
```

##### JavaScript (Dashboard)

```javascript
async function getGlueJobRuns(pipelineId, maxResults = 25, nextToken = null) {
  let url = `/api/pipelines/${pipelineId}/glue-job/runs?max_results=${maxResults}`;
  if (nextToken) {
    url += `&next_token=${encodeURIComponent(nextToken)}`;
  }
  
  const response = await fetch(url);
  
  if (!response.ok) {
    if (response.status === 404) {
      const error = await response.json();
      console.log('No job runs found:', error.message);
      return null;
    }
    throw new Error('Failed to fetch job runs');
  }
  
  const data = await response.json();
  console.log(`Retrieved ${data.total_returned} runs`);
  console.log(`Has more: ${data.has_more}`);
  
  return data;
}

// Paginate through all runs
async function getAllJobRuns(pipelineId) {
  const allRuns = [];
  let nextToken = null;
  
  do {
    const response = await getGlueJobRuns(pipelineId, 50, nextToken);
    if (!response) break;
    
    allRuns.push(...response.runs);
    nextToken = response.next_token;
  } while (nextToken);
  
  console.log(`Total runs retrieved: ${allRuns.length}`);
  return allRuns;
}

// Display runs in table
function displayJobRuns(runs) {
  const tableBody = document.getElementById('job-runs-table-body');
  tableBody.innerHTML = '';
  
  runs.forEach(run => {
    const row = document.createElement('tr');
    row.className = run.status === 'SUCCEEDED' ? 'success' : 
                    run.status === 'FAILED' ? 'error' : 
                    run.status === 'RUNNING' ? 'running' : '';
    
    row.innerHTML = `
      <td>${run.run_id}</td>
      <td><span class="status-badge ${run.status.toLowerCase()}">${run.status}</span></td>
      <td>${new Date(run.started_at).toLocaleString()}</td>
      <td>${run.duration_minutes} min</td>
      <td>$${run.cost_estimate.toFixed(2)}</td>
      <td>
        <a href="${run.cloudwatch_logs_url}" target="_blank" class="btn-link">
          View Logs
        </a>
      </td>
    `;
    
    tableBody.appendChild(row);
  });
}
```

##### Python

```python
import requests

def get_glue_job_runs(pipeline_id, max_results=25, next_token=None):
    """Get job run history with pagination"""
    url = f"https://api.agentic_data_ingestion.com/api/pipelines/{pipeline_id}/glue-job/runs"
    params = {'max_results': max_results}
    
    if next_token:
        params['next_token'] = next_token
    
    response = requests.get(url, params=params)
    
    if response.status_code == 404:
        error = response.json()
        print(f"Error: {error.get('message', 'Not found')}")
        return None
    
    response.raise_for_status()
    return response.json()

def get_all_job_runs(pipeline_id):
    """Retrieve all job runs using pagination"""
    all_runs = []
    next_token = None
    
    while True:
        data = get_glue_job_runs(pipeline_id, max_results=50, next_token=next_token)
        if not data:
            break
        
        all_runs.extend(data['runs'])
        print(f"Retrieved {len(data['runs'])} runs (total: {len(all_runs)})")
        
        if not data['has_more']:
            break
        
        next_token = data['next_token']
    
    return all_runs

# Usage
runs = get_all_job_runs('my-pipeline-123')

# Analyze runs
succeeded = sum(1 for r in runs if r['status'] == 'SUCCEEDED')
failed = sum(1 for r in runs if r['status'] == 'FAILED')
total_cost = sum(r['cost_estimate'] for r in runs)

print(f"\nJob Run Summary:")
print(f"Total Runs: {len(runs)}")
print(f"Succeeded: {succeeded}")
print(f"Failed: {failed}")
print(f"Success Rate: {succeeded/len(runs)*100:.1f}%")
print(f"Total Cost: ${total_cost:.2f}")
```

#### Implementation Notes

##### Performance Considerations

- **Glue API Call**: get_job_runs with pagination (~200-500ms per page)
- **Response Time**: Typically 300-600ms for 25 results
- **Caching Strategy**: Consider caching recent runs for 2-5 minutes
- **Pagination**: Use smaller page sizes (10-25) for faster initial load

##### Caching Implementation (Optional)

```python
import time
from functools import lru_cache

# Simple in-memory cache (5 minute TTL)
_cache = {}
_cache_ttl = 300  # 5 minutes

def get_cached_job_runs(pipeline_id, max_results, next_token):
    """Get job runs with caching for recent results"""
    cache_key = f"{pipeline_id}:{max_results}:{next_token or 'first'}"
    
    # Check cache
    if cache_key in _cache:
        cached_data, timestamp = _cache[cache_key]
        if time.time() - timestamp < _cache_ttl:
            logger.info(f"Cache hit for {cache_key}")
            return cached_data
    
    # Fetch from Glue API
    data = get_glue_job_runs(pipeline_id, max_results, next_token)
    
    # Store in cache
    _cache[cache_key] = (data, time.time())
    
    # Clean old cache entries (simple cleanup)
    if len(_cache) > 100:
        oldest_key = min(_cache.keys(), key=lambda k: _cache[k][1])
        del _cache[oldest_key]
    
    return data
```

##### IAM Permissions Required

The Lambda execution role needs:
```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:GetItem",
    "glue:GetJobRuns"
  ],
  "Resource": [
    "arn:aws:dynamodb:*:*:table/agentic_data_ingestion_pipelines",
    "arn:aws:glue:*:*:job/agentic-data-ingestion-*"
  ]
}
```

##### Cost Calculation

Cost estimates are calculated using:
- **Glue 5.0 Pricing**: $0.44 per DPU-hour
- **Formula**: `cost = (dpu_seconds / 3600) * 0.44`
- **Note**: Running jobs show $0.00 until completion

##### Status Meanings

- **STARTING**: Job is initializing resources
- **RUNNING**: Job is actively processing data
- **STOPPING**: Job is being gracefully stopped
- **STOPPED**: Job was stopped by user action
- **SUCCEEDED**: Job completed successfully
- **FAILED**: Job failed with an error
- **TIMEOUT**: Job exceeded configured timeout
- **ERROR**: Job encountered a system error

---

## Related Endpoints

- `GET /api/pipelines/{id}` - Get pipeline details
- `GET /api/pipelines/{id}/executions` - Get pipeline execution history
- `GET /api/pipelines/{id}/glue-job` - Get Glue job details (Task 9.1) ✅ IMPLEMENTED
- `POST /api/pipelines/{id}/glue-job/run` - Start Glue job execution (Task 9.2) ✅ IMPLEMENTED
- `GET /api/pipelines/{id}/glue-job/runs` - Get job run history (Task 9.3) ✅ IMPLEMENTED
- `GET /api/pipelines/{id}/glue-job/runs/{run_id}/logs` - Get job logs (Task 9.4)


---

### GET /api/pipelines/{pipeline_id}/glue-job/runs/{run_id}/logs

Fetch CloudWatch logs for a specific Glue job run with support for filtering, search, and pagination.

#### Request

```
GET /api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123def456/logs?log_type=output&filter_pattern=ERROR&limit=100
```

#### Query Parameters

- `log_type` (string, optional): Type of logs to fetch
  - Values: `output`, `error`, `driver`
  - Default: `output`
  - `output`: Standard job output logs
  - `error`: Error logs and stack traces
  - `driver`: Spark driver logs (advanced debugging)
  
- `filter_pattern` (string, optional): CloudWatch Logs filter pattern for searching
  - Examples: `ERROR`, `"records processed"`, `[timestamp, level=ERROR*, ...]`
  - Uses CloudWatch Logs filter syntax
  - Case-sensitive
  
- `start_time` (integer, optional): Start time for log retrieval
  - Unix timestamp in milliseconds
  - Filters logs after this time
  
- `end_time` (integer, optional): End time for log retrieval
  - Unix timestamp in milliseconds
  - Filters logs before this time
  
- `limit` (integer, optional): Maximum number of log events to return
  - Default: 100
  - Range: 1-10000
  - Values outside range are clamped automatically
  
- `next_token` (string, optional): Pagination token from previous response
  - Used to retrieve the next page of logs
  - Omit for first page

#### Response (200 OK)

```json
{
  "pipeline_id": "my-pipeline-123",
  "job_name": "agentic-data-ingestion-job-my-pipeline-123",
  "run_id": "jr_abc123def456",
  "log_type": "output",
  "log_group": "/aws-glue/jobs/output",
  "log_stream": "jr_abc123def456",
  "logs": [
    {
      "timestamp": 1698765432000,
      "timestamp_iso": "2023-10-31T12:30:32.000Z",
      "message": "INFO: Starting Glue job execution",
      "log_level": "INFO",
      "ingestion_time": 1698765433000
    },
    {
      "timestamp": 1698765435000,
      "timestamp_iso": "2023-10-31T12:30:35.000Z",
      "message": "INFO: Reading data from s3://bucket/input/",
      "log_level": "INFO",
      "ingestion_time": 1698765436000
    },
    {
      "timestamp": 1698765440000,
      "timestamp_iso": "2023-10-31T12:30:40.000Z",
      "message": "ERROR: Failed to parse record at line 1234",
      "log_level": "ERROR",
      "ingestion_time": 1698765441000
    },
    {
      "timestamp": 1698765445000,
      "timestamp_iso": "2023-10-31T12:30:45.000Z",
      "message": "WARN: Skipping invalid record",
      "log_level": "WARN",
      "ingestion_time": 1698765446000
    }
  ],
  "total_returned": 4,
  "has_more": true,
  "next_forward_token": "f/37234567890123456789012345678901234567890123456789012345",
  "next_backward_token": "b/37234567890123456789012345678901234567890123456789012345",
  "filter_pattern": "ERROR",
  "job_status": "RUNNING",
  "started_at": "2023-10-31T12:30:00.000Z",
  "completed_at": "",
  "cloudwatch_console_url": "https://console.aws.amazon.com/cloudwatch/home?region=us-east-1#logsV2:log-groups/log-group/$252Faws-glue$252Fjobs$252Foutput/log-events/jr_abc123def456"
}
```

#### Response Fields

**Top-Level:**
- `pipeline_id` (string): Pipeline identifier
- `job_name` (string): AWS Glue job name
- `run_id` (string): Job run identifier
- `log_type` (string): Type of logs returned (output, error, driver)
- `log_group` (string): CloudWatch log group name
- `log_stream` (string): CloudWatch log stream name
- `logs` (array): List of log entry objects (see below)
- `total_returned` (integer): Number of log entries in this response
- `has_more` (boolean): Whether more logs are available
- `next_forward_token` (string): Token for next page (chronologically forward)
- `next_backward_token` (string): Token for previous page (chronologically backward)
- `filter_pattern` (string): Applied filter pattern (null if none)
- `job_status` (string): Current job run status (RUNNING, SUCCEEDED, FAILED, etc.)
- `started_at` (string): ISO 8601 timestamp when job started
- `completed_at` (string): ISO 8601 timestamp when job completed (empty if still running)
- `cloudwatch_console_url` (string): Direct link to CloudWatch console for these logs

**Log Entry Object:**
- `timestamp` (integer): Unix timestamp in milliseconds when log was generated
- `timestamp_iso` (string): ISO 8601 formatted timestamp
- `message` (string): Log message text
- `log_level` (string): Parsed log level (INFO, WARN, ERROR, DEBUG, UNKNOWN)
- `ingestion_time` (integer): Unix timestamp when log was ingested by CloudWatch

#### Log Types

##### Output Logs (`output`)
- **Log Group**: `/aws-glue/jobs/output`
- **Content**: Standard job output, progress messages, data processing logs
- **Use Case**: Monitor job progress, see data processing statistics
- **Example Messages**:
  ```
  INFO: Starting Glue job execution
  INFO: Reading 1,234,567 records from S3
  INFO: Processed 500,000 records (40.5%)
  INFO: Writing data to target table
  INFO: Job completed successfully
  ```

##### Error Logs (`error`)
- **Log Group**: `/aws-glue/jobs/error`
- **Content**: Error messages, exceptions, stack traces
- **Use Case**: Debug job failures, identify data quality issues
- **Example Messages**:
  ```
  ERROR: Failed to parse record at line 1234
  ERROR: java.lang.OutOfMemoryError: Java heap space
  ERROR: Schema mismatch: expected 'string', got 'int'
  ```

##### Driver Logs (`driver`)
- **Log Group**: `/aws-glue/jobs/logs-v2`
- **Content**: Spark driver logs, cluster management, resource allocation
- **Use Case**: Advanced debugging, performance tuning
- **Example Messages**:
  ```
  INFO: Starting Spark application
  INFO: Allocated 5 executors with 4 cores each
  WARN: Task 123 took 45s (GC time: 12s)
  ```

#### Filter Pattern Examples

##### Simple Text Search
```
# Find all ERROR messages
filter_pattern=ERROR

# Find specific text
filter_pattern="records processed"

# Case-sensitive search
filter_pattern="OutOfMemoryError"
```

##### Structured Filtering
```
# Find ERROR level logs
filter_pattern=[timestamp, level=ERROR*, ...]

# Find logs with specific field values
filter_pattern=[timestamp, level, message="*failed*"]

# Complex pattern
filter_pattern=[timestamp, level=ERROR* || level=WARN*, message]
```

##### Time-Based Filtering
```
# Logs from last hour
start_time=1698765432000
end_time=1698769032000

# Logs after specific time
start_time=1698765432000

# Logs before specific time
end_time=1698769032000
```

#### Error Responses

**404 Not Found - Pipeline Not Found:**
```json
{
  "error": "Pipeline not found: my-pipeline-123"
}
```

**404 Not Found - No Glue Job:**
```json
{
  "error": "No Glue job found for pipeline: my-pipeline-123",
  "message": "Glue job has not been created yet for this pipeline"
}
```

**404 Not Found - Job Run Not Found:**
```json
{
  "error": "Job run not found: jr_abc123def456",
  "message": "No job run found with ID jr_abc123def456 for job agentic-data-ingestion-job-my-pipeline-123"
}
```

**404 Not Found - Log Stream Not Found:**
```json
{
  "error": "Log stream not found",
  "message": "No logs available yet for run jr_abc123def456. The job may still be starting or logs may not have been created.",
  "log_group": "/aws-glue/jobs/output",
  "log_stream": "jr_abc123def456"
}
```

**400 Bad Request - Invalid Parameters:**
```json
{
  "error": "Invalid log query parameters",
  "message": "Invalid filter pattern syntax"
}
```

**404 Not Found - Log Resource Not Found:**
```json
{
  "error": "Log resource not found",
  "message": "The requested log group or stream does not exist. Logs may not have been created yet."
}
```

**500 Internal Server Error:**
```json
{
  "error": "Error fetching logs for pipeline my-pipeline-123, run jr_abc123def456",
  "details": "..."
}
```

#### Usage Examples

##### cURL

```bash
# Get recent output logs
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123/logs"

# Search for errors
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123/logs?filter_pattern=ERROR"

# Get error logs
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123/logs?log_type=error"

# Time-based filtering
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123/logs?start_time=1698765432000&end_time=1698769032000"

# Pagination
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123/logs?limit=50&next_token=f%2F37234567890"

# Combined filtering
curl -X GET "https://api.agentic_data_ingestion.com/api/pipelines/my-pipeline-123/glue-job/runs/jr_abc123/logs?log_type=output&filter_pattern=ERROR&limit=200"
```

##### JavaScript (Dashboard)

```javascript
async function getJobLogs(pipelineId, runId, options = {}) {
  const {
    logType = 'output',
    filterPattern = null,
    startTime = null,
    endTime = null,
    limit = 100,
    nextToken = null
  } = options;
  
  let url = `/api/pipelines/${pipelineId}/glue-job/runs/${runId}/logs?log_type=${logType}&limit=${limit}`;
  
  if (filterPattern) {
    url += `&filter_pattern=${encodeURIComponent(filterPattern)}`;
  }
  if (startTime) {
    url += `&start_time=${startTime}`;
  }
  if (endTime) {
    url += `&end_time=${endTime}`;
  }
  if (nextToken) {
    url += `&next_token=${encodeURIComponent(nextToken)}`;
  }
  
  const response = await fetch(url);
  
  if (!response.ok) {
    if (response.status === 404) {
      const error = await response.json();
      console.log('Logs not found:', error.message);
      return null;
    }
    throw new Error('Failed to fetch job logs');
  }
  
  const data = await response.json();
  console.log(`Retrieved ${data.total_returned} log entries`);
  
  return data;
}

// Display logs in UI
function displayLogs(logs) {
  const logContainer = document.getElementById('log-viewer');
  logContainer.innerHTML = '';
  
  logs.forEach(log => {
    const logEntry = document.createElement('div');
    logEntry.className = `log-entry log-${log.log_level.toLowerCase()}`;
    
    const timestamp = new Date(log.timestamp_iso).toLocaleTimeString();
    
    logEntry.innerHTML = `
      <span class="log-timestamp">${timestamp}</span>
      <span class="log-level ${log.log_level.toLowerCase()}">${log.log_level}</span>
      <span class="log-message">${escapeHtml(log.message)}</span>
    `;
    
    logContainer.appendChild(logEntry);
  });
}

// Real-time log streaming for running jobs
async function streamLogs(pipelineId, runId) {
  let nextToken = null;
  let isRunning = true;
  
  while (isRunning) {
    const data = await getJobLogs(pipelineId, runId, {
      logType: 'output',
      limit: 50,
      nextToken: nextToken
    });
    
    if (!data) break;
    
    // Display new logs
    displayLogs(data.logs);
    
    // Check if job is still running
    isRunning = data.job_status === 'RUNNING';
    
    // Update token for next fetch
    nextToken = data.next_forward_token;
    
    // Wait before next fetch (5 seconds)
    if (isRunning) {
      await new Promise(resolve => setTimeout(resolve, 5000));
    }
  }
  
  console.log('Job completed, log streaming stopped');
}

// Search logs
async function searchLogs(pipelineId, runId, searchTerm) {
  const data = await getJobLogs(pipelineId, runId, {
    filterPattern: searchTerm,
    limit: 200
  });
  
  if (data) {
    displayLogs(data.logs);
    document.getElementById('search-results-count').textContent = 
      `Found ${data.total_returned} matching log entries`;
  }
}

// Filter by log level
async function filterByLevel(pipelineId, runId, level) {
  const data = await getJobLogs(pipelineId, runId, {
    filterPattern: level,
    limit: 200
  });
  
  if (data) {
    displayLogs(data.logs);
  }
}
```

##### Python

```python
import requests
from datetime import datetime, timedelta

def get_job_logs(pipeline_id, run_id, log_type='output', filter_pattern=None, 
                 start_time=None, end_time=None, limit=100, next_token=None):
    """Fetch CloudWatch logs for a Glue job run"""
    url = f"https://api.agentic_data_ingestion.com/api/pipelines/{pipeline_id}/glue-job/runs/{run_id}/logs"
    
    params = {
        'log_type': log_type,
        'limit': limit
    }
    
    if filter_pattern:
        params['filter_pattern'] = filter_pattern
    if start_time:
        params['start_time'] = start_time
    if end_time:
        params['end_time'] = end_time
    if next_token:
        params['next_token'] = next_token
    
    response = requests.get(url, params=params)
    
    if response.status_code == 404:
        error = response.json()
        print(f"Error: {error.get('message', 'Not found')}")
        return None
    
    response.raise_for_status()
    return response.json()

def get_all_logs(pipeline_id, run_id, log_type='output', filter_pattern=None):
    """Retrieve all logs using pagination"""
    all_logs = []
    next_token = None
    
    while True:
        data = get_job_logs(
            pipeline_id, run_id, log_type=log_type,
            filter_pattern=filter_pattern, limit=1000, next_token=next_token
        )
        
        if not data:
            break
        
        all_logs.extend(data['logs'])
        print(f"Retrieved {len(data['logs'])} logs (total: {len(all_logs)})")
        
        if not data['has_more']:
            break
        
        next_token = data['next_forward_token']
    
    return all_logs

def search_errors(pipeline_id, run_id):
    """Search for error messages in logs"""
    data = get_job_logs(pipeline_id, run_id, filter_pattern='ERROR', limit=500)
    
    if not data:
        return []
    
    errors = []
    for log in data['logs']:
        if log['log_level'] == 'ERROR':
            errors.append({
                'timestamp': log['timestamp_iso'],
                'message': log['message']
            })
    
    return errors

def analyze_logs(pipeline_id, run_id):
    """Analyze logs and generate summary"""
    logs = get_all_logs(pipeline_id, run_id)
    
    # Count by log level
    level_counts = {}
    for log in logs:
        level = log['log_level']
        level_counts[level] = level_counts.get(level, 0) + 1
    
    # Find errors
    errors = [log for log in logs if log['log_level'] == 'ERROR']
    
    # Calculate time span
    if logs:
        first_log = datetime.fromisoformat(logs[0]['timestamp_iso'].replace('Z', '+00:00'))
        last_log = datetime.fromisoformat(logs[-1]['timestamp_iso'].replace('Z', '+00:00'))
        duration = (last_log - first_log).total_seconds()
    else:
        duration = 0
    
    print(f"\nLog Analysis for Run {run_id}:")
    print(f"Total Logs: {len(logs)}")
    print(f"Duration: {duration:.1f} seconds")
    print(f"\nLog Levels:")
    for level, count in sorted(level_counts.items()):
        print(f"  {level}: {count}")
    print(f"\nErrors Found: {len(errors)}")
    
    if errors:
        print("\nFirst 5 Errors:")
        for error in errors[:5]:
            print(f"  [{error['timestamp']}] {error['message'][:100]}")
    
    return {
        'total_logs': len(logs),
        'duration_seconds': duration,
        'level_counts': level_counts,
        'errors': errors
    }

# Usage examples
if __name__ == '__main__':
    pipeline_id = 'my-pipeline-123'
    run_id = 'jr_abc123def456'
    
    # Get recent logs
    logs = get_job_logs(pipeline_id, run_id, limit=50)
    if logs:
        print(f"Retrieved {logs['total_returned']} logs")
        print(f"Job Status: {logs['job_status']}")
    
    # Search for errors
    errors = search_errors(pipeline_id, run_id)
    print(f"\nFound {len(errors)} errors")
    
    # Analyze all logs
    analysis = analyze_logs(pipeline_id, run_id)
```

#### Implementation Notes

##### Performance Considerations

- **CloudWatch API Call**: filter_log_events (~200-1000ms depending on log volume)
- **Response Time**: Typically 300-1200ms for 100 log entries
- **Large Log Sets**: Use pagination to avoid timeouts
- **Caching**: Consider caching completed job logs (immutable)

##### Real-Time Log Streaming

For running jobs, implement polling:
1. Fetch logs every 5-10 seconds
2. Use `next_forward_token` to get only new logs
3. Stop polling when `job_status` is not "RUNNING"
4. Display logs in real-time in UI

##### Log Level Detection

The `parse_log_level()` function detects log levels using pattern matching:
- **ERROR**: Contains "ERROR", "EXCEPTION", "FAILED"
- **WARN**: Contains "WARN", "WARNING"
- **DEBUG**: Contains "DEBUG"
- **INFO**: Contains "INFO"
- **UNKNOWN**: No recognizable pattern

##### IAM Permissions Required

The Lambda execution role needs:
```json
{
  "Effect": "Allow",
  "Action": [
    "dynamodb:GetItem",
    "glue:GetJobRun",
    "logs:FilterLogEvents",
    "logs:DescribeLogStreams"
  ],
  "Resource": [
    "arn:aws:dynamodb:*:*:table/agentic_data_ingestion_pipelines",
    "arn:aws:glue:*:*:job/agentic-data-ingestion-*",
    "arn:aws:logs:*:*:log-group:/aws-glue/jobs/*"
  ]
}
```

##### CloudWatch Logs Pricing

- **Ingestion**: $0.50 per GB
- **Storage**: $0.03 per GB per month
- **Data Transfer**: $0.01 per GB (out to internet)
- **API Calls**: Included in AWS Free Tier (5 GB ingestion, 5 GB archive)

##### Best Practices

1. **Use Appropriate Log Type**
   - Start with `output` logs for general monitoring
   - Use `error` logs for debugging failures
   - Use `driver` logs only for advanced troubleshooting

2. **Implement Pagination**
   - Don't fetch all logs at once
   - Use reasonable page sizes (100-500 entries)
   - Provide UI controls for navigation

3. **Filter Effectively**
   - Use filter patterns to reduce data transfer
   - Combine with time-based filtering
   - Cache filter results

4. **Handle Missing Logs**
   - Jobs may take 30-60 seconds to start logging
   - Show helpful message when logs not available yet
   - Implement retry logic with exponential backoff

5. **Export Options**
   - Allow downloading logs as text file
   - Provide CloudWatch console link
   - Support copying to clipboard

---

## Summary

All Glue Jobs API endpoints are now implemented:

✅ **Task 9.1**: GET /api/pipelines/{id}/glue-job - Get job details  
✅ **Task 9.2**: POST /api/pipelines/{id}/glue-job/run - Start job execution  
✅ **Task 9.3**: GET /api/pipelines/{id}/glue-job/runs - Get run history  
✅ **Task 9.4**: GET /api/pipelines/{id}/glue-job/runs/{run_id}/logs - Get job logs

### Next Steps

1. **Dashboard UI Implementation** (Task 9.5)
   - Create log viewer component
   - Add search and filter controls
   - Implement real-time log streaming
   - Add export functionality

2. **Enhanced Features**
   - Log analytics and pattern detection
   - Saved filter presets
   - Email log summaries
   - Integration with alerting systems

3. **Performance Optimization**
   - Implement log caching
   - Add WebSocket support for real-time streaming
   - Optimize pagination
   - Add log compression for downloads
