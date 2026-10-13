# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
import hashlib
import json
import os
import sys
from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

import boto3

# Add shared directory to path for EventBridge publisher
sys.path.append("/opt/python")  # Lambda layer path
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "shared"))

from eventbridge_utils import get_eventbridge_publishers

publish_pipeline_state_change, publish_approval_status_change, publish_mcp_operation_event = (
    get_eventbridge_publishers()
)

from artifact_schema import create_output_data
from env_utils import require_env
from format_delimiters import delimiter_for_key
from pipeline_record_read import SELECTED_WORKSHEET_ATTRIBUTE, read_pipeline_record
from step_errors import StepFailure, TransientStepFailure, raise_for_read_client_error, step_handler
from step_names import STEP_SEQUENCE, StepName

try:
    from metrics_facade import PipelineMetrics
except ImportError:
    PipelineMetrics = None  # type: ignore[misc,assignment]  # optional dep; guarded by `is not None` at use sites

# Initialize logger
from aws_lambda_powertools import Logger

logger = Logger(service="agentic_data_ingestion")

# Environment variables
DYNAMODB_TABLE = require_env("STATE_TABLE", "agentic_data_ingestion_state")

# JDBC discovery Glue Catalog database — populated by both the wizard-time
# discovery crawler (Task 4.x) and the per-pipeline runtime crawler
# (Task 5.2). Same accessor pattern and default as
# ``handlers/jdbc_resources.py::DISCOVERY_DB`` (Task 4.1) so the two
# Lambdas stay in lock-step. Resolved at module load (rather than
# per-invocation) to keep the JDBC path's cold-start hit minimal.
DISCOVERY_DB = f"agentic_data_ingestion_jdbc_discovery_{os.environ.get('AGENTIC_DATA_INGESTION_ENV', 'dev')}"

# Initialize AWS clients
s3_client = boto3.client("s3")
# ``glue_client`` is used only by the JDBC source path
# (:func:`handle_jdbc_schema_extract`); the legacy S3 path never touches
# it, but creating the client at module load keeps the cold-start cost
# off the per-invocation path and lets unit tests monkey-patch it the
# same way they do ``s3_client``.
glue_client = boto3.client("glue")
dynamodb = boto3.resource("dynamodb")
table = dynamodb.Table(DYNAMODB_TABLE)


def read_json_from_s3(bucket, key):
    """Read JSON file from S3"""
    try:
        response = s3_client.get_object(Bucket=bucket, Key=key)
        content = response["Body"].read().decode("utf-8")
        return json.loads(content)
    except Exception as e:
        logger.error(f"Error reading JSON from S3 {bucket}/{key}: {e}")
        raise


def extract_json_schema(data):
    """Extract schema from JSON data"""

    def get_type(value):
        if isinstance(value, bool):
            return "boolean"
        elif isinstance(value, int):
            return "integer"
        elif isinstance(value, float):
            return "number"
        elif isinstance(value, str):
            return "string"
        elif isinstance(value, list):
            return "array"
        elif isinstance(value, dict):
            return "object"
        else:
            return "null"

    def extract_schema_recursive(obj):
        if isinstance(obj, dict):
            properties = {}
            for key, value in obj.items():
                if isinstance(value, dict):
                    properties[key] = {"type": "object", "properties": extract_schema_recursive(value)}
                elif isinstance(value, list) and len(value) > 0:
                    # Analyze first item to determine array item type
                    first_item = value[0]
                    if isinstance(first_item, dict):
                        properties[key] = {
                            "type": "array",
                            "items": {"type": "object", "properties": extract_schema_recursive(first_item)},
                        }
                    else:
                        properties[key] = {"type": "array", "items": {"type": get_type(first_item)}}
                else:
                    properties[key] = {"type": get_type(value)}
            return properties
        else:
            return {"type": get_type(obj)}

    # Handle array of objects (common for CSV converted to JSON)
    if isinstance(data, list) and len(data) > 0:
        first_item = data[0]
        if isinstance(first_item, dict):
            return {"type": "array", "items": {"type": "object", "properties": extract_schema_recursive(first_item)}}

    # Handle single object
    elif isinstance(data, dict):
        return {"type": "object", "properties": extract_schema_recursive(data)}

    return {"type": get_type(data)}


# ── Streaming Schema Extraction (Phase 9) ──────────────────────────────


def _get_type(value):
    """Determine JSON Schema type for a Python value."""
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        # Try to detect numeric strings from CSV (S3 Select returns all strings)
        return "string"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "string"


def infer_schema_from_records(records, max_sample=100):
    """
    Infer JSON Schema from multiple records with type conflict resolution.

    Samples up to max_sample records and resolves type conflicts:
    - string wins over any other type
    - number wins over integer (widening)
    - If only null values seen, defaults to string

    Returns a JSON Schema dict with type 'object' and properties.
    """
    if not records:
        return {"type": "object", "properties": {}}

    field_types: dict[Any, set[str]] = {}
    for record in records[:max_sample]:
        if not isinstance(record, dict):
            continue
        for key, value in record.items():
            field_types.setdefault(key, set()).add(_get_type(value))

    # Resolve conflicts
    properties = {}
    for field, types in field_types.items():
        types.discard("null")
        if not types:
            resolved = "string"
        elif len(types) == 1:
            resolved = types.pop()
        elif "string" in types:
            resolved = "string"
        elif types == {"number", "integer"}:
            resolved = "number"
        else:
            resolved = "string"
        properties[field] = {"type": resolved}

    return {"type": "object", "properties": properties}


def extract_schema_from_csv(bucket, key, sample_rows=100):
    """
    Extract schema from a CSV file using a bounded Range GET (first 64 KB).

    Memory use is proportional to the sample, not to file size, so a multi-GB
    object costs the same as a small one and "the file has no records" is not a
    cause of failure here.

    The field separator comes from ``key``'s own extension (p55 R4.2,
    ``LGR-bba16cc-2``). ``FORMAT_MAP`` maps both ``tsv`` and ``txt`` onto the
    Canonical_Format ``csv`` (``file_validation/lambda_function.py:57``), so a
    tab-separated or pipe-separated source arrives here and is read by this
    function — and ``csv.DictReader``'s ``delimiter`` defaults to a comma, which
    turned a ``name<TAB>age<TAB>city`` header into ONE field literally named
    ``name\\tage\\tcity``. :func:`delimiter_for_key` is what prevents that, and it
    is keyed on the extension rather than the Canonical_Format precisely because
    the format cannot distinguish these three.

    **This function used to attempt S3 Select first and fall back to this read.
    That arm was removed, and it must not come back as a "fast path".** Amazon S3
    Select has been closed to new customers since 2024-07-25, so on an account
    created after that date every call fails and this read did all the work.
    Verified before removal rather than assumed: a direct ``SelectObjectContent``
    against a real object in the primary dev account returns
    ``MethodNotAllowed``, and across both generations of this Lambda's log group
    there were **zero** ``CSV S3 Select: parsed`` success lines against many
    ``S3 Select failed`` warnings — it had never once succeeded. Keeping it cost a
    guaranteed-failed API round-trip on every extraction and, worse, made the
    unreachable arm read as the primary path while the arm that does the work
    read as a contingency.

    Raises:
        ValueError: extraction yielded a schema with zero fields — a CSV that
            carried a header and no data rows (p64 R1.3/R1.4). See the guard
            below for why it sits after the read rather than inside it.
    """
    import csv
    import io

    delimiter = delimiter_for_key(key)
    try:
        resp_get = s3_client.get_object(Bucket=bucket, Key=key, Range="bytes=0-65535")
        chunk = resp_get["Body"].read().decode("utf-8")
        reader = csv.DictReader(io.StringIO(chunk), delimiter=delimiter)
        records = []
        for i, row in enumerate(reader):
            if i >= sample_rows:
                break
            records.append(dict(row))
        logger.info(f"CSV Range GET: parsed {len(records)} records from s3://{bucket}/{key}")
        schema = infer_schema_from_records(records, max_sample=sample_rows)
    except Exception as read_err:
        # Logged and re-raised rather than swallowed. The log line is kept from
        # the pre-removal fallback arm deliberately: ``step_handler`` records the
        # failure, but this is the only place that names the object and the read
        # that failed, which is what distinguishes an unreadable object from a
        # decode failure on a genuinely binary body.
        logger.error(f"CSV Range GET failed for s3://{bucket}/{key}: {read_err}")
        raise

    # p64 R1.3/R1.4 — the Empty_Schema refusal, copying the guard shape
    # ``extract_schema_from_avro`` and ``extract_schema_from_excel`` already use:
    # interrogate the RETURNED schema's ``properties`` HERE, in the extractor,
    # rather than the input inside ``infer_schema_from_records``.
    #
    # That location is the decision. A guard written ``if not records: raise``
    # inside the helper would catch its ``if not records`` early return and
    # nothing else, while three further routes reach a zero-field schema with
    # truthy ``records``: none of them a dict (all skipped), all of them empty
    # dicts, and a sample bound that excludes every record. Guarding the output
    # covers all four by construction. The helper also stays total, because it is
    # on the JSON path too (``extract_schema_from_json``) and R1.3 scopes this
    # refusal to CSV.
    #
    # AFTER the try/except rather than inside it, and that placement is still
    # load-bearing now that there is one read arm instead of two: this function's
    # ``except Exception`` re-raises, so a ``ValueError`` raised inside the try
    # would be caught, logged as a *read* failure naming the wrong cause, and
    # re-raised — turning "header but no data rows" into "CSV Range GET failed".
    # Keeping the guard past the except is what keeps the two refusals
    # distinguishable, which is the whole point of p64 R1.4.
    #
    # (Before the S3 Select arm was removed this comment gave a second reason —
    # a raise inside that arm was silently converted into a Range-GET retry. That
    # arm is gone; the reason above is the one that survives it.)
    #
    # The message may honestly say "header" BY ELIMINATION: `file_validation`
    # refuses a zero-byte object at step 1 (p64 R1.1), so an object arriving here
    # has content, and content that yields zero records is a header line with no
    # data rows after it. R1.4 needs three distinguishable refusals — this one,
    # step 1's "is empty (0 bytes)", and the unsupported-format one — so this
    # message names the object, says header and data rows, and stays clear of
    # "empty (0 bytes)" and of "unsupported"/"recognised".
    if not schema.get("properties"):
        raise ValueError(
            f"CSV source s3://{bucket}/{key} carried a header but no data rows, so extraction "
            f"yielded a schema with zero fields. There is nothing to map and no Glue run can "
            f"write anything from it. Returning an empty schema here would report success with "
            f"zero fields, which is the defect this extractor exists to remove."
        )

    return schema


def _parse_partial_json(chunk):
    """
    Parse complete JSON records from a partial byte chunk.

    Handles two formats:
    - JSON array: [{"a":1}, {"b":2}, ...  (may be truncated)
    - JSONL/NDJSON: {"a":1}\n{"b":2}\n...  (line-delimited)
    """
    chunk = chunk.strip()
    records = []

    # Try JSONL first (line-delimited)
    if chunk and not chunk.startswith("["):
        for line in chunk.split("\n"):
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        if records:
            return records

    # Try JSON array — parse complete objects from truncated array
    if chunk.startswith("["):
        chunk = chunk[1:]  # Remove leading [

    # Try to parse the whole thing first (small file case)
    try:
        data = json.loads("[" + chunk.rstrip(",").rstrip() + "]")
        if isinstance(data, list):
            return data
    except json.JSONDecodeError:
        pass

    # Fallback: find complete JSON objects by brace matching
    depth = 0
    start = None
    for i, ch in enumerate(chunk):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    records.append(json.loads(chunk[start : i + 1]))
                except json.JSONDecodeError:
                    pass
                start = None

    return records


def extract_schema_from_json(bucket, key, sample_bytes=65536):
    """
    Extract schema from a JSON file using Range GET (first 64KB).

    Only reads the first sample_bytes of the file, parses complete records
    from the partial data, then infers schema from those records.
    Memory usage is bounded by sample_bytes regardless of file size.
    """
    try:
        resp = s3_client.get_object(
            Bucket=bucket,
            Key=key,
            Range=f"bytes=0-{sample_bytes - 1}",
        )
        chunk = resp["Body"].read().decode("utf-8")
        records = _parse_partial_json(chunk)

        if not records:
            logger.warning(f"No complete JSON records found in first {sample_bytes} bytes of s3://{bucket}/{key}")
            return {"type": "object", "properties": {}}

        logger.info(
            f"JSON Range GET: parsed {len(records)} records from first {sample_bytes} bytes of s3://{bucket}/{key}"
        )
        return infer_schema_from_records(records)

    except Exception as e:
        logger.error(f"JSON Range GET failed for s3://{bucket}/{key}: {e}")
        raise


def arrow_schema_to_json_schema(arrow_schema):
    """
    Convert a PyArrow schema to JSON Schema format.

    Maps Arrow types to JSON Schema types:
    - int8/16/32/64, uint8/16/32 → integer
    - uint64 → integer (may overflow but best match)
    - float16/32/64 → number
    - decimal128/256 → number
    - string/utf8/large_string → string
    - binary/large_binary → string (base64 encoded)
    - bool → boolean
    - date32/64, timestamp → string (ISO format)
    - list/large_list → array
    - struct → object
    - Everything else → string
    """
    try:
        import pyarrow as pa
    except ImportError:
        raise ImportError("pyarrow is required for Parquet schema extraction")

    type_map = {
        pa.int8(): "integer",
        pa.int16(): "integer",
        pa.int32(): "integer",
        pa.int64(): "integer",
        pa.uint8(): "integer",
        pa.uint16(): "integer",
        pa.uint32(): "integer",
        pa.uint64(): "integer",
        pa.float16(): "number",
        pa.float32(): "number",
        pa.float64(): "number",
        pa.bool_(): "boolean",
        pa.string(): "string",
        pa.utf8(): "string",
        pa.large_string(): "string",
        pa.binary(): "string",
        pa.large_binary(): "string",
        pa.date32(): "string",
        pa.date64(): "string",
    }

    properties = {}
    for field in arrow_schema:
        arrow_type = field.type
        # Check exact type match first
        json_type = type_map.get(arrow_type)
        if json_type is None:
            # Check type categories for parameterized types
            if pa.types.is_timestamp(arrow_type):
                json_type = "string"
            elif pa.types.is_decimal(arrow_type):
                json_type = "number"
            elif pa.types.is_list(arrow_type) or pa.types.is_large_list(arrow_type):
                json_type = "array"
            elif pa.types.is_struct(arrow_type):
                json_type = "object"
            elif pa.types.is_dictionary(arrow_type):
                json_type = "string"
            else:
                json_type = "string"
        properties[field.name] = {"type": json_type}

    return {"type": "object", "properties": properties}


