# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""The deploy.sh post-deploy completeness check, compared against the CDK entrypoint.

`deploy.sh`'s completeness check is the only thing between an interrupted
`cdk deploy --all` and a silently half-deployed system: a missing Orchestration
stack means pipelines never execute, and nothing else says so. Until this file
existed, no test referenced that check under any name — its correctness rested
entirely on someone reading it, which is how a reader recently concluded EdgeWaf
went unverified when it does not.

The pure parts now live in `scripts/deploy_prereq_lib.sh` — the two stack sets,
`stack_name` and `classify_stack_status` — and are exercised as the real shell
functions through the `tests/unit/conftest.py` harness. That shape is
`LGR-b5b76ed-1` (template / ratified): the computations go in a sourceable,
side-effect-free library and the entrypoint stays a thin driver, so the logic is
reachable without a deploy. The `aws cloudformation describe-stacks` calls stay
in `deploy.sh`, because the I/O is not the part that can be wrong quietly.

**Expectations are derived from the producer, never transcribed from it.** The
authority on which stacks a deploy creates, and in which region, is
`deploy-cdk/bin/agentic_data_ingestion.ts`, so this file parses it: the
`new <Logical>Stack(` constructions give the stack set, `env: awsEnv` marks the
app region and `env: { … region: '…' }` marks a pinned one. Copying the library's
own array into the test would produce a test that passes forever — "a test
written from the producer's output cannot detect the producer drifting"
(`LGR-28b4d75-5`). The requirement being asserted is *every stack a deploy
creates is verified exactly once*, which is a claim about two artifacts that
cannot import each other.

This is a name-set comparison, not a behaviour claim over source text, so it does
not run into `LGR-b46a5bb-4` — that record rejects substring-matching source or
aggregate output to prove *behaviour*. Here the TypeScript is read to enumerate
the set of stacks the app declares, and the assertion is that two sets are equal.
The same distinction `tests/unit/test_destroy_stack_set.py` draws for the
teardown side.

`TestFixturesStack` is constructed only inside the `adi:deployTestFixtures`
context gate, so a plain `cdk deploy --all` does not create it. It is therefore
expected by *neither* verification path, and that exclusion is pinned below
rather than left as a comment — otherwise a future reader "completes" the list
with it and a correct deploy starts reporting a missing stack.

