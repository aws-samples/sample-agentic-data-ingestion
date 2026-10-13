# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Wording guard: the held-assertion inventory's row 5 says what actually happens.

Feature: p36-schema-naming, item 3 — the sixteen wording statements.

The claim being guarded is a **frontend testing hazard**: a `vi.mock` factory
replaces the module, so any export it omits is `undefined`, and the component
then calls `undefined()`. Sixteen statements said that lands "at collection
time". It lands at **render** time, inside the test that renders the component,
as a stack trace naming a value that is not a function. p36 tasks 7.1 and 7.2
corrected all sixteen; this file is what keeps them corrected.

Two halves, and the second is the one that matters.

**Half 1 — exhaustive over the enumerated file list** (design §4.6, requirement
3.4). Each of the sixteen edited sites still states render time (3.1), still
names a stack trace rather than an assertion diff (3.2), and still preserves the
amend-the-factory-first instruction (3.3). This half is a positive check: it
fails if a correction is *deleted*, which a negative scan for the old wording
cannot see.

**Half 2 — negative scan over three whole trees** (requirement 3.6). None of the
four spellings p35 enumerated — ``collection time``, ``**collection** time``,
``at collection``, ``collection-time`` — survives anywhere in the scanned trees
outside a narrow allowlist. This is what makes a **new** site added later trip
the guard, so 3.6 stays discharged rather than discharged-once.

Not a property test. There is no input space: the file list is fixed and the
scan is exhaustive, so randomisation over it adds nothing (design §6 puts it in
the non-property table). Same family as ``test_no_lambda_threads_prop.py`` and
``test_no_duplicate_mocking_prop.py`` — a source-scanning fence, matched on
authored text because the subject *is* authored text.

Scope — an **inclusion** list, which is what keeps the two required exclusions
narrow instead of blanket:

* ``dashboard-react/src`` — where eight of the sixteen sites live, and where a
  new instance of a vitest hazard would plausibly be written. Zero tolerance:
  nothing legitimate in that tree says "collection time".
* ``.kiro`` markdown — where the other eight live, plus every other spec and
  steering document, so a new spec repeating the claim trips.
* ``agentic_data_ingestion/docs`` markdown — currently clean, included so it
  stays that way.

Everything else is out of scope **structurally**, not by allowlist. That matters
for the two comments p36 task 7.1 was told to leave alone —
``tests/integration/pipeline_flow/test_create_pipeline_integration.py:63`` and
``:78`` — which are correct statements about *pytest* collection-time
``sys.modules`` surgery, a different mechanism entirely. They are allowed
because they are not in a scanned tree, and
``test_the_excluded_pytest_collection_comments_are_a_different_subject`` pins
that so the exclusion stays legible rather than accidental.

**Why the Python trees are out of scope rather than allowlisted.** pytest has a
genuine collection phase and this repo documents it constantly. The four
spellings match at least six authored Python passages about it —
``tests/unit/lambdas/dashboard_api/conftest.py:15``, ``:58``, ``:122``,
``tests/unit/lambdas/dashboard_api/test_label_read_bound_prop.py:93``,
``tests/integration/performance/test_n_plus_one.py:282`` and the two named
above. Six allowlist entries for prose about a different mechanism is a wider
door than one scope boundary (``LGR-b46a5bb-4``: "an allowlist entry is a door").

**The correcting passages, and how they are allowed.** Twenty-one lines in
``.kiro/specs`` legitimately contain the old phrasing because they are talking
*about* it: p35's task-9 record and its checkpoint enumeration (ten lines, which
requirement 3.4 names as the source of the count), and p36's own requirements,
design and tasks (eleven lines, which quote the wrong claim to name what they
correct). ``CORRECTING_PASSAGES`` allows them by **exact stripped line text per
file, compared as a multiset** — not by a substring escape hatch. Consequences,
stated because a fence's holes should be written down:

* Rewording an allowed line trips the guard. Re-affirm it deliberately.
* Duplicating an allowed line inside the same file trips the guard, because the
  comparison is a multiset and not a set membership test.
* It does **not** catch a new instance written outside the three scanned trees,
  or a fifth spelling of the claim. The needle set is p35's four, inherited
  deliberately so this guard and that enumeration cannot drift apart; a
  paraphrase like "during collection" escapes both.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.6**