def extract_schema_from_parquet(bucket, key):
    """
    Extract schema from a Parquet file using PyArrow metadata-only read.

    Reads ONLY the Parquet file footer (metadata) — zero data rows are scanned.
    This makes it O(1) regardless of file size. A 5GB Parquet file takes the
    same time as a 5KB one.

    Missing pyarrow raises ``ImportError`` naming the layer — there is no
    silent degradation to another extractor.

    Two ways to reach the footer, in order:

    1. ``s3fs``, if it happens to be importable. Kept as the first attempt so
       behaviour is unchanged wherever it is present.
    2. ``pyarrow.fs.S3FileSystem().open_input_file()`` — a seekable
       ``NativeFile`` from a dependency already in the layer, reading only the
       byte ranges pyarrow asks for. This is the **normal** path in the
       deployed Lambda, not a degraded one: ``compute-stack.ts:218-219`` excludes
       ``s3fs`` to save ~60 MB, and the Layer_Budget has 2.14 MiB of headroom
       (``.kiro/specs/p46-avro-orc-schema-extraction/layer-measurement.md``),
       so it is not coming back.

    The object is never downloaded. This path previously fell back to
    ``s3_client.download_file`` into ``/tmp``, which is 512 MB in Lambda — so
    schema extraction failed on capacity for exactly the large files the
    footer-only read exists to handle. Same reasoning as
    :func:`extract_schema_from_orc`; do not reintroduce a download.

    The Arrow schema is read while the handle is open in both branches, so
    nothing depends on pyarrow's in-memory footer surviving a closed file.

    Verification level: ``S3FileSystem.open_input_file`` was confirmed present
    and seekable in the **deployed** pyarrow 20.0.0 (see
    :func:`extract_schema_from_orc`). ``ParquetFile`` over that ``NativeFile``,
    with ``schema_arrow`` read while open, was confirmed on pyarrow 24.0.0
    locally — not re-run inside the 20.0.0 layer image.
    """
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise ImportError(
            "pyarrow is required for Parquet schema extraction. Ensure the PyArrow Lambda layer is attached."
        )

    try:
        import s3fs

        fs = s3fs.S3FileSystem()
        arrow_schema = pq.ParquetFile(f"{bucket}/{key}", filesystem=fs).schema_arrow
    except ImportError:
        logger.info("s3fs not available, using pyarrow.fs for Parquet metadata read")
        import pyarrow.fs as pa_fs

        # Lambda always sets AWS_REGION; unset (local runs) passes
        # ``region=None``, which is pyarrow's own default. Same call
        # extract_schema_from_orc makes, for the same reason.
        pa_s3 = pa_fs.S3FileSystem(region=os.environ.get("AWS_REGION"))
        with pa_s3.open_input_file(f"{bucket}/{key}") as handle:
            arrow_schema = pq.ParquetFile(handle).schema_arrow

    schema = arrow_schema_to_json_schema(arrow_schema)
    logger.info(f"Parquet metadata: {len(schema.get('properties', {}))} fields from s3://{bucket}/{key}")
    return schema


# ── External_Compression guard (p46 task 7.1, R5) ──────────────────────
#
# ``detect_format("data.avro.gz")`` returns ``avro``: it strips the ``gz``/``bz2``/
# ``snappy``/``zst`` suffix and reports the format underneath
# (``file_validation/lambda_function.py:79-83``), and
# ``test_format_detection.py::test_compressed_variants`` pins that as a 7 × 4
# parametrization (``:87-99``) — seven base extensions against four compression
# suffixes, of which the eight ``{avro,orc} × {gz,bz2,snappy,zst}`` pairs are the
# ones this guard meets. **That behaviour is correct and is not changed here**
# (R5.4): the detector is untouched, and so are the twenty other cases in that
# parametrization, which include every compressed ``csv``/``json`` variant
# (R5.3). ``LGR-60c4985-7`` (template / ratified) names
# ``test_format_detection.py:73`` and ``:133`` as fences "so they cannot be
# crossed by accident", and its ``revisit_when`` fires only on a ``FORMAT_MAP``
# addition — which p46 does not make. Nothing in this guard touches the detector
# or that file.
#
# So an externally-compressed container reaches these extractors as ``avro`` or
# ``orc``, and hands them compressed bytes. Avro and ORC compress their own data
# *blocks* internally; a wrapper around the whole object hides the header (Avro)
# or the footer (ORC) that carries the schema. Without this guard the operator is
# told the file is not an Avro container, or that its ORC postscript length is
# invalid — both accurate, neither actionable.
#
# **This is a heuristic and is meant to be** (design §5 R5): it converts the
# *common* mistake into a clear message, not every mistake. A file that is
# neither a valid container nor a recognised compression still reports its
# ordinary parse error, which is accepted.
#
# ``snappy`` is deliberately absent. R5.1 enumerates exactly three magics, and
# raw snappy — the framing ``.snappy`` usually means in a data pipeline — has no
# magic at all, so there is nothing to recognise.

#: External_Compression magics, as ``(magic, tool, suffix)`` (R5.1). The suffix is
#: the one ``detect_format`` strips, so the message names what the operator
#: actually typed.
_EXTERNAL_COMPRESSION_MAGICS: tuple[tuple[bytes, str, str], ...] = (
    (b"\x1f\x8b", "gzip", ".gz"),
    (b"\x42\x5a\x68", "bzip2", ".bz2"),
    (b"\x28\xb5\x2f\xfd", "zstd", ".zst"),
)

#: Bytes needed to test every magic above — the longest of them.
_EXTERNAL_COMPRESSION_HEAD_BYTES = max(len(magic) for magic, _, _ in _EXTERNAL_COMPRESSION_MAGICS)


def _raise_if_externally_compressed(head: bytes, bucket: str, key: str, container: str) -> None:
    """Raise naming External_Compression if *head* begins with a known magic.

    Scoped to Avro and ORC only (R5.3). ``csv`` and ``json`` are **not** guarded
    and are not reachable from here: ``extract_schema_from_csv`` and
    ``extract_schema_from_json`` have no call to this function, and their
    behaviour on compressed input is unchanged by p46.

    Args:
        head: The first bytes of the object. Short or empty is fine — a magic
            that cannot fit simply does not match, so a truncated read degrades
            to "no opinion" rather than to a wrong one.
        bucket: S3 bucket, for the message.
        key: Object key, for the message.
        container: ``"Avro"`` or ``"ORC"`` — the container the caller was reading.

    Raises:
        ValueError: *head* begins with a recognised External_Compression magic.
            Naming the cause is R5.1; telling the operator to upload it
            uncompressed is R5.2.
    """
    for magic, tool, suffix in _EXTERNAL_COMPRESSION_MAGICS:
        if head.startswith(magic):
            ext = container.lower()
            raise ValueError(
                f"s3://{bucket}/{key} is externally compressed with {tool}: it begins with the "
                f"{tool} magic bytes {magic.hex(' ')}, not with an {container} container header. "
                f"{container} already compresses its own data blocks internally, and compressing "
                f"the whole container hides the metadata its schema is read from. Upload the "
                f"{container} container uncompressed — `data.{ext}` rather than "
                f"`data.{ext}{suffix}`. The file name is not the cause: format detection strips "
                f"the .gz/.bz2/.snappy/.zst suffix and reports the format underneath, so an "
                f"externally compressed object reaches this extractor whatever it is named."
            )


def _handle_head_bytes(handle) -> bytes:
    """Read the first bytes off an already-open seekable handle. Never raises.

    Used by :func:`extract_schema_from_orc`, which — unlike the Avro extractor —
    has no fetched prefix in memory to inspect. See that function for why this is
    called only after a failed footer read.

    Returns ``b""`` if the handle cannot be rewound or read, so a diagnostic
    refinement can never replace the real error with an error about itself.
    """
    try:
        handle.seek(0)
        # ``bytes()`` rather than the raw return: a pyarrow ``NativeFile`` yields
        # ``bytes``, but the conversion also covers a handle that yields a
        # ``memoryview`` or buffer, which has no ``startswith``.
        return bytes(handle.read(_EXTERNAL_COMPRESSION_HEAD_BYTES))
    except Exception:
        # Deliberately broad and deliberately silent. This function exists only
        # to refine a diagnosis, so anything that goes wrong here must not
        # replace the caller's real error with an error about the refinement.
        return b""


def extract_schema_from_orc(bucket, key):
    """
    Extract schema from an ORC file using a metadata-only read of its footer.

    ORC keeps its schema in a postscript plus footer at the **end** of the
    file, so extraction needs random access. A forward-only ranged GET — the
    shape :func:`extract_schema_from_json` uses — cannot reach it, which is why
    this goes through ``pyarrow.fs.S3FileSystem().open_input_file()``: that
    returns a seekable ``NativeFile`` and ``pyarrow.orc.ORCFile`` reads only the
    byte ranges it needs for the footer. **Do not "simplify" this to a ranged
    GET.**

    The object is never downloaded. No ``download_file``, nothing written to
    ``/tmp`` — Lambda ``/tmp`` is 512 MB, which is what made
    :func:`extract_schema_from_parquet`'s former download fallback fail on
    large files. That fallback now uses this same seekable-handle path.

    ``ORCFile.schema`` is documented as "The file schema, as an arrow schema"
    and returns a ``pyarrow.Schema``, so the existing
    :func:`arrow_schema_to_json_schema` converts it unchanged. **No second Arrow
    type mapping is introduced** — one Arrow vocabulary, one converter.

    Raises on any failure and never returns ``{"type": "object",
    "properties": {}}``. That empty schema is the defect this extractor exists
    to remove: it is a *handled* outcome, so the run advances to
    ``schema_mapping`` with nothing to map while reporting success. The
    ``@step_handler`` wrapper turns the raise into a permanent ``StepFailure``
    recorded against ``schema_extract``, which is the step that could not read
    the file.

    Verified against the **deployed** pyarrow, 20.0.0 — the version
    ``--platform manylinux2014_aarch64 --only-binary=:all:`` resolves in the
    layer's bundling command, not the 24.0.0 in the dev venv. In that wheel:
    ``pyarrow.orc`` imports, ``ORCFile.schema`` returns a ``pyarrow.lib.Schema``,
    ``pyarrow.fs.S3FileSystem`` exposes ``open_input_file``, and the handle it
    returns reports ``seekable() is True``. An unreadable footer surfaces as
    ``OSError`` ("Not an ORC file", "File size too small"), re-raised here with
    pyarrow's own message rather than flattened into a generic wrapper.

    **ORC does not preserve nullability** — every column reads back
    ``nullable=True``, because ORC has no NOT NULL constraint. Nothing may infer
    a required field from an ORC footer.

    **External_Compression is diagnosed after the footer read, not before it**
    (R5.1, p46 task 7.1) — the one place this extractor's shape differs from
    :func:`extract_schema_from_avro`'s. R5.1 phrases the guard as firing "where
    the fetched prefix begins with a recognised magic", and Avro *has* such a
    prefix: its ranged GET already holds the head in memory, so the check is a
    head test before the parse. This function fetches no prefix at all. It hands
    a seekable handle to pyarrow, which reads the **tail**, so there is nothing
    to inspect until something is deliberately read — and reading the head up
    front would cost a ranged read on **every** successful extraction to
    diagnose a case that is, by construction, already failing.

    So the head is read only once ``ORCFile`` has raised, off the handle that is
    already open. Three consequences worth stating:

    - the success path is byte-for-byte what task 5.1 shipped and task 5.2
      measured — a single ranged read for the fixture, and
      ``test_orc_footer_read_is_bounded``'s ``len(source.reads) == 1`` still
      holds. A check-first guard would have made it two and broken that test
    - **no boto3 call is introduced.** The head comes off the existing
      ``pyarrow.fs`` handle, so ``test_orc_never_downloads_the_object``'s
      assertion that ``get_object``, ``download_file`` and
      ``select_object_content`` are never called remains true, as does R2.3 — a
      handle from ``S3FileSystem``, and no whole-object download. At most four
      extra bytes are read, on a path that is already raising
    - the handle survives a failed ``ORCFile`` construction. Verified rather
      than assumed: after the ``OSError``, ``handle.closed`` is ``False`` and
      ``seek(0)``/``read(4)`` succeed. A gzipped ORC object otherwise reports
      ``Invalid ORC postscript length``, which is accurate and useless

    Args:
        bucket: S3 bucket holding the ORC object.
        key: Object key.

    Returns:
        ``{"type": "object", "properties": {<column>: {"type": <json_type>}}}``
        with one entry per top-level ORC column.

    Raises:
        ImportError: pyarrow is missing, i.e. the layer is not attached.
        ValueError: the object is externally compressed (R5.1, R5.2).
        Exception: any other failure reading or parsing the footer, re-raised
            with pyarrow's own message.
    """
    try:
        import pyarrow.fs as pa_fs
        import pyarrow.orc as pa_orc
    except ImportError as e:
        # Same message shape as extract_schema_from_parquet's (R2.6). The
        # ``from e`` is the one divergence, and it is deliberate: it keeps the
        # original ModuleNotFoundError in the chain, which says WHICH submodule
        # is missing — a pyarrow built without the ORC component fails on
        # ``pyarrow.orc`` alone and looks nothing like a detached layer. It also
        # keeps ruff's B904 count at its baseline.
        raise ImportError(
            "pyarrow is required for ORC schema extraction. Ensure the PyArrow Lambda layer is attached."
        ) from e

    try:
        # Lambda always sets AWS_REGION. When it is unset (local runs) this
        # passes ``region=None``, which is pyarrow's own default and falls
        # through to the AWS SDK's resolution chain — verified: a bare
        # ``S3FileSystem()`` already picks AWS_REGION up. Passing it is
        # explicitness, not a workaround.
        fs = pa_fs.S3FileSystem(region=os.environ.get("AWS_REGION"))
        # Seekable handle, NOT a download: pyarrow issues ranged reads for the
        # tail of the object only.
        with fs.open_input_file(f"{bucket}/{key}") as handle:
            try:
                arrow_schema = pa_orc.ORCFile(handle).schema
            except Exception:
                # External_Compression guard (R5.1), on the failure path only.
                # See the guard's block comment above and this function's
                # docstring for why the check follows the footer read here while
                # it precedes the parse in extract_schema_from_avro.
                _raise_if_externally_compressed(_handle_head_bytes(handle), bucket, key, "ORC")
                raise
    except Exception as e:
        logger.error(f"ORC footer read failed for s3://{bucket}/{key}: {e}")
        raise

    schema = arrow_schema_to_json_schema(arrow_schema)
    logger.info(f"ORC metadata: {len(schema.get('properties', {}))} columns from s3://{bucket}/{key}")
    return schema


# ── Avro_Converter ─────────────────────────────────────────────────────
#
# Avro's type vocabulary is unrelated to Arrow's, so ``arrow_schema_to_json_schema``
# does NOT apply here and this is NOT a second Arrow mapping — the two converters
# read different vocabularies and neither routes through the other (R2.4).
#
# Structurally this mirrors the pair the module already has for records:
# :func:`_avro_type_to_json_type` is to an Avro type token what :func:`_get_type`
# is to a Python value, and :func:`_resolve_json_type_union` is
# ``infer_schema_from_records``'s conflict resolution (``:171-181``) applied to a
# declared union instead of an observed one. That mirroring is deliberate — see
# :func:`_resolve_json_type_union`.

#: Avro primitive (R8.1) and complex (R8.2) type tokens → JSON Schema types.
#:
#: ``null`` maps to the JSON Schema type ``"null"`` here, matching what
#: :func:`_get_type` returns for ``None`` (``:129-130``). The discard of ``null``
#: is a *field-level* concern and happens in :func:`_resolve_json_type_union`,
#: exactly as it does for observed records.
#:
#: ``union`` is in the table because R8.2 names it, even though Avro spells a
#: union as a JSON array rather than as this token. Reaching it means a union was
#: named without its branches, which carries no type information — so it takes
#: the same answer R8.4 gives an unresolvable union.
_AVRO_TYPE_MAP: dict[str, str] = {
    # Primitives (R8.1)
    "null": "null",
    "boolean": "boolean",
    "int": "integer",
    "long": "integer",
    "float": "number",
    "double": "number",
    "bytes": "string",
    "string": "string",
    # Complex (R8.2)
    "record": "object",
    "map": "object",
    "array": "array",
    "enum": "string",
    "fixed": "string",
    "union": "string",
}

#: Avro logical types → JSON Schema types (R8.3).
#:
#: A logical type is an annotation on an underlying primitive, and it **wins over
#: that primitive** when recognised: ``{"type": "long", "logicalType":
#: "timestamp-millis"}`` is ``string``, not ``integer``, and ``{"type": "bytes",
#: "logicalType": "decimal"}`` is ``number``, not ``string``. An *unrecognised*
#: logical type falls through to the underlying type instead — see
#: :func:`_avro_type_to_json_type`.
_AVRO_LOGICAL_TYPE_MAP: dict[str, str] = {
    "date": "string",
    "timestamp-millis": "string",
    "timestamp-micros": "string",
    "decimal": "number",
}

#: Recursion bound for :func:`_avro_type_to_json_type`. An Avro schema parsed from
#: JSON is a tree and cannot be cyclic, so this is never reached by a real schema.
#: It is here because R8.5 requires the converter to return a type rather than
#: raise for *any* input, and ``RecursionError`` is a raise.
_AVRO_MAX_TYPE_DEPTH = 20


