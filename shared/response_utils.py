# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Shared HTTP response / error utilities for AgenticDataIngestion Lambdas (P21 Req 5).

Single source of truth for the API-Gateway-style response builder, the error
handler, and Decimal-aware JSON encoding. Previously these lived only in the
dashboard API handler (``dashboard_api/handlers/common.py``); promoting them to
``shared/`` makes them importable by every Lambda via the Layer_Import_Fallback
path (``/opt/python`` in the Lambda layer, or the local ``shared/`` directory in
test/local execution).

This module sits at dependency layer 0: it imports only the standard library
(``json``, ``decimal``, ``logging``, ``typing``) so it has no cross-layer
dependencies and can be imported by any Lambda.
"""

import json
import logging
from decimal import Decimal
from typing import Any

logger = logging.getLogger(__name__)


class DecimalEncoder(json.JSONEncoder):
    """JSON encoder for DynamoDB Decimal types"""

    def default(self, obj: Any) -> Any:
        if isinstance(obj, Decimal):
            return float(obj)
        return super().default(obj)


def create_response(status_code: int, body: Any, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """Create standardized API Gateway response"""
    # ``Access-Control-Allow-Origin: "*"`` below is DELIBERATE, and it is correct
    # because of two facts that are nowhere near this line. p47 R13 evaluated
    # narrowing it and kept it; the decision record is p47 E6, and the argument is
    # in `.kiro/specs/p47-operative-gaps-and-hot-path-efficiency/e4-cors-origin-arms.md`
    # §2. Both facts are cited with the file that carries them, so that a change to
    # either is findable from here:
    #
    # 1. **The deployed path is same-origin, so this header is never consulted.**
    #    `deploy-cdk/lib/hosting-stack.ts` deploys `config.json` with
    #    `apiBaseUrl: '/api'` (a relative path) and attaches the `/api/*` behaviour
    #    to the SAME CloudFront distribution whose default behaviour serves the SPA
    #    from S3. The browser loads the dashboard from `https://d….cloudfront.net`
    #    and calls `https://d….cloudfront.net/api/…`: same scheme, host and port. A
    #    same-origin fetch sends no `Origin` header, triggers no preflight, and does
    #    not read `Access-Control-Allow-Origin`. So this value is emitted and
    #    discarded on every production request. That premise is PINNED by
    #    `deploy-cdk/test/p47-cors-posture.test.ts`, which fails if `apiBaseUrl`
    #    stops being `/api` — do not "fix" that test by changing its expectation.
    #
    # 2. **The credential is not ambient, so `*` grants a hostile page nothing.**
    #    The Cognito token is attached explicitly by application code
    #    (`dashboard-react/src/services/api-client.ts`, `Authorization: token`) and
    #    held in `sessionStorage`, never in a cookie.
    #    `Access-Control-Allow-Credentials` is absent from this entire repository
    #    and is spec-incompatible with `*` anyway — a browser rejects the
    #    combination — so `*` is not covering for a cookie session. An attacker's
    #    page can issue the cross-origin fetch that `*` permits, but cannot obtain
    #    the token, so the request arrives unauthenticated and the
    #    `CognitoUserPoolsAuthorizer` answers 401. Every method on the API carries
    #    an authorizer; there is no unauthenticated body to leak.
    #
    # REVISIT THIS VALUE IF EITHER FACT CHANGES, specifically if any of:
    #   - `apiBaseUrl` becomes an absolute URL on a different host than the SPA, or
    #     `VITE_API_BASE_URL` (docs/LOCAL_DEVELOPMENT.md §4) becomes the supported
    #     production mode — fact 1 dies and this `*` becomes load-bearing;
    #   - any endpoint becomes unauthenticated — fact 2 dies for that endpoint;
    #   - `Access-Control-Allow-Credentials` is introduced anywhere — it cannot
    #     coexist with `*`, and the combination fails rather than leaks;
    #   - a custom domain + ACM certificate lands, which makes a build-time-constant
    #     origin available for the first time (`AwsSolutions-CFR4`).
    #
    # Independently of all of the above, the browser-visible header on `/api/*` is
    # narrowed at the edge: a `cloudfront.ResponseHeadersPolicy` with
    # `originOverride: true` in `hosting-stack.ts` REPLACES this value on the way
    # out. That is why narrowing it here as well would buy nothing, and why this
    # function is deliberately unchanged — one `create_response` definition stays
    # one (p47 R13.1).
    default_headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Headers": "Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token",
        "Access-Control-Allow-Methods": "GET,POST,PUT,DELETE,OPTIONS",
    }

    if headers:
        default_headers.update(headers)

    return {"statusCode": status_code, "headers": default_headers, "body": json.dumps(body, cls=DecimalEncoder)}


def handle_error(error: Exception, message: str = "Internal server error") -> dict[str, Any]:
    """Handle errors and return standardized error response"""
    logger.error(f"{message}: {error!s}")
    return create_response(500, {"error": message, "details": str(error)})
