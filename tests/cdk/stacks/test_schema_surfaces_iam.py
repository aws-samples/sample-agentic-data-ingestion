# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK assertion: the four grants p35 needs, on the two roles that need them.

**Why this assertion cannot live below the CDK layer.** ``LGR-7c9378d-1``
(template / ratified) records the mechanism verbatim: the unit tests stub the
boto3 clients, so an IAM gap is invisible to them. Fixing only the Python turns
one error message into another — there, ``Invalid bucket name ""`` became
``AccessDenied``: a different error, the same failed run. Every subject below is
in exactly that position:

- ``job_completion``'s unit tests stub ``glue_client`` and the S3 Tables client,
  so R7.3's ``s3tables:GetTable`` → ``glue:GetTable`` → ``glue:UpdateTable``
  sequence passes in the unit layer whether or not the role may perform it. With
  the grants absent it dies into ``_populate_mirror_metadata_location``'s
  non-blocking ``except`` and logs a warning on **every successful run** —
  present in the code, absent in effect.
- ``dashboard_api``'s unit tests stub ``s3_client``, so R3.1's ``delete_schema``
  passes its tests while returning ``500`` in production on exactly the schemas
  it is *willing* to delete. That is the ``7c9378d`` shape: a fixed handler in
  front of a missing permission.

**The four grants, all four verified ``implicitDeny`` before this change** (IAM
policy simulator, 777788889999 / us-west-2, against real resource ARNs — design
§2):

| Role | Action | Requirement |
|---|---|---|
| ``jobCompletionRole`` | ``s3tables:GetTable`` | 7.3 |
| ``jobCompletionRole`` | ``glue:GetTable`` | 7.3 |
| ``jobCompletionRole`` | ``glue:UpdateTable`` | 7.3 |
| ``infrastructureRole`` | ``s3:DeleteObject`` on the target-schemas bucket | 3.1 |

``glue:GetTable`` is **not** redundant next to ``glue:UpdateTable``. Glue
``UpdateTable`` *replaces* the stored table with the ``TableInput`` it is given,
so ``job_completion``'s write must be read-modify-write or it destroys the
``StorageDescriptor`` and the ``Columns`` Requirement 7.7 maintains.
``create_infrastructure`` only gets away without a read because it still holds
``warehouse_location`` from its own ``s3tables:GetTable``; ``job_completion``
holds none of that.

**The negative assertion is the load-bearing one.** ``LGR-b46a5bb-2``
(template / ratified) rejected attaching ``AgentCorePolicy`` to
``agentDispatchRole`` for exactly the shortcut available here — one line instead
of two statements, and it grants ``s3tables:*`` immediately — because
``agentCorePolicy`` also grants ``bedrock-agentcore:InvokeAgentRuntime`` on
``Resource: '*'``, which would turn a narrow completion role into a generic
invoke-any-agent capability. So this file asserts the three actions are present
*and* that they did not arrive by that route.

**Why a new file in this layer rather than an edit to
``deploy-cdk/test/foundation-iam.test.ts``** (design §4.8 inventory row 16):
searching ``deploy-cdk/test`` for ``jobcompletion`` / ``JobCompletion`` returns
**zero** hits — nothing in the vitest CDK suite asserts anything about this role.
That absence is the reason. This file is the third application of a pattern
ratified twice, after ``test_schema_compare_target_schema_iam.py``
(``LGR-7c9378d-1``) and ``test_code_roles_target_schema_iam.py``
(``LGR-78ce0b7-7``, whose obligation is to state a grant explicitly rather than
lean on a wildcard).

**Expected state when this file lands: RED on four assertions.** None of the four
grants exists yet; task 1.3 adds them. The three guard tests below exist so a red
result is legible as "the grant is absent" rather than "the parser found
nothing" — a test that fails because it sliced an empty section is not
characterization.

