# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Processed_File_Ledger — content-level idempotency for S3 sources (p30 R9.1, R9.2).

The per-pipeline lock in ``pipeline_trigger`` serialises runs; it does not stop
the *same content* being ingested twice, because ``_execution_name`` embeds a
timestamp on purpose (Step Functions rejects a reused name even after the
execution finishes, so re-uploads need fresh names). Against an append-mode
target a re-upload therefore duplicates rows. This ledger is the content-level
gate the lock cannot be.

WHY THIS MODULE EXISTS RATHER THAN INLINE CODE IN EITHER LAMBDA
--------------------------------------------------------------
The ledger is a **cross-Lambda contract**: the row is written by
``job_completion`` (the only place terminal write success is known) and read by
``pipeline_trigger`` (the only place a run can still be prevented). Two Lambdas
that must agree byte-for-byte on the fingerprint's composition and the row's key
is exactly the situation where a second, subtly different definition silently
disables the feature — the check would simply never match and nothing would
fail. One definition, imported by both.

FINGERPRINT COMPOSITION — object size plus normalised S3 ETag
-------------------------------------------------------------
``sha256(f"{size}:{normalized_etag}")``, hex, 64 chars. The pre-image is stored
alongside the row (``content_size``, ``content_etag``) so a fingerprint is
diagnosable without recomputing it. sha256 rather than the builtin ``hash()``
for the reason recorded in ``LGR-dd80c6d-1``: the builtin is randomised per
process, which would make the digest differ between the writing invocation and
the reading one.

Normalisation is load-bearing, because the three places the repo can read an
ETag do not agree on its spelling:

===============================  ==========================  =================
Source                           ETag form                   Size field
===============================  ==========================  =================
``head_object`` (file_validation) ``'"d41d8c…"'`` (quoted)   ``ContentLength``
``list_objects_v2`` (trigger)     ``'"d41d8c…"'`` (quoted)   ``Size``
EventBridge S3 detail (trigger)   ``'d41d8c…'`` (bare)       ``object.size``
===============================  ==========================  =================

:func:`normalize_etag` strips surrounding quotes and whitespace and lowercases,
so all three produce one fingerprint for one object. ``test_fingerprint_
composition.py`` pins that equivalence; if it ever breaks, the ledger stops
matching and the only symptom is duplicates quietly getting through.

THE MULTIPART LIMITATION, RECORDED RATHER THAN DISCOVERED LATER
---------------------------------------------------------------
For a multipart upload S3's ETag is **not** the MD5 of the object. It is a hash
of the part hashes with a ``-N`` suffix, and it depends on the part size the
*uploader* chose. The same bytes uploaded by two clients — or by one client with
different multipart thresholds — therefore carry **different** ETags and produce
**different** fingerprints.

The consequence is a **missed skip, never a wrong skip**:

* A genuine duplicate uploaded a different way will NOT be recognised. The run
  proceeds, exactly as it does today. The ledger's value is reduced for
  multipart traffic; nothing regresses.
* A *false* skip would require two objects with different content to share both
  a byte-exact size and an identical ETag string. For single-part objects the
  ETag is the content MD5, so this needs an MD5 collision at a fixed length; for
  multipart objects it needs a collision in the hash-of-part-hashes at a fixed
  length *and* the same part layout. Neither is reachable by accident, so the
  dangerous direction is not in play.

That asymmetry is why a missing or unusable fingerprint is treated as
"no fingerprint, no dedup, proceed" everywhere in this module rather than as an
error: every failure mode is steered into the harmless direction.

