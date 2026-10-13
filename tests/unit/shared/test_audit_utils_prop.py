# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property-based tests for ``extract_request_context`` in ``shared/audit_utils.py``.

Complements the example-based cases in ``test_audit_utils.py::TestExtractRequestContext``
(37 tests, which pin the four enumerated input shapes plus five specific
``AttributeError`` shapes). This file generalises them: the example tests say
"these nine shapes are safe", the properties below say "no shape is unsafe".

Why a property test earns its keep here rather than a tenth example. The resolver
sits on a fire-and-forget audit path with **10 call sites**
(`handlers/approvals.py`, `handlers/pipelines_crud.py`, `handlers/pipelines_runs.py`),
and R1.3 makes non-raising a *requirement*, not a quality: it "MUST NOT be able to
fail a request". API Gateway is also not the only producer of its input — 384 audit
rows written in 2026-08 arrived with no API Gateway event at all
(observation-notes §4), and `test_audit_writes.py:392` records the direct-invocation
shape `extract_request_context({})`. A guarantee stated over "any event shape" is
therefore the honest form of the requirement, and enumerated shapes can only
approximate it.

Properties established, and what each serves:

* **A4.1 — total.** For any input value, the call returns; it never raises. (R1.3)
* **A4.2 — the actor is always a non-empty ``str``.** The terminal ``"unknown"``
  is what makes this hold. (R1.3)
* **A4.3 — the return shape is exactly ``{"actor", "ip_address"}``, both ``str``.**
  A contract, not an implementation detail: every one of the 10 callers splats the
  result (``write_audit_entry(..., **ctx)``), and ``write_audit_entry`` is out of
  scope for p47 (design.md §2.1), so a third key is a ``TypeError`` at each site
  rather than a widened return. (R1.3)
* **A4.4 — no client-supplied value can become the actor or the IP.** Generated
  events carry a forged principal in ``body``, ``headers``, ``multiValueHeaders``,
  ``queryStringParameters`` and ``pathParameters`` simultaneously; the resolved
  actor is only ever a ``requestContext`` value or the fallback. This is R2's
  defect ("take the approver from the request body") pinned one layer down, where
  it would look like an identity resolver. (R1.5)
* **A4.5 — ``ip_address`` is always a ``str``, and mirrors ``identity["sourceIp"]``
  when that value is a non-empty string.** Deliberately conditional: task A3
  tightened a non-``str`` ``sourceIp`` to ``""`` where the pre-A3 code returned the
  raw value, matching the declared ``dict[str, str]``. An unconditional
  ``ip_address == identity["sourceIp"]`` would falsify on a synthetic non-string
  and would be asserting the old contract. (R1.4)

Not re-asserted here, because ``TestExtractRequestContext`` already pins them with
fixtures whose ``sub`` and ``cognito:username`` hold **distinct** values (the live
pool makes them byte-identical, so a naive fixture asserts nothing about ordering):
R1.1's ``sub`` → ``cognito:username`` → ``email`` preference and R1.2's
claims-beat-identity ordering. Generating those would duplicate existing coverage,
not extend it.

**The generator was probed, not assumed.** A property test whose strategy never
reaches the branch under test passes vacuously and reads exactly like one that
does. The first draft's slot strategies resolved an actor **6 times in 1,000
draws** and produced a resolvable ``sourceIp`` 4 times — so A4.2 was being carried
almost entirely by the ``"unknown"`` fallback and A4.5's conditional was
effectively untested. After the weighting below, a 1,000-draw probe reports: 156
resolved actors, 141 dict-shaped claims blocks (``sub`` 49 / ``cognito:username``
34 / ``email`` 28), 70 resolvable ``identity`` reads, 97 resolvable ``sourceIp``
values, and on the hostile side 96 non-dict ``requestContext``, 64 non-dict
``authorizer``, 52 non-dict ``claims``, 66 non-dict ``identity``, 101 non-dict
events. Both halves are exercised.

