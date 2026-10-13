# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""PySpark-to-Pandas converter for Code Interpreter testing.

Converts AI-generated PySpark transform blocks into pandas equivalents
so they can execute inside the AgentCore Code Interpreter (which has
pandas but not PySpark).

P28 R10.2/R10.3: conversion is all-or-nothing. A construct that cannot
be converted raises :class:`ConversionError` naming the construct and
the offending line — unconverted lines are never silently commented
out. A successful conversion returns the pandas code together with a
``conversion_report`` documenting what was converted.

p32 2.11 (design.md C6) — **the block is parsed as Python, not matched
against physical lines.** The predecessor was ~45 ordered regex
substitution pairs (`LGR-d2e9878-1`, backfilled: it shipped with 26)
followed by a *residual scan over the already-rewritten text*. That
architecture produced four distinct defects, all of them structural
rather than a missing pattern (`bugfix.md` 1.7-1.10):

* a statement chaining two ``.withColumn`` calls matched the single-call
  pattern, whose greedy capture swallowed the rest of the chain;
* ``otherwise(col(...))`` and a second ``.when(...)`` did not match a
  pattern that required quoted literals in every position;
* ``.withColumn`` chained with ``.select`` composed into text no residual
  pattern named, so conversion *succeeded* and emitted a ``[["a"]]``
  subscript applied to a Series — a silent wrong answer, which the
  residual scan is constitutionally unable to catch because it inspects
  output that has already been mangled.

So this module now walks the call chain of each parsed statement. An
unrecognised construct is a *translation* failure at the node that
carries it, and a block that is not valid Python is a ``SyntaxError``
surfaced as ``ConversionError`` (`LGR-8f3f9e1-1`, backfilled: bare
``except`` was removed repo-wide and is not reintroduced here).

**A fifth instance of the same defect survived that rewrite**, because
the rewrite changed *how* constructs are recognised and not *which names*
are enumerated: ``posexplode`` was in neither frozenset, so
``withColumn("t", posexplode(col("tags")))`` converted and emitted
``posexplode(df["tags"])``, which raised ``NameError`` in the Code
Interpreter. Enumerating one more name would only have moved the hole, so
an unrecognised bare name in FUNCTION position now refuses by default —
see the vocabulary comment below and ``_function_call``'s last branch.
Method position has no equivalent default and is discussed there.
``tests/unit/lambdas/test_code/test_converter_vocabulary_closure_prop.py``
holds both halves generatively.

What deliberately did NOT change (`bugfix.md` 3.1-3.10, pinned
byte-exactly by ``tests/unit/lambdas/test_code/test_converter_equivalence_baseline.py``):

* the public surface — ``convert()``, the ``conversion_report`` keys, the
  ``import pandas as pd`` / ``import numpy as np`` header, and
  ``ConversionError(construct, line, line_number)``. The production
  consumer is ``code_interpreter_runner.py`` (import ``:32``, ``convert``
  ``:83``, ``ConversionError`` at ``:84``/``:385``), so the success shape
  and the exception shape are both contracts (3.9). p40 added a fourth
  ``ConversionError`` field, ``reason``, **additively**: optional, keyword,
  defaulted to ``None`` and last, so the three-positional construction and
  the rendered ``str(exc)`` are byte-identical to what this line has always
  described (p40 `bugfix.md` 3.10, design B9);
* ``withColumn``'s new-frame copy semantics, and inner ``col()``
  references bound to the statement's SOURCE frame so a later line sees
  an earlier line's effect (3.1, 3.2);
* ``select``'s honest refusal when an output column name cannot be
  resolved — never a mangled ``[[...]]`` subscript (3.6). That resolution
  still runs through :func:`_select_output_column`, over the *emitted*
  pandas text, so the fence and its characterization tests keep their
  meaning;
* explicit ``to_date``/``to_timestamp`` formats survive (3.5);
* nested string-function composition (`LGR-5c33c04-1`, backfilled) —
  regressing it would reintroduce this spec's own bug class (3.4);
* blank lines and comments pass through verbatim, and identifiers inside
  a comment are not constructs (3.10);