Validates: Requirements 3.1, 7.3, 8.11
"""

import os
import re

import pytest

FOUNDATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "foundation-stack.ts"
)

# The two roles under assertion, and the actions each must be able to perform.
JOB_COMPLETION_ROLE = "jobCompletionRole"
INFRASTRUCTURE_ROLE = "infrastructureRole"

# R7.3's three actions on the completion path.
JOB_COMPLETION_ACTIONS = ["s3tables:GetTable", "glue:GetTable", "glue:UpdateTable"]

# R3.1's single action on the dashboard path.
DELETE_OBJECT = "s3:DeleteObject"

# The bucket property a schema delete may touch, and the only one it may touch:
# R3.3 makes catalog-derived schemas undeletable, so no other bucket is ever a
# delete target on this path.
TARGET_SCHEMAS_BUCKET_PROP = "targetSchemasBucket"

# The managed policy that must NOT be the way the three actions arrive.
FORBIDDEN_EXTRA_POLICY = "agentCorePolicy"


# ── TypeScript reading helpers ────────────────────────────────────────
# Kept small, and kept loud: every helper asserts rather than returning a
# quietly-empty result, because a silent miss here turns a real IAM gap into a
# passing test — the exact failure mode this file exists to prevent.


def _match_delimiter(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the delimiter matching the one at ``start``.

    Skips quoted strings so that ``${account}`` inside a template literal does
    not count as a brace.
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
    """Split on commas that are not nested inside brackets, braces or quotes."""
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


def _contiguous_role_section(content: str, role_prop: str) -> str:
    """The role's definition, sliced up to the next role assignment.

    The house pattern from ``test_schema_compare_target_schema_iam.py``.
    """
    pattern = (
        rf"(this\.{role_prop}\s*=\s*lambdaRole.*?)"
        r"(?=this\.\w+Role\s*=\s*(?:lambdaRole|new\s+iam\.Role))"
    )
    match = re.search(pattern, content, re.DOTALL)
    assert match, f"{role_prop} definition not found in foundation-stack.ts"
    return match.group(1)


def _role_section(content: str, role_prop: str) -> str:
    """The role's definition **plus every** ``addToPolicy`` call on it.

    The contiguous slice alone is not the whole role, and this stack proves it:
    ``this.infrastructureRole.addToPolicy(...)`` is called once well after
    ``agentDispatchRole`` has been assigned (the p30a ``lambda:InvokeFunction``
    grant on the dispatch function), so a contiguous slice ending at the next
    role assignment misses it. A grant added at that position would read as
    absent. Both parts are collected here so the assertion answers "does this
    role hold the grant" rather than "does the grant appear in this stretch of
    the file".
    """
    parts = [_contiguous_role_section(content, role_prop)]
    for call in re.finditer(rf"this\.{role_prop}\.addToPolicy\(", content):
        open_paren = call.end() - 1
        parts.append(content[open_paren : _match_delimiter(content, open_paren, "(", ")")])
    return "\n".join(parts)


def _policy_statements(section: str) -> list[str]:
    """Every ``new iam.PolicyStatement({...})`` body inside ``section``."""
    bodies = []
    for match in re.finditer(r"new iam\.PolicyStatement\(\{", section):
        open_brace = match.end() - 1
        bodies.append(section[open_brace : _match_delimiter(section, open_brace, "{", "}")])
    return bodies


def _array_after(body: str, key: str) -> list[str] | None:
    """The elements of the ``key: [...]`` array in ``body``, unsplit."""
    match = re.search(rf"\b{key}:\s*\[", body)
    if not match:
        return None
    open_bracket = match.end() - 1
    end = _match_delimiter(body, open_bracket, "[", "]")
    return _split_top_level(body[open_bracket + 1 : end - 1])


def _statements_granting(section: str, action: str) -> list[str]:
    """Statement bodies whose ``actions`` list contains ``action`` literally.

    Deliberately literal rather than wildcard-aware: design §4.1 requires these
    four grants to be **stated**, following ``LGR-78ce0b7-7``'s obligation, so a
    wildcard that happens to cover them is not what is being asserted. It is
    also what makes the ``agentCorePolicy`` shortcut fail this file rather than
    satisfy it — that policy's ``s3tables:*`` would cover ``s3tables:GetTable``
    under IAM semantics.
    """
    matching = []
    for body in _policy_statements(section):
        actions = [a.strip("'\"` ") for a in _array_after(body, "actions") or []]
        if action in actions:
            matching.append(body)
    return matching


def _extra_policies(content: str, role_prop: str) -> list[str]:
    """The ``extraPolicies`` argument of ``lambdaRole(name, extraPolicies, …)``."""
    helper = re.search(rf"this\.{role_prop}\s*=\s*lambdaRole\(", content)
    assert helper, f"{role_prop} is not defined by lambdaRole() — re-read its construction"
    open_paren = helper.end() - 1
    args = _split_top_level(
        content[open_paren + 1 : _match_delimiter(content, open_paren, "(", ")") - 1]
    )
    assert len(args) >= 2, (
        f"lambdaRole({role_prop}) was called with {len(args)} argument(s); the "
        "extraPolicies argument could not be located, so the negative assertion "
        "below would be vacuous"
    )
    extra = args[1].strip()
    assert extra.startswith("[") and extra.endswith("]"), (
        f"{role_prop}'s extraPolicies argument is not an array literal ({extra!r}) — "
        "the negative assertion below cannot read it"
    )
    return [e.strip() for e in _split_top_level(extra[1:-1])]


@pytest.fixture(scope="module")
def foundation_stack_content() -> str:
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


# ── Guards: the slicing found the right thing ─────────────────────────
# These must be GREEN even while the four assertions below are red. A red
# assertion is only characterization if the section it searched was located and
# non-empty; a test that fails because it sliced nothing proves nothing.


def test_job_completion_role_section_is_located_and_carries_its_existing_grants(
    foundation_stack_content,
):
    """The slice found ``jobCompletionRole`` and its known statements.

    ``glue:GetJobRun`` is the control — the grant this role has held since it was
    created. If it is missing, the parser is at fault and the four assertions
    below are meaningless rather than informative. (Design §7 keeps the same
    action as the control in the live ``simulate-principal-policy`` run, for the
    same reason.)
    """
    section = _role_section(foundation_stack_content, JOB_COMPLETION_ROLE)

    assert _statements_granting(section, "glue:GetJobRun"), (
        "jobCompletionRole's section does not contain its long-standing "
        "glue:GetJobRun grant — the slice or the statement parser is broken, so "
        "every assertion in this file is vacuous"
    )
    assert _statements_granting(section, "events:PutEvents"), (
        "jobCompletionRole's section does not contain the events:PutEvents grant "
        "added by an addToPolicy call — addToPolicy collection is broken"
    )


def test_job_completion_role_section_does_not_bleed_into_the_next_role(
    foundation_stack_content,
):
    """The slice stops at the next role, so a neighbour cannot satisfy it.

    ``selfHealingDispatchRole`` is assigned immediately after
    ``jobCompletionRole`` and holds ``bedrock-agentcore:InvokeAgentRuntime``. If
    that appears inside this section the slice is over-reaching, and a grant on
    some other role could make the three assertions below pass for the wrong
    reason.
    """
    section = _role_section(foundation_stack_content, JOB_COMPLETION_ROLE)

    assert not _statements_granting(section, "bedrock-agentcore:InvokeAgentRuntime"), (
        "jobCompletionRole's section reaches into selfHealingDispatchRole's grant — "
        "the slice is over-reaching and the assertions below could pass on a "
        "neighbouring role's statement"
    )


def test_infrastructure_role_section_captures_its_non_contiguous_grant(
    foundation_stack_content,
):
    """``infrastructureRole``'s statements are not all in one place.

    One ``addToPolicy`` on this role sits after ``agentDispatchRole`` has been
    assigned — the p30a grant on ``function:agentic_data_ingestion_agent_dispatch``.
    A contiguous slice ending at the next role assignment misses it. Pinned
    because it is the reason ``_role_section`` collects ``addToPolicy`` calls
    from the whole file: if it did not, a ``s3:DeleteObject`` statement added at
    that position would read as absent and this file would report a false RED.
    """
    contiguous = _contiguous_role_section(foundation_stack_content, INFRASTRUCTURE_ROLE)
    full = _role_section(foundation_stack_content, INFRASTRUCTURE_ROLE)

    assert "agentic_data_ingestion_agent_dispatch" not in contiguous, (
        "infrastructureRole's out-of-band addToPolicy is now inside the contiguous "
        "slice — re-check whether _role_section's whole-file collection is still "
        "needed, and whether it now double-counts"
    )
    assert "agentic_data_ingestion_agent_dispatch" in full, (
        "infrastructureRole's out-of-band addToPolicy was not collected — a grant "
        "added at that position would read as absent"
    )
    assert len(_policy_statements(full)) > len(_policy_statements(contiguous)), (
        "the full section holds no more statements than the contiguous slice, so "
        "addToPolicy collection contributed nothing"
    )


# ── Requirement 7.3: the three actions on jobCompletionRole ───────────
# Expected RED until task 1.3.


@pytest.mark.parametrize("action", JOB_COMPLETION_ACTIONS)
def test_job_completion_role_grants_the_action_r7_3_needs(foundation_stack_content, action):
    """R7.3 — the role that writes the Glue mirror can perform the write.

    All three are ``implicitDeny`` today. Without them
    ``_populate_mirror_metadata_location`` dies into its non-blocking ``except``
    and logs a warning on every successful run: present in the code, absent in
    effect, and invisible to the unit tests because they stub the clients.
    """
    section = _role_section(foundation_stack_content, JOB_COMPLETION_ROLE)

    granted = [a.strip("'\"` ") for body in _policy_statements(section) for a in _array_after(body, "actions") or []]
    assert _statements_granting(section, action), (
        f"jobCompletionRole has no stated {action} grant — R7.3's write will fail "
        f"with AccessDenied inside a non-blocking except, so no test layer below "
        f"CDK can see it. Actions currently stated on this role: {sorted(set(granted))}"
    )


def test_glue_gettable_and_updatetable_are_granted_together(foundation_stack_content):
    """``glue:GetTable`` is not redundant, and neither action is useful alone.

    Glue ``UpdateTable`` **replaces** the stored table with the ``TableInput``
    given, so the write is read-modify-write or it destroys the
    ``StorageDescriptor`` and the ``Columns`` R7.7 maintains. A grant carrying
    ``UpdateTable`` without ``GetTable`` authorizes exactly the destructive
    half — worse than no grant at all, because it succeeds.
    """
    section = _role_section(foundation_stack_content, JOB_COMPLETION_ROLE)

    can_read = bool(_statements_granting(section, "glue:GetTable"))
    can_write = bool(_statements_granting(section, "glue:UpdateTable"))
    assert can_read == can_write, (
        f"jobCompletionRole's Glue grants are asymmetric (GetTable={can_read}, "
        f"UpdateTable={can_write}). UpdateTable without GetTable authorizes the "
        f"destructive half of a read-modify-write and will erase the "
        f"StorageDescriptor and Columns"
    )
    assert can_read and can_write, (
        "jobCompletionRole grants neither glue:GetTable nor glue:UpdateTable — R7.3 "
        "cannot write the mirror's metadata_location"
    )


# ── Requirement 3.1: s3:DeleteObject on the target-schemas bucket ──────
# Expected RED until task 1.3.


def test_infrastructure_role_can_delete_a_target_schema_object(foundation_stack_content):
    """R3.1 — the dashboard role can delete an uploaded schema's backing object.

    ``s3ReadWrite`` enumerates Get/Put/List/Head/CreateBucket and **not**
    ``DeleteObject``, verified ``implicitDeny``. Without this grant
    ``delete_schema`` returns ``500`` on exactly the schemas it is willing to
    delete — a fixed handler in front of a missing permission.
    """
    section = _role_section(foundation_stack_content, INFRASTRUCTURE_ROLE)

    granted = [a.strip("'\"` ") for body in _policy_statements(section) for a in _array_after(body, "actions") or []]
    assert _statements_granting(section, DELETE_OBJECT), (
        f"infrastructureRole has no stated {DELETE_OBJECT} grant — R3.1's delete "
        f"will return 500 on exactly the schemas it means to delete. Actions "
        f"currently stated on this role: {sorted(set(granted))}"
    )


def test_the_delete_grant_is_scoped_to_the_target_schemas_bucket(foundation_stack_content):
    """The delete is scoped to the one bucket a schema delete may touch.

    Widening the shared ``s3ReadWrite`` statement instead was rejected (design
    §4.1): it is shared by ``codeGenerationRole``, ``codeTestingRole``,
    ``gluePipelineRole`` and ``infrastructureRole``, so it would hand a delete on
    every ``adi-*`` bucket to three roles with no delete to perform. The
    ``7c9378d`` lesson is about the cost of a missing grant, not a licence to
    over-grant.

    Object-level, not bucket-level: ``s3:DeleteObject`` is authorized against the
    object ARN, so a statement naming the bucket ARN alone reads as coverage and
    denies the call.
    """
    section = _role_section(foundation_stack_content, INFRASTRUCTURE_ROLE)
    delete_statements = _statements_granting(section, DELETE_OBJECT)

    assert delete_statements, (
        f"no statement on infrastructureRole grants {DELETE_OBJECT}, so its scope "
        f"cannot be checked"
    )

    resources = [r for s in delete_statements for r in _array_after(s, "resources") or []]
    scoped = [r for r in resources if TARGET_SCHEMAS_BUCKET_PROP in r]
    assert scoped, (
        f"the {DELETE_OBJECT} grant does not reference this.{TARGET_SCHEMAS_BUCKET_PROP} — "
        f"it is either unscoped or scoped to the wrong bucket. Resources: {resources}"
    )
    assert any(f"{TARGET_SCHEMAS_BUCKET_PROP}.bucketArn}}/*" in r for r in scoped), (
        f"the {DELETE_OBJECT} grant names the target-schemas bucket but not its "
        f"objects — s3:DeleteObject needs the /* object ARN. Resources: {scoped}"
    )
    assert not any("adi-*" in r for r in resources), (
        f"the {DELETE_OBJECT} grant reaches every adi-* bucket. Scope it to the "
        f"target-schemas bucket: R3.3 makes catalog-derived schemas undeletable, so "
        f"no other bucket is ever a delete target on this path. Resources: {resources}"
    )


# ── The negative assertion: not by the agentCorePolicy shortcut ────────
# Expected GREEN today, and it must stay green through task 1.3.


def test_agent_core_policy_is_not_attached_to_job_completion_role(foundation_stack_content):
    """``LGR-b46a5bb-2`` rejected exactly this shortcut, for another role.

    Attaching ``agentCorePolicy`` is one line instead of two statements and it
    grants ``s3tables:*`` immediately, which would satisfy R7.3's S3 Tables read.
    It also grants ``bedrock-agentcore:InvokeAgentRuntime`` on ``Resource: '*'``,
    ``glue:Get*``/``glue:List*``/``glue:StartJobRun`` and ``athena:*`` —
    turning a narrow completion role into a generic
    invoke-any-agent capability. ``LGR-b46a5bb-2`` (template / ratified) withheld
    this same policy from ``agentDispatchRole`` on that reasoning; the record
    states it outright: "AgentCorePolicy is deliberately not attached to this
    role: it grants InvokeAgentRuntime on Resource *."

    Note that ``agentCorePolicy``'s ``s3tables:*`` would **not** satisfy
    ``glue:UpdateTable`` — its Glue statement is read-only. So the shortcut is
    not merely over-broad, it is also incomplete for R7.3.
    """
    extra_policies = _extra_policies(foundation_stack_content, JOB_COMPLETION_ROLE)

    assert FORBIDDEN_EXTRA_POLICY not in extra_policies, (
        f"{FORBIDDEN_EXTRA_POLICY} is attached to {JOB_COMPLETION_ROLE} "
        f"(extraPolicies={extra_policies}). LGR-b46a5bb-2 rejected this shortcut: it "
        f"also grants bedrock-agentcore:InvokeAgentRuntime on Resource '*'. State the "
        f"two narrow statements instead."
    )


def test_the_forbidden_policy_detector_can_actually_detect_it(foundation_stack_content):
    """Guard: the negative assertion above is not vacuous.

    ``infrastructureRole`` **does** carry ``agentCorePolicy``, deliberately and
    for unrelated reasons. It is the positive control: if the parser cannot see
    it here, it could not see it on ``jobCompletionRole`` either, and the
    assertion above would pass no matter what was attached.
    """
    extra_policies = _extra_policies(foundation_stack_content, INFRASTRUCTURE_ROLE)

    assert FORBIDDEN_EXTRA_POLICY in extra_policies, (
        f"the extraPolicies parser cannot find {FORBIDDEN_EXTRA_POLICY} on "
        f"{INFRASTRUCTURE_ROLE}, where it is known to be attached "
        f"(found {extra_policies}) — so the negative assertion on "
        f"{JOB_COMPLETION_ROLE} proves nothing"
    )
