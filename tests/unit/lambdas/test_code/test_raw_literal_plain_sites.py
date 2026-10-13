# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 T1 — raw literals at the twelve plain-fence sites and the `_cast` key site.

**Validates: Requirements 1.1, 2.1, 3.1, Property 1**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: SEMANTICS. CURRENT OUTPUT MUST NOT BE
CONSULTED. READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 runs two opposite rules inside `tests/unit/lambdas/test_code/`, so
every new file has to say which one it follows.

**This file follows the semantics rule.** Every expectation below is derived
from Python string semantics and from `bugfix.md` 2.1, never from what the
converter emits. The derivation is one sentence and it is the whole file: **for
a raw literal the source text IS the value** — no escape processing occurs — so
`r"plain"` and `"plain"` denote the same six-character string, and a checker
that converts one and refuses the other is making a false statement about the
one it refused (`bugfix.md` 1.1, 1.2, 2.16).

**The files that follow the OPPOSITE rule** — current output IS the oracle —
are task 3's preservation pins (`test_string_site_preservation.py`) and task
1.1's characterization classes in `test_passthrough_and_alias_refusal.py`. Do
not reconcile an assertion here with a pin there; where they describe the same
site they are *meant* to disagree until task 4.3 lands.

**EXPECTED-RED WINDOW: every case in this file except the `TestPlainTwin*`
classes is RED until task 4.3 lands.** The failure mode is a **refusal** —
`ConversionError` raised where conversion should succeed — not an assertion
mismatch on text. **Do NOT "fix" a test or the production code because
something here is red.** A green here before task 4.3 would mean the
expectations were transcribed from current output, which is the exact failure
`LGR-28b4d75-5` describes.

──────────────────────────────────────────────────────────────────────
Why this file exists separately from `test_raw_literal_regex_fidelity.py`
──────────────────────────────────────────────────────────────────────

**This is the file that would catch a fix applied only at the regex sites.**
`design.md` §9 Q5's *rejected* arm — escape at the two `r"…"` emission sites and
leave `_string` returning source text — passes every case in task 2.1's
regex-fence file and **fails every case here**. That is Q5's argument made
checkable in CI rather than only argued in prose, and it is why
`TestThisFileIsTheQ5Discriminator` asserts that no case in this module is a
raw-fence site.

──────────────────────────────────────────────────────────────────────
Two things a later task must NOT do, recorded where they will be read
──────────────────────────────────────────────────────────────────────

1. **`:985` `_cast`'s string target key must NOT gain a fence wrap at task
   4.3.** It is a dict lookup into `_CAST_TARGETS`, not an emission — the value
   never reaches emitted text, it selects the emission. `design.md` §5.1 row 6
   and `tasks.md` 4.3 both say "no wrap"; `TestCastStringTargetKeyNeedsTheValue
   Only` is the assertion that makes adding one visible.
2. **The eight `P40_UNPINNED_CAST_KEY_PINS`
   (`test_converter_equivalence_baseline.py:1185`) must stay byte-exact** — that
   is `tasks.md` 11's diff review, and a moved pin there is the symptom of the
   wrap at (1) having been added. Nothing in this file touches them; this note
   exists so the two facts sit next to each other.

──────────────────────────────────────────────────────────────────────
Scope of the bodies used here, and where the escape cases live
──────────────────────────────────────────────────────────────────────

Every raw body below is **backslash-free**, deliberately. That makes the
semantic claim unambiguous — value equals source text equals the plain twin's
value — and it isolates the trigger `bugfix.md` 1.2 identifies: **the `r`
prefix alone**, not escaping. `regexp_replace(col("p"), r"[^0-9+]", "")` refuses
today with no backslash anywhere in it.

Raw bodies that DO carry a recognised escape at a plain-fence site — `col(r"a\b")`
emitting a column named backslash-b rather than the backspace `"a\b"` resolves
to — are **D5**, and task 6.1 extends *this* file with them. D5 has no red
window because it does not exist at HEAD (`col(r"a\b")` refuses today); it is a
defect of the *naive* D1 fix. Keeping the two groups apart is what lets a
reviewer see which claim each case is making.

──────────────────────────────────────────────────────────────────────
What "legitimately receives a raw literal" means here
──────────────────────────────────────────────────────────────────────

`tasks.md` 2.2 asks that a site which cannot legitimately receive a raw literal
be named with its reason instead of given an invented case. **All thirteen sites
in scope can receive one, and none is excluded.** Legitimacy is `bugfix.md`
2.16's standard — *valid Spark the checker must not make a false statement
about* — not *a spelling the generator has been measured writing*. Measured raw
usage is regex-only (5 of 5 regex literals in 56 generated versions were raw),
which is why task 2.1 carries the **measured** cases and this file carries the
**fan-in**: a relaxation of `_string` reaches all fifteen sites whether the
generator exercises them or not (`bugfix.md` 3.1), so all fifteen have to be
stated.

The two sites NOT in this file are the two raw-fence ones — `:845`
`regexp_replace` pattern and `:1232` `rlike` pattern — which are task 2.1's, by
`design.md` §5.1's split.

pandas is not installed in this venv and must not be (`bugfix.md` 3.14). Nothing
here executes the emitted pandas, so every assertion below is evidence about the
**translation**, not about the execution.

