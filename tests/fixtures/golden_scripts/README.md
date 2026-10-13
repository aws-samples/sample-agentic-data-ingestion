# Golden scripts — expected output, not source

The `.py` files in this directory are **fixtures, not source code.** Each one is
the exact expected rendering of a template in `pyspark_templates/`, and
`tests/unit/templates/test_golden_script_snapshot.py` compares the freshly
rendered output against them.

## Do not edit these files by hand, and do not add headers to them

Anything added to a golden file — a licence header, a comment, a reformat —
makes it differ from what the template renders, and the snapshot tests fail.
They are deliberately **excluded from the repository-wide SPDX header pass** for
this reason: a header here is a diff against generated output, not metadata.

This is not hypothetical. A publication-readiness pass added SPDX headers to
every `.py` file in the repository, which included these 16, and broke 11 tests
across `test_golden_script_snapshot.py` and `test_golden_snapshot_prop.py`. The
fix was to revert this directory.

## Regenerating

When a template legitimately changes, regenerate rather than edit:

```bash
python scripts/_regen_golden_snapshots.py
```

Then read the diff. A golden-file change is a record that rendered output moved,
so the diff is the thing under review — if it contains more than you intended,
the template change is wider than you thought.
