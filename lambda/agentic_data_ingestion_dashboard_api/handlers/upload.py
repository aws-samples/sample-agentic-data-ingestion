#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Handler for file upload operations"""

import json
import os
import sys
from typing import Any

import boto3
from botocore.config import Config as BotoConfig

from .common import create_response, logger

# Add shared directory to path for imports
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))

# Import shared env_utils via the Layer_Import_Fallback (fix path + import the
# canonical resolver — do not redefine it inline; P21 Req 5.3/5.4/5.7).
try:
    from env_utils import get_aws_account_id, require_env
except ImportError:
    sys.path.append("/opt/python")  # Lambda layer path
    sys.path.append(os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"))
    from env_utils import get_aws_account_id, require_env


# p64 hotfix — ONE expiry for both presigns in `presign_upload`, deliberately not
# two literals.
#
# The POST URL and the confirmation URL are minted together at the *start* of an
# upload, but the confirmation is fetched at the *end* of it. A 500 MB file over a
# slow link can be in flight for a long time, so a confirmation URL with a shorter
# expiry than the POST it confirms would start returning 403 on exactly the large
# uploads most worth confirming — the same "confirmation can only ever fail"
# outage this hotfix exists to remove, wearing a different hat. Sharing one
# constant makes that skew unrepresentable rather than merely unlikely.
#
# Do NOT "harden" this by shortening the confirmation's expiry alone.
_PRESIGN_EXPIRY_SECONDS = 3600  # 1 hour

# Initialize clients
s3_client = boto3.client("s3")

# p64 hotfix — a SECOND S3 client, existing only to mint the confirmation URL,
# and explicitly configured. Two reasons it cannot be `s3_client` above.
#
# 1. **SigV4 must be asked for explicitly.** A default `boto3.client("s3")`
#    reports `meta.config.signature_version == "s3v4"` and still presigns
#    **SigV2** (`AWSAccessKeyId`/`Signature`/`Expires`). Measured on botocore
#    1.43.33: only an explicit `Config(signature_version="s3v4")` produces
#    `X-Amz-Signature`. SigV2 presigned URLs are rejected outright by S3 in every
#    region launched after January 2014, so the default would hand a deploy in
#    such a region a confirmation URL that always fails.
# 2. **Blast radius.** `addressing_style: "virtual"` yields the *regional*
#    virtual-host endpoint, avoiding a 307 redirect on a cross-origin browser
#    HEAD. Setting either option on the shared client would change the `url` and
#    `fields` that `generate_presigned_post` returns — the contract the upload
#    itself rides on. A separate client leaves that byte-identical.
#
# Region is read here rather than inside the handler because a client is bound to
# one at construction; the handler's own `AWS_REGION` read stays the source of
# truth for the *bucket name*, and the two agree in Lambda where the variable is
# always set.
_confirm_s3_client = boto3.client(
    "s3",
    region_name=os.environ.get("AWS_REGION", "us-west-2"),
    config=BotoConfig(signature_version="s3v4", s3={"addressing_style": "virtual"}),
)

dynamodb = boto3.resource("dynamodb")
pipelines_table = dynamodb.Table(require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"))


def _confirmation_url(bucket: str, key: str) -> str | None:
    """A signed HEAD URL the browser can use to confirm `key` landed in `bucket`.

    **Why this exists at all.** p64 task 1d.5 shipped a landing confirmation that
    fetched the bare object URL anonymously (`objectUrlFor` +
    `fetch(url, {method: 'HEAD'})`, no credentials). The source bucket is
    `BlockPublicAccess.BLOCK_ALL` with no `AnyPrincipal` statement anywhere in
    `deploy-cdk/lib/`, so that request returned **403 for every object, present or
    absent** — S3 answers 403 rather than 404 to an unauthorized caller precisely so
    keys cannot be enumerated. The check could not ever have passed, and it turned
    successful uploads into a failure message.

    The reasoning that shipped it cited the bucket's CORS block
    (`foundation-stack.ts:75-82`, `allowedMethods` including `HEAD`,
    `allowedOrigins: ['*']`) and phase 0 task 0.2's verification of the deployed
    configuration. That verification was correct and **irrelevant**: CORS decides
    whether a browser will expose a response to JavaScript, and grants no S3
    authorization whatsoever. Do NOT reintroduce an unsigned confirmation on CORS
    grounds — and do NOT "fix" the 403 by relaxing `blockPublicAccess` or adding a
    public read policy. The 403 was correct; the caller was wrong to be anonymous.

    A signed URL keeps the check in the browser, so it adds no route — task 1d.3
    collapsed this surface to exactly one presign route and `presign_upload`'s
    docstring carries the do-NOT against a second (`LGR-4863153-1`). It also keeps
    the check out of Lambda entirely, respecting `LGR-b46a5bb-1`.

    Authorization comes from the *signer's* role, not the browser's: this Lambda
    runs as `foundation.infrastructureRole` (`compute-stack.ts:1030`), whose
    `s3ReadWrite` statement grants `s3:GetObject` (which is what a HeadObject
    call authorizes against) on
    `arn:aws:s3:::adi-*-{account}-{region}/*` (`foundation-stack.ts:241-244`, wired
    in at `:445-446`) — which covers the source bucket. No CDK change is required,
    and none should be made.

    **Returns `None` rather than raising.** A confirmation is a guard, not a gate
    (`LGR-4863153-2`: it exists so a silent no-op becomes visible). If the URL
    cannot be minted, the honest outcome is an upload that proceeds and reports
    itself *unconfirmed* — never an upload that is blocked because its optional
    guard could not be built. Blocking here would recreate the outage this function
    is fixing, from the other direction.
    """
    try:
        return _confirm_s3_client.generate_presigned_url(
            "head_object",
            Params={"Bucket": bucket, "Key": key},
            ExpiresIn=_PRESIGN_EXPIRY_SECONDS,
        )
    # Broad on purpose — never block an upload on its own optional guard.
    except Exception as e:
        # Loud in the log, non-fatal to the caller. The frontend renders its own
        # "could not be verified" notice, so this is not silent to an operator
        # either.
        logger.error(f"Could not mint a confirmation URL for s3://{bucket}/{key}: {e}")
        return None


#: Longest `file_name` accepted, in UTF-8 bytes. 255 bytes is the per-name
#: limit of ext4 and most Unix filesystems, and it keeps
#: `{pipeline_id}/{file_name}` well under S3's 1024-byte key limit. NTFS counts
#: 255 UTF-16 units instead, so a long non-Latin Windows file name can exceed
#: it; such a name is refused with a message naming the limit, never truncated.
_MAX_FILE_NAME_BYTES = 255


def _upload_key_refusal(pipeline_id: object, file_name: object) -> str | None:
    """Why `{pipeline_id}/{file_name}` cannot be signed as an upload key, or None.

    **Why the key needs guarding.** The source bucket sends its events to
    EventBridge, and an object landing in it starts a run of the pipeline its
    key names: `pipeline_trigger._pipeline_prefix` takes everything before the
    LAST `/` as the pipeline. A `/` inside `file_name` therefore changes which
    pipeline the upload is read as belonging to (`a/b.csv` is read as pipeline
    `{pipeline_id}/a`, and `../x.csv` as `{pipeline_id}/..`), which is a
    traversal out of the prefix the existence check approved.

    **A denylist, deliberately not an allowlist.** The dashboard sends the
    browser's raw `File.name` (`FileUploadModal.tsx`), so names with spaces,
    brackets, accented or non-Latin letters and several dots are ordinary input
    and must keep working. Only what can change which prefix the key falls
    under, or what no file name can legitimately contain, is refused:
    `/` and `\\` (the second because some clients and tools read it as a
    separator), control characters (code point below 0x20, and 0x7F), the
    names `.` and `..`, and anything over `_MAX_FILE_NAME_BYTES` in UTF-8.

    **Refuse, never repair.** Stripping or substituting would sign a key the
    caller did not ask for, and the response's `key` would then disagree with
    the name the user picked. A 400 naming the rule is the honest outcome.

    `pipeline_id` is held to "a string with no `/`" as defence in depth. The
    existence check in `presign_upload` already refuses any id that names no
    pipeline, but this refusal does not depend on the pipelines table never
    holding an id with a `/` in it.
    """
    if not isinstance(pipeline_id, str):
        return "pipeline_id must be a string"
    if "/" in pipeline_id:
        return "pipeline_id must not contain '/'"
    if not isinstance(file_name, str):
        return "file_name must be a string"
    if "/" in file_name or "\\" in file_name:
        return "file_name must not contain '/' or '\\'"
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in file_name):
        return "file_name must not contain control characters"
    if file_name in (".", ".."):
        return "file_name must not be '.' or '..'"
    try:
        encoded_length = len(file_name.encode("utf-8"))
    except UnicodeEncodeError:
        # A lone surrogate (`"\ud800"` survives `json.loads`) has no UTF-8 form,
        # so S3 could not store it as a key either. Refused here rather than left
        # to raise, which the outer `except` would report as a server error.
        return "file_name must be valid Unicode text"
    if encoded_length > _MAX_FILE_NAME_BYTES:
        return f"file_name must be at most {_MAX_FILE_NAME_BYTES} bytes when UTF-8 encoded"
    return None


def _resolve_upload_bucket() -> str | None:
    """The source bucket uploads land in, or ``None`` when it cannot be resolved.

    Shared by `presign_upload`, which signs keys in it, and `trigger_pipeline`,
    which refuses an object outside it, so the two cannot disagree about which
    bucket that is. Each caller answers ``None`` with its own fail-closed 500.
    """
    # `UPLOAD_BUCKET` **is** set on this function — `compute-stack.ts:1047`
    # sets it in the dashboard API's own `environment` block, from
    # `foundation.sourceBucket.bucketName`. So `os.environ.get` is the live
    # path and the name-derivation below is a genuine fallback rather than
    # the only route. The two agree by construction: foundation-stack
    # declares that bucket as `adi-source-files-{account}-{region}`, which
    # is exactly what the derivation composes — verified against the live
    # value in the validation account, where the two are byte-identical (task 0.2).
    #
    # KEEP the fallback. `LGR-4ea43f3-7` (template / ratified) settled this
    # identical question for the sibling bucket: `PROCESSED_BUCKET` was set
    # on `schema_extract` and its name-derivation was deliberately kept,
    # **explicitly rejecting removal**, because the fallback is what keeps a
    # fail-closed refusal reachable and what covers "an individual deploy
    # that lands this code before the variable". Per-stack and
    # `--exclusively` deploys are the documented norm in this repo
    # (`.kiro/steering/project-overview.md`), so that is a live case rather
    # than a hypothetical. R4.5's wording — read the variable "rather than
    # reconstructing" it — reads like a deletion; deleting it would undo
    # precisely what that record preserved.
    #
    # Do NOT reach for `require_env` here, despite it being the canonical
    # resolver this module already imports. Its `default` applies **only**
    # when `AGENTIC_DATA_INGESTION_ENV=local` (`env_utils.py:30`), so in
    # Lambda an unset variable RAISES rather than falling back — the same
    # removal the record rejected, wearing a helper's clothes.
    region = os.environ.get("AWS_REGION", "us-west-2")
    upload_bucket = os.environ.get("UPLOAD_BUCKET")
    if not upload_bucket:
        # Reached only on the fallback arm, which is why `get_aws_account_id`
        # is called here and not above: it resolves via STS when
        # `AWS_ACCOUNT_ID` is unset (`env_utils.py:17`), so the deployed
        # happy path now skips a network round-trip it used to make on every
        # request.
        account_id = get_aws_account_id()
        if account_id and region:
            upload_bucket = f"adi-source-files-{account_id}-{region}"
    return upload_bucket or None


def presign_upload(event: dict[str, Any]) -> dict[str, Any]:
    """Generate a presigned POST URL for file upload with progress tracking.

    **The only presign handler.** p64 task 1d.3 deleted its former sibling,
    `generate_upload_url`, which presigned a PUT and read `pipeline_key` where
    this one reads `pipeline_id`. Two presign handlers with incompatible body
    contracts is what let `/upload-file` stay wrong long enough to be documented
    wrong in the frontend too (`api-endpoints.ts`, amended by 1d.2): three routes
    reached the two of them, and the `upload` key's own `else` arm handed every
    non-`presign` subpath to the other handler, which answered a 400 about a
    field the caller had never been asked for. `LGR-4854b3d-1` and
    `LGR-e59edd7-6` (both template / ratified, cited via `LGR-78540f6-1`) hold
    that two surfaces phrasing one fact will diverge; these two had.

    Do NOT add a second presign handler here. If a PUT-presign is ever wanted
    again, it belongs behind its own route with its own test, not beside this one
    as a fall-through — `router.upload_router` carries that fence.

    Uses generate_presigned_post (POST) instead of generate_presigned_url (PUT)
    to support FormData-based uploads with XMLHttpRequest progress events.

    Response carries `url`, `fields`, `key` and `confirm_url` — the last being a
    signed HEAD URL for the R4.4 landing check (see `_confirmation_url`). It is
    `None` when it could not be minted, and the client must treat that as
    "unconfirmed", never as "confirmed". Adding it here rather than behind a new
    route is deliberate: 1d.3's do-NOT below forbids a second surface.

    Expected body:
    {
        "pipeline_id": "pipeline-key",
        "file_name": "data.csv"
    }

    Refusals, all before anything is signed: 400 when either field is missing
    or fails `_upload_key_refusal`, and 404 `Pipeline not found: {pipeline_id}`
    when the pipelines table has no such record. The signed key is exactly
    `f"{pipeline_id}/{file_name}"`, never a cleaned-up version of it.
    """
    try:
        # `or "{}"` rather than `get("body", "{}")`: a real API Gateway proxy
        # event for a body-less request carries `"body": None`, so the default
        # never fires and `json.loads(None)` raises TypeError — which the outer
        # `except` then reported as `500 Failed to generate presigned upload
        # URL`. A request that sent no body is a client error, and answering 500
        # blamed the server for it. The fix routes it into the 400 below, which
        # `tests/contracts/api_responses/test_presign_contract.py`'s
        # `test_presign_missing_body_returns_400` already *claimed* to cover —
        # its event sends `json.dumps({})`, a body-less body, so the shape its
        # own name describes was asserted but never exercised. Do NOT restore
        # `get("body", "{}")`; it only defends against the key being absent,
        # which is not how the gateway spells "no body".
        body = json.loads(event.get("body") or "{}")
        pipeline_id = body.get("pipeline_id")
        file_name = body.get("file_name")

        if not pipeline_id or not file_name:
            return create_response(400, {"error": "pipeline_id and file_name are required"})

        refusal = _upload_key_refusal(pipeline_id, file_name)
        if refusal:
            return create_response(400, {"error": refusal})

        # The pipeline must exist before anything is signed. An object written
        # under `{pipeline_id}/` starts that pipeline (see `_upload_key_refusal`),
        # so signing for an id the table does not hold let any caller write under
        # an arbitrary prefix. Same 404 shape as `pipelines_crud`'s
        # `get_pipeline_details` and `delete_pipeline`, which already read this
        # table with GetItem from this Lambda, so no IAM change is needed. Only a
        # missing item is a 404: a read error (throttle, access denied) raises
        # into the `except` below and answers 500, the rule P28 R17.3 set for
        # `get_pipeline_details`. `ConsistentRead` so a pipeline created a moment
        # ago is not refused.
        pipeline_record = pipelines_table.get_item(Key={"pipeline_id": pipeline_id}, ConsistentRead=True)
        if "Item" not in pipeline_record:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_id}"})

        upload_bucket = _resolve_upload_bucket()
        if not upload_bucket:
            # FAIL CLOSED. This is the refusal the fallback keeps reachable, and
            # the reason it is worth keeping: `generate_presigned_post` signs a
            # URL against any bucket name it is handed, existent or not, so an
            # unresolved name yields a 200 with a URL that fails opaquely at
            # upload time — a success report for an upload that cannot land.
            # That is the silent-no-op family R4.4 exists to close, caught one
            # step earlier where it can still name its own cause.
            logger.error(
                "Cannot resolve an upload bucket: UPLOAD_BUCKET is unset and the account/region derivation failed"
            )
            return create_response(
                500,
                {
                    "error": "Upload bucket is not configured",
                    "details": "Set UPLOAD_BUCKET on this function.",
                },
            )

        key = f"{pipeline_id}/{file_name}"

        presigned = s3_client.generate_presigned_post(
            Bucket=upload_bucket,
            Key=key,
            ExpiresIn=_PRESIGN_EXPIRY_SECONDS,
            Conditions=[
                ["content-length-range", 1, 500 * 1024 * 1024],  # 1 byte to 500 MB
            ],
        )

        return create_response(
            200,
            {
                "url": presigned["url"],
                "fields": presigned["fields"],
                "key": key,
                "confirm_url": _confirmation_url(upload_bucket, key),
            },
        )

    except Exception as e:
        logger.error(f"Error in presign_upload: {e}")
        return create_response(500, {"error": "Failed to generate presigned upload URL", "details": str(e)})


