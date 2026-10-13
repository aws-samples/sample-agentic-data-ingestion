# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p53 task 4.3 — the credential path did not widen and the IAM surface did not move.

Mechanism (d) puts the sample write **inside the generated Glue job**, which
already holds the source credentials and the VPC reach. Its headline claim is
therefore a *negative* one: no role gained a permission, and no new place exists
for a credential to travel. A negative claim that nobody asserts decays into an
assumption, which is why design §"CDK" asks for this file rather than for a
paragraph.

Two blocks, answering two different questions.

**Block A — no IAM diff (R3.5, R3.6).** Source text over ``foundation-stack.ts``:
``verificationSessionRole`` still carries exactly three policy statements, holds
no Secrets Manager permission at all, and the set of principals granted
``secretsmanager:GetSecretValue`` is unchanged.

**Block B — Property 8 (R3.1-R3.4).** Behavioural, over the two producers a
credential could actually reach: the canonical Glue connection provisioner and
the JDBC **source** job-argument builder. Plus the rendered JDBC source template,
where the Sampling_Writer itself lives.

Why source text for Block A rather than a synthesized template. This layer cannot
synthesize: ``aws-cdk-lib`` is absent from ``pyproject.toml`` and every file in
``tests/cdk/`` asserts on ``.ts`` source as text — ``LGR-1166391-1`` (**template /
ratified**) records that constraint and chose vitest for template-level scanning
because of it. The vitest counterpart is ``deploy-cdk/test/foundation-iam.test.ts``,
which sees the *effective* rendered action set; this file sees the source shape and
the comments, which is where a fence lives. Complements, not duplicates.