WHAT IS OUT OF SCOPE HERE
-------------------------
JDBC sources have no file, so they have no fingerprint and no ledger row. The
ledger is an S3-content mechanism only; a JDBC pipeline is unaffected by it.
"""

from __future__ import annotations

import contextlib
import hashlib
import time
from typing import Any

#: Physical DynamoDB table name (``data-stack.ts`` → ``ProcessedFilesTable``).
#: Only a fallback for local mode; both Lambdas read ``PROCESSED_FILES_TABLE``.
PROCESSED_FILES_TABLE_DEFAULT = "agentic_data_ingestion_processed_files"

#: Sort-key attribute holding the fingerprint. Partition key is ``pipeline_id``.
FINGERPRINT_KEY = "fingerprint"

#: Reason code written on a skip record and surfaced in the UI (R9.2).
SKIP_REASON_DUPLICATE = "duplicate_content"


def normalize_etag(etag: Any) -> str:
    """Canonical form of an S3 ETag, or ``""`` when there is nothing usable.

    ``head_object`` and ``list_objects_v2`` return the ETag wrapped in literal
    double quotes; the EventBridge S3 notification returns it bare. Lowercase
    because the hex is case-insensitive and S3 has not always been consistent
    about it. Returns ``""`` for ``None``/non-string/empty so callers get a
    single "unusable" signal instead of three.
    """
    if not isinstance(etag, str):
        return ""
    return etag.strip().strip('"').strip().lower()


def content_fingerprint(size: Any, etag: Any) -> str | None:
    """Fingerprint for an S3 object from its size and ETag, or ``None``.

    ``None`` means "this object cannot be fingerprinted" and every caller must
    read it as "do not dedup, proceed" — see the module docstring on why the
    harmless direction is the default. That happens when the ETag is absent
    (the EventBridge schema marks ``detail.object.etag`` optional: only ``key``
    and ``size`` are required — ``tests/contracts/event_schemas/schemas.py``),
    or when the size is missing or not a non-negative integer.

    ``size`` is accepted as ``int`` or as a numeric string/``Decimal`` because
    it arrives from JSON (event detail), from boto3 (``Size``/``ContentLength``)
    and from DynamoDB (``Decimal``). It is normalised to ``int`` before hashing
    so those three never disagree. A ``bool`` is rejected: it is an ``int``
    subclass and a ``True`` size is a bug, not a one-byte object.
    """
    normalized_etag = normalize_etag(etag)
    if not normalized_etag:
        return None
    if isinstance(size, bool):
        return None
    try:
        size_int = int(size)
    except (TypeError, ValueError):
        return None
    if size_int < 0:
        return None
    return hashlib.sha256(f"{size_int}:{normalized_etag}".encode()).hexdigest()


def ledger_key(pipeline_id: str, fingerprint: str) -> dict[str, str]:
    """The full DynamoDB key for one ledger row.

    Keyed by pipeline plus fingerprint exactly as R9.1 states: the same content
    submitted to two different pipelines is two legitimate ingestions, so the
    pipeline is part of the key and not a filter.
    """
    return {"pipeline_id": pipeline_id, FINGERPRINT_KEY: fingerprint}


def find_processed_file(table: Any, pipeline_id: str, fingerprint: str | None) -> dict[str, Any] | None:
    """The ledger row for this pipeline+fingerprint, or ``None``.

    A returned row means "this content already completed a successful run for
    this pipeline" — rows only exist for successful runs, because
    ``job_completion`` is the only writer and it writes on the Glue SUCCEEDED
    callback. There is deliberately no status attribute to interpret: existence
    *is* the success record. A provisional "started" row would have to be
    distinguished from a completed one on every read, and getting that wrong
    once would permanently block retries of a file whose run failed.

    Fails **open**. A throttle, a missing table, or an IAM gap must not stop
    ingestion — it must only cost the dedup. The alternative (propagating) would
    convert a ledger outage into a pipeline outage, which is a much worse trade
    for a mechanism whose whole purpose is to skip *redundant* work.
    """
    if not fingerprint or not pipeline_id:
        return None
    try:
        item = table.get_item(Key=ledger_key(pipeline_id, fingerprint)).get("Item")
    except Exception:
        return None
    return item or None


def record_processed_file(
    table: Any,
    pipeline_id: str,
    fingerprint: str,
    *,
    run_id: str,
    size: Any = None,
    etag: Any = None,
    source_key: str = "",
    ttl_seconds: int,
) -> dict[str, Any]:
    """Write the ledger row for a content fingerprint that ran successfully.

    Caller is responsible for having established that the run succeeded; this
    function does not re-derive that. Returns the item it wrote so the caller
    can log it.

    Last-writer-wins on purpose (a plain ``put_item``, no condition). Re-running
    the same content deliberately — the manual override path — should refresh
    the row's ``recorded_at`` and TTL rather than fail: the newest successful run
    is the one worth remembering, and a conditional write here would make the
    second legitimate run's completion log a spurious error.
    """
    item: dict[str, Any] = {
        "pipeline_id": pipeline_id,
        FINGERPRINT_KEY: fingerprint,
        "run_id": run_id,
        "recorded_at": _utc_now_iso(),
        "ttl": int(time.time()) + ttl_seconds,
    }
    # The fingerprint's pre-image, kept so an operator can tell WHY two
    # submissions did or did not match without recomputing a digest.
    if size is not None:
        with contextlib.suppress(TypeError, ValueError):
            item["content_size"] = int(size)
    normalized_etag = normalize_etag(etag)
    if normalized_etag:
        item["content_etag"] = normalized_etag
    if source_key:
        item["source_key"] = source_key
    table.put_item(Item=item)
    return item


def _utc_now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat()