def _trigger_object_refusal(pipeline_key: str, s3_uri: str, upload_bucket: str) -> str | None:
    """Why `trigger_pipeline` may not start `pipeline_key` on `s3_uri`, or None.

    The object must be `s3://{upload_bucket}/{pipeline_key}/{file_name}`: in the
    bucket whose uploads start runs, directly under the pipeline's own prefix,
    with a key `presign_upload` could have signed (`_upload_key_refusal`). So the
    keys accepted here are exactly the keys an upload can create. Nothing is
    trimmed, decoded or lowered, as there.
    """
    # Checked first so the refusal names this route's field. A `/` in the key
    # would also move the prefix `pipeline_trigger._pipeline_prefix` derives.
    if "/" in pipeline_key:
        return "pipeline_key must not contain '/'"
    prefix = f"s3://{upload_bucket}/{pipeline_key}/"
    if not s3_uri.startswith(prefix):
        return f"s3_uri must name an object directly under s3://{upload_bucket}/{pipeline_key}/"
    file_name = s3_uri[len(prefix) :]
    if not file_name:
        return "s3_uri must name an object, not the pipeline's prefix"
    refusal = _upload_key_refusal(pipeline_key, file_name)
    return f"s3_uri does not name an uploadable object: {refusal}" if refusal else None