Source is not modified by this file. If a property here falsifies the resolver, the
counterexample is a finding to report, not a licence to edit ``audit_utils.py``.

**Validates: Requirements R1.3, R1.4, R1.5**
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from typing import Any

from hypothesis import given, settings
from hypothesis import strategies as st

# Make shared modules importable without relying on the Lambda layer mount, and
# set the env before import so ``require_env()`` falls back to its default.
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "local")


def _load_real_audit_utils():
    """Load ``shared/audit_utils.py`` from disk, ignoring ``sys.modules``.

    A plain ``from audit_utils import extract_request_context`` here is
    **full-suite-only broken**, and silently so. Four pre-existing modules —
    ``tests/unit/lambdas/dashboard_api/test_retry_run_with_step.py:26-29``,
    ``test_field_context_roundtrip_prop.py:136-139``,
    ``test_pipelines_manual_mode_prop.py:36`` and
    ``test_edit_pipeline_approval_config_boolean.py:69`` — each install
    ``sys.modules["audit_utils"] = MagicMock(extract_request_context=
    MagicMock(return_value={}))`` at *module* scope, to keep the dashboard
    handlers they import from writing audit rows. pytest imports every test
    module during collection, before any test runs, so whether this file binds
    the real resolver or that mock is decided by collection order alone.

    Bound to the mock, four of the five properties below fail with
    ``KeyError: 'actor'`` / ``set() == {'actor', 'ip_address'}`` on *every*
    input, and A4.1 (never raises) passes vacuously — a MagicMock does not
    raise either. Reproducer, two files, no full suite needed:

        pytest tests/unit/lambdas/dashboard_api/test_retry_run_with_step.py \\
               tests/unit/shared/test_audit_utils_prop.py

    The sibling example-based file is immune because its
    ``_fresh_audit_utils()`` (``test_audit_utils.py:22-29``) deletes the entry
    and re-imports inside each test. That idiom is not usable here: a
    ``@given`` body runs up to 400 times, and ``audit_utils`` builds a boto3
    resource at import time.

    So the module is loaded once, from its file path, into a private module
    object that is **never published to** ``sys.modules``. That is deliberately
    stronger than deleting the mock: this file then neither trusts nor mutates
    shared import state, so it cannot be broken by a future stub and cannot
    break one. ``audit_utils``' own imports still resolve normally through
    ``sys.path`` (stubbed there or not — either is fine, none of them
    participate in actor resolution).
    """
    path = os.path.join(_SHARED_DIR, "audit_utils.py")
    spec = importlib.util.spec_from_file_location("_audit_utils_under_test", path)
    assert spec and spec.loader, f"could not load a module spec from {path}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_audit_utils = _load_real_audit_utils()
extract_request_context = _audit_utils.extract_request_context

# Non-vacuity guard. Every property below reads keys off the returned dict, and
# a mock satisfies "does not raise" while asserting nothing — the exact failure
# this loader exists to prevent. Fail at import with a legible message rather
# than reporting five confusing counterexamples.
assert type(extract_request_context).__module__ != "unittest.mock", (
    "extract_request_context resolved to a mock, not shared/audit_utils.py — "
    "the private module load above was defeated; these properties would be vacuous"
)
assert extract_request_context({}) == {"actor": "unknown", "ip_address": ""}, (
    "the loaded resolver does not answer the documented empty-event shape; it is probably not shared/audit_utils.py"
)

# ---------------------------------------------------------------------------
# Strategies
#
# A purely random dict essentially never contains the key ``"requestContext"``,
# so an unguided generator would exercise only the "not an API Gateway event"
# branch and never reach claim resolution at all. The generators below therefore
# mix two families: arbitrary junk, and request-context-shaped events whose four
# nested slots (``requestContext`` / ``authorizer`` / ``claims`` / ``identity``)
# are each *independently* well-formed or hostile — which is the combination the
# five AttributeError shapes task A2 pinned were individual instances of.
# ---------------------------------------------------------------------------

