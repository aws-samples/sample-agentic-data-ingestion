# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Worker sizing, cost estimation, and recommendation helpers.

Extracted from ``lambda_function.py`` (P21 Req 4.2) as part of decomposing the
oversized create_glue_pipeline entrypoint into focused sibling modules. This
module owns Glue worker-configuration sizing, DPU/cost estimation, and the
agent/rule-based worker-recommendation logic. The moves are behavior-preserving
(verbatim function bodies); ``lambda_function.py`` imports these names back so
the orchestration functions (``create_glue_job``, ``deploy_glue_pipeline_with_mcp``,
``lambda_handler``) keep working unchanged.

Dependency direction is one-way: ``lambda_function`` → ``workers``. This module
MUST NOT import ``lambda_function``.
"""

import json
import os
import sys
from dataclasses import dataclass
from typing import Any

import boto3
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Add shared directory to path for imports (Layer_Import_Fallback — /opt/python
# in the Lambda layer, else the local ../../shared path for local/test runs).
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.7).
try:
    from env_utils import require_env
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "shared"))
    from env_utils import require_env

# Typed DynamoDB item accessor (P22 Req 2) — narrow boto3-stubs' wide
# AttributeValueTypeDef union to a concrete dict at the item read site.
from dynamo_typing import item_dict

# Environment variables
PIPELINE_RUNS_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")

# Initialize AWS clients
# Convention: one module-level client per AWS service (see shared/README.md).
dynamodb = boto3.resource("dynamodb")

# DynamoDB tables
pipeline_runs_table = dynamodb.Table(PIPELINE_RUNS_TABLE)


@dataclass
class WorkerRecommendation:
    """Worker configuration recommendation from Strands Agent"""

    worker_type: str
    number_of_workers: int
    timeout_minutes: int
    estimated_cost_per_run: float
    estimated_duration_minutes: int
    rationale: str
    confidence: float
    mcp_tools_used: list

    def to_dict(self) -> dict[str, Any]:
        return {
            "worker_type": self.worker_type,
            "number_of_workers": self.number_of_workers,
            "timeout_minutes": self.timeout_minutes,
            "estimated_cost_per_run": self.estimated_cost_per_run,
            "estimated_duration_minutes": self.estimated_duration_minutes,
            "rationale": self.rationale,
            "confidence": self.confidence,
            "mcp_tools_used": self.mcp_tools_used,
        }


def get_baseline_worker_config(file_size_bytes: int, file_count: int = 1) -> dict:
    """Return baseline Glue worker configuration based on data volume.

    Tiers:
        <1 GB  → G.1X / 2 workers / 30 min timeout
        1-10 GB → G.1X / 5 workers / 60 min timeout
        10-50 GB → G.2X / 10 workers / 90 min timeout
        >50 GB → G.2X / 20 workers / 120 min timeout
    """
    file_count = min(file_count, 10000)
    total_gb = (file_size_bytes * file_count) / (1024**3)
    if total_gb < 1:
        return {"worker_type": "G.1X", "number_of_workers": 2, "timeout": 30}
    elif total_gb < 10:
        return {"worker_type": "G.1X", "number_of_workers": 5, "timeout": 60}
    elif total_gb < 50:
        return {"worker_type": "G.2X", "number_of_workers": 10, "timeout": 90}
    else:
        return {"worker_type": "G.2X", "number_of_workers": 20, "timeout": 120}


def incorporate_test_recommendations_into_job_config(
    job_config: dict[str, Any], test_results: dict[str, Any], worker_recommendation: WorkerRecommendation
) -> dict[str, Any]:
    """
    Incorporate useful test recommendations into Glue job configuration.

    Args:
        job_config: Base Glue job configuration
        test_results: Test results with recommendations and performance hints
        worker_recommendation: Worker configuration recommendation

    Returns:
        dict: Enhanced job configuration with test recommendations applied
    """
    try:
        performance_hints = test_results.get("performance_hints", {})
        recommendations = test_results.get("recommendations", [])

        logger.info("Incorporating test recommendations into job config")
        logger.info(f"Performance hints: {performance_hints}")
        logger.info(f"Recommendations: {recommendations}")

        # Apply memory recommendations
        if "memory_gb" in performance_hints:
            memory_gb = performance_hints["memory_gb"]
            logger.info(f"Test recommends {memory_gb}GB memory")

            # Adjust worker type if memory requirement is high
            if memory_gb > 32 and worker_recommendation.worker_type in ["G.025X", "G.1X"]:
                logger.info(
                    f"Upgrading worker type from {worker_recommendation.worker_type} to G.2X for memory requirements"
                )
                job_config["WorkerType"] = "G.2X"
                job_config["DefaultArguments"]["--memory-recommendation"] = f"{memory_gb}GB"

        # Apply partition count recommendations
        if "partition_count" in performance_hints:
            partition_count = performance_hints["partition_count"]
            logger.info(f"Test recommends {partition_count} partitions")
            job_config["DefaultArguments"]["--recommended-partitions"] = str(partition_count)

        # Apply worker type override if test strongly recommends it
        if "worker_type" in performance_hints:
            recommended_worker = performance_hints["worker_type"]
            logger.info(f"Test recommends worker type: {recommended_worker}")
            # Only override if test recommendation is more powerful
            current_dpu = get_worker_dpu(job_config.get("WorkerType", "G.1X"))
            recommended_dpu = get_worker_dpu(recommended_worker)
            if recommended_dpu > current_dpu:
                logger.info(f"Upgrading worker type from {job_config.get('WorkerType')} to {recommended_worker}")
                job_config["WorkerType"] = recommended_worker

        # Add all recommendations as job arguments for reference
        if recommendations:
            job_config["DefaultArguments"]["--test-recommendations"] = json.dumps(recommendations)

        # Add test validation status
        job_config["DefaultArguments"]["--code-tested"] = str(test_results.get("test_passed", False))
        job_config["DefaultArguments"]["--test-status"] = test_results.get("test_status", "unknown")

        logger.info("Enhanced job config with test recommendations")
        return job_config

    except Exception as e:
        logger.error(f"Error incorporating test recommendations: {e}")
        # Return original config if enhancement fails
        return job_config


def get_worker_dpu(worker_type: str) -> float:
    """Get DPU value for worker type"""
    dpu_map = {"G.025X": 0.25, "G.1X": 1.0, "G.2X": 2.0, "G.4X": 4.0, "G.8X": 8.0, "Z.2X": 2.0}
    return dpu_map.get(worker_type, 1.0)


def estimate_job_cost(worker_config: dict[str, Any]) -> dict[str, Any]:
    """
    Estimate Glue 5.0 job cost based on worker configuration.

    Glue 5.0 pricing (us-east-1):
    - $0.44 per DPU-hour
    - 1-second billing increments (improved from 1-minute in Glue 4.0)
    - 10-minute minimum billing per job

    DPU per worker type:
    - G.025X: 0.25 DPU
    - G.1X: 1 DPU
    - G.2X: 2 DPU
    - G.4X: 4 DPU
    - G.8X: 8 DPU
    - Z.2X: 2 DPU (memory-optimized)

    Args:
        worker_config: Worker configuration dict with WorkerType, NumberOfWorkers,
                      estimated_duration_minutes, and optionally data_volume_gb

    Returns:
        dict: Cost breakdown including per-run, monthly, per-GB, and DPU-hours
    """
    try:
        # DPU per worker type
        dpu_per_worker = {"G.025X": 0.25, "G.1X": 1.0, "G.2X": 2.0, "G.4X": 4.0, "G.8X": 8.0, "Z.2X": 2.0}

        # Extract configuration
        worker_type = worker_config.get("WorkerType", worker_config.get("worker_type", "G.1X"))
        num_workers = worker_config.get("NumberOfWorkers", worker_config.get("number_of_workers", 2))
        estimated_duration_minutes = worker_config.get("estimated_duration_minutes", 30)
        data_volume_gb = worker_config.get("data_volume_gb", 1.0)

        # Calculate total DPU
        total_dpu = dpu_per_worker.get(worker_type, 1.0) * num_workers

        # Glue 5.0 pricing: $0.44 per DPU-hour
        cost_per_dpu_hour = 0.44

        # Calculate cost
        estimated_duration_hours = estimated_duration_minutes / 60
        estimated_cost = total_dpu * cost_per_dpu_hour * estimated_duration_hours

        # Apply 10-minute minimum billing
        minimum_duration_hours = 10 / 60
        minimum_cost = total_dpu * cost_per_dpu_hour * minimum_duration_hours

        final_cost = max(estimated_cost, minimum_cost)

        # Calculate monthly cost estimate (assuming daily runs)
        monthly_cost = final_cost * 30

        # Calculate cost per GB processed
        cost_per_gb = final_cost / data_volume_gb if data_volume_gb > 0 else 0

        # Calculate DPU-hours
        dpu_hours = total_dpu * estimated_duration_hours

        cost_breakdown = {
            "estimated_cost_per_run": round(final_cost, 3),
            "minimum_cost": round(minimum_cost, 3),
            "cost_per_hour": round(total_dpu * cost_per_dpu_hour, 2),
            "cost_per_gb": round(cost_per_gb, 3),
            "estimated_monthly_cost": round(monthly_cost, 2),
            "total_dpu": total_dpu,
            "dpu_hours": round(dpu_hours, 2),
            "worker_type": worker_type,
            "num_workers": num_workers,
            "estimated_duration_minutes": estimated_duration_minutes,
            "billing_note": "Glue 5.0 uses 1-second billing increments with 10-minute minimum",
        }

        logger.info(
            f"Cost estimation: ${final_cost:.3f}/run, {dpu_hours:.2f} DPU-hours, ${monthly_cost:.2f}/month (30 runs)"
        )

        return cost_breakdown

    except Exception as e:
        logger.error(f"Error estimating job cost: {e}")
        # Return default cost estimate
        return {
            "estimated_cost_per_run": 0.0,
            "minimum_cost": 0.0,
            "cost_per_hour": 0.0,
            "cost_per_gb": 0.0,
            "estimated_monthly_cost": 0.0,
            "total_dpu": 0.0,
            "dpu_hours": 0.0,
            "worker_type": "unknown",
            "num_workers": 0,
            "estimated_duration_minutes": 0,
            "billing_note": "Cost estimation failed",
            "error": str(e),
        }


def get_data_characteristics(pipeline_id: str, run_id: str, event: dict) -> dict[str, Any]:
    """
    Extract data characteristics from pipeline metadata and previous steps.

    Returns:
        dict: Data characteristics including volume, complexity, record count
    """
    try:
        # Try to get from event first
        data_volume_gb = event.get("data_volume_gb", 0)
        record_count = event.get("record_count", 0)
        complexity = event.get("transformation_complexity", "medium")

        # If not in event, try to query from previous steps
        if data_volume_gb == 0:
            try:
                # Query file validation step for data volume
                response = pipeline_runs_table.get_item(Key={"run_id": run_id, "step_name": "file_validation"})
                if "Item" in response:
                    output_data = item_dict(response["Item"], "output_data")
                    data_volume_gb = output_data.get("file_size_mb", 0) / 1024.0
                    record_count = output_data.get("record_count", 0)
            except Exception as e:
                logger.warning(f"Could not retrieve data characteristics from previous steps: {e}")

        # Estimate complexity based on schema
        target_schema = event.get("target_schema", {})
        field_count = len(target_schema.get("fields", []))

        if field_count < 10:
            complexity = "simple"
        elif field_count < 30:
            complexity = "medium"
        else:
            complexity = "complex"

        return {
            "data_volume_gb": max(data_volume_gb, 0.1),  # Minimum 0.1 GB
            "record_count": max(record_count, 1000),  # Minimum 1000 records
            "transformation_complexity": complexity,
            "field_count": field_count,
            "target_type": event.get("target_type", "S3 Table Bucket"),
        }

    except Exception as e:
        logger.error(f"Error extracting data characteristics: {e}")
        # Return safe defaults
        return {
            "data_volume_gb": 1.0,
            "record_count": 10000,
            "transformation_complexity": "medium",
            "field_count": 10,
            "target_type": "S3 Table Bucket",
        }


def get_worker_recommendation_with_agent(
    agent, mcp_client, data_characteristics: dict[str, Any], target_config: dict[str, Any]
) -> WorkerRecommendation:
    """
    Use Strands Agent with MCP tools to recommend optimal Glue worker configuration.

    Args:
        agent: Initialized Strands Agent with MCP tools
        data_characteristics: Data volume, complexity, record count
        target_config: Target type and configuration details

    Returns:
        WorkerRecommendation: Recommended worker configuration with rationale
    """
    try:
        # Build detailed prompt for agent
        recommendation_prompt = f"""Analyze the following data characteristics and recommend optimal AWS Glue job configuration.

