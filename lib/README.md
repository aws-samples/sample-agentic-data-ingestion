# Agentic Data Ingestion Library Dependencies

This directory contains JAR files and other dependencies required for AWS Glue jobs.

## S3 Tables JAR

The S3 Tables catalog JAR for Apache Iceberg is managed via CDK. The canonical copy lives in `deploy-cdk/glue-jars/` and is automatically uploaded to S3 during `cdk deploy`.

**Purpose**: Provides S3 Tables catalog support for Apache Iceberg format in AWS Glue jobs.

**How it works**:
- CDK uploads the JAR from `deploy-cdk/glue-jars/` to `s3://adi-dependencies-{ACCOUNT_ID}-{REGION}/glue-jars/`
- Glue jobs reference it using the `--extra-jars` parameter

## Adding New Dependencies

To add new JAR dependencies:

1. Place the JAR file in `deploy-cdk/glue-jars/`
2. Update the CDK stack to upload and reference the new JAR
3. Document the dependency in this README
