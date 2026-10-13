# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.1 — design Property 1, plus the edge cases the allowlist forces.

# Feature: p55-format-support-and-teardown, Property 1: Rename targets are distinct and conforming

*For any* list of source column names, the produced Rename_Manifest's target
names are pairwise distinct and each satisfies the Identifier_Allowlist.

**Validates: Requirements 10.3, 10.4**

Subject
-------
``derive_rename_manifest`` in
``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:1954``, with its
three helpers ``_passes_identifier_allowlist`` (``:1845``),
``_sanitize_column_name`` (``:1861``) and ``_reserve_unique_target`` (``:1911``),
and its five module constants from ``:1812``. All of them are new in task 6.1, so
``why`` on their lines reports ``uncommitted_line`` and no ledger record governs
them — a gap, stated as a gap. ``codegraph query`` finds no pre-existing
sanitization or rename symbol anywhere in the tree, so this is greenfield rather
than a second definition of something.

Line figures elsewhere in this file's own module moved when 6.1 landed, because
6.1 adds an import block above them, and its own body above four of them:
``determine_approval_requirement`` 1553 → **1569**, its ``> 0.2`` clause
1651 → **1667**, ``create_approval_payload`` 1678 → **1694**,
``enforce_source_field_mappability`` 1815 → **2131**, ``store_mapping_result``
2099 → **2415**, ``lambda_handler`` 2182 → **2498**. Both figures are recorded
because the spec text cites the earlier set.

The derivation is placed **after** ``create_approval_payload`` rather than before
it, deliberately: 6.2 is its consumer, but task 1.2's characterization module
cites ``:1677``/``:1678`` in prose, and inserting above that line would have moved
it by the body's ~300 lines instead of the import block's 16.

Why this is a property and not a table of examples
--------------------------------------------------
Sanitization is **not injective** — ``first name``, ``first-name`` and
``first.name`` all yield ``first_name`` — so a naive implementation silently
drops a column, and a dropped column from a non-injective map is invisible to
any example test that happens not to collide. Hand-picked examples are exactly
the wrong instrument: the failure mode is *the collision you did not think of*.

Which means the generator has to actually collide, and a generator drawing random
text will not: two random strings essentially never sanitize to the same name. So
``_column_name`` draws from a small, deliberately overlapping alphabet — three
stems × seven separators × two tails, so ``first name`` / ``first-name`` /
``first.name`` / ``first  name`` co-occur routinely — plus four pools chosen to
collide with the *derived* forms rather than with each other:

* ``_CONFORMING_NAMES`` contains ``first_name``, ``first__name``, ``_2024_total``,
  ``column_1`` and ``column_2`` — i.e. the exact outputs the separator pool, the
  leading-digit pool and the sanitizes-to-nothing placeholder produce. So a
  conforming column and a proposal for a different column contend for one name,
  which is R10.4's case read literally (a conforming original "yields" itself).
* ``_LEADING_DIGIT_NAMES`` and ``_EMPTY_SANITIZE_NAMES`` reach the two branches
  the Identifier_Allowlist forces.
* ``_LONG_NAMES`` straddles 255 so the cap and the suffix interact.

**That the generator collides is asserted, not asserted-in-prose.**
``test_the_generator_reaches_each_hazard`` uses ``hypothesis.find`` for each
hazard in turn — a suffixed target, a truncated target, a suffix on an
already-255-character base, the placeholder, the leading-digit prefix, and a
proposal contending with a conforming column. ``find`` raises ``NoSuchExample``
if the strategy cannot produce one, so a generator that quietly stops exercising
a branch fails the suite rather than passing it vacuously.

Ledger and conventions
----------------------
``LGR-1e135d2-11`` (**template / ratified**) governs what happens if Property 1
ever fails: ``.hypothesis/examples`` is not committed, so a counterexample does
not survive a fresh clone and must be pinned as an example test in the tree.
The example tests below are where such a pin goes.

``LGR-b397ee2-2`` and ``LGR-c58f276-4`` are why patching here would be
``patch.object(_mod, …)`` rather than string-target patching — in the event this
file patches nothing, because the subject is pure. The ``sys.modules.pop`` /
``sys.path.insert`` preamble follows the sibling files in this directory:
``lambda_function`` is a name 23 Lambda directories share, so it
cross-contaminates between test modules. The fixtures are re-declared rather
than imported from a sibling for the reason task 3.2 records — an imported
helper closes over *that* module's ``_mod``, a different object from this one's.