def _resolve_json_type_union(json_types: Iterable[str]) -> str:
    """Resolve a set of JSON Schema types to one, discarding ``null``.

    This is deliberately the same policy as ``infer_schema_from_records``'s
    conflict resolution at ``:171-181``, branch for branch and in the same order:

    ==========================  ==========
    condition                   resolves to
    ==========================  ==========
    ``null`` discarded first    —
    nothing left                ``string``
    exactly one type            that type
    ``string`` present          ``string``
    exactly ``{number, integer}``  ``number``
    anything else               ``string``
    ==========================  ==========

    **Do not give Avro a second union policy.** Two type resolvers in one module
    disagreeing about the same question is drift, and R8.4 requires this one to
    agree with the existing one. The agreement is asserted by p46 task 3.2's
    property test (design Property 4) rather than by sharing a function, because
    ``infer_schema_from_records`` is reused unchanged by this spec.

    The one difference from ``:171-181`` is that this copies the set instead of
    mutating the caller's. Same result; the caller's set survives.

    Args:
        json_types: JSON Schema type names, e.g. the resolved branches of an Avro
            union. ``null`` may or may not be among them.

    Returns:
        A single JSON Schema type name. Never raises.
    """
    types = set(json_types)
    types.discard("null")
    if not types:
        return "string"
    if len(types) == 1:
        return types.pop()
    if "string" in types:
        return "string"
    if types == {"number", "integer"}:
        return "number"
    return "string"


def _avro_type_to_json_type(avro_type: Any, _depth: int = 0) -> str:
    """Map one Avro type to one JSON Schema type. Total: never raises.

    Handles the three shapes an Avro type can take:

    - a **string** token — ``"long"``, ``"record"``, … read from
      :data:`_AVRO_TYPE_MAP`, with an unrecognised token answering ``string``
      (R8.5), the same fallthrough ``arrow_schema_to_json_schema`` uses at
      ``:388-389``
    - a **list** — Avro's spelling of a union. Each branch is resolved and the
      results go through :func:`_resolve_json_type_union` (R8.4)
    - a **Mapping** — a parameterised or logically-annotated type. A recognised
      ``logicalType`` wins (R8.3); otherwise this recurses into ``type``, which is
      itself any of the three shapes

    An unrecognised ``logicalType`` deliberately falls through to the underlying
    type rather than to ``string``: ``{"type": "int", "logicalType":
    "time-millis"}`` is ``integer``, because the annotation is unknown but the
    primitive under it is not, and answering from what is known beats discarding
    it. Still total either way.

    Args:
        avro_type: An Avro type in any of the three shapes above. Anything else —
            ``None``, an ``int``, a missing key — answers ``string``.
        _depth: Recursion guard, internal. See :data:`_AVRO_MAX_TYPE_DEPTH`.

    Returns:
        A single JSON Schema type name. Never raises (R8.5).
    """
    if _depth > _AVRO_MAX_TYPE_DEPTH:
        return "string"

    # Union: Avro spells it as a JSON array of branch schemas.
    if isinstance(avro_type, list):
        return _resolve_json_type_union(_avro_type_to_json_type(branch, _depth + 1) for branch in avro_type)

    if isinstance(avro_type, Mapping):
        logical = avro_type.get("logicalType")
        if isinstance(logical, str) and logical in _AVRO_LOGICAL_TYPE_MAP:
            return _AVRO_LOGICAL_TYPE_MAP[logical]
        inner = avro_type.get("type")
        if inner is None:
            return "string"
        return _avro_type_to_json_type(inner, _depth + 1)

    if isinstance(avro_type, str):
        return _AVRO_TYPE_MAP.get(avro_type, "string")

    return "string"


def _avro_field_json_type(avro_type: Any) -> str:
    """The JSON Schema type for a *field*, with ``null`` discarded.

    Splitting this from :func:`_avro_type_to_json_type` is what keeps the two
    resolvers in step. The type mapper answers ``"null"`` for the ``null``
    primitive, as R8.1 asks and as :func:`_get_type` does for ``None``; the
    discard belongs one level up, to the field. So a field declared bare ``null``
    — a field that can only ever be null — resolves to ``string``, which is what
    ``infer_schema_from_records`` produces for a field observed only as null
    (``:171-173``). Passing every field through the union resolver, not just the
    ones spelled as a union, is what makes that hold for the degenerate case too;
    for any other single type the resolver is the identity.
    """
    return _resolve_json_type_union({_avro_type_to_json_type(avro_type)})


def _avro_record_fields(writer_schema: Any) -> list[Any]:
    """The ``fields`` list of an Avro record schema, or ``[]``.

    Unwraps a top-level union to its first record branch, which is legal Avro
    (``["null", {"type": "record", …}]``). Returns ``[]`` rather than raising for
    anything that is not a record — the caller then reports no properties, which
    is the honest answer and the signal the *extractor* raises on (see
    :func:`avro_schema_to_json_schema`).
    """
    if isinstance(writer_schema, Mapping):
        fields = writer_schema.get("fields")
        return list(fields) if isinstance(fields, list) else []
    if isinstance(writer_schema, list):
        for branch in writer_schema:
            fields = _avro_record_fields(branch)
            if fields:
                return fields
    return []


def avro_schema_to_json_schema(writer_schema: Any) -> dict[str, Any]:
    """Convert an Avro Writer_Schema to JSON Schema. Total: never raises.

    Maps Avro types to JSON Schema types (R8.1–R8.3):

    - ``int``, ``long`` → ``integer``
    - ``float``, ``double`` → ``number``
    - ``boolean`` → ``boolean``
    - ``string``, ``bytes``, ``enum``, ``fixed`` → ``string``
    - ``record``, ``map`` → ``object``
    - ``array`` → ``array``
    - a union → its non-``null`` branch, ``string`` when ambiguous
    - logical ``date``, ``timestamp-millis``, ``timestamp-micros`` → ``string``
    - logical ``decimal`` → ``number``
    - anything unrecognised → ``string``

    **Avro types are not Arrow types.** This exists rather than reusing
    ``arrow_schema_to_json_schema`` because the two vocabularies are unrelated,
    and neither converter routes through the other (R2.4).

    The output is **flat**, one entry per top-level field, matching what every
    other extractor in this module returns and what ``store_schema_in_dynamodb``
    counts as ``field_count``. Nested records and arrays report ``object`` /
    ``array`` without their inner fields, exactly as
    ``arrow_schema_to_json_schema`` does for a struct (``:384-385``).

    **On the empty case, and where the raise belongs.** A schema carrying no
    usable fields returns ``properties: {}``. That is this function being honest
    about its input, and it is *not* the module's Empty_Schema defect: the
    forbidden thing is an *extractor* handing an empty schema back to the
    pipeline as a success. The check therefore belongs to the caller —
    ``extract_schema_from_avro`` raises when this returns no properties — for the
    same reason ``arrow_schema_to_json_schema`` does not police its own output.
    Nothing here raises, because R8.5 requires a type for every input.

    Args:
        writer_schema: A parsed Avro writer schema, normally the record dict from
            an Avro_OCF header's ``avro.schema`` metadata. A top-level union
            wrapping a record is accepted. Anything else yields no properties.

    Returns:
        ``{"type": "object", "properties": {<field>: {"type": <json_type>}}}``.
    """
    properties: dict[str, Any] = {}
    for field in _avro_record_fields(writer_schema):
        if not isinstance(field, Mapping):
            continue
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue
        properties[name] = {"type": _avro_field_json_type(field.get("type"))}

    return {"type": "object", "properties": properties}


# ── Avro_OCF header parser ─────────────────────────────────────────────
#
# **There is no ``fastavro`` here, deliberately, and adding one would break the
# deployed Lambda.** p46 task 1 measured the Layer_Budget and the gate FAILED:
# ``fastavro`` needs 12.70 MiB against 2.14 MiB of headroom, or 8.68 MiB even
# built from source (see
# ``.kiro/specs/p46-avro-orc-schema-extraction/layer-measurement.md``). It is a
# **dev-only** dependency — ``pyproject.toml``'s ``dev`` group, used by
# ``tests/fixtures/data/generate_binary_fixtures.py`` to *write* the fixture —
# and it is **not in the Lambda layer**. An ``import fastavro`` below would
# raise ``ModuleNotFoundError`` on every real invocation.
#
# So Requirement 1 takes requirements.md's retained fallback: parse the OCF
# header by hand, with no new dependency.
#
# **The risk this carries, named rather than hidden.** requirements.md rejected
# hand-parsing *on correctness, not size*: "a subtle bug produces a wrong schema
# rather than a failure, which is the same class of silent wrongness this spec
# removes." That risk is now unavoidable, so it is the thing this code is
# engineered against, and the design rule throughout is **raise rather than
# guess**:
#
# - the magic is validated, so a non-Avro object is named as such
# - every varint decode is bounds-checked against the buffer, so a corrupt
#   length cannot walk off the end and read adjacent bytes as a schema
# - a metadata value whose declared length exceeds the prefix read raises
#   *naming the prefix boundary*, rather than returning the fields that did fit.
#   A truncated read that silently yields fewer fields is precisely the silent
#   wrongness p46 exists to remove
#
# **Developed against the committed fixture's real bytes**, not against a
# reading of the OCF spec — the spec is what a shared misreading would be shared
# *with*. Verified by reading ``tests/fixtures/data/customer_data.avro``
# (1,186 bytes) directly:
#
#   ===============================================  =========================
#   offset                                           content
#   ===============================================  =========================
#   ``0..3``                                         magic ``b'Obj\x01'``
#   ``4``                                            map block count, ``0x04`` → zigzag 2
#   ``5``                                            key length ``0x14`` → 10
#   ``6..15``                                        ``avro.codec``
#   ``16``                                           value length ``0x08`` → 4
#   ``17..20``                                       ``null`` — no internal block compression
#   ``21``                                           key length ``0x16`` → 11
#   ``22..32``                                       ``avro.schema``
#   ``33..34``                                       value length ``0xc2 0x07`` → 481
#   ``35..515``                                      the Writer_Schema JSON
#   ``516``                                          map terminator, ``0x00`` → 0
#   ``517..532``                                     16-byte sync marker
#   ===============================================  =========================
#
# Two corrections to the offsets recorded during task 2.2, both from re-reading
# the bytes: the schema JSON spans ``35..515`` (481 bytes) rather than 35–516 —
# 516 is the map terminator, one past the JSON — and ``["null", "double"]``
# appears at offset **495**, inside that header JSON, not near 1141. Neither
# changes the conclusion that matters: 515 ≪ 65536, so R1.3's bounded prefix
# reaches the whole Writer_Schema with room to spare.

#: The 4-byte Avro_OCF magic. Every OCF begins with it, which is what makes a
#: non-Avro object diagnosable rather than a parse error deep in a varint.
_AVRO_OCF_MAGIC = b"Obj\x01"

#: The metadata key carrying the Writer_Schema as a JSON string.
_AVRO_SCHEMA_METADATA_KEY = "avro.schema"

#: Maximum bytes one Avro ``long`` may occupy. The encoding is a 64-bit zigzag
#: varint at 7 payload bits per byte, so ``ceil(64/7) == 10`` is the ceiling. A
#: longer run of continuation bits is not a large number, it is garbage — and
#: without this bound a buffer of ``0xff`` bytes would be decoded as an
#: enormous length instead of being refused.
_AVRO_MAX_VARINT_BYTES = 10

#: Sanity bound on metadata map entries. Real headers carry a handful —
#: the fixture carries two. This exists so a corrupt block count is refused as
#: nonsense at the count rather than one overrun at a time.
_AVRO_MAX_METADATA_ENTRIES = 1000


def _read_avro_long(buf: bytes, pos: int, what: str) -> tuple[int, int]:
    """Decode one Avro ``long`` — a zigzag varint — at *pos*.

    Avro writes integers little-endian, 7 payload bits per byte, high bit set to
    continue, then zigzag-decodes so small negatives stay short.

    **Both guards are load-bearing** and are the reason this is a function
    rather than an inline loop. Running past the end of the buffer means the
    header was cut by the prefix boundary, and over-long continuation bits mean
    the bytes are not a varint at all. Either way the answer is a raise: a
    decoder that keeps going produces a plausible number from unrelated bytes,
    and a wrong length yields a wrong schema instead of a failure.

    Args:
        buf: The prefix read from S3.
        pos: Offset to decode at.
        what: What is being decoded, for the message. Named from the caller so a
            failure says which field was being read.

    Returns:
        ``(value, next_pos)``.

    Raises:
        ValueError: The buffer ends mid-varint, or the varint exceeds
            :data:`_AVRO_MAX_VARINT_BYTES`.
    """
    acc = 0
    shift = 0
    start = pos
    while True:
        if pos >= len(buf):
            raise ValueError(
                f"Avro OCF header is truncated: {what} runs past the end of the "
                f"{len(buf)}-byte prefix read (varint began at offset {start})"
            )
        if pos - start >= _AVRO_MAX_VARINT_BYTES:
            raise ValueError(
                f"Avro OCF header is malformed: {what} at offset {start} is not a valid "
                f"varint — continuation bits run past {_AVRO_MAX_VARINT_BYTES} bytes"
            )
        byte = buf[pos]
        pos += 1
        acc |= (byte & 0x7F) << shift
        if not byte & 0x80:
            break
        shift += 7
    # Zigzag: even encodes n/2, odd encodes -(n+1)/2.
    return (acc >> 1) ^ -(acc & 1), pos


def _read_avro_bytes(buf: bytes, pos: int, what: str) -> tuple[bytes, int]:
    """Decode one length-prefixed Avro ``bytes`` / ``string`` at *pos*.

    The truncation branch here is the single most important raise in this
    extractor. When a declared length exceeds what the prefix holds, the header
    was cut mid-value — and the tempting thing, returning what did arrive, is
    exactly the defect p46 removes: a schema with fewer fields than the file
    has, handed to the pipeline as a success. So it raises, and the message
    names the prefix boundary as the cause so an operator is not told the file
    is corrupt.

    Args:
        buf: The prefix read from S3.
        pos: Offset of the length prefix.
        what: What is being read, for the message.

    Returns:
        ``(value, next_pos)``.

    Raises:
        ValueError: Negative declared length, or a length reaching past the
            prefix.
    """
    length, pos = _read_avro_long(buf, pos, f"the length of {what}")
    if length < 0:
        raise ValueError(f"Avro OCF header is malformed: {what} declares a negative length ({length})")
    end = pos + length
    if end > len(buf):
        raise ValueError(
            f"Avro OCF header is truncated: {what} declares {length} bytes at offset {pos}, "
            f"but only {len(buf) - pos} remain in the {len(buf)}-byte prefix read. "
            f"The header did not fit the sampled prefix — increase sample_bytes. "
            f"No partial schema is returned, because a schema missing fields the file "
            f"declares would be wrong rather than incomplete."
        )
    return buf[pos:end], end