**Validates:** every stack `cdk deploy --all` creates is verified exactly once,
in the region it was deployed to, and no healthy-looking pass can be reported
for a stack that is absent or mid-transition.
"""

import re

from tests.unit.conftest import DEPLOY_PREREQ_LIB, REPO_ROOT, call_prereq

CDK_ENTRYPOINT = REPO_ROOT / "deploy-cdk" / "bin" / "agentic_data_ingestion.ts"

# `new FoundationStack(`, `new EdgeWafStack(` — the logical name is the capture.
# `[A-Za-z0-9]*` deliberately admits no `.`, so a hypothetical `new cdk.Stack(`
# is not mistaken for a declared application stack. Same expression as
# `test_destroy_stack_set.py`, which reads the same file for the same reason.
_STACK_CONSTRUCTION = re.compile(r"\bnew ([A-Za-z][A-Za-z0-9]*)Stack\(")

# `env: awsEnv` — the account/region pair resolveEnv() produced, i.e. the app
# region. Word-bounded so `env: awsEnvSomethingElse` would not match.
_APP_REGION_ENV = re.compile(r"env:\s*awsEnv\b")

# `env: { account: awsEnv.account, region: 'us-east-1' }` — a region pinned in
# the stack's own env, which is what takes a stack out of the app region. The
# capture is the region, so a stack pinned somewhere *other* than us-east-1 is
# visible here rather than silently folded in with EdgeWaf.
_PINNED_REGION_ENV = re.compile(r"env:\s*\{[^}]*?region:\s*'([a-z0-9-]+)'")

# The context key that gates TestFixtures. A literal, so renaming the key
# without revisiting this check fails here.
_TEST_FIXTURES_GATE = "adi:deployTestFixtures"

# Marker for "this stack goes wherever the app goes", as distinct from a region
# string. Not a region name, so it can never be confused with one.
_APP_REGION = "<app region>"

# The region the edge-region verification loop in deploy.sh queries. A
# CLOUDFRONT-scope WebACL can only exist here, which is why the second loop
# exists at all.
EDGE_REGION = "us-east-1"

# The prefix `stack_name` composes, from the CDK entrypoint's own template
# literals (`AgenticDataIngestion-Foundation-${env}`).
STACK_NAME_PREFIX = "AgenticDataIngestion"

# The three states that mean "this stack finished, and what it finished with is
# running". UPDATE_ROLLBACK_COMPLETE is healthy because the rollback completed:
# the stack is serving its previous, working template.
HEALTHY_STATUSES = ("CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE")

# The sentinel deploy.sh substitutes when describe-stacks cannot find the stack.
ABSENT_SENTINEL = "MISSING"


def _without_comments(source: str) -> str:
    """Drop `//` line comments and `/* … */` blocks.

    A commented-out construction is not a declared stack. Without this, removing
    a stack by commenting it out would fail this file with the *opposite* advice
    — "verify it" — for a stack that no longer exists.
    """
    source = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", "", source)


def _constructions() -> list[tuple[str, str]]:
    """Every `new <Logical>Stack(` in the entrypoint, with the text that follows it.

    Each construction's "segment" runs to the next construction (or end of file),
    which is where its props object lives — so the segment is what says which
    region the stack was given.
    """
    source = _without_comments(CDK_ENTRYPOINT.read_text())
    matches = list(_STACK_CONSTRUCTION.finditer(source))
    return [
        (match.group(1), source[match.start() : (matches[i + 1].start() if i + 1 < len(matches) else len(source))])
        for i, match in enumerate(matches)
    ]


def _region_of(segment: str) -> str | None:
    """`_APP_REGION`, a pinned region name, or None when neither is expressed.

    Pinned is tested first: it is the narrower form, and a stack that pins a
    region is out of the app region by construction.
    """
    pinned = _PINNED_REGION_ENV.search(segment)
    if pinned:
        return pinned.group(1)
    if _APP_REGION_ENV.search(segment):
        return _APP_REGION
    return None


def _declared_regions() -> dict[str, str | None]:
    return {name: _region_of(segment) for name, segment in _constructions()}


def _gated_stacks() -> set[str]:
    """Stack names constructed behind the `adi:deployTestFixtures` context gate.

    The gate and its construction are adjacent — the whole of it is one
    `if (…) { new TestFixturesStack(…) }` block — so a construction after the last
    mention of the key is inside it.
    """
    source = _without_comments(CDK_ENTRYPOINT.read_text())
    if _TEST_FIXTURES_GATE not in source:
        return set()
    tail = source[source.rindex(_TEST_FIXTURES_GATE) :]
    return {match.group(1) for match in _STACK_CONSTRUCTION.finditer(tail)}


def _lib_lines(fn: str) -> list[str]:
    result = call_prereq(fn)
    assert result.returncode == 0, result.stderr
    return result.stdout.splitlines()


def _classify(status: str) -> str:
    result = call_prereq("classify_stack_status", status)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


# ── The two artifacts are where this file expects them ────────────────
def test_the_cdk_entrypoint_and_library_are_where_they_are_expected_to_be():
    """A moved file must report itself as moved, not as a stack-set mismatch.

    Stated separately because `_constructions()` would raise on a missing
    entrypoint and `call_prereq` would fail on a missing library — both true, and
    both far less legible than this.
    """
    assert CDK_ENTRYPOINT.is_file(), f"CDK entrypoint not found at {CDK_ENTRYPOINT}"
    assert DEPLOY_PREREQ_LIB.is_file(), f"library not found at {DEPLOY_PREREQ_LIB}"


def test_every_declared_stack_states_a_region_this_file_can_read():
    """Guard on the parsing, not on the code under test.

    If a construction expressed its environment in a form neither regex matches,
    that stack would silently drop out of the expected set below and the coverage
    assertion would pass while verifying less. This is what makes a parsing
    failure look like a parsing failure.
    """
    unreadable = sorted(name for name, region in _declared_regions().items() if region is None)

    assert not unreadable, (
        f"cannot tell which region these stacks deploy to: {unreadable}. They express their "
        f"environment in a form neither `env: awsEnv` nor `env: {{ … region: '…' }}` matches, so "
        f"this file can no longer say whether they are verified."
    )


# ── The assertion this file exists for ────────────────────────────────
def test_every_deployed_stack_is_verified_exactly_once():
    """Coverage, both directions: not zero paths, not two.

    A stack in neither set is deployed and never checked — the dropped-stack case
    the whole check exists to catch. A stack in both is queried twice, in two
    different regions, so one of the two queries reports a missing stack on a
    correct deploy and the check fails closed for no reason.
    """
    declared = _declared_regions()
    gated = _gated_stacks()
    expected = set(declared) - gated

    app_region = _lib_lines("app_region_stacks")
    edge_region = _lib_lines("edge_region_stacks")
    verified = set(app_region) | set(edge_region)

    assert expected, "no stack constructions found — the entrypoint or the regex moved"
    assert not (set(app_region) & set(edge_region)), (
        f"verified twice, in two different regions: {sorted(set(app_region) & set(edge_region))}"
    )
    assert verified == expected, (
        f"stack verification drift: deployed but never verified {sorted(expected - verified)}; "
        f"verified but never deployed {sorted(verified - expected)}"
    )
    # A duplicate entry would let a set comparison pass while a name is absent.
    assert len(app_region) + len(edge_region) == len(verified), (
        f"a stack is named twice: app-region {app_region}, edge-region {edge_region}"
    )


def test_the_app_region_array_holds_exactly_the_app_region_stacks():
    """Assignment, not just coverage: the right stacks in the right loop.

    A stack that lands in the app-region loop but was deployed elsewhere is
    queried where it does not exist, which is indistinguishable from a failed
    deploy.
    """
    gated = _gated_stacks()
    declared_app_region = {
        name for name, region in _declared_regions().items() if region == _APP_REGION and name not in gated
    }

    assert set(_lib_lines("app_region_stacks")) == declared_app_region, (
        f"app-region set drift: expected {sorted(declared_app_region)} from "
        f"bin/agentic_data_ingestion.ts, library reports {sorted(_lib_lines('app_region_stacks'))}"
    )


def test_edgewaf_stays_out_of_the_app_region_array():
    """The specific wrong fix, pinned so it cannot be applied quietly.

    Appending EdgeWaf to the app-region array looks like tidying an asymmetry
    away. It would query a us-east-1 stack in the app region, where it does not
    exist — so a *correct* deploy would print `MISSING` and `exit 1`. The do-NOT
    comment in `deploy_prereq_lib.sh` says so; this is what enforces it.
    """
    assert "EdgeWaf" not in _lib_lines("app_region_stacks")
    assert "EdgeWaf" in _lib_lines("edge_region_stacks")


def test_the_edge_region_array_holds_exactly_the_region_pinned_stacks():
    """Every stack that pins its own region is in the edge loop, and pins us-east-1.

    The second assertion matters because the edge loop queries one region. A
    stack pinned to, say, eu-west-1 would be covered by the set comparison and
    still queried in the wrong place, so the region is checked rather than
    assumed.
    """
    gated = _gated_stacks()
    pinned = {
        name: region
        for name, region in _declared_regions().items()
        if region not in (_APP_REGION, None) and name not in gated
    }

    assert set(_lib_lines("edge_region_stacks")) == set(pinned), (
        f"edge-region set drift: entrypoint pins {sorted(pinned)}, library reports "
        f"{sorted(_lib_lines('edge_region_stacks'))}"
    )
    off_edge = {name: region for name, region in pinned.items() if region != EDGE_REGION}
    assert not off_edge, (
        f"{off_edge} pin a region other than {EDGE_REGION}, but deploy.sh's edge loop queries "
        f"{EDGE_REGION} only — these stacks would be checked in the wrong region"
    )


def test_test_fixtures_is_context_gated_and_deliberately_unverified():
    """Pins *which* stack is excluded and *why*, so nobody "completes" the list.

    Whether TestFixtures exists was decided by a context flag handed to a past
    `cdk deploy`; this check cannot know it. Expecting it would fail every plain
    `--all` deploy. Note the asymmetry with the teardown side, which *does* name
    it (`test_destroy_stack_set.py`): a name the teardown lacks is an undeletable
    stack, whereas a name this check adds is a false failure.
    """
    assert _gated_stacks() == {"TestFixtures"}, (
        f"expected TestFixtures alone behind the {_TEST_FIXTURES_GATE} gate, found {_gated_stacks()}"
    )

    verified = set(_lib_lines("app_region_stacks")) | set(_lib_lines("edge_region_stacks"))
    assert "TestFixtures" not in verified, (
        "TestFixtures is context-gated, so a plain `cdk deploy --all` does not create it — "
        "expecting it makes a correct deploy report a missing stack and exit 1"
    )


# ── stack_name ────────────────────────────────────────────────────────
def test_stack_name_composes_the_deployed_cloudformation_name():
    """The physical name is `AgenticDataIngestion-<Logical>-<env>`.

    Checked against every declared logical name rather than one example, because
    the whole point of the function is that it is applied to all of them.
    """
    for logical in _declared_regions():
        result = call_prereq("stack_name", logical, "dev")

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == f"{STACK_NAME_PREFIX}-{logical}-dev"


def test_stack_name_carries_the_environment_through():
    """A named environment reaches the queried stack name.

    `--environment prod` must query the prod stacks; a dropped environment would
    silently verify dev while deploying prod.
    """
    result = call_prereq("stack_name", "Orchestration", "prod")

    assert result.stdout.strip() == f"{STACK_NAME_PREFIX}-Orchestration-prod"


def test_stack_name_refuses_an_empty_argument():
    """Both arguments are required, because the empty forms look plausible.

    `AgenticDataIngestion--dev` is a well-formed name for a stack that does not
    exist, so describe-stacks would report it as missing rather than as a bad
    call — a bug that reads as a failed deploy.
    """
    for args in (("", "dev"), ("Compute", ""), ("", "")):
        result = call_prereq("stack_name", *args)

        assert result.returncode != 0, f"stack_name{args} should fail, got {result.stdout!r}"
        assert result.stdout == ""


# ── classify_stack_status ─────────────────────────────────────────────
def test_the_three_healthy_states_classify_as_ok():
    """CREATE_COMPLETE, UPDATE_COMPLETE, UPDATE_ROLLBACK_COMPLETE and no others."""
    for status in HEALTHY_STATUSES:
        assert _classify(status) == "ok", status


def test_an_absent_stack_classifies_as_missing():
    """The absent sentinel is its own verdict, separate from unhealthy.

    Only because the fixes differ: an absent stack is fixed by re-running
    deploy.sh, an unhealthy one is not. Both fail the check.
    """
    assert _classify(ABSENT_SENTINEL) == "missing"


def test_unhealthy_and_in_flight_states_classify_as_warn():
    """The load-bearing case: not-ok is not the same as absent, and still fails.

    `ROLLBACK_COMPLETE` is the sharpest example — it is a *create* that failed and
    rolled back, so the stack exists as an empty shell with none of its
    resources. An `*_IN_PROGRESS` stack is mid-transition and not yet a working
    deployment. Reading either as healthy reports a half-deployed system as
    complete, which is the exact failure the completeness check exists to
    prevent.
    """
    for status in (
        "ROLLBACK_COMPLETE",
        "ROLLBACK_FAILED",
        "CREATE_FAILED",
        "CREATE_IN_PROGRESS",
        "UPDATE_IN_PROGRESS",
        "UPDATE_ROLLBACK_IN_PROGRESS",
        "UPDATE_ROLLBACK_FAILED",
        "UPDATE_FAILED",
        "DELETE_IN_PROGRESS",
        "DELETE_FAILED",
        "DELETE_COMPLETE",
        "REVIEW_IN_PROGRESS",
        "IMPORT_ROLLBACK_COMPLETE",
    ):
        assert _classify(status) == "warn", f"{status} must not read as healthy"


def test_unrecognised_and_degenerate_output_is_not_ok():
    """Whatever describe-stacks prints that is not a known-good state fails closed.

    `None` is what `--output text` prints for a query that resolved to nothing,
    and the empty string is what an `aws` invocation that exits 0 with no output
    leaves behind. Both used to fall to the same default arm as a real bad state,
    and must keep doing so — a status this function has never heard of must never
    read as healthy.
    """
    for status in ("None", "", "create_complete", "CREATE_COMPLETE ", "OK", "COMPLETE"):
        assert _classify(status) != "ok", f"{status!r} must not read as healthy"


def test_every_non_ok_verdict_fails_the_check():
    """`missing` and `warn` are distinct messages but one outcome.

    Both arms set `MISSING=1` in `deploy.sh` and feed the same `exit 1`, so the
    only verdict that lets a deploy report success is `ok`. Asserted as a
    property of the vocabulary: exactly one token means healthy.
    """
    verdicts = {_classify(status) for status in ("CREATE_COMPLETE", ABSENT_SENTINEL, "ROLLBACK_COMPLETE")}

    assert verdicts == {"ok", "missing", "warn"}
    assert len([v for v in verdicts if v == "ok"]) == 1
