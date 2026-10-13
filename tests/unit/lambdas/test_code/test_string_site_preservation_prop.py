# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p41 T2 — a generative preservation property over plain literals at all 15 `_string` sites.

**Validates: Requirements 3.1, Property 2**

══════════════════════════════════════════════════════════════════════
AMENDED AFTER TASK 4.3 — THE ORACLE IS NOW STATED IN TWO TIERS.
`LGR-60c4985-6`, `LGR-5906ec6-2` (both template / ratified).
══════════════════════════════════════════════════════════════════════

**What changed.** The property asserted ONE thing — that the emitted text is
byte-identical to the recorded emission. It now asserts TWO, and the reason is
recorded at each assertion in
:func:`test_plain_literal_emission_equals_the_recorded_unfixed_baseline`:

* **Tier 1, over the WHOLE generated domain: value-identity.** Every string
  literal in the emission has the same *value* as the corresponding literal in
  the recorded emission, and everything outside the literals is byte-identical.
* **Tier 2, over the byte-stable subdomain: byte-identity, unchanged.** Where the
  value's spelling cannot legitimately move — printable, backslash-free — the
  original byte comparison still runs exactly as written.

**Why.** Task 4.3 routed all 15 sites through `_dq` / `_raw`, and the sweep
produced one falsifying example at `:814` with the value U+D7A4, which `repr`
spells `'\\ud7a4'`:

    recorded unfixed:  x = df["힤"]
    after 4.3:         x = df["\\ud7a4"]

U+D7A4 is category `Cn` (unassigned), so `str.isprintable()` is False, so `repr`
escapes it — and `_dq` is `repr`-based by ratified design (`tasks.md` 4.1: the
round-trip, not a substitution list, because a substitution list emits a
SyntaxError on a line break). **The value is preserved**:
`ast.literal_eval('"' + _dq(v) + '"') == v` holds, which is the contract `_dq`
was defined by. Only the literal's *spelling* moved, from raw UTF-8 to a
`\\uXXXX` escape. What was falsified is not the value contract but this file's
**byte-identity** claim, which was always stronger than the contract promised.

**Why this is not a `_dq` defect, and not a generator defect.** `_dq` is
*correct* at every site — task 3.3's Q5 verdict ("`_dq` is uniformly correct at
all 12 plain-fence sites") stands, and printable non-ASCII is untouched (`café`,
`naïve`, `日本語`, every `\\w` name emit byte-identically; verified over the whole
domain by :class:`TestTheByteStableSubdomainIsExactlyWhereDqIsTheIdentity`). And
the generator reaches this class deliberately: of the 1,111,997 code points
:data:`P41_LITERAL_CHARACTERS` admits, **957,189 are non-printable**, including
realistic ones — NBSP U+00A0 (`Zs`), LINE SEPARATOR U+2028 (`Zl`), zero-width
space U+200B (`Cf`). Narrowing the generator to printable characters would retire
the sweep over exactly the class where escaping is interesting, so it was
rejected; so was changing `_dq` to leave non-fatal non-printables unescaped,
which bolts a hand-maintained exception list onto `repr` — the shape task 4.1's
round-trip definition exists to avoid — and would need safety argued over ~957k
code points.

**Where byte-identity still holds, and why keeping it matters.** The class is
reachable from the generator and from nothing else measured: it appears in **0 of
79** baseline conversion/refusal inputs, **0 of 8** `P40_UNPINNED_CAST_KEY_PINS`
and **0 of 56** measured generated code versions. All of those live in the
printable subdomain, which is precisely tier 2's domain — so the byte-level claim
is kept where it is true and where a byte-level regression (a changed fence, a
dropped `.copy()`, an added space) is the right thing to catch.

**One exclusion was LIFTED by the same amendment: backslash.** It was excluded
for exactly the reason tier 1 now handles generally — bytes may move while the
value does not. See "THE GENERATED DOMAIN" below.

══════════════════════════════════════════════════════════════════════
ORACLE RULE FOR THIS FILE: CURRENT CONVERTER OUTPUT **IS** THE ORACLE.
READ THIS BEFORE EDITING.
══════════════════════════════════════════════════════════════════════

`LGR-28b4d75-5` (template / ratified): *a test written from the producer's
output cannot detect the producer drifting, because the output is its own
oracle.* p41 runs **two opposite rules** inside `tests/unit/lambdas/test_code/`,
so every new file has to say which one it follows.

**This file follows the observation rule: current output is the oracle.** The
expectations below were transcribed from what the **unfixed** converter emits
today, deliberately. That is legitimate here and nowhere else in this spec's D1
work, because the claim being made is *preservation* — `bugfix.md` 3.1's
"SHALL CONTINUE TO behave as today", `design.md` Property 2's "byte-identical
output". A preservation claim has no other oracle; today's behaviour is the
thing being pinned.

**The files that follow the OPPOSITE rule** — semantics, current output must NOT
be consulted — are task 2.1's `test_raw_literal_regex_fidelity.py` and task
2.2's `test_raw_literal_plain_sites.py`. Where this file and those describe the
same `_string` call site they are *meant* to disagree until task 4.3 lands: they
assert that a **raw** literal must convert (it refuses today); this file asserts
that a **plain** literal's emission must not move (it converts today). Do not
reconcile an assertion here with an assertion there.

Its hand-written sibling is task 3.1's `test_string_site_preservation.py`, which
pins one full emission per site by hand. **The two are complementary and the
division of labour is the point** (`design.md` §13): the hand-written pins are
what make a moved emission *legible* — a reviewer reads one site and one
emission — and this property is what makes it *findable*, because preservation is
a universal claim over the input domain and the generated cases reach the site a
hand-written list forgets.

**EXPECTED RESULT: GREEN ON THE FIRST RUN, BY DEFINITION** — the recorded
baseline is what the converter emits, so on the unfixed converter the property is
green trivially. **A red is a real finding: the transcription was wrong, or the
working tree has moved.** Investigate it; do not adjust the expectation to match.

──────────────────────────────────────────────────────────────────────
THE MECHANISM, AND WHY IT IS NOT "RUN THE CONVERTER TWICE"
──────────────────────────────────────────────────────────────────────

Task 3.2 requires the fixed converter's emission to be compared against the
unfixed converter's, **"captured as a recorded baseline rather than
recomputed"**. At the point this file is written the converter is still unfixed
(T3 has not landed), so the naive reading — call the old converter and the new
one and compare — is not available, and would be worthless anyway: there is only
one converter in the tree at a time, so it reduces to `convert(x) == convert(x)`,
which is true for every x forever and cannot detect drift. That is exactly the
self-oracle failure `LGR-28b4d75-5` names.

**What is recorded instead: the emission itself, as committed data, in two
forms.** Neither form calls the converter to build an expectation.

*Form 1 — a recorded emission TEMPLATE, for the 13 sites where the literal's
value reaches the emission verbatim* (:data:`P41_RECORDED_SITE_BASELINE`). Each
row is a frozen `(source_template, emitted_template)` pair carrying the slot
token :data:`P41_VALUE_SLOT` where the literal goes. For a generated value `v`
the property builds the input by substituting `v` into the source template and
the **expectation** by substituting the same `v` into the emitted template, then
asserts the live converter's emission equals it, byte for byte.

Substitution is sound at these 13 sites for a reason that is specific and
checkable rather than assumed: today's `_string` returns `segment[1:-1]` —
the literal's **source text** minus its quotes (`pyspark_to_pandas.py:756`) —
and each of these sites embeds that string into its fence unchanged. So the
emission genuinely *is* a template over the literal's text, and freezing the
template captures the whole of today's behaviour at that site rather than a
sample of it.

*Form 2 — a recorded PER-INPUT row, for the two sites where the value does not
reach the emission verbatim.* `:915` (`_to_datetime` format) rewrites the value
through the SimpleDateFormat→strftime map, and `:985` (`_cast` string target key)
uses the value as a **dict key** into `_CAST_TARGETS` and never emits it at all.
Neither is a template over the value, so for these two the baseline is an
enumerated table of `(input, emitted body)` pairs
(:data:`P41_RECORDED_STRFTIME_BASELINE`, :data:`P41_RECORDED_CAST_KEY_BASELINE`)
and the generator draws from the table. `:985`'s domain is finite — the 16 string
keys of `_CAST_TARGETS` — so its table is exhaustive, and
:class:`TestTheRecordedBaselineIsWellFormed` fails if a key is added without a
recorded row rather than leaving it silently unexercised. `:915`'s domain is
infinite, so its table is a recorded sample and says so.

**Why this can fail after T3, which is the whole test of the mechanism.** The
templates and rows are string literals in this file. They do not move when
`pyspark_to_pandas.py` moves. So after T3, a preservation-class input whose
emission changes in *any* way fails here: a different fence (`"…"` → `r"…"`), a
value escaped on the way out (`a b` → `a\x20b`), a dropped or added `.copy()`, a
`df["x"]` that becomes `df.loc[:, "x"]`, a strftime remap that stops mapping,
a `_CAST_TARGETS` emission that changes shape. The only change this property
does **not** flag is one that is exactly "the same template with the same value
substituted" — which is the definition of preservation, and therefore the
correct blind spot.

**Provenance, so the recording is reproducible rather than trusted.** Every
template and row below was produced by calling
`PySpark2PandasConverter.convert(source)` against the working tree at HEAD
**`4dd9b42`** (p40 committed; `bugfix.md`'s and `tasks.md`'s "uncommitted p40" /
"HEAD `ba3dfab`" notes are stale — see `observations.md`), with
`.venv/bin/python` 3.14.3. The template rows were derived mechanically: convert
the source with a unique probe value in the literal, then replace every
occurrence of the probe in the emitted text with :data:`P41_VALUE_SLOT`. Each
template was then cross-checked against 14 further hand-chosen values — empty
string, non-ASCII, tab, `'`, `#`, `;`, `{k}`, a 40-character run, a regex-looking
body, and the slot token itself — and held for all of them at all 13 sites.
**Cross-checked against an independent recording.** `observations.md`'s task 2.5
section recorded the plain spelling of 14 of these 15 sites during task 2.5, as
the "plain twin" column of its D1 table — as sorted string-VALUE multisets rather
than as full emitted bodies. Every one of those 14 multisets was re-derived from
the templates below and matched (`:1232` is the one site with no plain-twin row
there; `bugfix.md` 3.10 reproduces it, and that reproduction matches too). So the
table below agrees with a recording made from a different angle, by a different
task, before this file existed.

