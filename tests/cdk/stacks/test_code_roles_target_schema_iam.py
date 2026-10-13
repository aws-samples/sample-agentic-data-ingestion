# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""CDK assertion: the roles that run the target-schema read can reach the bucket.

p33 makes ``test_code`` and ``create_code`` read the target schema from the
canonical location on the pipeline record — ``target_schema_bucket`` +
``target_schema_s3_key``, i.e. the target-schemas bucket (requirement 2.1).
Before p33 those handlers read ``target_schema`` out of the mappings row and
got ``{}``, so no S3 read happened and no grant was exercised.

**No IAM change was needed** for that read, and this file does not assert one.
Verified by reading ``deploy-cdk/lib/foundation-stack.ts``:

- ``:85-88`` declares the bucket as ``adi-target-schemas-${account}-${region}``.
- ``:235-238`` defines ``s3ReadWrite`` with ``s3:GetObject`` on
  ``arn:aws:s3:::adi-*-${account}-${region}`` and the ``/*`` object form.
- ``:355`` gives ``codeGenerationRole`` ``[s3ReadWrite]``; ``:360`` gives
  ``codeTestingRole`` the same.
- ``:774-777`` gives the AgentCore runtime role its own identical ``adi-*``
  resource pair. That role matters most: ``USE_AGENTCORE_RUNTIME=true`` makes
  both handlers return ``invoke_agentcore_runtime(...)`` early, so in production
  the read executes inside the container, under this role — and unlike the two
  Lambda roles it carries **no** managed policies, so the ``adi-*`` wildcard is
  its only path to the object.
- Additionally, and not recorded in the p33 design: ``agentCorePolicy``
  (``:206-209``) grants ``s3:GetObject`` on ``...allBucketArns``, which
  ``:155-157`` builds from six buckets **including**
  ``this.targetSchemasBucket``, in both the bucket and ``/*`` object forms. So
  the two Lambda roles reach the object by two independent paths: the ``adi-*``
  wildcard and an explicit per-bucket ARN.

``schemaCompareRole`` needed an explicit grant (``LGR-7c9378d-1``,
template/ratified) only because it is built at ``:280`` with ``[]`` managed
policies and an enumerated resource list, so nothing wildcarded covered this
bucket. The three roles asserted here are in the opposite position.

**Why assert it anyway.** A wildcard that happens to cover the bucket is weaker
evidence than a stated grant: if ``s3ReadWrite``'s resource list is ever
tightened, the read breaks with an ``AccessDenied`` that no test layer below CDK
can see. ``tests/cdk/stacks/test_schema_compare_target_schema_iam.py`` exists
for exactly that reason and its docstring records the mechanism — the unit tests
stub ``s3_client``, so fixing only the Python turns ``Invalid bucket name ""``
into ``AccessDenied``: a different error, the same failed run.
``LGR-b46a5bb-3`` (template/ratified) is the precedent for stating a grant
anyway "despite an existing project-wide wildcard", on the reasoning that if the
wildcard is tightened the capability must not disappear silently.

**What these assertions key on, and what they deliberately do not.** They do
not match the literal ``adi-*`` string. That would pin today's *spelling* and
would go red on a change that replaced the wildcard with a narrower, explicit,
equally-working grant — while a rename of the bucket out from under the wildcard
would still pass. Instead each assertion resolves the target-schemas bucket's
own name from its ``s3.Bucket`` declaration, builds the object ARN the read
actually targets, and asks whether any resource pattern the role can use with
``s3:GetObject`` *matches* that ARN under IAM wildcard semantics. That is the
capability, so the assertion survives any re-spelling that preserves it and
fails on any change that removes it — including a bucket rename, which the
string form would miss.

The reachable set is the union of the role's inline statements and its attached
managed policies, because that union is what IAM evaluates. Note that managed
policy attachment is itself a governed, per-role decision (``LGR-b46a5bb-2``,
template/ratified, withholds ``AgentCorePolicy`` from the dispatch role on
least-privilege grounds), so a role losing a managed policy is a legitimate
change — one that these assertions will correctly flag if it takes the last
covering grant with it.

Validates: Requirements 2.1, 2.8
"""

import os
import re

import pytest

FOUNDATION_STACK_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "lib", "foundation-stack.ts"
)

# The stack interpolates ${account} / ${region} into every ARN. Any concrete
# pair works for a reachability question, as long as the same pair is used on
# both sides of the match — a grant scoped to one account must not appear to
# cover a bucket named for another.
ACCOUNT = "777788889999"
REGION = "us-west-2"

# The bucket property the p33 read targets, and the roles the read runs under.
TARGET_SCHEMAS_BUCKET_PROP = "targetSchemasBucket"
# The container side of the same read (create_code and test_code run in
# AgentCore with USE_AGENTCORE_RUNTIME=true) is checked against the synthesized
# template in deploy-cdk/test/agentcore-runtime-roles.test.ts. Since R-02 those
# runtime roles are built from parameterized statement factories that this
# source reader cannot evaluate, and a reader that cannot resolve a statement
# fails rather than guesses (test_every_resource_the_parser_examined_resolved).
READING_ROLES = ["codeGenerationRole", "codeTestingRole"]

GET_OBJECT = "s3:GetObject"


# ── TypeScript reading helpers ────────────────────────────────────────
# Deliberately small and deliberately loud: anything these cannot resolve is
# recorded as unresolved rather than dropped, and
# test_every_resource_the_parser_examined_resolved fails on a leftover. A silent
# drop here would turn a real IAM gap into a passing test, which is the exact
# failure mode this file exists to prevent.


def _match_delimiter(text: str, start: int, opener: str, closer: str) -> int:
    """Index just past the delimiter matching the one at ``start``.

    Skips over quoted strings so that ``${account}`` inside a template literal
    does not count as a brace.
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
                i += 2 if text[i] == "\\" else 1
                if text[i - 1] == "\\" and i - 1 < len(text):
                    current += text[i - 1]
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


def _array_after(body: str, key: str) -> list[str] | None:
    """The elements of the ``key: [...]`` array in ``body``, unsplit."""
    match = re.search(rf"\b{key}:\s*\[", body)
    if not match:
        return None
    open_bracket = match.end() - 1
    end = _match_delimiter(body, open_bracket, "[", "]")
    return _split_top_level(body[open_bracket + 1 : end - 1])


def _statement_bodies(text: str) -> list[str]:
    """Every ``new iam.PolicyStatement({...})`` body inside ``text``."""
    bodies = []
    for match in re.finditer(r"new iam\.PolicyStatement\(\{", text):
        open_brace = match.end() - 1
        bodies.append(text[open_brace : _match_delimiter(text, open_brace, "{", "}")])
    return bodies


def _bucket_arns(content: str) -> dict[str, str]:
    """``this.<prop>`` → the bucket's ARN, from its own ``bucketName``."""
    arns = {}
    for match in re.finditer(r"this\.(\w+)\s*=\s*new s3\.Bucket\([^;]*?bucketName:\s*`([^`]*)`", content, re.DOTALL):
        arns[match.group(1)] = "arn:aws:s3:::" + _substitute_placeholders(match.group(2))
    return arns


def _statement_constants(content: str) -> dict[str, str]:
    """Named single-statement constants, including ``() => new PolicyStatement``."""
    constants = {}
    for match in re.finditer(r"const (\w+)\s*=\s*(?:\(\)\s*=>\s*)?new iam\.PolicyStatement\(\{", content):
        open_brace = match.end() - 1
        constants[match.group(1)] = content[open_brace : _match_delimiter(content, open_brace, "{", "}")]
    return constants


def _managed_policy_statements(content: str) -> dict[str, list[str]]:
    """Named managed policies → the statement bodies they carry."""
    policies = {}
    for match in re.finditer(r"const (\w+)\s*=\s*new iam\.ManagedPolicy\(", content):
        open_paren = match.end() - 1
        block = content[open_paren : _match_delimiter(content, open_paren, "(", ")")]
        policies[match.group(1)] = _statement_bodies(block)
    return policies


def _arn_list_constants(content: str, bucket_arns: dict[str, str]) -> dict[str, list[str]]:
    """Resolve ``const allBucketArns = [...buckets].flatMap(...)`` style lists.

    The shape is asserted rather than assumed: if the flatMap stops emitting
    both the bucket ARN and its ``/*`` object form, this raises instead of
    quietly resolving to a narrower list that would still satisfy a
    bucket-level match.
    """
    lists: dict[str, list[str]] = {}
    for match in re.finditer(r"const (\w+)\s*=\s*\[", content):
        open_bracket = match.end() - 1
        end = _match_delimiter(content, open_bracket, "[", "]")
        elements = _split_top_level(content[open_bracket + 1 : end - 1])
        tail = content[end : content.find(";", end)]
        if ".flatMap" not in tail:
            continue
        props = [re.fullmatch(r"this\.(\w+)", e.strip()) for e in elements]
        if not all(props):
            continue
        assert "bucketArn}/*`" in tail and "b.bucketArn," in tail, (
            f"{match.group(1)}'s flatMap no longer emits both the bucket ARN and its /* "
            f"object form — this resolver would silently narrow the grant it expands to: {tail}"
        )
        resolved = []
        for prop in props:
            arn = bucket_arns[prop.group(1)]
            resolved += [arn, f"{arn}/*"]
        lists[match.group(1)] = resolved
    return lists


def _substitute_placeholders(text: str) -> str:
    return text.replace("${account}", ACCOUNT).replace("${region}", REGION)


def _resolve_resource(element: str, bucket_arns: dict[str, str]) -> str | None:
    """A single ``resources: [...]`` element as a concrete ARN pattern, or None."""
    element = element.strip()
    if element.startswith("`") and element.endswith("`"):
        element = element[1:-1]
    elif (element.startswith("'") and element.endswith("'")) or (element.startswith('"') and element.endswith('"')):
        return element[1:-1]
    else:
        bare = re.fullmatch(r"this\.(\w+)\.bucketArn", element)
        if bare:
            return bucket_arns.get(bare.group(1))
        return None
    for prop, arn in bucket_arns.items():
        element = element.replace(f"${{this.{prop}.bucketArn}}", arn)
    resolved = _substitute_placeholders(element)
    return None if "${" in resolved else resolved


def _iam_wildcard_matches(pattern: str, arn: str) -> bool:
    """IAM resource-pattern semantics: ``*`` spans anything, ``?`` one character."""
    regex = "".join(".*" if ch == "*" else "." if ch == "?" else re.escape(ch) for ch in pattern)
    return re.fullmatch(regex, arn) is not None


# ── Role grant collection ─────────────────────────────────────────────


def _role_statement_bodies(content: str, role_prop: str) -> tuple[list[str], list[str]]:
    """(resolved statement bodies, unresolved expressions) for one role.

    Covers all three ways this stack attaches a statement: the ``lambdaRole``
    helper's inline-statement array, an ``iam.Role``'s ``managedPolicies``, and
    ``addToPolicy`` calls. Named statement constants and statement factories are
    resolved through their definitions, so a grant that arrives as
    ``[s3ReadWrite]`` counts the same as one written out in place.
    """
    constants = _statement_constants(content)
    managed = _managed_policy_statements(content)
    bodies: list[str] = []
    unresolved: list[str] = []

    def add(expression: str) -> None:
        expression = expression.strip().removesuffix("()").strip()
        if expression.startswith("new iam.PolicyStatement("):
            bodies.extend(_statement_bodies(expression))
        elif expression in constants:
            bodies.append(constants[expression])
        elif expression in managed:
            bodies.extend(managed[expression])
        elif expression:
            unresolved.append(expression)

    helper = re.search(rf"this\.{role_prop}\s*=\s*lambdaRole\(", content)
    if helper:
        open_paren = helper.end() - 1
        args = _split_top_level(content[open_paren + 1 : _match_delimiter(content, open_paren, "(", ")") - 1])
        # lambdaRole(name, extraPolicies, inlineStatements)
        for arg in args[1:]:
            arg = arg.strip()
            if arg.startswith("[") and arg.endswith("]"):
                for element in _split_top_level(arg[1:-1]):
                    add(element)
    else:
        direct = re.search(rf"this\.{role_prop}\s*=\s*new iam\.Role\(", content)
        assert direct, f"{role_prop} is defined by neither lambdaRole() nor new iam.Role()"
        open_paren = direct.end() - 1
        props = content[open_paren : _match_delimiter(content, open_paren, "(", ")")]
        for element in _array_after(props, "managedPolicies") or []:
            add(element)

    for call in re.finditer(rf"this\.{role_prop}\.addToPolicy\(", content):
        open_paren = call.end() - 1
        add(content[open_paren + 1 : _match_delimiter(content, open_paren, "(", ")") - 1])

    assert bodies, f"no policy statements found for {role_prop}"
    return bodies, unresolved


def _reachable_patterns(content: str, role_prop: str, action: str) -> tuple[list[str], list[str]]:
    """(resource patterns usable with ``action``, elements that would not resolve)."""
    bucket_arns = _bucket_arns(content)
    arn_lists = _arn_list_constants(content, bucket_arns)
    bodies, unresolved = _role_statement_bodies(content, role_prop)

    patterns: list[str] = []
    for body in bodies:
        actions = [a.strip("'\"` ") for a in _array_after(body, "actions") or []]
        if not any(_iam_wildcard_matches(a, action) for a in actions):
            continue
        for element in _array_after(body, "resources") or []:
            spread = re.fullmatch(r"\.\.\.(\w+)", element.strip())
            if spread:
                if spread.group(1) in arn_lists:
                    patterns.extend(arn_lists[spread.group(1)])
                else:
                    unresolved.append(element.strip())
                continue
            resolved = _resolve_resource(element, bucket_arns)
            if resolved is None:
                unresolved.append(element.strip())
            else:
                patterns.append(resolved)
    return patterns, unresolved


@pytest.fixture(scope="module")
def foundation_stack_content() -> str:
    with open(FOUNDATION_STACK_PATH) as f:
        return f.read()


@pytest.fixture(scope="module")
def target_schemas_bucket_arn(foundation_stack_content) -> str:
    """The bucket ARN, resolved from the bucket's own ``bucketName``."""
    arns = _bucket_arns(foundation_stack_content)
    assert TARGET_SCHEMAS_BUCKET_PROP in arns, (
        f"could not resolve this.{TARGET_SCHEMAS_BUCKET_PROP}'s bucketName from "
        "foundation-stack.ts — the reachability assertions below would be vacuous"
    )
    return arns[TARGET_SCHEMAS_BUCKET_PROP]


@pytest.fixture(scope="module")
def target_schema_object_arn(target_schemas_bucket_arn) -> str:
    """The ARN of an object in the bucket the p33 read actually targets."""
    # The key shape create_pipeline writes; any key under the bucket would do.
    return f"{target_schemas_bucket_arn}/schemas/pipeline-1/target_schema.json"


def test_target_schemas_bucket_resolves_to_its_own_name(target_schemas_bucket_arn):
    """Guard: the bucket is named for itself, not matched by a hardcoded string.

    If the bucket is ever renamed, this is where the rename shows up, and the
    reachability assertions below re-evaluate against the new name rather than
    continuing to pass against the old one.
    """
    assert target_schemas_bucket_arn == f"arn:aws:s3:::adi-target-schemas-{ACCOUNT}-{REGION}"


@pytest.mark.parametrize("role_prop", READING_ROLES)
def test_reading_role_can_get_the_target_schema_object(foundation_stack_content, target_schema_object_arn, role_prop):
    """Requirement 2.1 — the role running the read can perform it.

    Keyed on ARN reachability rather than on the wildcard's spelling: any grant
    that covers the object passes, and no grant that fails to cover it does.
    """
    patterns, _ = _reachable_patterns(foundation_stack_content, role_prop, GET_OBJECT)

    covering = [p for p in patterns if _iam_wildcard_matches(p, target_schema_object_arn)]
    assert covering, (
        f"{role_prop} has no s3:GetObject grant reaching {target_schema_object_arn} — "
        f"p33's target-schema read will fail with AccessDenied, which no test layer "
        f"below CDK can see. Resource patterns reachable with s3:GetObject: {sorted(set(patterns))}"
    )


@pytest.mark.parametrize("role_prop", READING_ROLES)
def test_object_level_grant_not_only_bucket_level(
    foundation_stack_content, target_schemas_bucket_arn, target_schema_object_arn, role_prop
):
    """Requirement 2.8 — a bucket-only grant would fail the read at runtime.

    ``s3:GetObject`` is authorized against the object ARN. A grant listing the
    bucket ARN alone reads as coverage but denies the GetObject, producing the
    infrastructure failure 2.8 requires be propagated rather than degraded —
    from a policy that looks correct.
    """
    patterns, _ = _reachable_patterns(foundation_stack_content, role_prop, GET_OBJECT)

    bucket_only = [p for p in patterns if _iam_wildcard_matches(p, target_schemas_bucket_arn)]
    object_level = [p for p in patterns if _iam_wildcard_matches(p, target_schema_object_arn)]
    assert object_level, (
        f"{role_prop} reaches the bucket ARN ({bucket_only}) but no pattern covers an "
        f"object inside it — s3:GetObject needs the object form"
    )


@pytest.mark.parametrize("role_prop", READING_ROLES)
def test_every_resource_the_parser_examined_resolved(foundation_stack_content, role_prop):
    """Guard: nothing was skipped on the way to the assertions above.

    An unresolvable statement expression or resource element would make the
    reachability check blind in exactly the direction that produces a false
    pass, so it fails here instead of being dropped.
    """
    _, unresolved = _reachable_patterns(foundation_stack_content, role_prop, GET_OBJECT)

    assert not unresolved, (
        f"foundation-stack.ts grew IAM constructs this test cannot read for {role_prop}, so its "
        f"reachability check is no longer complete: {sorted(set(unresolved))}"
    )


def test_the_runtime_role_read_is_covered_by_the_synthesized_template_test():
    """The container read moved to a synthesized-template test, and that test exists.

    Before R-02 this file also checked ``agentCoreRuntimeRole`` here. A pointer
    that silently dangles would drop that coverage, so the twin is pinned by
    name and by the two roles it must name.
    """
    twin = os.path.join(
        os.path.dirname(__file__), "..", "..", "..", "deploy-cdk", "test", "agentcore-runtime-roles.test.ts"
    )
    assert os.path.exists(twin), f"the synthesized-template twin {twin} is missing"
    with open(twin) as f:
        content = f.read()
    for role in ("CreateCodeRuntimeRole", "TestCodeRuntimeRole"):
        assert role in content, f"{twin} no longer checks {role}'s target-schema read"
    assert "adi-target-schemas" in content, f"{twin} no longer checks the target-schemas bucket"