``do-NOT 3``: ``validate_identifier`` is **not** modified by this task, and this
file imports it read-only, as the authority the derived targets are checked
against. ``codegraph callers validate_identifier`` reports 20 call-site entries
and ``codegraph impact`` 97 affected symbols on this interface; the spec cites 42
callers. Whichever figure is right, the conclusion is the same and it is why the
sanitization happens upstream.
"""

import os
import sys
from unittest.mock import MagicMock

import pytest
from hypothesis import HealthCheck, find, given, settings
from hypothesis import strategies as st

# Region is pinned by tests/unit/conftest.py (LGR-3e45e65-1); these are no-ops
# kept for parity with the sibling files in this directory.
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_mapping_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_mapping")
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Lambda-layer-only dependencies (the pattern in the sibling files here).
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
sys.path.insert(0, _schema_mapping_dir)
try:
    import lambda_function as _mod

    # The Identifier_Allowlist, imported rather than restated — the same reason the
    # subject imports it. `tests/unit/shared/test_identifier_utils.py` owns its
    # behaviour; task 6.7 owns its verbatim-or-raise contract.
    from identifier_utils import (
        MAX_IDENTIFIER_LENGTH,
        IdentifierValidationError,
        validate_identifier,
    )
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

_MANIFEST_ID = "mapping-hash-deadbeef"
_SCHEMA_HASH = "sha256:0123456789abcdef"


def _derive(names, **kwargs):
    """Call the subject with the two identity arguments filled in.

    Their values are irrelevant to Property 1 and are wired to real ones by
    tasks 6.2 and 6.4; this keeps every call site below about the names.
    """
    return _mod.derive_rename_manifest(
        names,
        kwargs.pop("manifest_id", _MANIFEST_ID),
        kwargs.pop("source_schema_hash", _SCHEMA_HASH),
        **kwargs,
    )


def _targets(names):
    return [entry["target_name"] for entry in _derive(names)["renames"]]


def _conforms(name):
    try:
        validate_identifier(name, "test")
    except IdentifierValidationError:
        return False
    return True


# --- The generator ------------------------------------------------------------
#
# Every pool here exists to collide with something. See the module docstring.

#: Separators chosen so that the three forms design §4.8 names as colliding
#: (`first name`, `first-name`, `first.name`) are drawn from one pool, plus `_`
#: so the ALREADY-CONFORMING form of the same name co-occurs with them, and `  `
#: (two spaces) so the no-run-collapsing decision is exercised.
_SEPARATORS = (" ", "-", ".", "_", "  ", "/", "@")
_STEMS = ("first", "last", "full")
_TAILS = ("name", "id")

_separated_names = st.builds(
    lambda stem, sep, tail: f"{stem}{sep}{tail}",
    st.sampled_from(_STEMS),
    st.sampled_from(_SEPARATORS),
    st.sampled_from(_TAILS),
)

#: A name whose sanitized form starts with a digit, so the leading-character
#: branch fires. `2024-total` is design §4.8's own illustrative case.
_LEADING_DIGIT_NAMES = ("2024-total", "2024 total", "1a2b3c", "0", "9 lives")
_leading_digit_names = st.sampled_from(_LEADING_DIGIT_NAMES)

#: Names that sanitize to NOTHING, so the placeholder branch fires. The empty
#: string is included because `validate_identifier` rejects it too, so it is a
#: Non_Conforming_Column_Name by the same rule as `!!!`.
_EMPTY_SANITIZE_NAMES = ("", "!!!", "---", "  ", "***", "()", "%%")
_empty_sanitize_names = st.sampled_from(_EMPTY_SANITIZE_NAMES)

#: Names straddling the 255 cap, in a shape where two draws can sanitize to the
#: same 255-character base — which is what makes the cap and the suffix interact.
_long_names = st.builds(
    lambda pad, sep, tail: "a" * pad + sep + tail,
    st.integers(min_value=250, max_value=258),
    st.sampled_from((" ", "-", "_")),
    st.sampled_from(("x", "y", "zz")),
)

#: Already-conforming names, which must pass through UNCHANGED and must never be
#: given a suffix. Chosen to be exactly what the other pools derive:
#: `first_name` ← `first name`; `first__name` ← `first  name`;
#: `_2024_total` ← `2024-total`; `column_1`/`column_2` ← the placeholder.
_CONFORMING_NAMES = (
    "first_name",
    "last_name",
    "full_id",
    "first__name",
    "_2024_total",
    "column_1",
    "column_2",
    "_",
)
_conforming_names = st.sampled_from(_CONFORMING_NAMES)

_column_name = st.one_of(
    _separated_names,
    _leading_digit_names,
    _empty_sanitize_names,
    _long_names,
    _conforming_names,
)

_column_name_lists = st.lists(_column_name, min_size=0, max_size=10)


# --- Property 1 ---------------------------------------------------------------


@settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
@given(names=_column_name_lists)
def test_rename_targets_are_distinct_and_conforming(names):
    """Feature: p55-format-support-and-teardown, Property 1.

    *For any* list of source column names, the produced Rename_Manifest's target
    names are pairwise distinct and each satisfies the Identifier_Allowlist.

    **Validates: Requirements 10.3, 10.4**

    Four assertions, and the last two are the ones a non-injective map breaks:

    * R10.3 — every target passes the Identifier_Allowlist, checked through
      ``validate_identifier`` itself rather than a restated regex.
    * R10.4 — the targets are pairwise distinct.
    * the targets are also distinct from every **conforming** column name, which
      is R10.4 read literally: a conforming original yields itself as its target,
      so a proposal equal to one would be two distinct originals with one target.
      This is the assertion that catches the dropped column — a manifest whose
      entries are internally distinct can still rename a column on top of one
      that was already fine.
    * every non-conforming original appears exactly once, so nothing is lost.
    """
    manifest = _derive(names)
    renames = manifest["renames"]
    targets = [entry["target_name"] for entry in renames]

    for target in targets:
        assert _conforms(target), f"target {target!r} does not satisfy the Identifier_Allowlist"

    assert len(set(targets)) == len(targets), f"targets are not pairwise distinct: {targets}"

    passthrough = {name for name in names if _conforms(name)}
    assert not (set(targets) & passthrough), (
        f"a proposed target collides with a column that was already conforming: {set(targets) & passthrough}"
    )

    distinct_non_conforming = [n for i, n in enumerate(names) if n not in names[:i] and not _conforms(n)]
    assert [entry["original_name"] for entry in renames] == distinct_non_conforming, (
        "every distinct non-conforming column must appear exactly once, in source-column order"
    )


# --- Evidence that the generator reaches each hazard --------------------------


def _triples(names):
    """``(original, pre-collision base, emitted target)`` for each manifest entry.

    The predicates below are written against the **base** rather than against the
    emitted string, so each one identifies the branch that was taken instead of
    pattern-matching an output that could arise another way. ``_2`` on the end of
    a target and a column genuinely named ``x_2`` are indistinguishable by
    string; ``target != base`` is not.
    """
    positions: dict[str, int] = {}
    for index, name in enumerate(names):
        if isinstance(name, str):
            positions.setdefault(name, index + 1)
    triples = []
    for entry in _derive(names)["renames"]:
        original = entry["original_name"]
        base = _mod._sanitize_column_name(original, positions[original])
        triples.append((original, base, entry["target_name"]))
    return triples


def _has_suffixed_target(names):
    return any(target != base for _original, base, target in _triples(names))


def _has_truncated_target(names):
    # `len(base) == MAX` alone would be satisfied by a name that is *exactly* 255
    # and never truncated, so the original has to be longer than the cap for this
    # to be evidence that the truncation branch ran.
    return any(
        len(base) == MAX_IDENTIFIER_LENGTH and len(original) > MAX_IDENTIFIER_LENGTH
        for original, base, _target in _triples(names)
    )


def _has_suffix_on_a_capped_base(names):
    # A base already at the cap that still has to make room for `_2`: the target
    # is 253 characters of base plus the suffix, still exactly 255.
    return any(
        len(base) == MAX_IDENTIFIER_LENGTH and target != base and len(target) == MAX_IDENTIFIER_LENGTH
        for _original, base, target in _triples(names)
    )


def _has_placeholder_target(names):
    return any(base.startswith("column_") for _original, base, _target in _triples(names))


def _has_leading_prefix_target(names):
    return any(
        base.startswith("_") and len(base) > 1 and base[1].isdigit() for _original, base, _target in _triples(names)
    )


def _proposal_contends_with_a_conforming_column(names):
    conforming = {name for name in names if _conforms(name)}
    return any(base in conforming for _original, base, _target in _triples(names))


@pytest.mark.parametrize(
    ("label", "predicate"),
    [
        ("a collision resolved by a numeric suffix", _has_suffixed_target),
        ("a base truncated at the 255 cap", _has_truncated_target),
        ("a suffix appended to a base already at the cap", _has_suffix_on_a_capped_base),
        ("the sanitizes-to-nothing placeholder", _has_placeholder_target),
        ("the leading-digit underscore prefix", _has_leading_prefix_target),
        ("a proposal contending with an already-conforming column", _proposal_contends_with_a_conforming_column),
    ],
)
def test_the_generator_reaches_each_hazard(label, predicate):
    """The generator's non-vacuity, asserted rather than claimed in prose.

    ``hypothesis.find`` raises ``NoSuchExample`` when the strategy cannot produce
    an example satisfying the predicate. So if a later edit narrows the pools and
    Property 1 stops exercising one of these branches, this test fails instead of
    the property quietly passing on inputs that never collide.
    """
    example = find(_column_name_lists, predicate, settings=settings(max_examples=2000))
    assert example is not None, label


# --- The edge cases the Identifier_Allowlist forces, each decided on purpose --


def test_a_leading_digit_gets_an_underscore_prefix_not_a_reordering():
    """`2024-total` → `_2024_total`.

    The allowlist's first character must be ``[A-Za-z_]``, and ``_`` is one of
    the two it admits. Design §4.8's illustrative generated-job block shows
    ``total_2024``, i.e. a token reordering; that block illustrates the job's
    SHAPE and the rule is specified only in §4.8's "Collision handling"
    paragraph, which says nothing about a leading digit. Reordering is rejected
    here because it is not total — it has no answer for ``2024`` or ``1a2b3c``.
    """
    assert _targets(["2024-total"]) == ["_2024_total"]
    assert _targets(["1a2b3c"]) == ["_1a2b3c"]
    assert _targets(["0"]) == ["_0"]


def test_a_name_with_no_surviving_character_becomes_its_position():
    """`!!!` at the third column → `column_3`, not `___` and not `column`.

    One-for-one substitution never yields the empty string for a non-empty input,
    so ``!!!`` would become ``___`` — which conforms, and which is still rejected:
    ``!!!``, ``---`` and ``***`` all yield it, so the suffix rule would number
    them ``___``, ``____2``, ``____3`` and no line would tell an approver which
    header it came from. The 1-based position does. A constant stem such as
    ``column`` is rejected for the same reason.
    """
    assert _targets(["ok", "fine", "!!!"]) == ["column_3"]
    assert _targets(["!!!", "---"]) == ["column_1", "column_2"]
    # The empty string is non-conforming by the same rule and takes the same path.
    assert _targets([""]) == ["column_1"]
    # A name where SOMETHING survives keeps the substitution — the placeholder is
    # scoped to the no-information case and does not swallow ordinary names.
    assert _targets(["-a-"]) == ["_a_"]


def test_the_placeholder_still_yields_to_a_real_column_of_that_name():
    """`column_1` as a real column keeps its name; the placeholder takes a suffix.

    The placeholder is a derived name like any other, so it goes through the
    collision rule rather than around it.
    """
    assert _derive(["!!!", "column_1"])["renames"] == [{"original_name": "!!!", "target_name": "column_1_2"}]


def test_a_name_over_the_cap_is_truncated_to_the_cap():
    """255 characters is the allowlist's own ceiling, so the base is trimmed to it."""
    (target,) = _targets(["a" * 300])
    assert target == "a" * MAX_IDENTIFIER_LENGTH
    assert len(target) == MAX_IDENTIFIER_LENGTH
    assert _conforms(target)


