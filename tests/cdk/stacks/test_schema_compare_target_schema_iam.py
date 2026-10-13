# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK assertion: schemaCompareRole can read the target-schemas bucket.

``schema_compare``'s only S3 read is the target schema object, located by the
pipeline record's ``target_schema_bucket`` + ``target_schema_s3_key`` pair —
i.e. the target-schemas bucket. The role had no grant on that bucket because
the read was aimed at the wrong one (``generated_code_bucket``, an attribute no
writer sets) and was in any case unreachable: the target-schema check returned
early whenever the pipeline had no stored baseline version, which was every
pipeline. ``LGR-eee8ac8-4`` removed that early return so a first successful run
can establish the baseline, making the read live for the first time.

This assertion lives at the CDK layer because a unit test cannot see it: the
unit tests stub ``s3_client``, so an IAM gap is invisible to them. Fixing only
the Python would have turned ``Invalid bucket name ""`` into ``AccessDenied``,
which ``raise_for_read_client_error`` correctly raises as a step failure — a
different error, the same failed run.
"""

import os
import re

import pytest

FOUNDATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "foundation-stack.ts"
)


@pytest.fixture(scope="module")
def foundation_stack_content() -> str:
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


def _schema_compare_role_section(content: str) -> str:
    """The schemaCompareRole definition plus every addToPolicy call on it."""
    pattern = r"(this\.schemaCompareRole\s*=\s*lambdaRole.*?)(?=this\.\w+Role\s*=\s*(?:lambdaRole|new\s+iam\.Role))"
    match = re.search(pattern, content, re.DOTALL)
    assert match, "schemaCompareRole definition not found in foundation-stack.ts"
    return match.group(1)


def test_schema_compare_role_can_read_target_schemas_bucket(foundation_stack_content):
    """The bucket the Lambda actually reads must be in the role's resources."""
    section = _schema_compare_role_section(foundation_stack_content)

    assert "this.targetSchemasBucket.bucketArn" in section, (
        "schemaCompareRole has no grant on targetSchemasBucket — schema_compare's "
        "target-schema read will fail with AccessDenied"
    )
    assert "${this.targetSchemasBucket.bucketArn}/*" in section, (
        "schemaCompareRole grants the bucket but not its objects — s3:GetObject needs the /* object ARN"
    )


def test_schema_compare_role_grants_getobject_on_that_bucket(foundation_stack_content):
    """Guard against the grant being present but on the wrong actions."""
    section = _schema_compare_role_section(foundation_stack_content)

    statements = [
        section[m.start() : section.find("}))", m.start())]
        for m in re.finditer(r"new iam\.PolicyStatement\(\{", section)
    ]
    target_statements = [s for s in statements if "targetSchemasBucket" in s]

    assert target_statements, "no PolicyStatement references targetSchemasBucket"
    assert any("s3:GetObject" in s for s in target_statements), (
        "the targetSchemasBucket grant does not include s3:GetObject"
    )