**Why the baseline is committed here as module data rather than as a separate
JSON file.** House pattern, and reviewability: `CONVERSION_BASELINE`
(`test_converter_equivalence_baseline.py`) and `PRE_FIX_EMISSIONS`
(`test_scalar_member_emission_prop.py`) are both recorded-output tables held as
literals beside the assertions that read them, which is what lets a reviewer see
the recorded value and the claim about it in one place. A separate data file buys
nothing here — it would be equally mutable and one indirection further from the
assertion. What makes the baseline *recorded* is that it is frozen data with
stated provenance, not the file extension it lives in.

──────────────────────────────────────────────────────────────────────
THE GENERATED DOMAIN — preservation-class, and drawn per KIND
──────────────────────────────────────────────────────────────────────

Preservation-class means: plain double-quoted, and the literal's **parsed value
equals its source text** — i.e. `NOT isBugCondition_D1` (no `r` prefix) and `NOT
isBugCondition_D3` (text == value). The property asserts that membership on every
generated value rather than trusting the strategies to honour it, so a strategy
that later drifts out of the domain fails loudly instead of quietly testing
something else.

`bugfix.md` 3.1: **a pattern and a column name are not the same kind of
string** — "relaxing the predicate for one relaxes it for the other unless the
change is site-aware. That is a preservation risk." So the kinds are drawn from
**separate strategies**, and each site is fed only its own kind. There are five
kinds, not two, because the two the requirement names do not cover all 15 sites:

* `column` — a column / output name (9 sites). :func:`p41_column_name_values`.
* `pattern` — a regex (2 sites, both raw-fence). :func:`p41_regex_pattern_values`.
* `text` — a separator or a replacement (2 sites). :func:`p41_literal_text_values`.
  A separator is neither a name nor a pattern; collapsing it into either would
  misstate what the site receives.
* `date_format` — `:915`, drawn from the recorded strftime table.
* `cast_key` — `:985`, drawn from the exhaustive `_CAST_TARGETS` key table.

**The BACKSLASH exclusion was LIFTED by the two-tier amendment, and lifting it
strengthens the sweep.** It read, before: *"a backslash-carrying
preservation-class value is the one shape whose BYTES may legitimately move at T3
while its VALUE does not, so the domain excludes it rather than weakening the
property to value-identity everywhere."* That reasoning was sound and is now
**obsolete**, because it describes the general case tier 1 handles: bytes moving
while the value does not is exactly what tier 1 permits and tier 2 scopes. The
backslash was never special — it was the one member of that class the file
happened to know about, and U+D7A4 is the proof there were ~957k others inside
the domain all along. Excluding it while admitting NBSP was not a boundary, it
was a coincidence of which character got noticed first.

So backslash-carrying values are now **generated**, deliberately: they are the
shape where `_dq`'s escaping is most interesting, and the shape a reader is most
likely to worry about. `"a\\qb"` is preservation-class — Python leaves the
unrecognised escape alone, so its parsed value equals its source text — and the
fixed converter emits `x = df["a\\\\qb"]`, whose literal *value* is `a\\qb`.
Tier 1 holds; tier 2 correctly does not apply. Generation is *controlled* rather
than free: a backslash is only ever emitted followed by a character that does
**not** form a recognised Python escape (:data:`P41_RECOGNISED_ESCAPE_FOLLOWERS`),
because `"a\\nb"` would parse to a newline — text != value — which is a D3 shape
owned by task 7.1 and not a preservation case at all. A trailing backslash is
unreachable for the same reason (`"a\\"` does not terminate).

Still excluded, and these are real boundaries rather than inherited ones: `"` (it
cannot appear unescaped in the fence at all) and the `Cc` / `Cs` character
categories (LF and CR make the *source* line unparseable, NUL makes it
uncompilable, and the rest of `Cc` goes with them as a class rather than one
character at a time). Nothing else is excluded — `Zl`, `Zp`, astral-plane
characters, combining marks, format characters and now backslashes are all
generated. Escaped shapes whose text differs from their value stay owned by tasks
6.1 (D5) and 7.1 (D3); this file's domain is still exactly "text == value".

──────────────────────────────────────────────────────────────────────
VACUITY, since a vacuous sweep already happened once here
──────────────────────────────────────────────────────────────────────

`design.md` §9 Q6 records a templated sweep in which all 376 generated statements
refused, so its zero offenders proved nothing. This property cannot go vacuous
the same way: a refusal is a **failure**, not a skipped case, because
:func:`p41_emitted_body` lets `ConversionError` propagate and the property's
assertion is an equality on emitted text that a refusal never reaches. Site
coverage is not left to chance either —
:class:`TestEverySiteIsAnchoredAtAReferenceValue` exercises all 15 sites
deterministically, so the generative half adds input variety on top of a
guaranteed floor rather than being the only thing that reaches a site.

pandas is not installed in this venv and must not be (`bugfix.md` 3.14). Nothing
here executes the emitted pandas, so every assertion below is evidence about the
**translation**, not about the execution.

No shared fixture from `tests/fixtures/conftest.py` is relevant: the converter is
pure text-to-text and touches no AWS surface.
"""

# Feature: p41-converter-literal-fidelity, Property 2: for any transform block
# whose every string literal is plain double-quoted with its parsed value equal
# to its source text, the converter's emission carries the same literal VALUES as
# the recorded emission of the unfixed converter for that same literal at that
# same _string call site, and is byte-identical to it outside the literals — and
# byte-identical outright wherever the value's spelling cannot legitimately move
# (printable and backslash-free), which is where every measured input lives.
#
# AMENDED after task 4.3 from a single byte-identity claim; the reason is at the
# assertion (LGR-60c4985-6, LGR-5906ec6-2) and in the module docstring.

import ast
import io
import os
import re
import string
import sys
import tokenize
import unicodedata
import warnings
from typing import NamedTuple

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st

# Add Lambda source to path (same shape as the rest of this directory).
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)

# `_dq` and `_raw` are imported for ONE purpose and it is not to build an
# expectation: :class:`TestTheByteStableSubdomainIsExactlyWhereDqIsTheIdentity`
# checks that tier 2's subdomain is exactly the set on which the fence helpers
# are the identity, so the amended oracle's scope is machine-checked rather than
# argued in prose. Every expectation in this file still comes from the frozen
# recorded baseline below — `LGR-28b4d75-5` is not weakened by reading a helper
# to audit a subdomain boundary.
from pyspark_to_pandas import _CAST_TARGETS, PySpark2PandasConverter, _dq, _raw

#: The two-import header `convert()` prepends to every emission (`bugfix.md` 3.7).
P41_PANDAS_HEADER = "import pandas as pd\nimport numpy as np\n\n"

#: Where the generated literal goes in a recorded template. A token rather than a
#: `str.format` placeholder on purpose: generated values may contain `{` and `}`,
#: and `str.replace` cannot be format-injected. The token appearing inside a
#: generated *value* is harmless — substitution only ever reads the template.
P41_VALUE_SLOT = "@@P41V@@"


# ══════════════════════════════════════════════════════════════════════
# THE RECORDED BASELINE — FORM 1: emission templates.
#
# FROZEN OBSERVED DATA. Transcribed from `PySpark2PandasConverter.convert`
# at HEAD 4dd9b42. Do NOT regenerate a row to make a red go away: a red
# here after T3 means a preservation-class emission moved, which is the
# finding this file exists to produce (`tasks.md` 5's diff review).
#
# Rows are `design.md` §5.1's table in its row order. The two rows absent
# from this table are §5.1 rows 4 and 6 — `:915` and `:985` — whose value
# does not reach the emission verbatim; they are FORM 2, below.
# ══════════════════════════════════════════════════════════════════════


class P41SiteTemplate(NamedTuple):
    """One `_string` call site, with today's emission frozen as a template."""

    #: The call site as `design.md` §5.1 numbers it. Line numbers are
    #: DOCUMENTATION ONLY — they drift the moment task 4.3 adds module-level
    #: helpers. `TestTheRecordedBaselineIsWellFormed` audits the site *set*
    #: against §5.1, which does not drift.
    site: str
    #: What the site is, in one phrase, so the table reads against §5.1's.
    what: str
    #: Which strategy feeds this site. `bugfix.md` 3.1: a pattern and a column
    #: name are not the same kind of string.
    kind: str
    #: The PySpark statement, with `P41_VALUE_SLOT` where the literal goes.
    source_template: str
    #: The pandas body (header stripped) the UNFIXED converter emits for it,
    #: with the same slot. OBSERVED, not derived.
    emitted_template: str