def test_the_suffix_makes_room_for_itself_inside_the_255_cap():
    """The cap-plus-suffix interaction: 255 + `_2` would be 257, so the base gives way.

    Both inputs sanitize to the same 255-character base (the ``_x`` / ``_y``
    tails fall off the end of the truncation). The first keeps it; the second
    gets ``base[:253] + "_2"``, which is 255 characters and conforms.
    *Rejected: exceeding the cap* (the target would fail the validator the
    manifest exists to satisfy) and *rejected: dropping the suffix and letting
    the names collide* (that is the dropped column R10.4 forbids).
    """
    first, second = _targets(["a" * 255 + " x", "a" * 255 + " y"])
    assert first == "a" * MAX_IDENTIFIER_LENGTH
    assert second == "a" * (MAX_IDENTIFIER_LENGTH - 2) + "_2"
    assert len(second) == MAX_IDENTIFIER_LENGTH
    assert first != second
    assert _conforms(first) and _conforms(second)


def test_an_already_conforming_column_keeps_its_name_in_either_input_order():
    """A conforming column passes through unchanged and is NEVER given a suffix.

    Both orders are asserted because this is what the two-pass derivation buys:
    conforming names are reserved before any proposal is made, so the suffix
    always lands on the non-conforming column even when it comes first in the
    header row. A single-pass version passes the first assertion and fails the
    second.
    """
    forward = _derive(["first_name", "first name"])
    assert forward["renames"] == [{"original_name": "first name", "target_name": "first_name_2"}]

    reverse = _derive(["first name", "first_name"])
    assert reverse["renames"] == [{"original_name": "first name", "target_name": "first_name_2"}]