**The legacy fixture-secret prefix is deliberately not spelled out here.**
``foundation-stack.ts`` grants ``GetSecretValue`` on two prefixes: the current
``agentic-data-ingestion/*`` and a pre-rename survivor whose literal
``make name-check`` scans for as a legacy name. Two files are allow-listed to pin
that literal character-for-character (``deploy-cdk/test/foundation-iam.test.ts``,
``deploy-cdk/test/p17-jdbc-source.test.ts``); a third allow-list entry is a third
door, and ``LGR-b46a5bb-4`` (**template / ratified**) chose scope over exemptions
for exactly this trade. So the second prefix is identified below by its
``/test-fixtures/`` segment, which is unambiguous, carries no legacy name, and
still fails if the grant's scope changes.

Governing records read before writing this file:

- ``LGR-819980f-1`` (**standing invariant**) — JDBC credentials are only ever
  referenced by a Secrets Manager ARN; plaintext user/password never appear in
  Glue job arguments or ``ConnectionProperties``. This leaked a real target-DB
  credential once. Property 8 is the test of it.
- ``LGR-819980f-2`` (**template / ratified**, supersedes ``LGR-33eed95-1``) —
  ``SECRET_ID`` MUST live in ``ConnectionProperties`` and the ``adi:secret_arn``
  tag is a read-path mirror. **Both copies are load-bearing**; storing the ARN in
  the tag alone broke all JDBC provisioning. Asserting both present is what stops
  the duplication being "cleaned up" (do-NOT 2).
- ``LGR-53765e8-1`` (**template / ratified**) — the two sampled buckets are ONE
  statement listing four resources specifically so this role stays at three
  statements, "this role's documented shape", and notes that a fourth statement
  "would have forced that pin to be loosened to accommodate a cosmetic choice".
  ``LGR-fb2310b-1`` rejected the same add-another-statement move in this file.
- ``LGR-ad78946-4`` and ``LGR-b46a5bb-2`` — enumerate, never wildcard; a widening
  must be a deliberate policy edit.

Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6
"""

from __future__ import annotations

import importlib.util
import os
import re
import sys
from unittest.mock import MagicMock

import jinja2
import pytest
from botocore.exceptions import ClientError
from hypothesis import assume, given, settings
from hypothesis import strategies as st

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
DEPLOY_CDK_LIB = os.path.join(REPO_ROOT, "deploy-cdk", "lib")
FOUNDATION_STACK_PATH = os.path.join(DEPLOY_CDK_LIB, "foundation-stack.ts")
TEMPLATES_DIR = os.path.join(REPO_ROOT, "pyspark_templates")
SHARED_DIR = os.path.join(REPO_ROOT, "shared")
CREATE_GLUE_PIPELINE_DIR = os.path.join(REPO_ROOT, "lambda", "agentic_data_ingestion_create_glue_pipeline")

VERIFICATION_ROLE_PROP = "verificationSessionRole"
GET_SECRET_VALUE = "secretsmanager:GetSecretValue"

# R3.5 — the exact GetSecretValue grant set, as (role property, resource scope).
#
# Written as an exact set rather than a "these are permitted" list. A permissive
# form would go green when a fourth principal appeared, which is the whole thing
# R3.5 is about. Three statements across two principals:
#
#   gluePipelineRole    — provisions connections and secrets; also runs the JDBC
#                         discovery/runtime crawlers, hence the fixture prefix.
#   infrastructureRole  — the dashboard API's provisioning path.
#
# The AgentCore runtime role used to be a third principal, for the job
# builder's job-time credential lookup. T-19 (66e97e5) removed that lookup (the
# job now reads the secret under its own role at run time), and R-02 split the
# runtime role without carrying the grant to any runtime. A runtime role
# reappearing here is a widening.
#
# Scope tokens, not full ARNs: the resources are template literals carrying
# ${region}/${account}, so the discriminating part is the secret-name prefix.
# "/test-fixtures/" stands in for the pre-rename prefix — see the module
# docstring for why the literal is not written here.
EXPECTED_GET_SECRET_VALUE_GRANTS = frozenset(
    {
        ("gluePipelineRole", "agentic-data-ingestion/"),
        ("gluePipelineRole", "/test-fixtures/"),
        ("infrastructureRole", "agentic-data-ingestion/"),
    }
)

# Every credential-shaped token that must not appear in a Sampling_Writer's
# reach. `secret_arn` is deliberately absent: an ARN is a reference, which is
# precisely what LGR-819980f-1 permits.
CREDENTIAL_TOKENS = (
    "db_user",
    "db_password",
    "jdbc_user",
    "jdbc_password",
    "USERNAME",
    "PASSWORD",
    "get_secret_value",
)

# The Sampling_Writer block's own delimiters, as task 2.1 wrote them.
SAMPLE_BLOCK_START = "=== p53 C1: bounded source sample."
SAMPLE_BLOCK_END = "=== End p53 C1 ==="


# ── TypeScript reading ────────────────────────────────────────────────
#
# A purpose-built reader rather than an import from
# ``test_verification_session_role_iam.py``. That module has a fuller parser and
# reusing it would avoid ~70 lines, but there is no test-to-test import anywhere
# in this suite (verified by grep across tests/), and introducing the first one
# couples this file's correctness to another module's collection order. The
# duplication is bounded and the two can drift without either going quietly
# vacuous, because each carries its own positive control below.


def _strip_comments(source: str) -> str:
    """``source`` with ``//`` and ``/* */`` comments blanked, length preserved.

    Runs before any delimiter scanning, and that ordering is load-bearing.
    ``foundation-stack.ts`` is full of possessives inside comments — "the role's
    Glue statement" — and a scanner that treats ``'`` as a string delimiter
    swallows everything to the next apostrophe and reads real grants as absent.
    That is a live bug in a sibling file, not a hypothetical
    (``test_verification_session_role_iam.py`` documents it at length).
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        if ch in "'\"`":
            quote = ch
            out.append(ch)
            i += 1
            while i < n and source[i] != quote:
                if source[i] == "\\" and i + 1 < n:
                    out.append(source[i])
                    out.append(source[i + 1])
                    i += 2
                    continue
                out.append(source[i])
                i += 1
            if i < n:
                out.append(source[i])
                i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "/":
            while i < n and source[i] != "\n":
                out.append(" ")
                i += 1
        elif ch == "/" and i + 1 < n and source[i + 1] == "*":
            while i < n and not (source[i] == "*" and i + 1 < n and source[i + 1] == "/"):
                out.append("\n" if source[i] == "\n" else " ")
                i += 1
            out.append("  ")
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _match_delimiter(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the delimiter matching the one at ``start``.

    Skips quoted strings so a ``${account}`` inside a template literal is not
    counted as a brace. Expects comment-stripped input.
    """
    assert text[start] == opener, f"expected {opener!r} at {start}, found {text[start]!r}"
    depth = 0
    i = start
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            i += 1
            while i < len(text) and text[i] != quote:
                i += 2 if text[i] == "\\" else 1
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    raise AssertionError(f"unbalanced {opener!r} starting at {start}")


def _role_spans(stripped: str) -> list[tuple[str, int, int]]:
    """``(role_prop, start, end)`` for every expression that grants to a role.

    Two shapes carry a grant in this file and both must be read, because the
    ``GetSecretValue`` statements are split across them:

    * ``this.<prop>.addToPolicy( ... )`` — the later, incremental form.
    * ``this.<prop> = lambdaRole('Name', [managed], [ ...statements... ])`` —
      the constructor form, where ``gluePipelineRole``'s two secret statements
      live as inline arguments.

    A statement inside neither is *unattributed*, and
    ``test_every_getsecretvalue_grant_is_attributed_to_a_role`` fails on it
    rather than dropping it. That is what keeps the exact set above complete: a
    grant arriving through a managed policy or an unrecognised call shape cannot
    slip past by being invisible.
    """
    spans: list[tuple[str, int, int]] = []
    for match in re.finditer(r"this\.(\w+)\.addToPolicy\(", stripped):
        open_paren = match.end() - 1
        spans.append((match.group(1), open_paren, _match_delimiter(stripped, open_paren, "(", ")")))
    for match in re.finditer(r"this\.(\w+)\s*=\s*(?:lambdaRole|new\s+iam\.Role)\(", stripped):
        open_paren = match.end() - 1
        spans.append((match.group(1), open_paren, _match_delimiter(stripped, open_paren, "(", ")")))
    return spans


def _statement_spans(stripped: str, start: int, end: int) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``new iam.PolicyStatement({...})`` body in a range."""
    spans = []
    for match in re.finditer(r"new iam\.PolicyStatement\(\{", stripped[start:end]):
        open_brace = start + match.end() - 1
        spans.append((open_brace, _match_delimiter(stripped, open_brace, "{", "}")))
    return spans


def _array_after(body: str, key: str) -> list[str] | None:
    """Elements of the ``key: [...]`` array in ``body``, unsplit but unquoted."""
    match = re.search(rf"\b{key}:\s*\[", body)
    if not match:
        return None
    open_bracket = match.end() - 1
    end = _match_delimiter(body, open_bracket, "[", "]")
    inner = body[open_bracket + 1 : end - 1]
    parts: list[str] = []
    depth = 0
    current = ""
    i = 0
    while i < len(inner):
        ch = inner[i]
        if ch in "'\"`":
            quote = ch
            current += ch
            i += 1
            while i < len(inner) and inner[i] != quote:
                current += inner[i]
                i += 1
            current += inner[i] if i < len(inner) else ""
        elif ch in "([{":
            depth += 1
            current += ch
        elif ch in ")]}":
            depth -= 1
            current += ch
        elif ch == "," and depth == 0:
            parts.append(current.strip())
            current = ""
        else:
            current += ch
        i += 1
    if current.strip():
        parts.append(current.strip())
    return parts


def _unquote(literal: str) -> str:
    return literal.strip().strip("'\"` ")


def _actions(statement_body: str) -> list[str]:
    return [_unquote(a) for a in _array_after(statement_body, "actions") or []]


def _resources(statement_body: str) -> list[str]:
    return [_unquote(r) for r in _array_after(statement_body, "resources") or []]


@pytest.fixture(scope="module")
def raw_foundation() -> str:
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def foundation(raw_foundation: str) -> str:
    return _strip_comments(raw_foundation)


# ── Positive controls ─────────────────────────────────────────────────
# Every assertion below reads through the parser. If the parser finds nothing,
# an empty result reads as "no widening", which is the one failure mode this
# file cannot afford.


def test_the_parser_finds_the_roles_and_the_secret_statements(foundation: str) -> None:
    """The reader located real grants, so the assertions below are not vacuous."""
    spans = _role_spans(foundation)
    props = {prop for prop, _, _ in spans}
    for required in ("gluePipelineRole", "infrastructureRole", "createGluePipelineRuntimeRole", VERIFICATION_ROLE_PROP):
        assert required in props, (
            f"the reader found no grant expression for this.{required} — either the "
            f"role was renamed or _role_spans no longer recognises how it is built. "
            f"Found: {sorted(props)}"
        )

    statements = [foundation[a:b] for prop, start, end in spans for a, b in _statement_spans(foundation, start, end)]
    assert statements, "no iam.PolicyStatement bodies were parsed at all"
    assert any(GET_SECRET_VALUE in _actions(s) for s in statements), (
        f"the reader parsed {len(statements)} policy statements but none grants "
        f"{GET_SECRET_VALUE}. The file grants it in four places, so this is a "
        f"parser failure, not a clean bill of health"
    )


def test_comment_stripping_neutralises_the_apostrophe_hazard(raw_foundation: str, foundation: str) -> None:
    """The hazard is present in the file, and stripping removes it.

    Two directions, because either alone is insufficient: the file must still
    contain a ``//`` comment with an odd number of apostrophes (or this guard
    exercises nothing), and that comment must be gone after stripping (or the
    delimiter scanners desynchronise and read grants as absent).
    """
    hazards = [
        line for line in raw_foundation.splitlines() if "//" in line and line.split("//", 1)[1].count("'") % 2 == 1
    ]
    assert hazards, (
        "no comment in foundation-stack.ts carries an odd number of apostrophes, so "
        "this guard no longer exercises anything. Check before deleting it"
    )
    assert len(raw_foundation) == len(foundation), "_strip_comments is not length-preserving"
    for line in hazards:
        comment = line.split("//", 1)[1]
        assert comment not in foundation, (
            f"comment text survived stripping: {comment.strip()!r} — its apostrophe will be read as a string delimiter"
        )


# ── Block A: no IAM diff ──────────────────────────────────────────────


def test_the_verification_session_role_still_has_exactly_three_statements(foundation: str) -> None:
    """R3.5 — mechanism (d) added no statement to the verification session role.

    Asserted here as p53's own no-diff claim. ``LGR-53765e8-1`` (template /
    ratified) already made this count load-bearing from the other direction: it
    chose ONE statement listing four bucket resources over two statements
    precisely because three is "this role's documented shape", and noted that a
    fourth "would have forced that pin to be loosened to accommodate a cosmetic
    choice". ``LGR-fb2310b-1`` rejected the same move earlier in this file.

    Deliberately duplicates the count in
    ``test_verification_session_role_iam.py``, where it is a *parser guard* for
    that file's allowlist. Here it is the assertion itself. A single count
    serving both purposes reads as one and fails as one; the p53 claim deserves
    a failure message that names p53.
    """
    spans = [s for s in _role_spans(foundation) if s[0] == VERIFICATION_ROLE_PROP]
    statements = [foundation[a:b] for _, start, end in spans for a, b in _statement_spans(foundation, start, end)]
    assert len(statements) == 3, (
        f"expected 3 policy statements on {VERIFICATION_ROLE_PROP}, found {len(statements)}. "
        f"p53 claims mechanism (d) needs no IAM change: the sample write runs inside the "
        f"generated Glue job under the pipeline's own execution role, and the sample is "
        f"read from processedBucket, which this role already reaches. A fourth statement "
        f"means that claim is false — do not loosen this number to accommodate it "
        f"(LGR-53765e8-1, LGR-fb2310b-1)"
    )


def test_the_verification_session_role_holds_no_secrets_manager_permission(foundation: str) -> None:
    """R3.6 / do-NOT 10 — not narrowed to GetSecretValue, but the whole service.

    The role's own description says it never writes "a DynamoDB item or a
    secret". A sampling mechanism that needed this role to read a credential
    would be a different mechanism, and the failure would be silent: an
    AccessDenied inside a Glue statement reads as a failed transform, not as a
    failed harness. Asserting on the ``secretsmanager:`` prefix rather than on
    ``GetSecretValue`` means ``DescribeSecret`` or ``ListSecretVersionIds``
    cannot arrive as "not the one that was forbidden".
    """
    spans = [s for s in _role_spans(foundation) if s[0] == VERIFICATION_ROLE_PROP]
    actions = [
        action
        for _, start, end in spans
        for a, b in _statement_spans(foundation, start, end)
        for action in _actions(foundation[a:b])
    ]
    assert actions, f"no actions parsed for {VERIFICATION_ROLE_PROP} — see the positive control above"

    secrets = sorted(a for a in actions if a.lower().startswith("secretsmanager:"))
    assert not secrets, (
        f"{VERIFICATION_ROLE_PROP} now grants {secrets}. R3.6 and do-NOT 10 forbid ANY "
        f"Secrets Manager permission on this role: it executes AI-generated PySpark that "
        f"no human has approved at the point TestCode runs, so a credential read here is "
        f"unreviewed generated code holding a database credential. Actions: {sorted(set(actions))}"
    )


def _get_secret_value_grants(foundation: str) -> set[tuple[str, str]]:
    """``(role_prop, scope_token)`` for every parsed ``GetSecretValue`` grant."""
    grants: set[tuple[str, str]] = set()
    for prop, start, end in _role_spans(foundation):
        for a, b in _statement_spans(foundation, start, end):
            body = foundation[a:b]
            if GET_SECRET_VALUE not in _actions(body):
                continue
            for resource in _resources(body):
                if "/test-fixtures/" in resource:
                    grants.add((prop, "/test-fixtures/"))
                elif "agentic-data-ingestion/" in resource:
                    grants.add((prop, "agentic-data-ingestion/"))
                else:
                    grants.add((prop, resource))
    return grants


def test_the_getsecretvalue_principal_set_is_unchanged(foundation: str) -> None:
    """R3.5 — the set of principals that can read a JDBC credential did not move.

    An exact set comparison in both directions. A missing grant is a breakage
    (JDBC provisioning stops working); an extra grant is the widening R3.5
    forbids, and is the failure mode p53 could plausibly have caused had the
    sample write been put in a Lambda instead of in the Glue job.

    The scope token is compared, not the full ARN: the resources are template
    literals carrying ``${region}``/``${account}``. Adding a *resource* to an
    existing statement therefore fails this too, which is the point — widening
    ``infrastructureRole`` to a second secret prefix is a real change even
    though the principal count is unmoved.
    """
    actual = _get_secret_value_grants(foundation)

    added = sorted(actual - EXPECTED_GET_SECRET_VALUE_GRANTS)
    removed = sorted(EXPECTED_GET_SECRET_VALUE_GRANTS - actual)

    assert not added, (
        f"new {GET_SECRET_VALUE} grant(s): {added}. R3.5 requires this set to be "
        f"UNCHANGED by p53. Mechanism (d) writes the sample from inside the generated "
        f"Glue job, which already holds the credentials — nothing in this spec needs a "
        f"new principal to read a secret. If a later spec does, change the constant in "
        f"the same commit and say why (LGR-b46a5bb-2: a widening must be a deliberate "
        f"policy edit)"
    )
    assert not removed, (
        f"{GET_SECRET_VALUE} grant(s) disappeared: {removed}. This is not a tightening "
        f"to celebrate — every entry backs a live path (connection provisioning, the "
        f"dashboard API's JDBC wizard), and the failure "
        f"mode is an AccessDenied surfacing as 'API error 500'"
    )


def test_every_getsecretvalue_grant_is_attributed_to_a_role(foundation: str) -> None:
    """Completeness: the exact set above is the whole set.

    ``_role_spans`` reads two call shapes. A ``GetSecretValue`` reaching a
    principal any other way — inside a ``new iam.ManagedPolicy``, via
    ``addManagedPolicy``, via an ``iam.Policy({roles: [...]})`` — would be
    invisible to the set comparison and it would pass while the surface widened.

    So every occurrence of the action literal in the file is required to fall
    inside a parsed statement. Enumerating and failing on the unrecognised one
    is stricter than pattern-matching the known-bad forms: a route nobody
    thought of still fails, and the failure names the line.
    """
    attributed_spans = [
        (a, b) for _, start, end in _role_spans(foundation) for a, b in _statement_spans(foundation, start, end)
    ]
    # The Glue job permissions boundary (threat model T-03) names GetSecretValue
    # but grants nothing: a boundary only caps the roles it is set on. It is
    # exempt here ONLY because the test below proves it is never attached to a
    # principal — attached, it would be exactly the invisible route this test
    # exists to catch.
    exempt_spans = [_boundary_span(foundation)]

    unattributed = []
    for occurrence in re.finditer(re.escape(GET_SECRET_VALUE), foundation):
        offset = occurrence.start()
        if not any(a <= offset < b for a, b in attributed_spans + exempt_spans):
            line = foundation.count("\n", 0, offset) + 1
            unattributed.append(line)

    assert not unattributed, (
        f"{GET_SECRET_VALUE} appears at foundation-stack.ts line(s) {unattributed} outside "
        f"any policy statement this file can attribute to a role. The exact-set assertion "
        f"above cannot see it, so the surface may have widened while reading green. Either "
        f"teach _role_spans the new grant route, or move the grant to a recognised one"
    )


BOUNDARY_PROP = "glueJobRoleBoundary"


def _boundary_span(stripped: str) -> tuple[int, int]:
    """``(start, end)`` of the ``this.glueJobRoleBoundary = new iam.ManagedPolicy(...)`` expression."""
    match = re.search(rf"this\.{BOUNDARY_PROP}\s*=\s*new\s+iam\.ManagedPolicy\(", stripped)
    assert match, f"this.{BOUNDARY_PROP} = new iam.ManagedPolicy(...) not found in foundation-stack.ts"
    open_paren = match.end() - 1
    return open_paren, _match_delimiter(stripped, open_paren, "(", ")")


def test_the_glue_job_boundary_is_only_ever_referenced_by_arn(foundation: str) -> None:
    """The exemption above is sound only while the boundary grants nothing.

    A ManagedPolicy becomes a grant when it is attached: ``managedPolicies: [...]``
    on a role, ``addManagedPolicy(...)``, ``attachToRole(...)``, or its own
    ``roles:`` property. Every reference outside its own definition must
    therefore read ``.managedPolicyArn``, which is how the boundary is used: in
    the ``iam:PermissionsBoundary`` condition and the env var the role builder
    reads. Its statements name ``secret:*`` and ``s3`` on ``*``, so an attached
    boundary would be the widest credential grant in the stack.
    """
    start, end = _boundary_span(foundation)
    definition = foundation[start:end]
    assert not re.search(r"\broles\s*:", definition), "the boundary ManagedPolicy declares roles: it is attached"

    references = [m for m in re.finditer(rf"\b{BOUNDARY_PROP}\b", foundation) if not start <= m.start() < end]
    assert references, f"{BOUNDARY_PROP} is defined but never referenced; the boundary condition is missing"
    for reference in references:
        following = foundation[reference.end() : reference.end() + len(".managedPolicyArn")]
        line = foundation.count("\n", 0, reference.start()) + 1
        is_declaration = foundation[max(0, reference.start() - 20) : reference.start()].rstrip().endswith("readonly")
        is_assignment = foundation[reference.start() - len("this.") : reference.start()] == "this." and re.match(
            r"\s*=", foundation[reference.end() :]
        )
        assert following == ".managedPolicyArn" or is_declaration or is_assignment, (
            f"foundation-stack.ts line {line} uses {BOUNDARY_PROP} other than by ARN. Attaching the "
            f"boundary turns its caps into grants (secret:*, s3 on *)"
        )


def test_no_secretsmanager_secret_is_granted_to_a_principal_in_the_deployed_stacks(foundation: str) -> None:
    """The other invisible route: a ``Secret.grantRead(principal)`` call.

    ``grantRead`` on an ``aws-cdk-lib`` Secret emits ``GetSecretValue`` into the
    grantee's policy without the action literal ever appearing in source, so the
    scans above are blind to it by construction. The deployed stacks create
    exactly one Secret — ``compute-stack.ts``'s ``OriginVerifySecret``, a
    CloudFront origin-verification value read through ``secretValue``, never
    granted to a role — and this asserts it stays that way.

    ``test-fixtures-stack.ts`` is excluded, and that exclusion is the honest
    part: it *does* call ``secret.grantRead(seedingLambda)`` twice. It is
    instantiated only under ``-c adi:deployTestFixtures=true`` (see
    ``bin/agentic_data_ingestion.ts``), so it is not in the deployed set a plain
    ``cdk deploy --all`` produces and not part of R3.5's principal set. Naming
    the exclusion here is what stops a later reader "fixing" the apparent gap.
    """
    del foundation  # scope is every deployed stack, not just Foundation

    offenders: list[str] = []
    for name in sorted(os.listdir(DEPLOY_CDK_LIB)):
        if not name.endswith(".ts") or name == "test-fixtures-stack.ts":
            continue
        with open(os.path.join(DEPLOY_CDK_LIB, name)) as f:
            stripped = _strip_comments(f.read())

        secret_identifiers = {
            m.group(1) for m in re.finditer(r"(this\.\w+|const\s+(\w+))\s*=\s*new\s+secretsmanager\.Secret\(", stripped)
        }
        for identifier in secret_identifiers:
            for grant in re.finditer(rf"{re.escape(identifier)}\.grant\w*\(", stripped):
                line = stripped.count("\n", 0, grant.start()) + 1
                offenders.append(f"{name}:{line} {grant.group(0)}")

    assert not offenders, (
        "a Secrets Manager Secret is granted to a principal in a deployed stack: "
        f"{offenders}. A grantRead emits secretsmanager:GetSecretValue into the "
        "grantee's policy with no action literal in source, so the exact-set assertion "
        "above cannot see it and R3.5 would be unpinned while reading green"
    )


# ── Block B: Property 8 ───────────────────────────────────────────────
#
# Property 8: No credential text reaches an argument or a property.
#
# For any rendered JDBC job and any provisioned Glue connection, the plaintext
# username and password appear in neither the job arguments nor
# ConnectionProperties, while SECRET_ID and the adi:secret_arn tag both remain
# present.
#
# Split across the three surfaces a credential could travel through, because
# each has a different quantifier and a different failure mode.


def _load_module(directory: str, mod_name: str, alias: str):
    """Load a Lambda-sibling or shared module standalone.

    Same idiom as ``tests/unit/lambdas/create_glue_pipeline/test_jdbc_source_job_wiring.py``.
    ``job_arguments`` imports ``boto3`` at module scope but creates every client
    lazily inside a function, and ``build_jdbc_source_arguments`` makes no AWS
    call at all, so no credentials or network are touched by importing it.
    """
    path = os.path.join(directory, f"{mod_name}.py")
    spec = importlib.util.spec_from_file_location(alias, path, submodule_search_locations=[directory])
    assert spec and spec.loader, f"could not build an import spec for {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def jdbc_helpers():
    if SHARED_DIR not in sys.path:
        sys.path.insert(0, SHARED_DIR)
    return _load_module(SHARED_DIR, "jdbc_helpers", "p53_jdbc_helpers")


@pytest.fixture(scope="module")
def job_arguments():
    if SHARED_DIR not in sys.path:
        sys.path.insert(0, SHARED_DIR)
    return _load_module(CREATE_GLUE_PIPELINE_DIR, "job_arguments", "p53_job_arguments")


def _entity_not_found() -> ClientError:
    return ClientError({"Error": {"Code": "EntityNotFoundException", "Message": "absent"}}, "GetConnection")


# Credential-shaped strings, distinctive enough that a leak is unambiguous and
# a coincidental substring match is not. Constrained to identifier-safe
# characters so the generated value cannot accidentally *be* a JSON fragment or
# an ARN separator, which would make a leak assertion ambiguous rather than
# strict.
_CREDENTIAL_TEXT = st.text(
    alphabet=st.characters(min_codepoint=48, max_codepoint=122, categories=("Lu", "Ll", "Nd")),
    min_size=8,
    max_size=24,
)


@settings(max_examples=50, deadline=None)
@given(
    secret_name=_CREDENTIAL_TEXT,
    tag_value=_CREDENTIAL_TEXT,
    has_secret=st.booleans(),
    connection_exists=st.booleans(),
    with_physical=st.booleans(),
)
def test_the_connection_provisioner_carries_a_reference_and_never_a_credential(
    jdbc_helpers,
    secret_name: str,
    tag_value: str,
    has_secret: bool,
    connection_exists: bool,
    with_physical: bool,
) -> None:
    """Property 8, arm 1 — R3.1, R3.2, R3.3, R3.4 over the canonical provisioner.

    ``create_glue_jdbc_connection`` is the single source of truth for Glue JDBC
    connections (``jdbc_helpers.py:379-384``), and the property is a shape
    property of its output: whatever the caller supplies, ``ConnectionProperties``
    holds exactly ``JDBC_CONNECTION_URL``, ``JDBC_ENFORCE_SSL`` and — when a
    secret ARN is given — ``SECRET_ID``. No ``USERNAME``, no ``PASSWORD``, on any
    input, on either the create or the update branch.

    That the function takes no username or password parameter is the structural
    reason this holds, and the reason the property is worth pinning rather than
    reasoning about: an edit adding a plaintext key would be one line and would
    violate ``LGR-819980f-1`` immediately.

    ``SECRET_ID`` in properties **and** ``adi:secret_arn`` in tags are both
    required. ``LGR-819980f-2`` records that removing either copy broke all JDBC
    provisioning — wizard discovery plus source and target creation — so this is
    the assertion that stops the duplication being tidied away (do-NOT 2).
    """
    client = MagicMock()
    if connection_exists:
        client.get_connection.return_value = {"Connection": {"Name": "conn"}}
    else:
        client.get_connection.side_effect = _entity_not_found()

    jdbc_helpers._glue_client_cache = None
    original = jdbc_helpers._glue_client
    jdbc_helpers._glue_client = lambda: client
    # The tagging path builds a connection ARN, which needs an account id. Stubbed
    # rather than resolved, so this test needs no AWS credentials: it asserts on
    # what reaches ConnectionProperties, and the account id is not part of that.
    # Before the seam existed this reached STS for real and the test failed with
    # NoCredentialsError on any machine without ambient credentials.
    original_account_id = jdbc_helpers._account_id
    jdbc_helpers._account_id_cache = None
    jdbc_helpers._account_id = lambda: "123456789012"
    try:
        secret_arn = (
            f"arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/{secret_name}"
            if has_secret
            else None
        )
        physical = {"SubnetId": "subnet-abc", "SecurityGroupIdList": ["sg-abc"]} if with_physical else None
        jdbc_helpers.create_glue_jdbc_connection(
            connection_name="agentic-data-ingestion-p53-source",
            jdbc_url="jdbc:postgresql://db.example.internal:5432/app",
            jdbc_driver="org.postgresql.Driver",
            secret_arn=secret_arn,
            physical_requirements=physical,
            tags={"adi:pipeline_id": tag_value},
        )
    finally:
        jdbc_helpers._glue_client = original
        jdbc_helpers._glue_client_cache = None
        jdbc_helpers._account_id = original_account_id
        jdbc_helpers._account_id_cache = None

    call = client.update_connection if connection_exists else client.create_connection
    assert call.called, f"neither branch called Glue (exists={connection_exists})"
    connection_input = call.call_args.kwargs["ConnectionInput"]
    properties = connection_input["ConnectionProperties"]

    # R3.2 — no plaintext credential property, whatever the caller passed.
    for forbidden in ("USERNAME", "PASSWORD"):
        assert forbidden not in properties, (
            f"{forbidden} is set in ConnectionProperties. LGR-819980f-1 (standing "
            f"invariant) forbids a plaintext credential there — it is visible in the "
            f"Glue console, CloudTrail and job-run history, and this leaked a real "
            f"target-DB credential once. Reference the secret by ARN instead. "
            f"Properties: {sorted(properties)}"
        )

    expected_keys = {"JDBC_CONNECTION_URL", "JDBC_ENFORCE_SSL"} | ({"SECRET_ID"} if has_secret else set())
    assert set(properties) == expected_keys, (
        f"ConnectionProperties key set is {sorted(properties)}, expected "
        f"{sorted(expected_keys)}. An unexpected key is where a credential arrives; "
        f"Glue also rejects custom keys (LGR-819980f-1)"
    )

    if has_secret:
        # R3.3 — SECRET_ID in properties. Glue consults this at job runtime;
        # tags alone are never consulted (LGR-819980f-2).
        assert properties["SECRET_ID"] == secret_arn, (
            f"SECRET_ID is {properties.get('SECRET_ID')!r}, expected the ARN passed in. "
            f"Glue reads credentials from SECRET_ID at run time, and a connection "
            f"carrying only JDBC_CONNECTION_URL is rejected outright"
        )
        # R3.4 — the tag mirror, on whichever call carries tags for this branch.
        tags = client.tag_resource.call_args.kwargs["TagsToAdd"] if connection_exists else call.call_args.kwargs["Tags"]
        assert tags.get("adi:secret_arn") == secret_arn, (
            f"the adi:secret_arn tag mirror is {tags.get('adi:secret_arn')!r}, expected "
            f"the ARN. LGR-819980f-2: the duplication is deliberate and load-bearing — "
            f"removing either copy broke ALL JDBC provisioning (do-NOT 2)"
        )


@settings(max_examples=50, deadline=None)
@given(
    username=_CREDENTIAL_TEXT,
    password=_CREDENTIAL_TEXT,
    read_mode=st.sampled_from(["full_table", "incremental"]),
)
def test_no_credential_text_reaches_a_jdbc_source_job_argument(
    job_arguments,
    username: str,
    password: str,
    read_mode: str,
) -> None:
    """Property 8, arm 2 — R3.1, R3.2 over the argument surface the sample runs under.

    This is the arm that matters most for p53. The Sampling_Writer executes
    inside the generated JDBC **source** job, so the source job's arguments are
    the arguments a sampling change could plausibly widen —
    ``build_jdbc_source_arguments`` is what builds them.

    Quantified over a ``source_config`` that **does** carry plaintext
    credentials under every key name a caller might plausibly use. That is the
    load-bearing part of the strategy: asserting on a config with no credentials
    in it would pass trivially and prove nothing. Here the credentials are
    present and available, and the property is that the builder still emits only
    ``--secret_arn`` — a reference.

    What it would catch: a future edit that "helpfully" backfills
    ``--jdbc_user``/``--jdbc_password`` onto the source side the way
    ``build_jdbc_arguments`` did for the target until T-19 (``66e97e5``) deleted
    that branch. That edit is one plausible reading of "make the source job
    self-sufficient", and it would violate ``LGR-819980f-1``.
    """
    # Split into the legitimate half and the credential half, so the credential
    # half can be removed to derive what the builder emits with nothing to leak.
    legitimate_config: dict[str, str] = {
        "jdbc_url": "jdbc:postgresql://db.example.internal:5432/app",
        "jdbc_driver": "org.postgresql.Driver",
        "jdbc_schema": "public",
        "jdbc_table": "customers",
        "secret_arn": "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/p53",
        "read_mode": read_mode,
    }
    if read_mode == "incremental":
        legitimate_config["watermark_column"] = "updated_at"
        legitimate_config["watermark_type"] = "timestamp"

    # Credentials the builder must ignore rather than forward.
    credential_config: dict[str, str] = {
        "username": username,
        "password": password,
        "jdbc_user": username,
        "jdbc_password": password,
        "USERNAME": username,
        "PASSWORD": password,
    }
    source_config = {**legitimate_config, **credential_config}

    # Oracle scoping, added 2026-08-30. This narrows the *generator*, not the
    # property — the assertions below are unchanged.
    #
    # The value assertion compares each built argument against the generated
    # credentials by equality, so on its own it cannot tell a leaked credential
    # from a legitimate argument that happens to hold the same string.
    # ``_CREDENTIAL_TEXT`` draws unconstrained alphanumerics of length 8-24, which
    # includes "incremental" — a value ``read_mode`` legitimately takes — and the
    # collision password="incremental" / read_mode="incremental" made the oracle
    # report the entirely correct ``--read_mode`` argument as a plaintext
    # credential. A **false positive in the oracle**, not a leak. "customers" and
    # "timestamp" are the same hazard waiting to be drawn.
    #
    # The excluded set is derived, never transcribed: building ``legitimate_config``
    # — the same config with the credential keys absent — yields exactly the values
    # this builder emits when there is no credential available to forward, so every
    # value in it is legitimate by construction. A new ``read_mode``, a new emitted
    # argument or a changed fixture value updates the exclusion automatically. A
    # hand-written vocabulary would go stale silently, which is the failure mode
    # this deliberately avoids.
    #
    # ``LGR-819980f-1``'s coverage is unchanged, and that is checkable rather than
    # asserted: a leak forwards the credential verbatim, and the credential-free
    # build cannot emit that value because the credential is not in its input — so
    # a leaked argument is still flagged, under any key, including an innocuous one
    # (verified by injecting exactly that leak). The excluded strings are precisely
    # the ones on which this oracle has no discriminating power: it reports a
    # violation for them whether or not a leak occurred, so an example drawn from
    # that set can neither pass nor fail informatively. Dropping them costs no
    # coverage. The key-name assertion is untouched and still catches any
    # user/password-shaped key whatever its value.
    legitimate_values = set(job_arguments.build_jdbc_source_arguments(legitimate_config).values())
    assume(username not in legitimate_values)
    assume(password not in legitimate_values)

    built = job_arguments.build_jdbc_source_arguments(source_config)

    # R3.1 — the credential travels as an ARN reference.
    assert built.get("--secret_arn") == source_config["secret_arn"], (
        f"--secret_arn is {built.get('--secret_arn')!r}. The generated job resolves "
        f"credentials from Secrets Manager at run time, so the ARN is the only "
        f"credential-bearing argument it may receive (LGR-819980f-1)"
    )

    # R3.2 — nothing else credential-shaped, by key or by value.
    leaked_keys = sorted(k for k in built if re.search(r"user|password|passwd|pwd", k, re.IGNORECASE))
    assert not leaked_keys, (
        f"the JDBC source job arguments declare credential key(s) {leaked_keys}. Glue "
        f"job arguments are visible in the console, CloudTrail and job-run history — "
        f"LGR-819980f-1 forbids plaintext there, and this leaked a real credential once. "
        f"The source path must pass --secret_arn only"
    )
    leaked_values = sorted(k for k, v in built.items() if v in (username, password))
    assert not leaked_values, (
        f"argument(s) {leaked_values} carry a plaintext credential value even though the "
        f"key name looks innocuous. Arguments built: {sorted(built)}"
    )


@pytest.fixture(scope="module")
def jinja_env() -> jinja2.Environment:
    """The production Jinja configuration — ``StrictUndefined``, no autoescape.

    Matches ``create_code/code_assembly.py``'s environment so a render here is
    the render that ships.
    """
    return jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )


