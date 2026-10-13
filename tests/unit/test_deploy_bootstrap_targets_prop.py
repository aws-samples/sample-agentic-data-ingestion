# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: Bootstrap_Targets is a deduplicated list covering both regions.

For any 12-digit account and any region, `bootstrap_targets` emits a list that
contains us-east-1 exactly once, contains the app region exactly once, and
repeats no environment. The two enumerated R3.3 cases live in
`test_deploy_bootstrap_targets.py` and stay there — this property generalises
them, it does not replace them (design §6 names only the two examples; the
property was signed off as an addition).

Driven through the same real shell function as the enumerated cases, via the
`tests/unit/conftest.py` harness — imported directly rather than as a fixture,
because Hypothesis rejects function-scoped fixtures under `@given`.
Deterministic per example: the function is pure string work over its two
arguments.

**Validates: Requirements 3.3**
"""

from hypothesis import given, settings
from hypothesis import strategies as st
from tests.unit.conftest import call_prereq

FN = "bootstrap_targets"
US_EAST_1 = "us-east-1"

# 12-digit account IDs, the only form AWS issues and the only form deploy.sh
# receives (it reads CDK_DEFAULT_ACCOUNT from `sts get-caller-identity`).
accounts = st.from_regex(r"\A[0-9]{12}\Z")

# Region codes in the shape AWS uses: partition prefix, area, ordinal. Generated
# rather than sampled from a fixed list so the property also holds for regions
# that do not exist yet, and us-east-1 is included explicitly because it is the
# boundary case the deduplication exists for.
regions = st.one_of(
    st.just(US_EAST_1),
    st.sampled_from(["us-west-2", "eu-west-1", "ap-southeast-2", "eu-central-1", "sa-east-1"]),
    st.from_regex(r"\A(us|eu|ap|sa|ca|me|af)-(north|south|east|west|central|northeast|southeast)-[1-4]\Z"),
)


@settings(max_examples=30)
@given(account=accounts, region=regions)
def test_targets_cover_both_regions_without_duplication(account, region):
    """**Validates: Requirements 3.3**

    Three claims at once, because they constrain each other: dropping us-east-1
    breaks the EdgeWaf/Hosting cross-region reference (R3.2), dropping the app
    region breaks the deploy outright, and repeating either one is the reported
    `cdk bootstrap` collision (R3.1).
    """
    result = call_prereq(FN, account, region)

    assert result.returncode == 0, result.stderr
    targets = result.stdout.splitlines()

    assert targets, "bootstrap_targets must always emit at least one environment"
    assert len(targets) == len(set(targets)), f"duplicate environment in {targets}"
    assert targets.count(f"aws://{account}/{US_EAST_1}") == 1, targets
    assert targets.count(f"aws://{account}/{region}") == 1, targets
    # The app region is first, so the environment whose failure surfaces first
    # is the one the user asked for.
    assert targets[0] == f"aws://{account}/{region}", targets
