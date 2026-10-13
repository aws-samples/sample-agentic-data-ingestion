# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for shared/identifier_utils.py (P28 R12 — Identifier_Allowlist).

Validates: Requirements 12.1 (allowlist enforcement `^[A-Za-z_][A-Za-z0-9_]*$`,
max length 255) and 12.2 (failure names the offending identifier; no
sanitize-and-continue — the input is never modified).

The hostile-input catalog below is the per-sink reuse set for task 11.1
(DQDL assembly, merge_key MERGE SQL, Athena query builder): quotes,
semicolons, newlines, comment tokens, and friends per requirement 12.4.

The S3 bucket-name validator (security finding S1) is covered at the end.
"""

import os
import sys

import pytest

# Add shared directory to path (same convention as test_step_names.py)
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

from identifier_utils import (
    IDENTIFIER_ALLOWLIST,
    MAX_IDENTIFIER_LENGTH,
    IdentifierValidationError,
    S3BucketNameError,
    bucket_from_s3_uri,
    parse_identifier_list,
    validate_identifier,
    validate_identifiers,
    validate_s3_bucket_name,
    validate_s3_table_bucket_name,
)

VALID_IDENTIFIERS = [
    pytest.param("user_id", id="simple-snake-case"),
    pytest.param("email", id="simple-word"),
    pytest.param("a_b_c", id="multiple-underscores"),
    pytest.param("_private", id="leading-underscore"),
    pytest.param("_", id="single-underscore"),
    pytest.param("a", id="single-letter"),
    pytest.param("Z", id="single-uppercase"),
    pytest.param("col2", id="digit-after-first-char"),
    pytest.param("a1b2c3", id="interleaved-digits"),
    pytest.param("CamelCase", id="mixed-case"),
    pytest.param("SCREAMING_SNAKE", id="uppercase-snake"),
    pytest.param("a" * 255, id="exactly-255-chars"),
]

# Each entry must raise, and the error message must name the identifier.
HOSTILE_IDENTIFIERS = [
    # Quotes — string-literal escape attempts
    pytest.param("col'name", id="single-quote"),
    pytest.param('col"name', id="double-quote"),
    pytest.param("col`name", id="backtick"),
    pytest.param("'--", id="quote-then-comment"),
    # Statement separators
    pytest.param("col;name", id="semicolon"),
    pytest.param(";", id="bare-semicolon"),
    # Line breaks — including the trailing-newline regex pitfall ($ matches
    # before a final \n under re.match; fullmatch must reject it)
    pytest.param("col\nname", id="embedded-newline"),
    pytest.param("abc\n", id="trailing-newline"),
    pytest.param("col\rname", id="carriage-return"),
    pytest.param("abc\r\n", id="trailing-crlf"),
    # SQL comment tokens
    pytest.param("col--comment", id="line-comment-dashes"),
    pytest.param("--", id="bare-dash-comment"),
    pytest.param("col/*hidden", id="block-comment-open"),
    pytest.param("col*/", id="block-comment-close"),
    pytest.param("col#comment", id="hash-comment"),
    # Whitespace
    pytest.param("col name", id="embedded-space"),
    pytest.param(" col", id="leading-space"),
    pytest.param("col ", id="trailing-space"),
    pytest.param("col\tname", id="tab"),
    # Structure characters
    pytest.param("count(x)", id="parens-function-call"),
    pytest.param("(", id="bare-open-paren"),
    # Qualified names must be validated per-part by callers
    pytest.param("db.table", id="dotted-qualified-name"),
    pytest.param("a.b.c", id="double-dotted-name"),
    # Dashes
    pytest.param("col-name", id="dash"),
    # Unicode homoglyphs / non-ASCII — 'а' below is CYRILLIC SMALL A
    pytest.param("c\u0430t", id="cyrillic-homoglyph-a"),
    pytest.param("caf\u00e9", id="accented-latin"),
    pytest.param("\uff43ol", id="fullwidth-latin-c"),
    pytest.param("\u540d\u524d", id="cjk"),
    # Shape violations
    pytest.param("", id="empty-string"),
    pytest.param("a" * 256, id="256-chars-over-limit"),
    pytest.param("a" * 1000, id="1000-chars-way-over"),
    pytest.param("1col", id="leading-digit"),
    pytest.param("9", id="bare-digit"),
    # Full injection payloads
    pytest.param("x; DROP TABLE users", id="classic-drop-table"),
    pytest.param("Robert'); DROP TABLE students;--", id="bobby-tables"),
    pytest.param("a OR 1=1", id="tautology-or"),
    pytest.param("UNION SELECT password FROM users", id="union-select"),
    # Control characters
    pytest.param("col\x00name", id="null-byte"),
    pytest.param("\x00", id="bare-null-byte"),
    pytest.param("col\x1bname", id="escape-char"),
]


class TestValidIdentifiers:
    """Allowlisted shapes pass through verbatim."""

    @pytest.mark.parametrize("name", VALID_IDENTIFIERS)
    def test_valid_identifier_returned_unchanged(self, name):
        """A valid identifier is returned exactly as given (same object)."""
        result = validate_identifier(name, "unit test sink")
        assert result is name

    def test_boundary_length_255_accepted_256_rejected(self):
        """Length 255 is the inclusive maximum."""
        assert validate_identifier("a" * MAX_IDENTIFIER_LENGTH, "sink") == "a" * 255
        with pytest.raises(IdentifierValidationError):
            validate_identifier("a" * (MAX_IDENTIFIER_LENGTH + 1), "sink")


class TestHostileIdentifiers:
    """Every catalog entry raises, and the message names the identifier."""

    @pytest.mark.parametrize("name", HOSTILE_IDENTIFIERS)
    def test_hostile_identifier_rejected_and_named(self, name):
        """Hostile input raises IdentifierValidationError naming the input."""
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier(name, "DQDL rule field")
        message = str(excinfo.value)
        # The offending identifier is named (repr for <=300 chars, truncated
        # prefix + length beyond that) — never silently swallowed.
        if len(name) <= 300:
            assert repr(name) in message
        else:
            assert repr(name[:300]) in message
            assert str(len(name)) in message

    @pytest.mark.parametrize("name", HOSTILE_IDENTIFIERS)
    def test_hostile_identifier_never_modified(self, name):
        """No sanitize-and-continue: the exception carries the input verbatim."""
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier(name, "MERGE statement merge_key")
        assert excinfo.value.name == name

    def test_control_characters_escaped_in_message(self):
        """Newlines/null bytes appear escaped, not raw, in the message."""
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier("bad\nname\x00", "Athena column")
        message = str(excinfo.value)
        assert "\n" not in message and "\x00" not in message
        assert "\\n" in message and "\\x00" in message


class TestErrorContract:
    """IdentifierValidationError carries name + context; both in the message."""

    def test_error_carries_name_and_context_attributes(self):
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier("foo;bar", "DQDL rule field")
        err = excinfo.value
        assert err.name == "foo;bar"
        assert err.context == "DQDL rule field"

    def test_message_names_identifier_and_context(self):
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier("foo;bar", "DQDL rule field")
        message = str(excinfo.value)
        assert message.startswith("Invalid identifier 'foo;bar' in DQDL rule field")

    def test_length_failure_reports_actual_and_max_length(self):
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier("a" * 300, "sink")
        message = str(excinfo.value)
        assert "300" in message
        assert str(MAX_IDENTIFIER_LENGTH) in message

    def test_empty_string_failure_says_empty(self):
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier("", "sink")
        assert "empty" in str(excinfo.value)

    def test_error_is_plain_exception_subclass(self):
        """Catchable as Exception; not a ValueError/TypeError masquerade."""
        assert issubclass(IdentifierValidationError, Exception)


class TestNonStringInputs:
    """Non-string inputs raise IdentifierValidationError, not a bare TypeError."""

    @pytest.mark.parametrize(
        "value",
        [
            pytest.param(None, id="none"),
            pytest.param(123, id="int"),
            pytest.param(4.5, id="float"),
            pytest.param(["user_id"], id="list"),
            pytest.param(b"user_id", id="bytes"),
            pytest.param({"name": "x"}, id="dict"),
        ],
    )
    def test_non_string_raises_validation_error(self, value):
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier(value, "merge_key")
        err = excinfo.value
        assert err.name is value or err.name == value
        assert err.context == "merge_key"
        assert type(value).__name__ in str(err)


class TestBulkValidation:
    """validate_identifiers: all-valid passes; first offender named."""

    def test_all_valid_returns_list_unchanged(self):
        names = ["user_id", "email", "_ts", "col2"]
        assert validate_identifiers(names, "bulk sink") == names

    def test_empty_iterable_returns_empty_list(self):
        assert validate_identifiers([], "bulk sink") == []

    def test_accepts_any_iterable(self):
        assert validate_identifiers(iter(["a", "b"]), "bulk sink") == ["a", "b"]

    def test_raises_on_first_offender_naming_it(self):
        """The FIRST invalid entry is named — not a later one."""
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifiers(["ok_col", "bad;col", "also'bad"], "bulk sink")
        err = excinfo.value
        assert err.name == "bad;col"
        assert "'bad;col'" in str(err)

    def test_bulk_reports_context(self):
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifiers(["x; DROP TABLE users"], "Athena query builder")
        assert excinfo.value.context == "Athena query builder"
        assert "Athena query builder" in str(excinfo.value)


class TestAllowlistConstant:
    """The published regex is exactly the requirements-glossary allowlist."""

    def test_pattern_matches_requirements_glossary(self):
        assert IDENTIFIER_ALLOWLIST.pattern == r"^[A-Za-z_][A-Za-z0-9_]*$"

    def test_max_length_is_255(self):
        assert MAX_IDENTIFIER_LENGTH == 255


class TestParseIdentifierList:
    """A configured column list, stored as a comma string or sent as a list."""

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (None, []),
            ("", []),
            ("   ", []),
            ("region", ["region"]),
            ("region,dt", ["region", "dt"]),
            (" region , dt ", ["region", "dt"]),
            (["region", "dt"], ["region", "dt"]),
            (("region",), ["region"]),
            ([], []),
        ],
    )
    def test_accepted_shapes(self, value, expected):
        assert parse_identifier_list(value, "ctx") == expected

    @pytest.mark.parametrize(
        "value",
        [
            '__import__("os").getcwd()',
            'region"); import os; ("',
            "region,,dt",
            "region,",
            ["region", " dt"],
            ["region", 1],
            ["region", None],
        ],
    )
    def test_any_entry_that_is_not_an_identifier_is_refused(self, value):
        with pytest.raises(IdentifierValidationError) as exc_info:
            parse_identifier_list(value, "target_config.partition_columns")
        assert "target_config.partition_columns" in str(exc_info.value)

    @pytest.mark.parametrize("value", [{"a": "b"}, 7, 1.5, True])
    def test_another_type_is_refused_naming_it(self, value):
        with pytest.raises(IdentifierValidationError, match=type(value).__name__):
            parse_identifier_list(value, "ctx")


# ── S3 bucket names (security finding S1) ─────────────────────────────
#
# Expectations come from S3's general purpose bucket naming rules, not from the
# validator's output: 3-63 characters; only lowercase letters, digits, dots and
# hyphens; begins and ends with a letter or digit; no two adjacent dots; not in
# IPv4 form. The bucket of an operator-supplied S3 location is interpolated into
# an IAM Resource, where `*` and `?` are wildcards, which is why those two lead
# the refusal table.

VALID_BUCKET_NAMES = [
    pytest.param("abc", id="minimum-3-chars"),
    pytest.param("a" * 63, id="maximum-63-chars"),
    pytest.param("customer-analytics-lake", id="hyphens"),
    pytest.param("my.bucket.name", id="single-dots"),
    pytest.param("123", id="all-digits"),
    pytest.param("1bucket9", id="digit-edges"),
    pytest.param("a-b", id="hyphen-in-the-middle"),
    pytest.param("1.2.3", id="three-dotted-groups-is-not-ipv4"),
    pytest.param("192.168.1.1a", id="ipv4-prefix-with-a-letter"),
    pytest.param("adi-processed-files-123456789012-us-west-2", id="system-bucket-shape"),
]

INVALID_BUCKET_NAMES = [
    # IAM wildcards
    pytest.param("*", id="star"),
    pytest.param("a*b", id="embedded-star"),
    pytest.param("abc*", id="trailing-star"),
    pytest.param("?", id="question-mark"),
    pytest.param("ab?c", id="embedded-question-mark"),
    # Case and character set
    pytest.param("Bucket", id="uppercase-letter"),
    pytest.param("BUCKET", id="all-uppercase"),
    pytest.param("bucket_name", id="underscore"),
    pytest.param("my bucket", id="space"),
    pytest.param("bucket\n", id="trailing-newline"),
    pytest.param("bucket/x", id="slash"),
    pytest.param("arn:aws:s3:::x", id="an-arn"),
    pytest.param("b\u00fccket", id="non-ascii-letter"),
    pytest.param("\uff42ucket", id="fullwidth-letter"),
    # Length
    pytest.param("", id="empty"),
    pytest.param("ab", id="two-chars"),
    pytest.param("a", id="one-char"),
    pytest.param("a" * 64, id="64-chars"),
    # Edges
    pytest.param("-bucket", id="leading-hyphen"),
    pytest.param("bucket-", id="trailing-hyphen"),
    pytest.param(".bucket", id="leading-dot"),
    pytest.param("bucket.", id="trailing-dot"),
    # Adjacent dots
    pytest.param("bucket..x", id="adjacent-dots"),
    # IPv4 form
    pytest.param("192.168.1.1", id="ipv4"),
    pytest.param("10.0.0.1", id="ipv4-private"),
    pytest.param("999.999.999.999", id="ipv4-shaped-out-of-range"),
]


class TestValidateS3BucketName:
    @pytest.mark.parametrize("name", VALID_BUCKET_NAMES)
    def test_a_valid_bucket_name_is_returned_unchanged(self, name):
        assert validate_s3_bucket_name(name, "target bucket") is name

    @pytest.mark.parametrize("name", INVALID_BUCKET_NAMES)
    def test_an_invalid_bucket_name_is_refused_and_named(self, name):
        with pytest.raises(S3BucketNameError) as excinfo:
            validate_s3_bucket_name(name, "target bucket")
        err = excinfo.value
        assert err.value == name, "the error carries the input verbatim"
        assert err.context == "target bucket"
        assert repr(name) in str(err)
        assert "target bucket" in str(err)

    @pytest.mark.parametrize("value", [None, 7, b"bucket", ["bucket"]])
    def test_a_non_string_is_refused_naming_its_type(self, value):
        with pytest.raises(S3BucketNameError, match=type(value).__name__):
            validate_s3_bucket_name(value, "ctx")

    def test_the_error_is_not_a_value_error(self):
        """An `except ValueError` written for another failure must not swallow it."""
        assert not issubclass(S3BucketNameError, ValueError)
        assert issubclass(S3BucketNameError, Exception)


class TestBucketFromS3Uri:
    @pytest.mark.parametrize(
        ("uri", "bucket"),
        [
            ("s3://customer-analytics-lake/curated/", "customer-analytics-lake"),
            ("s3://customer-analytics-lake", "customer-analytics-lake"),
            ("s3://customer-analytics-lake/", "customer-analytics-lake"),
            ("s3://my.bucket/a/b/part-0000.parquet", "my.bucket"),
            ("s3://abc/prefix*with?glob", "abc"),
        ],
    )
    def test_the_bucket_of_a_valid_uri_is_returned(self, uri, bucket):
        assert bucket_from_s3_uri(uri, "target_config.s3_path") == bucket

    @pytest.mark.parametrize(
        "uri",
        [
            # The finding's own cases
            pytest.param("s3://*/", id="wildcard-bucket"),
            pytest.param("s3://a*b/x", id="embedded-wildcard"),
            pytest.param("s3://Bucket/x", id="uppercase"),
            pytest.param("s3://ab/", id="two-chars"),
            pytest.param("s3://bucket..x/", id="adjacent-dots"),
            pytest.param("s3://192.168.1.1/x", id="ipv4"),
            pytest.param("s3://?/", id="question-mark"),
            # No bucket
            pytest.param("s3://", id="scheme-only"),
            pytest.param("s3:///x", id="empty-bucket"),
            # Not an s3:// URI; nothing is repaired
            pytest.param("", id="empty"),
            pytest.param("S3://bucket/x", id="uppercase-scheme"),
            pytest.param("s3a://bucket/x", id="s3a-scheme"),
            pytest.param("https://bucket/x", id="https"),
            pytest.param("bucket/x", id="no-scheme"),
            pytest.param(" s3://bucket/x", id="leading-space"),
        ],
    )
    def test_a_uri_whose_bucket_is_invalid_is_refused_naming_the_uri(self, uri):
        with pytest.raises(S3BucketNameError) as excinfo:
            bucket_from_s3_uri(uri, "target_config.s3_path")
        err = excinfo.value
        assert err.value == uri
        assert err.context == "target_config.s3_path"
        assert repr(uri) in str(err)
        assert "target_config.s3_path" in str(err)

    @pytest.mark.parametrize("value", [None, 5, {"s3_path": "s3://bucket/"}])
    def test_a_non_string_is_refused_naming_its_type(self, value):
        with pytest.raises(S3BucketNameError, match=type(value).__name__):
            bucket_from_s3_uri(value, "target_config.temp_dir")


# ── S3 table bucket names (S1 follow-up: the role builder's S3 Tables branch) ──
#
# Expectations come from the S3 Tables naming rules
# (docs.aws.amazon.com/AmazonS3/latest/userguide/s3-tables-buckets-naming.html),
# not from the validator's output: 3-63 characters; only lowercase letters,
# digits and hyphens; begins and ends with a letter or digit; no underscore and
# no dot. Those are the general purpose rules without the dot, so every name the
# general validator refuses is refused here too, and dotted names are refused
# here only.

VALID_TABLE_BUCKET_NAMES = [
    pytest.param("abc", id="minimum-3-chars"),
    pytest.param("a" * 63, id="maximum-63-chars"),
    pytest.param("my-table-bucket", id="hyphens"),
    pytest.param("123", id="all-digits"),
    pytest.param("agentic-data-ingestion-table-orders-1a2b3c4d", id="role-builder-default-shape"),
    pytest.param("agentic-data-ingestion-table-p-1a2b3c4d-123456789012", id="create-infrastructure-default-shape"),
]

TABLE_ONLY_INVALID_NAMES = [
    pytest.param("my.bucket", id="dot"),
    pytest.param("a.b.c", id="dots"),
    pytest.param("1.2.3", id="dotted-digits"),
]


class TestValidateS3TableBucketName:
    @pytest.mark.parametrize("name", VALID_TABLE_BUCKET_NAMES)
    def test_a_valid_table_bucket_name_is_returned_unchanged(self, name):
        assert validate_s3_table_bucket_name(name, "s3_table_bucket.bucket_name") is name

    @pytest.mark.parametrize("name", INVALID_BUCKET_NAMES + TABLE_ONLY_INVALID_NAMES)
    def test_an_invalid_table_bucket_name_is_refused_and_named(self, name):
        with pytest.raises(S3BucketNameError) as excinfo:
            validate_s3_table_bucket_name(name, "s3_table_bucket.bucket_name")
        err = excinfo.value
        assert err.value == name, "the error carries the input verbatim"
        assert err.context == "s3_table_bucket.bucket_name"
        assert repr(name) in str(err)

    @pytest.mark.parametrize("name", TABLE_ONLY_INVALID_NAMES)
    def test_a_dotted_name_is_valid_for_a_general_bucket_only(self, name):
        """The one rule the two kinds differ on, and the message must not invite a dot."""
        assert validate_s3_bucket_name(name, "ctx") is name
        with pytest.raises(S3BucketNameError, match=r"lowercase letters, digits and hyphens$"):
            validate_s3_table_bucket_name(name, "ctx")

    @pytest.mark.parametrize("value", [None, 7, b"bucket", {"bucket_name": "abc"}])
    def test_a_non_string_is_refused_naming_its_type(self, value):
        with pytest.raises(S3BucketNameError, match=type(value).__name__):
            validate_s3_table_bucket_name(value, "ctx")