P41_RECORDED_SITE_BASELINE: "tuple[P41SiteTemplate, ...]" = (
    # ── §5.1 row 1 — direct, RAW `r"…"` fence. The pattern reaches the emission
    # verbatim today because `_string` returns source text and the site wraps it
    # in `r"…"` unchanged; that is also precisely the re-wrap D3 exploits when
    # text != value, which is why this file's domain is backslash-free. ──
    P41SiteTemplate(
        site=":845",
        what="regexp_replace pattern",
        kind="pattern",
        source_template=(
            f'df_transformed = df.withColumn("phone", regexp_replace(col("phone_number"), "{P41_VALUE_SLOT}", ""))'
        ),
        emitted_template=(
            'df_transformed = df.copy(); df_transformed["phone"] = '
            f'df["phone_number"].str.replace(r"{P41_VALUE_SLOT}", "", regex=True)'
        ),
    ),
    # ── §5.1 row 2 — direct, plain `"…"` fence. ──
    P41SiteTemplate(
        site=":846",
        what="regexp_replace replacement",
        kind="text",
        source_template=f'x = regexp_replace(col("p"), "[^0-9]", "{P41_VALUE_SLOT}")',
        emitted_template=f'x = df["p"].str.replace(r"[^0-9]", "{P41_VALUE_SLOT}", regex=True)',
    ),
    # ── §5.1 row 3 — direct. The SEPARATOR only. `concat_ws`'s hardcoded
    # `.astype(str)` on COLUMN parts is visible in the recorded emission and is a
    # separate, real fidelity divergence deliberately out of scope (3.11, p40 §5
    # N3a) — freezing it here is exactly right: `tasks.md` 11 requires its two
    # pins to end this spec with empty diffs, and this row is a third witness. ──
    P41SiteTemplate(
        site=":852",
        what="concat_ws separator",
        kind="text",
        source_template=f'x = concat_ws("{P41_VALUE_SLOT}", col("a"), col("b"))',
        emitted_template=f'x = df["a"].astype(str) + "{P41_VALUE_SLOT}" + df["b"].astype(str)',
    ),
    # ── §5.1 row 5 — direct. `_SELECT_RENAME_SUFFIX` (`:205`) matches on the
    # emitted `.rename("y")`, and §5.1 row 5 requires that match to keep working;
    # this row freezes the text it matches against. ──
    P41SiteTemplate(
        site=":961",
        what="alias new name",
        kind="column",
        source_template=f'x = col("a").alias("{P41_VALUE_SLOT}")',
        emitted_template=f'x = df["a"].rename("{P41_VALUE_SLOT}")',
    ),
    # ── §5.1 row 7 — direct, RAW `r"…"` fence, reached only through
    # `_filter_mask`. Outside filter position `_method_call` refuses `rlike` by
    # name at `:972` before `_string` is reached (`observations.md` task 1.4), so
    # the `df.filter(...)` wrapper is what puts a pattern through this site. ──
    P41SiteTemplate(
        site=":1232",
        what="rlike pattern",
        kind="pattern",
        source_template=f'df_transformed = df.filter(col("email").rlike("{P41_VALUE_SLOT}"))',
        emitted_template=(f'df_transformed = df[df["email"].str.contains(r"{P41_VALUE_SLOT}", na=False)]'),
    ),
    # ── §5.1 row 8 — via `_column_name`. ──
    P41SiteTemplate(
        site=":814",
        what="col column name",
        kind="column",
        source_template=f'x = col("{P41_VALUE_SLOT}")',
        emitted_template=f'x = df["{P41_VALUE_SLOT}"]',
    ),
    # ── §5.1 row 9 — via `_column_name`. The `df.copy()` prefix is the
    # new-frame semantics of 3.1/3.2, frozen here with the rest of the line. ──
    P41SiteTemplate(
        site=":1174",
        what="withColumn column name",
        kind="column",
        source_template=f'df_transformed = df.withColumn("{P41_VALUE_SLOT}", col("a"))',
        emitted_template=f'df_transformed = df.copy(); df_transformed["{P41_VALUE_SLOT}"] = df["a"]',
    ),
    # ── §5.1 rows 10 and 11 — via `_column_name`, TWO sites in one §5.1 row, so
    # two rows here. The generated literal moves; everything else is identical. ──
    P41SiteTemplate(
        site=":1196",
        what="withColumnRenamed old name",
        kind="column",
        source_template=f'df_transformed = df.withColumnRenamed("{P41_VALUE_SLOT}", "new_name")',
        emitted_template=f'df_transformed = df.rename(columns={{"{P41_VALUE_SLOT}": "new_name"}})',
    ),
    P41SiteTemplate(
        site=":1197",
        what="withColumnRenamed new name",
        kind="column",
        source_template=f'df_transformed = df.withColumnRenamed("old_name", "{P41_VALUE_SLOT}")',
        emitted_template=f'df_transformed = df.rename(columns={{"old_name": "{P41_VALUE_SLOT}"}})',
    ),
    # ── §5.1 row 12 — via `_column_name`. ──
    P41SiteTemplate(
        site=":1202",
        what="drop column name",
        kind="column",
        source_template=f'df_transformed = df.drop("{P41_VALUE_SLOT}")',
        emitted_template=f'df_transformed = df.drop(columns=["{P41_VALUE_SLOT}"])',
    ),
    # ── §5.1 row 13 — via `_column_name`, through `_filter_mask`. `isNotNull` is
    # the predicate deliberately: an `rlike` predicate would also put its PATTERN
    # through `:1232` and the row would stop isolating this site. ──
    P41SiteTemplate(
        site=":1222",
        what="filter's col name",
        kind="column",
        source_template=f'df_transformed = df.filter(col("{P41_VALUE_SLOT}").isNotNull())',
        emitted_template=f'df_transformed = df[df["{P41_VALUE_SLOT}"].notna()]',
    ),
    # ── §5.1 row 14 — via `_column_name`. EVERY argument must be a string
    # constant for the all-string form, which is what selects `:1248` over
    # `:1254`. A generated value equal to `"other"` is fine and needs no
    # exclusion: the recorded template duplicates it exactly as the converter
    # does, so the byte comparison still holds. ──
    P41SiteTemplate(
        site=":1248",
        what="select all-string form",
        kind="column",
        source_template=f'df_transformed = df.select("{P41_VALUE_SLOT}", "other")',
        emitted_template=f'df_transformed = df[["{P41_VALUE_SLOT}", "other"]]',
    ),
    # ── §5.1 row 15 — via `_column_name`. A MIXED argument list routes to
    # `:1254` instead of `:1248`. The slot appears TWICE in the emission (as the
    # dict key and as the subscript), which is why substitution replaces every
    # occurrence rather than the first. ──
    P41SiteTemplate(
        site=":1254",
        what="select string argument (mixed arg list)",
        kind="column",
        source_template=f'df_transformed = df.select("{P41_VALUE_SLOT}", col("a").alias("renamed"))',
        emitted_template=(
            f'df_transformed = pd.DataFrame({{"{P41_VALUE_SLOT}": df["{P41_VALUE_SLOT}"], '
            '"renamed": df["a"].rename("renamed")})'
        ),
    ),
)


# ══════════════════════════════════════════════════════════════════════
# THE RECORDED BASELINE — FORM 2: per-input rows, for the two sites whose
# value does not reach the emission verbatim.
# ══════════════════════════════════════════════════════════════════════

#: §5.1 row 4. `_to_datetime` rewrites the value through
#: `_java_date_format_to_strftime`, so there is no template over the value and
#: the baseline is an enumerated `(java format, emitted body)` table.
#:
#: A recorded SAMPLE, not an exhaustive domain — SimpleDateFormat patterns are
#: unbounded. The sample is chosen to cover every token in the map's own
#: `token_map` (`pyspark_to_pandas.py:160-179`) at both widths where it has two,
#: plus three shapes worth freezing for their own sake: an unknown letter run
#: (`MMM`) which passes through UNMAPPED, a quoted literal (`'T'`) which the map
#: does not treat as a quote, and a single-token format. Task 3.1's hand-written
#: pin covers this site once; this table is what makes the *mapping* preserved
#: rather than just one format string.
P41_RECORDED_STRFTIME_BASELINE: "tuple[tuple[str, str], ...]" = (
    ("yyyy-MM-dd", 'x = pd.to_datetime(df["d"], format="%Y-%m-%d", errors="coerce").dt.date'),
    ("dd-MM-yyyy", 'x = pd.to_datetime(df["d"], format="%d-%m-%Y", errors="coerce").dt.date'),
    ("MM/dd/yyyy", 'x = pd.to_datetime(df["d"], format="%m/%d/%Y", errors="coerce").dt.date'),
    (
        "yyyy-MM-dd HH:mm:ss",
        'x = pd.to_datetime(df["d"], format="%Y-%m-%d %H:%M:%S", errors="coerce").dt.date',
    ),
    (
        "yyyy/MM/dd HH:mm",
        'x = pd.to_datetime(df["d"], format="%Y/%m/%d %H:%M", errors="coerce").dt.date',
    ),
    ("dd.MM.yy", 'x = pd.to_datetime(df["d"], format="%d.%m.%y", errors="coerce").dt.date'),
    ("yyyyMMdd", 'x = pd.to_datetime(df["d"], format="%Y%m%d", errors="coerce").dt.date'),
    ("HH:mm:ss.SSS", 'x = pd.to_datetime(df["d"], format="%H:%M:%S.%f", errors="coerce").dt.date'),
    ("hh:mm a", 'x = pd.to_datetime(df["d"], format="%I:%M %p", errors="coerce").dt.date'),
    # `MMM` is NOT in the token map, so the letter run passes through unchanged.
    # Frozen because it is behaviour a reader would not predict from the map.
    (
        "EEEE dd MMM yyyy",
        'x = pd.to_datetime(df["d"], format="%A %d MMM %Y", errors="coerce").dt.date',
    ),
    ("EEE, dd MM yyyy", 'x = pd.to_datetime(df["d"], format="%a, %d %m %Y", errors="coerce").dt.date'),
    # A SimpleDateFormat quoted literal. The map has no notion of quoting, so the
    # apostrophes survive into the strftime string as themselves.
    (
        "yyyy-MM-dd'T'HH:mm:ss",
        'x = pd.to_datetime(df["d"], format="%Y-%m-%d\'T\'%H:%M:%S", errors="coerce").dt.date',
    ),
    ("d/M/yy", 'x = pd.to_datetime(df["d"], format="%d/%m/%y", errors="coerce").dt.date'),
    ("yyyy", 'x = pd.to_datetime(df["d"], format="%Y", errors="coerce").dt.date'),
    ("ss", 'x = pd.to_datetime(df["d"], format="%S", errors="coerce").dt.date'),
)

