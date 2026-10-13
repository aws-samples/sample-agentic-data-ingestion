# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The ApprovalHandler Lambda publishes MAPPINGS_TABLE (p54 R1.1, task 2.2).

THE FAILURE MODE THIS FILE EXISTS TO CATCH — read before editing.
=================================================================
``get_approval_data``'s reuse arm resolves a Reuse_Run's mapping by reading the
schema-mappings table, and ``approval_handler/lambda_function.py`` builds that table
handle at **module scope** through ``require_env``. ``require_env`` **raises** when the
variable is unset and the process is not in local mode
(``shared/env_utils.py:13-27``) — it does not fall back to its own default argument.

So omitting ``MAPPINGS_TABLE`` from this Lambda's environment is not a degraded reuse
read. It is an ``OSError`` at import, which means **every** schema-mapping approval —
reuse or not — dies on cold start, and the ``waitForTaskToken`` gate fails with it.

Before task 2.2 the ApprovalHandler construct had **no assertion of any kind**: grep
across ``deploy-cdk/test/`` and ``tests/cdk/`` for ``ApprovalHandler`` returned zero
hits. Task 2.2 made one of its environment keys load-bearing, so this is the first.

WHY SOURCE TEXT RATHER THAN A SYNTHESISED TEMPLATE
--------------------------------------------------
``test_lambda_env_table_names.py`` in this directory reads ``compute-stack.ts`` as text
for the same reason: this layer asserts what the CDK source says without paying for a
synth. The block is located by the construct id so a key added to a *different*
Lambda's environment cannot satisfy these assertions.

BOTH SIDES, ONE SPELLING
------------------------
The producer is asserted here and the consumer's spelling is asserted against it, so
the two cannot drift into a variable that is published under one name and read under
another — the shape ``LGR-87b8538-1`` calls "an inert flag on the live path while every
test passes". The other reader of the same table
(``dashboard_api/handlers/common.py``) already uses ``MAPPINGS_TABLE``, which is why
that is the spelling rather than a new one.

NOT ASSERTED HERE, AND DELIBERATELY: IAM
----------------------------------------
No grant accompanies this key. ``approvalConfigRole`` already carries
``dynamodb:GetItem`` on ``arn:aws:dynamodb:*:*:table/agentic_data_ingestion*``
(``foundation-stack.ts``), and the schema-mappings table is
``agentic_data_ingestion_schema_mappings``, so the wildcard covers it. Adding a
narrower statement would be a change to a shared role for no gain, and asserting the
wildcard here would duplicate ``tests/cdk/stacks``'s IAM files.
"""

import os
import re

import pytest

COMPUTE_STACK_PATH = os.path.join(os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "compute-stack.ts")
APPROVAL_HANDLER_PY_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_approval_handler",
    "lambda_function.py",
)


@pytest.fixture(scope="module")
def compute_stack_content() -> str:
    with open(COMPUTE_STACK_PATH) as handle:
        return handle.read()


@pytest.fixture(scope="module")
def approval_handler_block(compute_stack_content: str) -> str:
    """Just the ``new lambda.Function(this, 'ApprovalHandler', {...})`` construct.

    Bounded by the next ``new lambda.Function`` so a key on a sibling Lambda cannot
    satisfy the assertions below.
    """
    start = compute_stack_content.index("new lambda.Function(this, 'ApprovalHandler'")
    remainder = compute_stack_content[start + 1 :]
    next_function = remainder.find("new lambda.Function(")
    end = len(compute_stack_content) if next_function == -1 else start + 1 + next_function
    return compute_stack_content[start:end]


def test_the_approval_handler_publishes_mappings_table(approval_handler_block: str):
    """The key is present on THIS Lambda's environment.

    Its absence is a cold-start ``OSError`` on every schema-mapping approval, not a
    missing reuse read — see the module docstring.
    """
    assert re.search(r"^\s*MAPPINGS_TABLE:", approval_handler_block, re.MULTILINE), (
        "ApprovalHandler's environment has no MAPPINGS_TABLE. approval_handler's "
        "module-scope require_env('MAPPINGS_TABLE') raises outside local mode, so this "
        "omission fails every schema-mapping approval at import, reuse or not."
    )


def test_it_points_at_the_schema_mappings_table_construct(approval_handler_block: str):
    """The value is the table construct, not a re-spelled literal.

    A hardcoded ``'agentic_data_ingestion_schema_mappings'`` would work today and
    would silently point at nothing the moment the table is renamed, because nothing
    else in this stack would move with it.
    """
    match = re.search(r"^\s*MAPPINGS_TABLE:\s*(.+?),\s*$", approval_handler_block, re.MULTILINE)
    assert match is not None, "MAPPINGS_TABLE's value could not be parsed"
    assert match.group(1).strip() == "data.schemaMappingsTable.tableName", (
        f"expected the table construct's own name; got {match.group(1).strip()!r}"
    )


def test_the_producer_and_the_consumer_agree_on_the_spelling(approval_handler_block: str):
    """Published as ``MAPPINGS_TABLE`` and read as ``MAPPINGS_TABLE``.

    ``dashboard_api/handlers/common.py`` — the only other reader of this table —
    already uses that spelling, so this is agreement with an existing convention
    rather than a new one.
    """
    with open(APPROVAL_HANDLER_PY_PATH) as handle:
        handler_source = handle.read()

    assert 'require_env("MAPPINGS_TABLE"' in handler_source, (
        "the consumer does not read MAPPINGS_TABLE — a published variable nothing reads "
        "is the inert half of the two-sided wiring problem"
    )
    assert "MAPPINGS_TABLE" in approval_handler_block, "the producer does not publish it"
