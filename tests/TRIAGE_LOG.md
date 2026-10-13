# Test Triage Log

## Summary

This log documents tests that were removed or skipped during the testing overhaul triage process. Each entry includes the original file name, reason for removal, date, and developer responsible.

## Removed Tests

| File | Reason | Date | Developer |
|------|--------|------|-----------|
| test_file_validation_props.py | Removed during multi-format support refactor — property tests were tightly coupled to single-format JSON validation and no longer applicable after multi-format support added CSV, Parquet, Avro, and ORC. Replaced by format-specific unit tests in `tests/unit/lambdas/file_validation/`. | 2026-02-15 | maintainer |

## Notes

- All removed tests have been replaced by equivalent or improved coverage in the new test structure.
- The testing overhaul (Phase testing-overhaul) reorganized tests into layered directories (unit, contracts, integration, e2e).