_SCALARS = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.floats(allow_nan=True, allow_infinity=True),
    st.text(max_size=12),
    st.binary(max_size=8),
)

# Non-string keys are deliberate: nothing guarantees a directly-invoked payload
# uses string keys, and ``dict.get`` on a mixed-key dict is only safe by accident
# if never exercised.
_ANY_KEYS = st.one_of(st.text(max_size=6), st.integers(), st.booleans(), st.none())

# Deeply nested arbitrary values, for the "any event shape" half of A4.1.
_ARBITRARY = st.recursive(
    _SCALARS,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.tuples(children, children),
        st.dictionaries(_ANY_KEYS, children, max_size=3),
    ),
    max_leaves=10,
)

# Values with no ``.get`` — what goes into a slot the resolver expects to be a
# dict. ``None`` is included because it is the shape an absent-but-present-as-null
# JSON field produces.
_NON_DICT = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(),
    st.text(max_size=10),
    st.binary(max_size=6),
    st.lists(_SCALARS, max_size=3),
)

# The three keys R1.1 reads, plus near-misses that must NOT resolve: casing
# variants, an underscore instead of the literal colon, and `username` / `scope`,
# which are access-token claims absent from the ID token this API receives
# (observation-notes §2, §6).
_CLAIM_KEYS = st.sampled_from(
    [
        "sub",
        "cognito:username",
        "email",
        "Sub",
        "SUB",
        "cognito_username",
        "cognito:Username",
        "username",
        "iss",
        "aud",
        "token_use",
        "email_verified",
        "principalId",
    ]
)

_IDENTITY_KEYS = st.sampled_from(
    [
        "caller",
        "cognitoIdentityId",
        "sourceIp",
        "Caller",
        "sourceIP",
        "userArn",
        "accountId",
    ]
)

# Claim and identity values are not restricted to strings. API Gateway
# stringifies claims, but that is documented platform behaviour rather than
# something observed on this deployment (observation-notes §3), and the direct
# invocation path is bound by nothing at all. ``st.text()`` includes ``""``,
# which is the "present but empty" case R1.2's fallback turns on.
_SLOT_VALUES = st.one_of(
    st.text(max_size=12),
    st.none(),
    st.integers(),
    st.booleans(),
    st.lists(st.text(max_size=4), max_size=2),
    st.dictionaries(st.text(max_size=3), st.text(max_size=3), max_size=2),
)

_CLAIMS_DICTS = st.dictionaries(_CLAIM_KEYS, _SLOT_VALUES, max_size=5)
_IDENTITY_DICTS = st.dictionaries(_IDENTITY_KEYS, _SLOT_VALUES, max_size=4)

_NON_EMPTY_TEXT = st.text(min_size=1, max_size=12)
_SOURCE_IPS = st.sampled_from(["198.51.100.7", "203.0.113.42", "10.0.0.5", "2001:db8::1"])


# A slot drawn purely from the dictionaries above resolves almost never: with 13
# candidate keys, six candidate value types and hypothesis' bias toward small
# collections, a measured 1,000-draw probe hit a *resolving* claims dict 12 times
# and a resolved actor 6 times. Properties proven at that rate are proven by the
# ``"unknown"`` fallback alone, and A4.5's conditional would fire ~4 times in 1,000.
# So each slot strategy explicitly mixes three families — resolving, arbitrary,
# non-dict — rather than hoping the arbitrary family stumbles into a resolution.


@st.composite
def _claims_slot(draw) -> Any:
    kind = draw(st.sampled_from(["resolving", "resolving", "arbitrary", "non-dict"]))
    if kind == "non-dict":
        return draw(_NON_DICT)
    claims: dict[Any, Any] = dict(draw(_CLAIMS_DICTS))
    if kind == "resolving":
        # Pin at least one R1.1 key to a non-empty string, so the claims branch is
        # actually taken. Which one is drawn, so no single key dominates.
        claims[draw(st.sampled_from(["sub", "cognito:username", "email"]))] = draw(_NON_EMPTY_TEXT)
    return claims


