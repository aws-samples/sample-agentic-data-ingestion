FROM python:3.12-slim@sha256:05cda9777409a9c3ffddd94a4c476b79f0769a0b4857f0c7ed9226b6800b0d6f
# Cache bust: P13a trace plugin v2 2026-03-16T15

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc curl \
    && rm -rf /var/lib/apt/lists/*

COPY agentcore/create_code/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && pip install --no-cache-dir aws-opentelemetry-distro==0.18.0

COPY shared /app/shared
COPY lambda/agentic_data_ingestion_create_code /app/lambda/agentic_data_ingestion_create_code
COPY pyspark_templates /app/pyspark_templates
COPY agentcore/create_code/main.py .

ENV PYTHONPATH="/app:/app/shared:/app/lambda/agentic_data_ingestion_create_code"

# Create non-root user for security (CKV_DOCKER_3). Placed after every COPY
# so the chown covers the whole tree; nothing below this line writes to /app.
# Mirrors the mcp/*/Dockerfile pattern, which already ran as uid 1000.
RUN useradd -m -u 1000 agentuser && chown -R agentuser:agentuser /app
USER agentuser
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD curl -f http://localhost:8080/ping || exit 1

CMD ["opentelemetry-instrument", "python", "main.py"]
