# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`approvals_router` dispatch — the per-id GET must shadow nothing (p56).

WHY THIS FILE EXISTS
--------------------
codegraph reports **no covering tests** for `approvals_router`, and adding a
per-id branch to a router that already has four is precisely the change that went
wrong once in this repo. `schemas_router`'s own docstring records it: its pre-p35
third branch tested only ``http_method == "GET"`` and therefore caught every GET
under its resource, so ``GET /api/schemas/{id}`` returned the whole list and the
detail modal always described ``[0]``. The fix was "EVERY branch tests the
method", and this file is that rule asserted for `approvals_router` rather than
trusted.

The three contract suites (`test_router_response_contract.py`,
`test_router_mapping_prop.py`, `test_router_props.py`) were amended to enumerate
the new pair, per `LGR-ce8641e-1`. They assert *reachability*. This file asserts
*disjointness* — which handler, not merely "not a routing 404" — because a
shadowing bug is reachable by construction and would pass all three.

WHAT DISJOINTNESS RESTS ON
--------------------------
The per-id branch tests ``len(parts) == 1`` **and** ``http_method == "GET"``,
while approve/reject test a second segment **and** POST. The pairs are therefore
disjoint on both coordinates, not on one — so neither ordering nor luck is
carrying it. `pending` and `cleanup` are matched on the whole `subpath` above the
per-id branch, and neither is an id the approval_handler can produce (it writes
``{pipeline_id}_{approval_type}_{epoch}``), which is the same reasoning
`schemas_router` gives for ``GET /api/schemas/upload``.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
From the routing table the change is required to produce, not from running the
router and recording where each path landed (`LGR-28b4d75-5`). Every handler is
patched to a sentinel, so a test can say *which* function ran rather than
inferring it from a status code — a 404 from `get_approval` and a 404 from the
router's fall-through are the same status and completely different facts.
"""

import json
import os
import sys
from typing import Any
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

_UNIT_LAYER_REGION = os.environ["AWS_REGION"]

_APPROVAL_ID = "newtest-ab2a08a0_schema_mapping_1787402782"


@pytest.fixture
def router_module(mock_lambda_env, monkeypatch):
    """`router`, imported with the env its transitive handler imports require."""
    monkeypatch.setenv("AWS_REGION", _UNIT_LAYER_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _UNIT_LAYER_REGION)

    import router as router_mod

    return router_mod


def _event(path_suffix: str, method: str) -> dict[str, Any]:
    return {
        "path": f"/api/approvals/{path_suffix}" if path_suffix else "/api/approvals",
        "httpMethod": method,
        "headers": {},
        "queryStringParameters": {},
        "body": None,
    }


def _sentinel(name: str):
    """A stand-in handler that reports which one it is."""

    def _handler(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"statusCode": 200, "body": json.dumps({"handler": name})}

    return _handler


def _dispatch(router_module: Any, subpath: str, method: str) -> str:
    """Route one request with every approvals handler stubbed; name the winner.

    Returns the sentinel's name, or ``"ROUTER_404"`` for the fall-through.

    `get_approval` is patched on `handlers.approvals` rather than on `router`
    because the router imports it lazily inside the branch (the established shape
    for `cleanup_ghost_approvals` in the same function), so a module-level patch
    on `router` would not be seen.
    """
    from handlers import approvals as approvals_mod

    with (
        patch.object(router_module, "get_approvals", _sentinel("get_approvals")),
        patch.object(router_module, "get_pending_approvals", _sentinel("get_pending_approvals")),
        patch.object(router_module, "approve_request", _sentinel("approve_request")),
        patch.object(router_module, "reject_request", _sentinel("reject_request")),
        patch.object(approvals_mod, "get_approval", _sentinel("get_approval")),
        patch.object(approvals_mod, "cleanup_ghost_approvals", _sentinel("cleanup_ghost_approvals")),
    ):
        response = router_module.approvals_router(_event(subpath, method), subpath)

    body = json.loads(response["body"])
    return body.get("handler", "ROUTER_404" if response["statusCode"] == 404 else "UNKNOWN")


# ===========================================================================
# The new route reaches the new handler
# ===========================================================================


class TestPerIdGetRoutes:
    def test_get_one_approval_reaches_get_approval(self, router_module):
        assert _dispatch(router_module, _APPROVAL_ID, "GET") == "get_approval"

    def test_a_trailing_slash_still_reaches_it(self, router_module):
        """`.strip("/")` handles it, which is why the branch needs no empty-segment
        filter — and the filter is deliberately absent, because adding one would
        widen the two POST branches below (`/api/approvals/x//approve` would start
        routing to `approve_request`) and this change may not alter that flow."""
        assert _dispatch(router_module, f"{_APPROVAL_ID}/", "GET") == "get_approval"

    def test_the_id_is_passed_through_verbatim(self, router_module):
        """The whole segment, not a prefix or a normalised form.

        Live approval ids carry underscores and an epoch suffix
        (`{pipeline_id}_{approval_type}_{epoch}`), and the 404 has to name the id
        the caller asked for.
        """
        seen: list[str] = []

        def _capture(approval_id: str) -> dict[str, Any]:
            seen.append(approval_id)
            return {"statusCode": 200, "body": "{}"}

        from handlers import approvals as approvals_mod

        with patch.object(approvals_mod, "get_approval", _capture):
            router_module.approvals_router(_event(_APPROVAL_ID, "GET"), _APPROVAL_ID)

        assert seen == [_APPROVAL_ID]


# ===========================================================================
# The new route shadows nothing — the reason this file exists
# ===========================================================================


class TestPerIdGetShadowsNothing:
    """Every pre-existing branch still wins the request it won before."""

    def test_approve_still_reaches_approve_request(self, router_module):
        assert _dispatch(router_module, f"{_APPROVAL_ID}/approve", "POST") == "approve_request"

    def test_reject_still_reaches_reject_request(self, router_module):
        assert _dispatch(router_module, f"{_APPROVAL_ID}/reject", "POST") == "reject_request"

    def test_pending_still_reaches_the_pending_listing(self, router_module):
        """And is NOT read as an approval whose id is `"pending"`.

        The literal branch is matched on the whole `subpath` before `parts` is
        computed, so the per-id branch cannot see it. `"pending"` is also not an
        id the producer can write, so even the ordering is belt-and-braces.
        """
        assert _dispatch(router_module, "pending", "GET") == "get_pending_approvals"

    def test_cleanup_still_reaches_cleanup(self, router_module):
        assert _dispatch(router_module, "cleanup", "POST") == "cleanup_ghost_approvals"

    def test_the_root_listing_is_untouched(self, router_module):
        assert _dispatch(router_module, "", "GET") == "get_approvals"

    @pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
    def test_a_non_get_on_one_segment_does_not_reach_get_approval(self, router_module, method):
        """The method half of the disjointness, asserted directly.

        This is the `schemas_router` defect's exact shape: a branch that tested
        only the path would answer a POST to `/api/approvals/{id}` with the
        per-id read. It must fall through instead.
        """
        assert _dispatch(router_module, _APPROVAL_ID, method) == "ROUTER_404"

    def test_a_get_on_two_segments_does_not_reach_get_approval(self, router_module):
        """The segment-count half. `GET /api/approvals/{id}/approve` is not a read.

        It 404s, as it did before this change — the per-id branch must not widen
        it into one.
        """
        assert _dispatch(router_module, f"{_APPROVAL_ID}/approve", "GET") == "ROUTER_404"

    def test_a_get_on_an_unknown_two_segment_action_still_404s(self, router_module):
        assert _dispatch(router_module, f"{_APPROVAL_ID}/nonsense", "GET") == "ROUTER_404"


class TestEveryBranchTestsTheMethod:
    """The rule `schemas_router` learned the hard way, for the branch p56 adds.

    Scoped to the new branch on purpose. The `pending` branch above remains
    method-agnostic, and tightening it would be a behaviour change to an existing
    route rather than the addition of a new one — recorded here so the omission
    reads as deliberate rather than missed, with the disjointness argument that
    makes it safe stated in the router's own docstring.
    """

    def test_the_new_branch_is_get_only(self, router_module):
        reached = {
            method: _dispatch(router_module, _APPROVAL_ID, method) for method in ("GET", "POST", "PUT", "DELETE")
        }

        assert reached["GET"] == "get_approval"
        assert set(reached.values()) == {"get_approval", "ROUTER_404"}

    def test_pending_is_still_method_agnostic_and_that_is_recorded(self, router_module):
        """Characterization, not endorsement.

        If someone later tightens `pending` to GET-only, this test fails and
        points at the decision rather than letting the behaviour change silently.
        """
        assert _dispatch(router_module, "pending", "POST") == "get_pending_approvals"