@st.composite
def _identity_slot(draw) -> Any:
    kind = draw(st.sampled_from(["resolving", "arbitrary", "arbitrary", "non-dict"]))
    if kind == "non-dict":
        return draw(_NON_DICT)
    identity: dict[Any, Any] = dict(draw(_IDENTITY_DICTS))
    if kind == "resolving":
        identity[draw(st.sampled_from(["caller", "cognitoIdentityId"]))] = draw(_NON_EMPTY_TEXT)
    # Roughly half real IP, a quarter present-but-malformed, a quarter absent —
    # the three cases A4.5 partitions on. Left to ``_SLOT_VALUES`` alone, the
    # non-empty-string branch fired 42 times in 1,000 draws, which is too thin to
    # call the conditional tested.
    if draw(st.booleans()):
        identity["sourceIp"] = draw(_SOURCE_IPS)
    elif draw(st.booleans()):
        identity["sourceIp"] = draw(_SLOT_VALUES)
    return identity


@st.composite
def _events(draw) -> Any:
    """Draw an event: arbitrary junk, or a request-context-shaped dict.

    Each nested slot is independently either plausible or hostile, so the four
    dict-expecting reads in the resolver get exercised against non-dicts in every
    combination rather than one at a time.
    """
    # One in four draws is unstructured — a scalar, a list, a deeply nested dict,
    # or ``None``. These reach the ``isinstance(event, dict)`` guard.
    if draw(st.integers(min_value=0, max_value=3)) == 0:
        return draw(_ARBITRARY)

    authorizer: Any
    if draw(st.integers(min_value=0, max_value=4)):
        authorizer = {"claims": draw(_claims_slot())}
        if draw(st.booleans()):
            # Whether the live authorizer carries siblings beside ``claims``
            # (e.g. ``principalId``) was not verified — observation-notes §3 —
            # so both shapes are generated rather than assumed.
            authorizer["principalId"] = draw(st.text(max_size=8))
    else:
        authorizer = draw(_NON_DICT)

    request_context: Any = {}
    if draw(st.integers(min_value=0, max_value=4)):
        request_context["authorizer"] = authorizer
    if draw(st.integers(min_value=0, max_value=4)):
        request_context["identity"] = draw(_identity_slot())
    if draw(st.booleans()):
        request_context["requestId"] = draw(st.text(max_size=8))
    if draw(st.integers(min_value=0, max_value=6)) == 0:
        request_context = draw(_NON_DICT)

    # Sibling top-level keys first, so a drawn key can never displace the
    # ``requestContext`` this event is built around.
    event: dict[Any, Any] = dict(draw(st.dictionaries(_ANY_KEYS, _ARBITRARY, max_size=3)))
    if draw(st.integers(min_value=0, max_value=9)):
        event["requestContext"] = request_context
    return event


# Two disjoint namespaces, so A4.4 can tell where a resolved value came from.
# Neither generator can produce the other's values, which is what makes
# "the actor is never the forged value" a real assertion rather than a
# coincidence of the alphabet.
_CLIENT_PRINCIPAL = st.builds(
    lambda n: f"client-supplied-principal-{n}@example.invalid",
    st.integers(min_value=0, max_value=10_000),
)
_SERVER_PRINCIPAL = st.builds(
    lambda n: f"server-principal-{n}",
    st.integers(min_value=0, max_value=10_000),
)
_SERVER_PREFIX = "server-principal-"