"""

import pathlib
import re

import pytest

# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------

# tests/unit/<this file> -> tests -> the Python project root. Anchored on the
# PROJECT root, not the repository root, so that every in-project path below is
# independent of where the project sits: internally it is a subdirectory of a
# larger repository, and in the published export it IS the repository root.
PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[2]

# The p35/p36 spec documents live in ``.kiro/`` at the REPOSITORY root, one level
# above the Python project. That directory is internal-only and is absent from
# the published export, so the checks that read it are SKIPPED when it is missing
# rather than failed. A guard that cannot see its subject has nothing to say
# about it; reporting 29 failures in a published clone would say something false.
REPO_ROOT = PROJECT_ROOT.parent
KIRO_ROOT = REPO_ROOT / ".kiro"
KIRO_PRESENT = KIRO_ROOT.is_dir()

_NEEDS_KIRO = pytest.mark.skipif(
    not KIRO_PRESENT,
    reason=f"{KIRO_ROOT} is absent (internal-only spec documents); this check has no subject here.",
)

DASHBOARD_SRC = PROJECT_ROOT / "dashboard-react" / "src"

SCAN_ROOTS: tuple[tuple[pathlib.Path, frozenset[str]], ...] = (
    (DASHBOARD_SRC, frozenset({".ts", ".tsx", ".js", ".jsx", ".md"})),
    *(((KIRO_ROOT, frozenset({".md"})),) if KIRO_PRESENT else ()),
    (PROJECT_ROOT / "docs", frozenset({".md"})),
)

# ---------------------------------------------------------------------------
# The wrong claim — p35's four spellings, inherited verbatim
# ---------------------------------------------------------------------------

# (display spelling, matching pattern). The two are separate because
# ``**collection** time`` has to be escaped to be a regex and unescaped to be a
# readable failure message.
WRONG_CLAIM_SPELLINGS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("collection time", re.compile(r"collection time")),
    ("**collection** time", re.compile(r"\*\*collection\*\* time")),
    ("at collection", re.compile(r"at collection")),
    ("collection-time", re.compile(r"collection-time")),
)

# ---------------------------------------------------------------------------
# Half 1: the enumerated sixteen (design §4.6)
# ---------------------------------------------------------------------------

# Eight sites in dashboard-react/src, one per file. Task 7.1 kept both phrases
# intact on a single line each on purpose, so this half needs no whitespace
# normalisation and a reflow of a comment block is itself a finding.
CODE_SITE_FILES: tuple[str, ...] = (
    "src/__tests__/pages.test.tsx",
    "src/components/__tests__/SchemaSourceSelector.async.test.tsx",
    "src/components/SchemaSourceSelector.tsx",
    "src/hooks/use-schemas.ts",
    "src/pages/__tests__/SchemaManagementPage.detail-modal.test.tsx",
    "src/pages/SchemaManagementPage.tsx",
    "src/pages/wizard/__tests__/WizardStepSchema.picker.test.tsx",
    "src/pages/wizard/WizardStepSchema.tsx",
)

# R3.1 — render time, inside the test that renders it.
RENDER_TIME_PHRASE = "during render, inside the test that renders it"
# R3.2 — a stack trace, not an assertion diff. Two clauses, both required.
STACK_TRACE_PHRASE = "a stack trace naming a value that is not a function"
ASSERTION_DIFF_PHRASE = "an assertion diff"
# R3.3 — the practical instruction.
AMEND_FIRST_LINE_PHRASE = "Amend the factory before"
AMEND_FIRST_PHRASE = "amend the factory before adding a hook to a component the factory's tests render"

# Two of the sixteen are **section headings**, which is why they carry the
# render-time correction and not the full sentence: a heading is not the place
# for a practical instruction, and the row it introduces carries it instead.
# Pinned separately so neither site is silently uncovered by the sentence check.
HEADING_PHRASE = "breaks at render time inside the test"

# The remaining eight sites, in p35's spec documents. Markdown reflows, so this
# half normalises whitespace; the counts are pinned so a deleted correction is a
# failure rather than a smaller number nobody looks at.
SPEC_DOC_SITES: tuple[tuple[str, int, int], ...] = (
    # (path relative to REPO_ROOT, full corrected passages, heading sites)
    (".kiro/specs/p35-schema-surfaces/tasks.md", 5, 1),
    (".kiro/specs/p35-schema-surfaces/design.md", 1, 1),
)

# design.md's §7 C4 row spells the amend instruction as a cross-reference to the
# row that has to change, rather than repeating the sentence. Same instruction,
# different sentence, so it needs its own needle.
DESIGN_AMEND_CROSSREF = "amend §4.8 row 5"

ENUMERATED_SITE_COUNT = 16

# ---------------------------------------------------------------------------
# Half 2: the allowlist for passages that legitimately name the old wording
# ---------------------------------------------------------------------------

# Keyed on path relative to REPO_ROOT; values are the exact stripped line text,
# compared as a MULTISET against what the scan finds. Line numbers are
# deliberately not used — they drift on every edit above them, and a guard that
# has to be renumbered gets weakened instead of updated.
CORRECTING_PASSAGES: dict[str, tuple[str, ...]] = {
    # p35 task 9's record of the correction, and checkpoint 10's enumeration —
    # the block requirement 3.4's count comes from, annotated rather than edited
    # by task 7.2 because it is the record *of* the claim, not an instance of it.
    ".kiro/specs/p35-schema-surfaces/tasks.md": (
        "> not land at collection time. It lands at *render* time, inside a test.** The `vi.mock` factory is",
        '> the phrase at `:90`). All five say "collection time". Whoever amends the inventory next should',
        "> **Row 5 says the failure lands at collection time. It lands at *render* time, inside a test.** The",
        '> **Task 9 recorded this and said five places say "collection time". The accurate figure is',
        "> checkpoint (`collection time` / `**collection** time` / `at collection` / `collection-time`,",
        '> 9\'s grep missed, because the sentence is "at **collection** time" with the emphasis markers',
        '> last bullet, phrased "at collection"). **Two in `design.md`** — `:1194` (§4.8\'s heading) and',
        '> `:1536` (§7\'s C4 row, "it is a collection-time failure, not an assertion").',
        "> p36 task 7 re-ran the enumeration across the same four spellings (`collection time` /",
        "> `**collection** time` / `at collection` / `collection-time`) plus a `TypeError` sweep over",
    ),
    # p36 — this spec. Its requirements, design and tasks quote the wrong claim
    # in order to name what they are correcting. Allowed line-exact rather than
    # by excluding the directory, so a *new* line here trips like any other.
    ".kiro/specs/p36-schema-naming/requirements.md": (
        'says a missing `vi.mock` export causes "a `TypeError` at collection time". It lands at',
        '> `pages.test.tsx:17`, whose sentence is "at **collection** time" with the emphasis',
    ),
    ".kiro/specs/p36-schema-naming/design.md": (
        "Enumerated by search in this phase across four spellings (`collection time`, `**collection** time`,",
        "`at collection`, `collection-time`) plus a `TypeError` sweep over `dashboard-react/src`. **The count",
        '| 1 | `src/__tests__/pages.test.tsx:17` — "at **collection** time", emphasis markers inside the '
        "phrase; the site p35's first grep missed |",
        "`:1000`, `:1027`, `:1267`, `:1295` (the `at collection` variant).",
        "statements about *pytest* collection-time `sys.modules` surgery. Unrelated. p35 excluded them for",
    ),
    ".kiro/specs/p36-schema-naming/tasks.md": (
        "(`collection time`, `**collection** time`, `at collection`, `collection-time`) plus a `TypeError`",
        '- `src/__tests__/pages.test.tsx:17` (the "at **collection** time" variant with emphasis markers',
        "correct statements about *pytest* collection-time `sys.modules` surgery, unrelated to this claim.",
        "`:1267`, `:1295` (the `at collection` variant).",
    ),
    # p45 — recorded by that spec, task 21.1. Its sentence is about **pytest**
    # collecting a Python contract test, where a module-scope import genuinely
    # does execute producer code as the file is collected. That is the same
    # different-mechanism case as the two excluded
    # ``test_create_pipeline_integration.py`` comments above, and it is here
    # rather than out of scope only because it lives in ``.kiro`` markdown,
    # which is a scanned tree. The claim this guard polices is p36's, about
    # vitest ``vi.mock`` factories, where an omitted export fails during render
    # rather than as the file is collected. Both statements are true in their
    # own domain; the four spellings are matched as substrings and cannot
    # separate them.
    ".kiro/specs/p45-unmapped-target-field-visibility/tasks.md": ("and would run producer code at collection time",),
}

# The two comments p36 task 7.1 was told not to edit. Named here so the
# exclusion is documented and detectable if either ever changes subject.
# Paths relative to PROJECT_ROOT, so they resolve whether or not the project is
# itself the repository root.
#
# These two line numbers are the one place in this guard that drifts. They moved
# 63/78 -> 65/80 when the repository-wide SPDX header pass added two lines to the
# top of every source file. If they drift again, re-derive them by searching the
# file for ``sys.modules`` rather than guessing an offset.
PYTEST_COLLECTION_COMMENTS: tuple[tuple[str, int], ...] = (
    ("tests/integration/pipeline_flow/test_create_pipeline_integration.py", 65),
    ("tests/integration/pipeline_flow/test_create_pipeline_integration.py", 80),
)

REMEDIATION = (
    "The claim is wrong: a `vi.mock` factory replaces the module, so an omitted export is "
    "`undefined` and the component calls `undefined()` DURING RENDER, inside the test that "
    "renders it — a stack trace naming a value that is not a function, not an assertion diff, "
    "and the file still collects and still reports per test. Use p36 design §4.6's replacement "
    "sentence verbatim so a future grep finds every site together. If the line is talking "
    "*about* the wrong claim rather than asserting it, add its exact stripped text to "
    "CORRECTING_PASSAGES above and say which spec records it. See "
    ".kiro/specs/p36-schema-naming/design.md §4.6 and requirement 3.6."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _normalise(text: str) -> str:
    """Strip comment/blockquote leaders and collapse whitespace, lowercased.

    Markdown reflows and comment blocks wrap, so a sentence that is one phrase
    to a reader is several lines to a grep. Leaders (``>``, ``//``, ``*``, ``#``)
    are removed first so the collapse joins the prose rather than the syntax.
    """
    without_leaders = re.sub(r"(?m)^\s*(?:>|//|\*|#)\s?", "", text)
    return re.sub(r"\s+", " ", without_leaders).lower()


def find_wrong_claim(text: str) -> list[tuple[int, tuple[str, ...], str]]:
    """Find every line carrying one or more of the four spellings.

    Returns one ``(line_number, spellings_matched, stripped_line)`` triple **per
    line**, not per spelling. Several of the enumeration-record lines list all
    four spellings at once, and keying on the line is what lets the allowlist be
    a multiset over lines rather than over (line, spelling) pairs.

    Matching is per raw line and deliberately **not** normalised: the negative
    scan wants the literal spellings p35 enumerated, so this guard and that
    enumeration cannot drift apart.
    """
    hits: list[tuple[int, tuple[str, ...], str]] = []
    for lineno, line in enumerate(text.splitlines(), 1):
        matched = tuple(spelling for spelling, pattern in WRONG_CLAIM_SPELLINGS if pattern.search(line))
        if matched:
            hits.append((lineno, matched, line.strip()))
    return hits


def _scanned_files() -> list[pathlib.Path]:
    """Every authored file in the three scan roots."""
    files: list[pathlib.Path] = []
    for root, suffixes in SCAN_ROOTS:
        if not root.is_dir():
            continue
        files.extend(sorted(p for p in root.rglob("*") if p.is_file() and p.suffix in suffixes))
    return files


def _read(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _code_site_path(rel: str) -> pathlib.Path:
    return DASHBOARD_SRC.parent / rel


def _rel_key(path: pathlib.Path) -> str:
    """A stable, layout-independent key for a scanned file.

    ``.kiro`` files keep their repository-root form, because that is how
    ``CORRECTING_PASSAGES`` is keyed. Everything else is keyed from the project
    root, so the key does not change when the project becomes the repository
    root in the published export.
    """
    if KIRO_PRESENT and KIRO_ROOT in path.parents:
        return str(path.relative_to(REPO_ROOT))
    return str(path.relative_to(PROJECT_ROOT))


def _lines_containing(text: str, needle: str) -> list[str]:
    return [line for line in text.splitlines() if needle in line]


SCANNED_FILES = _scanned_files()


# ---------------------------------------------------------------------------
# Non-vacuity — a guard that scans nothing passes for the wrong reason
# ---------------------------------------------------------------------------


def test_the_scan_is_not_vacuous():
    """Every scan root exists, holds files, and every enumerated site is present.

    **Validates: Requirements 3.4, 3.6**
    """
    for root, _ in SCAN_ROOTS:
        assert root.is_dir(), f"Scan root not found: {root}. Fix the path before trusting the scan."

    assert len(SCANNED_FILES) > 400, (
        f"Expected several hundred authored files across {[str(r) for r, _ in SCAN_ROOTS]}; "
        f"found {len(SCANNED_FILES)}. The negative scan below would pass vacuously."
    )

    missing = [rel for rel in CODE_SITE_FILES if not _code_site_path(rel).is_file()]
    if KIRO_PRESENT:
        missing += [rel for rel, _, _ in SPEC_DOC_SITES if not (REPO_ROOT / rel).is_file()]
    assert not missing, "Enumerated wording sites that no longer exist: " + ", ".join(missing)


# ---------------------------------------------------------------------------
# Half 1 — exhaustive over the enumerated sixteen
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("rel", CODE_SITE_FILES)
def test_every_corrected_code_site_states_render_time(rel: str):
    """R3.1, per file: the render-time phrase is intact on exactly one line.

    Un-normalised on purpose. Task 7.1 kept the phrase on a single line at all
    eight sites so this check needs no whitespace handling, which means a reflow
    that splits the sentence is itself reported rather than silently tolerated.

    **Validates: Requirements 3.1, 3.4**
    """
    lines = _lines_containing(_read(_code_site_path(rel)), RENDER_TIME_PHRASE)
    assert len(lines) == 1, (
        f"{rel}: expected exactly one line carrying {RENDER_TIME_PHRASE!r}, found {len(lines)}.\n\n" + REMEDIATION
    )


@pytest.mark.parametrize("rel", CODE_SITE_FILES)
def test_every_corrected_code_site_preserves_the_amend_first_instruction(rel: str):
    """R3.3, per file: the amend-the-factory-first instruction survives.

    This is the half of the correction that is practical rather than accurate.
    The wording fix would be a net loss if it dropped the only sentence that
    tells the next reader what to do.

    **Validates: Requirements 3.3, 3.4**
    """
    text = _read(_code_site_path(rel))
    lines = _lines_containing(text, AMEND_FIRST_LINE_PHRASE)
    assert len(lines) == 1, (
        f"{rel}: expected exactly one line carrying {AMEND_FIRST_LINE_PHRASE!r}, found {len(lines)}.\n\n" + REMEDIATION
    )
    assert AMEND_FIRST_PHRASE in _normalise(text), (
        f"{rel}: the amend-first instruction is truncated — the full instruction names the "
        f"component the factory's tests render.\n\n" + REMEDIATION
    )


@pytest.mark.parametrize("rel", CODE_SITE_FILES)
def test_every_corrected_code_site_names_a_stack_trace_not_an_assertion_diff(rel: str):
    """R3.2, per file: the observable output is a stack trace, not a diff.

    Normalised, because this clause wraps across comment lines at every site.

    **Validates: Requirements 3.2, 3.4**
    """
    normalised = _normalise(_read(_code_site_path(rel)))
    assert STACK_TRACE_PHRASE in normalised, f"{rel}: no stack-trace clause.\n\n{REMEDIATION}"
    assert ASSERTION_DIFF_PHRASE in normalised, (
        f"{rel}: the correction no longer contrasts the stack trace with an assertion diff, "
        f"which is the half of R3.2 that tells the reader what NOT to look for.\n\n" + REMEDIATION
    )


@_NEEDS_KIRO
@pytest.mark.parametrize(("rel", "passages", "headings"), SPEC_DOC_SITES)
def test_every_corrected_spec_document_carries_its_passages(rel: str, passages: int, headings: int):
    """R3.1-3.3, per spec document: every corrected passage is still there.

    Counts are pinned rather than asserted as "at least one", so deleting one of
    the six passages in ``tasks.md`` fails instead of shrinking a number nobody
    reads. Whitespace-normalised: markdown reflows.

    **Validates: Requirements 3.1, 3.2, 3.3, 3.4**
    """
    normalised = _normalise(_read(REPO_ROOT / rel))

    assert normalised.count(RENDER_TIME_PHRASE) == passages, (
        f"{rel}: expected {passages} corrected passage(s) naming render time, "
        f"found {normalised.count(RENDER_TIME_PHRASE)}.\n\n" + REMEDIATION
    )
    assert normalised.count(STACK_TRACE_PHRASE) == passages, (
        f"{rel}: expected {passages} stack-trace clause(s), found {normalised.count(STACK_TRACE_PHRASE)}."
    )
    assert normalised.count(HEADING_PHRASE) == headings, (
        f"{rel}: expected {headings} corrected heading(s) naming render time, found {normalised.count(HEADING_PHRASE)}."
    )
    # R3.3 in the documents: tasks.md repeats the instruction; design.md's C4
    # row points at the row that has to change first. Either spelling counts.
    amend_uses = normalised.count(AMEND_FIRST_PHRASE) + normalised.count(DESIGN_AMEND_CROSSREF)
    assert amend_uses >= passages, (
        f"{rel}: {passages} corrected passage(s) but only {amend_uses} amend-first instruction(s).\n\n" + REMEDIATION
    )


def test_the_sixteen_enumerated_sites_are_all_accounted_for():
    """R3.4: eight in ``dashboard-react/src``, six in p35 tasks.md, two in design.md.

    The arithmetic is pinned because item 3's count went five -> sixteen once
    already, and both corrections came from grepping rather than from a report.

    **Validates: Requirements 3.4**
    """
    code_sites = len(CODE_SITE_FILES)
    doc_sites = {rel: passages + headings for rel, passages, headings in SPEC_DOC_SITES}

    assert code_sites == 8
    # The declared arithmetic holds regardless of whether the spec documents are
    # on disk: SPEC_DOC_SITES is a transcribed constant, not a scan result, so
    # the sum is checkable in the published export even though the eight
    # ``.kiro`` sites it counts are not readable there.
    assert doc_sites[".kiro/specs/p35-schema-surfaces/tasks.md"] == 6
    assert doc_sites[".kiro/specs/p35-schema-surfaces/design.md"] == 2
    assert code_sites + sum(doc_sites.values()) == ENUMERATED_SITE_COUNT, (
        "The enumerated site count no longer sums to 16. Requirement 3.6 says a seventeenth "
        "must be included AND the revised count recorded — update p36 design §4.6 and the "
        "annotation at .kiro/specs/p35-schema-surfaces/tasks.md, then this constant."
    )


# ---------------------------------------------------------------------------
# Half 2 — the tree-wide negative scan
# ---------------------------------------------------------------------------


def test_no_scanned_file_still_carries_the_wrong_claim():
    """R3.6: no unallowed instance of any of the four spellings survives.

    Exhaustive over the three scan roots, so a **new** site added anywhere in
    them trips this without being registered first. That is what keeps 3.6
    discharged rather than discharged-once.

    **Validates: Requirements 3.1, 3.6**
    """
    violations: list[str] = []

    for path in SCANNED_FILES:
        rel = _rel_key(path)
        allowed = list(CORRECTING_PASSAGES.get(rel, ()))
        for lineno, spellings, stripped in find_wrong_claim(_read(path)):
            if stripped in allowed:
                # Multiset, not set: a second copy of an allowed line is a
                # finding, because a violation could hide behind a paste.
                allowed.remove(stripped)
                continue
            violations.append(f"{rel}:{lineno}: matched {list(spellings)} -> {stripped[:120]}")

    assert not violations, (
        f"The wrong claim survives at {len(violations)} site(s):\n  " + "\n  ".join(violations) + "\n\n" + REMEDIATION
    )


@_NEEDS_KIRO
def test_the_allowlist_holds_no_stale_entries():
    """Every allowlisted line is still present, exactly as many times as claimed.

    A stale entry is a door left open: it exempts nothing today and silently
    exempts whatever reoccupies that text tomorrow (``LGR-b46a5bb-4`` — "an
    allowlist entry is a door").

    **Validates: Requirements 3.6**
    """
    problems: list[str] = []

    for rel, allowed in CORRECTING_PASSAGES.items():
        path = REPO_ROOT / rel
        if not path.is_file():
            problems.append(f"{rel}: allowlisted file does not exist")
            continue
        stripped_lines = [line.strip() for line in _read(path).splitlines()]
        for entry in allowed:
            found = stripped_lines.count(entry)
            if found != 1:
                problems.append(f"{rel}: allowlisted line occurs {found} time(s), expected 1 -> {entry[:100]}")

    assert not problems, (
        "CORRECTING_PASSAGES is out of date:\n  "
        + "\n  ".join(problems)
        + "\n\nRemove entries whose text is gone, and re-affirm any that were reworded."
    )


def test_the_excluded_pytest_collection_comments_are_a_different_subject():
    """The two comments p36 leaves alone are about pytest, not about ``vi.mock``.

    They are allowed because they sit outside the scan roots, which is narrower
    than an allowlist entry. This asserts both halves of that: they really are
    out of scope, and they really are about ``sys.modules`` surgery at pytest
    collection — so if either ever changes subject, the exclusion stops being
    silently correct.

    **Validates: Requirements 3.4**
    """
    scanned = set(SCANNED_FILES)

    for rel, lineno in PYTEST_COLLECTION_COMMENTS:
        path = PROJECT_ROOT / rel
        assert path.is_file(), f"{rel} no longer exists; the documented exclusion is stale."
        assert path not in scanned, (
            f"{rel} is now inside a scan root. Its collection-time comments are about pytest "
            "module-cache surgery, not about the vi.mock hazard — narrow the scan roots or "
            "allowlist those two lines explicitly."
        )
        line = _read(path).splitlines()[lineno - 1]
        assert "sys.modules" in line, (
            f"{rel}:{lineno} no longer names sys.modules. p36 excluded it as a correct statement "
            "about pytest collection-time module surgery; if the subject changed, re-check it "
            "against requirement 3.6 rather than leaving the exclusion in place."
        )


# ---------------------------------------------------------------------------
# Negative control — a guard that cannot fail is worthless
# ---------------------------------------------------------------------------

VIOLATING_SOURCES: tuple[tuple[str, str], ...] = (
    ("collection time", "// the factory omits the export, so it fails at collection time.\n"),
    ("**collection** time", "// a `TypeError` at **collection** time, before any test runs.\n"),
    ("at collection", "// the module is never imported, so the file dies at collection.\n"),
    ("collection-time", "// this is a collection-time failure, not an assertion.\n"),
)


@pytest.mark.parametrize(("spelling", "source"), VIOLATING_SOURCES)
def test_the_detector_catches_every_spelling(spelling: str, source: str):
    """All four spellings p35 enumerated are actually detected.

    **Validates: Requirements 3.6**
    """
    hits = find_wrong_claim(source)
    assert hits, f"Detector missed the {spelling!r} spelling in:\n{source}"
    matched = {found for _, spellings, _ in hits for found in spellings}
    assert spelling in matched, (
        f"Detector matched {source!r} but attributed it to {sorted(matched)} rather than {spelling!r}."
    )


def test_the_detector_ignores_the_corrected_wording():
    """The replacement sentence does not trip the guard it is the fix for.

    **Validates: Requirements 3.1, 3.6**
    """
    corrected = (
        "// a `vi.mock` factory **replaces** the module, so any export it omits is\n"
        "// `undefined`; the component then calls `undefined()` **during render, inside\n"
        "// the test that renders it**. The failure is a stack trace naming a value that\n"
        "// is not a function — not an assertion diff — and the file still collects and\n"
        "// still reports per test. Amend the factory before adding a hook to a component\n"
        "// the factory's tests render.\n"
    )
    assert find_wrong_claim(corrected) == []