def _parse_avro_ocf_metadata(buf: bytes) -> dict[str, bytes]:
    """Parse an Avro_OCF header's metadata map out of *buf*.

    The layout, per the Avro specification and confirmed byte-for-byte against
    the committed fixture (offset table above): the 4-byte magic, then a map
    encoded as a series of blocks. Each block is prefixed by its entry count as
    a ``long``; a **negative** count means ``abs(count)`` entries preceded by
    the block's size in bytes (a writer's option, for skippable blocks); a count
    of **zero** terminates the map. Each entry is a length-prefixed UTF-8 key
    followed by a length-prefixed value.

    The negative-count form is handled rather than refused because it is legal
    Avro that ``fastavro`` happens not to emit — the fixture's count is ``2``,
    positive, so this branch is **not exercised by the fixture** and is written
    from the specification. That is stated plainly rather than implied: it is
    the one part of this parser the independent oracle does not cover.

    Stops at the map terminator and does **not** read the 16-byte sync marker
    that follows — nothing here needs it.

    Args:
        buf: The prefix read from S3. May be shorter than the whole header, in
            which case this raises rather than returning what it managed to
            read.

    Returns:
        The metadata map, values as raw bytes.

    Raises:
        ValueError: The magic is wrong, the buffer is truncated mid-header, a
            key is not UTF-8, or the entry count is implausible.
    """
    if not buf.startswith(_AVRO_OCF_MAGIC):
        raise ValueError(
            f"Not an Avro Object Container File: expected magic {_AVRO_OCF_MAGIC!r} at offset 0, found {buf[:4]!r}"
        )

    pos = len(_AVRO_OCF_MAGIC)
    metadata: dict[str, bytes] = {}

    while True:
        count, pos = _read_avro_long(buf, pos, "a metadata map block count")
        if count == 0:
            return metadata
        if count < 0:
            # Negative count: abs(count) entries, preceded by the block's byte
            # size. The size is read to advance past it and is not trusted as a
            # bound — the per-entry length checks in _read_avro_bytes are.
            count = -count
            block_size, pos = _read_avro_long(buf, pos, "a metadata map block size")
            if block_size < 0:
                raise ValueError(
                    f"Avro OCF header is malformed: metadata map block declares a negative size ({block_size})"
                )
        if len(metadata) + count > _AVRO_MAX_METADATA_ENTRIES:
            raise ValueError(
                f"Avro OCF header is malformed: metadata map declares {count} entries "
                f"(bound is {_AVRO_MAX_METADATA_ENTRIES}); the bytes are not an OCF header"
            )

        for _ in range(count):
            raw_key, pos = _read_avro_bytes(buf, pos, "a metadata key")
            try:
                metadata_key = raw_key.decode("utf-8")
            except UnicodeDecodeError as e:
                raise ValueError(
                    f"Avro OCF header is malformed: a metadata key is not valid UTF-8 ({raw_key[:32]!r})"
                ) from e
            metadata[metadata_key], pos = _read_avro_bytes(buf, pos, f"the metadata value for {metadata_key!r}")


