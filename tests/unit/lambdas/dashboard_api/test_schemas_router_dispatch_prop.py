# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization of ``schemas_router``'s dispatch, and Property 1.

# Feature: p35-schema-surfaces, Property 1: Exactly one branch of schemas_router handles any request, and it is the intended one

Every expectation here is derived from design §4.5's dispatch table and from
``requirements.md`` — **never** from running the current ``schemas_router``.
Current behaviour contains the defect: the third branch (``router.py:329-332``)
is ``if http_method == "GET": return get_schemas()``, unconditional on the path,
so it answers *every* ``GET`` under ``/api/schemas/…`` with the whole listing.
That is why the detail modal always describes ``[0]`` (``use-schemas.ts:38-45``).
A test written from current output would pin the defect instead of catching it.

**Expected state when this file lands: RED.** Four routes do not exist yet —
per-id ``GET`` (R1.1), per-id ``DELETE`` (R3.1), the field-context ``PUT``
(R2.3) and the display-label ``PUT`` (R4.9) — and the collection ``POST``
answers the trailing ``404`` where design §4.5 requires ``405``. Task 4.2
(design B3) is what turns those assertions green. Per design §5 risk 9, that red
window is the expected state and not a regression: a reviewer who sees it and
reverts has undone the only thing that would have caught the defect.

Assertions that are **green today** and must stay green: the collection ``GET``
(branch 1), ``POST /api/schemas/upload`` (branch 2), and the trailing ``404``
for every shape that reaches it without passing through today's permissive
``GET`` branch — ``PUT``/``PATCH``/``HEAD``/``POST`` on an id, for instance.

Expressed against ``schemas_router`` rather than against the handler functions,
per R8.1. ``schemas_router`` has no symbol-level test coverage today: the three
router contract tests reach it through ``("/api/schemas", "GET")`` only, so
neither the per-id case nor any non-``GET`` method is exercised anywhere.

No shared fixture is needed and no AWS call is made: every handler
``schemas_router`` can reach is replaced by a recording sentinel before the
router is called, so what is exercised is dispatch alone. There is no
``moto.mock_aws()`` and no boto3 client mock here
(``tests/unit/test_no_duplicate_mocking_prop.py``), and this file does **not**
set ``AWS_REGION`` — the unit layer pins it by assignment in
``tests/unit/conftest.py`` (``LGR-3e45e65-1``), and ``get_schemas`` reads it at
call time (``schemas.py:23``).

**Validates: Requirements 1.1, 1.2, 2.3, 3.1, 4.9, 8.1**
"""

import json
import os
import string
import sys
from contextlib import ExitStack
from unittest.mock import patch

from hypothesis import given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Path setup — must come before importing router
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

# Table names only. AWS_REGION and AWS_DEFAULT_REGION are deliberately absent —
# see the module docstring and LGR-3e45e65-1.
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "ERRORS_TABLE": "agentic_data_ingestion_errors",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

sys.modules.pop("lambda_function", None)

import router

# ---------------------------------------------------------------------------
# Design §4.5's dispatch table, transcribed as an oracle
# ---------------------------------------------------------------------------
# The six handlers the six branches call. Four of them do not exist in
# handlers/schemas.py yet, which is why every patch below passes create=True:
# the sentinel has to be installable before the route that calls it exists, or
# this characterization could not be written ahead of task 4.2.
HANDLER_NAMES = (
    "get_schemas",
    "upload_schema",
    "get_schema",
    "delete_schema",
    "update_field_context",
    "set_display_label",
)

# Acceptance predicate per branch, in design §4.5's dispatch order. "Accepts"
# means "handles the request rather than falling through", so branch 1 accepts
# every method on the collection — it answers 405 for the non-GET ones.
BRANCH_PREDICATES = (
    (1, "collection", lambda method, parts: not parts),
    (2, "upload", lambda method, parts: parts == ["upload"] and method == "POST"),
    (
        3,
        "field context",
        lambda method, parts: len(parts) == 4 and parts[1] == "fields" and parts[3] == "context" and method == "PUT",
    ),
    (4, "display label", lambda method, parts: len(parts) == 2 and parts[1] == "display-label" and method == "PUT"),
    (5, "per id", lambda method, parts: len(parts) == 1 and method == "GET"),
    (6, "delete", lambda method, parts: len(parts) == 1 and method == "DELETE"),
)


def split_subpath(subpath: str) -> list[str]:
    """Segment a subpath the way design §4.5's router does."""
    return [p for p in subpath.strip("/").split("/") if p]


def accepting_branches(method: str, parts: list[str]) -> list[tuple[int, str]]:
    """Every branch of design §4.5's table whose predicate accepts this shape."""
    return [(number, label) for number, label, predicate in BRANCH_PREDICATES if predicate(method, parts)]


