# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
MCP Integration Module with Fallback Mechanisms
Provides circuit breaker patterns and automatic fallback to boto3 implementations
"""

import json
import logging
import os
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from functools import wraps
from typing import Any

import boto3
import requests
from botocore.exceptions import ClientError
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# Configure logging
logger = logging.getLogger(__name__)


class CircuitBreakerError(Exception):
    """Raised when circuit breaker is open"""

    pass


class MCPIntegrationError(Exception):
    """Raised when MCP integration fails"""

    pass


class CircuitBreaker:
    """Circuit breaker implementation for MCP operations"""

    def __init__(self, failure_threshold: int = 5, recovery_timeout: int = 60, expected_exception: type = Exception):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.expected_exception = expected_exception
        self.failure_count = 0
        self.last_failure_time = None
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN

    def call(self, func: Callable, *args, **kwargs):
        """Execute function with circuit breaker protection"""
        if self.state == "OPEN":
            if self._should_attempt_reset():
                self.state = "HALF_OPEN"
            else:
                raise CircuitBreakerError(f"Circuit breaker is OPEN. Last failure: {self.last_failure_time}")

        try:
            result = func(*args, **kwargs)
            self._on_success()
            return result
        except self.expected_exception as e:
            self._on_failure()
            raise e

    def _should_attempt_reset(self) -> bool:
        """Check if enough time has passed to attempt reset"""
        if self.last_failure_time is None:
            return True
        return datetime.now() - self.last_failure_time > timedelta(seconds=self.recovery_timeout)

    def _on_success(self):
        """Reset circuit breaker on successful call"""
        self.failure_count = 0
        self.state = "CLOSED"
        self.last_failure_time = None

    def _on_failure(self):
        """Handle failure and potentially open circuit breaker"""
        self.failure_count += 1
        self.last_failure_time = datetime.now()

        if self.failure_count >= self.failure_threshold:
            self.state = "OPEN"
            logger.warning(f"Circuit breaker opened after {self.failure_count} failures")


class MCPClient:
    """MCP client with fallback mechanisms and intelligent caching"""

    def __init__(self):
        self.agentcore_endpoint = os.environ.get("AGENTCORE_ENDPOINT")
        self.agent_id = os.environ.get("AGENT_ID")
        self.agent_alias_id = os.environ.get("AGENT_ALIAS_ID")
        self.region = os.environ.get("AWS_REGION", "us-east-1")

        # Circuit breakers for different operation types
        self.circuit_breakers = {
            "aws_operations": CircuitBreaker(failure_threshold=3, recovery_timeout=30),
            "code_generation": CircuitBreaker(failure_threshold=2, recovery_timeout=60),
            "optimization": CircuitBreaker(failure_threshold=5, recovery_timeout=120),
        }

        # Response cache
        self.cache = {}
        self.cache_ttl = 300  # 5 minutes

        # Cost tracking
        self.cost_threshold = float(os.environ.get("MCP_COST_THRESHOLD", "100.0"))
        self.daily_cost = 0.0
        self.cost_reset_date = datetime.now().date()

        # Initialize boto3 clients for fallback
        self.boto3_clients = {}

        # Configure HTTP session with retries
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _get_boto3_client(self, service_name: str):
        """Get or create boto3 client for fallback operations"""
        if service_name not in self.boto3_clients:
            self.boto3_clients[service_name] = boto3.client(service_name, region_name=self.region)
        return self.boto3_clients[service_name]

    def _is_cache_valid(self, cache_key: str) -> bool:
        """Check if cached response is still valid"""
        if cache_key not in self.cache:
            return False

        cached_time = self.cache[cache_key]["timestamp"]
        return (datetime.now() - cached_time).seconds < self.cache_ttl

    def _get_cached_response(self, cache_key: str) -> dict[str, Any] | None:
        """Get cached response if valid"""
        if self._is_cache_valid(cache_key):
            logger.info(f"Using cached response for: {cache_key}")
            return self.cache[cache_key]["data"]
        return None

    def _cache_response(self, cache_key: str, data: dict[str, Any]):
        """Cache response data"""
        self.cache[cache_key] = {"data": data, "timestamp": datetime.now()}

    def _check_cost_threshold(self, estimated_cost: float) -> bool:
        """Check if operation would exceed cost threshold"""
        # Reset daily cost if new day
        if datetime.now().date() > self.cost_reset_date:
            self.daily_cost = 0.0
            self.cost_reset_date = datetime.now().date()

        if self.daily_cost + estimated_cost > self.cost_threshold:
            logger.warning(f"Cost threshold exceeded: {self.daily_cost + estimated_cost} > {self.cost_threshold}")
            return False

        return True

    def _track_cost(self, actual_cost: float):
        """Track actual cost of operation"""
        self.daily_cost += actual_cost
        logger.info(f"Daily MCP cost: ${self.daily_cost:.4f}")

    def _call_agentcore(self, operation: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Call AgentCore MCP server with error handling"""
        if not self.agentcore_endpoint or not self.agent_id:
            raise MCPIntegrationError("AgentCore configuration not available")

        # Check cost threshold (estimate $0.01 per operation)
        if not self._check_cost_threshold(0.01):
            raise MCPIntegrationError("Daily cost threshold exceeded")

        # Prepare request
        bedrock_runtime = boto3.client("bedrock-agent-runtime", region_name=self.region)

        session_id = f"mcp-session-{int(time.time())}"
        input_text = f"Execute MCP operation: {operation} with parameters: {json.dumps(parameters)}"

        try:
            response = bedrock_runtime.invoke_agent(
                agentId=self.agent_id, agentAliasId=self.agent_alias_id, sessionId=session_id, inputText=input_text
            )

            # Process streaming response
            result_text = ""
            for event in response["completion"]:
                if "chunk" in event:
                    chunk = event["chunk"]
                    if "bytes" in chunk:
                        result_text += chunk["bytes"].decode("utf-8")

            # Track cost (estimate based on tokens)
            estimated_cost = len(input_text) * 0.000001  # Rough estimate
            self._track_cost(estimated_cost)

            # Parse result
            try:
                return json.loads(result_text)
            except json.JSONDecodeError:
                return {"result": result_text, "raw_response": True}

        except Exception as e:
            logger.error(f"AgentCore call failed: {e}")
            raise MCPIntegrationError(f"AgentCore operation failed: {e}")

    def list_s3_objects(self, bucket: str, prefix: str = "") -> dict[str, Any]:
        """List S3 objects with MCP optimization and fallback"""
        cache_key = f"s3_list_{bucket}_{prefix}"

        # Check cache first
        cached_result = self._get_cached_response(cache_key)
        if cached_result:
            return cached_result

        # Try MCP operation with circuit breaker
        try:
            result = self.circuit_breakers["aws_operations"].call(
                self._call_agentcore, "list_s3_objects", {"bucket": bucket, "prefix": prefix}
            )

            # Cache successful result
            self._cache_response(cache_key, result)
            return result

        except (CircuitBreakerError, MCPIntegrationError) as e:
            logger.warning(f"MCP operation failed, falling back to boto3: {e}")
            return self._fallback_list_s3_objects(bucket, prefix)

    def _fallback_list_s3_objects(self, bucket: str, prefix: str = "") -> dict[str, Any]:
        """Fallback S3 list operation using boto3"""
        try:
            s3_client = self._get_boto3_client("s3")

            response = s3_client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=1000)

            objects = []
            if "Contents" in response:
                objects = [obj["Key"] for obj in response["Contents"]]

            result = {"objects": objects, "fallback_used": True, "operation": "list_s3_objects"}

            logger.info(f"Fallback S3 list completed: {len(objects)} objects")
            return result

        except ClientError as e:
            logger.error(f"Fallback S3 operation failed: {e}")
            raise MCPIntegrationError(f"Both MCP and fallback failed for S3 list: {e}")

    def optimize_glue_job(
        self, job_name: str, data_size: str = "medium", transformation_type: str = "basic"
    ) -> dict[str, Any]:
        """Optimize Glue job configuration with MCP intelligence and fallback"""
        cache_key = f"glue_optimize_{job_name}_{data_size}_{transformation_type}"

        # Check cache first
        cached_result = self._get_cached_response(cache_key)
        if cached_result:
            return cached_result

        # Try MCP operation with circuit breaker
        try:
            result = self.circuit_breakers["optimization"].call(
                self._call_agentcore,
                "optimize_glue_job",
                {"job_name": job_name, "data_size": data_size, "transformation_type": transformation_type},
            )

            # Cache successful result
            self._cache_response(cache_key, result)
            return result

        except (CircuitBreakerError, MCPIntegrationError) as e:
            logger.warning(f"MCP optimization failed, using fallback: {e}")
            return self._fallback_optimize_glue_job(job_name, data_size, transformation_type)

    def _fallback_optimize_glue_job(self, job_name: str, data_size: str, transformation_type: str) -> dict[str, Any]:
        """Fallback Glue job optimization using predefined rules"""
        # Simple rule-based optimization
        optimization_rules = {
            "small": {"worker_type": "G.1X", "number_of_workers": 2, "max_capacity": 2},
            "medium": {"worker_type": "G.1X", "number_of_workers": 5, "max_capacity": 10},
            "large": {"worker_type": "G.2X", "number_of_workers": 10, "max_capacity": 20},
            "xlarge": {"worker_type": "G.2X", "number_of_workers": 20, "max_capacity": 40},
        }

        config = optimization_rules.get(data_size, optimization_rules["medium"])

        # Adjust for transformation type
        if transformation_type == "complex":
            config["number_of_workers"] = int(config["number_of_workers"] * 1.5)
            config["max_capacity"] = int(config["max_capacity"] * 1.5)

        result = {
            **config,
            "fallback_used": True,
            "operation": "optimize_glue_job",
            "optimization_level": "basic_rules",
        }

        logger.info(f"Fallback Glue optimization completed: {result}")
        return result

    def generate_pyspark_code(
        self, source_schema: dict, target_schema: dict, mappings: dict, optimization_level: str = "standard"
    ) -> dict[str, Any]:
        """Generate PySpark code with MCP intelligence and fallback"""
        cache_key = f"pyspark_gen_{hash(str(source_schema))}_{hash(str(target_schema))}_{hash(str(mappings))}"

        # Check cache first
        cached_result = self._get_cached_response(cache_key)
        if cached_result:
            return cached_result

        # Try MCP operation with circuit breaker
        try:
            result = self.circuit_breakers["code_generation"].call(
                self._call_agentcore,
                "generate_pyspark_code",
                {
                    "source_schema": source_schema,
                    "target_schema": target_schema,
                    "mappings": mappings,
                    "optimization_level": optimization_level,
                },
            )

            # Cache successful result (shorter TTL for code generation)
            self.cache[cache_key] = {"data": result, "timestamp": datetime.now()}
            return result

        except (CircuitBreakerError, MCPIntegrationError) as e:
            logger.warning(f"MCP code generation failed, using fallback: {e}")
            return self._fallback_generate_pyspark_code(source_schema, target_schema, mappings)

    def _fallback_generate_pyspark_code(
        self, source_schema: dict, target_schema: dict, mappings: dict
    ) -> dict[str, Any]:
        """Fallback PySpark code generation using templates"""
        # Load basic template
        template_code = '''
from pyspark.sql import SparkSession
from pyspark.sql.functions import *
from pyspark.sql.types import *

def transform_data(spark, input_path, output_path):
    """Generated ETL transformation function"""
    
    # Read source data
    df = spark.read.option("header", "true").csv(input_path)
    
    # Apply transformations
    transformed_df = df.select(
        {select_expressions}
    )
    
    # Write to target
    transformed_df.write.mode("overwrite").parquet(output_path)
    
    return transformed_df.count()

if __name__ == "__main__":
    spark = SparkSession.builder.appName("AgenticDataIngestion-ETL").getOrCreate()
    
    input_path = sys.argv[1]
    output_path = sys.argv[2]
    
    record_count = transform_data(spark, input_path, output_path)
    print(f"Processed {{record_count}} records")
    
    spark.stop()
'''

        # Generate select expressions from mappings
        select_expressions = []
        for target_field, source_field in mappings.items():
            if isinstance(source_field, str):
                select_expressions.append(f'col("{source_field}").alias("{target_field}")')
            elif isinstance(source_field, list):
                # Handle multiple source fields
                col_exprs = ", ".join([f'col("{f}")' for f in source_field])
                concat_expr = f'concat({col_exprs}).alias("{target_field}")'
                select_expressions.append(concat_expr)

        generated_code = template_code.format(select_expressions=",\n        ".join(select_expressions))

        result = {
            "code": generated_code,
            "fallback_used": True,
            "operation": "generate_pyspark_code",
            "template_used": "basic_etl",
            "optimizations": ["basic_column_mapping"],
            "estimated_cost": 0.0,
        }

        logger.info("Fallback PySpark code generation completed")
        return result

    def get_health_status(self) -> dict[str, Any]:
        """Get MCP integration health status"""
        status = {
            "agentcore_configured": bool(self.agentcore_endpoint and self.agent_id),
            "circuit_breakers": {},
            "daily_cost": self.daily_cost,
            "cost_threshold": self.cost_threshold,
            "cache_size": len(self.cache),
            "fallback_available": True,
        }

        # Circuit breaker status
        for name, cb in self.circuit_breakers.items():
            status["circuit_breakers"][name] = {
                "state": cb.state,
                "failure_count": cb.failure_count,
                "last_failure": cb.last_failure_time.isoformat() if cb.last_failure_time else None,
            }

        return status

    def reset_circuit_breakers(self):
        """Reset all circuit breakers (for testing/recovery)"""
        for cb in self.circuit_breakers.values():
            cb.failure_count = 0
            cb.state = "CLOSED"
            cb.last_failure_time = None
        logger.info("All circuit breakers reset")

    def clear_cache(self):
        """Clear response cache"""
        self.cache.clear()
        logger.info("Response cache cleared")


# Global MCP client instance
mcp_client = MCPClient()


# Decorator for automatic MCP integration with fallback
def with_mcp_fallback(operation_type: str = "aws_operations"):
    """Decorator to add MCP integration with fallback to any function"""

    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            try:
                # Try MCP operation first
                return mcp_client.circuit_breakers[operation_type].call(func, *args, **kwargs)
            except (CircuitBreakerError, MCPIntegrationError) as e:
                logger.warning(f"MCP operation failed for {func.__name__}: {e}")
                # Call original function as fallback
                return func(*args, **kwargs)

        return wrapper

    return decorator


# Health check function for monitoring
def check_mcp_health() -> dict[str, Any]:
    """Check MCP integration health for monitoring"""
    return mcp_client.get_health_status()


# Cost monitoring function
def get_mcp_costs() -> dict[str, Any]:
    """Get MCP cost information for monitoring"""
    return {
        "daily_cost": mcp_client.daily_cost,
        "cost_threshold": mcp_client.cost_threshold,
        "cost_utilization": (mcp_client.daily_cost / mcp_client.cost_threshold) * 100,
        "reset_date": mcp_client.cost_reset_date.isoformat(),
    }
