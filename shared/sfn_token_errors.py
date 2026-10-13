# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Step Functions task-token consumed/expired error classification (P28 R2.3).

Single shared source (the P28 R18.2 single-source habit) for the ClientError
codes that mean a ``SendTaskSuccess``/``SendTaskFailure`` token was already
consumed or has expired — the execution is no longer waiting on this
callback, so re-sending can never succeed. Callers treat these as
already-processed (mark the approval "expired", delete the token record),
NEVER as retryable errors; every other ClientError (throttling, service 5xx)
is transient and must keep the token record and the pending status intact so
the send can be retried.

Importers (p28b R3): the approval_handler and approval_timeout_handler
Lambdas, and ``dashboard_api/handlers/approvals.py`` (which aliases its
``_TOKEN_CONSUMED_ERROR_CODES`` to this constant).
"""

from botocore.exceptions import ClientError

#: ClientError codes meaning the task token was already consumed or expired
#: (the P28 R2.3 set).
TOKEN_CONSUMED_ERROR_CODES = frozenset({"TaskTimedOut", "TaskDoesNotExist", "InvalidToken"})


def is_token_consumed_error(client_error: ClientError) -> bool:
    """True when the ClientError means the token was already consumed/expired.

    True → already-processed outcome (not retryable); False → transient,
    the caller should re-raise so the send can be retried.
    """
    error_code = client_error.response.get("Error", {}).get("Code", "")
    return error_code in TOKEN_CONSUMED_ERROR_CODES