def expected_dispatch(method: str, parts: list[str]):
    """Design §4.5's table as a function.

    Returns ``("handler", name, path_args)`` — where ``path_args`` is the tuple
    of path segments the branch passes to its handler — or ``("status", code)``
    for the two shapes the table answers without a handler.
    """
    # 1. /api/schemas                              GET     — collection listing
    #    A POST to the collection is a method error, not a missing endpoint, so
    #    405 rather than the trailing 404 (design §4.5).
    if not parts:
        return ("handler", "get_schemas", ()) if method == "GET" else ("status", 405)

    # 2. /api/schemas/upload                       POST    — literal, precedes {id}
    if parts == ["upload"] and method == "POST":
        return ("handler", "upload_schema", ())

    # 3. /api/schemas/{id}/fields/{name}/context    PUT     — R2.3
    if len(parts) == 4 and parts[1] == "fields" and parts[3] == "context" and method == "PUT":
        return ("handler", "update_field_context", (parts[0], parts[2]))

    # 4. /api/schemas/{id}/display-label            PUT     — R4.9, D2's rename
    if len(parts) == 2 and parts[1] == "display-label" and method == "PUT":
        return ("handler", "set_display_label", (parts[0],))

    # 5. /api/schemas/{id}                          GET     — R1.1
    if len(parts) == 1 and method == "GET":
        return ("handler", "get_schema", (parts[0],))

    # 6. /api/schemas/{id}                          DELETE  — R3.1
    if len(parts) == 1 and method == "DELETE":
        return ("handler", "delete_schema", (parts[0],))

    return ("status", 404)


# ---------------------------------------------------------------------------
# Observation — sentinels in place of every reachable handler
# ---------------------------------------------------------------------------
def _recorder(name: str, calls: list):
    """A stand-in handler that records the path segments it was handed."""

    def handler(*args, **kwargs):
        path_args = tuple(a for a in args if isinstance(a, str))
        calls.append((name, path_args))
        return {"statusCode": 200, "body": json.dumps({"handler": name})}

    return handler


def observe(method: str, subpath: str) -> tuple[list, dict]:
    """Call ``schemas_router`` with every handler replaced by a sentinel."""
    calls: list = []
    event = {
        "httpMethod": method,
        "path": f"/api/schemas/{subpath}" if subpath else "/api/schemas",
        "headers": {},
        "queryStringParameters": None,
        "body": None,
    }
    with ExitStack() as stack:
        for name in HANDLER_NAMES:
            stack.enter_context(patch.object(router, name, _recorder(name, calls), create=True))
        response = router.schemas_router(event, subpath)
    return calls, response


def dispatch_failures(method: str, subpath: str) -> list[str]:
    """Every way one ``(method, subpath)`` departs from design §4.5's table.

    Returns readable strings rather than asserting, so the matrix test below can
    report the whole red/green split in one message instead of one row at a
    time.
    """
    parts = split_subpath(subpath)
    expected = expected_dispatch(method, parts)
    accepted = accepting_branches(method, parts)
    calls, response = observe(method, subpath)
    status = response.get("statusCode")
    shape = f"{method} /api/schemas/{subpath}" if subpath else f"{method} /api/schemas"

    problems: list[str] = []

    # No two branches accept the same (method, subpath).
    if len(accepted) > 1:
        overlap = ", ".join(f"branch {number} ({label})" for number, label in accepted)
        problems.append(f"{shape}: {len(accepted)} branches accept this shape — {overlap}")

    # The table lists a handler for this shape, or it does not; those are the
    # only two cases, and each has exactly one right answer.
    if expected[0] == "handler":
        want = (expected[1], expected[2])
        if calls != [want]:
            got = ", ".join(f"{name}{args}" for name, args in calls) or "no handler call"
            problems.append(f"{shape}: expected exactly one call to {want[0]}{want[1]} — got {got} (status {status})")
    else:
        want_status = expected[1]
        if calls:
            got = ", ".join(f"{name}{args}" for name, args in calls)
            problems.append(f"{shape}: expected no handler call and status {want_status} — got {got} (status {status})")
        elif status != want_status:
            problems.append(f"{shape}: expected status {want_status} with no handler call — got status {status}")

    return problems


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------
METHODS = ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD")

# The literals matter as *id candidates*, not only in their own position.
# GET /api/schemas/upload is the shape that tells a correct ordering from a
# lucky one: it must reach the per-id branch and 404 out of get_schema("upload"),
# which is right rather than fortunate because "upload" is not an id any source
# can produce (R1.2).
LITERAL_SEGMENTS = ("upload", "fields", "context", "display-label")

