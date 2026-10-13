# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK assertions for the real-Spark verification IAM surface (P42 task 2.3).

Pins six requirements about two roles in ``foundation-stack.ts``:

| Req | Claim |
|---|---|
| 6.1 | ``AgenticDataIngestion-VerificationSession-Role`` exists and trusts ``glue.amazonaws.com`` |
| 6.2 | It grants source-bucket reads, Glue catalog reads and ``/aws-glue/*`` log writes |
| 6.3 | It grants **no action outside a read/log allowlist** |
| 6.5 | The AgentCore runtime role's Glue statement carries ``session/*`` |
| 6.6 | That statement still carries **both** the snake and kebab naming forms |
| 6.7 | The ``iam:PassRole`` grant on the new role carries ``iam:PassedToService: glue.amazonaws.com`` |

**AMENDED (p44 IAM fix): R6.2's source read spans TWO buckets, not one.**
As first written, this file asserted the S3 grant reached ``processedBucket``
*and nothing else*, on the stack comment's claim that the processed bucket is
"the only bucket a verification session has any reason to open". That claim was
false for the S3-upload path. ``file_validation`` writes ``original_s3_uri``
from the upload event's own bucket (``file_validation/lambda_function.py:632``),
which is ``sourceBucket``; ``schema_extract`` carries it through (``:904`` →
``:960`` → the ``json_s3_uri`` it returns at ``:1021``) and Step Functions maps
that onto the TestCode payload as ``source_data_uri``
(``orchestration-stack.ts:594``, via ``source_sample_uri``, same value here). So the URI a sample is read from names
``sourceBucket`` on every S3-upload run, and the processed-only grant denied it.

The narrowing assertion below has therefore been **widened by exactly one
bucket, and not by more**: both buckets are now required in both ARN forms, the
``adi-*`` wildcard is still forbidden, and ``ALLOWED_ACTIONS`` is untouched — no
action was added to fix this. Requiring both, rather than merely permitting the
second, is the point: a permissive assertion would have gone green on the
original defect and would go green again if someone deleted the source-bucket
resources.

**Why an assertion rather than trust.** ``LGR-fb2310b-1`` and ``LGR-b2f79ef-7``
(both template / ratified) each pin an IAM shape with a test instead of relying
on it being correct today — ``b2f79ef-7`` explicitly because a later tightening
of a wildcard would break a read "with an ``AccessDenied`` that no test layer
below CDK can observe". Every claim here is in that position: the verification
session's authority is only observable at session-creation time in a real
account, which is the slowest feedback loop this spec has (tasks.md, "IAM before
runner").

**R6.3 is written as an allowlist, deliberately.** A denylist of known-bad
actions passes the moment someone adds an action nobody thought to forbid. The
allowlist inverts that: widening the role fails this file, and widening the
allowlist to match fails ``test_the_allowlist_itself_stays_read_shaped`` below.
Design §4.6's Decision is the thing being defended — the code a verification
session executes is AI-generated and **unreviewed by a human at that point**
(the code-approval gate is downstream of TestCode), so a write action here means
unreviewed generated PySpark can alter the data it is meant to be verifying
against.

**R6.7 must target the CONDITIONED statement specifically.** An unconditioned
``iam:PassRole`` on ``arn:aws:iam::<account>:role/AgenticDataIngestion-*``
already exists (``foundation-stack.ts:1042-1045``, alongside
``iam:PutRolePolicy`` and ``iam:AttachRolePolicy``) and matches the new role by
name. So "some statement grants ``iam:PassRole`` and can reach this role" is
already true and pins nothing. The assertion below selects by resource — the
statement must name ``this.verificationSessionRole.roleArn`` — and
``test_the_broad_unconditioned_passrole_would_not_satisfy_r6_7`` proves the
discriminator is live by asserting the broad statement is still present and
still unconditioned.

**On parsing TypeScript, and one inherited bug this file does not inherit.**
``test_code_roles_target_schema_iam.py``'s ``_match_delimiter`` (``:100-124``) is
comment-blind: it treats ``'`` as a string delimiter, so a lone apostrophe in a
comment — ``// the role's Glue statement``, of which ``foundation-stack.ts`` has
many — desynchronises its scanner and makes real grants read as absent. The
mechanism is verified here by reading that function; the reported consequence
(three failures presenting as unresolved IAM resources) comes from p42's task
2.2 write-up rather than from an observation made in this file.
``_strip_comments`` below removes comments before any scanning, so quotes are
only ever real string delimiters. It is length-preserving, so offsets in an
assertion message still line up with the file — and
``test_comment_stripping_removes_the_apostrophes_that_desync_the_scanner`` fails
if the hazard ever stops being present, rather than quietly guarding nothing.

Two further parser choices, both to keep a red result legible:

- **No contiguous-section slicing.** The sibling files slice from
  ``this.<role> = lambdaRole(`` to the next role assignment; that pattern cannot
  reach this role (it is a bare ``new iam.Role``) and a slice that ends in the
  wrong place reads as "grant absent". Every grant here is collected by exact
  property name instead, so there is no bleed and no under-reach.
- **A completeness guard on the grant routes.** Name-scoped collection is only
  the whole role if ``addToPolicy`` is the only way a grant arrives.
  ``test_addtopolicy_is_the_only_grant_route_to_the_role`` enumerates every
  mention of the property and fails on an unrecognised one — an
  ``addManagedPolicy``, an ``attachInlinePolicy`` or a
  ``bucket.grantReadWrite(role)`` would otherwise slip past the allowlist
  unseen.

The synthesized-template counterpart lives in
``deploy-cdk/test/foundation-iam.test.ts`` (P42 R6 block). It asserts the
**effective** action set on the role as CloudFormation renders it, which catches
a grant arriving from any source rather than from source text this file knows how
to read. The two layers are complements: this one can see the naming-fence
comment and the deliberate source shape of the conditioned statement, that one
can see what actually deploys.

Validates: Requirements 6.1, 6.2, 6.3, 6.5, 6.6, 6.7
"""

import os
import re

import pytest

FOUNDATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "foundation-stack.ts"
)

VERIFICATION_ROLE_PROP = "verificationSessionRole"
# Since R-02 each AgentCore runtime has its own role. The verifier runs in the
# test_code runtime, so that role is the caller-side subject of R6.5 and R6.7.
# The project-wide glue:* statement whose naming fence R6.6 guards moved to the
# job-builder runtime, the only runtime role that still holds glue:*.
AGENTCORE_ROLE_PROP = "testCodeRuntimeRole"
JOB_BUILDER_ROLE_PROP = "createGluePipelineRuntimeRole"
CODE_TESTING_ROLE_PROP = "codeTestingRole"

ROLE_NAME = "AgenticDataIngestion-VerificationSession-Role"
GLUE_SERVICE_PRINCIPAL = "glue.amazonaws.com"

# R6.3 — the complete set of actions the verification session role may hold.
# Read the "Decision" in design §4.6 before adding anything to this set; the
# meta-assertion below will reject a write action even if the role carries it.
ALLOWED_ACTIONS = frozenset(
    {
        # Source sample reads, on the source-files AND processed-files buckets.
        # ListBucket authorizes on the bucket ARN, GetObject on the object ARN,
        # hence both forms of each. Unchanged by the p44 two-bucket fix: that
        # widened the RESOURCE list only, and this allowlist is about actions.
        "s3:GetObject",
        "s3:ListBucket",
        # Glue Data Catalog reads, so a transform resolving a catalog table runs.
        "glue:GetDatabase",
        "glue:GetTable",
        "glue:GetPartitions",
        # Session log writes. Glue's /aws-glue/* groups are service-owned and
        # carry a runtime session id, so they cannot be enumerated at deploy time.
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    }
)

# R6.2's three grant groups, each keyed by an action that identifies its statement.
SOURCE_READ_ACTIONS = ("s3:GetObject", "s3:ListBucket")
CATALOG_READ_ACTIONS = ("glue:GetDatabase", "glue:GetTable", "glue:GetPartitions")
LOG_WRITE_ACTIONS = ("logs:CreateLogStream", "logs:PutLogEvents")

# R6.6 — the naming fence. Glue forbids hyphens in database/table names, so those
# stay snake_case; jobs, crawlers and connections are created kebab-case at
# runtime. Both forms exist because a real glue:GetJob AccessDenied was observed
# in fresh-account validation (2026-07-17) and fixed by adding the kebab form.
NAMING_FENCE_PAIRS = (
    ("job/agentic_data_ingestion", "job/agentic-data-ingestion-"),
    ("crawler/agentic_data_ingestion", "crawler/agentic-data-ingestion-"),
    ("connection/agentic_data_ingestion", "connection/agentic-data-ingestion-"),
)
NAMING_FENCE_COMMENT = "NAMING FENCE"

# R6.5 — the interactive-session resource. Session ids are minted per
# verification run, so the ARN cannot be enumerated at deploy time.
SESSION_RESOURCE_SUFFIX = ":session/*"

# R6.7
PASS_ROLE = "iam:PassRole"
PASSED_TO_SERVICE = "iam:PassedToService"
# The pre-existing unconditioned grant that must NOT be what satisfies R6.7.
BROAD_PASSROLE_RESOURCE = "role/AgenticDataIngestion-*"

# ── The Lambda-side break-glass grant (post-spec widening) ────────────
#
# R6.5 and R6.7 name the AgentCore runtime role specifically, and that is the
# intended route: the live path runs in the container
# (USE_AGENTCORE_RUNTIME: 'true', compute-stack.ts:432). The grants asserted
# below are a DELIBERATE WIDENING past those ACs, not an implementation of
# them, so they are asserted under their own heading rather than folded into
# the R6.5/R6.7 blocks.
#
# What went wrong without them: VERIFICATION_SESSION_ROLE_ARN is published to
# both environments, so with USE_AGENTCORE_RUNTIME off and
# VERIFICATION_EXECUTOR=glue, resolve_effective_executor selects `glue` on a
# populated ARN and the run then dies at CreateSession on AccessDenied. A
# configuration that reads as correct and fails at the last moment is worse
# than one that refuses up front, which is why the grant exists rather than
# the env var being removed.
#
# Exactly the six APIs glue_session_runner.py calls, verified by reading it:
# create_session, get_session (readiness poll), run_statement, get_statement
# (completion poll), stop_session and delete_session — plus glue:TagResource,
# which Glue requires for the Tags that create_session passes. No
# CancelStatement and no ListSessions — the runner
# calls neither, and LGR-b46a5bb-2's posture is that a later widening must be
# a deliberate edit. Deliberately NOT the shape of the AgentCore grant, whose
# action is `glue:*`: copying that would hand a Lambda role glue:* across
# catalogs, databases, tables, jobs, crawlers and connections to buy six
# session calls.
SESSION_LIFECYCLE_ACTIONS = frozenset(
    {
        "glue:CreateSession",
        # create_session passes Tags=_SESSION_TAGS, which Glue authorizes as
        # glue:TagResource on the new session. Missing it failed every
        # verification on a deployment on 2026-10-08 after the R-02 split.
        "glue:TagResource",
        "glue:GetSession",
        "glue:RunStatement",
        "glue:GetStatement",
        "glue:StopSession",
        "glue:DeleteSession",
    }
)
# Actions a reader might reasonably add "for completeness" that the runner does
# not call. Asserted absent so the enumeration stays derived from the runner.
SESSION_ACTIONS_NOT_CALLED = ("glue:CancelStatement", "glue:ListSessions", "glue:ListStatements")


# ── TypeScript reading helpers ────────────────────────────────────────
# Every helper asserts rather than returning a quietly-empty result: a silent
# miss turns a real IAM gap into a passing test, which is the one failure mode
# this file cannot afford.


def _strip_comments(source: str) -> str:
    """``source`` with ``//`` and ``/* */`` comments replaced by spaces.

    Length-preserving (newlines kept, everything else blanked) so an offset in
    an assertion message still corresponds to the real file.

    This runs **before** any delimiter scanning, and that ordering is the point.
    ``foundation-stack.ts`` is full of possessives in comments — "the role's
    Glue statement", "the pipeline's target" — and a scanner that treats ``'``
    as a string delimiter silently swallows everything up to the next
    apostrophe. Comment-blind scanning is a real bug in a sibling file, not a
    hypothetical one.
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

    Skips quoted strings so ``${account}`` inside a template literal is not
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