def trigger_pipeline(event: dict[str, Any]) -> dict[str, Any]:
    """Start a run of one S3-upload pipeline for one object it owns.

    Expected body:
    {
        "pipeline_key": "pipeline-id",
        "s3_uri": "s3://<upload bucket>/<pipeline-id>/<file name>"
    }

    No dashboard code calls this route: an upload starts its run through the
    source bucket's EventBridge rule, and Run Now has its own route.

    **Refusals, all before a run is started.** This route used to start a run
    for any `pipeline_key` and any `s3_uri`, so a caller could run a pipeline
    that does not exist, an archived one or a JDBC one, on any object the
    workflow's roles could read (threat model T-13's residual).

    * 400 when a field is missing or not a string, or `s3_uri` fails
      `_trigger_object_refusal`.
    * 404 `Pipeline not found: {pipeline_key}` when the pipelines table has no
      such record, the shape `presign_upload` answers.
    * 409 when the pipeline is archived, or its `source_type` is not
      `S3_UPLOAD`. Run Now (`pipelines_runs.run_now`) answers the archived case
      the same way, and serves JDBC pipelines.
    * 500 when the upload bucket cannot be resolved or the read fails. Only a
      missing item is a 404, the rule P28 R17.3 set for `get_pipeline_details`.

    **Still not enforced here.** The run is started with `StartExecution`
    directly, so it takes no pipeline lock and skips the duplicate-content gate
    and the global concurrency limit that `pipeline_trigger` applies to an
    upload and to Run Now.
    """
    try:
        # `or "{}"`: a body-less request carries `"body": None`, which
        # `json.loads` rejects with TypeError; see `presign_upload`.
        body = json.loads(event.get("body") or "{}")
        pipeline_key = body.get("pipeline_key")
        s3_uri = body.get("s3_uri")

        if not pipeline_key or not s3_uri:
            return create_response(400, {"error": "pipeline_key and s3_uri are required"})

        if not isinstance(pipeline_key, str) or not isinstance(s3_uri, str):
            return create_response(400, {"error": "pipeline_key and s3_uri must be strings"})

        upload_bucket = _resolve_upload_bucket()
        if not upload_bucket:
            # Fail closed: without the bucket there is nothing to hold s3_uri to.
            logger.error(
                "Cannot resolve an upload bucket: UPLOAD_BUCKET is unset and the account/region derivation failed"
            )
            return create_response(
                500,
                {"error": "Upload bucket is not configured", "details": "Set UPLOAD_BUCKET on this function."},
            )

        refusal = _trigger_object_refusal(pipeline_key, s3_uri, upload_bucket)
        if refusal:
            return create_response(400, {"error": refusal})

        # Same read as `presign_upload`: GetItem, consistent, from this Lambda's
        # existing grant. A read error raises into the outer `except` (500).
        pipeline = pipelines_table.get_item(Key={"pipeline_id": pipeline_key}, ConsistentRead=True).get("Item")
        if not pipeline:
            return create_response(404, {"error": f"Pipeline not found: {pipeline_key}"})

        if pipeline.get("status") == "archived":
            return create_response(409, {"error": f"Pipeline {pipeline_key} is archived and cannot be run"})

        # A record without `source_type` predates the discriminator and is an
        # upload pipeline, the default `run_now` applies too.
        source_type = pipeline.get("source_type", "S3_UPLOAD")
        if source_type != "S3_UPLOAD":
            return create_response(
                409,
                {
                    "error": (
                        f"Pipeline {pipeline_key} has source_type {source_type!r}; this route starts S3 upload "
                        f"runs only. Use POST /api/pipelines/{pipeline_key}/run."
                    )
                },
            )

        # Trigger Step Functions workflow
        try:
            sfn_client = boto3.client("stepfunctions")

            # Get state machine ARN
            region = os.environ.get("AWS_REGION", "us-west-2")
            account_id = get_aws_account_id()
            state_machine_name = os.environ.get("STATE_MACHINE_NAME", "agentic-data-ingestion-pipeline")
            state_machine_arn = f"arn:aws:states:{region}:{account_id}:stateMachine:{state_machine_name}"

            # file_validation expects { source_bucket, source_key }, not s3_uri.
            # `_trigger_object_refusal` has already held s3_uri to exactly
            # `s3://{upload_bucket}/{pipeline_key}/{file_name}`.
            source_bucket = upload_bucket
            source_key = s3_uri[len(f"s3://{upload_bucket}/") :]

            # P28 R9.2: build the execution input via the shared trigger
            # contract. pipeline_key doubles as the pipeline_id (the upload
            # prefix); file_validation derives the same value from the S3 key.
            from rerun_contract import build_trigger_input

            try:
                execution_input = build_trigger_input(
                    {"pipeline_id": pipeline_key, "source_type": "S3_UPLOAD"},
                    triggered_by="manual_upload",
                    source_bucket=source_bucket,
                    source_key=source_key,
                )
            except ValueError as exc:
                return create_response(400, {"error": f"Invalid trigger input: {exc}"})

            import time

            execution_name = f"upload-{pipeline_key.replace('/', '-')}-{int(time.time())}"

            response = sfn_client.start_execution(
                stateMachineArn=state_machine_arn, name=execution_name, input=json.dumps(execution_input)
            )

            logger.info(f"Started Step Functions execution: {execution_name}")

            return create_response(
                200,
                {
                    "success": True,
                    "message": "Pipeline triggered successfully",
                    "execution_arn": response["executionArn"],
                    "execution_name": execution_name,
                },
            )

        except Exception as e:
            logger.error(f"Error triggering Step Functions: {e}")
            return create_response(500, {"error": "Failed to trigger pipeline", "details": str(e)})

    except Exception as e:
        logger.error(f"Error in trigger_pipeline: {e}")
        return create_response(500, {"error": "Failed to process trigger request", "details": str(e)})