# Ids in the shapes the three sources actually produce (schemas.py:47, :85, :199),
# including the live two-suffix uploaded id from pipelines_crud.py:822-830.
REALISTIC_IDS = (
    "uploaded_schema_1785844048926",
    "uploaded_schema_1785844048926_20260804_114729",
    "glue_agentic_data_ingestion_testingbucket_testingbucketns_testingbuckettn",
    "s3_table_testingbucket_testingbucketns_testingbuckettn",
    "schema_1785844048926_20260804_114729",
    "email",
)

FIELD_NAMES = ("email", "first_name", "order_id", "customer_id")

free_form_ids = st.text(alphabet=string.ascii_lowercase + string.digits + "_-.", min_size=1, max_size=14)

# Every segment position draws from the same alphabet, literals included, so a
# literal can land where an id belongs and vice versa.
path_segments = st.one_of(
    st.sampled_from(LITERAL_SEGMENTS),
    st.sampled_from(REALISTIC_IDS),
    free_form_ids,
)
field_segments = st.one_of(st.sampled_from(FIELD_NAMES), path_segments)

# Unstructured half: 0–5 segments, uniform. Segments are non-empty and contain
# no "/", which is what route_request can hand this router — it builds subpath
# from path.strip("/").split("/")[2:] (router.py:621-628).
#
# On its own this half is not enough. Measured over 200 examples it reached
# branches 1, 5 and 6 and the trailing 404, and reached branches 2, 3 and 4
# **zero times**: the four- and two-segment shapes need three specific literals
# in three specific positions, which uniform sampling does not find. A property
# that never generates the shape it is about cannot fail on it, so the
# structured half below is what makes this test able to detect a
# mis-ordered router at all.
unstructured_subpaths = st.lists(path_segments, min_size=0, max_size=5).map("/".join)

# Structured half: each of design §4.5's six shapes, and the near misses that
# must fall through — a fifth segment after context, a third after
# display-label, a truncated fields path, an id under the upload literal.
structured_subpaths = st.one_of(
    st.just(""),  # the collection
    st.just("upload"),  # branch 2's literal
    path_segments,  # one segment — branches 5 and 6
    st.tuples(path_segments, field_segments).map(lambda t: f"{t[0]}/fields/{t[1]}/context"),  # branch 3
    path_segments.map(lambda i: f"{i}/display-label"),  # branch 4
    st.tuples(path_segments, field_segments).map(lambda t: f"{t[0]}/fields/{t[1]}"),  # near miss
    st.tuples(path_segments, field_segments, path_segments).map(
        lambda t: f"{t[0]}/fields/{t[1]}/context/{t[2]}"
    ),  # near miss
    st.tuples(path_segments, path_segments).map(lambda t: f"{t[0]}/display-label/{t[1]}"),  # near miss
    st.tuples(path_segments, path_segments).map(lambda t: f"upload/{t[0]}/{t[1]}"),  # near miss
    st.tuples(path_segments, path_segments).map(lambda t: f"{t[0]}/{t[1]}"),  # arbitrary two segments
)

subpaths = st.one_of(structured_subpaths, unstructured_subpaths)

# Method and subpath are drawn together, not independently. Independent draws
# put a listed shape and its listed method in the same example only rarely —
# branch 3 needs four segments in a fixed pattern *and* PUT, which is ~1/6 of a
# ~1/10 slice, and it was reached zero times in 200 measured examples. Half the
# examples below are coherent (method, subpath) pairs from design §4.5's table,
# so every branch is exercised; the other half is the free cross product of all
# six methods with any subpath, which is what covers the shapes the table does
# not list and every near miss.
table_requests = st.one_of(
    st.tuples(st.just("GET"), st.just("")),  # branch 1
    st.tuples(st.just("POST"), st.just("upload")),  # branch 2
    st.tuples(
        st.just("PUT"),
        st.tuples(path_segments, field_segments).map(lambda t: f"{t[0]}/fields/{t[1]}/context"),
    ),  # branch 3
    st.tuples(st.just("PUT"), path_segments.map(lambda i: f"{i}/display-label")),  # branch 4
    st.tuples(st.just("GET"), path_segments),  # branch 5
    st.tuples(st.just("DELETE"), path_segments),  # branch 6
)
free_requests = st.tuples(st.sampled_from(METHODS), subpaths)
requests = st.one_of(table_requests, free_requests)


@given(request=requests)
@settings(max_examples=200)
def test_exactly_one_intended_branch_handles_any_request(request):
    """Property 1: exactly one branch handles any request, and it is the intended one.

    For any method in {GET, POST, PUT, DELETE, PATCH, HEAD} and any subpath of
    0–5 segments over an alphabet that includes the literals ``upload``,
    ``fields``, ``context`` and ``display-label`` as well as free-form ids,
    ``schemas_router`` dispatches to the handler design §4.5's table names, and
    to the trailing ``404`` for every shape that table does not list. No two
    branches accept the same ``(method, subpath)``.

    **Validates: Requirements 1.1, 1.2, 2.3, 3.1, 4.9, 8.1**
    """
    method, subpath = request
    problems = dispatch_failures(method, subpath)
    assert not problems, "\n".join(problems)