def test_conforming_columns_produce_no_manifest_entry_at_all():
    """A Proposed_Rename's original must FAIL the allowlist, so a conforming
    column has nothing proposed about it and is absent from ``renames``."""
    manifest = _derive(["first_name", "last_name", "_", "column_1"])
    assert manifest["renames"] == []


def test_the_collision_suffix_runs_from_two_in_source_column_order():
    """R10.4: the first colliding name keeps the bare form, the second and
    subsequent take `_2`, `_3`, … — and which is which is decided by the input
    order and nothing else."""
    assert _targets(["first name", "first-name", "first.name"]) == [
        "first_name",
        "first_name_2",
        "first_name_3",
    ]


def test_reordering_the_input_changes_the_names():
    """The load-bearing consequence of the order, stated as a test.

    This is why ``derive_rename_manifest`` walks its argument once, in order,
    with no sort and no set iteration on the path from input order to emitted
    name: a caller that reorders the columns changes what a human approves.
    """
    forward = {e["original_name"]: e["target_name"] for e in _derive(["first name", "first-name"])["renames"]}
    reverse = {e["original_name"]: e["target_name"] for e in _derive(["first-name", "first name"])["renames"]}
    assert forward == {"first name": "first_name", "first-name": "first_name_2"}
    assert reverse == {"first-name": "first_name", "first name": "first_name_2"}
    assert forward != reverse


