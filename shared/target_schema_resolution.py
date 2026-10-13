# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Target-schema resolution — one definition of the bucket-and-key pair read (p33 D5).

WHY THIS MODULE EXISTS RATHER THAN A FOURTH COPY
------------------------------------------------
Four near-copies of "read the target schema off the pipeline record" exist in
this repo, and one of them diverged for six months: ``schema_compare``'s
``check_target_schema_changed`` used to take the **bucket as a parameter** while
reading the **key** from the pipeline record, and its caller passed
``pipeline_metadata["generated_code_bucket"]`` — an attribute ``create_pipeline``
has never written. ``item_str`` returned ``""`` for the absent attribute, so the
run reached ``s3_client.get_object(Bucket="", ...)`` and died in botocore
parameter validation. ``LGR-7c9378d-1`` (**template / ratified**) records the
fix and the rule it established: **resolve the pair together, where the pair is
used**, and never accept a bucket from a caller. This module is that rule made
structural — there is no bucket parameter to get wrong.

The same reasoning that put ``content_fingerprint`` in
``shared/processed_file_ledger.py`` applies here (``LGR-eee8ac8-5``,
**template / ratified**): two callers that must agree on how an address and a
hash are composed is exactly the situation where a second, subtly different
definition silently disables a feature rather than failing.

THE NOT-FOUND CONTRACT — AND ITS DELIBERATE DIVERGENCE FROM ``schema_compare``
-----------------------------------------------------------------------------
``(None, None)`` is returned **only** when the pipeline record carries no
``target_schema_s3_key`` at all. A key that is present but points at nothing
**raises** (p33 D14 / C16), because a present key is a *claim that an object
exists*: ``create_pipeline`` writes ``target_schema_bucket`` and
``target_schema_s3_key`` together or writes neither — both assignments sit
inside one ``if len(s3_parts) == 2:`` block at
``dashboard_api/handlers/pipelines_crud.py:761-767`` (verified read;
``LGR-724f88c-1``, **backfilled**). A key pointing at nothing is therefore an
**inconsistent record**, the same category as key-without-bucket, and not a
pipeline that never had a target schema.

Returning ``(None, None)`` for it would route a dangling reference to the
"no target schema, nothing verified" path and report it as "no target schema" —
the same "could not check" vs "checked and passed" confusion p33 exists to
remove, in a new costume.

**``schema_compare.get_target_schema_from_s3`` (``lambda_function.py:369-385``)
returns ``(None, None)`` for exactly this input and says so in its docstring.
That divergence is deliberate and is NOT a bug to align away.** Its question is
"did the schema change since the baseline", where a missing object is answered
by the observed-version report (``LGR-eee8ac8-4``, **template / ratified**).
This module's question is "what am I measuring compliance against", and there is
no honest answer to that when the named object is gone. ``schema_compare`` is
not modified by p33 (design D5), so the two will differ on this input on
purpose. Do not "fix" either one to match the other.

**Revisit when** a legitimate writer starts setting ``target_schema_s3_key``
before the object exists (a deferred upload, say). At that point the key stops
being a claim and this contract has to change.

HOW THE RAISE COEXISTS WITH ``raise_for_read_client_error``
----------------------------------------------------------
``NoSuchKey``, ``NoSuchBucket``, ``404``, ``NotFound`` and
``ResourceNotFoundException`` are all in ``NOT_FOUND_CLIENT_ERROR_CODES``
(``shared/step_errors.py:54-62``), and the classifier **returns normally** for
them. That is not a conflict with the raise above: the classifier's contract
exists precisely so the **caller** decides what its not-found value is (its
docstring, ``:167-169``), and this caller's not-found value — for a key its own
record named — is a failure. So the raise below is this module's own, issued
*after* the classifier has returned. The classifier is neither modified nor
bypassed; it is used as designed.

HASHING
-------
``hashlib.sha256`` over the **raw object bytes**, matching
``schema_compare:384`` and ``schema_mapping:254``. Never the builtin ``hash()``,
which is randomised per process and would make the digest differ between the
invocation that wrote it and the one that reads it (``LGR-dd80c6d-1``).