# ---------------------------------------------------------------------------
# The table itself, row by row — R8.1's characterization
# ---------------------------------------------------------------------------
# Design §4.5's exhaustive table, verbatim in substance: every request shape the
# frontend issues plus the shapes that must fall through.
DESIGN_TABLE = (
    ("GET", "", "branch 1 — list"),
    ("POST", "upload", "branch 2 — upload"),
    ("PUT", "abc/fields/email/context", "branch 3 — context write"),
    ("PUT", "abc/display-label", "branch 4 — rename"),
    ("GET", "abc", "branch 5 — one schema"),
    ("DELETE", "abc", "branch 6 — delete"),
    ("GET", "upload", "branch 5 — 404 from get_schema('upload'), R1.2"),
    ("GET", "abc/fields/email/context", "404 — was the whole list before"),
    ("PUT", "abc", "404"),
    ("POST", "", "branch 1 — 405"),
)


def test_dispatch_matches_design_table_row_for_row():
    """Every row of design §4.5's table dispatches as the table says.

    Reported as one message so the red/green split is legible in a single place:
    the four missing routes and the collection ``405`` fail here today, the
    collection ``GET``, the upload ``POST`` and the plain fall-through ``404``s
    pass.

    **Validates: Requirements 1.1, 1.2, 2.3, 3.1, 4.9, 8.1**
    """
    problems: list[str] = []
    for method, subpath, note in DESIGN_TABLE:
        for problem in dispatch_failures(method, subpath):
            problems.append(f"{problem}   [expected {note}]")
    assert not problems, "design §4.5's table is not what schemas_router does:\n" + "\n".join(problems)


def test_no_two_branches_accept_the_same_request():
    """The six acceptance predicates are pairwise disjoint over the table.

    This is the structural half of Property 1's last clause. Branches 3–6 are
    disjoint on ``len(parts)`` alone — 4, 2, 1, 1 — and the two one-segment
    branches are disjoint on method; branch 2 shares a length with 5 and 6 and
    is disjoint from both on method; branch 1 is the only branch reachable with
    zero segments.

    **Validates: Requirement 8.1**
    """
    overlaps = []
    for method in METHODS:
        for subpath in ("", "upload", "abc", "abc/display-label", "abc/fields/email/context", "a/b/c", "a/b/c/d/e"):
            accepted = accepting_branches(method, split_subpath(subpath))
            if len(accepted) > 1:
                overlaps.append(f"{method} {subpath!r}: {accepted}")
    assert not overlaps, "branches are not disjoint:\n" + "\n".join(overlaps)


# ---------------------------------------------------------------------------
# The two collection cases, asserted explicitly
# ---------------------------------------------------------------------------
def test_collection_get_returns_the_listing():
    """``GET /api/schemas`` reaches ``get_schemas``. Green today; keep it green.

    **Validates: Requirement 8.1**
    """
    calls, response = observe("GET", "")
    assert calls == [("get_schemas", ())], f"expected the listing handler, got {calls}"
    assert response["statusCode"] == 200


def test_collection_post_is_a_method_error_not_a_missing_endpoint():
    """``POST /api/schemas`` answers ``405``, not the trailing ``404``.

    Design §4.5: a POST to the collection is a real method error. Today it falls
    through to the trailing ``404``, which reads as "the endpoint does not
    exist" when it does — so this assertion is red until task 4.2.

    **Validates: Requirement 8.1**
    """
    calls, response = observe("POST", "")
    assert calls == [], f"expected no handler call, got {calls}"
    assert response["statusCode"] == 405, (
        f"expected 405 on POST /api/schemas, got {response['statusCode']}: {response.get('body')}"
    )


def test_get_on_the_upload_literal_reaches_the_per_id_branch():
    """``GET /api/schemas/upload`` is a per-id read of an id no source produces.

    The shape that distinguishes a correct ordering from a lucky one: it must
    reach branch 5 and ``404`` out of ``get_schema("upload")`` (R1.2), not be
    special-cased and not be swallowed by a path-agnostic ``GET`` branch.

    **Validates: Requirements 1.1, 1.2**
    """
    calls, _response = observe("GET", "upload")
    assert calls == [("get_schema", ("upload",))], f"expected get_schema('upload'), got {calls}"


def test_trailing_404_names_the_subpath():
    """The residual ``404`` still names the subpath it rejected. Green today.

    **Validates: Requirement 8.1**
    """
    _calls, response = observe("PATCH", "abc/def")
    assert response["statusCode"] == 404
    assert "abc/def" in json.loads(response["body"])["error"]