No shared fixture from `tests/fixtures/conftest.py` is relevant: the converter is
pure text-to-text and touches no AWS surface.
"""

import ast
import os
import sys
from typing import NamedTuple

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import (
    _CAST_TARGETS,
    ConversionError,
    PySpark2PandasConverter,
)

#: The two-import header `convert()` prepends to every emission (`bugfix.md` 3.7).
P41_PANDAS_HEADER = "import pandas as pd\nimport numpy as np\n\n"

#: The converter, for the fan-in audit. Read as text rather than imported so the
#: audit is over the shipped source and not over an already-imported module.
P41_CONVERTER_PATH = os.path.join(
    os.path.dirname(__file__),
    "..",
    "..",
    "..",
    "..",
    "lambda",
    "agentic_data_ingestion_test_code",
    "pyspark_to_pandas.py",
)


# ──────────────────────────────────────────────────────────────────────
# Helpers. `p41_` prefixed per `design.md` §10, which also carries the standing
# naming fence: `is_bug_condition_d3` already exists at
# `test_converter_equivalence_baseline.py:910` and means an EARLIER spec's
# Defect 3, so no p41 helper may take that name.
# ──────────────────────────────────────────────────────────────────────


def p41_emitted_body(source: str) -> str:
    """The pandas `convert()` emits for *source*, with the import header removed."""
    code = PySpark2PandasConverter.convert(source)["code"]
    assert code.startswith(P41_PANDAS_HEADER), code
    return code[len(P41_PANDAS_HEADER) :]


def p41_emitted_string_values(body: str) -> "list[str]":
    """Every string literal VALUE in the emitted pandas.

    Values, not text — `ast` resolves each fence to what it denotes, which is
    the only level at which this file's claim can be stated. Asserting over
    emitted *text* would pass on D3 (a successful conversion whose meaning
    changed) and asserting only "did not raise" would pass on it too
    (`bugfix.md` 1.20).

    Order is `ast.walk`'s traversal order, not source order, so every caller
    below compares as a sorted multiset or by membership. Nothing here depends
    on the order.
    """
    return [n.value for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def p41_plain_twin(source: str, raw_source_text: str) -> str:
    """*source* with the one `r` prefix dropped — the same VALUE, plainly spelled.

    The twin is the mechanical statement of this file's premise: dropping the
    `r` from a backslash-free raw literal cannot change what the literal
    denotes, so the two spellings must convert identically. The twin converts
    today; the raw spelling does not.
    """
    assert raw_source_text.startswith('r"'), raw_source_text
    assert source.count(raw_source_text) == 1, source
    return source.replace(raw_source_text, raw_source_text[1:], 1)


class P41RawPlainSiteCase(NamedTuple):
    """One raw literal at one plain-fence `_string` site, with its derived value."""

    case_id: str
    #: The `_string` / `_column_name` call site, as `design.md` §5.1 numbers it.
    #: Line numbers are DOCUMENTATION ONLY — they drift the moment task 4.3 adds
    #: `_dq`/`_raw` at module level. `TestThisFileIsTheQ5Discriminator` audits
    #: the fan-in by call-site COUNT, which does not drift.
    site: str
    #: What the site is, in one phrase, so the file reads against §5.1's table.
    what: str
    source: str
    #: The raw literal's source text, exactly as spelled in `source`.
    raw_source_text: str
    #: The raw literal's body — and, because the literal is raw, its VALUE.
    raw_body: str
    #: The value the site must carry into the emission. Equal to `raw_body`
    #: except where the site legitimately transforms the value (`:915`), and
    #: `None` where the site emits nothing at all (`:985`).
    expected_site_value: "str | None"
    #: Every string VALUE the emitted pandas must carry, as a sorted multiset.
    expected_values: "tuple[str, ...]"


# ══════════════════════════════════════════════════════════════════════
# THE THIRTEEN CASES — enumerated from `design.md` §5.1's table, in its
# row order, NOT invented. Rows 1 and 7 (`:845`, `:1232`) are the two
# raw-fence sites and belong to task 2.1's file; every other row is here.
#
# `expected_values` is a composite and the two halves have different
# provenance, which matters for the oracle rule:
#   * the SITE value is derived from raw-string semantics (this file's rule);
#   * the other literals in each statement are plain, so their values are
#     the PRESERVED ones task 3.1 pins (current output is the oracle there).
# `TestPlainTwinIsTheSameValue` cross-checks the composite mechanically, so
# no multiset below is a hand transcription anybody has to trust.
# ══════════════════════════════════════════════════════════════════════

P41_RAW_PLAIN_SITE_CASES: "list[P41RawPlainSiteCase]" = [
    # ── §5.1 row 2 — direct, plain `"…"` fence. ──
    P41RawPlainSiteCase(
        case_id="raw-regexp-replace-replacement",
        site=":846",
        what="regexp_replace replacement",
        source='x = regexp_replace(col("p"), "[^0-9]", r"X")',
        raw_source_text='r"X"',
        raw_body="X",
        expected_site_value="X",
        expected_values=("X", "[^0-9]", "p"),
    ),
    # ── §5.1 row 3 — direct. The separator only; `concat_ws`'s hardcoded
    # `.astype(str)` on COLUMN parts is a separate, real fidelity divergence
    # deliberately out of scope (3.11, p40 §5 N3a) and must end this spec with
    # an empty diff. Nothing here touches it. ──
    P41RawPlainSiteCase(
        case_id="raw-concat-ws-separator",
        site=":852",
        what="concat_ws separator",
        source='x = concat_ws(r"-", col("a"), col("b"))',
        raw_source_text='r"-"',
        raw_body="-",
        expected_site_value="-",
        expected_values=("-", "a", "b"),
    ),
    # ── §5.1 row 4 — direct, and the ONE site that legitimately transforms the
    # value: the body is a Java SimpleDateFormat pattern and the emission is its
    # strftime translation. `yyyy`→`%Y`, `MM`→`%m`, `dd`→`%d` is SimpleDateFormat
    # semantics, derived here rather than read off the converter. The fidelity
    # claim at this site is that the string handed to the mapping is the raw
    # BODY (`yyyy-MM-dd`), not the source text (`r"yyyy-MM-dd"`); the mapping
    # itself is 3.5's preserved behaviour and task 4.3 must leave it alone. ──
    P41RawPlainSiteCase(
        case_id="raw-to-date-format",
        site=":915",
        what="_to_datetime format string",
        source='x = to_date(col("d"), r"yyyy-MM-dd")',
        raw_source_text='r"yyyy-MM-dd"',
        raw_body="yyyy-MM-dd",
        expected_site_value="%Y-%m-%d",
        expected_values=("%Y-%m-%d", "coerce", "d"),
    ),
    # ── §5.1 row 5 — direct. A word-character name deliberately: `.rename("y")`
    # is what `_SELECT_RENAME_SUFFIX` (`:205`) matches on to resolve a select
    # output column, and §5.1 row 5 requires that match to keep working. ──
    P41RawPlainSiteCase(
        case_id="raw-alias-name",
        site=":961",
        what="alias new name",
        source='x = col("a").alias(r"plain")',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("a", "plain"),
    ),
    # ── §5.1 row 6 — direct, and the ONLY site with no fence at all: the value
    # is a dict key into `_CAST_TARGETS`, never emitted. `int` is chosen over
    # `string` on purpose — its emission (`Int64`) differs from the key, so the
    # assertion cannot pass by the key coincidentally appearing in the output.
    # `expected_site_value=None` records "this site emits nothing"; task 4.3
    # must NOT add a wrap here. ──
    P41RawPlainSiteCase(
        case_id="raw-cast-string-target-key",
        site=":985",
        what="_cast string target key (no fence — dict lookup)",
        source='x = col("a").cast(r"int")',
        raw_source_text='r"int"',
        raw_body="int",
        expected_site_value=None,
        expected_values=("Int64", "a", "coerce"),
    ),
    # ── §5.1 row 8 — via `_column_name`. `tasks.md` 2.2's own worked example:
    # `col(r"plain")` → `df["plain"]`, not `df["r\"plain\""]` and not a refusal. ──
    P41RawPlainSiteCase(
        case_id="raw-col-column-name",
        site=":814",
        what="col column name",
        source='x = col(r"plain")',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("plain",),
    ),
    # ── §5.1 row 9 — via `_column_name`. The new-frame `.copy()` semantics in
    # the emission are 3.1/3.2's preserved behaviour, not this file's subject. ──
    P41RawPlainSiteCase(
        case_id="raw-with-column-column",
        site=":1174",
        what="withColumn column name",
        source='df_transformed = df.withColumn(r"plain", col("a"))',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("a", "plain"),
    ),
    # ── §5.1 rows 10 and 11 — via `_column_name`, TWO sites in one table row,
    # so two cases. The raw literal moves; everything else is identical. ──
    P41RawPlainSiteCase(
        case_id="raw-with-column-renamed-old",
        site=":1196",
        what="withColumnRenamed old name",
        source='df_transformed = df.withColumnRenamed(r"old_name", "new_name")',
        raw_source_text='r"old_name"',
        raw_body="old_name",
        expected_site_value="old_name",
        expected_values=("new_name", "old_name"),
    ),
    P41RawPlainSiteCase(
        case_id="raw-with-column-renamed-new",
        site=":1197",
        what="withColumnRenamed new name",
        source='df_transformed = df.withColumnRenamed("old_name", r"new_name")',
        raw_source_text='r"new_name"',
        raw_body="new_name",
        expected_site_value="new_name",
        expected_values=("new_name", "old_name"),
    ),
    # ── §5.1 row 12 — via `_column_name`. ──
    P41RawPlainSiteCase(
        case_id="raw-drop-column",
        site=":1202",
        what="drop column name",
        source='df_transformed = df.drop(r"plain")',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("plain",),
    ),
    # ── §5.1 row 13 — via `_column_name`, and reached ONLY through
    # `_filter_mask`: in filter position `_filter_mask` reads the `col(...)`
    # node itself at `:1222` and never routes it through `:814`. `isNotNull` is
    # the predicate deliberately — an `rlike` predicate would also put its
    # PATTERN through `:1232`, which is task 2.1's site, and the case would stop
    # isolating this one. ──
    P41RawPlainSiteCase(
        case_id="raw-filter-col-name",
        site=":1222",
        what="filter's col name",
        source='df_transformed = df.filter(col(r"plain").isNotNull())',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("plain",),
    ),
    # ── §5.1 row 14 — via `_column_name`. The all-string form requires EVERY
    # argument to be a string constant, which is what selects `:1248` over
    # `:1254`. ──
    P41RawPlainSiteCase(
        case_id="raw-select-all-string-form",
        site=":1248",
        what="select all-string form",
        source='df_transformed = df.select(r"plain", "other")',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("other", "plain"),
    ),
    # ── §5.1 row 15 — via `_column_name`. A MIXED argument list is what routes
    # to `:1254` instead of `:1248`: one string constant plus one expression
    # whose output name resolves through `_SELECT_RENAME_SUFFIX`. ──
    P41RawPlainSiteCase(
        case_id="raw-select-string-argument",
        site=":1254",
        what="select string argument (mixed arg list)",
        source='df_transformed = df.select(r"plain", col("a").alias("renamed"))',
        raw_source_text='r"plain"',
        raw_body="plain",
        expected_site_value="plain",
        expected_values=("a", "plain", "plain", "renamed", "renamed"),
    ),
]

#: The plain-fence sites `design.md` §5.1 enumerates, minus the two raw-fence
#: ones. The audit below asserts the case list covers exactly these.
P41_PLAIN_FENCE_SITES = (
    ":814",
    ":846",
    ":852",
    ":915",
    ":961",
    ":1174",
    ":1196",
    ":1197",
    ":1202",
    ":1222",
    ":1248",
    ":1254",
)

#: `design.md` §5.1's two RAW-fence sites. Task 2.1's file, not this one — and
#: the reason this file discriminates Q5's rejected arm.
P41_RAW_FENCE_SITES = (":845", ":1232")

#: §5.1 row 6 — the one site with no fence. Value only, no wrap at task 4.3.
P41_NO_FENCE_SITE = ":985"

P41_CASES_BY_ID = {case.case_id: case for case in P41_RAW_PLAIN_SITE_CASES}


def p41_ids(cases: "list[P41RawPlainSiteCase]") -> "list[str]":
    return [f"{case.site}-{case.case_id}" for case in cases]


# ══════════════════════════════════════════════════════════════════════
# EXPECTED RED until task 4.3. The failure mode is a REFUSAL, not a text
# mismatch — `ConversionError` raised where conversion must succeed.
# ══════════════════════════════════════════════════════════════════════


class TestRawLiteralAtAPlainFenceSiteConverts:
    """2.1: a literal is not refused for carrying an `r` prefix.

    The first half of the claim, and on its own it is **not enough** — a test
    asserting only "did not raise" passes on D3 (`bugfix.md` 1.20), which is
    why every case is also asserted over its emitted VALUES below.
    """

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_conversion_succeeds(self, case: P41RawPlainSiteCase) -> None:
        try:
            p41_emitted_body(case.source)
        except ConversionError as exc:  # pragma: no cover - the expected red until 4.3
            pytest.fail(
                f"site {case.site} ({case.what}) refused a raw literal: construct={exc.construct!r} "
                f"reason={exc.reason!r} :: {exc}\n"
                f"2.1: for a raw literal the source text IS the value, so {case.raw_source_text} denotes "
                f"{case.raw_body!r} exactly as the plain spelling does."
            )

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_emitted_pandas_parses(self, case: P41RawPlainSiteCase) -> None:
        """A fence artefact shows up here before it shows up as a wrong value.

        Stripping the `r` from the SOURCE TEXT rather than taking the value
        yields `"plain` for `r"plain"` — one unbalanced quote — and emits
        `df[""plain"]`, which is a `SyntaxError` in the generated pandas rather
        than a wrong answer. This assertion is what tells those two apart.
        """
        body = p41_emitted_body(case.source)
        ast.parse(body)  # raises SyntaxError if the fence was built from source text


class TestEmittedValueIsTheRawLiteralBody:
    """2.1 + Property 1: the value survives, asserted as a value.

    `col(r"plain")` → `df["plain"]`, **not** `df["r\\"plain\\""]` and not a
    refusal (`tasks.md` 2.2). Stated over `ast`-resolved values rather than over
    emitted text, because a claim about text cannot survive a fence change and
    would pass on D3.
    """

    @pytest.mark.parametrize(
        "case",
        [c for c in P41_RAW_PLAIN_SITE_CASES if c.expected_site_value is not None],
        ids=p41_ids([c for c in P41_RAW_PLAIN_SITE_CASES if c.expected_site_value is not None]),
    )
    def test_site_value_is_the_raw_body(self, case: P41RawPlainSiteCase) -> None:
        values = p41_emitted_string_values(p41_emitted_body(case.source))
        assert case.expected_site_value in values, (
            f"site {case.site} ({case.what}): expected the emitted value "
            f"{case.expected_site_value!r}, got values {sorted(values)!r}"
        )

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_emitted_values_are_exactly_the_expected_multiset(self, case: P41RawPlainSiteCase) -> None:
        """Nothing extra, nothing dropped, nothing garbled — at every site at once."""
        values = p41_emitted_string_values(p41_emitted_body(case.source))
        assert sorted(values) == sorted(case.expected_values), (
            f"site {case.site} ({case.what}): emitted values {sorted(values)!r} != "
            f"expected {sorted(case.expected_values)!r}"
        )

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_the_source_text_spelling_is_never_an_emitted_value(self, case: P41RawPlainSiteCase) -> None:
        """The naive-relaxation guard, stated positively.

        A fix that accepts the `r` prefix but keeps returning SOURCE TEXT would
        put the spelling itself into the emission. No emitted value may be the
        literal's source text, and none may carry the fence quote at all.
        """
        values = p41_emitted_string_values(p41_emitted_body(case.source))
        assert case.raw_source_text not in values, (
            f"site {case.site} ({case.what}): the literal's SOURCE TEXT {case.raw_source_text!r} became an "
            f"emitted value — `_string` is returning text where 2.1 requires the value"
        )
        assert not [v for v in values if '"' in v], (
            f"site {case.site} ({case.what}): an emitted value carries the fence quote: {sorted(values)!r}"
        )


class TestCastStringTargetKeyNeedsTheValueOnly:
    """§5.1 row 6 — `:985` has no fence, so it needs the value and nothing else.

    The value is a **dict key** into `_CAST_TARGETS`; it selects the emission
    rather than appearing in it. Two consequences, both asserted:

    * a raw target spelling must resolve exactly as its plain twin does, and
    * **task 4.3 must NOT add a fence wrap at `:985`.** `tasks.md` 4.3 says so
      and `tasks.md` 11's diff review checks the symptom: a wrap here would
      move the eight `P40_UNPINNED_CAST_KEY_PINS`
      (`test_converter_equivalence_baseline.py:1185`), which must stay
      **byte-exact**. This class is where that instruction is asserted rather
      than only written down.
    """

    CASE = P41_CASES_BY_ID["raw-cast-string-target-key"]

    def test_raw_cast_target_resolves_through_cast_targets(self) -> None:
        values = p41_emitted_string_values(p41_emitted_body(self.CASE.source))
        assert "Int64" in values, (
            f"site {P41_NO_FENCE_SITE}: the raw cast target {self.CASE.raw_source_text} did not resolve — "
            f"`_CAST_TARGETS[{self.CASE.raw_body!r}]` emits `Int64`; got {sorted(values)!r}"
        )

    def test_the_cast_key_itself_is_never_emitted(self) -> None:
        """No wrap at `:985`: the key selects an emission, it is not one.

        `int` must not appear as an emitted value. It appearing would mean the
        key had been routed into a fence — the wrap task 4.3 is forbidden from
        adding, whose other symptom is a moved `P40_UNPINNED_CAST_KEY_PINS`.
        """
        values = p41_emitted_string_values(p41_emitted_body(self.CASE.source))
        assert self.CASE.raw_body not in values, (
            f"site {P41_NO_FENCE_SITE}: the cast KEY {self.CASE.raw_body!r} reached the emission — "
            f"`:985` is a dict lookup and must carry no fence wrap; got {sorted(values)!r}"
        )

    def test_the_chosen_key_distinguishes_resolution_from_coincidence(self) -> None:
        """`int` rather than `string`, and this is why.

        `_CAST_TARGETS["string"]` emits the dtype spelling `"string"` — the same
        text as the key — so a `string` case would pass whether the key had been
        resolved or merely echoed. `int` emits `Int64`, which the key cannot
        produce by accident.
        """
        assert _CAST_TARGETS[self.CASE.raw_body] == ("numeric", '.astype("Int64")')
        assert _CAST_TARGETS["string"] == ("astype", '"string"')


# ══════════════════════════════════════════════════════════════════════
# GREEN TODAY. These two classes are the premise of every red above, made
# mechanical: they establish that the ONLY difference between a case and
# its twin is the `r` prefix, so no multiset above is a transcription
# anyone has to trust.
# ══════════════════════════════════════════════════════════════════════


class TestPlainTwinIsTheSameValue:
    """The plain twin converts today, and to exactly what this file expects.

    **GREEN from the start**, and that is the point: it is what makes the reds
    above legible. If a `expected_values` multiset above were wrong, this class
    would catch it **now**, against the unfixed converter, instead of the error
    surfacing as a spurious red after task 4.3.

    Note the provenance split honestly: this class reads current output, so for
    the *surrounding* literals it is a preservation pin of the kind task 3.1
    owns. It is here rather than there because its subject is the twin
    relationship, not the site.
    """

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_plain_twin_converts(self, case: P41RawPlainSiteCase) -> None:
        twin = p41_plain_twin(case.source, case.raw_source_text)
        p41_emitted_body(twin)  # raises ConversionError if the twin does not convert

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_plain_twin_emits_the_expected_values(self, case: P41RawPlainSiteCase) -> None:
        twin = p41_plain_twin(case.source, case.raw_source_text)
        values = p41_emitted_string_values(p41_emitted_body(twin))
        assert sorted(values) == sorted(case.expected_values), (
            f"site {case.site} ({case.what}): the multiset this file expects for the RAW spelling does not "
            f"match what the PLAIN twin emits today — {sorted(case.expected_values)!r} vs {sorted(values)!r}. "
            f"Fix the expectation, not the converter: the twin is the preserved shape."
        )

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_every_body_is_backslash_free_so_text_equals_value(self, case: P41RawPlainSiteCase) -> None:
        """The premise, asserted rather than assumed.

        Dropping the `r` is value-preserving only for a body with no backslash
        in it. Every case in this file is deliberately in that class
        (`bugfix.md` 1.2: the trigger is the prefix alone). Raw bodies that DO
        carry an escape are D5 and arrive at task 6.1.
        """
        assert "\\" not in case.raw_body, case
        assert case.raw_source_text == f'r"{case.raw_body}"', case
        # Python agrees the two spellings denote the same value.
        assert ast.literal_eval(case.raw_source_text) == ast.literal_eval(f'"{case.raw_body}"') == case.raw_body


class TestRawAndPlainTwinConvertIdentically:
    """2.1, stated without any hand-written expectation at all.

    Two spellings of the same value must convert to the same pandas. This is the
    whole of D1 in one assertion, and it is the assertion that cannot be got
    wrong by transcription — there is nothing transcribed.

    **EXPECTED RED until task 4.3**, with the raw half raising `ConversionError`.
    """

    @pytest.mark.parametrize("case", P41_RAW_PLAIN_SITE_CASES, ids=p41_ids(P41_RAW_PLAIN_SITE_CASES))
    def test_raw_and_plain_twin_emit_identical_pandas(self, case: P41RawPlainSiteCase) -> None:
        twin = p41_plain_twin(case.source, case.raw_source_text)
        expected = p41_emitted_body(twin)
        try:
            actual = p41_emitted_body(case.source)
        except ConversionError as exc:  # pragma: no cover - the expected red until 4.3
            pytest.fail(
                f"site {case.site} ({case.what}): the plain twin converts to {expected!r} but the raw "
                f"spelling refused — construct={exc.construct!r} :: {exc}\n"
                f"Both spell the value {case.raw_body!r}."
            )
        assert actual == expected, (
            f"site {case.site} ({case.what}): raw and plain spellings of {case.raw_body!r} diverged\n"
            f"  raw   : {actual!r}\n  plain : {expected!r}"
        )


# ══════════════════════════════════════════════════════════════════════
# The audit — what makes this file checkable against `design.md` §5.1
# rather than merely plausible.
# ══════════════════════════════════════════════════════════════════════


class TestThisFileIsTheQ5Discriminator:
    """§9 Q5's argument, made checkable in CI.

    Q5's **rejected** arm escapes at only the two `r"…"` emission sites and
    leaves `_string` returning source text. That arm passes every case in
    `test_raw_literal_regex_fidelity.py` (task 2.1) and fails every case here,
    because none of these sites is a raw fence. Asserting that structurally is
    what stops the two files drifting into each other and quietly losing the
    discrimination.
    """

    def test_no_case_in_this_file_is_a_raw_fence_site(self) -> None:
        raw_fence_cases = [c for c in P41_RAW_PLAIN_SITE_CASES if c.site in P41_RAW_FENCE_SITES]
        assert raw_fence_cases == [], (
            f"a raw-fence site leaked into the plain-site file, so it no longer discriminates Q5's "
            f"rejected arm: {[c.case_id for c in raw_fence_cases]}"
        )

    def test_every_plain_fence_site_from_design_5_1_has_exactly_one_case(self) -> None:
        covered = sorted(c.site for c in P41_RAW_PLAIN_SITE_CASES if c.site != P41_NO_FENCE_SITE)
        assert covered == sorted(P41_PLAIN_FENCE_SITES), (
            f"the case list no longer matches `design.md` §5.1's plain-fence rows: covered {covered!r}, "
            f"expected {sorted(P41_PLAIN_FENCE_SITES)!r}"
        )

    def test_the_no_fence_cast_key_site_has_a_case(self) -> None:
        assert [c.site for c in P41_RAW_PLAIN_SITE_CASES].count(P41_NO_FENCE_SITE) == 1

    def test_the_thirteen_sites_plus_task_2_1_s_two_are_the_fifteen(self) -> None:
        """§5.1's fan-in accounted for: 12 plain + 1 no-fence + 2 raw = 15."""
        assert len(P41_PLAIN_FENCE_SITES) == 12
        assert len(P41_RAW_FENCE_SITES) == 2
        assert len(P41_PLAIN_FENCE_SITES) + 1 + len(P41_RAW_FENCE_SITES) == 15
        assert len(P41_RAW_PLAIN_SITE_CASES) == 13

    def test_the_string_fan_in_is_still_fifteen_call_sites(self) -> None:
        """`bugfix.md` 3.1's fan-in, audited by COUNT rather than by line number.

        Line numbers drift the moment task 4.3 adds `_dq`/`_raw` at module
        level, so an audit pinned to them would go red for the fix rather than
        for a real change. Counts do not drift: `_string` keeps 8 call sites
        (one of which is `_column_name`'s) and `_column_name` keeps 8, for a
        total reach of 15. A site added or removed changes a count and lands
        here.
        """
        with open(P41_CONVERTER_PATH, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        calls = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        direct_string_calls = calls.count("_string")
        column_name_calls = calls.count("_column_name")
        assert direct_string_calls == 8, f"`_string` call sites moved: {direct_string_calls}"
        assert column_name_calls == 8, f"`_column_name` call sites moved: {column_name_calls}"
        # One of `_string`'s 8 callers IS `_column_name`, so the reach is
        # (8 - 1) direct + 8 via `_column_name` = 15.
        assert (direct_string_calls - 1) + column_name_calls == 15


# ══════════════════════════════════════════════════════════════════════
# TASK 6.1 — D5: a raw literal carrying a RECOGNISED escape, at a
# plain-fence site.
#
# ──────────────────────────────────────────────────────────────────────
# THESE CASES HAVE NO EXPECTED-RED WINDOW, AND THAT IS CORRECT.
# ──────────────────────────────────────────────────────────────────────
#
# **This shape REFUSES on the unfixed converter.** `col(r"a\b")` at HEAD —
# before task 4.3 — hit `_string`'s `r`-prefix fence and raised
# `ConversionError`, exactly as `col(r"plain")` did: the fence looked at the
# literal's first character and never at its body, so a backslash in the body
# changed nothing. Recorded evidence rather than an assertion here, because the
# unfixed converter is no longer in the working tree: `observations.md` task 2.5
# ran all 18 D1 shapes against it and got **18 refusals, 0 conversions**, with
# `x = col(r"plain")` answering `construct='col'`, `reason=None`,
# `classify == not_yet_expressible`.
#
# **Why the missing red window is not a gap.** D5 is a defect of the *naive* D1
# fix, not of HEAD (`design.md` §"Bug Condition — D5", reproduced there against
# a simulated fix: `col(r"a\b")` converting and emitting `df["a\b"]`). There was
# never a version of this system in which these cases could fail for D5's
# reason — at HEAD they refuse, and from task 4.3 they convert correctly — so
# `tasks.md`'s per-defect red rule has nothing to bite on and `tasks.md` says so
# in terms: *"D5 has no red window, and that is correct — do not go looking for
# one."*
#
# **What the cases are for, then.** They are the tripwire on a future revert:
# any change that puts `_string` back to returning SOURCE TEXT, or that drops a
# `_dq` wrap at one of the twelve plain-fence sites, fails **here** — at the
# site where the damage is a silently wrong COLUMN NAME rather than a crash.
# `TestD5NaiveFixWouldCorruptTheColumnName` makes that purpose checkable instead
# of merely documented: it computes what a source-text-returning `_string` would
# have emitted, from this run's own emission, and asserts the two mean different
# things. No production code is monkeypatched to do it.
#
# **§9 Q5 and §9 Q6, both made checkable by this section.** Q5's *rejected* arm
# — escape at only the two `r"…"` regex sites, keep returning source text —
# converts every case here and gets every value wrong; that is Q5's argument in
# CI. Q6 is the other half: `test_scalar_member_emission_prop.py` returns `[]`
# for `col(r"a\b")`, **correctly**, because a wrong column name is not a
# scalar-receiver problem — so the existing generative pair structurally cannot
# see D5. `test_the_p40_scalar_property_is_blind_to_this_defect` asserts that
# blindness rather than asserting the fence, which is why this section has to
# exist at all.
#
# ──────────────────────────────────────────────────────────────────────
# THE SEMANTICS, STATED ONCE — the whole derivation, and no converter in it
# ──────────────────────────────────────────────────────────────────────
#
# * `r"a\b"` denotes `a` `\` `b` — **three characters, of which `\b` is two,
#   backslash and b** — because a raw fence performs no escape processing.
# * `"a\b"` denotes `a` `\x08` — **two characters, of which `\b` is one, a
#   BACKSPACE** — because `\b` is a *recognised* escape in a plain fence. It
#   consumes the `b`, so the length goes 3 → 2. That is a value **corruption**,
#   not a re-spelling.
# * So the emitted column name must be `a\b`, and the emitted LITERAL must be
#   spelled so that it evaluates to that: `_dq` doubles the backslash and the
#   emission is `df["a\\b"]`, whose value is `a\b`. **Every assertion below is
#   over the value, via `ast`** — never over emitted text, and never over "did
#   not raise" (`bugfix.md` 1.20).
#
# Same for `\t`: `r"z\t"` is `z` `\` `t` (three characters), `"z\t"` is `z` TAB
# (two). `\b` and `\t` are chosen because they are recognised escapes whose
# resolution is *invisible* in a column name — a backspace and a tab both read
# as nothing much in a report, which is what makes D5 silent rather than loud.
#
# Contrast with the thirteen D1 cases above, whose bodies are deliberately
# backslash-FREE: there, text and value coincide and the only question is
# whether the `r` prefix is refused. Here they diverge, and the question is
# which of the two the emission carries. Keeping the groups in separate lists is
# what lets `TestPlainTwinIsTheSameValue`'s backslash-free premise and
# `TestThisFileIsTheQ5Discriminator`'s one-case-per-site audit stay true;
# `test_d5_cases_are_disjoint_from_the_d1_case_list` asserts they stay apart.
# ══════════════════════════════════════════════════════════════════════


class P41RawEscapeSiteCase(NamedTuple):
    """One raw literal WITH a recognised escape, at one plain-fence site."""

    case_id: str
    #: The `_string` / `_column_name` call site, as `design.md` §5.1 numbers it.
    #: Documentation only — see `P41RawPlainSiteCase.site` on why the audits
    #: count call sites instead of trusting these numbers.
    site: str
    what: str
    source: str
    #: The raw literal's source text, exactly as spelled in `source`.
    raw_source_text: str
    #: The raw literal's body — and, because the fence is raw, its VALUE.
    raw_body: str
    #: What the SAME body would denote inside a plain `"…"` fence, i.e. what the
    #: naive D1 fix would have made this site mean. Hardcoded so the corruption
    #: is legible in the file; `TestD5OracleIntegrity` checks it against Python's
    #: own literal semantics, so it is not a number anyone has to trust.
    naive_fix_value: str
    #: Every string VALUE the emitted pandas must carry, as a sorted multiset.
    expected_values: "tuple[str, ...]"


P41_D5_ESCAPE_CASES: "list[P41RawEscapeSiteCase]" = [
    # ── §5.1 row 8 — via `_column_name`. `tasks.md` 6.1's worked example. ──
    P41RawEscapeSiteCase(
        case_id="d5-col-backslash-b",
        site=":814",
        what="col column name",
        source=r'x = col(r"a\b")',
        raw_source_text=r'r"a\b"',
        raw_body="a\\b",
        naive_fix_value="a\x08",
        expected_values=("a\\b",),
    ),
    # ── §5.1 row 9 — via `_column_name`. `\t` rather than `\b`, so the section
    # covers two different recognised escapes and does not generalise from one. ──
    P41RawEscapeSiteCase(
        case_id="d5-with-column-backslash-t",
        site=":1174",
        what="withColumn column name",
        source=r'df_transformed = df.withColumn(r"z\t", col("a"))',
        raw_source_text=r'r"z\t"',
        raw_body="z\\t",
        naive_fix_value="z\t",
        expected_values=("a", "z\\t"),
    ),
    # ── §5.1 row 14 — via `_column_name`, reached through `select`, which
    # `tasks.md` 6.1 names explicitly. The all-string form requires every
    # argument to be a string constant, which is what selects `:1248`. ──
    P41RawEscapeSiteCase(
        case_id="d5-select-all-string-form-backslash-b",
        site=":1248",
        what="select all-string form (via _column_name)",
        source=r'df_transformed = df.select(r"a\b", "other")',
        raw_source_text=r'r"a\b"',
        raw_body="a\\b",
        naive_fix_value="a\x08",
        expected_values=("a\\b", "other"),
    ),
]


def p41_d5_ids(cases: "list[P41RawEscapeSiteCase]") -> "list[str]":
    return [f"{case.site}-{case.case_id}" for case in cases]


def p41_plain_fence_spelling(value: str) -> str:
    """The body a plain ``"…"`` fence needs in order to carry *value*.

    Derived from Python's literal semantics and **self-checked by round-trip**,
    not read off the converter: to spell a backslash inside a plain fence you
    double it, and the assertion below is what says so. No production helper is
    imported — `_dq` is the thing under test, and using it here would make the
    comparison in `TestD5NaiveFixWouldCorruptTheColumnName` agree with the
    producer by construction (`LGR-28b4d75-5`).
    """
    spelling = value.replace("\\", "\\\\")
    assert ast.literal_eval('"' + spelling + '"') == value, (value, spelling)
    return spelling


def p41_naive_fix_plain_fence_value(raw_body: str) -> str:
    """What the NAIVE D1 fix would make a plain-fence site mean.

    The naive fix is the one `design.md` §"Bug Condition — D5" reproduces
    against a simulated implementation: recognise the `r` prefix, strip it from
    the SOURCE TEXT, and keep returning text. The site then embeds that text in
    its plain fence without escaping it, so the literal the customer's pandas
    carries is ``"<raw body>"`` — and what that literal *denotes* is this.
    """
    value = ast.literal_eval('"' + raw_body + '"')
    assert isinstance(value, str)
    return value


def p41_naive_fix_emission(body: str, raw_body: str) -> str:
    """*body* rewritten as the naive fix would have emitted it.

    Takes THIS run's real emission and unescapes the one literal at issue —
    replacing the correctly-escaped body with the raw body verbatim, which is
    what a source-text-returning `_string` would have handed the fence. The
    result is still parseable pandas; it just means something else, which is the
    entire point of D5 and the reason it is a *silent* defect.
    """
    correct = '"' + p41_plain_fence_spelling(raw_body) + '"'
    assert correct in body, (
        f"the escaped literal {correct!r} is not in the emission {body!r} — either the fence changed or "
        f"the value never reached it, and either way the naive-fix comparison below would be vacuous"
    )
    return body.replace(correct, '"' + raw_body + '"')


class TestD5EscapedRawBodyKeepsItsBackslash:
    """2.1 + 2.12 + Property 1: a recognised escape in a raw body survives.

    **GREEN from the first run by construction**, because the value contract
    (task 4.3) *prevents* D5 rather than fixing it — `_string` returns the value
    and `_column_name` escapes it for the plain fence it emits into. A red here
    is a real finding about that contract, not a test to adjust.
    """

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_conversion_succeeds(self, case: P41RawEscapeSiteCase) -> None:
        try:
            p41_emitted_body(case.source)
        except ConversionError as exc:  # pragma: no cover - not reachable after task 4.3
            pytest.fail(
                f"site {case.site} ({case.what}) refused {case.raw_source_text}: "
                f"construct={exc.construct!r} reason={exc.reason!r} :: {exc}\n"
                f"2.1: a raw body carrying an escape is still just a value — {case.raw_body!r}."
            )

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_emitted_pandas_parses(self, case: P41RawEscapeSiteCase) -> None:
        ast.parse(p41_emitted_body(case.source))

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_emitted_value_is_the_raw_body_and_not_the_resolved_escape(self, case: P41RawEscapeSiteCase) -> None:
        """The D5 assertion itself, stated over values.

        `col(r"a\\b")` must name the column `a\\b` — backslash, b — and must not
        name it `a\\x08`, the backspace a plain fence resolves `"a\\b"` to.
        """
        values = p41_emitted_string_values(p41_emitted_body(case.source))
        assert case.raw_body in values, (
            f"site {case.site} ({case.what}): the raw body {case.raw_body!r} is not an emitted value; "
            f"got {sorted(values)!r}"
        )
        assert case.naive_fix_value not in values, (
            f"site {case.site} ({case.what}): the emission carries {case.naive_fix_value!r} — the escape was "
            f"RESOLVED, so this site is emitting source text into a plain fence. That is D5: a silently wrong "
            f"column name. Expected the value {case.raw_body!r}; got {sorted(values)!r}"
        )

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_emitted_values_are_exactly_the_expected_multiset(self, case: P41RawEscapeSiteCase) -> None:
        values = p41_emitted_string_values(p41_emitted_body(case.source))
        assert sorted(values) == sorted(case.expected_values), (
            f"site {case.site} ({case.what}): emitted values {sorted(values)!r} != "
            f"expected {sorted(case.expected_values)!r}"
        )


class TestD5NaiveFixWouldCorruptTheColumnName:
    """The section's PURPOSE, made checkable rather than only documented.

    `tasks.md` 6.1: these cases exist so that *a future revert to source text
    fails here*. An assertion that only says "the value is right today" does not
    demonstrate that; it passes for any number of reasons. So this class
    computes what the naive fix — recognise the `r` prefix, keep returning
    SOURCE TEXT — would have emitted, **from this run's own emission**, and
    asserts the two emissions mean different things.

    **No production code is monkeypatched and no production helper is imported
    to do it.** The naive emission is derived by unescaping one literal in text
    that the converter already produced, and its meaning is read with
    `ast.literal_eval`. If the value contract is ever reverted, the real
    emission becomes the naive one and
    `test_the_real_emission_and_the_naive_emission_disagree` fails on the
    identity.
    """

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_naive_fix_value_differs_from_the_raw_body(self, case: P41RawEscapeSiteCase) -> None:
        """Non-vacuity: each body really is a D5 shape.

        A body whose escape Python does not recognise — `\\.`, `\\d`, `\\w`,
        `\\s` — has value equal to text, so the naive fix would have been
        harmless there and the case would prove nothing. That is exactly why D3
        needs a doubled backslash and why 0 of 56 measured generated versions hit
        it. These three bodies carry RECOGNISED escapes, asserted here.
        """
        assert p41_naive_fix_plain_fence_value(case.raw_body) != case.raw_body, (
            f"{case.case_id}: {case.raw_body!r} means the same thing in both fences, so this case cannot "
            f"discriminate the naive fix from the value contract"
        )

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_resolved_escape_is_shorter_so_it_is_corruption_not_respelling(
        self, case: P41RawEscapeSiteCase
    ) -> None:
        """`\\b` consumes the `b`: 3 characters become 2.

        The length change is what makes this a value corruption rather than a
        different spelling of one value — the distinction the whole spec turns
        on (`design.md` §"Hypothesized Root Cause": source text is
        value-preserving only under an *identical* fence).
        """
        naive = p41_naive_fix_plain_fence_value(case.raw_body)
        assert len(naive) < len(case.raw_body), (case.raw_body, naive)

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_real_emission_and_the_naive_emission_disagree(self, case: P41RawEscapeSiteCase) -> None:
        """The tripwire, asserted over the two emissions' VALUES.

        Both emissions parse; they simply name different columns. A revert to
        source text collapses the real emission onto the naive one, and this
        assertion is what notices.
        """
        real_body = p41_emitted_body(case.source)
        naive_body = p41_naive_fix_emission(real_body, case.raw_body)
        assert naive_body != real_body, (
            f"site {case.site}: the naive-fix emission is IDENTICAL to the real one — `_string` is returning "
            f"source text at this site, which is D5. Emission: {real_body!r}"
        )
        ast.parse(naive_body)  # the naive fix emits parseable pandas; that is why D5 is silent
        real_values = p41_emitted_string_values(real_body)
        naive_values = p41_emitted_string_values(naive_body)
        assert case.raw_body in real_values and case.raw_body not in naive_values, (
            f"site {case.site}: real values {sorted(real_values)!r}, naive values {sorted(naive_values)!r}"
        )
        assert case.naive_fix_value in naive_values and case.naive_fix_value not in real_values, (
            f"site {case.site}: the naive fix would have named the column {case.naive_fix_value!r} "
            f"(from {sorted(naive_values)!r}) instead of {case.raw_body!r} (from {sorted(real_values)!r})"
        )

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_p40_scalar_property_is_blind_to_this_defect(self, case: P41RawEscapeSiteCase) -> None:
        """§9 Q6, asserted: the existing generative pair cannot see a wrong name.

        `test_scalar_member_emission_prop.py`'s detector returns `[]` for both
        the correct emission and the D5-corrupted one — **correctly**, because a
        wrong column name is not a scalar-receiver problem. So the p40 fence
        stays green either way, which is why the assertions above have to exist
        somewhere. Nothing here amends that fence; its detector is called
        read-only, and it is imported inside this test so that a change over
        there cannot break this file's collection.
        """
        from .test_scalar_member_emission_prop import series_only_members_on_scalars

        real_body = p41_emitted_body(case.source)
        naive_body = p41_naive_fix_emission(real_body, case.raw_body)
        assert series_only_members_on_scalars(real_body) == []
        assert series_only_members_on_scalars(naive_body) == [], (
            "p40's detector reported an offender for the D5 emission — if that is now true, §9 Q6's "
            "reasoning has changed and this section's argument needs re-deriving"
        )


class TestD5OracleIntegrity:
    """The declarations above, checked against Python rather than trusted.

    No converter call anywhere in this class: it audits the case table itself,
    so a mistyped `naive_fix_value` or a body that silently stopped being a D5
    shape lands here instead of as a confusing red elsewhere.
    """

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_declared_naive_value_is_what_python_says(self, case: P41RawEscapeSiteCase) -> None:
        assert p41_naive_fix_plain_fence_value(case.raw_body) == case.naive_fix_value

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_the_source_text_is_a_raw_fence_whose_body_is_its_value(self, case: P41RawEscapeSiteCase) -> None:
        assert case.raw_source_text == f'r"{case.raw_body}"', case
        assert ast.literal_eval(case.raw_source_text) == case.raw_body, case
        assert case.source.count(case.raw_source_text) == 1, case

    @pytest.mark.parametrize("case", P41_D5_ESCAPE_CASES, ids=p41_d5_ids(P41_D5_ESCAPE_CASES))
    def test_every_d5_body_carries_a_backslash(self, case: P41RawEscapeSiteCase) -> None:
        """The inverse of the D1 premise, and the reason for two lists.

        `TestPlainTwinIsTheSameValue` asserts every D1 body is backslash-FREE.
        Every body here carries one, so the two groups partition cleanly and
        neither class's premise is weakened by the other's cases.
        """
        assert "\\" in case.raw_body, case

    def test_d5_cases_are_disjoint_from_the_d1_case_list(self) -> None:
        """The D1 audits keep their exact counts.

        `TestThisFileIsTheQ5Discriminator` pins 13 D1 cases and one per §5.1
        plain-fence row; a D5 case leaking into that list would break both the
        count and the one-per-site rule. Adding to `P41_D5_ESCAPE_CASES` is
        always the right move.
        """
        d1_ids = {case.case_id for case in P41_RAW_PLAIN_SITE_CASES}
        d5_ids = {case.case_id for case in P41_D5_ESCAPE_CASES}
        assert d1_ids & d5_ids == set()
        assert len(P41_RAW_PLAIN_SITE_CASES) == 13
        assert all("\\" not in case.raw_body for case in P41_RAW_PLAIN_SITE_CASES)

    def test_every_d5_site_is_a_plain_fence_site(self) -> None:
        """Not a raw fence, and not the no-fence cast key.

        D5 is a *plain*-fence defect: the corruption is the plain fence
        resolving an escape the raw source never had one for. A raw-fence site
        (`:845`, `:1232`) cannot exhibit it — `_raw` keeps the backslash — and
        `:985` emits nothing at all. Escape fidelity at the two raw-fence sites
        is D3's, in `test_raw_literal_regex_fidelity.py` (task 7.1).
        """
        for case in P41_D5_ESCAPE_CASES:
            assert case.site in P41_PLAIN_FENCE_SITES, case
            assert case.site not in P41_RAW_FENCE_SITES, case
            assert case.site != P41_NO_FENCE_SITE, case