Data Characteristics:
- Data Volume: {data_characteristics["data_volume_gb"]:.2f} GB
- Record Count: {data_characteristics["record_count"]:,}
- Transformation Complexity: {data_characteristics["transformation_complexity"]}
- Field Count: {data_characteristics["field_count"]}
- Target Type: {data_characteristics["target_type"]}

Target Configuration:
{json.dumps(target_config, indent=2)}

Use available MCP tools to:
1. Analyze the data volume and complexity
2. Calculate optimal worker type and count
3. Estimate job duration and cost
4. Consider target type requirements (S3 Tables/Iceberg may need more memory)

Provide your recommendation in the following JSON format:
{{
    "worker_type": "G.1X|G.2X|G.025X|G.4X|G.8X|Z.2X",
    "number_of_workers": <integer 2-50>,
    "timeout_minutes": <integer 15-2880>,
    "estimated_cost_per_run": <float>,
    "estimated_duration_minutes": <integer>,
    "rationale": "<detailed explanation of your recommendation>",
    "confidence": <float 0.0-1.0>
}}

Worker Type Guidelines (for batch ETL jobs):
- G.1X: <10 GB data, standard transformations, 2-5 workers, $0.44/DPU-hour (minimum for batch jobs)
- G.2X: 10-100 GB data, medium complexity, 5-10 workers, $0.88/DPU-hour
- G.2X: 10-100 GB data, medium complexity, 5-10 workers, $0.88/DPU-hour
- G.4X: 100-500 GB data, complex transformations, 10-25 workers, $1.76/DPU-hour
- G.8X: >500 GB data, very complex, 25-50 workers, $3.52/DPU-hour
- Z.2X: Memory-intensive operations, 10-50 workers, $1.00/DPU-hour

