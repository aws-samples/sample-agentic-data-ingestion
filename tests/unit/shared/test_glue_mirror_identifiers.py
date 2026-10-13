# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The Glue mirror's ``(database, table)`` pair — one definition, pinned.

``create_infrastructure`` writes an S3 Tables table's Glue Catalog mirror under
a database derived from the table bucket name and a table derived from the
namespace and table name (``create_infrastructure/lambda_function.py:272`` and
``:273``). Two more sites will read that same pair after p35 — the
``Schema_Listing`` exclusion (R5.7) and ``job_completion``'s
``metadata_location`` writer (R7.3) — so the derivation now lives once in
``shared/glue_mirror_identifiers.py`` with ``create_infrastructure`` as its
defining caller (``LGR-acca05d-1``, template / ratified; its ``revisit_when`` is
*precisely* "``create_infrastructure`` changes either derivation").

What these tests pin:

1. The two expressions, on the two inputs that make them interesting — a bucket
   name containing **hyphens** (the ``replace('-', '_')`` half) and a namespace
   containing **underscores** (the join that cannot be decomposed back, DD7's
   axis 2).
2. That the helper is byte-identical to what ``create_infrastructure`` used to
   compute inline. Asserted against that module's **source** rather than
   transcribed, so the shared definition and its defining caller cannot drift
   apart the way `LGR-cad8032-1`'s two schema-address definitions did.
3. That the helper does **not** normalise the namespace. ``create_infrastructure``
   does it at ``:161`` before calling, and a second normalisation here would
   make the helper disagree with the writer it exists to describe.

**Validates: Requirements 5.7**
"""

import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, os.path.join(str(PROJECT_ROOT), "shared"))

from glue_mirror_identifiers import GLUE_MIRROR_DATABASE_PREFIX, glue_mirror_identifiers

_CREATE_INFRASTRUCTURE_SOURCE = (
    PROJECT_ROOT / "lambda" / "agentic_data_ingestion_create_infrastructure" / "lambda_function.py"
)


class TestTheTwoExpressions:
    def test_hyphenated_bucket_name_becomes_an_underscored_database(self):
        database, table = glue_mirror_identifiers("adi-table-bucket-444455556666", "ns", "orders")

        assert database == "agentic_data_ingestion_adi_table_bucket_444455556666"
        assert table == "ns_orders"

    def test_underscored_namespace_is_joined_verbatim(self):
        # DD7 axis 2: the namespace's own underscores survive into the table
        # name, which is exactly why the pair cannot be recovered from the
        # joined string and why callers must carry both values.
        database, table = glue_mirror_identifiers("testingbucket", "testing_bucket_ns", "testing_bucket_tn")

        assert database == "agentic_data_ingestion_testingbucket"
        assert table == "testing_bucket_ns_testing_bucket_tn"

    def test_both_halves_at_once(self):
        assert glue_mirror_identifiers("my-bucket", "my_ns", "my_table") == (
            "agentic_data_ingestion_my_bucket",
            "my_ns_my_table",
        )

    def test_bucket_without_hyphens_is_unchanged_beyond_the_prefix(self):
        database, _ = glue_mirror_identifiers("plainbucket", "ns", "t")

        assert database == f"{GLUE_MIRROR_DATABASE_PREFIX}plainbucket"

    def test_returns_a_plain_tuple_usable_as_a_set_member(self):
        # The exclusion (task 4.3) builds a set of these and tests a plain
        # (database, table) tuple for membership, so equality with a bare tuple
        # is part of the contract rather than an implementation detail.
        pair = glue_mirror_identifiers("a-b", "ns", "t")

        assert isinstance(pair, tuple)
        assert pair in {("agentic_data_ingestion_a_b", "ns_t")}

    def test_only_hyphens_are_replaced_in_the_bucket_name(self):
        # No case folding, no dot handling, no stripping — the derivation is
        # whatever create_infrastructure writes, nothing more.
        database, _ = glue_mirror_identifiers("Mixed-Case.Bucket", "ns", "t")

        assert database == "agentic_data_ingestion_Mixed_Case.Bucket"

    def test_namespace_hyphens_are_NOT_replaced(self):
        # create_infrastructure normalises namespace_name at :161, BEFORE the
        # derivation. Repeating it here would silently diverge from the writer.
        _, table = glue_mirror_identifiers("bucket", "not-normalised", "t")

        assert table == "not-normalised_t"


class TestLossyDatabaseDerivationIsBoundedNotFixed:
    def test_two_buckets_can_collide_on_one_database(self):
        # R5.8 states the bound rather than removing the collision:
        # replace('-','_') is not injective, and the exclusion's
        # S3-Tables-row → Glue-row direction is what keeps the cost to one
        # suppressed mirror. A "fix" here (hashing, escaping) would stop
        # matching what create_infrastructure writes.
        assert glue_mirror_identifiers("a-b", "ns", "t")[0] == glue_mirror_identifiers("a_b", "ns", "t")[0]


class TestTheHelperAgreesWithItsDefiningCaller:
    """The refactor's whole claim: same pair, one definition."""

    def test_create_infrastructure_calls_the_helper_and_holds_no_inline_copy(self):
        source = _CREATE_INFRASTRUCTURE_SOURCE.read_text()

        assert "from glue_mirror_identifiers import glue_mirror_identifiers" in source
        assert "catalog_db, catalog_table = glue_mirror_identifiers(bucket_name, namespace, table_name)" in source

        # Neither derivation may survive as a second definition anywhere in the
        # module — that second definition is the drift LGR-acca05d-1 fences.
        assert not re.search(r"agentic_data_ingestion_\{bucket_name", source)
        assert not re.search(r"catalog_table\s*=\s*f?\"\{namespace\}_\{table_name\}\"", source)

    def test_pair_matches_the_registration_call_that_consumes_it(self):
        # The registration passes catalog_db as DatabaseName and catalog_table
        # as TableInput["Name"]; this is the value those two arguments now carry
        # for the live fixture used throughout p35's design.
        assert glue_mirror_identifiers("testingbucket", "testingbucketns", "testingbuckettn") == (
            "agentic_data_ingestion_testingbucket",
            "testingbucketns_testingbuckettn",
        )