def extract_schema_from_avro(bucket, key, sample_bytes: int = 65536) -> dict[str, Any]:
    """Extract schema from an Avro file using a Range GET of its OCF header.

    An Avro Object Container File carries its Writer_Schema in the header, so it
    sits in the file's **first** bytes and a single bounded ranged GET reaches
    it — the same shape :func:`extract_schema_from_json` uses (``:304-309``).
    This is the opposite situation to ORC and Parquet, whose footers are at the
    end of the file and need a seekable handle
    (:func:`extract_schema_from_orc`).

    The object is never downloaded. No ``download_file``, nothing written to
    ``/tmp`` — memory is bounded by ``sample_bytes`` regardless of file size
    (R1.3, R1.4, C8).

    **The header is parsed here rather than by a library**, because ``fastavro``
    does not fit the Layer_Budget — see the block comment above this function
    for the measurement, the correctness risk that choice carries, and the
    fixture offsets this parser was developed against.

    **Raises on any failure and never returns** ``{"type": "object",
    "properties": {}}``. That empty schema is the defect p46 exists to remove:
    it is a *handled* outcome, so the run advances to ``schema_mapping`` with
    nothing to map while reporting success. The ``@step_handler`` wrapper turns
    the raise into a permanent ``StepFailure`` recorded against
    ``schema_extract``, the step that could not read the file. Design Property 2:
    for every input, either a non-empty ``properties`` or an exception — no
    third outcome.

    The empty-properties check lives here rather than in
    :func:`avro_schema_to_json_schema` by that function's own design: it is total
    and reports honestly on its input, and policing the result is the
    *extractor's* job, exactly as ``arrow_schema_to_json_schema`` does not
    police its own output.

    **No third-party import, so there is no ``ImportError`` to raise** — the
    only imports this path uses are ``json`` and the module-level ``boto3``
    client, both already present. R1.5's missing-dependency concern is moot
    here; it applies to :func:`extract_schema_from_orc` and
    :func:`extract_schema_from_parquet`, which do import ``pyarrow`` and do name
    the layer.

    **External_Compression is named as such** (R5.1, R5.2, p46 task 7.1). The
    prefix is already in memory when the guard runs, so this is a head test
    ahead of the magic check: a gzipped ``.avro.gz`` used to fail as "not an
    Avro Object Container File", which is accurate and unhelpful, and now
    reports external compression as the cause and says to upload the container
    uncompressed. :func:`extract_schema_from_orc` reaches the same conclusion by
    a different route, because it fetches no prefix — see its docstring.

    Args:
        bucket: S3 bucket holding the Avro object.
        key: Object key.
        sample_bytes: Upper bound on the ranged read. The fixture's header ends
            at offset 515, so the 64 KB default has ample room; a header longer
            than this raises naming the boundary rather than truncating.

    Returns:
        ``{"type": "object", "properties": {<field>: {"type": <json_type>}}}``
        with one entry per top-level field of the Writer_Schema (R1.2).

    Raises:
        ValueError: The object is externally compressed (R5.1, R5.2), the prefix
            is not an OCF, the header is truncated or malformed,
            ``avro.schema`` is absent, its JSON does not parse, or the
            Writer_Schema yields no fields.
        Exception: any S3 read failure, re-raised.
    """
    try:
        resp = s3_client.get_object(
            Bucket=bucket,
            Key=key,
            Range=f"bytes=0-{sample_bytes - 1}",
        )
        prefix = resp["Body"].read()
    except Exception as e:
        logger.error(f"Avro Range GET failed for s3://{bucket}/{key}: {e}")
        raise

    try:
        # External_Compression guard (R5.1), ahead of the magic check — the
        # prefix is already in memory, so this is a straightforward head test and
        # a gzipped object is named as such rather than as "not an OCF".
        _raise_if_externally_compressed(prefix, bucket, key, "Avro")

        metadata = _parse_avro_ocf_metadata(prefix)

        raw_schema = metadata.get(_AVRO_SCHEMA_METADATA_KEY)
        if raw_schema is None:
            raise ValueError(
                f"Avro OCF header carries no {_AVRO_SCHEMA_METADATA_KEY!r} metadata key "
                f"(found {sorted(metadata)}), so the writer schema cannot be read"
            )

        try:
            writer_schema = json.loads(raw_schema.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            raise ValueError(f"Avro OCF header's {_AVRO_SCHEMA_METADATA_KEY!r} value is not valid JSON: {e}") from e

        schema = avro_schema_to_json_schema(writer_schema)
        if not schema.get("properties"):
            raise ValueError(
                "Avro writer schema yielded no fields. The header parsed, but it declares no "
                "usable top-level record fields, so there is nothing to map. Returning an empty "
                "schema here would report success with zero fields, which is the defect this "
                "extractor exists to remove."
            )
    except Exception as e:
        logger.error(f"Avro header parse failed for s3://{bucket}/{key}: {e}")
        raise

    logger.info(
        f"Avro OCF header: {len(schema['properties'])} fields from first {len(prefix)} bytes of s3://{bucket}/{key}"
    )
    return schema


# ── p55 W3: Excel_Workbook reading and conversion ───────────────────────
#
# Excel is the FIRST Canonical_Format that is not in the Native_Format_Set:
# Spark and Glue have no built-in Excel datasource, so a schema alone is not
# enough — the Glue job cannot read the source object at all. R13.3 therefore
# requires the source to be converted to a Native_Format_Set member before the
# job reads it, and design §4.2 (W3-D1) sites that conversion HERE, because
# this step must open the workbook anyway to satisfy R6.2.
#
# The conversion gate is read in exactly ONE place — the ``elif source_format
# == "excel"`` branch of the Extraction_Dispatch below — and nowhere else.
# ``create_glue_pipeline`` and the templates consume the already-resolved
# ``readable_source_uri`` (``LGR-28b4d75-1``, derive once). There is no second
# copy of ``NATIVE_FORMATS`` here: the gate is the branch, which is what F5's
# guards read by ``ast``.
#
# The converted object goes to the PROCESSED bucket, never back to the source
# bucket. ``schemaExtractRole`` already carries ``s3:PutObject`` on both
# (``foundation-stack.ts:262``), so this needs no IAM change either way — but
# the source bucket is watched by the S3-upload EventBridge rule, so writing a
# converted object there would re-trigger the pipeline on its own output. The
# Glue job's read is covered by the existing ``adi-processed-files-*`` bucket
# wildcard in ``create_glue_pipeline/iam_roles.py:373-395``, ``:441-460``.
_CONVERTED_SOURCE_KEY = "converted/{pipeline_id}/{run_id}/source.csv"

#: The Canonical_Format the Excel conversion writes. It is a Native_Format_Set
#: member, and it is CSV rather than JSON or Parquet for one reason that
#: outweighs the others: the four Glue_Job_Templates already have a ``csv``
#: branch (``glue_job_glue_catalog.py.j2:50-51`` and siblings) whose read is
#: ``spark.read.option("header", True).option("inferSchema", False)
#: .option("sep", …).csv(source_path)``, so the readable format needs no new
#: template branch and no new render decision.
_EXCEL_READABLE_FORMAT = "csv"


def _excel_field_names(header_cells) -> list[str]:
    """One field name per column, total and collision-free.

    R6.2 wants one field per column of the processed Worksheet, *named from that
    column's header cell*. Two workbook realities make that a function rather
    than a comprehension, and both must not lose a column:

    * a **blank** header cell — named ``column_{n}``, 1-based, so the column is
      still described rather than dropped;
    * a **duplicate** header — suffixed ``_2``, ``_3``, … so two columns cannot
      collapse into one key.

    Collapsing either case would make the field count smaller than the column
    count, which is the same class of quiet wrongness as an Empty_Schema: the
    run would succeed describing fewer columns than it is about to load. It is
    also what makes Property 6 (field count == the selected sheet's column
    count) hold for *any* workbook rather than only tidy ones.

    The returned list is the header row the converter writes too, so the CSV the
    Glue job reads carries exactly the names this schema declares.
    """
    names: list[str] = []
    seen: dict[str, int] = {}
    for index, cell in enumerate(header_cells, start=1):
        raw = "" if cell is None else str(cell).strip()
        name = raw or f"column_{index}"
        if name in seen:
            seen[name] += 1
            name = f"{name}_{seen[name]}"
        seen.setdefault(name, 1)
        names.append(name)
    return names


def get_pipeline_metadata(original_pipeline_id: str) -> dict | None:
    """One pipelines-table record, or ``None`` when there genuinely is none.

    p55 task 5.1. **This is a NEW read path for this Lambda** — before p55
    ``schema_extract`` held ``STATE_TABLE`` only and reached pipeline
    configuration not at all; it read the ``file_validation`` state row
    (:func:`get_previous_state`) and nothing else. So the Selected_Worksheet
    (R8.3) needed *some* route to this step, and this is the route the
    repository already has rather than a second one:
    ``shared/pipeline_record_read.read_pipeline_record`` (`LGR-b397ee2-3`),
    already used by ``test_code``, ``schema_mapping`` and
    ``create_code/mapping_resolution``.

    Three properties of that primitive are load-bearing here and are not
    incidental (`LGR-b397ee2-2`):

    * The **table object** is constructed per call, in-function, exactly as
      ``schema_mapping.get_pipeline_metadata:225`` and
      ``test_code:179`` do. That keeps ``patch.object(_mod, "dynamodb", …)``
      working as a test seam, and — the reason that matters operationally —
      it keeps ``require_env("PIPELINES_TABLE")`` OFF the module-load path.
      A module-level ``require_env`` here would raise ``OSError`` on **every**
      invocation of this Lambda, including every CSV and Parquet run, on any
      deploy where ``compute-stack.ts`` had not yet been updated with the
      variable. Resolved lazily, only a run that actually needs pipeline
      configuration can be affected.
    * ``not_found=None``, so absence of the *record* is distinguishable from a
      record that simply carries no worksheet selection. Those are different
      facts and :func:`_configured_worksheet` treats them the same only because
      both mean "no human has chosen".
    * The **classifier** is passed from this module's globals, so an
      infrastructure error (throttle, access denied) raises rather than
      masquerading as a missing record (P28 R17.3), and
      ``patch.object(_mod, "raise_for_read_client_error", …)`` keeps
      intercepting.

    This function keeps its own ``def`` rather than being an import alias, for
    the reason that record states: roughly 60 test sites across this repository
    patch ``get_pipeline_metadata`` as a module attribute of the Lambda's own
    module, resolved at call time.

    **SEVEN functions in this repository are now named ``get_pipeline_metadata``**
    — this one plus ``schema_mapping``, ``schema_compare``,
    ``create_infrastructure``, ``create_code``, ``create_code/mapping_resolution``
    and ``test_code``. That is the ratified shape and not an accident, but it
    means a grep-driven edit lands on the wrong one easily; CodeGraph reports 30
    affected symbols for the bare name. Anything meant for *this* Lambda names
    the file. It is the same hazard p55 risk R5.8 records for the two functions
    called ``create_approval_payload``.
    """
    pipelines_table = dynamodb.Table(require_env("PIPELINES_TABLE", "agentic_data_ingestion_pipelines"))
    return read_pipeline_record(
        pipelines_table,
        original_pipeline_id,
        step_name="schema_extract",
        operation="get_pipeline_metadata",
        not_found=None,
        logger=logger,
        classify=raise_for_read_client_error,
    )


def _configured_worksheet(pipeline_metadata) -> str | None:
    """The pipeline's Selected_Worksheet, or ``None`` when none is configured.

    p55 R8.3. **Absence is read with a MEMBERSHIP TEST and is never coerced
    away** — ``SELECTED_WORKSHEET_ATTRIBUTE in pipeline_metadata``, not
    ``pipeline_metadata.get(...)`` tested for truth. The distinction is the
    whole point of the function existing rather than being inlined:

    * key **absent** → ``None``. No human has chosen. A single-sheet workbook
      proceeds (R8.1); a multi-sheet one refuses and names the sheets (R8.4
      residual).
    * key **present**, any string including ``""`` → that string. A recorded
      selection naming no sheet is a *selection*, and it must refuse under R8.5
      (naming the configured value and the names present) rather than quietly
      fall back to the workbook's active sheet. Falling back is the one
      behaviour R8.3 exists to prevent, because it ingests different data
      without saying so.

    This is the same distinction ``unmapped_fields``' ``default_value`` already
    draws between a recorded null and an absent key, and it is why
    :func:`_read_excel_table` tests its own parameter with ``is not None``
    rather than for truth.

    A non-string recorded value is returned as its ``str``, deliberately: a
    number-like sheet name round-trips through DynamoDB as a ``Decimal``, and
    coercing here means the R8.5 message reports what was configured rather
    than raising a ``TypeError`` a reader cannot act on.

    Args:
        pipeline_metadata: a pipelines-table record, or ``None`` when the record
            does not exist. A missing record and a record with no selection are
            both "no human has chosen", so both answer ``None``.
    """
    if not isinstance(pipeline_metadata, Mapping):
        return None
    if SELECTED_WORKSHEET_ATTRIBUTE not in pipeline_metadata:
        return None
    configured = pipeline_metadata[SELECTED_WORKSHEET_ATTRIBUTE]
    return configured if isinstance(configured, str) else str(configured)


def _read_excel_table(bucket, key, selected_worksheet=None, *, refuse_ambiguous_multi_sheet=False):
    """Download and parse one Worksheet ONCE; return ``(sheet_name, names, rows)``.

    Both public units below take this table as an argument so the workbook is
    downloaded once and parsed once per invocation — the whole cost argument
    behind design §4.2 (W3-D1) is that converting is *one additional
    ``put_object`` off an object already downloaded and parsed*, and re-opening
    it in the converter would spend that saving.

    **RAISES on any parse failure and never returns an empty table** (R6.5).
    A ``.xlsx`` that is not a zip, a truncated upload, a password-protected
    workbook and a workbook with no sheets all land here as a ``ValueError``
    naming the object and the reason. Returning ``{"type": "object",
    "properties": {}}`` from a binary file instead is the p46 defect exactly:
    the run reports success with zero fields and continues to ``schema_mapping``
    with nothing to map. F5's own failure message names that as the first of the
    four obligations a new ``FORMAT_MAP`` key incurs.

    The whole object is fetched, not a ranged prefix: an ``.xlsx`` is a zip
    whose central directory sits at the END of the file, so a forward-only
    ranged GET cannot open it — the same situation as ORC, solved differently
    because ``openpyxl`` takes a file-like object rather than a seekable
    ``NativeFile``. Rows are materialised because R6.4 needs *every* data row
    converted, not a sample; memory is therefore proportional to the workbook,
    which is the accepted cost of supporting a format Spark cannot read.

    Args:
        bucket: S3 bucket holding the workbook.
        key: S3 key of the workbook.
        selected_worksheet: Worksheet name to process, or ``None`` for the
            workbook's active sheet. Tested with ``is not None`` and NOT for
            truth: ``""`` is a *recorded* selection naming no sheet and refuses
            under R8.5, where coercing it to "absent" would fall back to the
            active sheet and ingest different data. :func:`_configured_worksheet`
            is what turns a pipeline record into this argument, and it draws the
            same distinction on the way in.
        refuse_ambiguous_multi_sheet: whether ``selected_worksheet=None`` means
            *no human has chosen* (``True``) or merely *this caller did not say*
            (``False``, the default).

            This flag exists because those two are genuinely different facts and
            only one of them is R8.4's subject. R8.4 is about a **configured
            pipeline run**: a multi-sheet workbook arriving on a pipeline whose
            record carries no Selected_Worksheet must fail the run and name every
            Worksheet present, because there is a human to direct and no
            defensible sheet to pick. A direct caller that simply passed nothing
            has made no such claim, so the ``workbook.active`` read stays
            reachable for it — which is the behaviour task 4.3's property test
            ``test_the_field_count_matches_the_active_sheet_when_none_is_selected``
            pins over generated **multi-sheet** workbooks, and this task does not
            weaken it.

            ``lambda_handler``'s ``excel`` branch is the one caller that passes
            ``True``, because it is the one caller that has consulted the
            pipeline record and therefore the only one for which absence means
            absence.

            A **single-sheet** workbook proceeds either way (R8.1) — there is no
            ambiguity to resolve, so there is nothing for a human to decide.
    """
    try:
        import io

        import openpyxl
    except ImportError as e:
        # Same message shape as the pyarrow extractors' (p46 R2.6): name the
        # dependency and the layer, because the only way this fires is a layer
        # that did not install `shared/runtime_requirements.txt`.
        raise ValueError(
            "openpyxl is not available in this runtime, so an Excel_Workbook cannot be read. "
            "It is pinned in shared/runtime_requirements.txt and installed into both the "
            "SharedBaseLayer — which is the one THIS function carries — and the "
            "StrandsAgentsLayer; check the layer attachment for this function."
        ) from e

    payload = s3_client.get_object(Bucket=bucket, Key=key)["Body"].read()

    try:
        workbook = openpyxl.load_workbook(io.BytesIO(payload), read_only=True, data_only=True)
    except Exception as e:
        raise ValueError(
            f"s3://{bucket}/{key} carries the xlsx extension but could not be opened as an "
            f"Excel_Workbook ({type(e).__name__}: {e}). An .xlsx is a zip archive; a file that "
            f"is not one, or is truncated or password-protected, cannot be read. Upload the "
            f"workbook again, or upload it as CSV."
        ) from e

    try:
        sheet_names = list(workbook.sheetnames)
        if not sheet_names:
            raise ValueError(
                f"s3://{bucket}/{key} opened as an Excel_Workbook but contains no Worksheet, "
                f"so there is no column to describe."
            )

        # `is not None`, NOT truth: `""` is a recorded selection naming no
        # sheet, so it belongs on the R8.5 arm below and must not be coerced
        # into "nothing was chosen" (design §Data Models).
        if selected_worksheet is not None:
            if selected_worksheet not in sheet_names:
                raise ValueError(
                    f"the configured Worksheet {selected_worksheet!r} is not present in "
                    f"s3://{bucket}/{key}, which contains {sheet_names}. The run is failed "
                    f"rather than falling back to another sheet, which would ingest different "
                    f"data (R8.5). Change or clear the worksheet on this pipeline's Schema tab in "
                    f"the dashboard — or send {SELECTED_WORKSHEET_ATTRIBUTE!r} in a PUT to "
                    f"/api/pipelines/<pipeline_id>/edit, where an empty value clears it — then "
                    f"trigger the run again."
                )
            worksheet = workbook[selected_worksheet]
        elif refuse_ambiguous_multi_sheet and len(sheet_names) > 1:
            # R8.4 residual (design §4.4, decision W3-D5 arm (a)). No human has
            # chosen and the workbook offers more than one answer, so there is
            # no honest sheet to read. Names EVERY Worksheet present — that is
            # the list the user has to choose from, so a refusal that withheld
            # it would send them back to the file — and says where the choice
            # goes, because the selection is pipeline configuration and not
            # something to retry the run with.
            #
            # It does NOT default to the first sheet, and it does not fall
            # through to `workbook.active`. Both would ingest one sheet's data
            # while the user believed another was in play, which is the failure
            # R8.3 exists to prevent.
            raise ValueError(
                f"s3://{bucket}/{key} contains more than one Worksheet ({sheet_names}) and this "
                f"pipeline has no {SELECTED_WORKSHEET_ATTRIBUTE!r} configured, so which sheet to "
                f"ingest is not determined. The run is failed rather than defaulting to one of "
                f"them, which would ingest different data than intended (R8.4). Set the worksheet "
                f"on this pipeline's Schema tab in the dashboard — or send "
                f"{SELECTED_WORKSHEET_ATTRIBUTE!r} in a PUT to /api/pipelines/<pipeline_id>/edit — "
                f"then trigger the run again. The pipeline does not need to be recreated."
            )
        else:
            worksheet = workbook.active if workbook.active is not None else workbook[sheet_names[0]]

        rows = worksheet.iter_rows(values_only=True)
        try:
            header = next(rows)
        except StopIteration:
            raise ValueError(
                f"Worksheet {worksheet.title!r} in s3://{bucket}/{key} is empty, so it has no "
                f"header row to name columns from (R6.2)."
            ) from None

        field_names = _excel_field_names(header)
        if not field_names:
            raise ValueError(
                f"Worksheet {worksheet.title!r} in s3://{bucket}/{key} has a header row with no "
                f"cells, so no field can be named from it. An empty schema here would report "
                f"success with zero fields."
            )

        width = len(field_names)
        data_rows = [
            # Pad and trim to the header's width so every written row has the
            # same arity as the declared schema. `read_only` worksheets return
            # ragged tuples for sheets with trailing blanks.
            [("" if value is None else value) for value in (list(row) + [None] * width)[:width]]
            for row in rows
        ]
        return worksheet.title, field_names, data_rows
    finally:
        # `read_only=True` keeps the underlying zip handle open until closed.
        workbook.close()


def extract_schema_from_excel(bucket, key, selected_worksheet=None, table=None) -> dict[str, Any]:
    """Extract schema from an Excel_Workbook — one field per Worksheet column.

    R6.2: one field per column of the processed Worksheet, named from that
    column's header cell. Returns the same ``{"type": "object", "properties":
    {…}}`` shape its four siblings return, and it **RAISES rather than
    returning an empty ``properties``** (R6.5) — see :func:`_read_excel_table`
    for why that is not a preference.

    **Every field is declared ``string``, deliberately.** The workbook's own
    cell types are richer than that, and they are discarded because the frame
    the generated Glue job receives is not the workbook: it is the converted
    CSV, read with ``.option("inferSchema", False)``
    (``glue_job_glue_catalog.py.j2:51`` and siblings), so every column arrives
    as a string. Declaring ``integer`` for a column Spark will hand over as a
    string would describe the *workbook* rather than the frame, and the mapping
    and transform downstream reason about the frame. This is also exactly what
    ``extract_schema_from_csv`` already declares: S3 Select returns CSV cells as
    strings, so ``infer_schema_from_records`` types them ``string`` too. One
    format family, one answer.

    Args:
        bucket: S3 bucket holding the workbook.
        key: S3 key of the workbook.
        selected_worksheet: see :func:`_read_excel_table`.
        table: an already-parsed ``(sheet_name, field_names, rows)`` triple, so
            the dispatch can extract and convert off ONE parse. ``None`` parses
            the object here, which is what a direct caller and the unit tests
            do.

    Returns:
        dict: ``{"type": "object", "properties": {name: {"type": "string"}, …}}``

    Raises:
        ValueError: the object cannot be opened as a workbook, carries no
            Worksheet, names a Worksheet it does not contain, or yields no
            field.
    """
    sheet_name, field_names, rows = table if table is not None else _read_excel_table(bucket, key, selected_worksheet)

    schema = {"type": "object", "properties": {name: {"type": "string"} for name in field_names}}

    if not schema["properties"]:
        # Unreachable via `_read_excel_table`, which raises first. Kept for a
        # caller that supplies its own `table`, because the "never an empty
        # properties" obligation (R6.5) belongs to this function's contract and
        # not only to its default parse path — the same reason
        # `extract_schema_from_avro` polices `avro_schema_to_json_schema`'s
        # output rather than trusting it.
        raise ValueError(
            f"Excel_Workbook s3://{bucket}/{key} yielded no fields. Returning an empty schema "
            f"here would report success with zero fields, which is the defect this extractor "
            f"exists to remove."
        )

    logger.info(
        f"Excel_Workbook: {len(schema['properties'])} fields from Worksheet {sheet_name!r} "
        f"({len(rows)} data rows) of s3://{bucket}/{key}"
    )
    return schema


def convert_source_to_csv(bucket, key, run_id, pipeline_id, selected_worksheet=None, table=None) -> str:
    """Write the Worksheet as CSV to the processed bucket; return its ``s3://`` URI.

    R13.3: a source whose Canonical_Format is in the Conversion_Required_Set is
    converted to a Native_Format_Set member before the Glue job reads it. The
    returned URI becomes ``readable_source_uri``.

    The name matters: R13.6's contract guard AST-reads the ``excel`` dispatch
    branch for a call to a ``convert_source_to_*`` function, exactly as F5's
    guard 4 reads it for ``extract_schema_from_*``. Rename rather than relax
    that check.

    R6.4 — one target row per data row of the processed Worksheet — is why every
    row is written rather than a sample, and why the header the CSV carries is
    the same :func:`_excel_field_names` list the schema declares: the Glue read
    passes ``.option("header", True)``, so a header that disagreed with the
    schema would rename every column at read time.

    The key is per-run (``converted/{pipeline_id}/{run_id}/source.csv``), which
    makes a retry of the same run idempotent — same key, same bytes — while
    never letting one run read another run's converted object.

    Args:
        bucket: S3 bucket holding the workbook.
        key: S3 key of the workbook.
        run_id: the run this conversion belongs to; part of the object's key.
        pipeline_id: the pipeline this run belongs to; part of the object's key.
        selected_worksheet: see :func:`_read_excel_table`.
        table: an already-parsed triple, as in :func:`extract_schema_from_excel`.

    Returns:
        str: the converted object's ``s3://`` URI.

    Raises:
        ValueError: the workbook cannot be read (see :func:`_read_excel_table`),
            or the processed bucket cannot be resolved, or the write fails.
            A conversion that did not happen must not be reported as one — the
            URI would name an object the Glue job then cannot read.
    """
    import csv as _csv
    import io

    sheet_name, field_names, rows = table if table is not None else _read_excel_table(bucket, key, selected_worksheet)

    # `PROCESSED_BUCKET` **is** now set on this function — `compute-stack.ts`
    # sets it in `schemaExtraction`'s own `environment` block, alongside the
    # three functions (`create_code`, `test_code`, `dashboard_api`) that already
    # carried it. So `os.environ.get` is the live path and the name-derivation
    # below is a genuine fallback rather than the only route.
    #
    # The two agree by construction, which is why swapping which one runs is not
    # a behaviour change in the value: foundation-stack declares the buckets as
    # `adi-source-files-{account}-{region}` and
    # `adi-processed-files-{account}-{region}` (`foundation-stack.ts:75`, `:88`),
    # so `replace` on the former yields exactly the latter — the same naming
    # `create_glue_pipeline/iam_roles.py:373-395`, `:441-460` grants the Glue
    # role by wildcard. Verified by reading both declarations, not assumed.
    #
    # Keep the fallback. It is what makes the refusal below reachable, and it
    # covers a deploy that lands this code before the variable — an
    # Orchestration-before-Compute ordering, which the steering file warns is
    # exactly the individual-deploy case CDK does not order for you.
    target_bucket = os.environ.get("PROCESSED_BUCKET") or bucket.replace("adi-source-files-", "adi-processed-files-", 1)
    if not target_bucket:
        raise ValueError(
            f"cannot resolve a destination bucket for the converted form of s3://{bucket}/{key}; "
            f"set PROCESSED_BUCKET on this function."
        )
    if target_bucket == bucket:
        # REFUSE rather than write the converted object back into the bucket it
        # came from. `str.replace` returns its input unchanged when the pattern
        # does not match, so a source bucket not carrying the
        # `adi-source-files-` prefix resolves to ITSELF — and the source bucket
        # is watched by the S3-upload EventBridge rule, so a converted object
        # landing there re-triggers the pipeline on its own output. That is a
        # loop, and it would look like a duplicate upload rather than like a
        # misconfiguration. Fail-closed: a conversion that cannot be written
        # somewhere safe is not reported as one.
        raise ValueError(
            f"the converted form of s3://{bucket}/{key} would be written back into the source "
            f"bucket {bucket!r}, which the S3-upload EventBridge rule watches — the pipeline "
            f"would re-trigger on its own output. Set PROCESSED_BUCKET on this function."
        )
    target_key = _CONVERTED_SOURCE_KEY.format(pipeline_id=pipeline_id or "unknown", run_id=run_id or "unknown")

    buffer = io.StringIO()
    # `QUOTE_MINIMAL` with the default comma: a cell containing a comma, a quote
    # or a newline is quoted, which is what makes the round trip through
    # `spark.read.csv` lossless. The delimiter is a comma, so
    # `delimiter_for_key` on the converted `.csv` key answers a comma too
    # (R4.6) and the Glue read's `sep` matches what was written.
    writer = _csv.writer(buffer, lineterminator="\n")
    writer.writerow(field_names)
    writer.writerows(rows)

    try:
        s3_client.put_object(
            Bucket=target_bucket,
            Key=target_key,
            Body=buffer.getvalue().encode("utf-8"),
            ContentType="text/csv",
        )
    except Exception as e:
        raise ValueError(
            f"failed to write the converted form of s3://{bucket}/{key} to "
            f"s3://{target_bucket}/{target_key} ({type(e).__name__}: {e}). The run is failed "
            f"rather than reporting a readable_source_uri naming an object that is not there."
        ) from e

    converted_uri = f"s3://{target_bucket}/{target_key}"
    logger.info(
        f"Converted Worksheet {sheet_name!r} of s3://{bucket}/{key} to {converted_uri} "
        f"({len(rows)} data rows, {len(field_names)} columns)"
    )
    return converted_uri


def compute_schema_hash(schema: dict) -> str:
    """Deterministic SHA-256 hash of a JSON schema dict."""
    schema_json = json.dumps(schema, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(schema_json.encode("utf-8")).hexdigest()


def store_schema_in_dynamodb(
    run_id,
    original_pipeline_id,
    schema,
    json_s3_uri,
    execution_arn=None,
    sample_s3_uri=None,
    readable_source_uri=None,
    readable_source_format=None,
    processed_worksheet=None,
):
    """Store extracted schema in DynamoDB using new run_id + sequence structure

    ``sample_s3_uri`` (p53 C3) is an OPTIONAL, ADDITIONAL attribute naming a
    bounded sample of the source rows written by the generated Glue job
    (``pyspark_templates/glue_job_jdbc_source.py.j2``). It is written to the
    item only when non-empty, so a pipeline with no Sample_Artifact carries no
    such key at all — the shape every consumer's ``.get(...)``-with-fallback
    already tolerates.

    It does NOT replace ``json_s3_uri``, and ``json_s3_uri`` must not be
    repointed at it (p53 do-NOT 11). Three other readers take ``json_s3_uri``
    to mean the canonical source URI and break on a repoint:
    ``dashboard_api/handlers/data_preview.py`` (renders it as the data),
    ``dashboard_api/handlers/files.py`` (derives the display filename from its
    last path segment), and this module's own legacy extraction path (extracts
    a schema *from* it).

    ``readable_source_uri`` / ``readable_source_format`` (p55 R13.3, R13.4) are
    the Conversion_Required_Set resolution: the object the generated Glue job
    should read, and the Canonical_Format it should be read AS. For every
    Native_Format_Set member the URI equals ``json_s3_uri``; for an
    Excel_Workbook it names the converted CSV. **This row is the read path** —
    the three consumers on the Glue-read path
    (``create_glue_pipeline/artifacts.py::get_source_path``,
    ``create_code``'s ``source_object_uri``, and ``test_code`` via the existing
    ``source_sample_uri`` payload) take them from here in Python rather than
    through a new ASL dereference, so a pre-p55 row is a ``.get()`` that falls
    back instead of an uncatchable ``States.ParameterPathFailure`` (task 4.4's
    measurement: 19 replayable runs, 17 of them at ``test_code``).

    Both are written UNCONDITIONALLY when supplied — unlike ``sample_s3_uri``,
    whose absence carries the meaning "there is no sample". Absence of these two
    means only "this row predates p55", which is what makes the consumers'
    fallback correct rather than a guess: no Conversion_Required_Set format
    existed before p55, so every pre-p55 row's readable object IS its original.

    ``processed_worksheet`` (p55 R8.2) names the Worksheet this run actually
    read, and follows ``sample_s3_uri``'s conditional-attribute shape rather
    than the pair's unconditional one — deliberately, and the two differ because
    absence means different things. For the readable pair, absence means "pre-p55
    row" and a consumer must fall back. For this attribute, absence means "this
    source has no Worksheet at all", which is true of every format but Excel and
    is not something to fall back from. So a CSV run's row carries no such key,
    exactly as it carries no ``sample_s3_uri``, and a key's presence answers
    R8.2's question without a reader having to know the source format.

    The envelope is the other way round — ``processed_worksheet`` is always
    present there, empty on the paths with no Worksheet — because a Step
    Functions payload path that is sometimes absent raises an uncatchable
    ``States.ParameterPathFailure`` while a DynamoDB attribute that is sometimes
    absent is a ``.get()``. Same value, two audiences, and the difference in
    shape follows from which failure each audience has.
    """
    try:
        timestamp = datetime.now(UTC).isoformat()
        schema_hash = compute_schema_hash(schema)

        # Use run_id as PK directly — no synthetic pipeline_id needed
        pipeline_id = original_pipeline_id  # For PipelineIndex GSI

        # Build standardized output_data
        output_data = create_output_data(
            artifact_type="schema_extraction",
            primary_id=schema_hash,
            status="success",
            summary={
                "field_count": len(schema.get("properties", {})),
                "schema_hash": schema_hash,
                "is_new_schema": True,
            },
            s3_uris={},
        )

        item = {
            "run_id": run_id,  # Partition key
            "step_sequence": 2,  # Step number in pipeline
            "step_name": "schema_extract",
            "status": "success",
            "timestamp": timestamp,
            "extracted_schema": json.dumps(schema),
            "json_s3_uri": json_s3_uri,
            "schema_hash": schema_hash,
            "pipeline_id": pipeline_id,  # For PipelineIndex GSI
            "original_pipeline_id": original_pipeline_id,  # Groups all runs for this pipeline
            "output_data": output_data,
        }

        # Add execution ID if available
        if execution_arn:
            item["execution_id"] = execution_arn

        # p53 C3: present only when a Sample_Artifact exists for this pipeline.
        # Absent — not empty-string — otherwise, so a consumer's
        # ``item.get("sample_s3_uri") or item["json_s3_uri"]`` preference reads
        # naturally and the attribute's presence means "there is a sample".
        if sample_s3_uri:
            item["sample_s3_uri"] = sample_s3_uri

        # p55 R13.3/R13.4. Written together or not at all: a row carrying the
        # URI without the format would let a consumer repoint its read and keep
        # the old format, which is task 4.1's finding 2 (a converted CSV read as
        # JSON) reintroduced one layer down.
        if readable_source_uri:
            item["readable_source_uri"] = readable_source_uri
            item["readable_source_format"] = readable_source_format or ""

        # p55 R8.2. Present only when the source HAS a Worksheet, so the
        # attribute's presence is itself the answer — see the docstring on why
        # this is conditional where the pair above is not.
        if processed_worksheet:
            item["processed_worksheet"] = processed_worksheet

        table.put_item(Item=item)
        logger.info(f"Schema stored - run_id: {run_id}, pipeline_id: {pipeline_id}, schema_hash: {schema_hash}")

        return schema_hash

    except Exception as e:
        logger.error(f"Error storing schema in DynamoDB: {e}")
        raise


def get_previous_state(run_id):
    """Get the previous file validation state from DynamoDB using run_id"""
    try:
        # Query for step 1 (file validation) in this run — direct base table lookup
        response = table.get_item(Key={"run_id": run_id, "step_name": "file_validation"})

        if "Item" in response:
            return response["Item"]
        return None

    except Exception as e:
        logger.error(f"Error getting previous state: {e}")
        raise


def _glue_columns_to_json_schema(columns: list, partition_keys: list | None = None) -> dict:
    """Convert Glue Catalog columns to a JSON Schema-shaped dict.

    Mirrors the JSON Schema shape produced by the existing S3 path
    (:func:`extract_json_schema` / :func:`infer_schema_from_records`) so
    downstream consumers (`schema_compare`, `schema_mapping`) see an
    identical row shape regardless of source format. Both regular columns
    (``StorageDescriptor.Columns``) and partition keys (``PartitionKeys``)
    are surfaced as ordinary fields — Glue crawlers occasionally promote
    indexed columns to partition keys for JDBC sources, and downstream
    code does not need to distinguish.

    Glue type → JSON Schema mapping is intentionally permissive. ``decimal``
    (with or without precision/scale), ``double``, and ``float`` collapse to
    JSON Schema ``number``; integer family collapses to ``integer``;
    timestamp/date are rendered as ISO strings. Anything unrecognised
    falls back to ``string`` so the schema is always usable, even for
    engine-specific types we have not enumerated.

    Args:
        columns: ``[{Name, Type, Comment?}]`` from
            ``Table.StorageDescriptor.Columns``.
        partition_keys: ``[{Name, Type, Comment?}]`` from
            ``Table.PartitionKeys``. Optional.

    Returns:
        ``{"type": "object", "properties": {<field>: {"type": <json_type>}}}``
        with all field names listed in ``required`` (Glue Catalog does not
        preserve nullability, so we report all fields as required to match
        the S3 path's behaviour for object schemas).
    """
    properties: dict = {}
    all_columns = list(columns or []) + list(partition_keys or [])

    for column in all_columns:
        name = column.get("Name", "")
        if not name:
            continue
        glue_type = (column.get("Type", "string") or "string").lower()

        if glue_type in ("bigint", "int", "integer", "smallint", "tinyint", "long"):
            json_type = "integer"
        elif glue_type in ("double", "float", "real") or glue_type.startswith("decimal"):
            json_type = "number"
        elif glue_type == "boolean":
            json_type = "boolean"
        elif glue_type in ("timestamp", "date") or glue_type.startswith("timestamp"):
            # ISO 8601 string — JSON Schema has no native date/timestamp
            # type, and the existing CSV / JSON paths render dates as
            # strings as well, so we stay consistent.
            json_type = "string"
        else:
            # ``string``, ``varchar(...)``, ``char(...)``, ``binary``,
            # ``array<...>``, ``struct<...>``, plus anything we have not
            # enumerated. Falling back to ``string`` keeps the schema
            # usable for downstream codegen even when Glue surfaces an
            # engine-specific type.
            json_type = "string"

        properties[name] = {"type": json_type}

    return {
        "type": "object",
        "properties": properties,
        "required": list(properties.keys()),
    }


# ── p53 C3: the Sample_Artifact probe ───────────────────────────────────
# The generated Glue job writes a bounded sample of the rows it has ALREADY
# read to one deterministic key (p53 task 2.1, the sample block in
# ``pyspark_templates/glue_job_jdbc_source.py.j2``):
#
#     s3://adi-processed-files-{account}-{region}/samples/{pipeline_id}/source-sample.json
#
# This Lambda only PROBES that key and records the URI when it is there. It
# never writes the artifact, never reads the source database, and never
# constructs SQL — the whole point of mechanism (d) is that the component
# holding the credentials and the VPC reach is the Glue job, not this Lambda
# (p53 C2, do-NOT 6, do-NOT 7).
#
# The key is a CONTRACT shared with that template. Change one side and the
# other stops finding the object; there is no lookup table to fall back on.
_SAMPLE_ARTIFACT_KEY = "samples/{pipeline_id}/source-sample.json"


def _sample_artifact_bucket(context=None) -> str:
    """Resolve the processed-files bucket holding the Sample_Artifact.

    ``processedBucket`` is the only bucket all four sample consumers can read
    (p53 design §"The S3 read scopes"), and ``schemaExtractRole`` already holds
    ``s3:GetObject``/``s3:ListBucket`` on it (``foundation-stack.ts:262``), so
    the probe needs no IAM change.

    Two sources, in order:

    1. ``PROCESSED_BUCKET`` — what the deploy sets on the Lambdas that need the
       bucket by name (``compute-stack.ts:395``, ``:426``, ``:802``). This
       Lambda is not currently one of them; preferring it means a future deploy
       that adds it, or a bucket rename, is picked up for free.
    2. The invocation's own ARN, ``arn:aws:lambda:{region}:{account}:function:…``
       — segments 3 and 4 are exactly what ``adi-processed-files-{account}-{region}``
       needs. No API call, so no STS round trip and no VPC-endpoint dependency
       beyond the probe's own. This mirrors how the Glue job derives the same
       name from ``--secret_arn``.

    Returns ``""`` when neither yields a name, which the caller treats as
    "no sample" rather than as an error.
    """
    configured = os.environ.get("PROCESSED_BUCKET")
    if configured:
        return configured

    arn = getattr(context, "invoked_function_arn", None)
    if isinstance(arn, str):
        segments = arn.split(":")
        # arn:aws:lambda:<region>:<account>:function:<name>
        if len(segments) > 4 and segments[3] and segments[4]:
            return f"adi-processed-files-{segments[4]}-{segments[3]}"
    return ""


def _probe_sample_artifact_uri(pipeline_id, context=None) -> str:
    """Return the Sample_Artifact's ``s3://`` URI, or ``""`` when it is absent.

    A HEAD, never a GET: this function decides whether the object exists, not
    what is in it. The consumers read the body themselves.

    **Absence is the ordinary state, not a failure.** Run 1 of a JDBC pipeline
    has no artifact because nothing has read its source yet (the First_Run_Gap,
    p53 design C4), so a miss is logged at INFO and yields ``""``. Any other
    probe failure — throttling, ``AccessDenied``, a transient S3 error — is
    logged at WARNING and also yields ``""``: a telemetry probe must not fail a
    schema extraction that otherwise succeeded, which is the same posture the
    ``jdbc://`` fallback in :func:`handle_jdbc_schema_extract` already takes
    ("rather than failing closed — the schema itself is the artifact we care
    about"). This mirrors the two-armed INFO/WARNING split the p49 sample
    readers use for the same distinction.
    """
    bucket = _sample_artifact_bucket(context)
    if not bucket or not pipeline_id:
        logger.info(
            "No source sample probed: could not resolve the sample location "
            f"(bucket={bucket!r}, pipeline_id={pipeline_id!r})"
        )
        return ""

    key = _SAMPLE_ARTIFACT_KEY.format(pipeline_id=pipeline_id)
    try:
        s3_client.head_object(Bucket=bucket, Key=key)
    except Exception as exc:
        # ``s3_client.exceptions.ClientError`` is not usable in an ``except``
        # clause under the unit suite's mocked client, so classify off the
        # botocore error payload and fall back to WARNING when it is absent.
        code = str((getattr(exc, "response", None) or {}).get("Error", {}).get("Code", ""))
        if code in ("404", "403", "NoSuchKey", "NotFound"):
            # 403 lands here too: S3 answers HeadObject on a missing key with
            # 403 rather than 404 when the caller lacks ListBucket, and either
            # way there is nothing to hand a consumer.
            logger.info(f"No source sample yet for pipeline {pipeline_id} at s3://{bucket}/{key}")
        else:
            logger.warning(f"Could not probe the source sample at s3://{bucket}/{key}: {type(exc).__name__}: {exc}")
        return ""

    uri = f"s3://{bucket}/{key}"
    logger.info(f"Source sample found for pipeline {pipeline_id}: {uri}")
    return uri


def handle_jdbc_schema_extract(event, context):
    """JDBC schema extraction entry point — triggers the per-pipeline
    runtime crawler, polls until done, and stores the resulting schema.

    Flow (Requirement 13.3):

        1. Resolve ``pipeline_id``, ``run_id``, ``original_pipeline_id``,
           and ``execution_arn`` from the event. ``pipeline_id`` is read
           from ``event["original_pipeline_id"]`` because the file_validation
           step (Task 6.2) writes BOTH ``pipeline_id`` and
           ``original_pipeline_id`` to the same value for JDBC sources
           (JDBC has no schema-evolution clone semantics — see
           ``handle_jdbc_source_validation`` and Requirement 13.1).
        2. Start the per-pipeline runtime crawler
           ``agentic-data-ingestion-{pipeline_id}-source-discover`` (created by Task 5.2's
           ``create_pipeline`` handler). On
           :exc:`CrawlerRunningException` we treat the in-flight run as
           ours and skip the start call rather than failing — a
           concurrent Step Functions execution targeting the same
           pipeline produces the same table either way.
        3. Poll :func:`glue:GetCrawler` every 5 seconds with a 3-minute
           cap. The crawler is "done" when ``State == "READY"`` AND
           ``LastCrawl`` carries a status (``SUCCEEDED`` / ``FAILED`` /
           ``CANCELLED``). Anything else (``RUNNING``, ``STOPPING``, or
           a missing ``LastCrawl``) is treated as still in-flight.
        4. Reject ``FAILED`` and ``CANCELLED`` outcomes with the
           crawler's ``ErrorMessage`` so operators can debug.
        5. Locate the produced table via
           :func:`glue:GetTables(DatabaseName=DISCOVERY_DB, Expression="agentic_data_ingestion_{pipeline_id}_*")`.
           This deliberately avoids relying on the wizard-time-persisted
           ``detected_glue_table`` name (a different ``TablePrefix`` and
           subject to the wizard janitor's 7-day TTL — see Requirement 13.3).
        6. Apply Task 7.3's zero/multi-match guards: 0 tables raises
           ``ValueError("Crawler completed but produced no table")``; >1
           tables raises ``ValueError(f"Crawler produced unexpected
           tables: {names}")``.
        7. Convert the table's ``StorageDescriptor.Columns + PartitionKeys``
           to JSON Schema via :func:`_glue_columns_to_json_schema` and
           call :func:`store_schema_in_dynamodb` with the synthetic
           ``jdbc://...`` URI from the file_validation row (no real S3
           file backs a JDBC source).

    Returns the S3 path's response shape plus one key. Every field the Step
    Functions state machine reads off ``$.schema_extract_result`` is present in
    both arms; this arm additionally carries ``sample_s3_uri``, which no ASL
    path reads. See the return statement's comment for why that distinction is
    the one that matters.

    Validates: Requirement 13.3.
    """
    import time as _time

    _metrics_start = _time.time()

    logger.info(f"JDBC schema extraction event: {json.dumps(event)}")

    # Same accessor names as the legacy path (lambda_handler reads
    # ``original_pipeline_id`` + ``run_id`` directly off the event).
    # file_validation (Task 6.2) writes them both to the same value
    # for JDBC pipelines, so reading either is correct — we use
    # ``original_pipeline_id`` for symmetry with the S3 path.
    run_id = event.get("run_id")
    original_pipeline_id = event.get("original_pipeline_id")
    execution_arn = event.get("execution_arn")

    if not run_id:
        raise ValueError("Run ID not provided in event")
    if not original_pipeline_id:
        raise ValueError("original_pipeline_id not provided in event")

    pipeline_id = original_pipeline_id  # Convenient alias for crawler/prefix names.
    crawler_name = f"agentic-data-ingestion-{pipeline_id}-source-discover"
    table_prefix = f"agentic_data_ingestion_{pipeline_id}_"

    logger.append_keys(run_id=run_id, pipeline_id=pipeline_id, step_name="SchemaExtract")
    logger.info(
        f"JDBC schema extraction starting: crawler={crawler_name}, "
        f"discovery_db={DISCOVERY_DB}, table_prefix={table_prefix}"
    )

    try:
        # ---------------------------------------------- start the crawler
        try:
            glue_client.start_crawler(Name=crawler_name)
            logger.info(f"Started runtime crawler: {crawler_name}")
        except glue_client.exceptions.CrawlerRunningException:
            # Concurrent execution — there is already a run in flight for
            # this pipeline's crawler. The in-flight run produces the
            # same table either way, so fall through to the polling loop
            # rather than starting a new one. Mirrors the wizard-time
            # behaviour in handlers/jdbc_resources.py::detect_schema_start.
            logger.info(f"Crawler {crawler_name} is already running; waiting for the existing run to complete")

        # ----------------------------------------------------- poll loop
        # Crawler wall-clock cap. A JDBC discovery crawl does ~3 min of
        # actual work, but the crawler's State stays RUNNING well beyond
        # that while Glue provisions and tears down the crawler — so the
        # old 3-minute cap fired right as the crawl finished, raising a
        # spurious TimeoutError even though the crawl succeeded. 840s
        # (14 min) sits under the Lambda's 900s ceiling with headroom for
        # the post-poll get_tables read + state write. 5-second sleep
        # keeps us gentle on the Glue control-plane API.
        timeout_seconds = 840
        poll_interval = 5
        deadline = _time.time() + timeout_seconds
        last_crawl: Mapping[str, Any] = {}

        while True:
            crawler_resp = glue_client.get_crawler(Name=crawler_name)
            crawler = crawler_resp.get("Crawler", {}) or {}
            state = crawler.get("State", "")
            last_crawl = crawler.get("LastCrawl") or {}
            last_status = last_crawl.get("Status", "")

            # The crawler is "done" when ``State`` flips back to ``READY``
            # AND ``LastCrawl.Status`` carries the run outcome. Glue's
            # ``STOPPING`` is transient between RUNNING and READY; treat
            # both ``RUNNING`` and ``STOPPING`` (and the rare empty
            # ``LastCrawl``) as still-in-flight so we keep polling.
            if state == "READY" and last_status:
                logger.info(f"Crawler {crawler_name} finished with status={last_status}")
                break

            if _time.time() >= deadline:
                raise TimeoutError(f"Crawler {crawler_name} did not complete within {timeout_seconds // 60} minutes")

            _time.sleep(poll_interval)

        # ----------------------------------------------- reject failures
        if last_crawl.get("Status") == "FAILED":
            error_message = last_crawl.get("ErrorMessage") or "Crawler reported FAILED with no error message"
            raise RuntimeError(f"Crawler {crawler_name} failed: {error_message}")

        if last_crawl.get("Status") == "CANCELLED":
            raise RuntimeError(f"Crawler {crawler_name} was cancelled before completion")

        # --------------------------------------------------- get_tables
        tables_response = glue_client.get_tables(
            DatabaseName=DISCOVERY_DB,
            Expression=table_prefix + "*",
        )
        table_list = tables_response.get("TableList", []) or []

        # ---------------------------------- Task 7.3: zero/multi guards
        if len(table_list) == 0:
            raise ValueError("Crawler completed but produced no table")
        if len(table_list) > 1:
            names = sorted(t.get("Name", "") for t in table_list)
            raise ValueError(f"Crawler produced unexpected tables: {names}")

        # ------------------------------------------- happy path: 1 match
        table_meta = table_list[0]
        glue_table_name = table_meta.get("Name", "")
        storage_descriptor = table_meta.get("StorageDescriptor", {}) or {}
        columns = storage_descriptor.get("Columns", []) or []
        partition_keys = table_meta.get("PartitionKeys", []) or []

        schema = _glue_columns_to_json_schema(columns, partition_keys)
        logger.info(
            f"Extracted JDBC schema from {DISCOVERY_DB}.{glue_table_name}: {len(schema.get('properties', {}))} fields"
        )

        # ---------------------------------------------- pull synthetic URI
        # The file_validation step (Task 6.2) wrote ``original_s3_uri =
        # jdbc://{engine}/{schema}/{table}`` for traceability. Re-use it
        # here as the canonical URI for the schema row so the lineage
        # stays consistent across steps. If file_validation is missing
        # for any reason, fall back to a deterministic synthetic URI
        # built from the pipeline_id rather than failing closed — the
        # schema itself is the artifact we care about.
        previous_state = get_previous_state(run_id)
        json_s3_uri = (previous_state or {}).get("original_s3_uri") or f"jdbc://pipeline/{pipeline_id}"

        # ------------------------------------------ p53: sample artifact
        # ``json_s3_uri`` above is UNCHANGED and must stay so — it is not
        # repointed at the sample (p53 do-NOT 11 / design C3). The sample is
        # exposed as a separate optional attribute instead, so each consumer
        # can prefer it and fall back, and the readers that mean "canonical
        # source URI" by ``json_s3_uri`` keep getting that.
        #
        # A miss is expected on run 1 and is never a step failure; see
        # :func:`_probe_sample_artifact_uri`.
        sample_s3_uri = _probe_sample_artifact_uri(pipeline_id, context)

        # The resolved URI a Sample_Reader should be handed: the Sample_Artifact
        # when there is one, the canonical source URI otherwise. The preference
        # is resolved HERE, in Python, because Amazon States Language cannot
        # express it — see the return's comment below.
        source_sample_uri = sample_s3_uri or json_s3_uri

        # p55 R13.4 on this arm: there is no S3 source object, so there is
        # nothing to convert and nothing to gate. ``json_s3_uri`` here is the
        # ``jdbc://…`` sentinel `file_validation` wrote, and the JDBC template's
        # frame comes from ``spark.read.format("jdbc")`` — ``job_builders.py:603``
        # pops ``--source_path`` outright for a JDBC source — so the readable
        # pair is the sentinel and the format itself. The keys exist on this arm
        # for `LGR-5c539a6-2`'s reason and not because anything derives a read
        # from them.
        readable_source_uri = json_s3_uri
        readable_source_format = "jdbc"

        # ------------------------------------- store using shared helper
        schema_hash = store_schema_in_dynamodb(
            run_id=run_id,
            original_pipeline_id=original_pipeline_id,
            schema=schema,
            json_s3_uri=json_s3_uri,
            execution_arn=execution_arn,
            sample_s3_uri=sample_s3_uri,
            readable_source_uri=readable_source_uri,
            readable_source_format=readable_source_format,
        )

        # ------------------------------------ EventBridge + stage metrics
        metrics = {
            "duration_ms": int((_time.time() - _metrics_start) * 1000),
            "schema_fields_extracted": len(schema.get("properties", {})),
            "extraction_method": "jdbc_crawler",
        }

        publish_pipeline_state_change(
            pipeline_id=original_pipeline_id,
            execution_id=execution_arn or "unknown",
            stage="agentic_data_ingestion_schema_extract",
            status="completed",
            run_id=run_id,
            metrics=metrics,
        )

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="SchemaExtract",
                    pipeline_id=original_pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: stage metrics are best-effort telemetry; a publish failure
                # must not fail a successful JDBC schema extraction (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish SchemaExtract (JDBC completed) stage metrics for "
                    f"pipeline_id={original_pipeline_id} run_id={run_id}: {type(exc).__name__}: {exc}"
                )

        # Almost the S3 path's ``lambda_handler`` envelope: both arms carry the
        # same nine keys, and this arm carries one more, ``sample_s3_uri``. The
        # arms are NOT byte-identical in shape and the earlier claim that they
        # were stopped being true when p53 task 3.1 added ``sample_s3_uri`` to
        # this arm alone. What still holds — and is the property the state
        # machine actually needs — is that every key any ASL path reads off
        # ``$.schema_extract_result`` exists in BOTH arms. Nothing reads
        # ``sample_s3_uri`` from the state machine; it is for the stored row and
        # for diagnosis.
        #
        # ``source_sample_uri`` is the resolved URI a Sample_Reader should be
        # handed: the Sample_Artifact when one exists, ``json_s3_uri``
        # otherwise. Resolving the preference in Python rather than in the
        # payload mapping is not a style choice — ASL has no coalesce and no
        # conditional intrinsic, so no ``…Payload`` expression can read a
        # possibly-absent path safely. Step Functions resolves every
        # ``$.schema_extract_result.…`` reference at state entry and raises
        # States.ParameterPathFailure when the path is missing, which neither
        # Retry nor Catch can intercept (``orchestration-stack.ts:338-340``).
        #
        # This mirrors RESUME_MARKER_PATH (``orchestration-stack.ts:332-350``),
        # which solves the identical problem the identical way: FileValidation
        # echoes the marker into its own result "defaulting to '', never
        # absent", so the path is "guaranteed-present by construction […] so it
        # cannot raise the States.ParameterPathFailure that no Retry or Catch
        # can intercept". ``source_sample_uri`` is present on every run of both
        # arms for exactly that reason, and must stay so.
        #
        # ``sample_s3_uri`` stays alongside it (p53 do-NOT 11 keeps
        # ``json_s3_uri`` too). It is the honest "is there a sample" signal —
        # empty when there is none — where ``source_sample_uri`` deliberately
        # collapses that distinction in favour of always being readable.
        return {
            "statusCode": 200,
            "run_id": run_id,
            "original_pipeline_id": original_pipeline_id,
            "schema_hash": schema_hash,
            "extracted_schema": schema,
            "json_s3_uri": json_s3_uri,
            "sample_s3_uri": sample_s3_uri,
            "source_sample_uri": source_sample_uri,
            # p55 R13.4 — present on this arm too, so every key an ASL path
            # could read off ``$.schema_extract_result`` exists on BOTH arms
            # (`LGR-5c539a6-2`). See the S3 arm's return for why no ASL path
            # reads them today.
            "readable_source_uri": readable_source_uri,
            "readable_source_format": readable_source_format,
            # p55 R8.2 — always ``""`` on this arm: a JDBC source has no
            # Worksheet. Present anyway, for `LGR-5c539a6-2`'s reason — a key an
            # ASL path could read must exist on BOTH arms, and the way that is
            # made true is to echo it unconditionally rather than to add a
            # Choice that tests for it.
            "processed_worksheet": "",
            "source_format": "jdbc",
            "message": "Schema extraction completed successfully",
        }

    except Exception as exc:
        logger.error(f"Error in JDBC schema extraction: {exc!s}")

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="SchemaExtract",
                    pipeline_id=original_pipeline_id or "unknown",
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception as metrics_exc:
                # Non-fatal: failure-path stage metrics are best-effort telemetry; the
                # original error is still recorded and surfaced below (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish SchemaExtract (JDBC failed) stage metrics for "
                    f"pipeline_id={original_pipeline_id}: {type(metrics_exc).__name__}: {metrics_exc}"
                )

        publish_pipeline_state_change(
            pipeline_id=original_pipeline_id or "unknown",
            execution_id=execution_arn or "unknown",
            stage="agentic_data_ingestion_schema_extract",
            status="failed",
            run_id=run_id or "unknown",
            error_details=str(exc),
        )

        # P28 R1.1: anything that escapes here is converted to a permanent
        # StepFailure by the step_handler decorator on lambda_handler, so the
        # Step Functions task fails and Retry/Catch fire. A crawler still
        # RUNNING at the polling deadline is genuinely retryable — a retried
        # task polls a crawl that has likely finished by then — so surface
        # TimeoutError as TransientStepFailure for the SFN Retry policy.
        # ValueError / RuntimeError (Task 7.3 guards, crawler FAILED /
        # CANCELLED) stay permanent.
        if isinstance(exc, TimeoutError):
            raise TransientStepFailure(
                "schema_extract",
                str(exc),
                details={"run_id": run_id, "pipeline_id": pipeline_id, "crawler_name": crawler_name},
            ) from exc
        raise


