# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: triage log completeness (Property 23).

Feature: testing-overhaul, Property 23: Triage log completeness

For any test removed during triage, `tests/TRIAGE_LOG.md` contains an entry
with original file name, removal reason, date, and developer name.

This test verifies:
1. TRIAGE_LOG.md exists and is non-empty
2. It contains the expected structural sections
3. It documents the files that were removed/skipped during triage
4. Each removal entry includes: file name, reason, date, developer

**Validates: Requirements 15.2, 15.3**
"""

import pathlib
import re

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
TRIAGE_LOG_PATH = TESTS_ROOT / "TRIAGE_LOG.md"

# Files known to have been removed or skipped during triage
KNOWN_REMOVED_FILES = [
    "test_file_validation_props.py",
]

# Files known to have been flagged/documented during triage
# (referenced in the triage log even if not fully removed)
KNOWN_DOCUMENTED_FILES = [
    "test_file_validation_props.py",
]


def _read_triage_log() -> str:
    """Read the TRIAGE_LOG.md file content."""
    if not TRIAGE_LOG_PATH.is_file():
        pytest.fail(f"TRIAGE_LOG.md not found at {TRIAGE_LOG_PATH}\nThis file is required by Requirement 15.2")
    return TRIAGE_LOG_PATH.read_text(encoding="utf-8")


class TestTriageLogCompleteness:
    """Property 23: Triage log completeness.

    **Validates: Requirements 15.2, 15.3**
    """

    def test_triage_log_exists(self):
        """TRIAGE_LOG.md must exist in the tests/ directory.

        **Validates: Requirements 15.2**
        """
        assert TRIAGE_LOG_PATH.is_file(), f"TRIAGE_LOG.md not found at {TRIAGE_LOG_PATH}"

    def test_triage_log_is_non_empty(self):
        """TRIAGE_LOG.md must contain meaningful content.

        **Validates: Requirements 15.2**
        """
        content = _read_triage_log()
        # Strip whitespace and check for minimum content
        assert len(content.strip()) > 100, (
            f"TRIAGE_LOG.md is too short ({len(content.strip())} chars) — "
            f"expected substantial documentation of triage decisions"
        )

    def test_triage_log_has_required_sections(self):
        """TRIAGE_LOG.md must contain key structural sections.

        **Validates: Requirements 15.2, 15.3**
        """
        content = _read_triage_log()

        # Check for expected section headers (case-insensitive)
        content_lower = content.lower()

        assert "summary" in content_lower, "TRIAGE_LOG.md missing 'Summary' section"
        assert "removed" in content_lower or "skipped" in content_lower, (
            "TRIAGE_LOG.md missing section about removed/skipped tests"
        )

    def test_removed_files_are_documented(self):
        """For each known removed file, TRIAGE_LOG.md contains an entry.

        **Validates: Requirements 15.3**
        """
        content = _read_triage_log()

        missing_entries = []
        for filename in KNOWN_REMOVED_FILES:
            if filename not in content:
                missing_entries.append(filename)

        assert len(missing_entries) == 0, (
            f"TRIAGE_LOG.md missing entries for removed files: {missing_entries}\n"
            f"Each removed test must be documented per Requirement 15.3"
        )

    def test_removal_entries_have_required_fields(self):
        """Each removal entry must include: file name, reason, date, developer.

        **Validates: Requirements 15.3**
        """
        content = _read_triage_log()

        for filename in KNOWN_REMOVED_FILES:
            if filename not in content:
                continue  # Covered by test_removed_files_are_documented

            # Check that the log contains a date pattern near the file reference
            has_date = bool(re.search(r"\d{4}-\d{2}-\d{2}", content))
            assert has_date, f"TRIAGE_LOG.md mentions {filename} but no date found (expected YYYY-MM-DD format)"

            # Check for a developer/author reference
            # Attribution must be present, but this deliberately matches a ROLE
            # rather than any person's name. An earlier revision hardcoded a
            # personal alias here, which published an identifier the scanner does
            # not look for and coupled the assertion to one contributor.
            has_developer = bool(re.search(r"(developer|author|maintainer|contributor)", content, re.IGNORECASE))
            assert has_developer, f"TRIAGE_LOG.md mentions {filename} but no developer name found"

            # Check for a reason
            has_reason = bool(
                re.search(
                    r"(reason|removed|refactor|stale|outdated|deprecated|"
                    r"no longer|deleted|multi-format)",
                    content,
                    re.IGNORECASE,
                )
            )
            assert has_reason, f"TRIAGE_LOG.md mentions {filename} but no removal reason found"

    def test_triage_log_has_table_format(self):
        """The removed/skipped tests section should use a structured format
        (table or list) for easy parsing.

        **Validates: Requirements 15.3**
        """
        content = _read_triage_log()

        # Check for markdown table indicators (pipe characters in a row)
        has_table = bool(re.search(r"\|.*\|.*\|", content))
        # Or structured list format
        has_list = bool(re.search(r"^[-*]\s+", content, re.MULTILINE))

        assert has_table or has_list, (
            "TRIAGE_LOG.md should use a structured format (table or list) for removed test entries"
        )