#: §5.1 row 6. `:985` has NO fence — the value is a dict key into
#: `_CAST_TARGETS` and selects the emission rather than appearing in it — so the
#: baseline is an `(key, emitted body)` table.
#:
#: EXHAUSTIVE over `_CAST_TARGETS`'s 16 string keys, which is why
#: `test_every_cast_targets_string_key_has_a_recorded_row` can assert coverage
#: against the module rather than against a transcribed list: a key added later
#: fails that assertion instead of going silently unexercised.
#:
#: The `boolean` / `BooleanType` rows freeze `astype(bool)`, whose null → False
#: mapping is a KNOWN divergence deliberately not fixed (3.12, p40 N3(b)).
#: Freezing it is correct: preservation means it must not move in THIS spec.
P41_RECORDED_CAST_KEY_BASELINE: "tuple[tuple[str, str], ...]" = (
    ("BooleanType", 'x = df["a"].astype(bool)'),
    ("DoubleType", 'x = pd.to_numeric(df["a"], errors="coerce")'),
    ("FloatType", 'x = pd.to_numeric(df["a"], errors="coerce").astype("float32")'),
    ("IntegerType", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("LongType", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("ShortType", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("StringType", 'x = df["a"].astype("string")'),
    ("bigint", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("boolean", 'x = df["a"].astype(bool)'),
    ("decimal", 'x = pd.to_numeric(df["a"], errors="coerce")'),
    ("double", 'x = pd.to_numeric(df["a"], errors="coerce")'),
    ("float", 'x = pd.to_numeric(df["a"], errors="coerce")'),
    ("int", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("integer", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("long", 'x = pd.to_numeric(df["a"], errors="coerce").astype("Int64")'),
    ("string", 'x = df["a"].astype("string")'),
)

#: The two Form-2 sites and their source shapes.
P41_STRFTIME_SITE = ":915"
P41_STRFTIME_SOURCE_TEMPLATE = f'x = to_date(col("d"), "{P41_VALUE_SLOT}")'
P41_CAST_KEY_SITE = ":985"
P41_CAST_KEY_SOURCE_TEMPLATE = f'x = col("a").cast("{P41_VALUE_SLOT}")'

#: All 15 `_string`-reachable sites, from `design.md` §5.1 / `bugfix.md` 3.1.
#: Transcribed from the design table so the audit below compares two independent
#: lists rather than one list with itself.
P41_ALL_FIFTEEN_SITES = (
    ":814",
    ":845",
    ":846",
    ":852",
    ":915",
    ":961",
    ":985",
    ":1174",
    ":1196",
    ":1197",
    ":1202",
    ":1222",
    ":1232",
    ":1248",
    ":1254",
)


# ══════════════════════════════════════════════════════════════════════
# Helpers. `p41_` prefixed per `design.md` §10, which also carries the
# standing naming fence: `is_bug_condition_d3` already exists at
# `test_converter_equivalence_baseline.py:910` and means an EARLIER spec's
# Defect 3, so no p41 helper may take that name.
# ══════════════════════════════════════════════════════════════════════


def p41_emitted_body(source: str) -> str:
    """The pandas `convert()` emits for *source*, with the import header removed.

    `ConversionError` is deliberately NOT caught. A refusal on a
    preservation-class input is a failure of Property 2, not a case to skip —
    which is also what stops this property going vacuous the way `design.md`
    §9 Q6's 376-refusal sweep did.
    """
    code = PySpark2PandasConverter.convert(source)["code"]
    assert code.startswith(P41_PANDAS_HEADER), code
    return code[len(P41_PANDAS_HEADER) :]


def p41_is_preservation_class(value: str) -> bool:
    """True when *value* spelled as `"value"` is a preservation-class literal.

    Two conditions, both from `bugfix.md`'s bug-condition definitions:

    * NOT `isBugCondition_D1` — no `r`/`R` prefix. Guaranteed by construction
      here, since every source template spells the fence as a bare `"…"`.
    * NOT `isBugCondition_D3` — the literal's **parsed value equals its source
      text**. Checked by round-tripping through `ast.literal_eval`, which is
      Python's own answer rather than a re-implementation of escape rules.

    A value that would not parse inside a plain fence at all (a real LF, an
    unescaped `"`) is reported False rather than raising, so the property's
    domain assertion reads as a domain violation instead of an error.

    `SyntaxWarning` is suppressed for the same reason `convert` suppresses it
    (`pyspark_to_pandas.py:1372-1376`): Python's advice about an unrecognised
    escape is about the *text*, and this function is asking Python what that
    text *means*. The warning is not information this predicate wants, and
    `tasks.md` 7.1 forbids making a test depend on it being raised.
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        try:
            parsed = ast.literal_eval('"' + value + '"')
        except (SyntaxError, ValueError):
            return False
    return parsed == value


#: Placeholder a string literal is replaced by in a skeleton. Positional, so two
#: literals cannot be transposed without the skeletons differing.
P41_LITERAL_PLACEHOLDER = "@@P41LIT{}@@"


def p41_literal_split(code: str) -> "tuple[str, tuple[str, ...]]":
    """Split *code* into its **skeleton** and the **values** of its string literals.

    The skeleton is *code* byte-for-byte with every string literal replaced by a
    positional placeholder; the values are what those literals evaluate to, in
    source order. Together they are the amended oracle's two halves: comparing
    skeletons is a byte claim about everything that is not a literal, and
    comparing values is a value claim about the literals themselves.

    Tokenizing rather than parsing on purpose. `ast.unparse` would normalise the
    whole statement — quote style, spacing, redundant parentheses — and a
    normalising comparison cannot see a skeleton change, which is most of what
    this property exists to catch. `tokenize` gives exact source spans, so
    everything outside a `STRING` token stays under a byte comparison.

    **What this deliberately CANNOT see, so tier 2 has to exist.** A fence change
    that preserves the value — `"[^0-9]"` becoming `r"[^0-9]"`, or `_raw` falling
    back from its raw arm to a plain one — is a change of literal *spelling*, so
    the skeleton is identical and the values are equal and tier 1 passes.
    :class:`TestTheValueOracleAndTheByteOracleCatchDifferentThings` pins that
    blind spot rather than leaving a reader to discover it.

    A code string that does not tokenize raises :class:`AssertionError` naming the
    problem. That is a feature, not defensive noise: `tasks.md` 4.1 records that
    the escaping hazard `_dq` was defined to avoid is an emission that does not
    *parse* (a line break inside a plain fence), so an unparseable emission is a
    real finding and this is the only place in this file that would notice it.
    """
    if not code.endswith("\n"):
        code += "\n"

    # Line starts computed over `\n` ONLY. `str.splitlines` also splits on U+2028
    # / U+2029 / U+0085, which `tokenize` does not, and U+2028 (`Zl`) is inside
    # this file's generated domain — so splitlines would misalign every span
    # after the first such character.
    line_starts = [0]
    for index, character in enumerate(code):
        if character == "\n":
            line_starts.append(index + 1)

    spans: list[tuple[int, int]] = []
    values: list[str] = []
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            for token in tokenize.generate_tokens(io.StringIO(code).readline):
                if token.type != tokenize.STRING:
                    continue
                start = line_starts[token.start[0] - 1] + token.start[1]
                end = line_starts[token.end[0] - 1] + token.end[1]
                literal = ast.literal_eval(token.string)
                assert isinstance(literal, str), f"non-str literal {token.string!r} in {code!r}"
                spans.append((start, end))
                values.append(literal)
    except (SyntaxError, tokenize.TokenError, IndentationError) as exc:
        raise AssertionError(
            f"emitted or recorded pandas does not tokenize: {exc!r}\n  code: {code!r}\n"
            f"An emission that does not parse is a real finding (`tasks.md` 4.1: the hazard a "
            f"substitution-list `_dq` creates is a SyntaxError in the generated pandas, not a "
            f"wrong string), not a comparison to work around."
        ) from exc

    skeleton = code
    for index, (start, end) in reversed(list(enumerate(spans))):
        skeleton = skeleton[:start] + P41_LITERAL_PLACEHOLDER.format(index) + skeleton[end:]
    return skeleton, tuple(values)


def p41_is_byte_stable(value: str) -> bool:
    """True when *value*'s SPELLING cannot legitimately move through either fence.

    This is tier 2's subdomain — the one on which the original byte-identity
    claim is still exactly right, and it is not a hand-drawn boundary. `_dq` is
    `repr`-based, and CPython's `repr` escapes exactly three things: the
    backslash, the quote it chose, and every character for which
    `str.isprintable()` is False. This file's domain already excludes `"`, so
    what is left is *printable* and *backslash-free*, and on that set `_dq` is the
    identity and `_raw` returns its raw arm — verified over all 1,111,997 code
    points of the domain by
    :class:`TestTheByteStableSubdomainIsExactlyWhereDqIsTheIdentity`, which finds
    zero disagreement in either direction.

    **`str.isprintable()` is False for more than the obvious controls**, which is
    the whole reason the original claim broke: it is False for `Cc`, `Cf`, `Cs`,
    `Co`, `Cn`, `Zl`, `Zp` and every `Zs` except ASCII space. So NBSP U+00A0,
    LINE SEPARATOR U+2028 and unassigned code points such as U+D7A4 are all
    outside this subdomain — and all inside the generated domain.

    Every measured input is inside this subdomain: the 79 baseline cases, the 8
    `P40_UNPINNED_CAST_KEY_PINS` and the 56 measured generated code versions
    (`bugfix.md`'s production measurement, `tasks.md` 13.1).
    """
    return value.isprintable() and "\\" not in value


# ──────────────────────────────────────────────────────────────────────
# The generated domain, drawn per KIND.
#
# `bugfix.md` 3.1: a pattern and a column name are NOT the same kind of string.
# Separate strategies is that sentence made structural — a change that relaxed
# one kind's handling and not the other's shows up as a red at the sites fed by
# the kind it broke, naming that site.
# ──────────────────────────────────────────────────────────────────────

#: Every ORDINARY character a preservation-class plain literal may carry —
#: everything but a backslash, which is drawn as part of a pair instead (see
#: :data:`P41_LITERAL_UNITS`).
#:
#: `"` is excluded because it cannot appear unescaped inside the fence. `Cc`/`Cs`
#: are excluded because LF and CR make the *source* line unparseable and NUL makes
#: it uncompilable, and the rest of `Cc` goes with them as a class. `Zl`, `Zp`,
#: `Zs`, astral-plane characters, combining marks, format characters and
#: unassigned code points are all IN, and every recorded template was verified to
#: hold on them.
#:
#: **1,111,997 code points, of which 957,189 are non-printable** — measured over
#: the domain, not estimated. That class is what the two-tier amendment exists
#: for: `_dq` escapes every one of them, preserving the value and moving the
#: bytes. Before the amendment this file asserted byte-identity over all 1.1M and
#: was falsified by the first non-printable Hypothesis drew (U+D7A4). The sweep is
#: kept over them because escaping is where the interesting behaviour is; what
#: moved is the oracle, not the domain.
P41_LITERAL_CHARACTERS = st.characters(
    codec="utf-8",
    exclude_characters='"\\',
    exclude_categories=("Cc", "Cs"),
)

#: The characters which, immediately after a backslash, form a **recognised**
#: Python escape — so `"a\<c>b"` parses to something other than its own source
#: text, making it an `isBugCondition_D3` shape (task 7.1's) rather than a
#: preservation case (this file's).
#:
#: Derived rather than transcribed from the language reference: every code point
#: in the generated domain was tried as a follower and exactly these 21 broke
#: `text == value`.
#: :meth:`TestTheGeneratedDomainIsSound.test_the_recognised_escape_follower_set_is_exactly_pythons`
#: re-derives the ASCII half on every run, so the set cannot silently drift.
#: `"` and LF are absent only because the domain excludes them already.
P41_RECOGNISED_ESCAPE_FOLLOWERS = frozenset("01234567NUabfnrtuvx\\'")

#: A backslash plus a follower that leaves it unrecognised, so the pair's value is
#: itself. This is the shape the LIFTED backslash exclusion admits.
P41_BACKSLASH_PAIRS = st.characters(
    codec="utf-8",
    exclude_characters='"\\' + "".join(sorted(P41_RECOGNISED_ESCAPE_FOLLOWERS)),
    exclude_categories=("Cc", "Cs"),
).map(lambda follower: "\\" + follower)

#: One unit of a generated value: an ordinary character, or a backslash pair.
#:
#: The ordinary branch is repeated so that roughly one unit in four carries a
#: backslash. A value that is half backslashes is not a shape any generator would
#: realistically write, and what the lifted exclusion is for is a backslash
#: sitting AMONG ordinary characters.
#:
#: Building values out of units rather than adding `\` to the alphabet is what
#: keeps every draw preservation-class **by construction** rather than by a
#: Hypothesis `.filter`: a bare `\` drawn from an alphabet could land before a
#: recognised follower (`"a\nb"` — value is a newline, text is not, so it is a D3
#: shape) or at the end of the value, where `"a\"` does not terminate at all.
P41_LITERAL_UNITS = st.one_of(
    P41_LITERAL_CHARACTERS,
    P41_LITERAL_CHARACTERS,
    P41_LITERAL_CHARACTERS,
    P41_BACKSLASH_PAIRS,
)


def p41_unit_text(max_units: int) -> st.SearchStrategy:
    """Free-form preservation-class text of at most *max_units* units.

    The unit count rather than the character count is bounded, so a value may be
    up to twice *max_units* characters long. That is deliberate: the bound exists
    to keep examples readable, not to make a claim about length.
    """
    return st.lists(P41_LITERAL_UNITS, min_size=0, max_size=max_units).map("".join)


def p41_column_name_values() -> st.SearchStrategy:
    """Column / output-name shapes — the kind 9 of the 15 sites receive.

    Mixed on purpose: a realistic identifier alone would never reach a site
    whose handling depends on a space, a dot or a non-ASCII character, and the
    empty name is included because `col("")` converts today (`x = df[""]`) and
    preservation is a claim about what the converter does, not about what the
    generation prompt is likely to write.
    """
    identifier = st.builds(
        lambda head, tail: head + tail,
        st.sampled_from(string.ascii_letters + "_"),
        st.text(alphabet=st.sampled_from(string.ascii_letters + string.digits + "_"), max_size=15),
    )
    awkward = p41_unit_text(24)
    return st.one_of(identifier, awkward)


#: Regex atoms for :func:`p41_regex_pattern_values`, all backslash-free and all
#: `"`-free.
#:
#: **Backslash-free is no longer the domain constraint** — the two-tier amendment
#: lifted that for the `column` and `text` kinds — but it is kept deliberately for
#: THIS kind, and the reason is that keeping it makes the claim at the two
#: raw-fence sites *stronger*, not weaker:
#:
#: * A backslash-carrying value is outside :func:`p41_is_byte_stable`, so every
#:   pattern case would drop to tier 1 and the two `r"…"` sites would lose their
#:   byte-level claim — the opposite of strengthening. A raw-spellable pattern
#:   emits byte-identically through `_raw`, so byte-identity is the right and
#:   available claim here.
#: * Escape fidelity at exactly these two sites is task 7.1's owned surface
#:   (`test_raw_literal_regex_fidelity.py`), where the oracle is `re` semantics —
#:   strictly stronger than this file's recorded baseline. Restating it here with
#:   a weaker oracle buys nothing.
#:
#: Backslash-free is also why D3 needs a DOUBLED backslash to manifest and why
#: these shapes are exactly the ones whose text equals their value
#: (`observations.md` task 2.5's control).
P41_REGEX_ATOMS = (
    "[0-9]",
    "[^0-9]",
    "[a-z]",
    "[A-Za-z]",
    "[^0-9+]",
    "[-.@]",
    ".",
    "(ab|cd)",
    "abc",
    "x",
    "@example",
    "com",
)

#: At most ONE quantifier per atom, which is what keeps every composition a
#: valid regex: a repeat can never land on a repeat.
P41_REGEX_QUANTIFIERS = ("", "?", "*", "+")


@st.composite
def p41_regex_pattern_values(draw) -> str:
    """Regex shapes — the kind the two RAW-fence sites receive.

    Composed from :data:`P41_REGEX_ATOMS` rather than drawn as free text, so
    every generated pattern is a *plausible* regex and a valid one.
    :class:`TestTheGeneratedDomainIsSound` proves validity by enumerating the
    atom × quantifier grid and every pair of it, rather than asserting it
    per-example inside the property, which would mix a generator sanity check
    into the preservation claim.
    """
    anchor_start = draw(st.booleans())
    anchor_end = draw(st.booleans())
    pieces = draw(
        st.lists(
            st.tuples(st.sampled_from(P41_REGEX_ATOMS), st.sampled_from(P41_REGEX_QUANTIFIERS)),
            min_size=1,
            max_size=3,
        )
    )
    body = "".join(atom + quantifier for atom, quantifier in pieces)
    return ("^" if anchor_start else "") + body + ("$" if anchor_end else "")


def p41_literal_text_values() -> st.SearchStrategy:
    """Separator / replacement shapes — neither a name nor a pattern.

    `:846`'s replacement and `:852`'s separator are the two sites that receive
    this kind. Collapsing them into the column-name kind would misstate what
    the site receives, which is the distinction `bugfix.md` 3.1 asks to be kept.
    Short, because a separator is short.
    """
    return p41_unit_text(6)


P41_VALUE_STRATEGIES = {
    "column": p41_column_name_values(),
    "pattern": p41_regex_pattern_values(),
    "text": p41_literal_text_values(),
}

P41_TEMPLATES_BY_SITE = {row.site: row for row in P41_RECORDED_SITE_BASELINE}


class P41Trial(NamedTuple):
    """One generated preservation-class input, with its expectation from the baseline."""

    site: str
    what: str
    kind: str
    value: str
    source: str
    #: Read from the recorded baseline. NEVER computed by calling the converter.
    expected_body: str
    #: Which of the two recorded forms produced `expected_body`.
    mechanism: str


@st.composite
def p41_trials(draw) -> P41Trial:
    """A site drawn from all 15, and a value drawn from that site's own kind."""
    site = draw(st.sampled_from(P41_ALL_FIFTEEN_SITES))

    if site == P41_STRFTIME_SITE:
        java_format, emitted_body = draw(st.sampled_from(P41_RECORDED_STRFTIME_BASELINE))
        return P41Trial(
            site=site,
            what="_to_datetime format string",
            kind="date_format",
            value=java_format,
            source=P41_STRFTIME_SOURCE_TEMPLATE.replace(P41_VALUE_SLOT, java_format),
            expected_body=emitted_body,
            mechanism="recorded per-input row (value is remapped, not emitted verbatim)",
        )

    if site == P41_CAST_KEY_SITE:
        key, emitted_body = draw(st.sampled_from(P41_RECORDED_CAST_KEY_BASELINE))
        return P41Trial(
            site=site,
            what="_cast string target key (no fence — dict lookup)",
            kind="cast_key",
            value=key,
            source=P41_CAST_KEY_SOURCE_TEMPLATE.replace(P41_VALUE_SLOT, key),
            expected_body=emitted_body,
            mechanism="recorded per-input row (value selects the emission, never appears in it)",
        )

    row = P41_TEMPLATES_BY_SITE[site]
    value = draw(P41_VALUE_STRATEGIES[row.kind])
    return P41Trial(
        site=row.site,
        what=row.what,
        kind=row.kind,
        value=value,
        source=row.source_template.replace(P41_VALUE_SLOT, value),
        expected_body=row.emitted_template.replace(P41_VALUE_SLOT, value),
        mechanism="recorded emission template",
    )


#: 300 examples over 15 sites — ~20 draws per site, comfortably above
#: `tasks.md` 3.2's minimum of 100. `deadline=None` because the first example
#: pays the module import; each `convert` call after that is sub-millisecond.
P41_PROP_SETTINGS = settings(
    max_examples=300,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)


# ══════════════════════════════════════════════════════════════════════
# THE PROPERTY. One property, per `tasks.md` 3.2.
# ══════════════════════════════════════════════════════════════════════


@P41_PROP_SETTINGS
@given(trial=p41_trials())
def test_plain_literal_emission_equals_the_recorded_unfixed_baseline(trial: P41Trial) -> None:
    """Property 2: a plain literal's emission preserves the recorded one's literal VALUES,
    and its bytes too wherever the value's spelling cannot legitimately move.

    **Validates: Requirements 3.1, Property 2**

    The expectation comes from :data:`P41_RECORDED_SITE_BASELINE` /
    :data:`P41_RECORDED_STRFTIME_BASELINE` /
    :data:`P41_RECORDED_CAST_KEY_BASELINE` — frozen observed data — and never
    from a second call to the converter. That is what makes this capable of
    failing after T3: the baseline does not move when the converter moves.

    **AMENDED after task 4.3, in two tiers rather than one byte comparison.** The
    reason is recorded at each assertion below and the whole argument is in the
    module docstring; the short form is that the value contract `_dq` was defined
    by guarantees value-identity, not byte-identity, and this property was
    claiming the stronger of the two over a domain where 957,189 of 1,111,997
    code points make the two differ.

    `event()` records the site, the kind and **which tier applied**, so
    `--hypothesis-show-statistics` reports whether the sweep is still reaching
    both sides of the amended oracle rather than leaving that assumed — a run in
    which every draw were byte-stable would be a run in which nothing new is
    being exercised. The deterministic floor is
    :class:`TestEverySiteIsAnchoredAtAReferenceValue`.
    """
    event(f"site={trial.site}")
    event(f"kind={trial.kind}")
    event(f"byte_stable={p41_is_byte_stable(trial.value)}")

    # The domain, asserted rather than trusted: a strategy that drifts out of
    # the preservation class fails here instead of quietly testing D1 or D3.
    assert p41_is_preservation_class(trial.value), (
        f"generated value {trial.value!r} for site {trial.site} is not preservation-class: "
        f"its parsed value differs from its source text, so it is an isBugCondition_D3 shape "
        f"and belongs to task 7.1, not to a preservation property"
    )
    assert P41_VALUE_SLOT not in trial.source or P41_VALUE_SLOT in trial.value, (
        f"the slot token survived substitution into {trial.source!r} — the recorded template "
        f"for site {trial.site} is malformed"
    )

    # A refusal propagates out of here as a ConversionError and fails the
    # property. It is not a skip: refusing a preservation-class input IS the
    # regression Property 2 forbids.
    emitted = p41_emitted_body(trial.source)

    # ══════════════════════════════════════════════════════════════════
    # TIER 1 — VALUE-IDENTITY, over the WHOLE generated domain.
    #
    # AMENDED HERE, and this comment is the amendment's record.
    # `LGR-60c4985-6` and `LGR-5906ec6-2` (both template / ratified): a
    # deliberately-held assertion is amended IN PLACE with its reason AT the
    # assertion, never silently weakened and never deleted.
    #
    # WHAT CHANGED. This was one line — `assert emitted == trial.expected_body`.
    # It is now this value comparison plus the byte comparison in tier 2 below.
    #
    # WHY. Task 4.3 routed all 15 `_string` sites through the fence helpers, and
    # the sweep produced exactly one falsifying example: site `:814`, value
    # U+D7A4, recorded `x = df["<U+D7A4>"]` against emitted `x = df["\ud7a4"]`.
    # U+D7A4 is category `Cn` (unassigned), so `str.isprintable()` is False, so
    # `repr` escapes it — and `_dq` is `repr`-based by ratified design
    # (`tasks.md` 4.1 defines it by a round-trip, not a substitution list,
    # because a substitution list emits a SyntaxError on a line break).
    #
    # THE VALUE CONTRACT GUARANTEES VALUE-IDENTITY, NOT BYTE-IDENTITY. `_dq`'s
    # contract is `ast.literal_eval('"' + _dq(v) + '"') == v`, and it holds for
    # U+D7A4: only the literal's SPELLING moved, from raw UTF-8 to a `\uXXXX`
    # escape. So the falsified claim is this file's, not the converter's — this
    # property was asserting something strictly stronger than anything the fix
    # ever promised, over a domain in which 957,189 of 1,111,997 code points
    # make the two claims differ.
    #
    # NOT A `_dq` DEFECT. Task 3.3's Q5 verdict — "`_dq` is uniformly correct at
    # all 12 plain-fence sites" — stands unamended. Printable non-ASCII is
    # untouched: `café`, `naïve`, `日本語` and every `\w` name emit
    # byte-identically, verified over the whole domain by
    # `TestTheByteStableSubdomainIsExactlyWhereDqIsTheIdentity`.
    #
    # AND THE BYTE-LEVEL CLAIM HELD FOR THE PRINTABLE DOMAIN, which is why it is
    # kept in tier 2 rather than dropped: the non-printable class appears in 0 of
    # the 79 baseline inputs, 0 of the 8 `P40_UNPINNED_CAST_KEY_PINS` and 0 of
    # the 56 measured generated code versions. Every measured and baseline input
    # lives in the printable subdomain, and there byte-identity is exactly right.
    #
    # The two arms rejected in reaching this, recorded so they are not re-tried:
    # narrowing the generator to printable characters (retires the sweep over
    # precisely the class where escaping is interesting), and changing `_dq` to
    # leave non-fatal non-printables unescaped (bolts a hand-maintained
    # exception list onto `repr` — the shape task 4.1's round-trip definition
    # exists to avoid — and needs safety argued over ~957k code points).
    # ══════════════════════════════════════════════════════════════════
    emitted_skeleton, emitted_values = p41_literal_split(emitted)
    recorded_skeleton, recorded_values = p41_literal_split(trial.expected_body)

    assert emitted_skeleton == recorded_skeleton, (
        f"site {trial.site} ({trial.what}), kind={trial.kind}, value={trial.value!r}\n"
        f"  source            : {trial.source!r}\n"
        f"  emitted           : {emitted!r}\n"
        f"  recorded          : {trial.expected_body!r}\n"
        f"  emitted skeleton  : {emitted_skeleton!r}\n"
        f"  recorded skeleton : {recorded_skeleton!r}\n"
        f"  baseline          : {trial.mechanism}\n"
        f"Everything OUTSIDE a string literal must stay byte-identical — a dropped or added "
        f'`.copy()`, a `df["x"]` that became `df.loc[:, "x"]`, a changed argument order, an '
        f"extra space. This half of the oracle was NOT weakened by the amendment: it is the "
        f"original byte comparison, restricted to the part of the emission where literal "
        f"spelling cannot be the explanation. The recorded baseline is frozen data observed on "
        f"the unfixed converter at HEAD 4dd9b42, so a mismatch means the EMISSION moved."
    )
    assert emitted_values == recorded_values, (
        f"site {trial.site} ({trial.what}), kind={trial.kind}, value={trial.value!r}\n"
        f"  source          : {trial.source!r}\n"
        f"  emitted         : {emitted!r}\n"
        f"  recorded        : {trial.expected_body!r}\n"
        f"  emitted values  : {emitted_values!r}\n"
        f"  recorded values : {recorded_values!r}\n"
        f"  baseline        : {trial.mechanism}\n"
        f"A string literal in the emission carries a DIFFERENT VALUE from the one the unfixed "
        f"converter emitted at this site. This is the value contract itself failing, not a "
        f"spelling change: it is the class D3 and D5 belong to, at a preservation-class input. "
        f"Do not amend the recorded baseline to match — establish first why the value moved."
    )

    # ══════════════════════════════════════════════════════════════════
    # TIER 2 — BYTE-IDENTITY, over the byte-stable subdomain, UNCHANGED.
    #
    # The original assertion, run verbatim, on the subdomain where it is still
    # exactly right: printable and backslash-free, which is where `_dq` is the
    # identity, `_raw` takes its raw arm, and every measured and baseline input
    # lives. Keeping it is what stops the amendment costing the sweep its
    # byte-level teeth — a changed FENCE (`"…"` → `r"…"`, or `_raw` falling back
    # to a plain fence) is value-preserving and therefore invisible to tier 1,
    # and this is the assertion that sees it.
    # ══════════════════════════════════════════════════════════════════
    if p41_is_byte_stable(trial.value):
        assert emitted == trial.expected_body, (
            f"site {trial.site} ({trial.what}), kind={trial.kind}, value={trial.value!r}\n"
            f"  source   : {trial.source!r}\n"
            f"  emitted  : {emitted!r}\n"
            f"  recorded : {trial.expected_body!r}\n"
            f"  baseline : {trial.mechanism}\n"
            f"The value is BYTE-STABLE — printable and backslash-free — so `_dq` is the identity "
            f"on it and `_raw` spells it raw, and Property 2's original byte-identity claim "
            f"applies unchanged. The literal VALUES already matched above, so what moved is "
            f"spelling: a different fence, an escape applied where none is needed, or "
            f"`_raw`'s plain-fence fallback becoming reachable (`tasks.md` 4.1 records that "
            f"fallback as unreachable from any measured input). The recorded baseline was "
            f"observed on the unfixed converter at HEAD 4dd9b42; it is frozen data, so a "
            f"mismatch means the EMISSION moved, not that the expectation is stale. Amend it at "
            f"the assertion with its reason (LGR-60c4985-6) only after establishing the move was "
            f"intended."
        )


# ══════════════════════════════════════════════════════════════════════
# Integrity of the recorded baseline and of the generated domain.
#
# None of these calls the converter to build an expectation, and none is
# generative. They exist so that the property's two silent failure modes
# — a malformed recorded row, and a site or a domain that is never
# reached — are checked rather than assumed.
# ══════════════════════════════════════════════════════════════════════


class TestTheRecordedBaselineIsWellFormed:
    """The frozen data is auditable against `design.md` §5.1 and against the module."""

    def test_the_two_forms_together_cover_exactly_the_fifteen_sites(self) -> None:
        """No site missed, none invented, none covered twice.

        `bugfix.md` 3.1 enumerates all 15 "because a change inside a 20-line
        function with this fan-in is the whole risk of this spec". A
        preservation property over 14 of them would be the exact shape
        `design.md` §13 warns about — the generated cases are supposed to catch
        the site a hand-written list forgets, which they cannot do for a site
        the site list itself forgets.
        """
        covered = [row.site for row in P41_RECORDED_SITE_BASELINE] + [
            P41_STRFTIME_SITE,
            P41_CAST_KEY_SITE,
        ]
        assert len(covered) == len(set(covered)), f"a site is covered twice: {sorted(covered)}"
        assert set(covered) == set(P41_ALL_FIFTEEN_SITES), (
            f"missing {sorted(set(P41_ALL_FIFTEEN_SITES) - set(covered))}, "
            f"unexpected {sorted(set(covered) - set(P41_ALL_FIFTEEN_SITES))}"
        )

    def test_the_two_raw_fence_sites_are_fed_regex_shapes(self) -> None:
        """`design.md` §5.1's fence column, asserted rather than left in prose.

        `:845` and `:1232` are the only sites emitting into an `r"…"` fence, and
        they are the only two that must receive the `pattern` kind — that is
        `bugfix.md` 3.1's "a pattern and a column name are not the same kind of
        string" stated over this file's own table.
        """
        by_kind: dict[str, list[str]] = {}
        for row in P41_RECORDED_SITE_BASELINE:
            by_kind.setdefault(row.kind, []).append(row.site)
        assert sorted(by_kind["pattern"]) == [":1232", ":845"], by_kind
        assert sorted(by_kind["text"]) == [":846", ":852"], by_kind
        assert len(by_kind["column"]) == 9, by_kind

    @pytest.mark.parametrize("row", P41_RECORDED_SITE_BASELINE, ids=[r.site for r in P41_RECORDED_SITE_BASELINE])
    def test_every_template_row_carries_the_slot_on_both_sides(self, row: P41SiteTemplate) -> None:
        """A row with no slot in its emission would assert a constant, not a preservation.

        The source side must carry the slot exactly once — two slots would mean
        one generated value landing at two `_string` sites, and a red would not
        say which. The emitted side must carry it at least once, and `:1254`
        carries it twice legitimately.
        """
        assert row.source_template.count(P41_VALUE_SLOT) == 1, row
        assert P41_VALUE_SLOT in row.emitted_template, row

    @pytest.mark.parametrize("row", P41_RECORDED_SITE_BASELINE, ids=[r.site for r in P41_RECORDED_SITE_BASELINE])
    def test_every_template_row_is_a_plain_double_quoted_fence(self, row: P41SiteTemplate) -> None:
        """The generated literal is spelled `"…"` — never `r"…"`, never `'…'`.

        This is `NOT isBugCondition_D1` at the level of the table: a row that
        spelled its slot `r"@@P41V@@"` would be generating D1 shapes and
        asserting they are preserved, which is task 2.1/2.2's claim inverted.
        """
        slot_at = row.source_template.index(P41_VALUE_SLOT)
        assert row.source_template[slot_at - 1] == '"', row.source_template
        assert row.source_template[slot_at - 2] not in ("r", "R", "'"), row.source_template

    def test_every_cast_targets_string_key_has_a_recorded_row(self) -> None:
        """`:985`'s table is exhaustive, checked against the module and not a copy.

        Read from `_CAST_TARGETS` rather than transcribed, so a key added to the
        converter later fails HERE — a visible finding — instead of being a site
        input this property silently never exercises.
        """
        recorded = {key for key, _ in P41_RECORDED_CAST_KEY_BASELINE}
        module_keys = set(_CAST_TARGETS)
        assert recorded == module_keys, (
            f"missing rows for {sorted(module_keys - recorded)}, "
            f"rows for keys the converter does not have: {sorted(recorded - module_keys)}"
        )

    def test_the_strftime_table_covers_every_token_in_the_converters_map(self) -> None:
        """`:915`'s table is a recorded SAMPLE, but not an arbitrary one.

        Every SimpleDateFormat token the converter maps appears in at least one
        sampled format, so the mapping is preserved as a whole rather than at
        one format string. `MMM` is checked separately, as the unmapped-run case.
        """
        sampled = " ".join(fmt for fmt, _ in P41_RECORDED_STRFTIME_BASELINE)
        for token in ("yyyy", "yy", "MM", "M", "dd", "d", "HH", "hh", "mm", "ss", "SSS", "a", "EEEE", "EEE"):
            assert re.search(rf"(?<![A-Za-z]){re.escape(token)}(?![A-Za-z])", sampled), (
                f"token {token!r} from `_java_date_format_to_strftime`'s token_map is not covered by any sampled format"
            )
        assert any("MMM" in fmt for fmt, _ in P41_RECORDED_STRFTIME_BASELINE), (
            "the unmapped-letter-run case is not covered"
        )

    @pytest.mark.parametrize(("java_format", "_emitted"), P41_RECORDED_STRFTIME_BASELINE)
    def test_every_strftime_input_is_itself_preservation_class(self, java_format: str, _emitted: str) -> None:
        """A recorded row whose own input carried an escape would not be a preservation case."""
        assert p41_is_preservation_class(java_format)

    @pytest.mark.parametrize(("key", "_emitted"), P41_RECORDED_CAST_KEY_BASELINE)
    def test_every_cast_key_is_itself_preservation_class(self, key: str, _emitted: str) -> None:
        assert p41_is_preservation_class(key)


class TestTheGeneratedDomainIsSound:
    """The strategies stay inside the preservation class, and the regex shapes are regexes."""

    def test_the_formerly_excluded_backslash_shape_is_preservation_class_and_now_generated(self) -> None:
        """The backslash exclusion was LIFTED, and this is where that is recorded.

        **AMENDED** (`LGR-60c4985-6`, `LGR-5906ec6-2`). It read: *"the exclusion
        is a stated boundary, not an oversight"*, and it proved `"a\\qb"` is
        preservation-class while being excluded, *"because a round-trip-safe
        primitive escapes the backslash — so this shape's VALUE is preserved
        while its BYTES legitimately move"*.

        That reasoning was correct and is now the GENERAL case the two-tier oracle
        handles, so the exclusion no longer buys anything: U+D7A4 showed that
        ~957k other code points in the domain behave the same way, and the domain
        admitted them all along. The backslash was not a boundary, it was the one
        member of the class this file happened to know about. So the assertion is
        kept and inverted: the shape is preservation-class **and it is now inside
        the generated domain**, which strengthens the sweep over exactly the
        characters where `_dq`'s escaping is most interesting.
        """
        assert p41_is_preservation_class("a\\qb")
        # The lifted half, asserted: the character is no longer excluded from the
        # ordinary alphabet's *purpose* — it is drawn as a pair — and such a value
        # is tier 1 only, which is the whole reason the exclusion can go.
        assert "q" not in P41_RECOGNISED_ESCAPE_FOLLOWERS, "`\\q` is the pair shape the units strategy draws"
        assert not p41_is_byte_stable("a\\qb"), "a backslash value is tier 1 only, by construction"

        # And the shapes a plain fence cannot carry, or that carry a RECOGNISED
        # escape, are still correctly rejected — the domain is still exactly
        # "text == value", which is what the amendment did NOT change:
        assert not p41_is_preservation_class('a"b'), "an unescaped quote cannot sit in the fence"
        assert not p41_is_preservation_class("a\nb"), "a real newline does not parse in a plain fence"
        # `a\b` is the D5 shape task 6.1 owns: `"a\b"` parses to a backspace, so
        # text != value and it is NOT preservation-class. This is why the lifted
        # exclusion draws `\` as part of a PAIR with a safe follower rather than
        # adding it to the alphabet.
        assert not p41_is_preservation_class("a\\b")
        assert not p41_is_preservation_class("a\\"), "a trailing backslash does not terminate the fence"

    def test_the_recognised_escape_follower_set_is_exactly_pythons(self) -> None:
        """:data:`P41_RECOGNISED_ESCAPE_FOLLOWERS` is derived, not transcribed.

        Re-derived from Python's own behaviour on every run over ASCII, plus a
        non-ASCII sample, so the set cannot drift silently if Python adds an
        escape sequence. The full domain (all 1,111,997 admitted code points) was
        swept once when the exclusion was lifted and returned exactly these 21;
        ASCII is the part worth paying for on every run, since all 21 are ASCII.

        A follower that Python starts recognising and this set does not would put
        D3 shapes into a preservation sweep — the failure the property's own
        domain assertion would then report as a converter finding.
        """
        # Over the ASCII half of the GENERATED domain — so `"` and the `Cc`
        # controls are out, exactly as `P41_LITERAL_CHARACTERS` has them out. They
        # break `text == value` too, but for an unrelated reason (LF continues the
        # line, CR and NUL make the source unparseable), and counting them here
        # would conflate "Python recognises this escape" with "the fence cannot
        # carry this character at all".
        candidates = [chr(code) for code in range(128) if chr(code) != '"' and unicodedata.category(chr(code)) != "Cc"]
        breaks_it = {follower for follower in candidates if not p41_is_preservation_class("a\\" + follower + "b")}
        assert breaks_it == P41_RECOGNISED_ESCAPE_FOLLOWERS, (
            f"Python recognises {sorted(breaks_it - P41_RECOGNISED_ESCAPE_FOLLOWERS)!r} "
            f"which this set does not, and/or this set claims "
            f"{sorted(P41_RECOGNISED_ESCAPE_FOLLOWERS - breaks_it)!r} which Python does not"
        )
        for follower in ("é", "名", "😀", "\u00a0", "\u2028", "q", "-", " "):
            assert p41_is_preservation_class("a\\" + follower + "b"), follower

    @pytest.mark.parametrize("max_units", [0, 1, 6, 24])
    def test_unit_built_text_is_preservation_class_and_never_ends_in_a_bare_backslash(self, max_units: int) -> None:
        """The unit construction's two invariants, over a hand-built worst case.

        Enumerated rather than generated so the invariant is checked without
        depending on the draw: every backslash is followed by a safe follower, so
        no run of backslashes and no trailing backslash is constructible.
        """
        followers = ["q", "é", "-", " ", "\u00a0", "😀"]
        for follower in followers:
            value = ("x\\" + follower) * max(max_units // 2, 1)
            assert p41_is_preservation_class(value), value
            assert not value.endswith("\\"), value

    @pytest.mark.parametrize("atom", P41_REGEX_ATOMS)
    @pytest.mark.parametrize("quantifier", P41_REGEX_QUANTIFIERS)
    def test_every_quantified_regex_atom_compiles(self, atom: str, quantifier: str) -> None:
        """Validity proved over the grid, so the property need not assert it per example."""
        re.compile(atom + quantifier)

    def test_every_pair_of_quantified_atoms_compiles_and_carries_no_backslash(self) -> None:
        """Concatenation cannot invalidate a pair, and no atom smuggles in a backslash.

        At most one quantifier is attached per atom, so a repeat can never land
        on a repeat — the one way concatenating valid regex fragments produces an
        invalid regex. Enumerated rather than argued: 12 atoms x 4 quantifiers,
        all 2304 ordered pairs.
        """
        quantified = [atom + quantifier for atom in P41_REGEX_ATOMS for quantifier in P41_REGEX_QUANTIFIERS]
        assert not [piece for piece in quantified if "\\" in piece or '"' in piece]
        for left in quantified:
            for right in quantified:
                re.compile("^" + left + right + "$")

    @pytest.mark.parametrize(
        "value",
        [
            "",
            "a",
            "col_1",
            "a b",
            "a.b",
            "Ünïcodé_名",
            "😀",
            "\t",
            "[^0-9]",
            "{k}",
            # Added by the amendment: the non-printable class the two-tier oracle
            # exists for, and the backslash shapes the lifted exclusion admits.
            "\u00a0",
            "\u2028",
            "\u200b",
            "\ud7a4",
            "a\\qb",
            "col\\ name",
            "\\é",
        ],
    )
    def test_the_domain_predicate_accepts_the_shapes_the_strategies_produce(self, value: str) -> None:
        """A cross-check on :func:`p41_is_preservation_class` itself.

        Every one of these is a value the strategies can draw, and every one is
        preservation-class. If the predicate ever rejected one, the property's
        domain assertion would fail on a legitimate draw and read as a converter
        finding when it was a predicate bug.

        The last seven were added by the two-tier amendment: four non-printables
        that were always in the domain and are the class the amendment is for, and
        three backslash shapes from the lifted exclusion.
        """
        assert p41_is_preservation_class(value)


class TestTheByteStableSubdomainIsExactlyWhereDqIsTheIdentity:
    """Tier 2's subdomain is not a hand-drawn boundary — it is checked against the fences.

    This is the one place in the file that reads a production helper. It builds
    **no expectation** from it: every expectation still comes from the frozen
    recorded baseline, so `LGR-28b4d75-5` is untouched. What is checked here is
    the amended oracle's *scope* — that :func:`p41_is_byte_stable` describes
    exactly the set on which the fence helpers leave a value's spelling alone.
    Without this, "printable and backslash-free" would be an assertion in prose.
    """

    def test_dq_is_the_identity_on_exactly_the_byte_stable_characters(self) -> None:
        """Over ASCII plus a non-ASCII sample: `_dq(v) == v` iff `p41_is_byte_stable(v)`.

        The full sweep — all 1,111,997 code points the domain admits — was run
        once when the amendment was made and found **zero disagreement in either
        direction**. ASCII plus the interesting non-ASCII categories is what is
        worth paying for on every run.
        """
        probes = [chr(code) for code in range(128) if chr(code) not in '"\\'] + [
            "é",
            "名",
            "😀",
            "\u00a0",  # Zs, NBSP — non-printable, and the realistic member
            "\u2028",  # Zl, LINE SEPARATOR
            "\u2029",  # Zp
            "\u200b",  # Cf, zero-width space
            "\ud7a4",  # Cn, the falsifying example
            "\U000e0001",  # Cf, astral
            "café",
            "naïve",
            "日本語",
            "ref_col",
            "",
        ]
        disagreements = [
            (value, _dq(value) == value, p41_is_byte_stable(value))
            for value in probes
            if (_dq(value) == value) != p41_is_byte_stable(value)
        ]
        assert not disagreements, (
            f"tier 2's subdomain does not match where `_dq` is the identity: {disagreements!r}. "
            f"If `_dq`'s escaping changed, tier 2's scope has to change with it — amend "
            f"`p41_is_byte_stable` at its definition with the reason, do not widen the tier."
        )

    def test_raw_takes_its_raw_arm_on_every_byte_stable_value(self) -> None:
        """The other fence: `_raw(v) == ("r", v)` for every byte-stable value.

        Tier 2 asserts byte-identity at the two `r"…"` sites as well as the twelve
        plain ones, so it needs `_raw` to be spelling-preserving on the same
        subdomain — otherwise the raw sites would red for a legitimate fallback.
        """
        for value in ["ref_col", "[^0-9]", "^a$", "(ab|cd)+", "é", "😀", "日本語", "", "a b", "a.b"]:
            assert p41_is_byte_stable(value), value
            assert _raw(value) == ("r", value), value


class TestTheValueOracleAndTheByteOracleCatchDifferentThings:
    """Both tiers exist because neither subsumes the other. Pinned, not argued.

    :func:`p41_literal_split` is deliberately blind to a literal's spelling, which
    is what lets tier 1 sweep the whole domain — and is also why tier 2 has to
    stay. These cases are the two directions of that trade, so a later reader does
    not have to rediscover the blind spot or wonder whether one tier is redundant.
    """

    def test_a_pure_spelling_change_is_invisible_to_tier_one(self) -> None:
        """Same values, same skeleton — so tier 1 passes on a changed FENCE."""
        plain = 'x = df["p"].str.replace("[^0-9]", "", regex=True)'
        raw = 'x = df["p"].str.replace(r"[^0-9]", "", regex=True)'
        escaped = 'x = df["p"].str.replace("[^0\\u002d9]", "", regex=True)'
        assert p41_literal_split(plain) == p41_literal_split(raw)
        assert p41_literal_split(plain) == p41_literal_split(escaped)
        # …and tier 2's comparison DOES see all three apart:
        assert plain != raw and plain != escaped

    def test_a_value_change_is_visible_to_tier_one_even_when_the_skeleton_matches(self) -> None:
        """The half tier 2 cannot supply: value drift inside an identical shape."""
        recorded = 'x = df["a_col"]'
        wrong_value = 'x = df["a_col_REGRESSION"]'
        recorded_skeleton, recorded_values = p41_literal_split(recorded)
        wrong_skeleton, wrong_values = p41_literal_split(wrong_value)
        assert recorded_skeleton == wrong_skeleton, "identical shape, so only the values differ"
        assert recorded_values != wrong_values

    def test_a_skeleton_change_is_visible_even_when_every_value_matches(self) -> None:
        """The half tier 1 keeps from the original byte comparison."""
        recorded = 'df_transformed = df.copy(); df_transformed["a"] = df["b"]'
        no_copy = 'df_transformed = df; df_transformed["a"] = df["b"]'
        loc_form = 'df_transformed = df.copy(); df_transformed.loc[:, "a"] = df["b"]'
        recorded_skeleton, recorded_values = p41_literal_split(recorded)
        for moved in (no_copy, loc_form):
            moved_skeleton, moved_values = p41_literal_split(moved)
            assert moved_values == recorded_values, moved
            assert moved_skeleton != recorded_skeleton, moved

    def test_literal_split_reports_values_in_source_order_and_a_readable_skeleton(self) -> None:
        """The mechanism, pinned on a hand-written case."""
        skeleton, values = p41_literal_split('df_transformed = df.rename(columns={"old": "new"})')
        assert values == ("old", "new")
        assert skeleton == "df_transformed = df.rename(columns={@@P41LIT0@@: @@P41LIT1@@})\n"

    def test_literal_split_survives_the_line_separator_character_inside_a_literal(self) -> None:
        """U+2028 is in the generated domain and `str.splitlines` splits on it.

        `tokenize` does not, so spans are computed over `\\n` only. A regression
        to `splitlines` would misalign every literal after the first U+2028 —
        silently, since the skeleton would still look plausible.
        """
        skeleton, values = p41_literal_split('x = df["a\u2028b"] + df["tail"]')
        assert values == ("a\u2028b", "tail")
        assert skeleton == "x = df[@@P41LIT0@@] + df[@@P41LIT1@@]\n"

    def test_literal_split_fails_loudly_on_an_emission_that_does_not_parse(self) -> None:
        """`tasks.md` 4.1's hazard: a substitution-list `_dq` emits a SyntaxError.

        This file is the only place that would notice, so the failure is an
        `AssertionError` naming the problem rather than a raw `TokenError`.
        """
        with pytest.raises(AssertionError, match="does not tokenize"):
            p41_literal_split('x = df["a\nb"]')


class TestEverySiteIsAnchoredAtAReferenceValue:
    """The deterministic floor: all 15 sites, one fixed value each, every run.

    Hypothesis reaching every site over 300 examples is overwhelmingly likely
    and not guaranteed, and `design.md` §9 Q6 is the reason this file does not
    rely on likely. These cases are the same recorded-baseline comparison the
    property makes, pinned at one value per site so site coverage does not
    depend on the draw.

    This is NOT task 3.1's file. Task 3.1 pins one full emission per site by
    hand for legibility; this class pins the recorded TEMPLATE at one value, so
    a template that was mis-transcribed fails here with the site named even if
    the generative half happens not to draw that site.
    """

    #: One fixed value per kind. Ordinary shapes deliberately — the awkward ones
    #: are the generative half's job; this class's job is per-site coverage.
    P41_REFERENCE_VALUES = {
        "column": "ref_col",
        "pattern": "[^0-9]",
        "text": "-",
    }

    @pytest.mark.parametrize("row", P41_RECORDED_SITE_BASELINE, ids=[r.site for r in P41_RECORDED_SITE_BASELINE])
    def test_template_site_emits_the_recorded_template(self, row: P41SiteTemplate) -> None:
        value = self.P41_REFERENCE_VALUES[row.kind]
        source = row.source_template.replace(P41_VALUE_SLOT, value)
        expected = row.emitted_template.replace(P41_VALUE_SLOT, value)
        assert p41_emitted_body(source) == expected, f"site {row.site} ({row.what}) moved"

    @pytest.mark.parametrize(
        ("java_format", "emitted"), P41_RECORDED_STRFTIME_BASELINE, ids=[f for f, _ in P41_RECORDED_STRFTIME_BASELINE]
    )
    def test_strftime_site_emits_the_recorded_row(self, java_format: str, emitted: str) -> None:
        source = P41_STRFTIME_SOURCE_TEMPLATE.replace(P41_VALUE_SLOT, java_format)
        assert p41_emitted_body(source) == emitted, f"site {P41_STRFTIME_SITE} moved for {java_format!r}"

    @pytest.mark.parametrize(
        ("key", "emitted"), P41_RECORDED_CAST_KEY_BASELINE, ids=[k for k, _ in P41_RECORDED_CAST_KEY_BASELINE]
    )
    def test_cast_key_site_emits_the_recorded_row(self, key: str, emitted: str) -> None:
        source = P41_CAST_KEY_SOURCE_TEMPLATE.replace(P41_VALUE_SLOT, key)
        assert p41_emitted_body(source) == emitted, f"site {P41_CAST_KEY_SITE} moved for {key!r}"
