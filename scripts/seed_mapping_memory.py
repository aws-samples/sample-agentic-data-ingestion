#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""
One-time seed script: load approved schema mappings into AgentCore Memory.

Reads approved mappings from `agentic_data_ingestion_schema_mappings` and `agentic_data_ingestion_approvals`
tables, then stores them in Memory so future mapping runs benefit from historical
patterns.

Usage:
    python scripts/seed_mapping_memory.py                  # seed all approved mappings
    python scripts/seed_mapping_memory.py --dry-run        # count without writing
    python scripts/seed_mapping_memory.py --limit 50       # cap at 50 mappings
"""
import argparse
import logging
import os
import sys
import time

import boto3

# Add shared directory to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'shared'))

from mapping_memory import MappingMemoryClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def get_approved_approval_ids(approvals_table) -> dict:
    """Scan approvals table for approved schema_mapping records.

    Returns:
        Dict mapping approval_id -> approval record.
    """
    approved = {}
    scan_kwargs = {
        'FilterExpression': '#s = :status AND #t = :atype',
        'ExpressionAttributeNames': {'#s': 'status', '#t': 'approval_type'},
        'ExpressionAttributeValues': {':status': 'approved', ':atype': 'schema_mapping'},
    }
    while True:
        response = approvals_table.scan(**scan_kwargs)
        for item in response.get('Items', []):
            approved[item['approval_id']] = item
        if 'LastEvaluatedKey' not in response:
            break
        scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    logger.info("Found %d approved schema_mapping approvals", len(approved))
    return approved


def get_mappings_for_approvals(mappings_table, approved_ids: dict) -> list:
    """Scan schema_mappings table and match against approved approval IDs.

    Each mapping record has a `mapping_id` (PK) and may reference an
    `approval_id`. We also look at `proposed_mappings` on the approval
    record itself as a fallback.

    Returns:
        List of dicts with source_field, target_field, confidence,
        pipeline_id, and approved=True.
    """
    results = []
    seen = set()

    # Strategy 1: pull proposed_mappings directly from approval records
    for approval_id, approval in approved_ids.items():
        pipeline_id = approval.get('pipeline_id', '')
        for mapping in approval.get('proposed_mappings', []):
            key = (mapping.get('source_field', ''), mapping.get('target_field', ''))
            if key not in seen and key[0] and key[1]:
                seen.add(key)
                results.append({
                    'source_field': mapping['source_field'],
                    'target_field': mapping['target_field'],
                    'confidence': mapping.get('confidence', 0.5),
                    'pipeline_id': pipeline_id,
                })

    # Strategy 2: scan mappings table for records with status=approved
    scan_kwargs = {}
    while True:
        response = mappings_table.scan(**scan_kwargs)
        for item in response.get('Items', []):
            if item.get('status') != 'approved':
                continue
            for mapping in item.get('mappings', []):
                key = (mapping.get('source_field', ''), mapping.get('target_field', ''))
                if key not in seen and key[0] and key[1]:
                    seen.add(key)
                    results.append({
                        'source_field': mapping['source_field'],
                        'target_field': mapping['target_field'],
                        'confidence': mapping.get('confidence', 0.5),
                        'pipeline_id': item.get('pipeline_id', ''),
                    })
        if 'LastEvaluatedKey' not in response:
            break
        scan_kwargs['ExclusiveStartKey'] = response['LastEvaluatedKey']

    logger.info("Collected %d unique field mappings to seed", len(results))
    return results


def seed_memory(mappings: list, memory_client: MappingMemoryClient, dry_run: bool = False) -> dict:
    """Store each mapping in Memory.

    Returns:
        Dict with counts: stored, failed, skipped (dry_run).
    """
    stored = 0
    failed = 0

    for mapping in mappings:
        if dry_run:
            logger.info(
                "  [dry-run] %s -> %s (confidence=%.0f%%, pipeline=%s)",
                mapping['source_field'],
                mapping['target_field'],
                mapping['confidence'],
                mapping['pipeline_id'],
            )
            continue

        ok = memory_client.store_mapping_decision(
            source_field=mapping['source_field'],
            target_field=mapping['target_field'],
            confidence=mapping['confidence'],
            pipeline_id=mapping['pipeline_id'],
            approved=True,
        )
        if ok:
            stored += 1
        else:
            failed += 1

    if dry_run:
        return {'stored': 0, 'failed': 0, 'skipped': len(mappings)}
    return {'stored': stored, 'failed': failed, 'skipped': 0}


def run_seed(dry_run: bool = False, limit: int = 0):
    """Main entry point: scan tables, collect mappings, seed Memory."""
    dynamodb = boto3.resource('dynamodb')
    approvals_table = dynamodb.Table(
        os.environ.get('APPROVALS_TABLE', 'agentic_data_ingestion_approvals')
    )
    mappings_table = dynamodb.Table(
        os.environ.get('MAPPINGS_TABLE', 'agentic_data_ingestion_schema_mappings')
    )
    memory_client = MappingMemoryClient()

    start = time.time()

    approved = get_approved_approval_ids(approvals_table)
    mappings = get_mappings_for_approvals(mappings_table, approved)

    if limit > 0:
        mappings = mappings[:limit]
        logger.info("Limited to %d mappings", limit)

    result = seed_memory(mappings, memory_client, dry_run=dry_run)
    elapsed = time.time() - start

    logger.info(
        "Seed complete in %.1fs — stored=%d, failed=%d, skipped=%d",
        elapsed,
        result['stored'],
        result['failed'],
        result['skipped'],
    )
    return result


def main():
    parser = argparse.ArgumentParser(description="Seed AgentCore Memory with approved mappings")
    parser.add_argument("--dry-run", action="store_true", help="Count mappings without writing to Memory")
    parser.add_argument("--limit", type=int, default=0, help="Max mappings to seed (0 = unlimited)")
    args = parser.parse_args()

    run_seed(dry_run=args.dry_run, limit=args.limit)


if __name__ == "__main__":
    main()