NORMALISATION — A LIFT, NOT A SECOND DEFINITION
-----------------------------------------------
:func:`normalize_schema_format` was lifted verbatim (logic-for-logic) from
``schema_mapping/lambda_function.py:169-231``, because a resolver that returned
an un-normalised schema would hand its callers two different shapes depending on
what the user uploaded. p33 task 4.1 added this module with **no adopters**, so
for the duration of that one task the function existed twice. **Task 4.2 closed
that**: ``schema_mapping`` now imports this definition and no longer defines its
own. It could not simply drop the name — its fallback chain
(``get_uploaded_json_schema``) and its mapping path both still call it — so the
local ``def`` was replaced by the import and those call sites now resolve here.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

import boto3
from botocore.exceptions import ClientError

from step_errors import StepFailure, raise_for_read_client_error

logger = logging.getLogger(__name__)

#: Pipeline-record attribute names. Named constants because requirement 2.7
#: requires the half-written-record failure to name BOTH of them, and a typo in
#: a message is a diagnostic that sends the reader to the wrong attribute.
TARGET_SCHEMA_BUCKET_ATTR = "target_schema_bucket"
TARGET_SCHEMA_KEY_ATTR = "target_schema_s3_key"

#: Operation name threaded into ``raise_for_read_client_error``'s failure
#: details, so a classified failure says which read produced it.
READ_OPERATION = "resolve_target_schema"

_s3_client_cache: Any = None


def _s3_client() -> Any:
    """Return a process-wide cached boto3 S3 client.

    Lazy for the reason ``jdbc_helpers._glue_client`` is lazy: importing this
    module must not reach the AWS SDK, so unit tests can import it with no
    credentials and no network. Tests patch this helper (or reset the cache
    inside a moto context) to inject a client.
    """
    global _s3_client_cache
    if _s3_client_cache is None:
        _s3_client_cache = boto3.client("s3")
    return _s3_client_cache


def _record_attribute(pipeline_metadata: Mapping[str, Any], name: str) -> tuple[bool, str]:
    """Read one address attribute as ``(is_present, usable_value)``.

    ``is_present`` is whether the record carries the attribute **at all** with a
    non-``None`` value; ``usable_value`` is the stripped string, or ``""`` when
    the value cannot address an S3 object (empty, whitespace, or not a string).

    The two are kept apart on purpose. ``dynamo_typing.item_str`` collapses
    them — it returns ``""`` for "absent" and for "present but not a string"
    alike — and that collapse is what let the original divergence reach
    ``get_object(Bucket="")`` instead of failing where the record was wrong.
    """
    if name not in pipeline_metadata:
        return False, ""
    value = pipeline_metadata[name]
    if value is None:
        return False, ""
    if not isinstance(value, str):
        return True, ""
    return True, value.strip()


def normalize_schema_format(schema: dict[str, Any]) -> dict[str, Any]:
    """Normalise a schema to JSON-Schema form with ``properties``.

    Handles the JSON-Schema form, the custom ``fields``-array form, and an array
    schema wrapping either (the items schema is extracted). Anything else is
    returned unchanged for downstream to deal with.

    Lifted from ``schema_mapping/lambda_function.py:169-231`` — see the module
    docstring on why it lives here and why the copy there outlives task 4.1.
    """
    # An array schema wraps the real object schema in ``items``.
    if schema.get("type") == "array" and "items" in schema:
        logger.info("Extracting items schema from array type")
        schema = schema["items"]

    if "properties" in schema:
        return schema

    if "fields" in schema and isinstance(schema["fields"], list):
        properties: dict[str, Any] = {}
        required: list[str] = []

        for field in schema["fields"]:
            field_name = field.get("name")
            if not field_name:
                continue

            field_type = str(field.get("type", "string")).lower()
            type_mapping = {
                "integer": "integer",
                "int": "integer",
                "string": "string",
                "str": "string",
                "boolean": "boolean",
                "bool": "boolean",
                "number": "number",
                "float": "number",
                "date": "string",
                "datetime": "string",
                "timestamp": "string",
            }
            json_type = type_mapping.get(field_type, "string")

            properties[field_name] = {"type": json_type, "description": field.get("description", "")}

            if "format" in field:
                properties[field_name]["format"] = field["format"]

            if field.get("required", False):
                required.append(field_name)

        normalized: dict[str, Any] = {"type": "object", "properties": properties}
        if required:
            normalized["required"] = required

        logger.info(f"Converted fields array to JSON Schema format: {len(properties)} fields")
        return normalized

    logger.warning("Schema format not recognized, returning as-is")
    return schema


