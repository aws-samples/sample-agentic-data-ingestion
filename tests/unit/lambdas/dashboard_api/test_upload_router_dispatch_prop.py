#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: no generated request under /api/upload/* reaches a body-contract handler.

Companion to ``test_upload_router_dispatch.py``, which enumerates the shapes p64
task 1d.3 named. This file generates them instead, because the defect being
guarded was a **fall-through** — an `else` arm that caught *everything* not
matched above it — and a fixed list can only ever prove the cases someone thought
to write down. The pre-collapse ``upload`` entry answered
``400 pipeline_key and file_name are required`` for every subpath except
``presign`` and every method except ``POST``; an unbounded generator over both
coordinates is the honest shape for "every other request".

The property, in one sentence: **exactly one (subpath, method) pair reaches a
handler, and every other pair is refused with a status that describes the routing
or the method — never a 400 about a body field, and never a message naming
``pipeline_key``.**

Expectations come from R4.3 and task 1d.3's routing table, not from running the
collapsed router (``LGR-28b4d75-5``). Generated inputs deliberately include
``presign``-adjacent strings (``presigns``, ``Presign``, ``presign/x``), because
near-misses are what a substring or method-only branch gets wrong —
``schemas_router``'s docstring records exactly that failure in this file's
neighbour.

``presign_upload`` is patched to a sentinel, so no AWS call is made and no boto3
client or ``moto.mock_aws()`` is created here
(``tests/unit/test_no_duplicate_mocking_prop.py``). ``AWS_REGION`` is pinned by
assignment in ``tests/unit/conftest.py`` (``LGR-3e45e65-1``).

**Validates: Requirements 4.3**
"""

import json
import os
import sys
from typing import Any
from unittest.mock import patch

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

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

_LIVE_BODY = {"pipeline_id": "some-pipeline", "file_name": "data.csv"}

# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

# Methods API Gateway can present. OPTIONS is excluded because `route_request`
# answers it before any resource router is consulted, so it never arrives here.
_methods = st.sampled_from(["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])

# Subpaths, weighted towards near-misses of the one real route.
_subpaths = st.one_of(
    st.just(""),
    st.just("presign"),
    st.sampled_from(["presigns", "presigned", "Presign", "PRESIGN", "presign/", "presign/extra", "pre/sign"]),
    st.from_regex(r"[a-z0-9\-]{1,12}", fullmatch=True),
    st.from_regex(r"[a-z]{1,6}/[a-z]{1,6}", fullmatch=True),
)


@pytest.fixture(scope="module")
def router_module():
    """`router`, imported once for the module.

    Module-scoped and env-free: `tests/unit/conftest.py` already pins `AWS_REGION`
    and the table names by assignment, and nothing here calls a handler.
    """
    import router as router_mod

    return router_mod


def _sentinel(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
    return {"statusCode": 200, "body": json.dumps({"handler": "presign_upload"})}


def _dispatch(router_module: Any, subpath: str, method: str) -> tuple[int, dict[str, Any]]:
    event = {
        "path": f"/api/upload/{subpath}" if subpath else "/api/upload",
        "httpMethod": method,
        "headers": {},
        "queryStringParameters": {},
        "body": json.dumps(_LIVE_BODY),
    }
    with patch.object(router_module, "presign_upload", _sentinel):
        response = router_module.upload_router(event, subpath)
    return response["statusCode"], json.loads(response["body"])


def _is_the_live_route(subpath: str, method: str) -> bool:
    """R4.3's routing table, transcribed as an oracle.

    Segmentation matches the router's: strip the surrounding slashes, drop empty
    segments. So ``presign/`` and ``/presign`` are the live route; ``presign/x``
    is not.
    """
    parts = [p for p in subpath.strip("/").split("/") if p]
    return parts == ["presign"] and method == "POST"


# ---------------------------------------------------------------------------
# The property
# ---------------------------------------------------------------------------


class TestExactlyOneShapeReachesAHandler:
    @given(subpath=_subpaths, method=_methods)
    @settings(max_examples=10)
    def test_only_post_presign_reaches_a_handler(self, router_module, subpath, method):
        """**Validates: Requirements 4.3**"""
        status, body = _dispatch(router_module, subpath, method)

        if _is_the_live_route(subpath, method):
            assert status == 200
            assert body.get("handler") == "presign_upload"
        else:
            assert body.get("handler") is None, f"{method} /api/upload/{subpath} reached a handler: {body}"
            assert status in (404, 405), f"{method} /api/upload/{subpath} answered {status}: {body}"

    @given(subpath=_subpaths, method=_methods)
    @settings(max_examples=10)
    def test_no_refusal_ever_names_pipeline_key(self, router_module, subpath, method):
        """**Validates: Requirements 4.3**

        The message half of the property, asserted separately from the status half
        because the pre-collapse defect was a *misleading message*: a fall-through
        reintroduced with tidy status codes would still be the same bug.
        """
        status, body = _dispatch(router_module, subpath, method)
        assert status != 400, f"{method} /api/upload/{subpath} answered 400: {body}"
        assert "pipeline_key" not in json.dumps(body)
