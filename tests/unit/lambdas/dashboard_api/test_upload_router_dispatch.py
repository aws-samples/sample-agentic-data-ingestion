#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit test: `upload_router` dispatch — one presign route, and honest refusals.

WHY THIS FILE EXISTS
--------------------
p64 R4.3 called for "exactly one server-side presign handler reachable", and
noted that cross-sending yields "a 400 with no test covering the mismatch". This
file is that missing test. **The mismatch was not where R4.3 said it was**, and
p64 task 0.2 corrected it against the deployed artifacts: the two dead keys
(``upload-url``, ``upload-file``) were unreachable from the shipped dashboard
bundle, so cross-sending to *them* was hypothetical. The reachable defect was the
``upload`` key's own fall-through::

    "upload": lambda event, subpath: (
        presign_upload(event)
        if subpath == "presign" and event.get("httpMethod") == "POST"
        else generate_upload_url(event)          # <-- every other shape
    ),

Any request under ``/api/upload/*`` that was not exactly ``POST .../presign``
landed in ``generate_upload_url``, which reads ``pipeline_key``. So
``GET /api/upload/presign`` — a correctly spelled route, wrong method — answered
``400 pipeline_key and file_name are required``: a complaint about a field the
caller had never been asked for, naming a vocabulary no live caller uses
(``FileUploadModal.tsx`` sends ``pipeline_id``). Nothing explained why, and
nothing tested it.

WHAT IS ASSERTED, AND WHY IT IS NOT JUST "STATUS CODES"
-------------------------------------------------------
Two separable things, because a status alone cannot tell them apart:

1. **Which handler ran.** ``presign_upload`` is patched to a sentinel, so a test
   can say *which* function answered rather than inferring it — following
   ``test_approvals_router_dispatch.py``'s reasoning that a 404 from a handler and
   a 404 from a router fall-through are the same status and different facts.
2. **What the body says.** The regression that mattered was a *misleading message*,
   not merely a wrong number. Every refusal is asserted to be free of
   ``pipeline_key`` — the field the caller was never asked for. A future
   fall-through that reintroduced a body-contract handler would restore the 400
   and fail here even if someone had kept the status codes tidy.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
From R4.3 and task 1d.3's routing table, **not** from running the collapsed
router and recording what it did (``LGR-28b4d75-5``). The pre-collapse behaviour
these assertions describe is the defect, so a test written from that output would
have pinned it.

No AWS call is made and no boto3 client or ``moto.mock_aws()`` is created here
(``tests/unit/test_no_duplicate_mocking_prop.py``): every handler ``upload_router``
can reach is replaced before the router is called, so what is exercised is
dispatch alone. ``AWS_REGION`` is pinned by assignment in
``tests/unit/conftest.py`` (``LGR-3e45e65-1``) rather than set here.

**Validates: Requirements 4.3**
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

# The body the live caller actually sends (`FileUploadModal.tsx`). Every refusal
# below is driven with this shape, because that is what made the old 400 a lie:
# the caller HAD supplied an identifier, under the name the live route reads.
_LIVE_BODY = {"pipeline_id": "some-pipeline", "file_name": "data.csv"}

# Subpaths under `upload` that are NOT the presign route. Each of these used to
# reach `generate_upload_url`.
_NON_PRESIGN_SUBPATHS = ["", "presigned", "presign/extra", "file", "url"]

# Every method API Gateway can hand the router. OPTIONS is excluded: it is
# answered by `route_request` before any resource router is consulted
# (`router.py`, the `if http_method == "OPTIONS"` arm), so it never arrives here.
_NON_POST_METHODS = ["GET", "PUT", "PATCH", "DELETE", "HEAD"]


@pytest.fixture
def router_module(mock_lambda_env, monkeypatch):
    """`router`, imported with the env its transitive handler imports require."""
    monkeypatch.setenv("AWS_REGION", _UNIT_LAYER_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _UNIT_LAYER_REGION)

    import router as router_mod

    return router_mod


def _event(subpath: str, method: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    path = f"/api/upload/{subpath}" if subpath else "/api/upload"
    return {
        "path": path,
        "httpMethod": method,
        "headers": {},
        "queryStringParameters": {},
        "body": json.dumps(body if body is not None else _LIVE_BODY),
    }


def _sentinel(name: str):
    """A stand-in handler that reports which one it is."""

    def _handler(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return {"statusCode": 200, "body": json.dumps({"handler": name})}

    return _handler


def _dispatch(router_module: Any, subpath: str, method: str) -> tuple[int, dict[str, Any]]:
    """Route one request with `presign_upload` stubbed; return (status, body)."""
    with patch.object(router_module, "presign_upload", _sentinel("presign_upload")):
        response = router_module.upload_router(_event(subpath, method), subpath)
    return response["statusCode"], json.loads(response["body"])


# ===========================================================================
# The one route reaches the one handler
# ===========================================================================


class TestTheLiveRoute:
    def test_post_presign_reaches_presign_upload(self, router_module):
        status, body = _dispatch(router_module, "presign", "POST")
        assert status == 200
        assert body["handler"] == "presign_upload"


# ===========================================================================
# The body-shape mismatch that used to 400 silently — R4.3's missing test
# ===========================================================================


class TestNoRefusalBlamesAFieldTheCallerWasNeverAskedFor:
    """The regression this file exists for.

    Pre-collapse, every case below answered ``400 pipeline_key and file_name are
    required``. The caller had supplied ``pipeline_id`` — the name the live route
    reads — so the message named a field no live caller sends, on a route the
    caller had spelled correctly.
    """

    @pytest.mark.parametrize("method", _NON_POST_METHODS)
    def test_wrong_method_on_presign_is_405_not_a_body_complaint(self, router_module, method):
        status, body = _dispatch(router_module, "presign", method)
        assert status == 405, f"{method} /api/upload/presign should be a method error, got {status}: {body}"
        assert "pipeline_key" not in json.dumps(body)
        assert method in body["error"], "the refusal should name the method it refused"
        assert "presign" in body["error"], "the refusal should name the route it refused"

    @pytest.mark.parametrize("subpath", _NON_PRESIGN_SUBPATHS)
    def test_unknown_subpath_is_404_not_a_body_complaint(self, router_module, subpath):
        status, body = _dispatch(router_module, subpath, "POST")
        assert status == 404, f"POST /api/upload/{subpath} should be a routing 404, got {status}: {body}"
        assert "pipeline_key" not in json.dumps(body)

    @pytest.mark.parametrize("method", ["POST", *_NON_POST_METHODS])
    @pytest.mark.parametrize("subpath", ["presign", *_NON_PRESIGN_SUBPATHS])
    def test_no_shape_under_upload_ever_answers_400(self, router_module, method, subpath):
        """The whole matrix. A 400 here means a body-contract handler was reached."""
        status, body = _dispatch(router_module, subpath, method)
        assert status != 400, f"{method} /api/upload/{subpath} answered 400: {body}"
        assert "pipeline_key" not in json.dumps(body)

    def test_the_405_does_not_pretend_the_route_is_missing(self, router_module):
        """A wrong method on an existing route is 405, not 404.

        Kept separate from the parametrized case above because the distinction is
        the point: 404 would tell the caller to fix the URL, which is the one
        thing that is already correct.
        """
        status, _ = _dispatch(router_module, "presign", "GET")
        assert status == 405
        status, _ = _dispatch(router_module, "presigned", "POST")
        assert status == 404


# ===========================================================================
# The collapse holds — one route, one handler
# ===========================================================================


class TestTheCollapseHolds:
    def test_the_dead_aliases_are_unroutable(self, router_module):
        """`/api/upload-url` and `/api/upload-file` now 404 at the resource level.

        Asserted through `route_request` rather than `RESOURCE_ROUTERS`, because
        what matters to an old caller is the answer it gets, not the registry: a
        resource-level 404 naming the resource is strictly more informative than
        the 400 about `pipeline_key` these two used to produce.
        """
        for resource in ("upload-url", "upload-file"):
            response = router_module.route_request(
                {
                    "path": f"/api/{resource}",
                    "httpMethod": "POST",
                    "headers": {},
                    "queryStringParameters": {},
                    "body": json.dumps(_LIVE_BODY),
                }
            )
            body = json.loads(response["body"])
            assert response["statusCode"] == 404, f"/api/{resource} should be unroutable, got {response['statusCode']}"
            assert resource in body["error"], "the 404 should name the resource that is gone"
            assert "pipeline_key" not in json.dumps(body)

    def test_no_second_presign_handler_survives(self, router_module):
        """`generate_upload_url` is gone from both the handler module and the router.

        A fence, not a tautology: leaving the function in place would have left a
        callable second definition of "presign an upload" with an incompatible body
        contract, one route away from being reachable again. That duplication is
        how `/upload-file` stayed wrong long enough to be documented wrong in the
        frontend as well (`api-endpoints.ts`, amended by task 1d.2).
        """
        from handlers import upload as upload_mod

        assert not hasattr(upload_mod, "generate_upload_url")
        assert not hasattr(router_module, "generate_upload_url")

    def test_upload_router_is_a_named_function_not_a_lambda(self, router_module):
        """The registry entry is `upload_router`, so its branches can be documented.

        The fall-through this file exists to remove lived inside a conditional
        expression in the `RESOURCE_ROUTERS` literal, where there was nowhere to
        write down why it was wrong.
        """
        assert router_module.RESOURCE_ROUTERS["upload"] is router_module.upload_router
        assert router_module.upload_router.__name__ == "upload_router"