def _render_jdbc_source(
    env: jinja2.Environment,
    *,
    target_type: str,
    read_mode: str,
    write_mode: str,
    target_secret_arn: str | None,
) -> str:
    return env.get_template("glue_job_jdbc_source.py.j2").render(
        engine="postgres",
        target_type=target_type,
        read_mode=read_mode,
        write_mode=write_mode,
        partition_columns="",
        merge_key="id",
        initial_watermark="",
        target_secret_arn=target_secret_arn,
        transform_block="df = df",
    )


def _sample_block(rendered: str) -> str:
    """The Sampling_Writer's own lines, delimited as task 2.1 wrote them."""
    start = rendered.find(SAMPLE_BLOCK_START)
    assert start != -1, (
        f"the rendered JDBC source job contains no {SAMPLE_BLOCK_START!r} marker, so the "
        f"Sampling_Writer cannot be located. Either task 2.1's block was removed or its "
        f"delimiter changed — do not relax this into a file-wide scan, which would go "
        f"green with the block absent"
    )
    end = rendered.find(SAMPLE_BLOCK_END, start)
    assert end != -1, f"the sample block opens but never closes with {SAMPLE_BLOCK_END!r}"
    return rendered[start:end]


@settings(max_examples=40, deadline=None)
@given(
    target_type=st.sampled_from(["S3_TABLES", "GLUE_CATALOG", "JDBC", "RDS_DATABASE", "S3_PARQUET"]),
    read_mode=st.sampled_from(["full_table", "incremental"]),
    write_mode=st.sampled_from(["overwrite", "append", "upsert"]),
    target_secret_arn=st.sampled_from(
        [None, "arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/target"]
    ),
)
def test_the_sampling_writer_touches_no_credential_in_any_render(
    jinja_env: jinja2.Environment,
    target_type: str,
    read_mode: str,
    write_mode: str,
    target_secret_arn: str | None,
) -> None:
    """Property 8, arm 3 — R3.1 over the Sampling_Writer as it actually renders.

    Quantified over the render axes rather than asserted on the template source,
    because the block sits inside a file full of ``{% if target_type == ... %}``
    branches and "the source text is fine" is not the same claim as "every
    render is fine".

    The property: the Sampling_Writer's own lines reference **no** credential
    symbol. Not ``db_user`` or ``db_password`` — which the template genuinely
    holds, resolved from the secret at run time some 200 lines above, and which
    are in scope at the sample block. Not ``get_secret_value``. The only
    credential-adjacent thing the block may touch is ``args['secret_arn']``, and
    it touches it for its account and region segments, not for the secret.

    An ARN is a reference, which is exactly what ``LGR-819980f-1`` permits, so
    that use is compliant rather than tolerated. The failure this catches is
    concrete: a sample enriched with connection context, or a diagnostic log line
    that prints the resolved user "to make the sample traceable".
    """
    rendered = _render_jdbc_source(
        jinja_env,
        target_type=target_type,
        read_mode=read_mode,
        write_mode=write_mode,
        target_secret_arn=target_secret_arn,
    )
    block = _sample_block(rendered)

    found = sorted(token for token in CREDENTIAL_TOKENS if token in block)
    assert not found, (
        f"the Sampling_Writer block references {found} in the "
        f"{target_type}/{read_mode}/{write_mode} render. R3.1 allows the sample write to "
        f"reference credentials only by Secrets Manager ARN. db_user and db_password ARE "
        f"in scope at this point in the script, which is why this is asserted rather than "
        f"assumed — the sample is telemetry and must not carry a credential into an S3 "
        f"object that four consumers read"
    )
    assert "args['secret_arn']" in block, (
        "the sample block no longer reads args['secret_arn']. That is where it derives "
        "the account and region for the destination bucket, with no API call and so no "
        "extra VPC-endpoint dependency. If the derivation moved, re-check that its "
        "replacement is also a reference and not a credential"
    )