def resolve_target_schema(
    pipeline_metadata: Mapping[str, Any],
    step_name: str,
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve a pipeline's target schema from its record, as one pair.

    Args:
        pipeline_metadata: The pipeline record (a DynamoDB item). Both address
            attributes are read from **this** mapping; there is deliberately no
            bucket parameter (C1, ``LGR-7c9378d-1``).
        step_name: Canonical step name of the caller, so a classified failure is
            attributed to the step that failed — the convention at
            ``schema_compare:380`` and ``schema_mapping:110``.

    Returns:
        ``(normalized_schema, sha256_hex)``, or ``(None, None)`` when the record
        carries no ``target_schema_s3_key``. That is the ONLY input that returns
        the not-found value.

    Raises:
        StepFailure: the record is inconsistent (a key with no usable bucket, an
            unusable key, an object the key names that does not exist, or bytes
            that are not a JSON object), or the read failed permanently
            (access denied, ...).
        TransientStepFailure: the read failed for a retryable reason (throttle,
            service unavailable, ...), so the SFN Retry policy fires.
    """
    key_present, key = _record_attribute(pipeline_metadata, TARGET_SCHEMA_KEY_ATTR)
    # The bucket's presence flag is deliberately discarded: an absent bucket and
    # an unusable one are the same failure (requirement 2.7), because neither can
    # address the object the key claims exists.
    _, bucket = _record_attribute(pipeline_metadata, TARGET_SCHEMA_BUCKET_ATTR)

    if not key_present:
        # The pipeline genuinely has no target-schema object. The caller reports
        # "nothing verified" for this, and ONLY for this (requirement 2.6).
        logger.info("Pipeline record carries no %s — no target schema to resolve", TARGET_SCHEMA_KEY_ATTR)
        return None, None

    if not key:
        # Present but unusable: empty, whitespace, or not a string. Reachable
        # from a `schema_location` of "s3://bucket/" — `pipelines_crud.py:763`
        # splits that into ["bucket", ""], len 2, so the pair is written with an
        # empty key. That is the mirror image of the half-written record
        # requirement 2.7 already says must fail, so it fails the same way
        # rather than being read as "no target schema".
        raise StepFailure(
            step_name,
            f"Pipeline record has {TARGET_SCHEMA_KEY_ATTR} "
            f"({pipeline_metadata[TARGET_SCHEMA_KEY_ATTR]!r}) but it does not address an S3 object, "
            f"and {TARGET_SCHEMA_BUCKET_ATTR} is {pipeline_metadata.get(TARGET_SCHEMA_BUCKET_ATTR)!r} — "
            "the pipelines-table record is incomplete.",
            details={"operation": READ_OPERATION, "reason": "unusable_target_schema_key"},
        )

    if not bucket:
        # A key with no bucket is a half-written record, not an absent schema:
        # `create_pipeline` writes both or neither. Naming both attributes is
        # requirement 2.7's actual demand — the reader has to know which
        # attribute is missing AND which one made it load-bearing. An empty
        # string is rejected here too: `Invalid bucket name ""` is the exact
        # live failure `LGR-7c9378d-1` was written for.
        raise StepFailure(
            step_name,
            f"Pipeline record has {TARGET_SCHEMA_KEY_ATTR} ({key!r}) but no usable "
            f"{TARGET_SCHEMA_BUCKET_ATTR} ({pipeline_metadata.get(TARGET_SCHEMA_BUCKET_ATTR)!r}) — cannot read "
            "the target schema. The pipelines-table record is incomplete.",
            details={"operation": READ_OPERATION, "reason": "missing_target_schema_bucket"},
        )

    logger.info(f"Resolving target schema from s3://{bucket}/{key}")
    try:
        response = _s3_client().get_object(Bucket=bucket, Key=key)
        raw: bytes = response["Body"].read()
    except ClientError as exc:
        # Classifier first: a throttle becomes TransientStepFailure, a permission
        # problem becomes StepFailure, and it RETURNS for the not-found codes.
        raise_for_read_client_error(exc, step_name, operation=READ_OPERATION)
        # Reached only for a not-found code, i.e. the record names an object that
        # is not there. That is a dangling reference, not an absent schema
        # (D14/C16) — see the module docstring for why this is the resolver's own
        # raise and not a violation of the classifier's contract.
        error_code = getattr(exc, "response", {}).get("Error", {}).get("Code", "Unknown")
        raise StepFailure(
            step_name,
            f"Pipeline record names a target schema at s3://{bucket}/{key} but the object does not exist "
            f"({error_code}). {TARGET_SCHEMA_BUCKET_ATTR} and {TARGET_SCHEMA_KEY_ATTR} are written together, "
            "so a key pointing at nothing is an inconsistent record — not a pipeline without a target schema.",
            details={
                "operation": READ_OPERATION,
                "reason": "target_schema_object_missing",
                "error_code": error_code,
                "bucket": bucket,
                "key": key,
            },
        ) from exc

    schema_hash = hashlib.sha256(raw).hexdigest()

    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError both
        raise StepFailure(
            step_name,
            f"Target schema at s3://{bucket}/{key} is not readable JSON: {exc}",
            details={"operation": READ_OPERATION, "reason": "target_schema_unparseable"},
        ) from exc

    if not isinstance(parsed, dict):
        raise StepFailure(
            step_name,
            f"Target schema at s3://{bucket}/{key} is a JSON {type(parsed).__name__}, not an object — "
            "a schema must be a JSON object.",
            details={"operation": READ_OPERATION, "reason": "target_schema_not_an_object"},
        )

    schema = normalize_schema_format(parsed)
    logger.info(f"Resolved target schema {key} (hash: {schema_hash})")
    return schema, schema_hash


# ---------------------------------------------------------------------------
# WHERE AN UPLOADED TARGET SCHEMA LIVES — one definition, two callers
# ---------------------------------------------------------------------------
#
# The pair above is only unsplittable once something WRITES it. Nothing did for
# the existing-schema path: ``create_pipeline`` wrote
# ``target_schema_bucket`` / ``target_schema_s3_key`` exclusively from the
# ``schema_location`` the wizard echoes back from an inline ``/schemas/upload``,
# and the wizard only performs that upload when NO existing schema was chosen
# (``CreatePipelinePage.tsx:309-313``). Selecting an existing schema therefore
# produced a record carrying ``target_schema_name`` and no address at all, so
# :func:`resolve_target_schema` returned ``(None, None)`` and p33's gate reported
# ``schema_compliance`` as ``verified: False`` / ``no_target_schema`` for a
# pipeline whose schema object exists. Two live records show it:
# ``p32ver-ren-renc-6dbdcdf6`` and ``p32obs-ren-renb-41466288``.
#
# The second half of the same defect: ``create_pipeline`` GUESSED the key from
# the id's prefix — ``schemas/uploaded/{name}.json`` for an ``uploaded_`` id,
# ``uploaded_schemas/target_{name}.json`` for a ``schema_`` id, else
# ``schemas/{name}.json``. ``upload_schema`` writes
# ``shared/{name}_{timestamp}.json`` and returns ``schemaId`` as
# ``{name}_{timestamp}`` (``handlers/schemas.py:322-334``), and ``get_schemas``
# derives an existing schema's id by stripping exactly ``shared/`` and ``.json``
# (``:81-92``) — so ``shared/{id}.json`` is the address for BOTH id shapes and
# none of the three guesses ever matched either.
#
# ``get_uploaded_json_schema`` (``schema_mapping/lambda_function.py:273``) was
# already the de facto authority: six candidate keys, ``shared/`` among them,
# which is why the mapping step kept working while the pipeline record stayed
# addressless. That list now lives here and that function consumes it, so the
# writer and the reader compose the address from one definition rather than two.
# This is ``LGR-eee8ac8-5``'s situation verbatim — two callers that must agree on
# how an address is composed is exactly where a second definition silently
# disables a feature instead of failing. It does NOT reopen p33 D5: D5 declined
# to converge READ sites that were already correct (``schema_compare``) or whose
# requirements were unanswered (``create_infrastructure``); it never spoke to the
# writer, and the writer is the half that was wrong.
#
# NOT a seventh naming convention. The six candidates below are lifted
# verbatim, in order, from that function.

#: Operation name for :func:`locate_uploaded_schema`'s failure details.
LOCATE_OPERATION = "locate_uploaded_schema"


def uploaded_schema_candidate_keys(schema_name: str, pipeline_id: str | None = None) -> list[str]:
    """Every key an uploaded target schema named ``schema_name`` may occupy.

    Ordered most- to least-preferred, lifted verbatim from
    ``schema_mapping.get_uploaded_json_schema``, which is the authority this
    aligns with rather than adding to:

    1. ``{pipeline_id}/{schema_name}.json`` — pipeline-scoped (only when a
       ``pipeline_id`` is supplied)
    2. ``shared/{schema_name}.json`` — where ``upload_schema`` actually writes
       and where ``get_schemas`` derives ids from
    3. ``schemas/uploaded/{schema_name}.json`` — legacy
    4. ``uploaded_schemas/target_{schema_name}.json`` — legacy
    5. ``schemas/{schema_name}.json`` — legacy
    6. ``{schema_name}.json`` — legacy, bucket root

    Args:
        schema_name: The schema id / object basename, without ``.json``.
        pipeline_id: When given, prepends the pipeline-scoped candidate.

    Returns:
        Candidate keys in probe order. Never empty.
    """
    keys: list[str] = []
    if pipeline_id:
        keys.append(f"{pipeline_id}/{schema_name}.json")
    keys.extend(
        [
            f"shared/{schema_name}.json",
            f"schemas/uploaded/{schema_name}.json",
            f"uploaded_schemas/target_{schema_name}.json",
            f"schemas/{schema_name}.json",
            f"{schema_name}.json",
        ]
    )
    return keys


def locate_uploaded_schema(
    bucket: str,
    schema_name: str,
    pipeline_id: str | None = None,
    *,
    s3_client: Any = None,
    step_name: str = "locate_uploaded_schema",
) -> str | None:
    """Return the key of the first candidate that exists, or ``None``.

    ``HeadObject`` per candidate, so this costs no object bytes and can be run
    at creation time to establish the address that gets written onto the
    pipeline record.

    **A missing object and a failed read are not the same answer here**, which
    is the whole point of not reusing ``get_uploaded_json_schema``'s loop: its
    per-candidate ``except Exception: continue``
    (``schema_mapping/lambda_function.py:314-316``) swallows a throttle on all
    six candidates and then reports "Could not find uploaded schema ... in any
    expected location" — an infrastructure error asserting the opposite of what
    happened. That defect is a known, deliberately-unfixed exception to p33
    requirement 2.8, fenced by a standing ``xfail(strict=True)`` at
    ``tests/unit/lambdas/schema_mapping/test_get_target_schema_characterization.py``;
    it is left exactly as it is, and NOT reproduced here. Here a not-found code
    advances to the next candidate and everything else is classified by
    ``raise_for_read_client_error`` and raised on the spot.

    Args:
        bucket: Target-schemas bucket to probe.
        schema_name: Schema id / object basename, without ``.json``.
        pipeline_id: Optional pipeline-scoped candidate (see
            :func:`uploaded_schema_candidate_keys`).
        s3_client: Client to probe with. Defaults to this module's cached
            client; callers with their own (the dashboard API builds a
            region-pinned one) pass it in.
        step_name: Attribution for a classified failure.

    Returns:
        The first existing candidate key, or ``None`` when every candidate
        returned a genuine not-found. ``None`` means "no such object", never
        "the read did not work".

    Raises:
        StepFailure: the read failed permanently (access denied, ...).
        TransientStepFailure: the read failed for a retryable reason.
    """
    client = s3_client if s3_client is not None else _s3_client()
    for key in uploaded_schema_candidate_keys(schema_name, pipeline_id):
        try:
            client.head_object(Bucket=bucket, Key=key)
        except ClientError as exc:
            # Classifier first: a throttle or an AccessDenied raises out of the
            # loop rather than being counted as "not at this key".
            raise_for_read_client_error(exc, step_name, operation=LOCATE_OPERATION)
            logger.debug("No target schema at s3://%s/%s", bucket, key)
            continue
        logger.info("Located uploaded target schema at s3://%s/%s", bucket, key)
        return key
    logger.info(
        "No uploaded target schema named %r in s3://%s — probed %s",
        schema_name,
        bucket,
        uploaded_schema_candidate_keys(schema_name, pipeline_id),
    )
    return None
