# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The one definition of an S3 Tables table's Glue mirror identifiers.

``create_infrastructure`` registers every S3 Tables Iceberg table in the Glue
Data Catalog so the Data Quality console has something to describe
(``LGR-7c18e81-1``, backfilled). It derives the mirror's ``(database, table)``
pair from the table bucket's name, the namespace and the table name. Three
sites need that same pair:

1. the registration itself — ``create_infrastructure/lambda_function.py``,
   which is this module's **first and defining caller**;
2. ``Schema_Listing``'s mirror exclusion (p35 R5.7), which drops the
   ``glue_catalog`` row a concrete S3 Tables row accounts for;
3. ``job_completion``'s ``metadata_location`` writer (p35 R7.3), which has to
   find the mirror it is updating.

``LGR-acca05d-1`` (**template / ratified**) states the dedupe's ``revisit_when``
as *precisely* "``create_infrastructure`` changes either derivation at ``:272``
or ``:273``". Two definitions of one identifier is how a reader and a writer
drift apart without either being wrong, so the derivation lives here once and
the writer is a caller like everyone else — the same move ``LGR-cad8032-1``
made for the uploaded-schema address.

**This module carries those two expressions and nothing else.** No validation,
no case folding, no normalisation of the namespace: ``create_infrastructure``
normalises ``namespace_name`` itself before the derivation (``:161``), and
adding a second normalisation here would make the helper disagree with the
writer it exists to describe. Anything a caller wants beyond the pair belongs
at the caller.

**The database derivation is lossy, deliberately.** ``replace('-', '_')`` is
not injective — buckets ``a-b`` and ``a_b`` both derive
``agentic_data_ingestion_a_b``. p35 R5.8 bounds what that costs: the exclusion
matches S3-Tables-row → Glue-row only, so a collision can suppress one mirror
row idempotently and can never drop a real schema. Do not "fix" the collision
by hashing or escaping — that would stop matching what ``create_infrastructure``
writes, which is the only thing this function is for.
"""

from typing import Final

#: Prefix ``create_infrastructure`` puts on every mirror database
#: (``create_infrastructure/lambda_function.py:272``). Distinct from
#: ``create_glue_pipeline/iam_roles.py:673``'s per-pipeline
#: ``agentic_data_ingestion_{pipeline_id}`` databases, which share the prefix
#: but not the scheme — which is why callers must match the exact pair rather
#: than test this prefix.
GLUE_MIRROR_DATABASE_PREFIX: Final[str] = "agentic_data_ingestion_"


def glue_mirror_identifiers(bucket_name: str, namespace: str, table_name: str) -> tuple[str, str]:
    """Derive the Glue Catalog ``(database, table)`` pair for an S3 Tables table.

    Args:
        bucket_name: The S3 Tables table bucket name, as
            ``create_infrastructure`` resolved it from ``target_config``.
        namespace: The S3 Tables namespace, **already** underscore-normalised
            by the caller (``create_infrastructure/lambda_function.py:161``).
        table_name: The S3 Tables table name.

    Returns:
        ``(database, table)`` — ``agentic_data_ingestion_{bucket_name with '-'
        replaced by '_'}`` and ``{namespace}_{table_name}``. A plain tuple, so
        it can go straight into a set for the exclusion's membership test.
    """
    database = f"{GLUE_MIRROR_DATABASE_PREFIX}{bucket_name.replace('-', '_')}"
    table = f"{namespace}_{table_name}"
    return database, table
