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

# Copy requirements and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the custom AgentCore S3 Tables MCP server code
COPY agentcore_s3tables_mcp.py ./agentcore_s3tables_mcp.py

# Create non-root user for security
RUN useradd -m -u 1000 mcpuser && chown -R mcpuser:mcpuser /app
USER mcpuser

# Set environment variables for AWS and AgentCore
# Note: AgentCore will override these with the correct region
ENV AWS_REGION=us-east-1
ENV AWS_DEFAULT_REGION=us-east-1
ENV PYTHONUNBUFFERED=1
ENV MCP_SERVER_HOST=0.0.0.0
ENV MCP_SERVER_PORT=8000

# Expose the MCP server port (required for AgentCore)
EXPOSE 8000

# Health check for AgentCore Runtime
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/mcp || curl -f http://localhost:8000/ || exit 1

# Run the custom AgentCore S3 Tables MCP server with OTEL auto-instrumentation
CMD ["opentelemetry-instrument", "python", "agentcore_s3tables_mcp.py"]
