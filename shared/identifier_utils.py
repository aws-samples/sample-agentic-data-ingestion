# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Identifier_Allowlist validator — injection hardening for SQL/DQDL sinks.

P28 R12.1: a single shared validator applied to every user-influenced
identifier (field names, merge keys, table/database names) before it is
interpolated into DQDL rule strings, the ``spark.sql`` MERGE statement, or
Athena SQL. A hostile CSV header must never become code.

This module only DEFINES the validator (P28 task 2.2); application at the
three sinks (DQDL assembly, merge_key MERGE SQL, data_quality_agent Athena
query builder) happens in task 11.1. ``parse_identifier_list`` reads a
configured list of names (``target_config.partition_columns``) through the
same validator.

Contract (R12.2): validation failure raises
:class:`IdentifierValidationError` naming the offending identifier and the
sink context. There is no sanitize-and-continue — the input is never
modified, only accepted verbatim or rejected.

Allowlist: ``^[A-Za-z_][A-Za-z0-9_]*$`` (ASCII-only by construction — the
explicit character ranges do not match Unicode homoglyphs), maximum length
255 (the common ceiling for Glue/Athena/Hive identifiers).

Note for callers: qualified names (``db.table``) do NOT pass the allowlist —
split on ``.`` and validate each part separately.

S3 bucket names (security finding S1) are a second sink with the same contract.
An operator-supplied S3 location's bucket is interpolated into a per-pipeline
Glue role's IAM ``Resource`` (``arn:aws:s3:::{bucket}/*``), where ``*`` and
``?`` are wildcards: ``s3://*/`` used to grant every object in the account.
:func:`validate_s3_bucket_name` and :func:`bucket_from_s3_uri` accept a name
verbatim or raise :class:`S3BucketNameError`; nothing is stripped or lowered.
The dashboard API refuses such a value at create and edit, and the role
builder refuses it again for records written before that check existed.
:func:`validate_s3_table_bucket_name` applies the same contract to an S3 table
bucket name, which the role builder's S3 Tables branch takes from the
create_infrastructure step's output and from an earlier run's stored output.
"""

import re
from collections.abc import Iterable
from typing import Final

#: The Identifier_Allowlist regex (requirements glossary). Matched with
#: ``fullmatch`` so a trailing newline can never sneak past the ``$`` anchor.
IDENTIFIER_ALLOWLIST: Final[re.Pattern[str]] = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Maximum identifier length (design R12).
MAX_IDENTIFIER_LENGTH: Final[int] = 255

# Cap on how much of a hostile identifier is echoed into the error message.
# The full value stays available on the exception's ``name`` attribute; the
# message stays log-friendly even for megabyte-sized garbage.
_DISPLAY_LIMIT: Final[int] = 300


def _display(name: object) -> str:
    """Log-safe representation of the offending value for error messages.

    ``repr`` makes control characters (newlines, null bytes, tabs) visible
    instead of letting them mangle log lines; oversized values are truncated.
    """
    if isinstance(name, str):
        shown = repr(name) if len(name) <= _DISPLAY_LIMIT else f"{name[:_DISPLAY_LIMIT]!r}… ({len(name)} chars)"
        return shown
    return f"{name!r} (type {type(name).__name__})"


class IdentifierValidationError(Exception):
    """A user-influenced identifier failed the Identifier_Allowlist.

    Carries the offending ``name`` (verbatim, unmodified) and the sink
    ``context`` it was destined for, and puts both in the message so the
    failure is diagnosable from logs alone (R12.2).
    """

    def __init__(self, name: object, context: str, reason: str | None = None) -> None:
        self.name = name
        self.context = context
        if reason is None:
            reason = f"does not match the identifier allowlist {IDENTIFIER_ALLOWLIST.pattern}"
        super().__init__(f"Invalid identifier {_display(name)} in {context}: {reason}")


def validate_identifier(name: str, context: str) -> str:
    """Validate one identifier against the Identifier_Allowlist.

    Args:
        name: The identifier as received (CSV header, merge key, table
            name, …). Never modified.
        context: Human-readable sink description for the error message,
            e.g. ``"DQDL rule field"`` or ``"MERGE statement merge_key"``.

    Returns:
        ``name`` unchanged, so the call can be used inline:
        ``sql = f"SELECT {validate_identifier(col, 'Athena column')} …"``.

    Raises:
        IdentifierValidationError: If ``name`` is not a string, is empty,
            exceeds :data:`MAX_IDENTIFIER_LENGTH`, or fails the allowlist
            regex. The message names the identifier and the context.
    """
    # Identifiers arrive from external, user-influenced payloads (CSV
    # headers, JSON bodies), so the declared ``str`` type is enforced at
    # runtime rather than assumed.
    if not isinstance(name, str):
        raise IdentifierValidationError(name, context, reason=f"expected a string, got {type(name).__name__}")
    if not name:
        raise IdentifierValidationError(name, context, reason="identifier is empty")
    if len(name) > MAX_IDENTIFIER_LENGTH:
        raise IdentifierValidationError(
            name, context, reason=f"length {len(name)} exceeds maximum {MAX_IDENTIFIER_LENGTH}"
        )
    if not IDENTIFIER_ALLOWLIST.fullmatch(name):
        raise IdentifierValidationError(name, context)
    return name


def parse_identifier_list(value: object, context: str) -> list[str]:
    """Read a configured list of column names, refusing any that is not an identifier.

    For configuration fields that hold several identifiers and have been stored
    in two shapes, such as ``target_config.partition_columns``. ``None`` and a
    blank string mean "none" and return ``[]``. A string is a comma-separated
    list (``"region, dt"``): it is split on commas and the whitespace around
    each entry is removed, which reads the list format rather than altering an
    identifier. A list or tuple is taken entry by entry, verbatim. Every entry
    then passes :func:`validate_identifier` unchanged.

    An empty entry (``"a,,b"``, ``"a,"``) is refused rather than dropped, so a
    typo is reported instead of being reinterpreted as a shorter list.

    Raises:
        IdentifierValidationError: If ``value`` has another type, or any entry
            fails :func:`validate_identifier`. The message names the entry and
            ``context``.
    """
    if value is None:
        return []
    if isinstance(value, str):
        if not value.strip():
            return []
        entries: list[object] = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        entries = list(value)
    else:
        raise IdentifierValidationError(
            value,
            context,
            reason=f"expected a list of names or a comma-separated string, got {type(value).__name__}",
        )
    # `validate_identifier` checks the type at run time and refuses a non-string
    # entry, which is what a list from a JSON body can hold.
    return [validate_identifier(entry, context) for entry in entries]  # type: ignore[arg-type]


def validate_identifiers(names: Iterable[str], context: str) -> list[str]:
    """Validate a batch of identifiers, failing on the first offender.

    Args:
        names: Identifiers to validate, in order.
        context: Sink description shared by all of them.

    Returns:
        The identifiers as a list, all unchanged.

    Raises:
        IdentifierValidationError: On the first invalid entry, naming it.
    """
    return [validate_identifier(name, context) for name in names]


# ── S3 bucket names (security finding S1) ─────────────────────────────

#: Shortest and longest general purpose bucket name S3 accepts.
S3_BUCKET_NAME_MIN_LENGTH: Final[int] = 3
S3_BUCKET_NAME_MAX_LENGTH: Final[int] = 63

#: The characters a bucket name may hold. Explicit ASCII ranges, matched with
#: ``fullmatch``, so neither a Unicode letter nor a trailing newline passes.
_S3_BUCKET_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[a-z0-9.-]+")

#: The characters an S3 table bucket name may hold: the general set without the
#: dot, which S3 Tables refuses.
_S3_TABLE_BUCKET_CHARACTERS: Final[re.Pattern[str]] = re.compile(r"[a-z0-9-]+")

#: A name in dotted-quad IPv4 form, which S3 refuses as a bucket name.
_IPV4_SHAPE: Final[re.Pattern[str]] = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}")

_S3_BUCKET_EDGE_CHARACTERS: Final[frozenset[str]] = frozenset("abcdefghijklmnopqrstuvwxyz0123456789")

_S3_URI_SCHEME: Final[str] = "s3://"


class S3BucketNameError(Exception):
    """An S3 bucket name, or the bucket of an ``s3://`` URI, is not a valid bucket name.

    The sibling of :class:`IdentifierValidationError`, and deliberately not a
    ``ValueError`` for the same reason: an ``except ValueError`` written for some
    other failure must not swallow it. Carries the offending ``value`` verbatim
    and the ``context`` it came from, and names both in the message.
    """

    def __init__(self, value: object, context: str, reason: str) -> None:
        self.value = value
        self.context = context
        self.reason = reason
        super().__init__(f"Invalid S3 bucket in {context}: {_display(value)} {reason}")


def _s3_bucket_name_problem(name: str, *, table_bucket: bool = False) -> str | None:
    """Why ``name`` is not a valid S3 bucket name, or ``None`` if it is one.

    ``table_bucket`` selects the S3 table bucket character set, which has no
    dot. Every other rule here is the same for both kinds, and the dot rules
    below cannot fire for a table bucket name.
    """
    if not name:
        return "is empty"
    if table_bucket:
        if not _S3_TABLE_BUCKET_CHARACTERS.fullmatch(name):
            return "may contain only lowercase letters, digits and hyphens"
    elif not _S3_BUCKET_CHARACTERS.fullmatch(name):
        return "may contain only lowercase letters, digits, dots and hyphens"
    if not S3_BUCKET_NAME_MIN_LENGTH <= len(name) <= S3_BUCKET_NAME_MAX_LENGTH:
        return (
            f"is {len(name)} characters; a bucket name is "
            f"{S3_BUCKET_NAME_MIN_LENGTH}-{S3_BUCKET_NAME_MAX_LENGTH} characters"
        )
    if name[0] not in _S3_BUCKET_EDGE_CHARACTERS or name[-1] not in _S3_BUCKET_EDGE_CHARACTERS:
        return "must begin and end with a lowercase letter or digit"
    if ".." in name:
        return "must not contain two adjacent dots"
    if _IPV4_SHAPE.fullmatch(name):
        return "must not be formatted as an IPv4 address"
    return None


def validate_s3_bucket_name(name: str, context: str) -> str:
    """Validate a general purpose S3 bucket name against S3's naming rules.

    The rules: 3-63 characters, only lowercase ASCII letters, digits, dots and
    hyphens, beginning and ending with a letter or digit, no two adjacent dots,
    and not in IPv4 dotted-quad form. So ``*``, ``?``, uppercase, whitespace and
    the empty string are all refused. S3's reserved prefixes and suffixes
    (``xn--``, ``-s3alias`` and the like) are not checked: they cannot widen an
    IAM grant, and S3 itself refuses them at bucket creation.

    Returns:
        ``name`` unchanged.

    Raises:
        S3BucketNameError: If ``name`` is not a string or breaks a rule. The
            message names the value and ``context``.
    """
    # The value comes from a JSON body or a stored record, so the declared type
    # is enforced at run time rather than assumed.
    if not isinstance(name, str):
        raise S3BucketNameError(name, context, f"is not a bucket name: expected a string, got {type(name).__name__}")
    problem = _s3_bucket_name_problem(name)
    if problem:
        raise S3BucketNameError(name, context, problem)
    return name


def validate_s3_table_bucket_name(name: str, context: str) -> str:
    """Validate an S3 table bucket name against S3 Tables' naming rules.

    The rules: 3-63 characters, only lowercase ASCII letters, digits and
    hyphens, beginning and ending with a letter or digit. They are the general
    purpose rules without the dot, so ``my.bucket`` is refused here although
    :func:`validate_s3_bucket_name` accepts it. As there, the reserved prefixes
    and suffixes (``xn--``, ``aws``, ``--table-s3`` and the rest) are not
    checked: they cannot widen an IAM grant, and S3 Tables refuses them at
    ``CreateTableBucket``.

    Returns:
        ``name`` unchanged.

    Raises:
        S3BucketNameError: If ``name`` is not a string or breaks a rule. The
            message names the value and ``context``.
    """
    if not isinstance(name, str):
        raise S3BucketNameError(name, context, f"is not a bucket name: expected a string, got {type(name).__name__}")
    problem = _s3_bucket_name_problem(name, table_bucket=True)
    if problem:
        raise S3BucketNameError(name, context, problem)
    return name


def bucket_from_s3_uri(uri: str, context: str) -> str:
    """The bucket of an ``s3://bucket/prefix`` URI, validated by :func:`validate_s3_bucket_name`.

    The scheme must be exactly ``s3://``. The bucket is everything between the
    scheme and the first ``/`` (or the end); the prefix is not examined, because
    no caller interpolates it. Nothing is trimmed or lowered: ``" s3://b/"`` and
    ``"S3://bucket/"`` are refused rather than repaired.

    Returns:
        The bucket name.

    Raises:
        S3BucketNameError: If ``uri`` is not a string, does not start with
            ``s3://``, or its bucket breaks a naming rule. The message names the
            whole URI and ``context``.
    """
    if not isinstance(uri, str):
        raise S3BucketNameError(uri, context, f"is not an s3:// URI: expected a string, got {type(uri).__name__}")
    if not uri.startswith(_S3_URI_SCHEME):
        raise S3BucketNameError(uri, context, "is not an s3://bucket/prefix URI")
    bucket = uri[len(_S3_URI_SCHEME) :].split("/", 1)[0]
    problem = _s3_bucket_name_problem(bucket)
    if problem:
        raise S3BucketNameError(uri, context, f"names bucket {_display(bucket)}, which {problem}")
    return bucket
