FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f

# Set working directory
WORKDIR /app

# Install system dependencies including curl for health checks
# --no-install-recommends, as the agentcore/*/Dockerfiles already do: gcc's
# recommended packages pull in linux-libc-dev (kernel headers), which carried
# the only CRITICAL and 60 of the HIGH findings in the 2026-10-12 Trivy scan
# and which nothing here compiles against.
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Build context is mcp/ (not this server dir) so the single vendored awslabs
# copy under mcp/vendor/ is reachable (P28 R19.7) — see agentcore-stack.ts
# (DockerImageAsset directory: mcp/, file: codegen_server/Dockerfile).

# Copy requirements and install Python dependencies
COPY codegen_server/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the codegen MCP server code + the single vendored awslabs fork
# (provenance: mcp/vendor/awslabs_aws_dataprocessing_mcp_server/PROVENANCE.md)
COPY codegen_server/server.py .
COPY vendor/awslabs_aws_dataprocessing_mcp_server/awslabs/ ./awslabs/

# Create non-root user for security
RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

# Set environment variables for AWS and AgentCore
ENV AWS_REGION=us-west-2
ENV AWS_DEFAULT_REGION=us-west-2
ENV PYTHONUNBUFFERED=1
ENV MCP_SERVER_HOST=0.0.0.0
ENV MCP_SERVER_PORT=8000

# Expose the MCP server port (required for AgentCore)
EXPOSE 8000

# Health check for AgentCore Runtime
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD ps aux | grep -v grep | grep "python server.py" || exit 1

# Run the codegen MCP server with OTEL auto-instrumentation
CMD ["opentelemetry-instrument", "python", "server.py"]