def test_runs_of_invalid_characters_are_not_collapsed():
    """One substitution per character, so `first  name` and `first name` do NOT
    collide. Collapsing reads better and makes the map more non-injective, which
    costs a suffix — and suffixes are what make a manifest hard to read."""
    assert _targets(["first name", "first  name"]) == ["first_name", "first__name"]


def test_the_manifest_shape_is_design_s_data_models_shape():
    """`manifest_id`, `source_schema_hash`, `approval_id`, ordered `renames` of
    `{original_name, target_name}` — and `approval_id` defaults to None, because
    no approver has decided yet (R10.10)."""
    manifest = _derive(["first name"])
    assert set(manifest) == {"manifest_id", "source_schema_hash", "approval_id", "renames"}
    assert manifest["manifest_id"] == _MANIFEST_ID
    assert manifest["source_schema_hash"] == _SCHEMA_HASH
    assert manifest["approval_id"] is None
    assert manifest["renames"] == [{"original_name": "first name", "target_name": "first_name"}]

    approved = _derive(["first name"], approval_id="approval-123")
    assert approved["approval_id"] == "approval-123"


def test_the_original_name_is_carried_verbatim_and_never_modified():
    """do-NOT 1: nothing is sanitized silently. The manifest is a proposal, and
    the original sits beside the proposal so the approver sees both."""
    hostile = 'first"; DROP TABLE x; --'
    (entry,) = _derive([hostile])["renames"]
    assert entry["original_name"] == hostile
    assert _conforms(entry["target_name"])


def test_an_empty_column_list_yields_an_empty_manifest():
    """Total on the empty input: a shape with no proposals, not an error."""
    manifest = _derive([])
    assert manifest["renames"] == []


def test_a_repeated_identical_original_yields_one_entry():
    """R10.4 speaks of two *distinct* originals. A source schema is a JSON object
    so its keys are unique and this is unreachable from the real producer;
    deduplicating keeps ``renames`` readable as (original → target) pairs for
    task 6.5, which two entries sharing an original could not be."""
    assert _derive(["a b", "a b"])["renames"] == [{"original_name": "a b", "target_name": "a_b"}]


def test_a_non_string_column_name_raises_naming_its_index_and_value():
    """Mirrors ``validate_identifier``'s own runtime ``isinstance`` check and its
    stated reason — these values arrive from external, user-influenced payloads,
    so the declared type is enforced rather than assumed. Classification of the
    failure belongs to the caller (task 6.2), not to a pure derivation."""
    with pytest.raises(TypeError) as excinfo:
        _derive(["ok", None])
    assert "index 1" in str(excinfo.value)
    assert "NoneType" in str(excinfo.value)


def test_validate_identifier_is_used_as_the_authority_not_reimplemented():
    """do-NOT 3 read positively: the sanitizer produces names that PASS the
    validator unmodified, and the validator is what says so.

    ``validate_identifier`` returns its input verbatim, so a target that passes
    is byte-identical to what the manifest carries — there is no second,
    validator-normalised form of the name.
    """
    for target in _targets(["first name", "2024-total", "!!!", "a" * 300]):
        assert validate_identifier(target, "test") == target
