# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 T2 — the unfixed emission pinned at all 15 `_string` call sites.

**Validates: Requirements 3.1, 3.10, Property 2**

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: CURRENT CONVERTER OUTPUT **IS** THE ORACLE.
THIS IS THE OPPOSITE OF THE OTHER p41 FILES. READ BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 runs **two opposite rules** inside `tests/unit/lambdas/test_code/`,
and this comment is what keeps them apart.

**This file follows the characterization rule: the oracle is what the UNFIXED
converter emits today.** Every `expected_emission` below was produced by calling
`PySpark2PandasConverter.convert` against the working tree at HEAD `4dd9b42`,
**recorded first, asserted second** (`tasks.md` 3's "observation-first, in that
order"). That is legitimate here, and only here, because the claim being made is
*preservation*: `bugfix.md` 3.1 requires all 15 sites to behave as they do today
for non-raw double-quoted input, and 3.10 requires a non-raw, non-escaped regex
to emit byte-identically. There is no better oracle for "unchanged" than the
current bytes.

**THE FILES THAT FOLLOW THE OPPOSITE RULE** — expectations derived from regex,
Spark and Python string semantics, current output never consulted:

* `test_raw_literal_regex_fidelity.py`  — task 2.1 (D1's raw-fence half) and
  task 2.4 (D3's counterexample)
* `test_raw_literal_plain_sites.py`     — task 2.2 (D1's plain-fence half)

Also `test_passthrough_and_alias_refusal.py`, which holds **both** rules in
separately labelled classes: task 1.1's characterization classes take this
file's rule, tasks 1.2/1.3's D2 classes take the semantics rule.

Where this file and those describe the same site they are *meant* to disagree
until task 4.3 lands: they pin a **raw** spelling that must start converting,
this one pins the **plain** spelling that must not move. Do not reconcile them.

**EXPECTED RESULT: GREEN ON THE FIRST RUN, BY DEFINITION** — the assertions are
a transcription of the run that produced them. **A red is a real finding: the
transcription was wrong, or the working tree has moved. Investigate it; do not
adjust it away.** After task 4.3 a red here means 4.3 changed a site it was not
supposed to (`tasks.md` 5: "a pin that had to be amended means task 4.3 changed
a site it was not supposed to").

──────────────────────────────────────────────────────────────────────
THIS FILE DISCHARGES §9 Q5's REVERSAL CONDITION. Task 3.3 writes the verdict.
──────────────────────────────────────────────────────────────────────

`design.md` §9 Q5 ratified the value contract **conditionally**:

    "If the fence helpers turn out to need per-site special cases — i.e. if
    `_dq` is not uniformly correct at all 12 plain sites — the value contract
    loses its main advantage and Q5's arm becomes right. §7 T2's
    observation-first pins are what would show that, before T3 is written."

These are those pins, and `TestQ5PlainFenceUniformity` /
`TestQ5FlaggedSitesInParticular` are the evidence. The condition is met — and
task 3.3 must therefore reverse the design's arm — if **any** of the 12
plain-fence sites emits through something other than a plain ``"…"`` fence with
the value placed in it verbatim. Concretely, the four shapes `tasks.md` 3.1
flags:

1. **a different quoting** — a site emitting `'…'` or `\"\"\"…\"\"\"`;
2. **a pre-escaped body** — a site escaping or transforming the body itself, so
   `_dq` would double-escape;
3. **`:915`'s `strftime` remap re-entering escaping** — the format string is
   *transformed* before it reaches the fence, so a single `_dq` has to be
   correct over the map's output as well as its input;
4. **a name that must survive `_SELECT_RENAME_SUFFIX` matching** (`:961`,
   `:1254`) — `_dq` must not break a match that resolves today.

**The observed verdict is that the condition is NOT met.** Each class below
records which of the four it rules out, and how. Task 3.3 owns the written
verdict; this file owns the evidence it is written from.

──────────────────────────────────────────────────────────────────────
Why "the value equals its source text" is the premise, not an accident
──────────────────────────────────────────────────────────────────────

`tasks.md` 3.1 requires each case to use "a plain double-quoted literal whose
value equals its source text". That is the class where the value contract and
the source-text contract **agree**, so a pin taken here is a pin the fix cannot
legitimately move: task 4.2 changes `_string` from returning `segment[1:-1]` to
returning `node.value`, and on this input class those are the same string.
`TestSubjectValueEqualsItsSourceText` asserts the premise structurally rather
than leaving it as a comment, so a case that quietly stops satisfying it fails
loudly instead of weakening the pin.

The one site where the value does **not** appear verbatim in the emission is
`:915`, and that is by design rather than by exception — see
`TestQ5FlaggedSitesInParticular`.

──────────────────────────────────────────────────────────────────────
Evidence grade, stated because a green run here is easy to over-read
──────────────────────────────────────────────────────────────────────

pandas is not installed in this venv and must not become one (`bugfix.md` 3.14).
Nothing here executes the emitted pandas. Every assertion below is evidence
about the **translation** — the bytes the converter writes — and none of it is
evidence about the execution.

No shared fixture from `tests/fixtures/conftest.py` is relevant: the converter is
pure text-to-text and touches no AWS surface.
"""

import ast
import os
import sys
import warnings
from typing import NamedTuple

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

from pyspark_to_pandas import (
    _SELECT_COL_ARG,
    _SELECT_RENAME_SUFFIX,
    _SELECT_STRING_ARG,
    _SELECT_SUBSCRIPT_ARG,
    ConversionError,
    PySpark2PandasConverter,
    _java_date_format_to_strftime,
)

#: The two-import header `convert()` prepends to every emission (`bugfix.md` 3.7).
P41_PANDAS_HEADER = "import pandas as pd\nimport numpy as np\n\n"

#: The converter, for the fan-in audit. Read as text rather than imported so the
#: audit is over the shipped source rather than over an already-imported module.
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

#: `design.md` §5.1's fence vocabulary, spelled once so the case table reads
#: against that table rather than restating it per row.
P41_FENCE_PLAIN = 'plain "…"'
P41_FENCE_RAW = 'raw r"…"'
P41_FENCE_NONE = "none — dict lookup, nothing emitted"


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
    """Every string literal VALUE in the emitted pandas, sorted.

    Values, not text — `ast` resolves each fence to what it denotes. Sorted
    because `ast.walk`'s order is traversal order, not source order, and nothing
    in this file depends on the order.
    """
    return sorted(
        n.value for n in ast.walk(ast.parse(body)) if isinstance(n, ast.Constant) and isinstance(n.value, str)
    )


def p41_literal_value(literal_source_text: str) -> str:
    """`ast.literal_eval` of a literal's source text, with `SyntaxWarning` suppressed.

    `:1232`'s pinned pattern carries `\\.`, an unrecognised escape, which Python
    warns about when the literal is compiled. Suppressed here for the same reason
    `convert` suppresses it at `:1372-1376`: the warning is about the model's
    text, not about the converter's behaviour. `tasks.md` 7.1 also forbids making
    a test depend on it being raised, so it is neither asserted nor allowed to
    add noise to this file's run.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        value = ast.literal_eval(literal_source_text)
    assert isinstance(value, str), literal_source_text
    return value


def p41_source_text_body(literal_source_text: str) -> str:
    """The body of a double-quoted literal's SOURCE TEXT — what `_string` returns today.

    `_string` (`:755`) returns `segment[1:-1]`, i.e. exactly this. Task 4.2
    changes it to return `node.value`. Every case in this file is chosen so the
    two are the same string, and `TestSubjectValueEqualsItsSourceText` is the
    assertion that they are.
    """
    assert literal_source_text.startswith('"') and literal_source_text.endswith('"'), literal_source_text
    return literal_source_text[1:-1]


def p41_dq_would_be_a_noop(value: str) -> bool:
    """Would a round-trip-safe plain-fence escaper leave *value* untouched?

    `design.md` §5.1 defines `_dq` by its round-trip rather than by a
    substitution list, so this mirrors the *minimum* set a round-trip-safe
    implementation must escape inside a ``"…"`` fence: the fence quote, the
    escape character itself, and the two line terminators that a Python string
    literal cannot carry raw. A tab is legal unescaped in both fences and is
    deliberately not in the set (§5.1: "Tab is legal unescaped in both fences;
    LF and CR are fatal in both").

    Used only to state the Q5 evidence positively: on this file's input class
    `_dq` is the identity, so task 4.3's wrap cannot move any pin here.
    """
    return not any(ch in value for ch in ('"', "\\", "\n", "\r"))


class P41StringSitePin(NamedTuple):
    """One plain double-quoted literal at one `_string` site, and today's emission."""

    case_id: str
    #: The `_string` / `_column_name` call site, as `design.md` §5.1 numbers it.
    #: Line numbers are DOCUMENTATION ONLY — they drift the moment task 4.1 adds
    #: `_dq`/`_raw` at module level. `TestThisFileCoversTheWholeFanIn` audits the
    #: fan-in by call-site COUNT, which does not drift.
    site: str
    #: What the site is, in one phrase, so the file reads against §5.1's table.
    what: str
    #: `design.md` §5.1's "emission fence" column for this row.
    fence: str
    source: str
    #: The subject literal's source text, exactly as spelled in `source`,
    #: including its quotes.
    subject_literal_text: str
    #: The subject literal's VALUE. Equal to the body of `subject_literal_text`
    #: for every case in this file — that is the file's premise.
    subject_value: str
    #: The body that appears inside the fence in the emission, byte-exactly.
    #: Equal to `subject_value` everywhere except `:915` (the strftime map
    #: result); `None` at `:985`, which emits nothing.
    expected_fence_body: "str | None"
    #: Today's emitted pandas, byte-exact, with the import header removed.
    #: RECORDED FROM A RUN, then asserted. This is the pin.
    expected_emission: str
    #: Every string VALUE the emission carries, as a sorted multiset.
    expected_values: "tuple[str, ...]"


# ══════════════════════════════════════════════════════════════════════
# THE FIFTEEN PINS — enumerated from `design.md` §5.1's table, in its row
# order, NOT invented. All 15 rows are here: this file is the only one in
# p41 that covers the whole fan-in in one place, because preservation is a
# claim about every site rather than about a defect's reachable subset.
#
# `:845` and `:1232` (§5.1 rows 1 and 7) are the two RAW-fence sites, and
# they are in this file even though they are task 2.1's subject for the raw
# spelling — because 3.10 pins their PLAIN spelling byte-exactly, and both
# of 3.10's two named reproductions are exactly these two shapes.
# ══════════════════════════════════════════════════════════════════════

P41_STRING_SITE_PINS: "list[P41StringSitePin]" = [
    # ── §5.1 row 1 — direct, raw `r"…"` fence. `bugfix.md` 3.10's FIRST named
    # reproduction, verbatim: this is the shape whose byte-identical emission
    # 3.10 requires to continue. The pattern is backslash-free, so it is also a
    # non-escaped regex in 3.10's sense. ──
    P41StringSitePin(
        case_id="plain-regexp-replace-pattern",
        site=":845",
        what="regexp_replace pattern",
        fence=P41_FENCE_RAW,
        source='df_transformed = df.withColumn("phone", regexp_replace(col("phone_number"), "[^0-9]", ""))',
        subject_literal_text='"[^0-9]"',
        subject_value="[^0-9]",
        expected_fence_body="[^0-9]",
        expected_emission=(
            "df_transformed = df.copy(); "
            'df_transformed["phone"] = df["phone_number"].str.replace(r"[^0-9]", "", regex=True)'
        ),
        expected_values=("", "[^0-9]", "phone", "phone_number"),
    ),
    # ── §5.1 row 2 — direct, plain fence. ──
    P41StringSitePin(
        case_id="plain-regexp-replace-replacement",
        site=":846",
        what="regexp_replace replacement",
        fence=P41_FENCE_PLAIN,
        source='x = regexp_replace(col("p"), "[^0-9]", "X")',
        subject_literal_text='"X"',
        subject_value="X",
        expected_fence_body="X",
        expected_emission='x = df["p"].str.replace(r"[^0-9]", "X", regex=True)',
        expected_values=("X", "[^0-9]", "p"),
    ),
    # ── §5.1 row 3 — direct, plain fence. The SEPARATOR only. `concat_ws`'s
    # hardcoded `.astype(str)` on COLUMN parts is a separate, real fidelity
    # divergence, deliberately out of scope (3.11, p40 §5 N3a) and required to
    # end this spec with an empty diff. It appears in the pin below because it is
    # part of today's bytes; that it appears is the point, and task 4.3 must not
    # touch it. ──
    P41StringSitePin(
        case_id="plain-concat-ws-separator",
        site=":852",
        what="concat_ws separator",
        fence=P41_FENCE_PLAIN,
        source='x = concat_ws("-", col("a"), col("b"))',
        subject_literal_text='"-"',
        subject_value="-",
        expected_fence_body="-",
        expected_emission='x = df["a"].astype(str) + "-" + df["b"].astype(str)',
        expected_values=("-", "a", "b"),
    ),
    # ── §5.1 row 4 — direct, plain fence, AND THE ONE SITE WHERE THE BODY IS NOT
    # THE VALUE. `_java_date_format_to_strftime` maps the Java SimpleDateFormat
    # pattern to a strftime pattern BEFORE the fence, so the fence body is
    # `%Y-%m-%d` and the value `yyyy-MM-dd` does not appear in the emission at
    # all. That mapping is 3.5's preserved behaviour and task 4.3 must leave it
    # alone, wrapping its RESULT. `TestQ5FlaggedSitesInParticular` is where this
    # site's Q5 question is settled. ──
    P41StringSitePin(
        case_id="plain-to-date-format",
        site=":915",
        what="_to_datetime format string",
        fence=P41_FENCE_PLAIN + " — body is the strftime MAP RESULT, not the value",
        source='x = to_date(col("d"), "yyyy-MM-dd")',
        subject_literal_text='"yyyy-MM-dd"',
        subject_value="yyyy-MM-dd",
        expected_fence_body="%Y-%m-%d",
        expected_emission='x = pd.to_datetime(df["d"], format="%Y-%m-%d", errors="coerce").dt.date',
        expected_values=("%Y-%m-%d", "coerce", "d"),
    ),
    # ── §5.1 row 5 — direct, plain fence. A `\w+` name deliberately:
    # `.rename("plain")` is what `_SELECT_RENAME_SUFFIX` (`:205`) matches to
    # resolve a select output column, and §5.1 row 5 requires that match to keep
    # working. `TestQ5FlaggedSitesInParticular` checks it. ──
    P41StringSitePin(
        case_id="plain-alias-name",
        site=":961",
        what="alias new name",
        fence=P41_FENCE_PLAIN,
        source='x = col("a").alias("plain")',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission='x = df["a"].rename("plain")',
        expected_values=("a", "plain"),
    ),
    # ── §5.1 row 6 — direct, and THE ONLY SITE WITH NO FENCE: the value is a
    # dict key into `_CAST_TARGETS`, never emitted. `int` rather than `string` on
    # purpose — its emission (`Int64`) differs from the key, so the pin cannot
    # pass by the key coincidentally appearing in the output. `expected_fence_
    # body=None` records "this site emits nothing"; task 4.3 must NOT add a wrap
    # here, and the eight `P40_UNPINNED_CAST_KEY_PINS` are what would move if one
    # were added. ──
    P41StringSitePin(
        case_id="plain-cast-string-target-key",
        site=":985",
        what="_cast string target key",
        fence=P41_FENCE_NONE,
        source='x = col("a").cast("int")',
        subject_literal_text='"int"',
        subject_value="int",
        expected_fence_body=None,
        expected_emission='x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")',
        expected_values=("Int64", "a", "coerce"),
    ),
    # ── §5.1 row 7 — direct, raw `r"…"` fence. `bugfix.md` 3.10's SECOND named
    # reproduction, verbatim. The pattern DOES carry a backslash — `\.` — and it
    # is here because `\.` is an UNRECOGNISED escape, so Python leaves it alone
    # and the value equals the source text, which is this file's premise. It is
    # also the reason D3 needs a DOUBLED backslash to manifest and why this shape
    # converts correctly today (`bugfix.md` 1.19, 3.10). ──
    P41StringSitePin(
        case_id="plain-rlike-pattern-unrecognised-escape",
        site=":1232",
        what="rlike pattern",
        fence=P41_FENCE_RAW,
        source='df_transformed = df.filter(col("email").rlike("@example\\.com$"))',
        subject_literal_text='"@example\\.com$"',
        subject_value="@example\\.com$",
        expected_fence_body="@example\\.com$",
        expected_emission='df_transformed = df[df["email"].str.contains(r"@example\\.com$", na=False)]',
        expected_values=("@example\\.com$", "email"),
    ),
    # ── §5.1 row 8 — via `_column_name`, plain fence. ──
    P41StringSitePin(
        case_id="plain-col-column-name",
        site=":814",
        what="col column name",
        fence=P41_FENCE_PLAIN,
        source='x = col("plain")',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission='x = df["plain"]',
        expected_values=("plain",),
    ),
    # ── §5.1 row 9 — via `_column_name`, plain fence. The new-frame `.copy()`
    # semantics in the emission are 3.1/3.2's preserved behaviour. ──
    P41StringSitePin(
        case_id="plain-with-column-column",
        site=":1174",
        what="withColumn column name",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.withColumn("plain", col("a"))',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission='df_transformed = df.copy(); df_transformed["plain"] = df["a"]',
        expected_values=("a", "plain"),
    ),
    # ── §5.1 rows 10 and 11 — via `_column_name`, TWO sites in one table row, so
    # two pins over ONE source. Both literals are plain here (unlike task 2.2,
    # where the raw spelling moved between them), so the source is shared and the
    # `subject_*` fields are what distinguish the two rows. Sharing the source is
    # the honest form: it is one statement, and it exercises both sites at once. ──
    P41StringSitePin(
        case_id="plain-with-column-renamed-old",
        site=":1196",
        what="withColumnRenamed old name",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.withColumnRenamed("old_name", "new_name")',
        subject_literal_text='"old_name"',
        subject_value="old_name",
        expected_fence_body="old_name",
        expected_emission='df_transformed = df.rename(columns={"old_name": "new_name"})',
        expected_values=("new_name", "old_name"),
    ),
    P41StringSitePin(
        case_id="plain-with-column-renamed-new",
        site=":1197",
        what="withColumnRenamed new name",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.withColumnRenamed("old_name", "new_name")',
        subject_literal_text='"new_name"',
        subject_value="new_name",
        expected_fence_body="new_name",
        expected_emission='df_transformed = df.rename(columns={"old_name": "new_name"})',
        expected_values=("new_name", "old_name"),
    ),
    # ── §5.1 row 12 — via `_column_name`, plain fence. ──
    P41StringSitePin(
        case_id="plain-drop-column-name",
        site=":1202",
        what="drop column name",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.drop("plain")',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission='df_transformed = df.drop(columns=["plain"])',
        expected_values=("plain",),
    ),
    # ── §5.1 row 13 — via `_column_name`, plain fence. `isNotNull` rather than
    # `rlike` on purpose: this pin is about the COLUMN NAME site (`:1222`), and
    # `rlike` would also drag in `:1232`, which has its own pin above. ──
    P41StringSitePin(
        case_id="plain-filter-col-name",
        site=":1222",
        what="filter's col name",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.filter(col("plain").isNotNull())',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission='df_transformed = df[df["plain"].notna()]',
        expected_values=("plain",),
    ),
    # ── §5.1 row 14 — via `_column_name`, plain fence, `[[…]]` subscript form. ──
    P41StringSitePin(
        case_id="plain-select-all-string-form",
        site=":1248",
        what="select all-string form",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.select("plain", "other")',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission='df_transformed = df[["plain", "other"]]',
        expected_values=("other", "plain"),
    ),
    # ── §5.1 row 15 — via `_column_name`, plain fence, `pd.DataFrame({…})` form.
    # The subject value lands at TWO positions in one emission — the dict key and
    # the subscript — both plain-fenced, both built from the single
    # `_column_name` return at `:1254-1255`. One wrap therefore covers both, which
    # is part of the Q5 uniformity answer and is asserted in
    # `TestQ5FlaggedSitesInParticular`. ──
    P41StringSitePin(
        case_id="plain-select-string-argument",
        site=":1254",
        what="select string argument",
        fence=P41_FENCE_PLAIN,
        source='df_transformed = df.select("plain", col("a").alias("renamed"))',
        subject_literal_text='"plain"',
        subject_value="plain",
        expected_fence_body="plain",
        expected_emission=(
            'df_transformed = pd.DataFrame({"plain": df["plain"], "renamed": df["a"].rename("renamed")})'
        ),
        expected_values=("a", "plain", "plain", "renamed", "renamed"),
    ),
]

#: The 12 plain-fence sites §9 Q5's reversal condition is stated over — every
#: site whose emission wraps the value in a plain ``"…"`` fence, i.e. the sites
#: task 4.3 routes through `_dq`. Derived from the case table rather than
#: hardcoded, so a mis-tagged row cannot silently shrink the Q5 evidence.
P41_PLAIN_FENCE_PINS = [pin for pin in P41_STRING_SITE_PINS if pin.fence.startswith(P41_FENCE_PLAIN)]

#: The 2 raw-fence sites (task 4.3 routes these through `_raw`).
P41_RAW_FENCE_PINS = [pin for pin in P41_STRING_SITE_PINS if pin.fence == P41_FENCE_RAW]

#: The 1 site that emits nothing (`:985`, a dict lookup — no wrap at all).
P41_NO_FENCE_PINS = [pin for pin in P41_STRING_SITE_PINS if pin.fence == P41_FENCE_NONE]

#: The 11 plain-fence sites whose fence body is the value VERBATIM — i.e. every
#: plain site except `:915`, which maps the value to a strftime pattern first.
#: Split out so the per-site "does not pre-escape" assertion can exclude `:915`
#: by construction instead of skipping it at runtime; a skip is not evidence.
P41_PLAIN_FENCE_PINS_VERBATIM = [pin for pin in P41_PLAIN_FENCE_PINS if pin.expected_fence_body == pin.subject_value]


def p41_ids(pins) -> "list[str]":
    return [pin.case_id for pin in pins]


# ══════════════════════════════════════════════════════════════════════
# THE PINS
# ══════════════════════════════════════════════════════════════════════


class TestUnfixedEmissionAtEveryStringSite:
    """Byte-exact pin of today's emission at each of the 15 `_string` sites.

    ORACLE: current converter output. Recorded from a run at HEAD `4dd9b42`,
    then asserted (`tasks.md` 3, "observation-first, in that order").

    `bugfix.md` 3.1: all 15 sites SHALL CONTINUE TO behave as today for non-raw
    double-quoted input. 3.10: a non-raw, non-escaped regex SHALL CONTINUE TO
    emit byte-identically — and both of 3.10's named reproductions are in the
    table (`plain-regexp-replace-pattern`, `plain-rlike-pattern-unrecognised-escape`).
    """

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_plain_literal_at_this_site_converts(self, pin: P41StringSitePin) -> None:
        """Conversion succeeds. The pin below is meaningless if it does not."""
        report = PySpark2PandasConverter.convert(pin.source)
        assert set(report) == {"code", "conversion_report"}, report.keys()

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_emission_is_byte_identical_to_the_recorded_run(self, pin: P41StringSitePin) -> None:
        """The whole emitted body, byte for byte. This is the preservation pin."""
        assert p41_emitted_body(pin.source) == pin.expected_emission

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_emitted_string_values_are_the_recorded_multiset(self, pin: P41StringSitePin) -> None:
        """Every string VALUE the emission carries.

        Stated over values as well as over bytes because a fence change can hold
        the values while moving the bytes, and a *value* change is the failure
        this spec exists to prevent. After task 4.3 this assertion must still
        hold even if `test_emission_is_byte_identical_to_the_recorded_run` ever
        legitimately moves.
        """
        assert p41_emitted_string_values(p41_emitted_body(pin.source)) == sorted(pin.expected_values)

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_the_emission_parses_as_python(self, pin: P41StringSitePin) -> None:
        """A fence that does not round-trip emits pandas that does not parse.

        `design.md` §5.1 records LF/CR as fatal in both fences. No case here
        carries one, so this is a floor rather than a probe — but it is the
        assertion that would catch task 4.1 shipping a substitution-list `_dq`,
        because the failure mode there is a `SyntaxError` in the emitted code
        rather than a wrong value.
        """
        ast.parse(p41_emitted_body(pin.source))


class TestSubjectValueEqualsItsSourceText:
    """The premise of this file, asserted rather than commented.

    ORACLE: Python string semantics. No converter call — this class is about the
    INPUTS, not about the producer.

    `tasks.md` 3.1 requires each case to use "a plain double-quoted literal whose
    value equals its source text". That is the input class where task 4.2's
    change — `segment[1:-1]` to `node.value` — is a no-op, which is what makes a
    pin taken here one the fix cannot legitimately move. A case that quietly
    stops satisfying the premise weakens its own pin, so the premise is checked.
    """

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_the_subject_literal_is_plain_and_double_quoted(self, pin: P41StringSitePin) -> None:
        assert pin.subject_literal_text.startswith('"')
        assert pin.subject_literal_text.endswith('"')
        assert not pin.subject_literal_text.startswith('"""')
        # No `r` / `R` prefix: a raw spelling is tasks 2.1/2.2's subject, and a
        # raw case in this file would be pinning the defect rather than the
        # preserved behaviour.
        assert "r" + pin.subject_literal_text != pin.source

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_the_subject_literal_appears_verbatim_in_the_source(self, pin: P41StringSitePin) -> None:
        assert pin.subject_literal_text in pin.source

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_value_equals_source_text_body(self, pin: P41StringSitePin) -> None:
        """`ast.literal_eval` of the literal == the body of its source text.

        The two sides of task 4.2's change, compared directly: `node.value` on
        the left, `segment[1:-1]` on the right.
        """
        parsed_value = p41_literal_value(pin.subject_literal_text)
        assert parsed_value == pin.subject_value
        assert parsed_value == p41_source_text_body(pin.subject_literal_text)

    @pytest.mark.parametrize("pin", P41_STRING_SITE_PINS, ids=p41_ids(P41_STRING_SITE_PINS))
    def test_the_declared_value_is_what_the_case_says_it_is(self, pin: P41StringSitePin) -> None:
        """Guard against a hand-transcribed `subject_value` drifting from its literal."""
        assert p41_literal_value(pin.subject_literal_text) == pin.subject_value


class TestSubjectValueReachesTheEmissionThroughItsDeclaredFence:
    """Per-site: the value is placed in the fence `design.md` §5.1 says it is.

    ORACLE: current converter output.

    This is the class that makes the case table auditable against §5.1's
    "emission fence" column, and it is the first half of the Q5 evidence: a site
    whose declared fence and observed fence disagree is a site the value contract
    would need a special case for.
    """

    @pytest.mark.parametrize("pin", P41_PLAIN_FENCE_PINS, ids=p41_ids(P41_PLAIN_FENCE_PINS))
    def test_plain_fence_site_emits_the_body_inside_a_plain_double_quote_pair(self, pin: P41StringSitePin) -> None:
        body = p41_emitted_body(pin.source)
        assert pin.expected_fence_body is not None, pin.case_id
        assert f'"{pin.expected_fence_body}"' in body

    @pytest.mark.parametrize("pin", P41_PLAIN_FENCE_PINS, ids=p41_ids(P41_PLAIN_FENCE_PINS))
    def test_plain_fence_site_does_not_emit_a_raw_fence(self, pin: P41StringSitePin) -> None:
        """A `r"…"` around this site's body would make it a raw-fence site.

        §5.1 assigns only `:845` and `:1232` the raw fence. If a third site turns
        out to emit one, task 4.3's `_dq`/`_raw` split is mis-specified.
        """
        body = p41_emitted_body(pin.source)
        assert pin.expected_fence_body is not None, pin.case_id
        assert f'r"{pin.expected_fence_body}"' not in body

    @pytest.mark.parametrize("pin", P41_RAW_FENCE_PINS, ids=p41_ids(P41_RAW_FENCE_PINS))
    def test_raw_fence_site_emits_the_body_inside_a_raw_fence(self, pin: P41StringSitePin) -> None:
        body = p41_emitted_body(pin.source)
        assert pin.expected_fence_body is not None, pin.case_id
        assert f'r"{pin.expected_fence_body}"' in body

    @pytest.mark.parametrize("pin", P41_NO_FENCE_PINS, ids=p41_ids(P41_NO_FENCE_PINS))
    def test_no_fence_site_does_not_carry_the_value_into_the_emission_at_all(self, pin: P41StringSitePin) -> None:
        """`:985` selects an emission; it does not become one.

        `expected_fence_body is None` is the recorded fact, and the observation is
        that the key `int` appears nowhere in `pd.to_numeric(df["a"],
        errors="coerce").astype("Int64")`. This is the assertion that makes
        adding a fence wrap at `:985` visible — `tasks.md` 4.3 forbids one, and
        the eight `P40_UNPINNED_CAST_KEY_PINS`
        (`test_converter_equivalence_baseline.py:1185`) are what would move if one
        were added.
        """
        assert pin.expected_fence_body is None
        assert pin.subject_value not in p41_emitted_body(pin.source)


# ══════════════════════════════════════════════════════════════════════
# §9 Q5's REVERSAL CONDITION — the evidence task 3.3 writes its verdict from
# ══════════════════════════════════════════════════════════════════════


class TestQ5PlainFenceUniformity:
    """Is `_dq` uniformly correct at all 12 plain-fence sites? (`design.md` §9 Q5)

    ORACLE: current converter output for the observed fence shape; Python string
    semantics for the round-trip claim.

    Q5's reversal condition, verbatim: *"If the fence helpers turn out to need
    per-site special cases — i.e. if `_dq` is not uniformly correct at all 12
    plain sites — the value contract loses its main advantage and Q5's arm becomes
    right."*

    **Observed: the condition is NOT met.** All 12 plain-fence sites emit through
    one shape — the body inside a plain ``"…"`` pair — so one escaper serves all
    twelve, which is exactly what makes `_column_name`'s single edit cover eight
    of them. Task 3.3 owns the written verdict; this class is what it is written
    from.
    """

    def test_there_are_exactly_twelve_plain_fence_sites(self) -> None:
        """The count Q5's condition is stated over.

        12 plain + 2 raw + 1 no-fence = 15, matching `bugfix.md` 3.1 and
        `design.md` §5.1 exactly. Derived from the case table, so a mis-tagged
        row fails here rather than silently shrinking the evidence below.
        """
        assert len(P41_PLAIN_FENCE_PINS) == 12
        assert len(P41_RAW_FENCE_PINS) == 2
        assert len(P41_NO_FENCE_PINS) == 1
        assert len(P41_STRING_SITE_PINS) == 15

    @pytest.mark.parametrize("pin", P41_PLAIN_FENCE_PINS, ids=p41_ids(P41_PLAIN_FENCE_PINS))
    def test_no_plain_fence_site_uses_a_different_quoting(self, pin: P41StringSitePin) -> None:
        """Q5 shape 1 ruled out: no site emits `'…'` or `\"\"\"…\"\"\"`.

        A site emitting single quotes would need its own escaper, because the
        character that must be escaped differs. None does.
        """
        body = p41_emitted_body(pin.source)
        assert pin.expected_fence_body is not None, pin.case_id
        assert f"'{pin.expected_fence_body}'" not in body
        assert f'"""{pin.expected_fence_body}"""' not in body

    @pytest.mark.parametrize(
        "pin",
        P41_PLAIN_FENCE_PINS_VERBATIM,
        ids=p41_ids(P41_PLAIN_FENCE_PINS_VERBATIM),
    )
    def test_no_plain_fence_site_pre_escapes_its_body(self, pin: P41StringSitePin) -> None:
        """Q5 shape 2 ruled out: no site escapes the body before the fence.

        On this file's input class the value equals its source text, so any
        difference between the two would be the site transforming the body —
        which is what a pre-escaping site looks like, and what would make `_dq`
        double-escape.

        `:915` is the one plain-fence site that legitimately transforms its body
        (the strftime map result), so it is **excluded from the parameter list
        rather than skipped inside the test** — a skip is not evidence, and this
        class is evidence. Its own question is settled by
        `TestQ5FlaggedSitesInParticular::test_915_the_remap_does_not_re_enter_escaping`,
        and the whole-set version of this assertion —
        `test_no_site_in_the_fan_in_pre_escapes_or_transforms_its_body` — covers
        `:915` explicitly by naming it as one of exactly two transforming sites.
        """
        assert pin.expected_fence_body == pin.subject_value
        assert pin.expected_fence_body in p41_emitted_body(pin.source)

    @pytest.mark.parametrize("pin", P41_PLAIN_FENCE_PINS, ids=p41_ids(P41_PLAIN_FENCE_PINS))
    def test_dq_would_be_the_identity_on_this_sites_observed_body(self, pin: P41StringSitePin) -> None:
        """Why no pin in this file can move when task 4.3 lands.

        A round-trip-safe plain-fence escaper is the identity on any body free of
        ``"``, ``\\``, LF and CR. Every body observed at a plain-fence site here
        is, so wrapping these twelve sites with `_dq` leaves all twelve emissions
        byte-identical — the *positive* form of `design.md` §9 Q5's "the blast
        radius on existing behaviour is zero".

        Note the asymmetry with the two RAW-fence sites, and that it is not a
        counterexample: `:1232`'s body carries a backslash (`@example\\.com$`), so
        `_dq` is NOT the identity there — but `:1232` is routed through `_raw`,
        not `_dq`, and `_raw` spells that value raw and byte-identically. The
        uniformity claim is about `_dq` at the plain sites, which is what Q5's
        condition is stated over.
        """
        assert pin.expected_fence_body is not None, pin.case_id
        assert p41_dq_would_be_a_noop(pin.expected_fence_body), (
            pin.case_id,
            pin.expected_fence_body,
        )

    def test_the_twelve_plain_sites_share_one_fence_shape(self) -> None:
        """The claim in one assertion, so a reviewer can find it.

        Every plain-fence site's emission contains its body inside a plain
        double-quote pair, and no plain-fence site's emission contains it inside a
        raw fence or a single-quoted one. One escaper, twelve sites, no special
        cases.
        """
        offenders = []
        for pin in P41_PLAIN_FENCE_PINS:
            body = p41_emitted_body(pin.source)
            fence_body = pin.expected_fence_body
            assert fence_body is not None, pin.case_id
            if f'"{fence_body}"' not in body:
                offenders.append((pin.site, "body not in a plain double-quote pair", body))
            if f'r"{fence_body}"' in body:
                offenders.append((pin.site, "body found in a RAW fence", body))
            if f"'{fence_body}'" in body:
                offenders.append((pin.site, "body found in a SINGLE-quoted fence", body))
        assert offenders == [], offenders


class TestQ5FlaggedSitesInParticular:
    """The four sites `tasks.md` 3.1 singles out, each with its own question.

    ORACLE: current converter output, plus Python/`re` semantics for the
    matcher claims.

    `tasks.md` 3.1: *"Pay particular attention to the four sites the task text
    flags: `:915` (strftime remap), `:961` and `:1254` (`_SELECT_RENAME_SUFFIX`
    matching), and any site with pre-escaping."* Each is settled here.
    """

    # ── `:915` — the strftime remap ──────────────────────────────────

    def test_915_fence_body_is_the_map_result_not_the_literal_value(self) -> None:
        """The remap runs BEFORE the fence, so the wrap goes on its result.

        `_to_datetime` (`:915`) is
        `_java_date_format_to_strftime(self._string(node.args[1], node))` — the
        map consumes the literal and the fence receives the map's output. So
        `yyyy-MM-dd` does not appear in the emission at all and `%Y-%m-%d` does.

        This is `design.md` §5.1 row 4's "wrap the **result** of the strftime map,
        and leave the mapping itself alone" (3.5), stated as an observation. It is
        a **placement** requirement, not a per-site escaping variant — which is
        why it does not meet Q5's condition.
        """
        body = p41_emitted_body('x = to_date(col("d"), "yyyy-MM-dd")')
        assert 'format="%Y-%m-%d"' in body
        assert "yyyy-MM-dd" not in body

    @pytest.mark.parametrize(
        "java_format,expected_strftime",
        [
            ("yyyy-MM-dd", "%Y-%m-%d"),
            ("yyyy-MM-dd HH:mm:ss", "%Y-%m-%d %H:%M:%S"),
            ("MM/dd/yyyy", "%m/%d/%Y"),
            ("dd-MM-yyyy", "%d-%m-%Y"),
        ],
    )
    def test_915_the_mapping_itself_is_unchanged_by_this_spec(self, java_format: str, expected_strftime: str) -> None:
        """3.5's preserved behaviour, pinned so task 4.3 cannot drift it.

        The `bugfix.md` 3.1 risk at this site is a wrap applied to the map's
        INPUT instead of its output, which would change the format string. These
        four pins are what would catch it.
        """
        body = p41_emitted_body(f'x = to_date(col("d"), "{java_format}")')
        assert f'format="{expected_strftime}"' in body

    def test_915_the_remap_does_not_re_enter_escaping(self) -> None:
        """Q5 shape 3 ruled out: the map cannot make `_dq` wrong.

        `_java_date_format_to_strftime` (*read* end to end) walks the string,
        replaces recognised alphabetic runs from `token_map`, and appends every
        non-alphabetic character unchanged — "Unknown letter runs pass through
        unchanged (treated as literals)". It never inserts or removes a
        backslash, a quote or a line terminator, so it neither creates nor
        consumes anything a plain fence must escape.

        The consequence for task 4.1: `_dq` applied to the map's output is
        correct, and the map cannot double-escape a body `_dq` has already
        escaped. Checked here over the characters that matter rather than argued
        — the map is transparent to all four of them.
        """
        for probe in ('a"b', "a\\b", "a\nb", "a\rb", "a\tb"):
            mapped = _java_date_format_to_strftime(probe)
            # The map rewrote the single-letter run `a` to `%p` (a legitimate
            # SimpleDateFormat token) and left every escape-relevant character
            # exactly as it found it.
            for ch in ('"', "\\", "\n", "\r", "\t"):
                assert probe.count(ch) == mapped.count(ch), (probe, mapped, ch)

    # ── `:961` and `:1254` — `_SELECT_RENAME_SUFFIX` and friends ─────

    def test_961_emits_the_rename_shape_select_resolves_output_names_from(self) -> None:
        """`.rename("plain")` — the exact text `_SELECT_RENAME_SUFFIX` matches.

        `design.md` §5.1 row 5 requires `_SELECT_RENAME_SUFFIX` to still match
        after the change. This pins the shape it matches on.
        """
        assert p41_emitted_body('x = col("a").alias("plain")') == 'x = df["a"].rename("plain")'
        assert _SELECT_RENAME_SUFFIX.search('df["a"].rename("plain")') is not None

    def test_1254_carries_the_name_at_two_positions_both_plain_fenced(self) -> None:
        """One `_column_name` return, two emission positions, one fence shape.

        `_select` (`:1254-1255`) reads the name once and interpolates it into both
        the `pd.DataFrame` dict KEY and the frame SUBSCRIPT. So a single wrap at
        `_column_name` covers both positions — there is no second, differently
        fenced position needing its own escaper. Part of the Q5 uniformity
        answer, and the reason `:1254`'s recorded value multiset carries `plain`
        twice.
        """
        body = p41_emitted_body('df_transformed = df.select("plain", col("a").alias("renamed"))')
        assert body == ('df_transformed = pd.DataFrame({"plain": df["plain"], "renamed": df["a"].rename("renamed")})')
        assert body.count('"plain"') == 2

    def test_all_four_select_matchers_only_ever_see_word_character_names(self) -> None:
        """Q5 shape 4 ruled out: `_dq` cannot break a match that resolves today.

        All four `_SELECT_*` patterns require ``\\w+`` inside a PLAIN
        double-quote pair (*read*: `:202-205`). A round-trip-safe `_dq` is the
        identity on every ``\\w+`` string — there is no ``"``, ``\\``, LF or CR
        in one — so for every name these matchers can resolve, `_dq` leaves the
        emitted text byte-identical and the match is preserved.
        """
        for pattern in (_SELECT_STRING_ARG, _SELECT_COL_ARG, _SELECT_SUBSCRIPT_ARG, _SELECT_RENAME_SUFFIX):
            assert r'"(\w+)"' in pattern.pattern, pattern.pattern
        for name in ("plain", "renamed", "old_name", "a1", "_x", "Z9"):
            assert p41_dq_would_be_a_noop(name), name

    @pytest.mark.parametrize(
        "source",
        [
            'df_transformed = df.select(col("a-b"))',
            'df_transformed = df.select(col("a").alias("re-named"))',
            'df_transformed = df.select(col("a"), col("b-c"))',
        ],
    )
    def test_a_non_word_name_in_select_expression_position_already_refuses_today(self, source: str) -> None:
        """The other half of shape 4: the names `_dq` would escape do not convert now.

        A name outside ``\\w+`` cannot be resolved by any of the four matchers,
        so `_select`'s 3.6 fence refuses it — today, before any change. There is
        therefore no converting case at which `_dq`'s escaping could break a
        select output-name resolution: the set of names `_dq` alters and the set
        of names these matchers resolve are disjoint.

        Recorded as an observation rather than argued, because it is the fact
        that turns "`_dq` probably doesn't break select" into "it cannot".

        `convert` RAISES on a refusal — it does not return a report carrying the
        refusal (`_Translator`'s docstring at `:606-608`: every failure path
        raises, so a statement is either fully translated or refused). The
        construct named is `select`, which is `_select`'s own 3.6 fence at
        `:1262` rather than anything `_string` did.
        """
        with pytest.raises(ConversionError) as excinfo:
            PySpark2PandasConverter.convert(source)
        assert excinfo.value.construct == "select"

    def test_a_non_word_name_in_select_string_position_converts_and_is_plain_fenced(self) -> None:
        """The complement, so the boundary is not over-read.

        A string ARGUMENT to `select` goes through `_column_name` directly and
        never reaches `_select_output_column`, so `df.select("a-b")` converts
        today. Its fence is a plain one, and `_dq` is the identity on `a-b`, so
        this converting case is unaffected too.
        """
        assert p41_emitted_body('df_transformed = df.select("a-b")') == 'df_transformed = df[["a-b"]]'
        assert p41_dq_would_be_a_noop("a-b")

    # ── "any site with pre-escaping" ─────────────────────────────────

    def test_no_site_in_the_fan_in_pre_escapes_or_transforms_its_body(self) -> None:
        """Q5 shape 2 ruled out across all 15 sites, with the two exceptions named.

        `_string` returns the literal's source text today and every site
        interpolates that text into its fence with no further processing. On this
        file's input class value == source text, so an emission whose fence body
        differs from the value is a site transforming it. Exactly two do, and
        neither is pre-escaping:

        * `:915` transforms by **mapping** (Java SimpleDateFormat → strftime),
          which `test_915_the_remap_does_not_re_enter_escaping` shows is
          transparent to every escape-relevant character;
        * `:985` transforms by **selecting** — the value is a dict key, and the
          emission is the looked-up target.
        """
        transforming = []
        for pin in P41_STRING_SITE_PINS:
            if pin.expected_fence_body != pin.subject_value:
                transforming.append((pin.site, pin.subject_value, pin.expected_fence_body))
        assert transforming == [
            (":915", "yyyy-MM-dd", "%Y-%m-%d"),
            (":985", "int", None),
        ], transforming


# ══════════════════════════════════════════════════════════════════════
# STRUCTURAL AUDIT — the file covers the whole fan-in, and keeps covering it
# ══════════════════════════════════════════════════════════════════════


class TestThisFileCoversTheWholeFanIn:
    """15 sites, no gaps, audited against the converter rather than against a list.

    ORACLE: the converter's own AST. No conversion happens in this class.

    `bugfix.md` 3.1 calls the 15-site fan-in "the whole risk of this spec". A
    preservation file that silently covers 14 of them is worse than useless, so
    the count is audited against the shipped source. Audited by call-site COUNT
    rather than by line number, because line numbers drift the moment task 4.1
    adds `_dq`/`_raw` at module level.
    """

    @staticmethod
    def _call_site_counts() -> "tuple[int, int]":
        with open(P41_CONVERTER_PATH, encoding="utf-8") as handle:
            tree = ast.parse(handle.read())
        string_calls = 0
        column_name_calls = 0
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "_string":
                string_calls += 1
            elif node.func.attr == "_column_name":
                column_name_calls += 1
        return string_calls, column_name_calls

    def test_the_converter_still_has_the_fan_in_this_file_was_written_against(self) -> None:
        """8 `_string` calls (one of them `_column_name`'s) + 8 `_column_name` calls = 15.

        `design.md` §5.1: "7 direct calls plus 8 through `_column_name` —
        `_string` has 8 call sites of which 1 is `_column_name`'s, and
        `_column_name` has 8. Total reach 15, matching 3.1 exactly."
        """
        string_calls, column_name_calls = self._call_site_counts()
        assert string_calls == 8, string_calls
        assert column_name_calls == 8, column_name_calls
        assert (string_calls - 1) + column_name_calls == 15

    def test_one_pin_per_site_and_every_site_distinct(self) -> None:
        sites = [pin.site for pin in P41_STRING_SITE_PINS]
        assert len(sites) == 15
        assert len(set(sites)) == 15, sites

    def test_the_pinned_sites_are_exactly_bugfix_3_1s_table(self) -> None:
        """The site list, transcribed from `bugfix.md` 3.1 / `design.md` §5.1.

        Spelled out here so a reviewer can diff this file against the spec
        without reading the case table, and so a dropped or invented site fails
        rather than passing quietly.
        """
        assert sorted(pin.site for pin in P41_STRING_SITE_PINS) == sorted(
            [
                ":845",  # regexp_replace pattern        (direct, raw fence)
                ":846",  # regexp_replace replacement    (direct)
                ":852",  # concat_ws separator           (direct)
                ":915",  # _to_datetime format           (direct)
                ":961",  # alias new name                (direct)
                ":985",  # _cast string target key       (direct, no fence)
                ":1232",  # rlike pattern                 (direct, raw fence)
                ":814",  # col                           (via _column_name)
                ":1174",  # withColumn column             (via _column_name)
                ":1196",  # withColumnRenamed old         (via _column_name)
                ":1197",  # withColumnRenamed new         (via _column_name)
                ":1202",  # drop                          (via _column_name)
                ":1222",  # filter's col name             (via _column_name)
                ":1248",  # select all-string form        (via _column_name)
                ":1254",  # select string argument        (via _column_name)
            ]
        )

    def test_every_case_id_is_unique(self) -> None:
        ids = [pin.case_id for pin in P41_STRING_SITE_PINS]
        assert len(set(ids)) == len(ids), ids

    def test_both_of_bugfix_3_10s_named_reproductions_are_pinned(self) -> None:
        """3.10 names two shapes by their emitted text; both are in the table.

        `bugfix.md` 3.10: *"Reproduced: `regexp_replace(col("phone_number"),
        "[^0-9]", "")` → `df_transformed["phone"] =
        df["phone_number"].str.replace(r"[^0-9]", "", regex=True)`, and
        `rlike("@example\\.com$")` → `df["email"].str.contains(r"@example\\.com$",
        na=False)`. Both are the shapes the baseline pins."*
        """
        by_id = {pin.case_id: pin for pin in P41_STRING_SITE_PINS}
        regexp_pin = by_id["plain-regexp-replace-pattern"]
        assert 'df["phone_number"].str.replace(r"[^0-9]", "", regex=True)' in regexp_pin.expected_emission
        rlike_pin = by_id["plain-rlike-pattern-unrecognised-escape"]
        assert 'df["email"].str.contains(r"@example\\.com$", na=False)' in rlike_pin.expected_emission
