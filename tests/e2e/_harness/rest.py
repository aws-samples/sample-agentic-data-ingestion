# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Cognito-authenticated dashboard REST client for e2e tests.

The deployed REST API uses a Cognito User Pools authorizer behind CloudFront
(the regional WAF blocks direct ``execute-api`` access), so the harness:

1. obtains an ID token via ``USER_PASSWORD_AUTH`` against the dedicated e2e
   app client (``E2eTestClientId`` output of the Auth stack), and
2. sends it as the **raw** ``Authorization`` header value — no ``Bearer``
   prefix; a scope-less Cognito authorizer parses the whole header as the JWT,
3. against the CloudFront distribution URL (paths keep their ``/api/...``
   form; CloudFront maps ``/api/*`` to the API's ``/v1`` stage).

Tokens are valid for one hour, which comfortably covers a test session; the
client does not refresh.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import requests

logger = logging.getLogger(__name__)


def acquire_cognito_id_token(
    boto3_session: Any,
    *,
    client_id: str,
    username: str,
    password: str,
) -> str:
    """Obtain a Cognito ID token via USER_PASSWORD_AUTH.

    Raises:
        RuntimeError: if the pool demands an MFA/challenge flow (e.g. deployed
            with ``-c adi:requireMfa=true``) — programmatic password auth
            cannot complete pool-level required MFA without TOTP enrollment.
    """
    idp = boto3_session.client("cognito-idp")
    resp = idp.initiate_auth(
        ClientId=client_id,
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    challenge = resp.get("ChallengeName")
    if challenge:
        raise RuntimeError(
            f"Cognito returned challenge {challenge!r} instead of tokens. If the "
            "pool was deployed with -c adi:requireMfa=true, pool-level MFA blocks "
            "the e2e programmatic-token path — run e2e against a deployment "
            "without required MFA (see README 'Hardening for real data')."
        )
    return resp["AuthenticationResult"]["IdToken"]


@dataclass
class RestResponse:
    """Parsed response from a REST call."""

    status_code: int
    body: Any  # parsed JSON (dict, list, or None if empty)


class CognitoRestClient:
    """Cognito-token HTTP client for the dashboard REST API via CloudFront.

    Args:
        base_url: The CloudFront distribution URL (e.g. ``https://dxxxx.cloudfront.net``).
        id_token: A Cognito ID token; sent as the raw ``Authorization`` header.
    """

    def __init__(self, base_url: str, id_token: str) -> None:
        self._base_url = base_url.rstrip("/")
        self._id_token = id_token

    def _request(self, method: str, path: str, body: Any | None = None) -> RestResponse:
        """Execute an authenticated HTTP request and return the parsed response."""
        url = f"{self._base_url}{path}"
        data = json.dumps(body) if body is not None else None
        # Raw token — the scope-less Cognito authorizer treats the entire
        # header value as the JWT; a 'Bearer ' prefix would fail validation.
        headers = {"Authorization": self._id_token}
        if data:
            headers["Content-Type"] = "application/json"

        logger.debug("%s %s", method, url)
        resp = requests.request(
            method=method,
            url=url,
            headers=headers,
            data=data,
            timeout=30,
        )

        parsed_body: Any = None
        if resp.content:
            try:
                parsed_body = resp.json()
            except (json.JSONDecodeError, ValueError):
                parsed_body = resp.text

        return RestResponse(status_code=resp.status_code, body=parsed_body)

    # ── Public helpers ────────────────────────────────────────────────

    def get(self, path: str) -> RestResponse:
        """Authenticated GET request."""
        return self._request("GET", path)

    def post(self, path: str, body: Any = None) -> RestResponse:
        """Authenticated POST request."""
        return self._request("POST", path, body)

    def put(self, path: str, body: Any = None) -> RestResponse:
        """Authenticated PUT request."""
        return self._request("PUT", path, body)

    def delete(self, path: str) -> RestResponse:
        """Authenticated DELETE request."""
        return self._request("DELETE", path)