def _split_top_level(text: str) -> list[str]:
    """Split on commas not nested inside brackets, braces or quotes."""
    parts: list[str] = []
    depth = 0
    current = ""
    i = 0
    while i < len(text):
        ch = text[i]
        if ch in "'\"`":
            quote = ch
            current += ch
            i += 1
            while i < len(text) and text[i] != quote:
                current += text[i]
                i += 1
            current += text[i] if i < len(text) else ""
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


def _role_constructor_args(stripped: str, role_prop: str) -> str:
    """The ``new iam.Role(...)`` argument text for ``this.<role_prop>``."""
    match = re.search(rf"this\.{role_prop}\s*=\s*new\s+iam\.Role\(", stripped)
    assert match, (
        f"this.{role_prop} is not assigned from `new iam.Role(` — re-read its "
        f"construction before trusting anything below"
    )
    open_paren = match.end() - 1
    return stripped[open_paren : _match_delimiter(stripped, open_paren, "(", ")")]


def _add_to_policy_spans(stripped: str, role_prop: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``this.<role_prop>.addToPolicy(...)`` argument list.

    Offsets rather than substrings so a caller can slice the **raw** source at
    the same position. ``_strip_comments`` is length-preserving precisely to make
    that legal: R6.6's fence lives in a comment inside a statement body, so
    finding the statement needs the stripped text and reading the fence needs the
    raw text.
    """
    spans = []
    for call in re.finditer(rf"this\.{role_prop}\.addToPolicy\(", stripped):
        open_paren = call.end() - 1
        spans.append((open_paren, _match_delimiter(stripped, open_paren, "(", ")")))
    return spans


def _policy_statement_spans(stripped: str, span: tuple[int, int]) -> list[tuple[int, int]]:
    """``(start, end)`` of every ``new iam.PolicyStatement({...})`` body in ``span``."""
    start, end = span
    spans = []
    for match in re.finditer(r"new iam\.PolicyStatement\(\{", stripped[start:end]):
        open_brace = start + match.end() - 1
        spans.append((open_brace, _match_delimiter(stripped, open_brace, "{", "}")))
    return spans


def _statement_spans_on(stripped: str, role_prop: str) -> list[tuple[int, int]]:
    """``(start, end)`` of every policy statement granted to ``this.<role_prop>``."""
    return [
        statement
        for call in _add_to_policy_spans(stripped, role_prop)
        for statement in _policy_statement_spans(stripped, call)
    ]


def _array_after(body: str, key: str) -> list[str] | None:
    """Elements of the ``key: [...]`` array in ``body``, unsplit and unquoted."""
    match = re.search(rf"\b{key}:\s*\[", body)
    if not match:
        return None
    open_bracket = match.end() - 1
    end = _match_delimiter(body, open_bracket, "[", "]")
    return [e.strip() for e in _split_top_level(body[open_bracket + 1 : end - 1])]


def _unquote(literal: str) -> str:
    return literal.strip().strip("'\"` ")


def _statements_on(stripped: str, role_prop: str) -> list[str]:
    """Every policy statement body granted to ``this.<role_prop>``."""
    return [stripped[a:b] for a, b in _statement_spans_on(stripped, role_prop)]


def _actions_on(stripped: str, role_prop: str) -> list[str]:
    """Every action literal granted to ``this.<role_prop>``, duplicates kept."""
    return [
        _unquote(a)
        for statement in _statements_on(stripped, role_prop)
        for a in _array_after(statement, "actions") or []
    ]


def _statements_granting(statements: list[str], action: str) -> list[str]:
    """Statement bodies whose ``actions`` list contains ``action`` literally."""
    return [s for s in statements if action in [_unquote(a) for a in _array_after(s, "actions") or []]]


@pytest.fixture(scope="module")
def raw_source() -> str:
    """The file as written, comments included (R6.6's fence lives in one)."""
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def source(raw_source) -> str:
    """The file with comments blanked — the input every scanner expects."""
    return _strip_comments(raw_source)


# ── Guards: the parser found the right thing ───────────────────────────
# These must be GREEN for any assertion below to mean anything. A test that
# fails because it sliced nothing proves nothing.


def test_comment_stripping_removes_the_apostrophes_that_desync_the_scanner(raw_source, source):
    """The inherited comment-blindness bug is neutralised, and provably so.

    Positive control in two directions. First, the file really does contain the
    hazard: at least one ``//`` comment carrying a lone apostrophe. Second,
    stripping removes it — so the delimiter scanners below only ever see real
    string literals. Without this the whole file could go green while its
    scanners were reading from the wrong offsets.
    """
    hazard_lines = [
        line for line in raw_source.splitlines() if "//" in line and line.split("//", 1)[1].count("'") % 2 == 1
    ]
    assert hazard_lines, (
        "no comment in foundation-stack.ts carries an odd number of apostrophes, so "
        "this guard is no longer exercising anything. Either the file changed or the "
        "detection is wrong — check before deleting the guard"
    )
    assert len(raw_source) == len(source), (
        "_strip_comments is not length-preserving, so offsets in assertion messages "
        "no longer correspond to the real file"
    )
    for line in hazard_lines:
        comment = line.split("//", 1)[1]
        assert comment not in source, (
            f"comment text survived stripping: {comment.strip()!r} — the scanners "
            f"below will treat its apostrophe as a string delimiter and desynchronise"
        )


def test_the_verification_role_and_its_statements_are_located(source):
    """The role is found, and its three known statements are collected.

    Task 2.1 created exactly three: source reads, catalog reads, log writes. If
    fewer are collected the parser is at fault, and the allowlist assertion
    below would pass by reading an empty set.
    """
    args = _role_constructor_args(source, VERIFICATION_ROLE_PROP)
    assert ROLE_NAME in args, (
        f"the {VERIFICATION_ROLE_PROP} constructor does not carry roleName "
        f"{ROLE_NAME!r} — either the role was renamed or the slice is wrong"
    )

    statements = _statements_on(source, VERIFICATION_ROLE_PROP)
    assert len(statements) == 3, (
        f"expected 3 policy statements on {VERIFICATION_ROLE_PROP}, collected "
        f"{len(statements)}. Design §4.6 grants exactly three groups (source reads, "
        f"catalog reads, log writes); a different count means either the parser "
        f"missed one or the role was widened"
    )


def test_the_verification_role_slice_does_not_reach_the_agentcore_role(source):
    """Name-scoped collection cannot pick up a neighbour's grant.

    The AgentCore runtime roles are defined immediately after and hold ``glue:*``,
    ``s3tables:*``, ``dynamodb:PutItem`` and ``bedrock:InvokeModel``. If any of
    those appear on the verification role's collected statements the scoping is
    broken and the allowlist assertion is reading the wrong role.
    """
    actions = set(_actions_on(source, VERIFICATION_ROLE_PROP))

    for foreign in ("glue:*", "s3tables:*", "dynamodb:PutItem", "bedrock:InvokeModel"):
        assert foreign not in actions, (
            f"{foreign} was collected onto {VERIFICATION_ROLE_PROP} — the collector is "
            f"reaching into an AgentCore runtime role, so R6.3's allowlist is checking the "
            f"wrong role. Collected: {sorted(actions)}"
        )


def test_addtopolicy_is_the_only_grant_route_to_the_role(source):
    """Completeness: no grant reaches this role except by ``addToPolicy``.

    The allowlist below is only the whole authority if ``addToPolicy`` is the
    only way a permission arrives. ``addManagedPolicy``, ``attachInlinePolicy``,
    an ``iam.Policy({roles: [...]})``, or a resource-side convenience grant
    (``bucket.grantReadWrite(this.verificationSessionRole)``) would each add
    actions this file never sees.

    Enumerating every mention and failing on an unrecognised one is deliberately
    stricter than pattern-matching the known-bad forms: a route nobody thought
    of still fails, and the failure names the line.
    """
    permitted = (
        # The declaration and the assignment.
        re.compile(rf"public readonly {VERIFICATION_ROLE_PROP}: iam\.Role;"),
        re.compile(rf"this\.{VERIFICATION_ROLE_PROP} = new iam\.Role\("),
        # The three grants this file reads.
        re.compile(rf"this\.{VERIFICATION_ROLE_PROP}\.addToPolicy\("),
        # Referencing the ARN grants nothing to the role (it is the PassRole
        # resource on testCodeRuntimeRole and codeTestingRole — R6.7's subject).
        re.compile(rf"this\.{VERIFICATION_ROLE_PROP}\.roleArn"),
    )

    unrecognised = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if VERIFICATION_ROLE_PROP not in line:
            continue
        if not any(p.search(line) for p in permitted):
            unrecognised.append(f"{lineno}: {line.strip()}")

    assert not unrecognised, (
        f"{VERIFICATION_ROLE_PROP} is referenced in a way this file does not read, so "
        f"R6.3's allowlist may be incomplete:\n  " + "\n  ".join(unrecognised) + "\n"
        "If the new reference grants a permission, teach _statements_on about it. If it "
        "does not, add it to `permitted` with a note saying why it grants nothing."
    )


# ── Requirement 6.1: the role exists and trusts Glue ──────────────────


def test_the_verification_role_trusts_the_glue_service_principal(source):
    """R6.1 — Glue must be able to assume the role, or no session can start.

    Asserted on the ``assumedBy`` argument rather than on file-wide presence of
    the principal string: ``glue.amazonaws.com`` appears several times in this
    file (the ``iam:PassedToService`` conditions, ``gluePipelineRole``'s trust),
    so a bare substring test would pass with the trust policy missing entirely.
    """
    args = _role_constructor_args(source, VERIFICATION_ROLE_PROP)

    match = re.search(r"assumedBy:\s*new\s+iam\.ServicePrincipal\(\s*'([^']+)'", args)
    assert match, (
        f"{VERIFICATION_ROLE_PROP} has no `assumedBy: new iam.ServicePrincipal(...)` — "
        f"Glue cannot assume it, so CreateSession fails with a trust-policy error"
    )
    assert match.group(1) == GLUE_SERVICE_PRINCIPAL, (
        f"{VERIFICATION_ROLE_PROP} trusts {match.group(1)!r}, not "
        f"{GLUE_SERVICE_PRINCIPAL!r} — the session cannot assume it"
    )


# ── Requirement 6.2: the three reads the verification path needs ───────


@pytest.mark.parametrize(
    "action_group,label",
    [
        (SOURCE_READ_ACTIONS, "source-bucket and processed-bucket source reads"),
        (CATALOG_READ_ACTIONS, "Glue Data Catalog reads"),
        (LOG_WRITE_ACTIONS, "/aws-glue/* log writes"),
    ],
)
def test_the_verification_role_grants_each_required_read(source, action_group, label):
    """R6.2 — each of the three grant groups is present, in full.

    Grouped rather than checked one action at a time because each group is only
    useful whole: ``s3:GetObject`` without ``s3:ListBucket`` cannot enumerate a
    prefix, and ``logs:PutLogEvents`` without ``logs:CreateLogStream`` has
    nowhere to write.
    """
    statements = _statements_on(source, VERIFICATION_ROLE_PROP)
    granted = {_unquote(a) for s in statements for a in _array_after(s, "actions") or []}

    missing = [a for a in action_group if a not in granted]
    assert not missing, (
        f"{VERIFICATION_ROLE_PROP} is missing {missing} — {label} incomplete. "
        f"An absent grant surfaces as an AccessDenied inside a Glue statement, "
        f"which reads as a failed transform rather than as a failed harness. "
        f"Granted: {sorted(granted)}"
    )


@pytest.mark.parametrize(
    "bucket_prop,why_this_bucket",
    [
        (
            "sourceBucket",
            "the S3-upload path's source_data_uri names it: file_validation writes "
            "original_s3_uri from the upload event's own bucket (:632), schema_extract "
            "carries it to json_s3_uri (:904 -> :960 -> :1021), and Step Functions maps "
            "that to source_data_uri (orchestration-stack.ts:594, via source_sample_uri)",
        ),
        (
            "processedBucket",
            "the legacy converted-JSON path (schema_extract:961-970) makes json_s3_uri "
            "the canonical URI, and that object lives in the processed bucket",
        ),
    ],
)
def test_the_source_read_reaches_both_sampled_buckets(source, bucket_prop, why_this_bucket):
    """Each sampled bucket is named, in BOTH ARN forms.

    AMENDED (p44). This assertion previously required ``processedBucket`` alone
    and forbade anything else, which pinned a live defect: a sample read on an
    S3-upload run resolves to ``sourceBucket``, so the grant denied it and the
    denial surfaced inside the Glue session as a failed transform.

    Written as "both are required" rather than "these two are permitted" on
    purpose. A permissive form was green on the original single-bucket grant and
    would be green again if the source-bucket resources were deleted — it would
    pin nothing in either direction.

    Both ARN forms per bucket because ``s3:ListBucket`` authorizes against the
    bucket ARN and ``s3:GetObject`` against the object ARN, so a statement naming
    only one of them reads as coverage and denies half the calls.
    """
    statements = _statements_granting(_statements_on(source, VERIFICATION_ROLE_PROP), "s3:GetObject")
    assert statements, "no statement grants s3:GetObject, so its scope cannot be checked"

    resources = [r for s in statements for r in _array_after(s, "resources") or []]
    arn = f"{bucket_prop}.bucketArn"

    assert any(arn in r and "/*" in r for r in resources), (
        f"the s3:GetObject grant does not name {bucket_prop}'s object ARN "
        f"(`${{...{arn}}}/*`) — GetObject authorizes on the object ARN, so a "
        f"bucket-only resource denies the read. This bucket is required because "
        f"{why_this_bucket}. Resources: {resources}"
    )
    assert any(arn in r and "/*" not in r for r in resources), (
        f"the statement does not name {bucket_prop}'s bucket ARN itself — s3:ListBucket "
        f"authorizes on the bucket, so prefix enumeration is denied. Resources: {resources}"
    )


def test_the_source_read_reaches_no_bucket_beyond_those_two(source):
    """The p44 widening is exactly two buckets — no wildcard, no third bucket.

    The companion to the test above, and the half that keeps the fix from
    becoming a licence. Widening to ``adi-*`` would be the one-line version of
    the same fix and would also hand the session the target schemas, the
    generated code and the test outputs; enumerating a third project bucket would
    need its own justification in the URI trace, which does not name one.
    """
    statements = _statements_granting(_statements_on(source, VERIFICATION_ROLE_PROP), "s3:GetObject")
    assert statements, "no statement grants s3:GetObject, so its scope cannot be checked"

    resources = [r for s in statements for r in _array_after(s, "resources") or []]
    assert not any("adi-*" in r for r in resources), (
        f"the source read reaches every adi-* bucket. It is scoped to the two buckets a "
        f"sample URI can actually name — sourceBucket (S3-upload path) and "
        f"processedBucket (legacy converted-JSON path) — and nothing else. "
        f"Resources: {resources}"
    )

    permitted_props = {"sourceBucket", "processedBucket"}
    foreign = [r for r in resources if not any(f"{prop}.bucketArn" in r for prop in permitted_props)]
    assert not foreign, (
        f"the source read names bucket resource(s) outside "
        f"{sorted(permitted_props)}: {foreign}. Every entry must be traceable to a URI "
        f"a verification sample can be read from — add the trace before adding the bucket"
    )


def test_the_log_write_is_scoped_to_the_aws_glue_group(source):
    """Log writes reach ``/aws-glue/*`` only, not every log group."""
    statements = _statements_granting(_statements_on(source, VERIFICATION_ROLE_PROP), "logs:PutLogEvents")
    assert statements, "no statement grants logs:PutLogEvents, so its scope cannot be checked"

    resources = [r for s in statements for r in _array_after(s, "resources") or []]
    assert resources, "the log-write statement enumerates no resources"
    assert all("/aws-glue/" in r for r in resources), f"a log-write resource reaches outside /aws-glue/*: {resources}"


# ── Requirement 6.3: nothing outside the read/log allowlist ────────────
# The load-bearing assertion in this file.


def test_the_verification_role_grants_nothing_outside_the_allowlist(source):
    """R6.3 — every action on the role is in ``ALLOWED_ACTIONS``.

    An allowlist, not a denylist, and the difference is the whole point: a
    denylist of writes passes the first time someone adds an action nobody
    thought to forbid, while this fails on **any** widening and names it.

    What R6.3 is protecting: the code a verification session executes is
    AI-generated and unreviewed by a human at the point TestCode runs — the
    code-approval gate is downstream (``orchestration-stack.ts``,
    ``CheckCodeApproval`` after ``TestResultChoice``). A write action here means
    unreviewed generated PySpark holding write access to the data it is meant to
    be verifying against, which is not a verification step.
    """
    actions = set(_actions_on(source, VERIFICATION_ROLE_PROP))
    assert actions, (
        "no actions were collected from the verification role — the parser found nothing and this assertion is vacuous"
    )

    extra = sorted(actions - ALLOWED_ACTIONS)
    assert not extra, (
        f"{VERIFICATION_ROLE_PROP} grants {extra}, which is outside the read/log "
        f"allowlist. Read the Decision in design §4.6 before widening: this role "
        f"executes unreviewed AI-generated PySpark, so a write here lets a "
        f"verification step corrupt what it is verifying. "
        f"Allowlist: {sorted(ALLOWED_ACTIONS)}"
    )


def test_no_wildcard_action_reaches_the_verification_role(source):
    """No ``service:*`` action, which would make the allowlist unenforceable.

    Separated from the allowlist assertion so the failure reads correctly: a
    wildcard is not "one unexpected action", it is an unbounded set, and the
    remedy is different (enumerate, per ``LGR-b46a5bb-2``'s posture that
    widening must require a deliberate policy edit).
    """
    wildcards = sorted(a for a in set(_actions_on(source, VERIFICATION_ROLE_PROP)) if "*" in a)
    assert not wildcards, (
        f"{VERIFICATION_ROLE_PROP} carries wildcard action(s) {wildcards}. Enumerate "
        f"instead: a wildcard makes the allowlist above unenforceable, and future "
        f"additions to the service's action set arrive silently"
    )


def test_no_managed_policy_is_attached_to_the_verification_role(source):
    """A managed policy would carry actions the allowlist never sees.

    ``LGR-b46a5bb-2`` (template / ratified) rejected exactly this shortcut for
    ``agentDispatchRole`` — attaching ``AgentCorePolicy`` is one line instead of
    two statements, and it grants ``s3tables:*`` plus
    ``bedrock-agentcore:InvokeAgentRuntime`` on ``Resource: '*'`` along the way.
    The same shortcut is available here and would defeat R6.3 while leaving the
    allowlist assertion above green.
    """
    args = _role_constructor_args(source, VERIFICATION_ROLE_PROP)
    assert "managedPolicies" not in args, (
        f"{VERIFICATION_ROLE_PROP} is constructed with managedPolicies. Its actions are "
        f"then invisible to R6.3's allowlist. State narrow statements instead "
        f"(LGR-b46a5bb-2)"
    )
    assert f"this.{VERIFICATION_ROLE_PROP}.addManagedPolicy" not in source, (
        f"addManagedPolicy is called on {VERIFICATION_ROLE_PROP} — same problem as "
        f"above, one line further from the constructor"
    )


def test_the_allowlist_itself_stays_read_shaped(source):
    """Meta-assertion: widening the allowlist to match a widened role fails too.

    Without this, R6.3 is one edit away from vacuous — add the action to the
    role, add it to ``ALLOWED_ACTIONS``, green. So the allowlist is itself
    constrained: only the three services R6.2 names, only read-shaped verbs on
    ``s3`` and ``glue``, and on ``logs`` only the two stream-write actions R6.2
    explicitly permits.

    Deliberately does not read the file. It is a property of the constant, and
    it fails at edit time rather than at deploy time.
    """
    read_verb = re.compile(r"^(Get|List|Describe|Head|BatchGet)")

    for action in sorted(ALLOWED_ACTIONS):
        service, _, verb = action.partition(":")
        assert service in {"s3", "glue", "logs"}, (
            f"{action} is outside the services R6.2 names (s3, glue, logs). R6.3 "
            f"forbids reaching a DynamoDB item, a secret or a table bucket at all"
        )
        if service == "logs":
            assert action in LOG_WRITE_ACTIONS, (
                f"{action} is a logs action outside the two R6.2 permits "
                f"({list(LOG_WRITE_ACTIONS)}) — no CreateLogGroup, no reads"
            )
        else:
            assert read_verb.match(verb), (
                f"{action} is not read-shaped. R6.3 grants no action that creates, "
                f"modifies or deletes a pipeline target, a catalog object, a table "
                f"bucket, a DynamoDB item or a secret — adding this to the allowlist "
                f"is the widening R6.3 exists to prevent"
            )


# ── Requirement 6.5 / 6.6: the caller-side Glue statements ───────────
#
# Before R-02 one shared AgentCore role reached sessions by adding ``session/*``
# to a ``glue:*`` statement that also carried the project naming fence. The
# split separated the two: the test_code runtime role now holds the six session
# APIs, enumerated exactly as codeTestingRole holds them, and the ``glue:*``
# statement with its naming fence lives on the job-builder runtime role only.


def _glue_star_span(source: str) -> tuple[int, int]:
    """``(start, end)`` of the single ``glue:*`` statement on the job-builder runtime role."""
    spans = [
        span
        for span in _statement_spans_on(source, JOB_BUILDER_ROLE_PROP)
        if "glue:*" in [_unquote(a) for a in _array_after(source[span[0] : span[1]], "actions") or []]
    ]
    assert len(spans) == 1, (
        f"expected exactly one glue:* statement on {JOB_BUILDER_ROLE_PROP}, found "
        f"{len(spans)} — R6.6's naming fence describes one statement, so a "
        f"different count means the parser or the stack changed"
    )
    return spans[0]


def _glue_star_statement(source: str) -> str:
    """The ``glue:*`` statement on the job-builder runtime role."""
    start, end = _glue_star_span(source)
    return source[start:end]


def test_the_agentcore_role_reaches_the_whole_session_lifecycle(source):
    """R6.5 — the test_code runtime role holds every API the runner calls.

    The live verification path runs in the test_code AgentCore container, so a
    missing action here is an AccessDenied partway through a run, after a
    session has already been created and charged.
    """
    granted = {
        a
        for s in _session_statements(source, AGENTCORE_ROLE_PROP)
        for a in [_unquote(x) for x in _array_after(s, "actions") or []]
    }
    missing = sorted(SESSION_LIFECYCLE_ACTIONS - granted)
    assert not missing, f"{AGENTCORE_ROLE_PROP} is missing {missing}. Granted: {sorted(granted)}"


def test_the_agentcore_session_grant_is_scoped_to_sessions_only(source):
    """The session statement reaches ``session/*`` and nothing else."""
    statements = _session_statements(source, AGENTCORE_ROLE_PROP)
    assert statements, f"no statement on {AGENTCORE_ROLE_PROP} grants a session-lifecycle action"
    resources = [r for s in statements for r in _array_after(s, "resources") or []]
    assert resources and all(SESSION_RESOURCE_SUFFIX in r for r in resources), (
        f"a resource on {AGENTCORE_ROLE_PROP}'s session statement reaches outside {SESSION_RESOURCE_SUFFIX}: {resources}"
    )


def test_no_wildcard_glue_action_reaches_the_agentcore_verifier_role(source):
    """The verifier's runtime role enumerates; only the job builder holds ``glue:*``.

    ``LGR-b46a5bb-2``'s posture: enumerate, so that widening later requires a
    deliberate policy edit. Also load-bearing for ``foundation-iam.test.ts``'s
    R6.6 test, which requires every ``glue:*`` statement in the template to
    carry both naming forms.
    """
    wildcards = sorted(a for a in set(_actions_on(source, AGENTCORE_ROLE_PROP)) if a.endswith(":*"))
    assert not wildcards, f"{AGENTCORE_ROLE_PROP} carries wildcard action(s) {wildcards}"


@pytest.mark.parametrize("action", SESSION_ACTIONS_NOT_CALLED)
def test_no_session_action_the_runner_does_not_call_is_granted_to_the_agentcore_role(source, action):
    """Same derivation rule as for codeTestingRole below."""
    assert action not in set(_actions_on(source, AGENTCORE_ROLE_PROP)), (
        f"{action} is granted to {AGENTCORE_ROLE_PROP} but glue_session_runner.py never calls it"
    )


@pytest.mark.parametrize("snake,kebab", NAMING_FENCE_PAIRS)
def test_both_glue_naming_forms_survive_on_the_statement(source, snake, kebab):
    """R6.6 — the dual snake/kebab forms are still both present.

    This is a fence, not a preference. The kebab form was added after a verified
    ``glue:GetJob`` AccessDenied from the AgentCore runtime in fresh-account
    validation (2026-07-17); the snake form is required because Glue forbids
    hyphens in database and table names. R-02 moved this statement onto the
    job-builder runtime role, which is exactly the kind of edit that could drop
    an entry, so it is asserted rather than trusted.
    """
    resources = _array_after(_glue_star_statement(source), "resources") or []
    joined = "\n".join(resources)

    for form, style in ((snake, "snake_case"), (kebab, "kebab-case")):
        assert form in joined, (
            f"the {style} form {form!r} is missing from the glue:* statement's "
            f"resources. Both forms are a NAMING FENCE (p31 C3) — do not delete "
            f"either. Resources: {resources}"
        )


def test_the_naming_fence_comment_is_still_there(raw_source, source):
    """The comment recording *why* both forms exist survives, on THIS statement.

    The forms and the reason have to travel together: a reader who finds two
    near-identical ARNs and no explanation deletes one. Scoped to the ``glue:*``
    statement's own body, not to the file, because ``NAMING FENCE`` appears
    several times in ``foundation-stack.ts`` and a file-wide substring test stays
    green while this statement's fence is deleted.

    Reads ``raw_source`` at offsets computed from the comment-stripped copy,
    which is why ``_strip_comments`` is length-preserving.
    """
    start, end = _glue_star_span(source)
    raw_statement = raw_source[start:end]

    assert NAMING_FENCE_COMMENT in raw_statement, (
        f"the {NAMING_FENCE_COMMENT!r} comment is gone from the glue:* statement on "
        f"{JOB_BUILDER_ROLE_PROP}. The dual naming forms on this statement are "
        f"load-bearing (a verified glue:GetJob AccessDenied) and this comment is the "
        f"only record of why — restore it"
    )
    assert "do NOT delete either form" in raw_statement, (
        "the naming fence comment on the glue:* statement no longer says not to delete "
        "either form — that instruction is the whole content of the fence"
    )


def test_the_job_builder_glue_statement_reaches_no_session(source):
    """``session/*`` moved to the verifier's role; the job builder runs no session."""
    resources = _array_after(_glue_star_statement(source), "resources") or []
    assert not any(SESSION_RESOURCE_SUFFIX in r for r in resources), (
        f"the glue:* statement on {JOB_BUILDER_ROLE_PROP} reaches sessions again: {resources}"
    )


# ── Requirement 6.7: the conditioned PassRole ─────────────────────────


def _conditioned_passrole_statements(source: str) -> list[str]:
    """``iam:PassRole`` statements on the verifier's runtime role naming the new role's ARN.

    Selected by resource, not by action, so that some other PassRole cannot
    satisfy R6.7 by accident.
    """
    return [
        s
        for s in _statements_granting(_statements_on(source, AGENTCORE_ROLE_PROP), PASS_ROLE)
        if any(f"{VERIFICATION_ROLE_PROP}.roleArn" in r for r in _array_after(s, "resources") or [])
    ]


def test_the_passrole_grant_on_the_new_role_is_conditioned(source):
    """R6.7 — ``iam:PassedToService`` equal to ``glue.amazonaws.com``.

    The condition is what stops the grant being a general "hand this role to
    anything" capability: without it, any service the runtime role can call
    could be given a role that reads the source and processed buckets.
    """
    statements = _conditioned_passrole_statements(source)
    assert statements, (
        f"no {PASS_ROLE} statement on {AGENTCORE_ROLE_PROP} names "
        f"this.{VERIFICATION_ROLE_PROP}.roleArn. Without it CreateSession fails on the "
        f"Role parameter even with every session action granted"
    )

    for statement in statements:
        assert "conditions" in statement, (
            f"the {PASS_ROLE} grant on {VERIFICATION_ROLE_PROP} carries no conditions "
            f"block. R6.7 requires {PASSED_TO_SERVICE} = {GLUE_SERVICE_PRINCIPAL}"
        )
        condition = re.search(
            rf"conditions:\s*\{{\s*StringEquals:\s*\{{\s*'{re.escape(PASSED_TO_SERVICE)}'\s*:\s*'([^']+)'",
            statement,
        )
        assert condition, (
            f"the {PASS_ROLE} grant's conditions block does not carry a StringEquals on "
            f"{PASSED_TO_SERVICE}. Statement: {statement.strip()}"
        )
        assert condition.group(1) == GLUE_SERVICE_PRINCIPAL, (
            f"{PASSED_TO_SERVICE} is {condition.group(1)!r}, not "
            f"{GLUE_SERVICE_PRINCIPAL!r} — the role could be passed to the wrong service"
        )


def test_the_verifier_runtime_role_can_pass_only_the_verification_role(source):
    """Since R-02 the verifier's runtime role holds exactly one PassRole: this one.

    The shared role used to carry a second, the per-pipeline ``-ExecutionRole``
    grant the job builder needs. That stayed with the job builder, so a second
    PassRole appearing here is a widening, not a refactor.
    """
    passrole = _statements_granting(_statements_on(source, AGENTCORE_ROLE_PROP), PASS_ROLE)
    assert len(passrole) == 1, (
        f"expected exactly one {PASS_ROLE} statement on {AGENTCORE_ROLE_PROP} (the verification role), "
        f"found {len(passrole)}"
    )


# ── The Lambda-side break-glass grant ─────────────────────────────────
#
# A widening past R6.5/R6.7 as written, asserted here so it stays legible as a
# considered choice. Nothing below changes what the verification session role
# itself may do: R6.3's allowlist above is scoped to
# ``verificationSessionRole`` and still passes unchanged, which is the assertion
# that the *assumed* role was not widened along with the *calling* one.


def _passrole_on_verification_role(source: str, role_prop: str) -> list[str]:
    """``iam:PassRole`` statements on ``role_prop`` naming the new role's ARN.

    Selected by resource, exactly as ``_conditioned_passrole_statements`` does
    for the runtime role, and for the same reason: "some statement grants
    PassRole" is too weak to pin anything.
    """
    return [
        s
        for s in _statements_granting(_statements_on(source, role_prop), PASS_ROLE)
        if any(f"{VERIFICATION_ROLE_PROP}.roleArn" in r for r in _array_after(s, "resources") or [])
    ]


def _session_statements(source: str, role_prop: str) -> list[str]:
    """Statements on ``role_prop`` granting any session-lifecycle action."""
    statements = _statements_on(source, role_prop)
    return [
        s for s in statements if SESSION_LIFECYCLE_ACTIONS & {_unquote(a) for a in _array_after(s, "actions") or []}
    ]


def test_the_code_testing_role_reaches_the_whole_session_lifecycle(source):
    """Every API ``glue_session_runner.py`` calls is granted.

    Grouped rather than one action per test because the lifecycle is only useful
    whole: ``CreateSession`` without ``DeleteSession`` leaks a billable session,
    and ``RunStatement`` without ``GetStatement`` can start a statement it can
    never read the result of. A missing one surfaces as an ``AccessDenied``
    partway through a run, after a session has already been created and charged.
    """
    granted = {
        a
        for s in _session_statements(source, CODE_TESTING_ROLE_PROP)
        for a in [_unquote(x) for x in _array_after(s, "actions") or []]
    }

    missing = sorted(SESSION_LIFECYCLE_ACTIONS - granted)
    assert not missing, (
        f"{CODE_TESTING_ROLE_PROP} is missing {missing}. The inline verification path "
        f"(USE_AGENTCORE_RUNTIME off) runs glue_session_runner.py under this role, so an "
        f"absent action is an AccessDenied mid-run. Granted: {sorted(granted)}"
    )


def test_the_code_testing_session_grant_is_scoped_to_sessions_only(source):
    """The session statement reaches ``session/*`` and nothing else.

    The point of enumerating actions is lost if the resource list is wide: a
    catalog or job ARN here would give the Lambda's six session verbs a reach
    they have no use for.
    """
    statements = _session_statements(source, CODE_TESTING_ROLE_PROP)
    assert statements, (
        f"no statement on {CODE_TESTING_ROLE_PROP} grants a session-lifecycle action, so "
        f"its scope cannot be checked — see the test above"
    )

    resources = [r for s in statements for r in _array_after(s, "resources") or []]
    assert resources, "the session statement enumerates no resources"
    assert all(SESSION_RESOURCE_SUFFIX in r for r in resources), (
        f"a resource on {CODE_TESTING_ROLE_PROP}'s session statement reaches outside "
        f"{SESSION_RESOURCE_SUFFIX}: {resources}"
    )


@pytest.mark.parametrize("action", SESSION_ACTIONS_NOT_CALLED)
def test_no_session_action_the_runner_does_not_call_is_granted(source, action):
    """The enumeration stays derived from the runner, not from the API surface.

    ``glue_session_runner.py`` calls no ``CancelStatement``, ``ListSessions`` or
    ``ListStatements`` — verified by reading it. Granting them "for completeness"
    is the drift ``LGR-b46a5bb-2`` guards against: a widening should be a
    deliberate edit, and this test makes it one. If the runner starts calling any
    of these, move it into ``SESSION_LIFECYCLE_ACTIONS`` in the same change.
    """
    granted = {a for a in _actions_on(source, CODE_TESTING_ROLE_PROP)}
    assert action not in granted, (
        f"{action} is granted to {CODE_TESTING_ROLE_PROP} but glue_session_runner.py "
        f"never calls it. Either the runner changed — in which case move it into "
        f"SESSION_LIFECYCLE_ACTIONS — or this is speculative widening"
    )


def test_no_wildcard_glue_action_reaches_the_code_testing_role(source):
    """The divergence from the AgentCore grant's ``glue:*`` shape is deliberate.

    The AgentCore statement reaches sessions by adding ``session/*`` to a
    statement whose action was already ``glue:*``. Mirroring that shape here
    would be the smaller diff and is the wrong trade: it hands a Lambda role
    ``glue:*`` over catalogs, databases, tables, jobs, crawlers and connections
    to buy six session calls. ``LGR-b46a5bb-2``'s posture governs — enumerate, so
    that widening later requires a deliberate policy edit.

    Also load-bearing for a neighbour: ``_glue_star_span`` asserts there is
    **exactly one** ``glue:*`` statement on the runtime role, and
    ``foundation-iam.test.ts``'s R6.6 test requires **every** ``glue:*``
    statement in the template to carry both naming forms. A ``glue:*`` here
    would trip that one from across the file.
    """
    wildcards = sorted(a for a in set(_actions_on(source, CODE_TESTING_ROLE_PROP)) if a.endswith(":*"))
    assert not wildcards, (
        f"{CODE_TESTING_ROLE_PROP} carries wildcard action(s) {wildcards}. Enumerate the "
        f"six session APIs instead (LGR-b46a5bb-2)"
    )


def test_the_code_testing_passrole_is_conditioned(source):
    """The condition is the escalation guard, not decoration.

    This role can now create a Glue session. An unconditioned ``iam:PassRole``
    would therefore let it hand any role it can name to a session it controls,
    which is the privilege-escalation shape the conditioned grants at
    ``foundation-stack.ts:386`` and ``:565-569`` exist to avoid. Same shape as
    those two and as the ``testCodeRuntimeRole`` grant — the point of asserting
    it separately is that a grant silently losing its condition later must fail a
    test rather than deploy.
    """
    statements = _passrole_on_verification_role(source, CODE_TESTING_ROLE_PROP)
    assert statements, (
        f"no {PASS_ROLE} statement on {CODE_TESTING_ROLE_PROP} names "
        f"this.{VERIFICATION_ROLE_PROP}.roleArn. Without it CreateSession fails on the "
        f"Role parameter even with every session action granted"
    )

    for statement in statements:
        assert "conditions" in statement, (
            f"the {PASS_ROLE} grant on {CODE_TESTING_ROLE_PROP} carries no conditions "
            f"block. A principal that can both create a Glue session and pass an "
            f"unconditioned role can hand any nameable role to a session it controls"
        )
        condition = re.search(
            rf"conditions:\s*\{{\s*StringEquals:\s*\{{\s*'{re.escape(PASSED_TO_SERVICE)}'\s*:\s*'([^']+)'",
            statement,
        )
        assert condition, (
            f"the {PASS_ROLE} grant's conditions block on {CODE_TESTING_ROLE_PROP} does "
            f"not carry a StringEquals on {PASSED_TO_SERVICE}. Statement: {statement.strip()}"
        )
        assert condition.group(1) == GLUE_SERVICE_PRINCIPAL, (
            f"{PASSED_TO_SERVICE} is {condition.group(1)!r}, not {GLUE_SERVICE_PRINCIPAL!r} "
            f"— the role could be passed to the wrong service"
        )


def test_no_broad_passrole_reaches_the_code_testing_role(source):
    """The conditioned grant is the *only* route to the verification role.

    The shared AgentCore role once also held an unconditioned ``iam:PassRole``
    on ``role/AgenticDataIngestion-*`` that reached the verification role by
    name; T-03 removed it and R-02 split that role. On ``codeTestingRole`` the
    conditioned statement has always been the sole route, and this test keeps
    it that way. If a broad ``iam:PassRole``
    ever lands on this role, the condition above stops being enforceable and
    this fails first.
    """
    broad = [
        s
        for s in _statements_granting(_statements_on(source, CODE_TESTING_ROLE_PROP), PASS_ROLE)
        if any(BROAD_PASSROLE_RESOURCE in r for r in _array_after(s, "resources") or [])
    ]
    assert not broad, (
        f"{CODE_TESTING_ROLE_PROP} now carries a {PASS_ROLE} on "
        f"{BROAD_PASSROLE_RESOURCE!r}. That reaches the verification role by name with "
        f"no iam:PassedToService condition, so the conditioned grant above no longer "
        f"bounds what this role can pass. Statements: {broad}"
    )


def test_the_code_testing_grants_are_stated_after_the_role_exists(source):
    """Ordering: the PassRole resource cannot be read before it is assigned.

    ``codeTestingRole`` is constructed at ``foundation-stack.ts:366`` and
    ``verificationSessionRole`` at ``:821``. Stating this grant beside the role
    it belongs to — the obvious place, and where a later reader will try to move
    it — reads ``this.verificationSessionRole.roleArn`` while the property is
    still ``undefined``. CDK renders that into the template rather than throwing,
    so the failure is a deploy-time one that ``tsc`` does not catch. Asserted on
    source order because that is the actual constraint.
    """
    assignment = re.search(rf"this\.{VERIFICATION_ROLE_PROP}\s*=\s*new\s+iam\.Role\(", source)
    assert assignment, f"this.{VERIFICATION_ROLE_PROP} assignment not found"

    grants = list(re.finditer(rf"this\.{CODE_TESTING_ROLE_PROP}\.addToPolicy\(", source))
    assert grants, f"no addToPolicy call on {CODE_TESTING_ROLE_PROP} found"

    passrole_grants = [
        g
        for g in grants
        if f"{VERIFICATION_ROLE_PROP}.roleArn" in source[g.end() - 1 : _match_delimiter(source, g.end() - 1, "(", ")")]
    ]
    assert passrole_grants, (
        f"no {CODE_TESTING_ROLE_PROP}.addToPolicy call names "
        f"{VERIFICATION_ROLE_PROP}.roleArn — see test_the_code_testing_passrole_is_conditioned"
    )
    for grant in passrole_grants:
        assert grant.start() > assignment.start(), (
            f"a {CODE_TESTING_ROLE_PROP} grant at offset {grant.start()} references "
            f"this.{VERIFICATION_ROLE_PROP}.roleArn before that role is assigned at "
            f"{assignment.start()}. The ARN resolves to undefined and CDK renders it "
            f"into the template without erroring — move the grant after the role"
        )
