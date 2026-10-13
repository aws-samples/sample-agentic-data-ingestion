# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Tests for the deploy.sh Bootstrap_Targets computation (p31 task 1.4).

The computation lives in `scripts/deploy_prereq_lib.sh` as `bootstrap_targets`
and is exercised as the real shell function through the `tests/unit/conftest.py`
harness — see `context/shell-test-approach.md` (spec task 0.4) for why this
shape rather than `./deploy.sh --dry-run`. `--dry-run` exits ~32 lines before the
bootstrap invocation, so it can observe nothing about the target list, and
asserting on aggregate deploy output is the substring-matching approach this repo
has already rejected once for an invariant test (`LGR-b46a5bb-4`,
template/ratified).

No PATH fixture is needed here: `bootstrap_targets` is pure string work over its
two arguments.

**Validates: Requirements 3.3**
"""

FN = "bootstrap_targets"
ACCOUNT = "123456789012"


def _targets(call_prereq_fn, account: str, region: str) -> list[str]:
    result = call_prereq_fn(FN, account, region)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


# ── R3.3 case 1: app region is us-east-1 → one target ─────────────────
def test_us_east_1_app_region_yields_a_single_target(call_prereq_fn):
    """**Validates: Requirements 3.3**

    The reported defect: with an app region of us-east-1 the old code passed
    that environment twice, and `cdk bootstrap` fails on the resulting
    concurrent change sets against the same bootstrap stack.
    """
    targets = _targets(call_prereq_fn, ACCOUNT, "us-east-1")

    assert targets == [f"aws://{ACCOUNT}/us-east-1"]


# ── R3.3 case 2: a different app region → two targets ─────────────────
def test_non_us_east_1_app_region_yields_app_region_then_us_east_1(call_prereq_fn):
    """**Validates: Requirements 3.3**

    us-east-1 stays in the list whenever the app region differs (R3.2): the
    EdgeWaf stack's CLOUDFRONT-scope WAF can only live there and Hosting
    consumes its ARN cross-region. App region first, preserving the argument
    order deploy.sh has always passed.
    """
    targets = _targets(call_prereq_fn, ACCOUNT, "us-west-2")

    assert targets == [
        f"aws://{ACCOUNT}/us-west-2",
        f"aws://{ACCOUNT}/us-east-1",
    ]


def test_another_non_us_east_1_region_is_not_special_cased(call_prereq_fn):
    """**Validates: Requirements 3.3**

    us-west-2 is the primary dev region, so pin one more region to show the
    two-target shape is general rather than a us-west-2 special case.
    """
    targets = _targets(call_prereq_fn, ACCOUNT, "eu-west-1")

    assert targets == [
        f"aws://{ACCOUNT}/eu-west-1",
        f"aws://{ACCOUNT}/us-east-1",
    ]


def test_account_id_is_carried_through_unchanged(call_prereq_fn):
    """**Validates: Requirements 3.3**

    Every target names the account it was given — a target list built against
    the wrong account bootstraps the wrong environment.
    """
    other_account = "210987654321"

    targets = _targets(call_prereq_fn, other_account, "ap-southeast-2")

    assert targets == [
        f"aws://{other_account}/ap-southeast-2",
        f"aws://{other_account}/us-east-1",
    ]