* refusal is all-or-nothing for the whole block (3.8).
"""

import ast
import re
import textwrap
import warnings

#: The refusal reason for p40's receiver-shape fence: a pandas member that only
#: exists on a Series was about to be applied to a scalar emission.
#:
#: **Defined HERE, in the converter, and ALIASED by the runner** — the dependency
#: runs the other way. ``code_interpreter_runner.py:39`` does
#: ``from pyspark_to_pandas import ConversionError, PySpark2PandasConverter``, so
#: the converter cannot import the runner's two existing reason constants
#: (``NOT_VERIFIED_REASON_PERMANENT`` / ``NOT_VERIFIED_REASON_NOT_YET``,
#: ``code_interpreter_runner.py:62-67``) without a cycle. The producer of the
#: reason therefore owns the string and the reader aliases it: one source of
#: truth, and a rename cannot leave the two halves disagreeing silently.
REFUSAL_REASON_SCALAR_LITERAL = "scalar_literal_receiver"


class ConversionError(Exception):
    """A PySpark construct could not be converted to pandas.

    Raised instead of silently dropping (commenting out) the offending
    line — a conversion that drops any transform must fail the test
    step with the construct named (P28 R10.2).

    **Why the reason travels on the EXCEPTION and not on the construct name**
    (p40 R4). Downstream, ``code_interpreter_runner.classify_conversion_refusal``
    maps a **construct name** to a non-verification reason. The receiver-shape
    fence's constructs — ``cast``, ``trim``, ``lower``, ``upper``,
    ``regexp_replace``, ``concat_ws``, ``to_date``, ``alias`` — are the *same
    names* a legitimate not-yet-expressible refusal uses: ``cast`` with an
    unrecognised target and ``cast`` applied to a literal are both refusals
    named ``cast``. So the construct cannot carry a third reason without
    relabelling the first kind of refusal as the second. The reason rides on the
    exception instead, and the runner prefers it over the construct-keyed
    classification when it is present.

    ``reason`` is **optional, defaulted and last**, which is what keeps
    `bugfix.md` 3.10 / design B9 true: ``ConversionError(construct, line,
    line_number)`` still constructs exactly as before at the 13 call sites and
    23 construction/reference sites design Appendix A enumerates, and
    ``str(exc)`` is unchanged — the reason is a separate field, never part of
    the rendered message an operator reads.
    """

    def __init__(self, construct: str, line: str, line_number: int, reason: "str | None" = None) -> None:
        self.construct = construct
        self.line = line
        self.line_number = line_number
        self.reason = reason
        super().__init__(f"unconvertible PySpark construct '{construct}' on line {line_number}: {line}")


class _Unconvertible(Exception):
    """Internal signal: a node could not be translated.

    Carries the construct name and the *physical* line it sits on;
    :meth:`PySpark2PandasConverter.convert` turns it into the public
    :class:`ConversionError` once it can attach the offending source line.

    ``reason`` is the same optional field, for the same reason, and it is
    carried here so the refusal that raises deep inside the translator can name
    *why* without the intervening frames having to know: ``convert``'s ``fail``
    closure forwards it verbatim onto the public exception. ``None`` — every
    refusal that predates p40 — leaves the downstream classification exactly
    where it was, keyed on the construct.
    """

    def __init__(self, construct: str, lineno: int, reason: "str | None" = None) -> None:
        self.construct = construct
        self.lineno = lineno
        self.reason = reason
        super().__init__(construct)


def _java_date_format_to_strftime(fmt: str) -> str:
    """Map a Spark/Java SimpleDateFormat pattern to a strftime pattern.

    Handles the common tokens produced by the transform-generation prompt
    (``dd-MM-yyyy``, ``yyyy-MM-dd HH:mm:ss``, ...). Unknown letter runs
    pass through unchanged (treated as literals).
    """
    token_map = {
        "yyyy": "%Y",
        "yy": "%y",
        "MM": "%m",
        "M": "%m",
        "dd": "%d",
        "d": "%d",
        "HH": "%H",
        "H": "%H",
        "hh": "%I",
        "h": "%I",
        "mm": "%M",
        "m": "%M",
        "ss": "%S",
        "s": "%S",
        "SSS": "%f",
        "a": "%p",
        "EEEE": "%A",
        "EEE": "%a",
    }
    out: list[str] = []
    i = 0
    while i < len(fmt):
        ch = fmt[i]
        if ch.isalpha():
            j = i
            while j < len(fmt) and fmt[j] == ch:
                j += 1
            run = fmt[i:j]
            out.append(token_map.get(run, run))
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# ── literal fences (p41 D1/D3/D5) ─────────────────────────────────────
# `_string` returns the literal's VALUE rather than its source text, so every
# call site that embeds that value back into an emitted literal owns escaping it
# for the fence it emits into. One rule, applied per site — not two contracts.
#
# This module emits string literals behind exactly two fences, so there are
# exactly two helpers: `_dq` for the twelve plain `"…"` sites and `_raw` for the
# two `r"…"` regex sites (`regexp_replace`'s pattern and `rlike`'s pattern).
# `_cast`'s string target key is a dict lookup into `_CAST_TARGETS`, not an
# emission, so it takes neither.
#
# Both helpers are defined by a ROUND-TRIP rather than by a list of
# substitutions: for every value ``v``, evaluating the literal they build must
# give back ``v``. The naive rule — escape `\` and `"` and nothing else — is not
# merely incomplete, it emits code that does not parse: a value carrying a line
# break yields `"a<LF>b"`, a SyntaxError in the generated pandas.


def _dq(value: str) -> str:
    """Body for a plain ``"…"`` fence, escaped so the fence carries *value*.

    Defined by its round-trip, not by a substitution list: for every value
    ``v``, ``ast.literal_eval('"' + _dq(v) + '"') == v``.

    **The primitive underneath is :func:`repr`**, because Python's ``repr`` of a
    ``str`` is by construction a literal that evaluates back to that ``str`` —
    it escapes the backslash, the quote it chose, and every control character
    (``\\n``, ``\\r``, ``\\t``, ``\\x00`` …). Escaping only ``\\`` and ``"`` by
    hand is NOT sufficient, and it fails in the worse direction: a value
    carrying a line break emits ``"a<LF>b"``, which is a ``SyntaxError`` in the
    generated pandas rather than a wrong string, and the same hole swallows
    every other control character.

    **The one thing ``repr`` does that has to be undone is its choice of
    quote.** ``repr`` prefers ``'…'`` and switches to ``"…"`` only when the
    value contains ``'`` and no ``"`` — and these emission sites are all ``"``
    fences. So when ``repr`` chose ``'``, two rewrites put the body into this
    fence: a ``"`` came back bare and is escaped, and an ``'`` came back as
    ``\\'``, which is legal but noisy inside a ``"`` fence, so it is unescaped.
    Both are value-preserving, and neither can manufacture the other — the
    backslash inserted before a ``"`` is never followed by ``'``, and every
    backslash run ``repr`` emits is already doubled, so the ``\\'`` scan cannot
    straddle one.

    Non-ASCII is deliberately left as itself rather than escaped (``ascii()``
    would spell ``café`` as ``caf\\xe9``): the emitted module is UTF-8 Python,
    and a unicode column name should stay legible in the code a reviewer reads.
    On every ``\\w`` name — which is all four ``select``-resolution matchers can
    match (`_SELECT_STRING_ARG` and friends below) — this function is the
    identity, so it cannot move a name those matchers resolve.
    """
    spelled = repr(value)
    if spelled.startswith('"'):
        return spelled[1:-1]
    return spelled[1:-1].replace('"', '\\"').replace("\\'", "'")


#: Characters that cannot appear literally inside a single-line ``r"…"`` fence.
#:
#: The first three are the ones the design names, and each is fatal rather than
#: merely wrong — verified by attempting the fence: ``"`` closes it, and LF or
#: CR ends the line, so ``r"a<X>b"`` raises ``SyntaxError: unterminated string
#: literal`` and the generated pandas does not parse at all.
#:
#: ``\x00`` is a fourth, found by running the round-trip over every code point
#: instead of reasoning up front: ``r"a<NUL>b"`` raises ``SyntaxError: source
#: code string cannot contain null bytes``. It is the only code point below
#: U+3000 that the three-character test gets wrong. ``_dq`` spells it ``\x00``
#: and parses, so a NUL-carrying value simply takes the fallback.
#:
#: TAB is deliberately NOT here: it is legal unescaped in a raw fence and
#: round-trips through one, so excluding it would push values onto the fallback
#: for no reason.
#:
#: This tuple is a membership test over CHARACTERS whose presence breaks the
#: emitted LITERAL. There is a fifth exclusion below that this tuple cannot
#: express, because it is a RANGE and because it breaks the emitted MODULE
#: rather than the literal — see `_raw_spellable`.
_RAW_UNSPELLABLE = ('"', "\n", "\r", "\x00")


def _raw_spellable(value: str) -> bool:
    """Whether ``r"<value>"`` is a literal that parses AND means *value*."""
    if any(ch in value for ch in _RAW_UNSPELLABLE):
        return False
    # A raw fence cannot carry a value that a UTF-8 source file cannot express,
    # which in practice means a LONE SURROGATE (U+D800–U+DFFF). This is the
    # FIFTH exclusion, and it fails differently from the four above: those make
    # the literal a `SyntaxError`, this one makes the whole emitted module
    # unencodable —
    #
    #     UnicodeEncodeError: 'utf-8' codec can't encode character '\ud800' …
    #
    # so the module can be neither written to a file nor shipped to the code
    # interpreter. Nothing runs, and the failure surfaces at the write rather
    # than at the literal.
    #
    # It is REACHABLE FROM PLAIN-ASCII SOURCE: `ast.parse('x = "\\ud800"')`
    # yields a one-character string that *is* a lone surrogate, so a model
    # writing a `\uD800`-`\uDFFF` escape inside a regex reaches it without the
    # source ever leaving ASCII. `_dq` handles the same value correctly (`repr`
    # spells it back as the `\ud800` escape, so the body stays pure ASCII),
    # which is why only the two `r"…"` regex sites were ever exposed and the
    # twelve plain-fence sites never were.
    #
    # Tested by the operation that actually fails rather than by enumerating the
    # range: `_RAW_UNSPELLABLE` would need 2048 members to say this, and would
    # then be asserting the range's EXTENT where its CAUSE is available. Swept
    # over all 0x110000 code points, `.encode("utf-8")` fails for exactly the
    # 2048 contiguous points U+D800–U+DFFF and no others, so the two formulations
    # are co-extensive and this one cannot drift.
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    # A raw fence also cannot END in an odd run of backslashes: the last one
    # escapes the closing quote, so `r"a\"` is `SyntaxError: unterminated string
    # literal … perhaps you escaped the end quote?`. An EVEN run is fine — a raw
    # fence keeps every backslash it contains, which is the whole reason a regex
    # is worth emitting into one.
    return (len(value) - len(value.rstrip("\\"))) % 2 == 0


def _raw(value: str) -> tuple[str, str]:
    """``(fence_prefix, body)`` for a regex emission carrying *value*.

    ``("r", value)`` when the value is spellable raw, which is the arm every
    measured input takes: the pattern reaches pandas as the same characters the
    model wrote, and the emitted line is byte-identical to what these two sites
    have always emitted for such a pattern.

    Otherwise ``("", _dq(value))`` — a plain fence carrying the **same regex
    value**. That arm is what makes a lone surrogate emittable at all: `repr`
    spells it as an ASCII ``\\ud800`` escape, so the module encodes, where the
    raw arm would have written the code point itself and left the module
    unwritable (see `_raw_spellable`'s fifth exclusion).

    **Why the fallback is safe, and not just smaller.** The regex engine is
    handed a *value*; it never sees the literal's spelling. So
    ``.str.replace(r"[^\\d]", …)`` and ``.str.replace("[^\\\\d]", …)`` pass the
    identical five-character pattern ``[^\\d]`` to ``re`` and behave
    identically. Falling back changes the spelling of the literal and not the
    meaning of the regex, which is the one property that makes a third code path
    acceptable here. Nothing downstream reads a pattern's spelling: the two
    text-reading fences in this module (`_select_output_column` and
    `_is_scalar_emission`) read select arguments and emission receivers, neither
    of which is a regex argument.

    **It is a third code path and it is currently unreachable from any measured
    input** — of the 79 baseline conversion/refusal cases exactly one carries a
    backslash at all, and its value (``@example\\.com$``) is raw-spellable, so it
    takes the raw arm. That unreachability is asserted rather than assumed: the
    mid-way checkpoint re-runs the baseline byte-exactly, so a value that starts
    reaching the fallback shows up as a moved pin.
    """
    if _raw_spellable(value):
        return ("r", value)
    return ("", _dq(value))


# ── select() argument resolution (P28b R1a, p32 3.6) ──────────────────
# These operate on EMITTED pandas text, not on PySpark: by the time a
# select argument's output column name has to be resolved, the argument
# has already been translated. That is what makes `.alias("y")` (emitted
# as `.rename("y")`) name the column.
_SELECT_STRING_ARG = re.compile(r'^"(\w+)"$')
_SELECT_COL_ARG = re.compile(r'^col\(\s*"(\w+)"\s*\)$')
_SELECT_SUBSCRIPT_ARG = re.compile(r'^\w+\[\s*"(\w+)"\s*\]$')
_SELECT_RENAME_SUFFIX = re.compile(r'\.rename\(\s*"(\w+)"\s*\)$')


def _select_output_column(arg: str) -> "str | None":
    """Resolve the output column name of one ``select()`` argument.

    A trailing ``.rename("y")`` (the converted ``.alias("y")``) names the
    column; plain ``col("x")`` / ``src["x"]`` / ``"x"`` references keep their
    own name. Anything else is unresolvable (``None``), which the caller turns
    into an honest refusal rather than a guessed name (p32 3.6).
    """
    renamed = _SELECT_RENAME_SUFFIX.search(arg)
    if renamed:
        return renamed.group(1)
    for pattern in (_SELECT_STRING_ARG, _SELECT_COL_ARG, _SELECT_SUBSCRIPT_ARG):
        match = pattern.match(arg)
        if match:
            return match.group(1)
    return None


# ── the receiver-shape fence (p40 R2/R3) ──────────────────────────────
# Second decision in this module made over EMITTED pandas text rather than
# over the PySpark AST — `_select_output_column` above is the first, and the
# reason is the same one, spelled out in `_is_scalar_emission`'s docstring.
#
# NOTE for whoever reads this next: no ledger record governs this module's
# translation rules. `chesterton why` on this file returns only `60c4985`'s
# seven records, none of which is about the converter, and `LGR-5c33c04-1` —
# cited twice in this very file (the module docstring, and the trim/lower/upper
# branch) — is NOT in the ledger. These comments and
# `tests/unit/lambdas/test_code/` are the only fence a future reader gets, so
# they carry the reasoning rather than pointing at a record that would carry it.


#: Members that exist on a pandas Series and NOT on a Python scalar.
#: Applying one to a scalar is the whole of this spec's defect class.
#:
#: Measured, both pandas 2.2.3 and 3.0.5 (p40 design §5, F5/F9): `None.astype(str)`,
#: `"x".astype(str)`, `"x".str.strip()`, `"x".rename("z")` and
#: `pd.to_datetime("2020-01-01", …).dt.date` every one raise `AttributeError`. An
#: INTEGER receiver is strictly worse — `0.astype(str)` is a `SyntaxError: invalid
#: decimal literal`, so the Code Interpreter loses the whole cell (every transform
#: in the block, including the ones that converted correctly) rather than one
#: statement (p40 verification-notes, task 3.4).
#: ``notna``/``isna`` joined the tuple with p42 gap 2's null-predicate rule
#: (:data:`_NULL_PREDICATES`), on the same measured ground: ``None.notna()`` and
#: ``"x".isna()`` both raise ``AttributeError``, and an INTEGER receiver is again
#: strictly worse — ``0.isna()`` is a ``SyntaxError: invalid decimal literal``.
#: They are members of a Series, not module-level ``pd.isna``/``pd.notna``, and
#: this module emits neither module-level form.
_SERIES_ONLY_MEMBERS = ("astype", "str", "dt", "rename", "notna", "isna")

#: The two pandas helpers this module emits that accept a SCALAR and hand back a
#: scalar: `pd.to_numeric(0, errors="coerce")` → `0`, and
#: `pd.to_datetime("2020-01-01", …)` → a `Timestamp` (p40 design §5, F1/F2/F5).
#: A call to either over a scalar first argument is therefore itself a scalar
#: emission — which is what puts `_cast`'s numeric branch and `to_date`'s
#: `.dt.date` inside this predicate's reach instead of outside it.
_SCALAR_PRESERVING_HELPERS = ("to_numeric", "to_datetime")


def _is_scalar_emission(text: str) -> bool:
    """True when *text* is emitted pandas that evaluates to a SCALAR.

    Decided over the EMITTED pandas text, not over the PySpark AST, for the
    same reason :func:`_select_output_column` (``:163-178``) resolves a select
    argument's name over emitted text: by the time a member is appended, the
    receiver has already been translated, so the emission is the thing that
    will actually run in the Code Interpreter. Reading the PySpark side would
    mean re-deriving what the translator just decided, and getting a different
    answer than the emission whenever the two drift.

    That choice is also what makes p40 R3 true. The refusal is keyed on the
    *shape of what was emitted*, never on an enumeration of construct names, so
    a construct added later that composes a scalar into one of
    :data:`_SERIES_ONLY_MEMBERS` refuses without anyone remembering to list it.

    Recognised scalar forms are a small CLOSED set — see :func:`_is_scalar_node`.
    Everything else is treated as NOT scalar.

    **The default is deliberately PERMISSIVE, and that is the one place this
    fence differs from `_function_call`'s refuse-by-default (``:692-717``).**
    That branch guards a set of **names**: a bare name inside a transform block
    can only have come from `pyspark.sql.functions`, so an unrecognised one is a
    converter gap and refusing it is safe. Here the subject is an **emission
    form**, and an unrecognised emission form may be any legitimate Series
    expression — a subscript, a `.str` chain, an `np.where(...)`, a
    `pd.Series(...)` broadcast. Refusing by default would refuse conversions
    that work today, which is a worse failure than the one being fixed. So an
    unrecognised form is assumed to be a Series and the guard stays out of its
    way.

    The closure this buys is therefore over *emission forms*, not over
    constructs: the set grows only when a translation rule starts emitting a new
    kind of scalar. That is the one class the permissive default leaves open, and
    it is closed in CI rather than in production, by the generative property in
    `tests/unit/lambdas/test_code/` that re-checks every emission for a
    Series-only member applied to a scalar sub-expression (p40 task 12.1).

    Text that does not parse is NOT scalar: nothing can be claimed about a form
    this predicate cannot read, and the permissive default applies to it too.
    """
    try:
        parsed = ast.parse(text.strip(), mode="eval")
    except SyntaxError:
        return False
    return _is_scalar_node(parsed.body)


def _is_scalar_node(node: ast.AST) -> bool:
    """The closed set of emitted forms recognised as evaluating to a scalar.

    Four forms, and nothing else:

    * an ``ast.Constant`` — what ``lit(<const>)`` emits, the constant's verbatim
      source (``_function_call``'s ``lit`` branch, ``:654-657``);
    * a unary minus over a scalar — ``_expr``'s ``USub`` branch (``:613-614``);
    * a ``BinOp`` whose **both** operands are scalars — ``_binop`` (``:632-636``);
      a mixed `scalar + Series` is a Series, so it is not scalar here;
    * ``pd.to_numeric(...)`` / ``pd.to_datetime(...)`` over a scalar first
      argument — :data:`_SCALAR_PRESERVING_HELPERS`.

    Everything else is NOT scalar, including the three forms most likely to be
    mistaken for one: a subscript (``df["a"]`` — a Series), any other method or
    function call (``df["a"].str.strip()``, ``np.where(...)`` — a Series by
    construction), and ``pd.Series(...)`` (a Series by name). See
    :func:`_is_scalar_emission` for why that default is permissive rather than
    strict.
    """
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        return _is_scalar_node(node.operand)
    if isinstance(node, ast.BinOp):
        return _is_scalar_node(node.left) and _is_scalar_node(node.right)
    if isinstance(node, ast.Call):
        func = node.func
        return (
            isinstance(func, ast.Attribute)
            and func.attr in _SCALAR_PRESERVING_HELPERS
            and isinstance(func.value, ast.Name)
            and func.value.id == "pd"
            and bool(node.args)
            and _is_scalar_node(node.args[0])
        )
    return False


# ──────────────────────────────────────────────────────────────────────
# The construct vocabulary.
#
# Split three ways, because the three sets answer different questions:
#   * SUPPORTED_*   — this module has a translation rule.
#   * UNSUPPORTED_* — a PySpark construct with no single-frame pandas
#                     equivalent (p32 3.7) or no rule yet. Naming one of
#                     these is a refusal, not a silent pass-through.
#   * everything else: position-dependent, and NOT symmetric.
#     - In FUNCTION position an unrecognised bare name refuses, whether or
#       not it is enumerated (see `_Translator._function_call`'s last
#       branch). A bare name inside a transform block can only have come
#       from `pyspark.sql.functions`, so passing one through emitted an
#       undefined name and turned a conversion that "succeeded" into a
#       `NameError` in the Code Interpreter — 2.10's failure mode.
#     - In METHOD position it still passes through, because a receiver can
#       be any object and recognising `<receiver>.<attr>(...)` would mean
#       inferring the receiver's type. `_UNSUPPORTED_METHODS` is the only
#       fence there, so that set has to be maintained by hand.
#
# Membership in UNSUPPORTED_FUNCTIONS therefore no longer decides *whether*
# a function refuses — it decides how well the refusal reads: which
# construct gets named when a statement carries several (`_occurrences`
# picks the leftmost), and whether the name appears in
# `pyspark_templates/README.md`'s published vocabulary.
# ──────────────────────────────────────────────────────────────────────

#: Functions with a translation rule. ``when`` is here because a ``when(...)``
#: expression is translated as a whole — with its ``.otherwise(...)`` when the
#: source supplies one, and with a null fallback when it does not
#: (:data:`_WHEN_NULL_FALLBACK`).
_SUPPORTED_FUNCTIONS = frozenset(
    {"col", "lit", "trim", "lower", "upper", "to_date", "to_timestamp", "regexp_replace", "concat_ws", "when"}
)

#: Methods with a translation rule (position-dependent — see
#: :meth:`_Translator._chain`).
_SUPPORTED_METHODS = frozenset(
    {
        "withColumn", "withColumnRenamed", "select", "drop", "filter",
        "cast", "alias", "otherwise", "when", "isNull", "isNotNull", "rlike",
    }
)  # fmt: skip

#: p32 3.7's list plus the rest of the multi-frame / multi-row surface — none of
#: these is expressible as a single-frame pandas transform. Every one refuses,
#: naming itself, which is what `code_interpreter_runner.classify_conversion_refusal`
#: reads to tell 2.23's "permanent" reason from its "not yet" one.
#:
#: ``posexplode``, ``array_contains``, ``spark_count`` and ``spark_max`` are here
#: because the surrounding surfaces offer them and this module did not enumerate
#: them, so they fell through :meth:`_Translator._function_call` and reached the
#: Code Interpreter as a bare undefined name — a conversion that *succeeded* and
#: then raised ``NameError``, which is 2.10's failure mode by the same mechanism
#: as `bugfix.md` 1.10. ``spark_count``/``spark_max`` are the aliases the four
#: ``glue_job_*.py.j2`` scaffolds import (``count as spark_count``,
#: ``max as spark_max``) and that ``transform_generation.j2:24`` tells the model
#: to use; ``array_contains`` is imported by every scaffold; ``posexplode`` is
#: ``explode``'s sibling.
_UNSUPPORTED_FUNCTIONS = frozenset(
    {
        "udf", "expr", "initcap", "substring", "coalesce", "split", "length",
        "regexp_extract", "concat", "date_format", "datediff", "year", "month",
        "dayofmonth", "current_date", "current_timestamp", "struct", "explode",
        "posexplode", "array", "array_contains", "collect_list", "collect_set",
        "count", "spark_count", "sum", "avg", "min", "max", "spark_max",
        "row_number", "rank", "dense_rank", "lag", "lead", "Window",
    }
)  # fmt: skip

#:
#: ``count`` is here as well as in :data:`_UNSUPPORTED_FUNCTIONS` because the two
#: positions are separate lookups: without it, ``df.count()`` passed through to
#: pandas' ``DataFrame.count()``, which counts non-null values **per column**
#: rather than rows — a silent wrong answer rather than a refusal. ``collect``,
#: ``writeTo``, ``createOrReplaceTempView``, ``cache`` and ``unpersist`` are the
#: Spark-only frame methods the scaffolds use around the transform block; each
#: passed through to a pandas frame that has no such attribute.
_UNSUPPORTED_METHODS = frozenset(
    {
        "groupBy", "agg", "join", "crossJoin", "union", "unionAll", "unionByName",
        "repartition", "coalesce", "distinct", "orderBy", "sort", "dropDuplicates",
        "rdd", "na", "over", "partitionBy", "count", "collect", "writeTo",
        "createOrReplaceTempView", "cache", "unpersist",
    }
)  # fmt: skip

#: Bare names that mean "a Spark entry point", i.e. not a dataframe.
#:
#: `F` and `functions` are also the two spellings of the *function namespace*,
#: and in CALL position that is a different thing — see
#: :func:`_aliased_function`, which resolves `F.<name>(...)` to `<name>` before
#: this set is consulted. All six members stay: a bare alias outside call
#: position is still a Spark entry-point reference with no pandas meaning
#: (p41 2.6, `design.md` §5.4).
_UNSUPPORTED_NAMES = frozenset({"spark", "sc", "sqlContext", "StructField", "F", "functions"})

_TYPE_NAME = re.compile(
    r"^(?:String|Integer|Long|Double|Float|Boolean|Struct|Array|Map|Date|Timestamp|Decimal|Short|Byte|Binary|Null)Type$"
)

#: ``.cast(...)`` targets → the emitted pandas conversion. Keyed by the
#: type constructor name and by the string literal spelling.
_CAST_NUMERIC_INT = ("numeric", '.astype("Int64")')
_CAST_NUMERIC_FLOAT = ("numeric", "")

#: The string cast, shared by both spellings of the target (``StringType`` and
#: ``string``) — the NULLABLE `"string"` dtype, not the `str` builtin, and the
#: spelling is MEASURED rather than preferred (p40 R7, design §4.3, `F6`/`F10`):
#:
#:   pd.Series(["p", None]).astype(str)        pandas 2.2.3 → ['p', 'None'], 0 nulls
#:                                            pandas 3.0.5 → ['p', nan],   1 null
#:   pd.Series(["p", None]).astype("string")   both majors  → ['p', <NA>],  1 null
#:
#: Spark's string cast leaves a null null, so `astype(str)` disagreed with Spark
#: on pandas 2 by writing the literal text "None" into null cells and reporting
#: ZERO nulls — and the harness injects ~5% nulls into every generated column
#: (`code_interpreter_runner._build_test_data_code:449`), so on a pandas-2
#: interpreter every string-cast column reported an inflated `completeness`.
#: `"string"` is the only spelling that agrees with Spark on both majors, which
#: also removes the checker's dependence on an interpreter pandas version nobody
#: in this repo pins (design §5, N1).
#:
#: `BooleanType` / `boolean` deliberately keep `astype(bool)`: that IS a real
#: divergence from Spark (a null becomes `False`, design §5 N3b), but fixing it
#: needs Spark semantics work this spec cannot verify locally, so it is pinned as
#: characterization only (task 2.1) and left alone here.
_CAST_STRING_NULLABLE = ("astype", '"string"')
_CAST_TARGETS: dict[str, tuple[str, str]] = {
    "IntegerType": _CAST_NUMERIC_INT,
    "LongType": _CAST_NUMERIC_INT,
    "ShortType": _CAST_NUMERIC_INT,
    "int": _CAST_NUMERIC_INT,
    "integer": _CAST_NUMERIC_INT,
    "long": _CAST_NUMERIC_INT,
    "bigint": _CAST_NUMERIC_INT,
    "DoubleType": _CAST_NUMERIC_FLOAT,
    "double": _CAST_NUMERIC_FLOAT,
    "float": _CAST_NUMERIC_FLOAT,
    "decimal": _CAST_NUMERIC_FLOAT,
    "FloatType": ("numeric", '.astype("float32")'),
    "StringType": _CAST_STRING_NULLABLE,
    "string": _CAST_STRING_NULLABLE,
    "BooleanType": ("astype", "bool"),
    "boolean": ("astype", "bool"),
}

#: Constructs that appear in ``conversion_report["constructs_converted"]``.
#: A construct is reported under its own name, except a ``when`` that HAS an
#: ``.otherwise(...)``, which is reported as ``when_otherwise`` because that is
#: the pair it belongs to. A bare ``when`` reports ``when``: the pair is not in
#: the source, so naming it would be a false claim about what converted.
#: :func:`_reported_constructs` decides which of the two applies, per node.
_REPORTED_FUNCTIONS = frozenset(
    {
        "col", "lit", "trim", "lower", "upper", "to_date", "to_timestamp",
        "regexp_replace", "concat_ws", "when",
    }
)  # fmt: skip
_REPORTED_METHODS = frozenset({"withColumn", "withColumnRenamed", "cast", "alias", "filter", "select", "drop", "rlike"})
_REPORTED_ALIASES = {"when": "when_otherwise"}

_COMPARE_OPS = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">="}
_BIN_OPS = {ast.Add: "+", ast.Sub: "-", ast.Mult: "*", ast.Div: "/", ast.Mod: "%"}

#: What a ``when(...)`` chain with NO ``.otherwise(...)`` falls back to. Spark's
#: ``when(c, v)`` yields ``v`` where ``c`` holds and NULL everywhere else, so the
#: bare form is :meth:`_Translator._when_chain`'s nest with the fallback fixed
#: here rather than supplied by the source.
#:
#: ``np.nan`` rather than ``None`` or ``pd.NA``, and the choice is MEASURED (two
#: throwaway venvs outside the repo — pandas 2.2.3 and 3.0.5, both numpy 2.5.2,
#: the same method `LGR-4dd9b42-1` used because pandas is not a dependency here).
#: All three report correctly under ``isna()``, so none of them inflates the
#: harness's ``completeness`` the way the ``astype(str)`` spelling did (p40 `F6`).
#: They differ in the dtype ``np.where`` then yields over a NUMERIC column:
#: ``np.nan`` gives ``float64`` on both majors, while ``None`` and ``pd.NA`` both
#: force **object**, which stops the column being numeric at all. Consistency
#: with the surrounding ``np.where`` emission is the constraint (this is a numpy
#: element, not a pandas dtype), which is also why `LGR-4dd9b42-1`'s nullable
#: ``"string"`` dtype does not transfer: that governs a ``pd.Series(...)``
#: broadcast at `_cast`, where the dtype is nameable and there is no numpy array.
#:
#: ACCEPTED COST, recorded rather than hidden: this widens an integer column to
#: float, where Spark would give a null inside an int column.
_WHEN_NULL_FALLBACK = "np.nan"

#: Chain steps that map to a pandas method returning a frame, so they may
#: appear anywhere in a chain. Everything else needs a statement of its own,
#: which is why a mixed chain refuses (see :meth:`_Translator._chain`).
_CHAIN_COMPOSABLE = frozenset({"withColumnRenamed", "drop"})

#: Chain steps this module can express when they own the whole statement.
_FRAME_METHODS = frozenset({"withColumn", "withColumnRenamed", "select", "drop", "filter"})

#: Spark's two null tests and the pandas Series method each becomes.
#:
#: Deliberately NOT in :data:`_REPORTED_METHODS`. Four ``CONVERSION_BASELINE``
#: cases (``filter-isNotNull-method``, ``filter-isNull-method``,
#: ``filter-assignment-self``, ``filter-assignment-differing``) pin
#: ``constructs_converted == ["col", "filter"]`` byte-exactly for shapes that
#: contain one of these names, so reporting them would move four pins for a
#: reason unrelated to what those pins guard. ``rlike`` IS reported, which makes
#: the omission look accidental; it is not, and this is where that is recorded.
_NULL_PREDICATES = {"isNull": "isna", "isNotNull": "notna"}


def _attr_start(node: ast.Attribute) -> int:
    """Column where *node*'s attribute NAME starts.

    ``ast.Attribute`` reports the position of its receiver, so
    ``df.withColumn`` has ``col_offset`` pointing at ``df``. The attribute
    name is what a reader would call the construct's position, and
    ``end_col_offset`` marks its end.
    """
    return (node.end_col_offset or 0) - len(node.attr)


def _attr_line(node: ast.Attribute) -> int:
    """Physical line the attribute NAME sits on.

    For a chained expression spread over several lines this is what makes a
    refusal point at the construct rather than at the statement's first line
    — ``.withColumn`` on line 4 of a parenthesised fluent chain reports 4.
    """
    return node.end_lineno or node.lineno


class _Occurrence:
    """One PySpark construct found in a statement, with its position."""

    __slots__ = ("col", "lineno", "name", "supported")

    def __init__(self, name: str, lineno: int, col: int, *, supported: bool) -> None:
        self.name = name
        self.lineno = lineno
        self.col = col
        self.supported = supported

    @property
    def sort_key(self) -> tuple[int, int]:
        return (self.lineno, self.col)


#: The two spellings of the `pyspark.sql.functions` namespace the generated
#: code reaches functions through: `from pyspark.sql import functions as F`
#: (what the scaffolds write and `transform_generation.j2` shows) and the
#: unabbreviated `functions`. Both are MODULE ALIASES, not constructs.
_FUNCTION_MODULE_ALIASES = frozenset({"F", "functions"})


def _aliased_function(node: ast.AST) -> "str | None":
    """The function name behind ``F.<name>(...)`` / ``functions.<name>(...)``.

    ``None`` for everything else. The whole fence is the receiver's shape:
    *node* must be a :class:`ast.Call` whose ``func`` is an
    :class:`ast.Attribute` whose ``.value`` is a bare :class:`ast.Name` in
    :data:`_FUNCTION_MODULE_ALIASES`.

    **This resolves a SPELLING and adds no expressiveness** (p41 2.6). The
    resolved name is then looked up in :data:`_SUPPORTED_FUNCTIONS` /
    :data:`_UNSUPPORTED_FUNCTIONS` **unchanged**, so ``F.trim(col("b"))``
    converts exactly as ``trim(col("b"))`` does and ``F.initcap(col("b"))``
    refuses exactly as ``initcap(col("b"))`` does — under the name `initcap`,
    which is a construct an operator can act on, rather than under `F`, which
    is not (1.7, 2.5).

    **Consulted by three readers, and it has to be more than one** — a
    normalisation in any single one of them is dead code on the others' paths
    (`design.md` §5.4, §12 item 2):

    1. :func:`_occurrences`, because ``convert``'s pre-scan refuses on the
       first unsupported occurrence **before** ``translator.statement()`` runs
       at all. This is the reader that decides the refusal's construct name.
    2. :meth:`_Translator._call`, which is what actually translates the call.
    3. :func:`_reported_constructs`, so a converted aliased call contributes
       the same ``constructs_converted`` entries as the unaliased spelling.

    **``spark.conf.set("k", "v")`` must NOT resolve here, and the receiver
    check is what stops it**: its ``func.value`` is an ``Attribute``
    (``spark.conf``), not a ``Name``, so the predicate cannot fire and that
    call keeps refusing under `spark`. Load-bearing — 1.14's argument that the
    3 measured `set` refusals come from the Python builtin ``set`` and not from
    the scaffolds' ``spark.conf.set(...)`` rests on it.

    Not consulted by :data:`_TEXT_CONSTRUCT`, deliberately: that reader runs
    only on a block that does not parse, and a block that does not parse has no
    alias to resolve.

    Known remaining divergence, recorded rather than fixed here because it is
    reached through neither of the two readers above: ``_when_chain`` walks a
    ``when(...).otherwise(...)`` chain itself, so ``F.when(c, v).otherwise(w)``
    refuses naming `when` where the unaliased spelling converts. It no longer
    names the alias, so 2.6 holds; making it convert would be a fourth reader
    and new converting behaviour, which p41 task 8.2 does not scope.
    """
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    if func.value.id not in _FUNCTION_MODULE_ALIASES:
        return None
    return func.attr


def _alias_receivers(node: ast.AST) -> dict[int, str]:
    """``id(alias Name node) -> resolved function name`` over *node*'s subtree.

    The pre-pass the two AST walkers below share. Keyed by ``id`` because two
    aliased calls in one statement carry two distinct ``Name`` nodes with the
    same ``.id``, and only the one that is an aliased call's receiver may be
    rewritten — a bare `F` elsewhere in the same statement must still be seen
    as `F` and still refuse.

    Separate from the walk that consumes it so neither consumer depends on
    :func:`ast.walk`'s traversal order to have seen the ``Call`` before its own
    receiver ``Name``.
    """
    receivers: dict[int, str] = {}
    for child in ast.walk(node):
        # Narrowing only — the decision is `_aliased_function`'s, so there is
        # one predicate and not two.
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
            continue
        resolved = _aliased_function(child)
        if resolved is not None:
            receivers[id(child.func.value)] = resolved
    return receivers


def _occurrences(node: ast.AST) -> list[_Occurrence]:
    """Every PySpark construct in *node*'s subtree, in source order.

    Used for two things and nothing else: choosing which construct a refusal
    names, and building ``constructs_converted``. It replaces the
    predecessor's residual regex scan — and, unlike that scan, it reads the
    INPUT rather than already-rewritten output, which is why it can see the
    ``bugfix.md`` 1.10 shape at all (2.11).

    **An aliased call contributes an occurrence for the FUNCTION, not for the
    alias** (2.6). This is the reader that matters for the refusal payload:
    ``convert``'s pre-scan refuses on the first unsupported occurrence found
    here, **before** ``translator.statement()`` runs, so a normalisation placed
    only in the call path would never execute (`design.md` §12 item 2). The
    resolved occurrence keeps the alias ``Name``'s own ``(lineno, col)``, so
    every sort key — and therefore every leftmost race ``_refuse_here`` decides
    — is byte-identical to what it was before; only the name and the
    ``supported`` flag move.
    """
    found: list[_Occurrence] = []
    aliased = _alias_receivers(node)
    for child in ast.walk(node):
        if isinstance(child, ast.Attribute):
            attr = child.attr
            if attr in _SUPPORTED_METHODS:
                found.append(_Occurrence(attr, _attr_line(child), _attr_start(child), supported=True))
            elif attr in _UNSUPPORTED_METHODS:
                found.append(_Occurrence(attr, _attr_line(child), _attr_start(child), supported=False))
        elif isinstance(child, ast.Name):
            name = aliased.get(id(child), child.id)
            if name in _SUPPORTED_FUNCTIONS:
                found.append(_Occurrence(name, child.lineno, child.col_offset, supported=True))
            elif name in _UNSUPPORTED_FUNCTIONS or name in _UNSUPPORTED_NAMES:
                found.append(_Occurrence(name, child.lineno, child.col_offset, supported=False))
            # Type constructors are deliberately NOT occurrences: `IntegerType()`
            # is consumed by `.cast(...)` (3.5's sibling), so flagging it here
            # would refuse every cast. A Type reached anywhere else refuses at
            # the node instead — see `_Translator._function_call`.
    found.sort(key=lambda occurrence: occurrence.sort_key)
    return found


def _otherwise_terminated_whens(node: ast.AST) -> set[int]:
    """``id()`` of every ``when`` ``Name`` node whose chain ends in ``.otherwise()``.

    The pre-pass :func:`_reported_constructs` uses to tell the paired shape from
    the bare one, in the same style as :func:`_alias_receivers` and keyed by
    ``id`` for the same reason: one statement may carry BOTH shapes, so the
    decision belongs to the individual ``when`` node and not to the name.

    Walks each ``.otherwise(...)`` receiver inwards over ``.when(...)`` links to
    the ``Name`` that opens the chain — the same traversal
    :meth:`_Translator._when_nest` performs, but read-only and tolerant: a
    receiver that is not a chain simply contributes nothing, because deciding
    whether a shape CONVERTS is the translator's job, not this function's.
    """
    terminated: set[int] = set()
    for child in ast.walk(node):
        if not isinstance(child, ast.Call) or not isinstance(child.func, ast.Attribute):
            continue
        if child.func.attr != "otherwise":
            continue
        current: ast.AST = child.func.value
        while isinstance(current, ast.Call):
            callee = current.func
            if isinstance(callee, ast.Attribute) and callee.attr == "when":
                current = callee.value
                continue
            if isinstance(callee, ast.Name) and callee.id == "when":
                terminated.add(id(callee))
            break
    return terminated


def _reported_constructs(statement: ast.stmt) -> set[str]:
    """``constructs_converted`` entries contributed by *statement*.

    Derived from the parsed statement, so a construct named inside a string
    literal or a comment is not a construct (3.10).

    Reads :func:`_aliased_function` for the same reason ``_occurrences`` does
    (2.6): once ``F.trim(F.col("b"))`` converts, it must report the same
    ``constructs_converted`` as ``trim(col("b"))``, or the alias would be a
    spelling fix that silently emptied the report. `F` is in neither
    ``_REPORTED_*`` set, so without this an aliased statement contributed
    nothing at all.
    """
    names: set[str] = set()
    if isinstance(statement, ast.Import | ast.ImportFrom):
        return {"pyspark_import"}
    aliased = _alias_receivers(statement)
    paired = _otherwise_terminated_whens(statement)
    for child in ast.walk(statement):
        if isinstance(child, ast.Attribute) and child.attr in _REPORTED_METHODS:
            names.add(_REPORTED_ALIASES.get(child.attr, child.attr))
        elif isinstance(child, ast.Name):
            name = aliased.get(id(child), child.id)
            if name not in _REPORTED_FUNCTIONS:
                continue
            if name == "when" and id(child) not in paired:
                # p42: a BARE `when(...)` has no `otherwise`, so reporting the
                # pair would name a construct the source does not contain — the
                # misnaming class p41 closed for refusal payloads, applied to
                # the conversion report. The alias below stays the spelling for
                # the paired shape, which is the only shape it was ever true of.
                names.add("when")
                continue
            names.add(_REPORTED_ALIASES.get(name, name))
    return names


def _is_pyspark_import(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.ImportFrom):
        return (statement.module or "").split(".")[0] == "pyspark"
    if isinstance(statement, ast.Import):
        return any(alias.name.split(".")[0] == "pyspark" for alias in statement.names)
    return False


class _Translator:
    """Translate one parsed statement into pandas source text.

    Every failure path raises :class:`_Unconvertible`, so a statement is
    either fully translated or refused — a partial translation is never
    returned (3.8).
    """

    def __init__(self, source: str) -> None:
        self._source = source

    # ── helpers ──────────────────────────────────────────────────────

    def _segment(self, node: ast.AST) -> str:
        """Verbatim source text of *node*."""
        segment = ast.get_source_segment(self._source, node)
        if segment is None:  # pragma: no cover - positions are always present
            raise _Unconvertible("python_syntax", getattr(node, "lineno", 1))
        return segment

    def _refuse(self, node: ast.AST, construct: str) -> "_Unconvertible":
        lineno = _attr_line(node) if isinstance(node, ast.Attribute) else getattr(node, "lineno", 1)
        return _Unconvertible(construct, lineno)

    def _refuse_here(self, node: ast.AST) -> "_Unconvertible":
        """Refuse at *node*, naming the leftmost construct it carries.

        For failures that belong to the whole expression rather than to one
        construct: the single-quoted-literal fence (3.7 lists it as an
        unconvertible construct), and shapes with no rule at all — an operator
        outside the supported set, a computed callee. Naming the leftmost
        construct is what the predecessor's leftmost-regex-match did, so a
        reviewer sees the same construct named for the same input.
        """
        occurrences = _occurrences(node)
        if occurrences:
            first = occurrences[0]
            return _Unconvertible(first.name, first.lineno)
        return _Unconvertible("python_syntax", getattr(node, "lineno", 1))

    def _refuse_scalar(self, node: ast.AST, construct: str) -> "_Unconvertible":
        """Refuse at *node*: a Series-only member would land on a scalar (p40 R2).

        The refusal PAYLOAD is identical to :meth:`_refuse`'s — same lineno
        derivation, so the attribute NAME's line is reported for a method
        (``.cast`` on line 4 of a parenthesised fluent chain reports 4) and the
        node's own line otherwise. `bugfix.md` 3.4 is therefore unchanged: the
        construct, the offending line and the line number all still travel, and
        ``str(exc)`` reads exactly as it always has.

        The one addition is the fourth field, ``reason``. It rides on the
        exception rather than being derived from the construct name because
        ``code_interpreter_runner.classify_conversion_refusal`` maps a construct
        NAME to a reason, and every construct this fence names — ``cast``,
        ``trim``, ``lower``, ``upper``, ``regexp_replace``, ``concat_ws``,
        ``to_date``, ``alias`` — is also the name a legitimate
        not-yet-expressible refusal uses (see :class:`ConversionError`).
        """
        lineno = _attr_line(node) if isinstance(node, ast.Attribute) else getattr(node, "lineno", 1)
        return _Unconvertible(construct, lineno, reason=REFUSAL_REASON_SCALAR_LITERAL)

    def _require_series(self, emitted: str, owner: ast.AST, construct: str) -> str:
        """Guard one member-application site: *emitted* must not be a scalar.

        Called with the text emitted for the RECEIVER, immediately before a
        member from :data:`_SERIES_ONLY_MEMBERS` is appended to it, and returns
        that same text so the guard reads inline at the site that appends the
        member — the guard and the emission cannot drift apart.

        **Why the receiver's text and not the finished emission.** Once the
        member is appended the text is no longer recognisable as a scalar:
        ``None.astype(str)`` parses as a call on an attribute of a constant, and
        ``pd.to_numeric(0, errors="coerce").astype("Int64")`` wraps the scalar
        two levels down. :func:`_is_scalar_emission` deliberately treats any
        other call as a Series (its permissive default), so the receiver, before
        it is wrapped, is the only point where the question is decidable.

        *construct* is the construct that OWNS the member, never the one that
        produced the scalar — a reviewer is pointed at the operation that cannot
        be expressed rather than at the ``lit(...)`` that is perfectly fine
        (design §4.1).
        """
        if _is_scalar_emission(emitted):
            raise self._refuse_scalar(owner, construct)
        return emitted

    def _string_cast_broadcast(self, emitted: str, frame: str) -> "str | None":
        """Arm A: the ONE literal-receiver family that converts (p40 R1/R7).

        ``None`` returned means "not this family" — the caller falls through to
        :meth:`_require_series`, i.e. to the fence. So this method is the only
        place a literal receiver escapes a refusal, and it escapes for exactly
        two constant types.

        Given the text emitted for a ``.cast("string")`` / ``.cast(StringType())``
        receiver, returns ``pd.Series(<const>, index=<frame>.index,
        dtype="string")`` when that text is a ``None`` or ``str`` constant.
        Spark's answer for those two is unambiguous — null in every row, or the
        literal in every row — and `F7` measured pandas expressing both
        identically on 2.2.3 and 3.0.5.

        **Why only ``None`` and ``str``.** `F7` also measured
        ``pd.Series(True, index=…, dtype="string")`` yielding ``'True'`` where
        Spark spells a boolean ``true``, and Spark's spelling of a numeric
        constant cast to string could not be verified in this environment at
        all. So ``lit(0)``, ``lit(1.5)`` and ``lit(True)`` fall to the fence and
        keep refusing (design §4.2's third row) — refusing is the safe disjunct,
        and widening this predicate is what `test_literal_receiver_cast.py`'s
        `TestStringCastOnNonStringLiteralRefuses` exists to catch.

        **Why *frame* is the right index.** It is the ``frame`` argument
        :meth:`_expr` already threads — the enclosing statement's SOURCE frame
        (3.1/3.2) — and under :meth:`_with_column_chain` the target is a
        ``.copy()`` of that frame, so the broadcast index and the target's index
        are identical. Indexing off the assignment TARGET instead would be a
        forward reference to a frame the statement is still building.

        Decided over the emitted text for :func:`_is_scalar_emission`'s reason:
        the receiver has already been translated, so the emission is what will
        actually run. A ``col(...)`` receiver emits a subscript, which is not a
        constant, so the column path is untouched by this method and takes the
        ``.astype("string")`` emission below instead.
        """
        try:
            parsed = ast.parse(emitted.strip(), mode="eval")
        except SyntaxError:  # pragma: no cover - every emission this reaches parses
            return None
        node = parsed.body
        if not isinstance(node, ast.Constant):
            return None
        if node.value is not None and not isinstance(node.value, str):
            return None
        return f'pd.Series({emitted.strip()}, index={frame}.index, dtype="string")'

    def _string(self, node: ast.AST, owner: ast.AST, construct: "str | None" = None) -> str:
        """The **value** of a string literal — not its source text.

        ``r"[^\\d]"`` and ``"[^\\\\d]"`` both return the five characters
        ``[^\\d]``, because that is what Python's own literal semantics say each
        one *means*. Two spellings of one string therefore reach every emission
        site as one value.

        **The caller owns the escaping.** A value is not a literal body, so
        embedding this return in a fence without re-escaping it *for that fence*
        is precisely p41's D3 and D5. Every site that emits it into a plain
        ``"…"`` fence passes it through :func:`_dq` first; the two sites that
        emit into an ``r"…"`` fence — ``regexp_replace``'s pattern and
        ``rlike``'s pattern — pass it through :func:`_raw`, which falls back to a
        plain fence carrying the same regex value when a raw fence cannot spell
        it. :meth:`_cast`'s string target key is a dict lookup into
        :data:`_CAST_TARGETS` rather than an emission, so it needs neither.

        **The old source-text rationale is deleted rather than softened, and
        this is why.** It read: *the source text rather than the parsed value, so
        a regex literal* (``rlike("@example\\.com$")``) *survives conversion
        byte-for-byte instead of being re-escaped.* Its own example falsifies it
        (`bugfix.md` 1.18). Source text is value-preserving only under an
        **identical** fence, and both regex sites change the fence from plain to
        raw — so ``rlike("@example\\\\.com$")`` emitted
        ``.str.contains(r"@example\\\\.com$", …)``, a regex that matches a literal
        backslash and no longer matches the address it was written for. Where
        that argument *is* true is :meth:`_function_call`'s ``lit`` arm (`:941`),
        which re-emits a literal's source text through :meth:`_segment` into the
        identical fence it came from; that is genuinely byte-preserving and is
        untouched here.

        **Two separately-evaluable fences, in this order** (`design.md` §5.2
        step 1):

        1. a leading ``r``/``R`` is recognised and **stripped**. It is never a
           reason to refuse — raw is a *spelling*, and refusing it made every
           regex the generator writes unconvertible (`bugfix.md` 1.1-1.3: 5 of 5
           measured production regexes, none of which needed a backslash to be
           refused);
        2. the double-quote fence — the same check as before, ``\"\"\"`` clause
           included — then runs on the prefix-stripped remainder.

        So **single-quoted literals still refuse, and the quote fence is the half
        that refuses them**: ``r'…'`` fails predicate 2, never predicate 1, and
        ``'…'`` fails it identically. That fence is kept deliberately rather than
        inherited (`bugfix.md` 2.3, `design.md` §9 Q1): the generation prompt
        emits double quotes, all 5 measured raw regexes are double-quoted, and a
        block spelled otherwise is not a block this checker has ever verified —
        so lifting it would widen the accepted surface for no measured demand.
        Predicate 1 accepting a spelling predicate 2 rejects is what leaves the
        two liftable independently.

        Which half failed is legible **here and in the behaviour**, never in the
        refusal payload. :class:`ConversionError` has no message field to widen
        (`bugfix.md` 3.6 freezes ``str(exc)``), and its ``reason`` is
        operator-facing output that
        ``code_interpreter_runner.classify_conversion_refusal`` defers to — so
        setting one would change what every single-quoted-literal refusal *means*
        downstream, which `bugfix.md` 2.11 puts out of scope.

        **``construct`` — who the refusal names** (`bugfix.md` 2.4/2.5, D2a;
        `design.md` §5.3). Optional, keyword-capable and **last**, so
        ``_string(node, owner)`` keeps working unchanged at every site that does
        not pass it. When present, the failure path raises
        :meth:`_refuse`\\ ``(owner, construct)`` — naming the construct that
        **owns** the operation. When absent it keeps raising
        :meth:`_refuse_here`, whose leftmost-occurrence rule is deliberate and
        which 8 other call sites rely on (3.5) — so this parameter narrows the
        blanket rule at the sites that know their own name instead of replacing
        it. That is `LGR-86698d4-2`'s shape (template / ratified): *draw the line
        by ORIGIN rather than by position; keep the specific clause and stop the
        blanket clause absorbing everything else.*

        **Five sites pass it, and every one is passing a name it already holds.**
        ``rlike``'s pattern (:meth:`_filter_mask`) — the D2a defect
        `bugfix.md` 1.6 reproduced, where ``col(`` starting left of ``.rlike``
        won a race it should not have been in. ``regexp_replace``'s pattern and
        replacement, which hand :meth:`_require_series` exactly that string one
        line above. ``alias``'s new name and :meth:`_cast`'s target key, which
        both already raise ``_refuse(func, "alias")`` / ``_refuse(func, "cast")``
        for their *other* failures — so before this parameter existed, one
        operation answered two different constructs depending on which of its own
        guards fired (``cast("nonsense")`` named ``cast``; ``cast('int')`` named
        ``col``).

        The eight column-name sites deliberately do **not** pass it:
        :meth:`_column_name` keeps the leftmost rule because leftmost is already
        the owner there — ``df.withColumn('name', …)`` names ``withColumn``,
        which baseline case ``refuse-single-quoted-col`` pins.

        This changes *which* construct is named and nothing else:
        :class:`ConversionError` keeps its three positional fields, ``reason``
        stays ``None`` on every path here, and ``str(exc)`` is untouched
        (`bugfix.md` 3.6).
        """
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            if construct is not None:
                raise self._refuse(owner, construct)
            raise self._refuse_here(owner)
        # Annotated rather than returned straight off the node: `ast.Constant.value`
        # is `Any`, and this module is checked with `warn_return_any`.
        value: str = node.value
        segment = self._segment(node)
        # PREDICATE 1 — the raw prefix, recognised and stripped.
        remainder = segment[1:] if segment[:1] in ("r", "R") else segment
        # PREDICATE 2 — the quote fence, run on the remainder.
        if (
            len(remainder) < 2
            or not remainder.startswith('"')
            or not remainder.endswith('"')
            or remainder.startswith('"""')
        ):
            if construct is not None:
                raise self._refuse(owner, construct)
            raise self._refuse_here(owner)
        return value

    def _column_name(self, node: ast.AST, owner: ast.AST) -> str:
        """A column name, escaped for the plain ``"…"`` fence every caller emits.

        **Wrap point 1 of 7, and the reason the value contract is cheap.** All
        eight of the sites that name a column go through here — ``col`` (`:995`),
        ``withColumn`` (`:1355`), ``withColumnRenamed``'s old and new (`:1377`,
        `:1378`), ``drop`` (`:1383`), ``filter``'s ``col`` (`:1403`), and
        ``select``'s two forms (`:1429`, `:1435`) — and every one of them emits
        into a plain ``"…"`` pair, so one :func:`_dq` here is the whole escaping
        rule for two thirds of `design.md` §5.1's table.

        ``select``'s string-argument arm (`:1435`) carries the return at **two**
        emission positions — the ``pd.DataFrame`` dict key and the frame
        subscript — both built from this one value, so both are covered here.

        :func:`_dq` is the identity on every ``\\w`` name, which is all four
        ``_SELECT_*`` matchers can resolve (`:324-327`), so this cannot move a
        name ``select`` resolves today.
        """
        return _dq(self._string(node, owner))

    # ── expressions ──────────────────────────────────────────────────

    def _expr(self, node: ast.AST, frame: str) -> str:
        """Translate an expression. *frame* is what bare ``col("x")`` binds to.

        Binding ``col()`` to the enclosing statement's SOURCE frame is
        3.1/3.2's requirement: a later line has to see an earlier line's
        effect, including a column an earlier ``withColumnRenamed`` created.
        """
        if isinstance(node, ast.Call):
            return self._call(node, frame)
        if isinstance(node, ast.Compare):
            return self._compare(node, frame)
        if isinstance(node, ast.BinOp):
            return self._binop(node, frame)
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
            return f"-{self._expr(node.operand, frame)}"
        if isinstance(node, ast.Attribute) and node.attr in _UNSUPPORTED_METHODS:
            raise self._refuse(node, node.attr)
        if isinstance(node, ast.Name):
            if node.id in _UNSUPPORTED_NAMES or _TYPE_NAME.match(node.id):
                raise _Unconvertible(node.id, node.lineno)
            return self._segment(node)
        if _occurrences(node):
            raise self._refuse_here(node)
        # Construct-free: not PySpark at all, so it passes through verbatim.
        return self._segment(node)

    def _compare(self, node: ast.Compare, frame: str) -> str:
        if len(node.ops) != 1 or type(node.ops[0]) not in _COMPARE_OPS:
            raise self._refuse_here(node)
        symbol = _COMPARE_OPS[type(node.ops[0])]
        return f"{self._expr(node.left, frame)} {symbol} {self._expr(node.comparators[0], frame)}"

    def _binop(self, node: ast.BinOp, frame: str) -> str:
        if type(node.op) not in _BIN_OPS:
            raise self._refuse_here(node)
        symbol = _BIN_OPS[type(node.op)]
        return f"{self._expr(node.left, frame)} {symbol} {self._expr(node.right, frame)}"

    def _call(self, node: ast.Call, frame: str) -> str:
        if isinstance(node.func, ast.Name):
            return self._function_call(node, node.func.id, frame)
        if isinstance(node.func, ast.Attribute):
            # `F.<name>(...)` is a FUNCTION call spelled through a module alias,
            # not a method call on a receiver named `F` (2.6). Dispatching it to
            # `_function_call` with the resolved name is what makes
            # `_SUPPORTED_FUNCTIONS` / `_UNSUPPORTED_FUNCTIONS` apply
            # **unchanged** — this branch adds no rule and no expressiveness.
            #
            # `_occurrences` consults the same predicate, and it has to: the
            # pre-scan at `convert` refuses before this method is ever reached
            # for an unsupported aliased function, so this branch alone would be
            # dead code for exactly the shapes 2.6 is about (`design.md` §5.4).
            aliased = _aliased_function(node)
            if aliased is not None:
                return self._function_call(node, aliased, frame)
            return self._method_call(node, node.func, frame)
        raise self._refuse_here(node)

    def _function_call(self, node: ast.Call, name: str, frame: str) -> str:
        if name in _UNSUPPORTED_FUNCTIONS or name in _UNSUPPORTED_NAMES or _TYPE_NAME.match(name):
            raise _Unconvertible(name, node.func.lineno)

        if name == "col":
            if len(node.args) != 1:
                raise _Unconvertible("col", node.lineno)
            return f'{frame}["{self._column_name(node.args[0], node)}"]'

        if name == "lit":
            if len(node.args) != 1 or not isinstance(node.args[0], ast.Constant):
                raise _Unconvertible("lit", node.lineno)
            return self._segment(node.args[0])

        if name in ("trim", "lower", "upper"):
            # LGR-5c33c04-1: the argument may itself be a converted string
            # expression (`lower(trim(col("x")))`), so the outer op appends to
            # whatever the inner one emitted rather than requiring a bare col().
            if len(node.args) != 1:
                raise _Unconvertible(name, node.lineno)
            operation = {"trim": "strip", "lower": "lower", "upper": "upper"}[name]
            # p40 R2, guard site 1 of 7: `.str` is a Series accessor, and
            # `"x".str.strip()` is an `AttributeError` (design §5, F9). So
            # `trim(lit("x"))` refuses, naming `trim` — while the nested
            # composition the comment above is about is untouched, because the
            # inner op emitted a Series and a Series is not a scalar emission.
            receiver = self._require_series(self._expr(node.args[0], frame), node, name)
            return f"{receiver}.str.{operation}()"

        if name in ("to_date", "to_timestamp"):
            return self._to_datetime(node, name, frame)

        if name == "regexp_replace":
            if len(node.args) != 3:
                raise _Unconvertible("regexp_replace", node.lineno)
            # p40 R2, guard site 2 of 7: `.str.replace` on the same accessor, so
            # the same `AttributeError` on a `str` receiver (design §5, F9).
            target = self._require_series(self._expr(node.args[0], frame), node, "regexp_replace")
            # Wrap points 2 and 3 of 7, and the two fences differ. The pattern
            # emits into `r"…"` so it takes `_raw`, which yields the prefix as
            # well as the body — `("r", value)` on every measured input, so the
            # emitted line is byte-identical to what this site always wrote. The
            # replacement emits into a plain `"…"` and takes `_dq`.
            #
            # Both pass `"regexp_replace"` as `_string`'s owning construct (2.4,
            # D2a) — the same string `_require_series` was handed one line above,
            # so the name is already held here. This site already ANSWERED
            # `regexp_replace`, because the call node it hands `_string` as
            # *owner* starts at `regexp_replace` and so wins `_refuse_here`'s
            # leftmost race; passing the name makes that a property of the site
            # rather than a coincidence of position.
            fence, pattern = _raw(self._string(node.args[1], node, "regexp_replace"))
            replacement = _dq(self._string(node.args[2], node, "regexp_replace"))
            return f'{target}.str.replace({fence}"{pattern}", "{replacement}", regex=True)'

        if name == "concat_ws":
            if len(node.args) < 3:
                raise _Unconvertible("concat_ws", node.lineno)
            # Wrap point 4 of 7: the separator emits into a plain `"…"` at the
            # join below. The COLUMN parts are deliberately untouched — see the
            # comment on `.astype(str)` immediately below.
            separator = _dq(self._string(node.args[0], node))
            # p40 R2, guard site 3 of 7 — the LITERAL parts only, and the
            # distinction is deliberate. `"x".astype(str)` raises (design §5, F9),
            # so a literal part refuses. A `col(...)` part's `.astype(str)` is a
            # SEPARATE, real fidelity divergence — Spark skips null inputs where
            # pandas splices in the string "None" — which is recorded and
            # deliberately NOT fixed here (design §5, N3a). Out of scope means its
            # emitted text stays byte-identical: it pins baseline case
            # `concat_ws-two-cols` (`test_converter_equivalence_baseline.py:414`)
            # and `test_pyspark_to_pandas.py:241`, both of which p40 task 14 checks
            # for an empty diff. The guard is a no-op on a column part, because a
            # subscript is not a scalar emission.
            parts = [
                f"{self._require_series(self._expr(arg, frame), node, 'concat_ws')}.astype(str)"
                for arg in node.args[1:]
            ]
            return f' + "{separator}" + '.join(parts)

        if name == "when":
            # A bare `when(c, v)` with no `.otherwise(...)` IS a complete
            # expression: Spark yields `v` where `c` holds and NULL elsewhere.
            # So it is the same nest the paired form produces, with the fallback
            # fixed at null (`_WHEN_NULL_FALLBACK`) instead of read from the
            # source. The paired form still enters through `_method_call`'s
            # `otherwise` branch and walks the outermost `.otherwise()` inwards,
            # so this branch is reached only when nothing wrapped the chain.
            #
            # This refused until p42, on the grounds that a bare `when` is "not
            # a complete expression". That was the one wrong premise: it is
            # complete, and the generated line that exposed it
            # (`latesttest-50e07536`) was valid PySpark the checker could not
            # read. Note the refusal fired BEFORE the condition and value were
            # translated, so `when` was reported for shapes whose real obstacle
            # was elsewhere; those now name their own obstacle instead.
            return self._when_chain_without_otherwise(node, frame)

        # An unrecognised BARE NAME in call position refuses (2.10).
        #
        # This branch used to pass the call through, "exactly as the
        # predecessor's substitution pass did". That is the hole `posexplode`
        # came through: a name in neither frozenset was emitted verbatim, so the
        # conversion *succeeded* and the Code Interpreter raised `NameError` on
        # a name nothing defines — the class of defect 2.10 exists to forbid,
        # and one that enumerating names can only ever chase, never close.
        #
        # Refusing by default is safe **in function position specifically**, and
        # only there. A bare name called inside a transform block can only have
        # come from `pyspark.sql.functions` (that is the sole import the
        # scaffolds provide and the only one `transform_generation.j2` permits —
        # it forbids the model from writing imports or helper functions), so an
        # unrecognised one is a converter gap, not a Python builtin. Measured
        # rather than assumed: none of the 79 inputs captured in
        # `test_converter_equivalence_baseline.py` calls a bare name outside
        # these frozensets, so no case that converts today depended on this
        # branch.
        #
        # `_passthrough_call` is deliberately NOT retired — `_method_call` still
        # needs it, and the same default is NOT safe there. A method's receiver
        # can be any object, so recognising `<receiver>.<attr>(...)` would mean
        # inferring the receiver's type rather than reading a name; the method
        # side is closed by enumeration (`_UNSUPPORTED_METHODS`) instead.
        raise _Unconvertible(name, node.func.lineno)

    def _passthrough_call(self, node: ast.Call, callee: str, frame: str) -> str:
        if node.keywords:
            raise self._refuse_here(node)
        arguments = ", ".join(self._expr(arg, frame) for arg in node.args)
        return f"{callee}({arguments})"

    def _to_datetime(self, node: ast.Call, name: str, frame: str) -> str:
        """``to_date``/``to_timestamp`` — the explicit format survives (3.5)."""
        if not node.args or len(node.args) > 2:
            raise _Unconvertible(name, node.lineno)
        target = self._expr(node.args[0], frame)
        if len(node.args) == 2:
            # Wrap point 5 of 7, and the ORDER is load-bearing: `_dq` goes on the
            # strftime map's RESULT, never on `_string`'s return. The map is the
            # identity on `"`, `\`, LF, CR and tab and its output alphabet
            # contains neither a quote nor a backslash, so escaping after it
            # cannot double-escape. Escaping BEFORE it could collide — the map
            # looks up alphabetic runs, and `_java_date_format_to_strftime("a")`
            # is `"%p"`, so an escape whose letter is a `token_map` key would be
            # rewritten and the emitted format string would silently change.
            # The mapping itself is untouched (`bugfix.md` 3.5).
            strftime = _dq(_java_date_format_to_strftime(self._string(node.args[1], node)))
            call = f'pd.to_datetime({target}, format="{strftime}", errors="coerce")'
        else:
            call = f'pd.to_datetime({target}, errors="coerce")'
        if name == "to_date":
            # p40 R2, guard site 4 of 7, and the pair's asymmetry is the point.
            # Only this arm appends a member, and `.dt` on a `Timestamp` / `NaT` /
            # `None` raises on both pandas majors (design §5, F5) — so
            # `to_date(lit(...))` refuses. `to_timestamp(lit(...))` falls through
            # with NO member appended and keeps converting, which F5 measured as
            # agreeing with Spark: the mechanical rule gets both halves right with
            # no special case, because it fires on a member being applied rather
            # than on a construct's name. `pd.to_datetime(<scalar>, …)` is itself a
            # scalar emission (:data:`_SCALAR_PRESERVING_HELPERS`), which is what
            # brings this site inside the predicate's reach at all.
            return f"{self._require_series(call, node, 'to_date')}.dt.date"
        return call

    def _method_call(self, node: ast.Call, func: ast.Attribute, frame: str) -> str:
        attr = func.attr

        if attr in _UNSUPPORTED_METHODS:
            raise self._refuse(func, attr)

        if attr == "otherwise":
            return self._when_chain(node, frame)

        if attr == "cast":
            return self._cast(node, func, frame)

        if attr == "alias":
            if len(node.args) != 1:
                raise self._refuse(func, "alias")
            receiver = self._expr(func.value, frame)
            if not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
                # A non-literal alias cannot name a column — refuse rather
                # than guess (the predecessor's safety net, kept).
                raise self._refuse(func, "alias")
            # p40 R2, guard site 5 of 7: `.rename(...)` is a Series method and
            # `"x".rename("z")` raises (design §5, F9). A scalar receiver here is a
            # `lit(...)` with nothing in between to turn it into a Series. A
            # receiver a REFUSED inner construct would have produced never reaches
            # this line — that construct's own guard fires first — which is 2.6:
            # a broken receiver is never carried into a surrounding expression that
            # converts.
            guarded = self._require_series(receiver, func, "alias")
            # Wrap point 6 of 7: a plain `"…"` fence inside `.rename(...)`. This
            # is one of the two names `_SELECT_RENAME_SUFFIX` (`:327`) reads back
            # off the emitted text, and `_dq` is the identity on every `\w` name
            # that matcher accepts, so the resolution is unmoved.
            #
            # `"alias"` is `_string`'s owning construct (2.4, D2a): the literal is
            # this operation's own new-name argument, and this branch already
            # raises `_refuse(func, "alias")` twice above for its other failures.
            # Before it was passed, `col("a").alias('plain')` refused naming
            # `col` while `col("a").alias(alias_var)` refused naming `alias` —
            # one operation, two answers.
            return f'{guarded}.rename("{_dq(self._string(node.args[0], node, "alias"))}")'

        if attr in _FRAME_METHODS:
            # A frame method reached through the expression translator means it
            # is not the statement's own operation — e.g. `.withColumn` in the
            # middle of a chain, which needs a statement of its own.
            raise self._refuse(func, attr)

        if attr == "when":
            # `when(c1, v1).when(c2, v2)` with no `.otherwise(...)`. Reached only
            # when `.when` is the OUTERMOST operation — a chain that does end in
            # `.otherwise()` is consumed by the `otherwise` branch above, which
            # walks past these inner `.when`s itself and never returns here.
            #
            # A receiver that is not a `when` chain (`col("a").when(...)`) still
            # refuses, inside the walk, still naming `when` on this same node.
            return self._when_chain_without_otherwise(node, frame)

        if attr in _NULL_PREDICATES:
            # p42 gap 2: a null test is a general boolean expression over ANY
            # convertible receiver, not only a `filter()` predicate. Spark
            # defines `e.isNull()` for any column expression `e`, and pandas'
            # `.isna()`/`.notna()` are Series methods, so they compose onto
            # whatever the receiver emitted. `_filter_mask` (`:1779`) keeps its
            # own rule — it resolves a column NAME off the receiver and so can
            # only ever see a bare `col("x")`, which is why the live shape
            # `col("x").cast(IntegerType()).isNotNull()` could not have been
            # handled there. The two rules are held to the same emission by
            # `test_null_predicate_expressions.TestBothPathsAgree`.
            if node.args or node.keywords:
                # Neither predicate takes an argument in Spark, and neither
                # pandas member takes one — a call carrying one is not this
                # construct, so refuse rather than drop it.
                raise self._refuse(func, attr)
            # p40 R2, guard site 8 — added by p42 gap 2; the seven labelled
            # "of 7" elsewhere in this module are p40's original set and are
            # left as they read. `.notna()`/`.isna()` are Series methods and
            # `None.notna()` / `"x".isna()` both raise `AttributeError`, so a
            # scalar receiver must refuse (`LGR-4dd9b42-1`). *attr* is the
            # construct that OWNS the member — a reviewer is pointed at the null
            # test, never at the `lit(...)` that is perfectly fine.
            receiver = self._require_series(self._expr(func.value, frame), func, attr)
            return f"{receiver}.{_NULL_PREDICATES[attr]}()"

        if attr == "rlike":
            # Deliberately NOT widened alongside the null predicates. `rlike`
            # carries a pattern argument that has to go through the `_raw` fence,
            # and `LGR-f6519e1-1` bounds itself on `_filter_mask`'s arm being the
            # ONLY site that hands an `rlike` pattern to that fence. A second
            # such site would move the boundary p41 just drew, so this stays a
            # filter-position-only construct (pinned by
            # `test_null_predicate_expressions.TestRlikeStaysRefusingOutsideFilter`).
            raise self._refuse(func, attr)

        return self._passthrough_call(node, f"{self._expr(func.value, frame)}.{attr}", frame)

    def _cast(self, node: ast.Call, func: ast.Attribute, frame: str) -> str:
        if len(node.args) != 1:
            raise self._refuse(func, "cast")
        spec = node.args[0]
        if isinstance(spec, ast.Call) and isinstance(spec.func, ast.Name):
            key = spec.func.id
        elif isinstance(spec, ast.Constant) and isinstance(spec.value, str):
            # `"cast"` is `_string`'s owning construct (2.4, D2a). No wrap point:
            # the value is a dict key into `_CAST_TARGETS`, never emitted, so it
            # needs neither `_dq` nor `_raw` (`tasks.md` 4.3). The name is
            # already held — both sibling branches raise `_refuse(func, "cast")`
            # — and before it was passed, `cast("nonsense")` refused naming
            # `cast` while `cast('int')` refused naming `col`.
            key = self._string(spec, node, "cast")
        else:
            raise self._refuse(func, "cast")
        target = _CAST_TARGETS.get(key)
        if target is None:
            raise self._refuse(func, "cast")
        # p40 R2, guard sites 6 and 7 of 7 — BOTH of `_cast`'s emissions, guarded
        # once, on the RECEIVER rather than on either finished string.
        #
        # `.astype(...)` on a scalar raises outright (design §5, F9), and so does
        # the numeric branch's `.astype("Int64")` / `.astype("float32")` suffix —
        # with two different exception types, `AttributeError` for `lit(0)` and
        # `TypeError: data type 'Int64' not understood` for `lit(None)` (F1/F2/F3).
        # The suffix-FREE numeric half (`_CAST_NUMERIC_FLOAT`) appends no member and
        # refuses anyway, deliberately: F4 measured it executing and producing an
        # `int64` column where Spark produces a double, so one rule per receiver
        # shape is both safer and testable as a class (design §4.2). Guarding the
        # receiver is what makes the two halves one rule — `pd.to_numeric(<scalar>,
        # …)` is itself a scalar emission, so the guard has to fire before the
        # suffix is appended, and after it there is nothing left to recognise.
        #
        # The guard sits AFTER the target lookup on purpose: an unrecognised cast
        # target keeps refusing as `cast` with no reason, exactly as before.
        emitted = self._expr(func.value, frame)
        # p40 task 5.1, Arm A — the ONE literal-receiver family that converts,
        # sitting immediately above the guard because it is an exception to it:
        # a `None` or `str` constant cast to `"string"` / `StringType()` becomes
        # a broadcast Series (`_string_cast_broadcast`). Every other constant
        # type, and every other cast target, falls through to the fence on the
        # next line and refuses.
        #
        # Emitting a Series rather than refusing is also what makes the
        # surrounding constructs compose for free: `.alias` appends `.rename(...)`
        # to a Series, `select` still resolves the output name through
        # `_SELECT_RENAME_SUFFIX`, and `trim` appends `.str.strip()` — none of
        # which needs a special case, because none of them can now see a scalar
        # (design §4.2's last-but-one row).
        if target == _CAST_STRING_NULLABLE:
            broadcast = self._string_cast_broadcast(emitted, frame)
            if broadcast is not None:
                return broadcast
        receiver = self._require_series(emitted, func, "cast")
        kind, detail = target
        if kind == "numeric":
            return f'pd.to_numeric({receiver}, errors="coerce"){detail}'
        return f"{receiver}.astype({detail})"

    def _when_chain(self, node: ast.Call, frame: str) -> str:
        """``when(c, v)[.when(c, v)]*.otherwise(v)`` → nested ``np.where``.

        Entered from :meth:`_method_call`'s ``otherwise`` branch, i.e. from the
        OUTERMOST ``.otherwise()`` inwards. The fallback is the source's own
        ``otherwise`` argument; the sibling below supplies null instead.
        """
        func = node.func
        if not isinstance(func, ast.Attribute):  # pragma: no cover - guarded by the caller
            raise self._refuse_here(node)
        if len(node.args) != 1:
            raise self._refuse(func, "otherwise")
        fallback = self._expr(node.args[0], frame)
        return self._when_nest(func.value, fallback, func, frame)

    def _when_chain_without_otherwise(self, node: ast.Call, frame: str) -> str:
        """``when(c, v)[.when(c, v)]*`` with NO ``.otherwise(...)``.

        Spark's ``when`` returns NULL where no branch matched, so this is the
        same nest with the fallback fixed at :data:`_WHEN_NULL_FALLBACK`. Both
        arities come here: the single bare ``when(c, v)`` from
        :meth:`_function_call`, and ``when(...).when(...)`` from
        :meth:`_method_call`. Covering both is deliberate — the missing
        ``otherwise`` is a property of the CHAIN, not of its branch count, so
        splitting them would leave a seam with no semantic justification.

        *node* is the outermost call of the chain, so ``node.func`` is the owner
        for refusals raised inside the walk: a ``Name`` (``when``) for the bare
        form and the ``.when`` ``Attribute`` for the chained one. Both are the
        exact nodes the two former refusal sites already used, so a shape that
        still refuses reports the same construct on the same line as before.
        """
        return self._when_nest(node, _WHEN_NULL_FALLBACK, node.func, frame)

    def _when_nest(self, innermost: ast.AST, fallback: str, owner: ast.AST, frame: str) -> str:
        """Walk a ``when`` chain inwards from *innermost*, nesting ``np.where``.

        Every branch survives, in order, which is what `bugfix.md` 2.9 is
        about: dropping one silently changes which rows get which value. The
        earliest ``when`` ends up OUTERMOST, which is Spark's first-match-wins.

        The walk is what keeps this safe for receivers that are not chains at
        all: ``col("a").when(...)`` reaches a non-``when`` callee and refuses,
        naming ``when`` — so routing the no-``otherwise`` shapes here converts
        genuine chains without widening what counts as one.
        """
        branches: list[tuple[str, str]] = []
        current: ast.AST = innermost
        while True:
            if not isinstance(current, ast.Call):
                raise self._refuse(owner, "when")
            callee = current.func
            if isinstance(callee, ast.Attribute) and callee.attr == "when":
                branches.append(self._when_branch(current, callee, frame))
                current = callee.value
                continue
            if isinstance(callee, ast.Name) and callee.id == "when":
                branches.append(self._when_branch(current, callee, frame))
                break
            raise self._refuse(owner, "when")

        expression = fallback
        for condition, value in branches:  # innermost .when() first
            expression = f"np.where({condition}, {value}, {expression})"
        return expression

    def _when_branch(self, call: ast.Call, callee: ast.AST, frame: str) -> tuple[str, str]:
        if len(call.args) != 2:
            lineno = _attr_line(callee) if isinstance(callee, ast.Attribute) else call.lineno
            raise _Unconvertible("when", lineno)
        return self._expr(call.args[0], frame), self._expr(call.args[1], frame)

    # ── statements ───────────────────────────────────────────────────

    def statement(self, statement: ast.stmt) -> "str | None":
        """Translate one statement. ``None`` means "emit nothing" (an import)."""
        if _is_pyspark_import(statement):
            return None
        if isinstance(statement, ast.Assign):
            return self._assign(statement)
        if isinstance(statement, ast.Expr):
            return self._bare_expression(statement.value)
        if _occurrences(statement):
            raise self._refuse_here(statement)
        return self._segment(statement)

    def _assign(self, statement: ast.Assign) -> str:
        if len(statement.targets) != 1 or not isinstance(statement.targets[0], ast.Name):
            if _occurrences(statement):
                raise self._refuse_here(statement)
            return self._segment(statement)
        target = statement.targets[0].id
        root, operations = _split_chain(statement.value)
        if root is None or not operations:
            return f"{target} = {self._expr(statement.value, 'df')}"
        return self._chain(target, root, operations, assigned=True)

    def _bare_expression(self, value: ast.expr) -> str:
        root, operations = _split_chain(value)
        if root is None or not operations:
            return self._expr(value, "df")
        # Method position: the receiver frame is also the assignment target,
        # so `df.filter(...)` yields `df[...]` rather than a rebinding.
        return self._chain(root, root, operations, assigned=False)

    def _chain(
        self,
        target: str,
        source: str,
        operations: "list[tuple[str, ast.Call, ast.Attribute]]",
        *,
        assigned: bool,
    ) -> str:
        """Translate a frame-operation chain rooted at *source*.

        Two grammars are expressible, and the split is the whole point of this
        rewrite:

        * ``source.withColumn(...)+ [.select(...)]`` — any number of chained
          ``withColumn`` calls (`bugfix.md` 2.7, the live failure chained
          seven), optionally restricted by a trailing ``select`` (2.10).
          Each step becomes its own assignment, joined with ``;`` onto one
          physical line so the emitted block keeps one line per input line.
        * a single ``withColumnRenamed`` / ``drop`` / ``filter`` / ``select``
          / ``withColumn``, which is what the predecessor handled.

        Anything else refuses. A mixed chain such as
        ``.withColumnRenamed(...).withColumn(...).drop(...)`` is refused at
        its first non-composable step, naming that construct and the physical
        line the construct sits on — so a parenthesised fluent chain still
        refuses, and still points at the step that cannot be expressed (3.8,
        and `pyspark_templates/README.md`'s worked example).
        """
        names = [name for name, _call, _func in operations]

        with_column_count = sum(1 for name in names if name == "withColumn")
        tail_select = names[-1] == "select" and with_column_count == len(names) - 1
        if with_column_count == len(names) or (tail_select and with_column_count >= 1):
            if not assigned:
                raise self._refuse(operations[0][2], "withColumn")
            return self._with_column_chain(target, source, operations, has_select=tail_select)

        if len(names) == 1:
            return self._single_operation(target, source, operations[0], assigned=assigned)

        for name, _call, func in operations:
            if name not in _CHAIN_COMPOSABLE:
                raise self._refuse(func, name)
        # Every step is composable but the chain is still not one of the two
        # grammars above (e.g. two renames): refuse at the second step rather
        # than emit a composition this module has never verified.
        raise self._refuse(operations[1][2], names[1])

    def _with_column_chain(
        self,
        target: str,
        source: str,
        operations: "list[tuple[str, ast.Call, ast.Attribute]]",
        *,
        has_select: bool,
    ) -> str:
        with_columns = operations[:-1] if has_select else operations
        statements: list[str] = []
        if target != source:
            # PySpark's withColumn returns a NEW dataframe — keep those
            # semantics so the source frame stays pristine for later
            # references (3.1).
            statements.append(f"{target} = {source}.copy()")
        # The first step reads the source frame (identical to the fresh copy);
        # every later step reads the frame the previous step wrote, so a chain
        # sees its own effects exactly as PySpark's does.
        frame = source
        for _name, call, func in with_columns:
            if len(call.args) != 2:
                raise self._refuse(func, "withColumn")
            column = self._column_name(call.args[0], call)
            statements.append(f'{target}["{column}"] = {self._expr(call.args[1], frame)}')
            frame = target
        if has_select:
            _name, call, func = operations[-1]
            statements.append(f"{target} = {self._select(target, call, func)}")
        return "; ".join(statements)

    def _single_operation(
        self,
        target: str,
        source: str,
        operation: "tuple[str, ast.Call, ast.Attribute]",
        *,
        assigned: bool,
    ) -> str:
        name, call, func = operation
        if name == "withColumn":  # pragma: no cover - handled by _with_column_chain
            raise self._refuse(func, "withColumn")
        if name == "withColumnRenamed":
            if len(call.args) != 2:
                raise self._refuse(func, "withColumnRenamed")
            old = self._column_name(call.args[0], call)
            new = self._column_name(call.args[1], call)
            body = f'{source}.rename(columns={{"{old}": "{new}"}})'
        elif name == "drop":
            if not call.args:
                raise self._refuse(func, "drop")
            columns = ", ".join(f'"{self._column_name(arg, call)}"' for arg in call.args)
            body = f"{source}.drop(columns=[{columns}])"
        elif name == "filter":
            body = f"{source}[{self._filter_mask(source, call, func)}]"
        elif name == "select":
            body = self._select(source, call, func)
        else:  # pragma: no cover - _split_chain only yields _FRAME_METHODS
            raise self._refuse(func, name)
        return f"{target} = {body}" if assigned else body

    def _filter_mask(self, frame: str, call: ast.Call, func: ast.Attribute) -> str:
        if len(call.args) != 1:
            raise self._refuse(func, "filter")
        predicate = call.args[0]
        if not isinstance(predicate, ast.Call) or not isinstance(predicate.func, ast.Attribute):
            raise self._refuse(func, "filter")
        attr = predicate.func.attr
        column = predicate.func.value
        if not (isinstance(column, ast.Call) and isinstance(column.func, ast.Name) and column.func.id == "col"):
            raise self._refuse(func, "filter")
        name = self._column_name(column.args[0], column) if column.args else None
        if name is None:
            raise self._refuse(func, "filter")
        if attr == "isNotNull":
            return f'{frame}["{name}"].notna()'
        if attr == "isNull":
            return f'{frame}["{name}"].isna()'
        if attr == "rlike":
            if len(predicate.args) != 1:
                raise self._refuse(predicate.func, "rlike")
            # Wrap point 7 of 7: the second `r"…"` site, so `_raw` again. The
            # column name on the same line arrived through `_column_name` and is
            # already `_dq`-escaped for its own plain fence.
            #
            # `"rlike"` is `_string`'s owning construct, and this is D2a's
            # original defect (`bugfix.md` 1.6, `design.md` §5.3): *owner* is the
            # whole `col("email").rlike(...)` call, `col(` starts left of
            # `.rlike`, so `_refuse_here`'s leftmost rule named `col` for a
            # literal that belongs to `rlike`. This is the ONLY site that hands
            # an `rlike` pattern to the fence — outside filter position
            # `_method_call` refuses `rlike` by name before any literal is read
            # (`design.md` §12 item 1) — so the blast radius is one call site and
            # `_refuse_here` stays untouched for its other 8 callers (3.5).
            fence, pattern = _raw(self._string(predicate.args[0], predicate, "rlike"))
            return f'{frame}["{name}"].str.contains({fence}"{pattern}", na=False)'
        raise self._refuse(predicate.func, attr)

    def _select(self, frame: str, call: ast.Call, func: ast.Attribute) -> str:
        """``select(...)`` — new-frame semantics, or an honest refusal (3.6).

        An all-string argument list keeps the frame subscript form. Expression
        arguments build the frame column by column, which needs every output
        column name to be resolvable; an argument whose name cannot be
        resolved refuses rather than emitting a ``[[...]]`` subscript applied
        to a Series.
        """
        if not call.args or call.keywords:
            raise self._refuse(func, "select")
        if all(isinstance(arg, ast.Constant) and isinstance(arg.value, str) for arg in call.args):
            literals = ", ".join(f'"{self._column_name(arg, call)}"' for arg in call.args)
            return f"{frame}[[{literals}]]"

        columns: list[str] = []
        for arg in call.args:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                name = self._column_name(arg, call)
                expression = f'{frame}["{name}"]'
            else:
                expression = self._expr(arg, frame)
                resolved = _select_output_column(expression)
                if resolved is None:
                    # 3.6's fence: refuse rather than guess a name or emit a
                    # `[[...]]` subscript applied to the expression's result.
                    raise self._refuse(func, "select")
                name = resolved
            columns.append(f'"{name}": {expression}')
        return f"pd.DataFrame({{{', '.join(columns)}}})"


def _split_chain(node: ast.AST) -> "tuple[str | None, list[tuple[str, ast.Call, ast.Attribute]]]":
    """Decompose ``name.op(...).op(...)`` into its root name and its steps.

    Returns ``(None, [])`` unless the chain bottoms out in a bare ``Name``
    (a dataframe variable) and every step is a frame method this module
    knows. ``col("x").cast(...)`` therefore is NOT a frame chain — its root
    is a call — and falls to the expression translator, which is what keeps
    ``renamed = col("x").alias("y")`` converting.
    """
    steps: list[tuple[str, ast.Call, ast.Attribute]] = []
    current: ast.AST = node
    while isinstance(current, ast.Call) and isinstance(current.func, ast.Attribute):
        func = current.func
        if func.attr not in _FRAME_METHODS:
            return None, []
        steps.append((func.attr, current, func))
        current = func.value
    if not isinstance(current, ast.Name):
        return None, []
    steps.reverse()  # innermost operation first
    return current.id, steps


class PySpark2PandasConverter:
    """Convert a PySpark transform block to pandas equivalent code."""

    @classmethod
    def convert(cls, pyspark_code: str) -> dict:
        """Convert PySpark transform code to pandas equivalent.

        The input is dedented first: the real template output indents the
        transform block (4 or 8 spaces inside ``main()``), and the emitted
        pandas code must be valid top-level Python for the Code Interpreter.

        Returns ``{"code": <str>, "conversion_report": <dict>}`` where the
        code has ``import pandas as pd`` / ``import numpy as np`` prepended
        and the report documents what was converted::

            {
                "input_lines": <meaningful (non-blank, non-comment) input lines>,
                "output_lines": <meaningful emitted lines>,
                "constructs_converted": ["cast", "col", "withColumn", ...],
            }

        Raises:
            ConversionError: when the block is not valid Python, or when any
                construct in it cannot be translated. Nothing is ever
                commented out or dropped — a transform the converter cannot
                express in pandas fails the conversion loudly with the
                construct named (P28 R10.2).
        """
        dedented = textwrap.dedent(pyspark_code)
        lines = dedented.split("\n")

        def fail(construct: str, line_number: int, reason: "str | None" = None) -> ConversionError:
            # *reason* is forwarded, never derived here: only the site that
            # refused knows why. One of this closure's four callers carries one
            # — the `_Unconvertible` handler below, which passes `exc.reason`
            # straight through. The other three (a block that does not parse, a
            # line with no statement, an unsupported construct) have no reason,
            # so they stay classified downstream by their construct name exactly
            # as every refusal was before p40 (see ConversionError's docstring).
            index = min(max(line_number, 1), len(lines)) - 1
            return ConversionError(
                construct=construct, line=lines[index].strip(), line_number=line_number, reason=reason
            )

        try:
            with warnings.catch_warnings():
                # Generated PySpark carries regex literals in plain strings
                # (rlike("@example\\.com$")); an invalid-escape warning from
                # the model's text is not a finding about the converter.
                warnings.simplefilter("ignore", SyntaxWarning)
                tree = ast.parse(dedented)
        # LGR-8f3f9e1-1: caught by name, never as a bare except. A block that
        # is not valid Python is a refusal like any other — it names the
        # construct on the offending line so a reviewer sees what broke (2.11).
        except SyntaxError as exc:
            line_number = exc.lineno or 1
            index = min(max(line_number, 1), len(lines)) - 1
            occurrences = _text_constructs(lines[index])
            raise fail(occurrences or "python_syntax", line_number) from exc

        translator = _Translator(dedented)
        # Statements are keyed by their FIRST physical line so the emitted
        # block stays line-aligned with the input, which is what makes the
        # report's line counts and a refusal's line number mean the same
        # thing they meant before the rewrite.
        by_start: dict[int, ast.stmt] = {statement.lineno: statement for statement in tree.body}
        continuation: set[int] = {
            number
            for statement in tree.body
            for number in range(statement.lineno + 1, (statement.end_lineno or statement.lineno) + 1)
        }

        output_lines: list[str] = []
        constructs: set[str] = set()
        input_line_count = 0
        output_line_count = 0

        for line_number, raw_line in enumerate(lines, start=1):
            stripped = raw_line.strip()
            # Preserve blank lines and comments as-is (3.10)
            if not stripped or stripped.startswith("#"):
                output_lines.append(raw_line)
                continue

            input_line_count += 1
            statement = by_start.get(line_number)
            if statement is None:
                # A continuation line of a multi-line statement: counted as
                # input, but the statement itself was emitted at its start.
                if line_number in continuation:
                    continue
                raise fail(_text_constructs(raw_line) or "python_syntax", line_number)

            detected = _reported_constructs(statement)
            unsupported = next((o for o in _occurrences(statement) if not o.supported), None)
            if unsupported is not None:
                raise fail(unsupported.name, unsupported.lineno)

            try:
                converted = translator.statement(statement)
            except _Unconvertible as exc:
                raise fail(exc.construct, exc.lineno, reason=exc.reason) from None

            constructs |= detected
            if converted is None:
                # Import line — stripped; replaced by the pandas header.
                continue
            output_lines.append(converted)
            output_line_count += 1

        header = "import pandas as pd\nimport numpy as np\n\n"
        return {
            "code": header + "\n".join(output_lines),
            "conversion_report": {
                "input_lines": input_line_count,
                "output_lines": output_line_count,
                "constructs_converted": sorted(constructs),
            },
        }


# The one place a construct is still named from TEXT rather than from the
# parsed tree: a block that does not parse has no tree to read. It names the
# construct in a refusal and never decides whether a conversion succeeded —
# which is the difference between this and the residual scan it replaces.
_TEXT_CONSTRUCT = re.compile(
    r"\.(?:" + "|".join(sorted(_SUPPORTED_METHODS | _UNSUPPORTED_METHODS)) + r")\b"
    r"|(?<!\.)\b(?:" + "|".join(sorted(_SUPPORTED_FUNCTIONS | _UNSUPPORTED_FUNCTIONS)) + r")\s*\("
    r"|(?<!\.)\b(?:" + "|".join(sorted(_UNSUPPORTED_NAMES)) + r")\b"
)


def _text_constructs(line: str) -> "str | None":
    match = _TEXT_CONSTRUCT.search(line)
    if not match:
        return None
    return re.sub(r"[\s.()]", "", match.group(0)) or None