@step_handler("schema_extract")
def lambda_handler(event, context):
    """
    Lambda function to extract schema from source files.

    Routes to format-specific streaming extractors when source_format is provided:
    - csv → S3 Select (samples N rows without full scan)
    - json → Range GET (first 64KB + partial parse)
    - parquet → PyArrow metadata-only read (zero data scanning)
    - avro → bounded Range GET of the OCF header, parsed in-repo by
      extract_schema_from_avro (the Writer_Schema sits in the file's FIRST bytes;
      fastavro does not fit the Layer_Budget, so the header parser is hand-written)
    - orc → PyArrow footer read over a seekable pyarrow.fs.S3FileSystem handle,
      extract_schema_from_orc (the ORC footer is at the END of the file, so a
      forward-only ranged GET cannot reach it)
    - excel → openpyxl over the whole object, extract_schema_from_excel, PLUS a
      conversion to CSV (convert_source_to_csv) because Spark and Glue have no
      Excel datasource at all. This is the one Canonical_Format that is not in
      the Native_Format_Set, and this branch is the ONLY place the conversion
      gate is read (p55 R13.3, design §4.2/§4.3)
    - jdbc → per-pipeline runtime crawler (handle_jdbc_schema_extract)

    The Avro, ORC and Excel extractors RAISE on an unreadable file and never return
    {"type": "object", "properties": {}}. Both formats previously fell through to
    the JSON reader, which found no complete records in a binary container and
    returned that empty schema — so the run reported success with zero fields.
    Removing that is what p46 was for; do not restore a fallback for either.

    Falls back to the existing read_json_from_s3() + extract_json_schema() flow
    when source_format is not provided (backward compatibility).

    Args:
        event: Step Functions event containing run_id, execution_arn, and optionally source_format
        context: Lambda context object

    Returns:
        dict: Schema extraction results including schema_hash, extracted_schema,
            json_s3_uri, and the p55 pair ``readable_source_uri`` /
            ``readable_source_format`` — the object the Glue job should read and
            the Canonical_Format to read it as, present on BOTH arms
            (`LGR-5c539a6-2`).
    """
    # JDBC-sourced pipelines: schema is produced by the per-pipeline runtime
    # crawler (Task 7.2). Dispatched before any S3-format-specific routing
    # because the inbound event has no S3 file to read.
    if event.get("source_format") == "jdbc":
        return handle_jdbc_schema_extract(event, context)

    try:
        import time as _time

        _metrics_start = _time.time()

        logger.info(f"Schema extraction event: {json.dumps(event)}")

        # Get run_id and execution info from event
        run_id = event.get("run_id")
        execution_arn = event.get("execution_arn")
        source_format = event.get("source_format")  # NEW: from file_validation via Step Functions
        if not run_id:
            raise ValueError("Run ID not provided in event")

        logger.append_keys(run_id=run_id, pipeline_id=event.get("original_pipeline_id", ""), step_name="SchemaExtract")

        # Get previous state to find file location
        previous_state = get_previous_state(run_id)
        if not previous_state:
            raise ValueError(f"No previous state found for run_id: {run_id}")

        original_pipeline_id = previous_state["original_pipeline_id"]

        # Determine which S3 URI to use and which extraction method
        # source_s3_uri is the original file (for streaming extraction)
        # json_s3_uri is the converted JSON (for legacy extraction)
        source_s3_uri = previous_state.get("original_s3_uri") or previous_state.get("source_s3_uri")
        json_s3_uri = previous_state.get("json_s3_uri")

        # ── Prefix mode: when source_s3_uri ends with '/', find the first file ──
        # The canonical_uri stays as the prefix (for downstream Glue job to read all files),
        # but schema extraction uses the first file as a representative sample.
        if source_s3_uri and source_s3_uri.endswith("/"):
            logger.info(f"Prefix mode detected: {source_s3_uri}")
            s3_parts = source_s3_uri.replace("s3://", "").split("/", 1)
            prefix_bucket = s3_parts[0]
            prefix_key = s3_parts[1]
            resp = s3_client.list_objects_v2(Bucket=prefix_bucket, Prefix=prefix_key, MaxKeys=10)
            first_file_key = None
            for obj in resp.get("Contents", []):
                if not obj["Key"].endswith("/"):
                    first_file_key = obj["Key"]
                    break
            if not first_file_key:
                raise ValueError(f"No files found in prefix: {source_s3_uri}")
            logger.info(f"Prefix mode: using first file '{first_file_key}' for schema extraction")
            # Override source_s3_uri for extraction, but keep prefix as canonical
            extraction_s3_uri = f"s3://{prefix_bucket}/{first_file_key}"
            canonical_prefix_uri = source_s3_uri
        else:
            extraction_s3_uri = source_s3_uri
            canonical_prefix_uri = None

        # ── p55 R13.3/R13.4: the Conversion_Required_Set resolution ──
        # TWO outputs, not one — the readable URI and the readable FORMAT. Both
        # stay empty for every Native_Format_Set member and are filled in only
        # by a branch that actually converts, so the defaults below resolve to
        # "the original source object, read as itself" (R13.4) by construction
        # rather than by a conditional. Task 4.1's finding 2 is why the format
        # travels with the URI: the four templates branch
        # `{% if source_format == 'csv' %} … {% else %} spark.read…json(…)`, so
        # an Excel run whose URI was repointed at a converted CSV while its
        # format still said "excel" would render the `{% else %}` and read that
        # CSV as JSON — a half fix that fails in a new place.
        converted_source_uri = ""
        converted_source_format = ""
        # p55 R8.2 — the Worksheet this run actually processed. Empty for every
        # format that has no worksheet concept, so the key is ALWAYS present and
        # never conditional: design's own architectural rule for this spec is
        # that a new control decision surfaces either as a value present on both
        # arms of an existing result or as a raise, and never as a
        # conditionally-present key. That is `LGR-5c539a6-2`'s construction
        # applied to a second value, not a second reading of it — an absent key
        # is what raises the uncatchable States.ParameterPathFailure, and an
        # empty string cannot.
        processed_worksheet = ""

        if source_format and source_s3_uri:
            # ── Streaming extraction path (Phase 9) ──
            extract_uri = extraction_s3_uri if extraction_s3_uri else source_s3_uri
            logger.info(f"Streaming extraction: format={source_format}, source={extract_uri}")
            s3_parts = extract_uri.replace("s3://", "").split("/", 1)
            bucket = s3_parts[0]
            key = s3_parts[1]

            if source_format == "csv":
                schema = extract_schema_from_csv(bucket, key)
            elif source_format == "json":
                schema = extract_schema_from_json(bucket, key)
            elif source_format == "parquet":
                try:
                    schema = extract_schema_from_parquet(bucket, key)
                except ImportError:
                    logger.warning("PyArrow not available, falling back to legacy extraction")
                    if json_s3_uri:
                        json_data = read_json_from_s3(*json_s3_uri.replace("s3://", "").split("/", 1))
                        schema = extract_json_schema(json_data)
                    else:
                        raise ValueError("PyArrow not available and no json_s3_uri for fallback")
            elif source_format == "avro":
                schema = extract_schema_from_avro(bucket, key)
            elif source_format == "orc":
                schema = extract_schema_from_orc(bucket, key)
            elif source_format == "excel":
                # p55 R6.2 + R13.3, and the ONLY place the conversion gate is
                # read. Ahead of the terminal `else`, which stays (F5;
                # `LGR-60c4985-7` records that deleting it reproduces an
                # `UnboundLocalError` on `schema`).
                #
                # ONE download and ONE parse feed both units: the extractor
                # describes the Worksheet's columns and the converter writes the
                # object the Glue job will actually read. Design §4.2 (W3-D1)
                # sites the conversion here precisely because this step already
                # has the workbook open — every other placement opens it twice.
                #
                # Both RAISE rather than degrading (R6.5). No `threading.Thread`
                # and no post-response work: the conversion completes before
                # this handler returns, because the Lambda environment freezes
                # on return and would kill a thread (`LGR-b46a5bb-1`, standing
                # invariant).
                #
                # p55 R8.1–R8.5. The Selected_Worksheet is pipeline
                # configuration (design §4.4, decision W3-D5 arm (a)), so it is
                # read HERE — the one place in this Lambda that has a pipeline id
                # and needs a worksheet — and passed down. Reading it per run
                # rather than per upload is what gives R8.3 for free: "the same
                # Selected_Worksheet on every subsequent run" is the default once
                # the selection lives on the pipeline instead of being decided
                # inside a run.
                #
                # ``refuse_ambiguous_multi_sheet=True`` is passed only here,
                # because only here does ``None`` mean *no human has chosen*
                # rather than *this caller did not say*. See
                # :func:`_read_excel_table`.
                _selected_worksheet = _configured_worksheet(get_pipeline_metadata(original_pipeline_id))
                _excel_table = _read_excel_table(
                    bucket,
                    key,
                    _selected_worksheet,
                    refuse_ambiguous_multi_sheet=True,
                )
                # R8.2 — which Worksheet was processed. Taken from the parsed
                # table rather than from ``_selected_worksheet``, because those
                # differ on exactly the case the requirement is about: with no
                # selection and one sheet, the configured value is ``None`` and
                # the processed sheet has a name. Recording the request instead
                # of the outcome would leave the single-sheet run with nothing
                # recorded at all.
                processed_worksheet = _excel_table[0]
                schema = extract_schema_from_excel(bucket, key, table=_excel_table)
                converted_source_uri = convert_source_to_csv(
                    bucket,
                    key,
                    run_id=run_id,
                    pipeline_id=original_pipeline_id,
                    table=_excel_table,
                )
                converted_source_format = _EXCEL_READABLE_FORMAT
            else:
                # Defensive, not a live path (R3.1, R3.2; design §4.3). Every
                # Canonical_Format that file_validation can emit now has its own
                # branch above, so the only value left for this branch is
                # "unknown" — and file_validation cannot hand that value to this
                # step. An extension outside FORMAT_MAP writes its `unsupported`
                # audit row and raises ValueError there, which that handler's own
                # `except Exception` wraps into a permanent StepFailure
                # attributed to file_validation, so the execution never reaches
                # schema_extract (LGR-60c4985-7, template/ratified — C2). A key
                # that gets this far was therefore already accepted with a known
                # extension.
                #
                # Kept rather than deleted so a dispatch that has drifted from
                # FORMAT_MAP is visible in the log instead of raising an
                # unrelated error. It no longer serves any binary container
                # format: those found no complete records through this reader,
                # got back an empty schema, and reported success with zero
                # fields — the defect p46 removes. A contract test asserts every
                # FORMAT_MAP value has a branch above (design Property 3), which
                # is the check whose absence let two claimed formats land here
                # unnoticed.
                logger.warning(
                    f"Unhandled source_format '{source_format}' reached the extraction dispatch — "
                    f"file_validation should have failed this key before schema_extract. "
                    f"Attempting the JSON reader."
                )
                schema = extract_schema_from_json(bucket, key)

            # Use prefix URI as canonical for downstream (Glue reads all files),
            # or the source file URI for single-file mode
            canonical_uri = canonical_prefix_uri if canonical_prefix_uri else source_s3_uri
        elif json_s3_uri:
            # ── Legacy extraction path (backward compatibility, Task 2.7) ──
            logger.info(f"Legacy extraction from JSON: {json_s3_uri}")
            s3_parts = json_s3_uri.replace("s3://", "").split("/", 1)
            bucket = s3_parts[0]
            key = s3_parts[1]

            json_data = read_json_from_s3(bucket, key)
            schema = extract_json_schema(json_data)
            canonical_uri = json_s3_uri
        else:
            raise ValueError(
                f"No source file found for run_id: {run_id}. Neither source_s3_uri nor json_s3_uri available."
            )

        # p55 R13.3/R13.4 — the resolved pair, on every path including the
        # legacy one. For a Native_Format_Set member (and for the legacy JSON
        # path) this is the original object read as itself; only a converting
        # branch above has filled the two locals in. Resolved HERE, in Python,
        # for `LGR-5c539a6-2`'s reason: ASL has no coalesce and no conditional
        # intrinsic, so a value that is sometimes absent cannot be read safely
        # from a payload mapping at all.
        readable_source_uri = converted_source_uri or canonical_uri
        readable_source_format = converted_source_format or source_format or "json"

        logger.info(f"Extracted schema with {len(schema.get('properties', {}))} fields")

        # Store schema in DynamoDB
        schema_hash = store_schema_in_dynamodb(
            run_id,
            original_pipeline_id,
            schema,
            canonical_uri,
            execution_arn,
            readable_source_uri=readable_source_uri,
            readable_source_format=readable_source_format,
            processed_worksheet=processed_worksheet,
        )

        # Publish pipeline state change event
        metrics = {
            "duration_ms": int((_time.time() - _metrics_start) * 1000),
            "schema_fields_extracted": len(schema.get("properties", {})),
            "extraction_method": source_format if source_format else "legacy_json",
        }

        publish_pipeline_state_change(
            pipeline_id=original_pipeline_id,
            execution_id=execution_arn or "unknown",
            stage="agentic_data_ingestion_schema_extract",
            status="completed",
            run_id=run_id,
            metrics=metrics,
        )

        # Publish stage metrics
        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="SchemaExtract",
                    pipeline_id=original_pipeline_id or "unknown",
                    status="completed",
                    duration_ms=_duration_ms,
                )
            except Exception as exc:
                # Non-fatal: stage metrics are best-effort telemetry; a publish failure
                # must not fail a successful schema extraction (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish SchemaExtract (completed) stage metrics for "
                    f"pipeline_id={original_pipeline_id} run_id={run_id}: {type(exc).__name__}: {exc}"
                )

        # ``source_sample_uri`` mirrors the key the JDBC arm returns so the
        # TestCode payload can read ONE path on every run (p53 task 3.3,
        # ``orchestration-stack.ts:594``). On this path there is no separate
        # Sample_Artifact — the uploaded source objects are themselves what a
        # Sample_Reader reads — so the resolved value is ``canonical_uri``, the
        # same value this arm already returns as ``json_s3_uri``. The S3-upload
        # path's ``source_data_uri`` therefore does not change value; only the
        # path it is read from does, which is why the source-files grant on
        # ``verificationSessionRole`` (``foundation-stack.ts:835-887``) still
        # covers it.
        #
        # ALWAYS present, never absent — that is the whole point of resolving
        # the preference here rather than in the payload mapping. ASL has no
        # coalesce or conditional intrinsic, and Step Functions raises
        # States.ParameterPathFailure on a ``$.schema_extract_result.…``
        # reference whose path is missing, a failure neither Retry nor Catch can
        # intercept (``orchestration-stack.ts:338-340``). RESUME_MARKER_PATH
        # (``orchestration-stack.ts:332-350``) is the in-repo precedent: a value
        # echoed into a step's own result "defaulting to '', never absent" and
        # therefore "guaranteed-present by construction".
        # ``readable_source_uri`` / ``readable_source_format`` (p55 R13.3, R13.4)
        # are returned on BOTH arms, for the same reason and under the same
        # invariant as ``source_sample_uri``: `LGR-5c539a6-2` requires every key
        # an ASL path could read off ``$.schema_extract_result`` to exist on
        # both arms, and the way that is made true is to resolve the value in
        # Python and echo it unconditionally. For every Native_Format_Set member
        # the URI IS ``canonical_uri`` and the format IS ``source_format``, so
        # the pair is guaranteed-present by construction and never a conditional.
        #
        # **No ASL path dereferences either key, deliberately** (see
        # ``orchestration-stack.ts:358-362``, which is not changed by p55). The
        # three consumers on the Glue-read path read the pair off this step's
        # DynamoDB row in Python instead. Task 4.4 measured why: 19 retryable
        # runs in one account replay a pre-p55 cached ``sfn_result`` that carries
        # neither key, **17 of them sitting at ``test_code``** — the
        # ``waitForTaskToken`` step `LGR-60c4985-3` excludes from resumption, so
        # it genuinely re-executes and re-reads the replayed envelope. A new
        # ``Payload`` read of a missing path there is a
        # ``States.ParameterPathFailure`` that neither Retry nor Catch can
        # intercept. A row read is a ``.get()`` that falls back. Echoing the pair
        # now, while nothing reads it, is what keeps a future ASL reader from
        # needing a second deploy boundary of exactly that shape.
        #
        # ``source_sample_uri`` now resolves to ``readable_source_uri`` rather
        # than to ``canonical_uri``. For every native format those are the same
        # bytes, so nothing changes; for a converted source it points TestCode's
        # sample read at the object the Glue job will read, which is the point of
        # verifying at all.
        return {
            "statusCode": 200,
            "run_id": run_id,
            "original_pipeline_id": original_pipeline_id,
            "schema_hash": schema_hash,
            "extracted_schema": schema,
            "json_s3_uri": canonical_uri,
            "source_sample_uri": readable_source_uri,
            "readable_source_uri": readable_source_uri,
            "readable_source_format": readable_source_format,
            # p55 R8.2 — the Worksheet processed, or ``""`` where the source has
            # none. Always present, on both arms, for the same construction
            # reason as the pair above.
            "processed_worksheet": processed_worksheet,
            "source_format": source_format or "json",
            "message": "Schema extraction completed successfully",
        }

    except Exception as e:
        logger.error(f"Error in schema extraction: {e!s}")

        if PipelineMetrics is not None:
            try:
                _duration_ms = (_time.time() - _metrics_start) * 1000
                PipelineMetrics.record_stage_execution(
                    stage_name="SchemaExtract",
                    pipeline_id=event.get("original_pipeline_id", "unknown"),
                    status="failed",
                    duration_ms=_duration_ms,
                )
            except Exception as metrics_exc:
                # Non-fatal: failure-path stage metrics are best-effort telemetry; the
                # original error is still recorded below (P21 Req 6.2/6.5).
                logger.warning(
                    f"Failed to publish SchemaExtract (failed) stage metrics for "
                    f"pipeline_id={event.get('original_pipeline_id', 'unknown')} "
                    f"run_id={event.get('run_id', 'unknown')}: {type(metrics_exc).__name__}: {metrics_exc}"
                )

        # P28 R8.5 (task 14.2): reliably write the failed state record with
        # the table's full key. The old item lacked the run_id partition key,
        # so the write was a 100% ValidationException silently swallowed by
        # the except — the failure record never landed. The record now
        # mirrors the sibling step Lambdas' failure rows: (run_id, canonical
        # step_name), status "failed", step_sequence from the shared table.
        try:
            _failed_pipeline_id = (
                original_pipeline_id
                if "original_pipeline_id" in locals()
                else event.get("original_pipeline_id", "unknown")
            )
            error_item = {
                "run_id": event.get("run_id", "unknown"),  # Partition key
                "step_name": StepName.SCHEMA_EXTRACT,
                "status": "failed",
                "step_sequence": STEP_SEQUENCE[StepName.SCHEMA_EXTRACT],
                "timestamp": datetime.now(UTC).isoformat(),
                "pipeline_id": _failed_pipeline_id,  # For PipelineIndex GSI
                "original_pipeline_id": _failed_pipeline_id,
                "error_message": str(e),
            }
            if "execution_arn" in locals():
                error_item["execution_id"] = execution_arn
            table.put_item(Item=error_item)
        except Exception as write_err:
            # Never mask the original failure with a state-write error.
            logger.warning(f"Failed to write error state to DynamoDB: {write_err}")

        # Publish failure event
        run_id = event.get("run_id", "unknown")
        execution_arn = event.get("execution_arn", "unknown")

        publish_pipeline_state_change(
            pipeline_id=event.get("original_pipeline_id", "unknown"),
            execution_id=execution_arn,
            stage="agentic_data_ingestion_schema_extract",
            status="failed",
            run_id=run_id,
            error_details=str(e),
        )

        # P28 R1.1: raise so the Step Functions task fails and Retry/Catch fire
        # (an error payload return would be treated as task success).
        raise StepFailure(
            "schema_extract",
            f"Schema extraction failed: {e!s}",
            details={"run_id": run_id, "pipeline_id": event.get("original_pipeline_id", "unknown")},
        ) from e