@pytest.mark.parametrize("read_mode", ["full_table", "incremental"])
def test_the_source_side_declares_no_credential_job_argument(jinja_env: jinja2.Environment, read_mode: str) -> None:
    """R3.2 on the rendered script's own ``getResolvedOptions`` list.

    Complements arm 2: that one asserts the builder emits no credential
    argument, this one asserts the script never *declares* one on the source
    side. Both are needed because ``getResolvedOptions`` requires every declared
    key to be present in ``sys.argv`` — a declaration and an emission are two
    halves of the same contract, and a mismatch in either direction is a real
    failure.

    Rendered in target-secret mode, which since T-19 (``66e97e5``) is the only
    mode a JDBC pipeline runs in: the source connection must have a
    ``SECRET_ID`` (``pipelines_jdbc.py`` fails fast without one on the source
    side), and the target is secret-backed. No ``jdbc_user``/``jdbc_password``
    argument is declared anywhere in the script.

    **Scoped honestly.** Until T-19 the template also declared
    ``target_jdbc_user`` and ``target_jdbc_password`` when the target connection
    had no ``SECRET_ID`` — an inline-credential path on the target side that
    predated p53. T-19 deleted that arm (``glue_job_jdbc_source.py.j2`` now
    always declares ``target_secret_arn``), so the finding this paragraph used to
    report is closed.
    """
    rendered = _render_jdbc_source(
        jinja_env,
        target_type="JDBC",
        read_mode=read_mode,
        write_mode="append",
        target_secret_arn="arn:aws:secretsmanager:us-west-2:123456789012:secret:agentic-data-ingestion/target",
    )

    declared = re.search(r"getResolvedOptions\(\s*sys\.argv\s*,\s*\[(.*?)\]", rendered, re.DOTALL)
    assert declared, "the rendered script has no getResolvedOptions([...]) declaration list"
    names = {_unquote(n) for n in declared.group(1).split(",") if n.strip()}

    assert "secret_arn" in names, (
        f"the rendered JDBC source job does not declare --secret_arn. Without it the "
        f"job cannot resolve credentials at run time and the only alternative is "
        f"plaintext arguments. Declared: {sorted(names)}"
    )
    leaked = sorted(n for n in names if re.search(r"user|password|passwd|pwd", n, re.IGNORECASE))
    assert not leaked, (
        f"the rendered JDBC source job declares credential argument(s) {leaked} in "
        f"secret-backed mode. Every declared key must be supplied in sys.argv, so a "
        f"declaration here means plaintext credentials in the job's DefaultArguments — "
        f"visible in the Glue console, CloudTrail and job-run history (LGR-819980f-1)"
    )