@st.composite
def _client_controlled_events(draw) -> tuple[dict[str, Any], str]:
    """Draw an event whose every client-supplied surface carries a forged principal.

    Returns ``(event, forged_value)``. ``requestContext`` is drawn from a disjoint
    namespace, so any appearance of ``forged_value`` in the result means a
    client-controlled field reached the actor.
    """
    forged = draw(_CLIENT_PRINCIPAL)
    server = draw(_SERVER_PRINCIPAL)

    request_context: Any = draw(
        st.sampled_from(
            [
                {},
                {"identity": {}},
                {"authorizer": {"claims": {}}, "identity": {}},
                {"authorizer": {"claims": {"sub": server}}, "identity": {}},
                {"authorizer": {"claims": {"email": server}}},
                {"identity": {"caller": server}},
                {"identity": {"cognitoIdentityId": server}},
                {"authorizer": None, "identity": None},
                {"authorizer": "not-a-dict", "identity": "not-a-dict"},
            ]
        )
    )
    if draw(st.booleans()):
        # ``sourceIp`` is read from the same dict as the actor, so a forged actor
        # that leaked through would plausibly leak here too.
        if isinstance(request_context, dict) and isinstance(request_context.get("identity"), dict):
            request_context["identity"]["sourceIp"] = draw(st.sampled_from(["198.51.100.7", "2001:db8::1"]))

    body: Any = draw(
        st.sampled_from(
            [
                json.dumps({"user": forged, "approved_by": forged, "actor": forged}),
                {"user": forged, "approved_by": forged},
                forged,
            ]
        )
    )

    return (
        {
            "body": body,
            "headers": {
                "X-Actor": forged,
                "Authorization": f"Bearer {forged}",
                "x-amzn-oidc-identity": forged,
                "x-amzn-oidc-data": forged,
                "User-Agent": forged,
            },
            "multiValueHeaders": {"X-Actor": [forged], "Authorization": [f"Bearer {forged}"]},
            "queryStringParameters": {"user": forged, "actor": forged, "approved_by": forged},
            "pathParameters": {"user": forged},
            "requestContext": request_context,
        },
        forged,
    )


def _assert_return_contract(ctx: Any) -> None:
    """Assert the two-key ``dict[str, str]`` contract every caller splats."""
    assert isinstance(ctx, dict)
    assert set(ctx) == {"actor", "ip_address"}
    assert isinstance(ctx["actor"], str)
    assert isinstance(ctx["ip_address"], str)


# ---------------------------------------------------------------------------
# Property A4.1 — the resolver is total
# ---------------------------------------------------------------------------


class TestResolverIsTotal:
    """For any input value, ``extract_request_context`` returns without raising.

    **Validates: Requirements R1.3**
    """

    @given(event=_events())
    @settings(max_examples=300)
    def test_never_raises_for_any_event_shape(self, event: Any):
        # No try/except: an exception escaping here IS the failure, and letting
        # hypothesis report it preserves the shrunk counterexample. Wrapping it in
        # ``pytest.fail`` would discard the traceback that identifies which read
        # broke.
        extract_request_context(event)

    @given(event=_ARBITRARY)
    @settings(max_examples=300)
    def test_never_raises_for_arbitrary_non_event_input(self, event: Any):
        """Inputs that are not API Gateway events at all.

        The direct-invocation path reaches this resolver — 384 audit rows in
        2026-08 were written with no API Gateway event (observation-notes §4) —
        so "not an event" is a live shape, not a synthetic one.
        """
        extract_request_context(event)


# ---------------------------------------------------------------------------
# Property A4.2 — the actor is always a non-empty string
# ---------------------------------------------------------------------------


class TestActorIsAlwaysNonEmptyString:
    """The resolved actor is always a non-empty ``str``.

    ``"unknown"`` as the terminal fallback is what makes this total. An empty
    actor would be worse than ``"unknown"``: it is written into an append-only
    table with a one-year TTL, and a blank identity reads as a missing column
    rather than as an unresolved principal.

    **Validates: Requirements R1.3**
    """

    @given(event=_events())
    @settings(max_examples=400)
    def test_actor_is_non_empty_string(self, event: Any):
        actor = extract_request_context(event)["actor"]
        assert isinstance(actor, str)
        assert actor != ""


# ---------------------------------------------------------------------------
# Property A4.3 — the return shape is exactly two string values
# ---------------------------------------------------------------------------