Consider:
- Glue 5.0 auto-scaling capabilities
- Cost optimization vs performance
- Target type requirements (Iceberg needs more memory)
- Transformation complexity multipliers
"""

        logger.info("Requesting worker recommendation from Strands Agent with MCP tools...")
        logger.info(f"Data characteristics: {data_characteristics}")

        # Get recommendation from agent with proper MCP context management
        if mcp_client:
            logger.info("Running worker recommendation agent with MCP tools in context")
            with mcp_client:
                response = agent(recommendation_prompt)
        else:
            logger.info("Running worker recommendation agent in standard mode")
            response = agent(recommendation_prompt)

        # Log the full agent response
        logger.info(f"Agent response: {response}")

        # Parse agent response
        # Try to extract JSON from response
        response_text = str(response)

        # Look for JSON in the response
        import re

        json_match = re.search(r'\{[^{}]*"worker_type"[^{}]*\}', response_text, re.DOTALL)

        if json_match:
            recommendation_json = json.loads(json_match.group(0))

            # Extract MCP tools used from agent metadata if available
            mcp_tools_used = []
            if hasattr(response, "tool_calls"):
                mcp_tools_used = [tool.name for tool in response.tool_calls]

            recommendation = WorkerRecommendation(
                worker_type=recommendation_json.get("worker_type", "G.1X"),
                number_of_workers=recommendation_json.get("number_of_workers", 2),
                timeout_minutes=recommendation_json.get("timeout_minutes", 60),
                estimated_cost_per_run=recommendation_json.get("estimated_cost_per_run", 0.0),
                estimated_duration_minutes=recommendation_json.get("estimated_duration_minutes", 30),
                rationale=recommendation_json.get("rationale", "Agent recommendation"),
                confidence=recommendation_json.get("confidence", 0.8),
                mcp_tools_used=mcp_tools_used,
            )

            logger.info(
                f"Agent recommendation: {recommendation.worker_type}, {recommendation.number_of_workers} workers"
            )
            logger.info(
                f"Estimated cost: ${recommendation.estimated_cost_per_run:.2f}, duration: {recommendation.estimated_duration_minutes}min"
            )
            logger.info(f"Rationale: {recommendation.rationale}")
            logger.info(f"MCP tools used: {', '.join(mcp_tools_used) if mcp_tools_used else 'None detected'}")

            return recommendation
        else:
            logger.warning("Could not parse JSON from agent response, using fallback")
            raise ValueError("No valid JSON in agent response")

    except Exception as e:
        logger.error(f"Error getting agent recommendation: {e}")
        logger.warning("Falling back to rule-based worker recommendation")
        raise


def get_default_worker_recommendation(data_characteristics: dict[str, Any]) -> WorkerRecommendation:
    """
    Fallback rule-based worker recommendation when MCP/agent unavailable.

    Args:
        data_characteristics: Data volume, complexity, record count

    Returns:
        WorkerRecommendation: Rule-based worker configuration
    """
    data_volume_gb = data_characteristics["data_volume_gb"]
    complexity = data_characteristics["transformation_complexity"]

    # Complexity multipliers
    complexity_multipliers = {"simple": 1.0, "medium": 1.3, "complex": 1.6}
    multiplier = complexity_multipliers.get(complexity, 1.3)

    # Determine worker type and count based on data volume
    # Note: G.025X is only for streaming jobs, use G.1X as minimum for batch ETL
    if data_volume_gb < 10:
        worker_type = "G.1X"
        base_workers = 2
        timeout_minutes = 30
        dpu_per_worker = 1.0
    elif data_volume_gb < 100:
        worker_type = "G.2X"
        base_workers = max(3, int(data_volume_gb / 10))
        timeout_minutes = 90
        dpu_per_worker = 2.0
    elif data_volume_gb < 500:
        worker_type = "G.4X"
        base_workers = max(10, int(data_volume_gb / 25))
        timeout_minutes = 180
        dpu_per_worker = 4.0
    else:
        worker_type = "G.8X"
        base_workers = max(15, int(data_volume_gb / 50))
        timeout_minutes = 240
        dpu_per_worker = 8.0

    # Apply complexity multiplier
    number_of_workers = min(50, max(2, int(base_workers * multiplier)))

    # Estimate duration (rough estimate: 1 GB per minute per worker)
    estimated_duration_minutes = int((data_volume_gb / number_of_workers) * multiplier)
    estimated_duration_minutes = max(5, min(estimated_duration_minutes, timeout_minutes))

    # Estimate cost (DPU-hours * cost per DPU-hour)
    dpu_hours = (number_of_workers * dpu_per_worker * estimated_duration_minutes) / 60.0
    cost_per_dpu_hour = 0.44 if worker_type in ["G.025X", "G.1X"] else (0.88 if worker_type == "G.2X" else 1.76)
    estimated_cost = dpu_hours * cost_per_dpu_hour

    rationale = f"""Rule-based recommendation (MCP unavailable):
- Data volume: {data_volume_gb:.2f} GB
- Complexity: {complexity} (multiplier: {multiplier})
- Selected {worker_type} workers for this data size range
- {number_of_workers} workers to process data efficiently
- Estimated {estimated_duration_minutes} minutes based on data-to-worker ratio
- Timeout set to {timeout_minutes} minutes for safety margin
- Estimated cost: ${estimated_cost:.2f} ({dpu_hours:.2f} DPU-hours @ ${cost_per_dpu_hour}/DPU-hour)
"""

    return WorkerRecommendation(
        worker_type=worker_type,
        number_of_workers=number_of_workers,
        timeout_minutes=timeout_minutes,
        estimated_cost_per_run=estimated_cost,
        estimated_duration_minutes=estimated_duration_minutes,
        rationale=rationale,
        confidence=0.7,  # Lower confidence for rule-based
        mcp_tools_used=[],
    )
