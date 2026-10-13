# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property test: the inventory manifest's counts are derived, not restated.

`## System Inventory` in `docs/ARCHITECTURE.md` is the single owner of every
inventory count in the documentation set. Each row states a figure, its count,
its class, and — for the classes that have one — the rule that produces the
count. This test reads that table as the source of truth and executes each rule
against the tree.

**The direction is the whole design.** The naive form of this test is
`assert count_lambda_dirs() == 25`. That relocates the staleness rather than
removing it: adding a Lambda breaks the test, someone edits the test, the doc
stays wrong, and now the staleness is *hidden*, because a green suite reads as
agreement. `LGR-8062016-2` rejected exactly that ("a second hardcoded list
inside the test"), and `LGR-768d884-1` rejected its analogue for a second reason
worth carrying: pinning a literal "teaches the next reader to update the literal
without reading why the bound exists".

So this module holds no number at all — not one integer literal appears in it,
which `test_this_test_holds_no_number_of_its_own` asserts mechanically over this
file's own AST. The counts and the rules both live in the doc, in one copy each,
and this file is a generic executor. After a count changes, the only edit that
makes this test pass is an edit to the doc.

**The derivation language is a three-verb DSL and deliberately not a shell
command.** `dirs(glob)`, `files(glob)`, `matches(path, "literal")`, with `-name`
exclusions. A real shell command read out of a markdown file and handed to
`shell=True` would turn any doc edit into arbitrary code execution inside the
test suite, and bandit excludes `tests/`, so nothing would flag it. Nothing here
imports `subprocess`; a Derivation cell reaches `pathlib.Path.glob` and
`str.count`, and has no path to a shell.

**There is no allowlist, no skip-list and no per-row exemption**
(`LGR-b46a5bb-4`: "an allowlist entry is a door"). An unparseable cell fails and
names itself. The two legitimate fixes are to correct the cell or to demote the
row to Class C with a command and a date, and both are edits to the doc. That
asymmetry is what the mechanism rests on.

Class A and B rows carry a machine-executable derivation and get a numeric
check. Class C and D rows carry a re-run command plus an ISO date and get no
numeric check — for those the date *is* the assertion, because a figure with
neither a derivation nor a date is the defect R23.6 describes.

**Placement and concurrency.** This file sits at the `tests/unit/` root because
it is a repository-scope assertion. It is **not** one of the four
`make test-meta` tests (`test_hierarchy_placement_prop.py`,
`test_marker_selection_prop.py`, `test_no_duplicate_mocking_prop.py`,
`test_migration_preservation_prop.py`) and it **carries no run-alone rule of its
own**: it writes no stub files and reads none of the five layer directories, so
`test_marker_selection_prop.py`'s stub window cannot affect it in either
direction. Its gate is the tier-2 row in `.kiro/steering/testing-standards.md`:
`make test-scope DIR=tests/unit/test_doc_inventory_counts_prop.py`.

Example-based despite the `_prop` suffix, which `LGR-8062016-2` establishes as
accepted for invariants over a repository rather than over generated inputs.

Validates: Requirements 17.8, 23.3, 23.4, 23.5, 23.6
"""

from __future__ import annotations

import ast
import pathlib
import re
from typing import NamedTuple

import pytest

TESTS_ROOT = pathlib.Path(__file__).resolve().parent.parent
PROJECT_ROOT = TESTS_ROOT.parent

# The manifest and its section. Paths inside Derivation cells are relative to
# PROJECT_ROOT, as the manifest's own preamble states.
ARCHITECTURE_DOC = PROJECT_ROOT / "docs" / "ARCHITECTURE.md"
SECTION_HEADING = "## System Inventory"
HEADER_CELLS = ("Figure", "Count", "Class", "Derivation")

# Classes that carry a machine-executable derivation, and classes that carry a
# re-run command plus an ISO date instead.
DERIVED_CLASSES = frozenset({"A", "B"})
DATED_CLASSES = frozenset({"C", "D"})
KNOWN_CLASSES = DERIVED_CLASSES | DATED_CLASSES

# The DSL. Verb names are data so the grammar and the coverage check cannot
# drift apart: the patterns below are built from these sets.
GLOB_VERBS = frozenset({"dirs", "files"})
LITERAL_VERBS = frozenset({"matches"})
SUPPORTED_VERBS = GLOB_VERBS | LITERAL_VERBS

# Grammar features beyond the bare verbs, tracked so the both-directions check
# covers them too.
EXCLUSION_FEATURE = "-name exclusion"
GLOB_PATH_FEATURE = 'glob path in matches(path, "literal")'
SUPPORTED_FEATURES = SUPPORTED_VERBS | frozenset({EXCLUSION_FEATURE, GLOB_PATH_FEATURE})

# expr := ("dirs" | "files") "(" glob ("," "-" name)* ")"
_GLOB_EXPR = re.compile(
    r"^(?P<verb>" + "|".join(sorted(GLOB_VERBS)) + r")"
    r"\(\s*(?P<glob>[^,)]+?)\s*(?P<exclusions>(?:,\s*-[^,)]+)*)\s*\)$"
)
# expr := "matches" "(" path "," '"' literal '"' ")"
# The literal is matched greedily up to the final double quote so that inner
# single quotes survive: several live cells carry them, e.g.
# matches(deploy-cdk/lib/data-stack.ts, "tableName: '").
_LITERAL_EXPR = re.compile(
    r"^(?P<verb>" + "|".join(sorted(LITERAL_VERBS)) + r")"
    r'\(\s*(?P<path>[^,]+?)\s*,\s*"(?P<literal>.*)"\s*\)$'
)
_EXCLUSION_TOKEN = re.compile(r",\s*-(?P<name>[^,)]+)")

# A Count cell is one integer, or the visible-arithmetic form R17.4 requires
# (`14 = 5 + 9`), or a pointer at an owner elsewhere (`see owner`).
_PLAIN_COUNT = re.compile(r"^(?P<total>\d+)$")
_SUM_COUNT = re.compile(r"^(?P<total>\d+)\s*=\s*(?P<addends>\d+(?:\s*\+\s*\d+)+)$")
_ADDEND = re.compile(r"\d+")

_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_BACKTICKED = re.compile(r"`[^`]+`")

_SEPARATOR_CHARS = frozenset("-: ")
_GLOB_CHARS = frozenset("*?[")
_PARENT_PART = ".."
_DOT = "."
_BACKTICK = "`"


class Row(NamedTuple):
    """One manifest row, as written."""

    figure: str
    count: str
    klass: str
    derivation: str
    source_line: str


class Derivation(NamedTuple):
    """A parsed Derivation cell."""

    verb: str
    pattern: str
    exclusions: tuple[str, ...]
    literal: str | None


class StatedCount(NamedTuple):
    """A parsed Count cell."""

    total: int
    addends: tuple[int, ...]


def _unfence(cell: str) -> str:
    """Strip one layer of surrounding backticks, if present."""
    stripped = cell.strip()
    if stripped.startswith(_BACKTICK) and stripped.endswith(_BACKTICK):
        return stripped.strip(_BACKTICK).strip()
    return stripped


def _require_section() -> str:
    """Return the `## System Inventory` section, or skip with a message.

    Absence is part of the interface. A missing doc or a missing section is a
    different defect from a wrong one, so this skips rather than fails — but it
    skips loudly, so a suite that has stopped checking anything says so. Matches
    `test_test_mapping_completeness_prop.py`'s `_require_doc`.
    """
    if not ARCHITECTURE_DOC.is_file():
        pytest.skip(f"architecture doc not present in this tree: {ARCHITECTURE_DOC}")

    inside = False
    collected: list[str] = []
    for line in ARCHITECTURE_DOC.read_text(encoding="utf-8").splitlines():
        if line.strip() == SECTION_HEADING:
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside:
            collected.append(line)

    if not inside:
        pytest.skip(f"{SECTION_HEADING!r} section not present in {ARCHITECTURE_DOC}")
    return "\n".join(collected)


def _manifest_rows() -> list[Row]:
    """Parse the manifest table.

    Rows are taken from the contiguous table introduced by the exact header
    `HEADER_CELLS`, so prose tables elsewhere in the section are not mistaken
    for manifest rows — and, in the other direction, a row inside that block
    that does not have four columns fails rather than being quietly dropped.
    """
    section = _require_section()
    rows: list[Row] = []
    inside_table = False

    for raw in section.splitlines():
        line = raw.strip()
        if not line.startswith("|"):
            if inside_table:
                break  # the table ended
            continue

        cells = tuple(cell.strip() for cell in line.strip("|").split("|"))
        if cells == HEADER_CELLS:
            inside_table = True
            continue
        if not inside_table:
            continue
        if all(frozenset(cell) <= _SEPARATOR_CHARS for cell in cells):
            continue  # the |---|---| separator

        try:
            figure, count, klass, derivation = cells
        except ValueError:
            pytest.fail(
                "Inventory row does not have the four columns "
                f"{HEADER_CELLS} the manifest schema requires: {line}\n"
                "Fix the row in docs/ARCHITECTURE.md — this test has no way to "
                "skip it."
            )
        rows.append(
            Row(
                figure=figure,
                count=count,
                klass=_unfence(klass).strip("*").upper(),
                derivation=derivation,
                source_line=line,
            )
        )

    if not inside_table:
        pytest.fail(
            f"{SECTION_HEADING!r} exists in {ARCHITECTURE_DOC} but carries no "
            f"table headed {HEADER_CELLS}. The section is the interface this "
            "test consumes; without the table it asserts nothing."
        )
    if not rows:
        pytest.fail(f"{SECTION_HEADING!r} carries a manifest header but no rows.")
    return rows


def _cell_fail(row: Row, problem: str) -> None:
    """Fail naming the offending row and cell. Never skip, never exempt."""
    pytest.fail(
        f"Unparseable inventory row {row.figure!r}: {problem}\n"
        f"  row: {row.source_line}\n"
        "The two legitimate fixes are both edits to docs/ARCHITECTURE.md: "
        "correct the cell, or demote the row to Class C with a command and an "
        "ISO date. There is no allowlist and no skip-list."
    )


def _stated_count(row: Row) -> StatedCount:
    """Read the Count cell. Both supported forms, no fallback."""
    cell = _unfence(row.count)

    plain = _PLAIN_COUNT.match(cell)
    if plain is not None:
        return StatedCount(total=int(plain.group("total")), addends=())

    summed = _SUM_COUNT.match(cell)
    if summed is not None:
        addends = tuple(int(part) for part in _ADDEND.findall(summed.group("addends")))
        return StatedCount(total=int(summed.group("total")), addends=addends)

    _cell_fail(
        row,
        f"Count cell {cell!r} is neither an integer nor the visible-arithmetic "
        "form (`14 = 5 + 9`). A Class A or B row must state a number this test "
        "can compare against.",
    )
    raise AssertionError  # unreachable; pytest.fail raises


def _parse_derivation(row: Row) -> Derivation:
    """Read the Derivation cell as a DSL expression.

    Three verbs, one exclusion form, no combinators, and no escape to a shell.
    """
    cell = _unfence(row.derivation)

    glob_match = _GLOB_EXPR.match(cell)
    if glob_match is not None:
        exclusions = tuple(
            token.group("name").strip() for token in _EXCLUSION_TOKEN.finditer(glob_match.group("exclusions"))
        )
        return Derivation(
            verb=glob_match.group("verb"),
            pattern=glob_match.group("glob").strip(),
            exclusions=exclusions,
            literal=None,
        )

    literal_match = _LITERAL_EXPR.match(cell)
    if literal_match is not None:
        return Derivation(
            verb=literal_match.group("verb"),
            pattern=literal_match.group("path").strip(),
            exclusions=(),
            literal=literal_match.group("literal"),
        )

    _cell_fail(
        row,
        f"Derivation cell {cell!r} is not an expression in the derivation "
        f"language. Supported verbs: {sorted(SUPPORTED_VERBS)}, with `-name` "
        "exclusions.",
    )
    raise AssertionError  # unreachable; pytest.fail raises


def _visible(path: pathlib.Path) -> bool:
    """Dotfiles are not matched by the glob, as the manifest's own rule states.

    `pathlib.Path.glob` does match them, unlike a shell, so the filter is
    explicit here — otherwise editor and OS droppings such as `.DS_Store` would
    silently inflate a count.
    """
    return not any(part.startswith(_DOT) for part in path.relative_to(PROJECT_ROOT).parts)


def _check_pattern(row: Row, pattern: str) -> None:
    parts = pathlib.PurePosixPath(pattern).parts
    if pattern.startswith("/") or _PARENT_PART in parts:
        _cell_fail(
            row,
            f"pattern {pattern!r} is not relative to the project directory. "
            "Derivations address paths under agentic_data_ingestion/ only.",
        )


def _glob(row: Row, pattern: str) -> list[pathlib.Path]:
    _check_pattern(row, pattern)
    # A trailing slash in `dirs(lambda/*/)` is documentation of intent; the verb
    # is what selects directories, so it is stripped before globbing.
    return sorted(path for path in PROJECT_ROOT.glob(pattern.rstrip("/")) if _visible(path))


def _kept(path: pathlib.Path, exclusions: tuple[str, ...]) -> bool:
    """Apply `-name` exclusions against the entry name and its path parts."""
    parts = path.relative_to(PROJECT_ROOT).parts
    return not any(token == path.name or token in parts for token in exclusions)


def _execute(row: Row, derivation: Derivation) -> int:
    """Run a parsed derivation against the tree. No shell, by construction."""
    if derivation.verb in GLOB_VERBS:
        selects_dir = derivation.verb == "dirs"
        return len(
            [
                path
                for path in _glob(row, derivation.pattern)
                if (path.is_dir() if selects_dir else path.is_file()) and _kept(path, derivation.exclusions)
            ]
        )

    # matches(path, "literal") — occurrences of the literal, across every file
    # the path names. The path may itself be a glob.
    literal = derivation.literal
    if literal is None or not literal:
        _cell_fail(row, "matches(...) carries an empty literal, which would count every position.")

    if frozenset(derivation.pattern) & _GLOB_CHARS:
        targets = [path for path in _glob(row, derivation.pattern) if path.is_file()]
        if not targets:
            _cell_fail(row, f"glob {derivation.pattern!r} matches no file, so the literal cannot be counted.")
    else:
        _check_pattern(row, derivation.pattern)
        single = PROJECT_ROOT / derivation.pattern
        if not single.is_file():
            _cell_fail(row, f"path {derivation.pattern!r} does not name a file in this tree.")
        targets = [single]

    return sum(target.read_text(encoding="utf-8").count(literal) for target in targets)


def _derived_rows(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.klass in DERIVED_CLASSES]


def _dated_rows(rows: list[Row]) -> list[Row]:
    return [row for row in rows if row.klass in DATED_CLASSES]


def _features_used(rows: list[Row]) -> set[str]:
    """Which grammar features the manifest actually exercises."""
    used: set[str] = set()
    for row in _derived_rows(rows):
        derivation = _parse_derivation(row)
        used.add(derivation.verb)
        if derivation.exclusions:
            used.add(EXCLUSION_FEATURE)
        if derivation.verb in LITERAL_VERBS and frozenset(derivation.pattern) & _GLOB_CHARS:
            used.add(GLOB_PATH_FEATURE)
    return used


def test_every_row_carries_a_known_class():
    """A row with an unrecognised class gets neither check, so it is a hole."""
    rows = _manifest_rows()
    unknown = sorted(f"{row.figure!r} -> class {row.klass!r}" for row in rows if row.klass not in KNOWN_CLASSES)
    assert not unknown, (
        f"Inventory rows whose Class is not one of {sorted(KNOWN_CLASSES)}:\n  "
        + "\n  ".join(unknown)
        + "\nA row outside the classification is checked by nothing. Assign it "
        "a class in docs/ARCHITECTURE.md."
    )


def test_every_derived_row_agrees_with_the_tree():
    """The central assertion: each stated count equals its own stated rule.

    The number on the left of each mismatch came from the doc and the number on
    the right came from the filesystem. Neither came from this file.
    """
    rows = _manifest_rows()
    derived = _derived_rows(rows)
    assert derived, (
        "The manifest has no Class A or B row, so this test compares nothing. "
        f"Classes carrying a derivation: {sorted(DERIVED_CLASSES)}."
    )

    mismatches: list[str] = []
    for row in derived:
        stated = _stated_count(row)
        derivation = _parse_derivation(row)
        actual = _execute(row, derivation)
        if actual != stated.total:
            mismatches.append(
                f"{row.figure}: Count says {stated.total}, but its own rule `{_unfence(row.derivation)}` gives {actual}"
            )
        if stated.addends and sum(stated.addends) != stated.total:
            mismatches.append(
                f"{row.figure}: Count cell {_unfence(row.count)!r} does not add "
                f"up — the parts sum to {sum(stated.addends)}, not {stated.total}"
            )

    assert not mismatches, (
        "Inventory rows that disagree with their own derivation:\n  "
        + "\n  ".join(mismatches)
        + "\nFix docs/ARCHITECTURE.md — correct the Count, or correct the "
        "Derivation if the counting rule is what changed. This test holds no "
        "number of its own, so editing the test cannot make it pass."
    )


def test_every_dated_row_carries_a_command_and_a_date():
    """Class C and D rows get no numeric check, so the date is the assertion.

    A C or D figure with no re-run instruction and no ISO date is the defect
    R23.6 describes: a number nobody can reproduce. A row with a command but no
    date fails the same way as one with neither.

    "A command is present" is read as "the cell carries something in backticks"
    — a command for a C row, or the owner reference a D row points at, since a D
    row's instruction is to re-run what its owner states.
    """
    rows = _manifest_rows()
    dated = _dated_rows(rows)
    assert dated, (
        "The manifest has no Class C or D row. If every figure became "
        "derivable that is good news, but confirm it rather than assume it."
    )

    missing_command: list[str] = []
    missing_date: list[str] = []
    for row in dated:
        if _BACKTICKED.search(row.derivation) is None:
            missing_command.append(f"{row.figure!r}: {row.derivation}")
        if _ISO_DATE.search(row.derivation) is None:
            missing_date.append(f"{row.figure!r}: {row.derivation}")

    assert not missing_command, (
        "Class C/D rows carrying no re-run command or owner reference (nothing "
        "in backticks):\n  " + "\n  ".join(missing_command)
    )
    assert not missing_date, (
        "Class C/D rows carrying no ISO date (YYYY-MM-DD):\n  "
        + "\n  ".join(missing_date)
        + "\nA measured figure with no date cannot be told from a stale one. "
        "Re-run the command and record the date, or promote the row to Class B "
        "with a derivation."
    )


def test_every_manifest_row_is_interpretable():
    """Direction one of the both-directions diff: manifest -> interpreter.

    Every Class A/B row's Count and Derivation parse, and every C/D row has the
    shape its class requires. A row this interpreter cannot read is a row that
    would otherwise pass by being ignored — which is why the parse helpers fail
    naming the cell instead of skipping it (`LGR-b46a5bb-4`).
    """
    rows = _manifest_rows()
    for row in _derived_rows(rows):
        _stated_count(row)
        _parse_derivation(row)
    # C/D shape is asserted by test_every_dated_row_carries_a_command_and_a_date;
    # what this test adds is that no row falls outside both checks.
    unchecked = sorted(row.figure for row in rows if row.klass not in KNOWN_CLASSES)
    assert not unchecked, f"Rows checked by neither the derived nor the dated path: {unchecked}"


def test_every_supported_grammar_feature_is_exercised():
    """Direction two of the both-directions diff: interpreter -> manifest.

    An unreachable verb is untested code that will be trusted the first time
    someone uses it in a cell. `LGR-8062016-2` established this both-directions
    shape for `test_test_mapping_completeness_prop.py`; this applies it to a
    different table.
    """
    unreachable = sorted(SUPPORTED_FEATURES - _features_used(_manifest_rows()))
    assert not unreachable, (
        "Derivation-language features this interpreter supports that no "
        "manifest row uses:\n  "
        + "\n  ".join(unreachable)
        + "\nEither a row should use the feature, or the interpreter should "
        "stop supporting it. Untested support is support nobody has checked."
    )


def test_this_test_holds_no_number_of_its_own():
    """Correctness Property 3, asserted rather than reviewed.

    Every number this module compares against is read from the file under
    validation. The mechanical form of that claim is that the module's own AST
    contains no integer constant at all — not for a count, and not for a column
    index that a later edit could turn into a count. Booleans are `int`
    subclasses in Python and are excluded; they carry no figure.
    """
    tree = ast.parse(pathlib.Path(__file__).read_text(encoding="utf-8"))
    literals = sorted(
        {
            node.value
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, int) and not isinstance(node.value, bool)
        }
    )
    assert not literals, (
        f"Integer literals found in {pathlib.Path(__file__).name}: {literals}\n"
        "This file must hold no number of its own. A count kept here is the "
        "staleness relocated from the doc into the test, where a green suite "
        "reads as agreement (LGR-8062016-2, LGR-768d884-1). Read the number "
        "from the manifest instead."
    )