class TestReturnShapeIsExactlyTwoStringKeys:
    """The return is exactly ``{"actor", "ip_address"}``, both ``str``.

    All 10 call sites splat the result into ``write_audit_entry(..., **ctx)``, and
    ``write_audit_entry`` is explicitly out of p47's scope (design.md §2.1). A
    third key is therefore a ``TypeError`` at ten places, which makes the shape a
    contract rather than a return value.

    **Validates: Requirements R1.3**
    """

    @given(event=_events())
    @settings(max_examples=400)
    def test_shape_holds_for_any_event(self, event: Any):
        _assert_return_contract(extract_request_context(event))


# ---------------------------------------------------------------------------
# Property A4.4 — no client-supplied value becomes the actor
# ---------------------------------------------------------------------------


class TestClientSuppliedValuesNeverResolve:
    """A forged principal in any client-controlled surface never reaches the actor.

    R1.5 in its general form. The example-based
    ``test_actor_never_read_from_body_or_headers`` pins one such event; this holds
    over a generated family, and additionally asserts the *positive* half — that a
    resolved actor is either a ``requestContext`` value or the fallback, with no
    third origin.

    **Validates: Requirements R1.5**
    """

    @given(case=_client_controlled_events())
    @settings(max_examples=250)
    def test_forged_principal_never_becomes_the_actor(self, case: tuple[dict[str, Any], str]):
        event, forged = case
        ctx = extract_request_context(event)

        _assert_return_contract(ctx)
        # Substring rather than equality: a resolver that concatenated or
        # templated a client value into the actor would pass an equality check.
        assert forged not in ctx["actor"]
        assert forged not in ctx["ip_address"]

    @given(case=_client_controlled_events())
    @settings(max_examples=250)
    def test_actor_origin_is_request_context_or_the_fallback(self, case: tuple[dict[str, Any], str]):
        """The actor has exactly two possible origins, and neither is the client.

        ``requestContext`` values are drawn from a namespace the client-controlled
        generator cannot produce, so this partition is decidable from the value
        alone.
        """
        event, _forged = case
        actor = extract_request_context(event)["actor"]
        assert actor == "unknown" or actor.startswith(_SERVER_PREFIX)


# ---------------------------------------------------------------------------
# Property A4.5 — ip_address is a string, and mirrors identity.sourceIp
# ---------------------------------------------------------------------------


class TestIpAddressMirrorsSourceIp:
    """``ip_address`` is always a ``str``, and equals ``identity["sourceIp"]`` when
    that value is a non-empty string.

    The condition is load-bearing and is not defensive phrasing. Task A3 routed
    ``sourceIp`` through the same non-empty-string filter as the actor keys, so a
    non-``str`` ``sourceIp`` now yields ``""`` where the pre-A3 code returned the
    raw value. That matches the declared ``dict[str, str]`` and is a deliberate
    tightening; an unconditional ``ip_address == identity["sourceIp"]`` would
    falsify on a synthetic non-string and would be pinning the superseded
    contract.

    ``ip_address`` is also read independently of the actor: an unresolvable actor
    does not suppress the IP. That combination is observable in production — 52
    audit rows carry a real IP alongside ``actor == "unknown"``
    (observation-notes §4).

    **Validates: Requirements R1.4**
    """

    @given(event=_events())
    @settings(max_examples=400)
    def test_ip_mirrors_source_ip_when_it_is_a_non_empty_string(self, event: Any):
        ctx = extract_request_context(event)
        assert isinstance(ctx["ip_address"], str)

        request_context = event.get("requestContext") if isinstance(event, dict) else None
        identity = request_context.get("identity") if isinstance(request_context, dict) else None
        source_ip = identity.get("sourceIp") if isinstance(identity, dict) else None

        if isinstance(source_ip, str) and source_ip:
            # Holds regardless of what the actor resolved to — the two values come
            # from different branches of the same event.
            assert ctx["ip_address"] == source_ip
        else:
            # Everything else — absent, empty, or not a string — is ``""``.
            assert ctx["ip_address"] == ""
