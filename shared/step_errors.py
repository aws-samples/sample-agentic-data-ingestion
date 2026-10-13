# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""StepFailure exception hierarchy and step_handler decorator for Step_Lambdas.

P28 R1 (error propagation): Step_Lambdas raise StepFailure (or a subclass)
instead of returning ``{"statusCode": 500, ...}`` so the Step Functions task
actually fails and the state machine's Retry/Catch policies fire.

SFN matching contract
    The exception CLASS NAME is what Step Functions reports as the ``Error``
    field and what Retry/Catch ``ErrorEquals`` match on (via the Lambda
    runtime's ``errorType``). The names ``StepFailure`` and
    ``TransientStepFailure`` are load-bearing — do not rename these classes.

Serialization contract (stable, parsed by the RecordFailure Lambda)
    ``str(exc)`` is a single JSON object with exactly these keys::

        {
            "step_name":  "<step that failed>",
            "error_type": "permanent" | "transient",
            "message":    "<human-readable message>",
            "details":    { ... }        # optional context, JSON-safe
        }

    The Lambda runtime places ``str(exc)`` in ``errorMessage`` inside the SFN
    ``Cause``, so the Catch path can recover the structured Failure_Record
    fields with ``json.loads``. Non-JSON-serializable detail values are
    stringified (``default=str``); if details cannot be serialized at all
    (e.g. circular references), a minimal document with a
    ``serialization_error`` marker is produced instead — constructing the
    exception never raises because of bad details.
"""

import functools
import json
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, Literal, ParamSpec, TypeVar

if TYPE_CHECKING:  # pragma: no cover - typing only; keeps runtime deps stdlib-only
    from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)

ErrorType = Literal["permanent", "transient"]

_VALID_ERROR_TYPES: tuple[str, ...] = ("permanent", "transient")

P = ParamSpec("P")
R = TypeVar("R")

#: ClientError codes that mean the requested resource genuinely does not
#: exist. Read helpers treat these as real not-found and return their
#: not-found value (None / {} / []) — everything else is an infrastructure
#: error and must NOT masquerade as not-found (P28 R17.3).
NOT_FOUND_CLIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ResourceNotFoundException",  # DynamoDB table/index missing
        "NoSuchKey",  # S3 object missing
        "NoSuchBucket",  # S3 bucket missing
        "404",  # S3 HeadObject-style not-found
        "NotFound",
    }
)

#: ClientError codes that are genuinely retryable (throttling, transient
#: service-side failures, timeouts). Single source of truth — shared with
#: ``shared/agentcore_proxy.py`` so the two classifications cannot drift.
TRANSIENT_CLIENT_ERROR_CODES: frozenset[str] = frozenset(
    {
        "ProvisionedThroughputExceededException",
        "ThrottlingException",
        "TooManyRequestsException",
        "RequestLimitExceeded",
        "ServiceQuotaExceededException",
        "ServiceUnavailable",
        "ServiceUnavailableException",
        "InternalServerError",
        "InternalServerException",
        "InternalFailure",
        "ServiceException",
        "RequestTimeout",
        "RequestTimeoutException",
        "SlowDown",
    }
)


class StepFailure(Exception):
    """Unrecoverable Step_Lambda failure (SFN does NOT retry by default).

    Raise this (or ``TransientStepFailure`` for retryable conditions) from
    state-machine-invoked handlers instead of returning an error payload.

    Args:
        step_name: Canonical name of the failing step (e.g. ``"schema_mapping"``).
        message: Human-readable description of the failure.
        error_type: ``"permanent"`` (default) or ``"transient"``.
        details: Optional JSON-safe context (bounded by the caller — the
            serialized exception travels in the SFN ``Cause``).
    """

    def __init__(
        self,
        step_name: str,
        message: str,
        *,
        error_type: ErrorType = "permanent",
        details: dict[str, Any] | None = None,
    ) -> None:
        if error_type not in _VALID_ERROR_TYPES:
            raise ValueError(f"error_type must be one of {_VALID_ERROR_TYPES}, got {error_type!r}")
        self.step_name = step_name
        self.message = message
        self.error_type: ErrorType = error_type
        self.details: dict[str, Any] = details if details is not None else {}
        super().__init__(self._serialize())

    def to_dict(self) -> dict[str, Any]:
        """Structured payload matching the documented str() serialization."""
        return {
            "step_name": self.step_name,
            "error_type": self.error_type,
            "message": self.message,
            "details": self.details,
        }

    def _serialize(self) -> str:
        """Serialize to the stable JSON format documented in the module docstring."""
        try:
            return json.dumps(self.to_dict(), default=str)
        except (TypeError, ValueError):
            # Never let bad details break exception construction (that would
            # mask the real failure). Fall back to a minimal, parseable doc.
            return json.dumps(
                {
                    "step_name": self.step_name,
                    "error_type": self.error_type,
                    "message": self.message,
                    "details": {"serialization_error": "details were not JSON-serializable"},
                }
            )


class TransientStepFailure(StepFailure):
    """Retryable Step_Lambda failure — SFN Retry policies match this name.

    Use for conditions that may succeed on retry (throttling, eventual
    consistency races, timeouts). ``error_type`` is always ``"transient"``.
    """

    def __init__(
        self,
        step_name: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(step_name, message, error_type="transient", details=details)


def raise_for_read_client_error(exc: "ClientError", step_name: str, *, operation: str) -> None:
    """Classify a botocore ClientError caught in a step-Lambda read helper (P28 R17.3).

    Read helpers (getters/lookups) used to swallow every exception and return
    their not-found value, so a DynamoDB throttle masqueraded as "no source
    schema found". This helper narrows that:

    - Not-found codes (:data:`NOT_FOUND_CLIENT_ERROR_CODES`): returns
      normally — the caller then returns its not-found value, which is the
      honest answer.
    - Transient infra codes (:data:`TRANSIENT_CLIENT_ERROR_CODES`): raises
      :class:`TransientStepFailure` so SFN Retry policies fire.
    - Everything else (access denied, validation errors, ...): raises
      :class:`StepFailure` (permanent).

    Args:
        exc: The caught ``botocore.exceptions.ClientError``.
        step_name: Canonical name of the step (matches the module's
            ``@step_handler`` name).
        operation: Name of the read operation for the failure details
            (e.g. ``"get_source_schema"``).
    """
    error: dict[str, Any] = getattr(exc, "response", {}).get("Error", {})
    code = error.get("Code", "Unknown")
    message = error.get("Message", str(exc))
    if code in NOT_FOUND_CLIENT_ERROR_CODES:
        return
    details = {"error_code": code, "operation": operation}
    if code in TRANSIENT_CLIENT_ERROR_CODES:
        raise TransientStepFailure(step_name, f"{operation} failed: {code}: {message}", details=details) from exc
    raise StepFailure(step_name, f"{operation} failed: {code}: {message}", details=details) from exc


def _event_arg(args: tuple[Any, ...], kwargs: dict[str, Any]) -> dict[str, Any] | None:
    """Recover the Lambda event from a handler's call arguments.

    Handlers are ``(event, context)``, but the decorator's signature is
    ParamSpec-generic and its own tests call it positionally and by keyword.
    Anything that is not a dict first argument is not an event, and the resume
    machinery below simply does not engage.
    """
    candidate = args[0] if args else kwargs.get("event")
    return candidate if isinstance(candidate, dict) else None


def _resume_hooks() -> Any:
    """Import the retry-resume helpers lazily, or return None.

    Deliberately a function-local import at call time rather than a module
    import: this module keeps its RUNTIME dependencies to the standard library
    (see the ``TYPE_CHECKING`` guard above), and ``shared/retry_utils`` pulls
    in boto3. Importing it here keeps ``step_errors`` importable in any
    context — including the unit tests that exercise the decorator with no AWS
    anything — while still giving all ten ``@step_handler`` Lambdas the
    behaviour from one place.
    """
    try:
        import retry_utils

        return retry_utils
    except Exception:  # pragma: no cover - layer/packaging variance only
        logger.debug("retry_utils unavailable; retry resume is inert for this invocation")
        return None


def step_handler(step_name: str) -> Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorator for Step_Lambda entrypoints (P28 R1.1).

    Behavior:
        - Successful returns pass through unchanged.
        - ``StepFailure`` (and subclasses) are logged and re-raised as-is,
          preserving the exception class name for SFN ``ErrorEquals`` matching.
        - Any other exception is logged and converted to a permanent
          ``StepFailure`` — the original exception is preserved as
          ``__cause__`` and its type recorded in ``details``.
        - NEVER returns an error payload: every failure path raises, so the
          Step Functions task fails and Retry/Catch fire.

    Retry resume (p32 2.18):
        - Before the handler runs, ``retry_utils.resolve_step_resume`` decides
          whether this step completed before the resume point of a
          retry-from-step execution. When it did AND its cached Step Functions
          result is available, that result is returned in the handler's place:
          the step is not re-executed and its ``{step}_result`` key is
          re-emitted through the same ``resultPath`` rather than clobbered
          (the failure task 12 observed live).
        - After a successful run, the returned value is cached on the step's
          state row so a later retry can serve it. Both halves fail soft —
          neither can turn a working step into a failing one.
        - "Successful returns pass through unchanged" still holds exactly: the
          decorator never rewrites a value the handler produced. A served
          result is a value the handler never produced, on a path where it
          never ran.

    Usage::

        @step_handler("file_validation")
        def lambda_handler(event, context):
            ...
    """

    def decorator(func: Callable[P, R]) -> Callable[P, R]:
        @functools.wraps(func)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            event = _event_arg(args, kwargs)
            hooks = _resume_hooks() if event is not None else None

            if hooks is not None:
                try:
                    served = hooks.resolve_step_resume(event, step_name)
                except Exception:
                    logger.exception("Resume check for step '%s' failed; executing the step", step_name)
                    served = None
                if served is not None:
                    logger.info("Step '%s' served from its cached result (retry resume)", step_name)
                    # The served value IS a step result — the same shape the
                    # wrapped handler would have returned, because it is the
                    # value it returned on the original run.
                    return served  # type: ignore[no-any-return]

            try:
                result = func(*args, **kwargs)
            except StepFailure:
                logger.exception("Step '%s' failed", step_name)
                raise
            except Exception as exc:
                logger.exception("Step '%s' raised unexpected %s", step_name, type(exc).__name__)
                raise StepFailure(
                    step_name,
                    f"{type(exc).__name__}: {exc}",
                    details={"original_error_type": type(exc).__name__},
                ) from exc

            # `hooks` is only set when `event` is a dict, so both are known good
            # here. The run id comes from the RESULT first: FileValidation MINTS
            # the run id and its event carries none, so reading the event alone
            # would leave step 1 — the anchor every downstream Payload
            # dereferences — permanently uncached.
            if hooks is not None and isinstance(result, dict):
                run_id = result.get("run_id") or event.get("run_id")  # type: ignore[union-attr]
                if isinstance(run_id, str) and run_id:
                    hooks.cache_step_result(run_id, step_name, result)

            return result

        return wrapper

    return decorator
