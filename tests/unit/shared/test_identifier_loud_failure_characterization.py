# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p55 task 6.7 — characterization of the loud-failure path W4 must preserve.

W4 (special characters / spaces in column names) sanitizes a
Non_Conforming_Column_Name **upstream** and hands the validator a name that
already passes. The risk the whole workstream carries is that an implementer
takes the cheaper route instead and relaxes `validate_identifier` — either by
widening the Identifier_Allowlist or by adding a sanitize-and-continue branch.
These assertions are the tripwire for that. R11.3 and R11.4 restate behaviour
that must survive W4 *unchanged*; this file is what makes "unchanged"
checkable.

The two assertions (p55 R12.1, R12.2):

1. An identifier failing the Identifier_Allowlist raises an error naming that
   identifier **and its sink context**.
2. `validate_identifier` returns its input **unmodified** for an identifier
   that satisfies the allowlist.

Expectations derive from the documented contract, **never from current
output** — a characterization test built from observed behaviour ratifies
whatever defect is in it. The two sources quoted, both read in full:

- `shared/identifier_utils.py`'s module docstring: *"A hostile CSV header must
  never become code. … validation failure raises IdentifierValidationError
  naming the offending identifier and the sink context. There is no
  sanitize-and-continue — the input is never modified, only accepted verbatim
  or rejected."* And `IdentifierValidationError`'s own docstring: it *"carries
  the offending `name` (verbatim, unmodified) and the sink `context` it was
  destined for, and puts both in the message so the failure is diagnosable
  from logs alone"*.
- `create_glue_pipeline/job_builders.py:342-354` (`_common_write_arguments`,
  the spec cites `:341-352`; re-verified this run — the docstring opens at
  `:342` and the `Raises:` block closes at `:354`, so the figure has drifted
  by one to two lines and the block is otherwise verbatim as quoted): the
  merge_key *"is validated against the Identifier_Allowlist BEFORE it enters
  DefaultArguments. An invalid merge_key fails job creation loudly, naming the
  offending identifier (R12.2) — it is never sanitized or defaulted away."*

What "sink context" means in the raised error, established by reading
`identifier_utils.py:54-68` rather than assumed: the caller-supplied `context`
argument, stored on the exception as `.context` and interpolated into the
message **raw** — `f"Invalid identifier {_display(name)} in {context}: …"`.
The *name* passes through `_display` (repr, capped at `_DISPLAY_LIMIT`); the
*context* does not, and is neither quoted nor capped. That asymmetry is
deliberate — `_DISPLAY_LIMIT` is documented as a cap on "how much of a hostile
identifier is echoed" — and it is the asymmetry W4-D2 mirrors when it
length-caps an emitted rename line. It is pinned below because the context is
the only part of the message that says *which* sink refused.

Existing coverage this file does **not** duplicate, and what it adds:

- `tests/unit/shared/test_identifier_utils.py` already pins the allowlist
  regex, the 255 boundary, a 42-entry hostile catalog, control-character
  escaping, the non-string paths and `validate_identifiers`. It asserts the
  context for exactly three hand-written literals and never asserts that the
  context is what *discriminates* two sinks, nor that it survives
  uncapped. Its unmodified-return assertion is twelve hand-picked examples.
- `tests/unit/lambdas/create_code/test_identifier_validation_sinks.py` and
  `tests/unit/lambdas/create_glue_pipeline/test_merge_key_identifier_validation.py`
  cover the two sinks, but their hostile catalogs are entirely injection
  payloads — quotes, semicolons, newlines, comment tokens. **W4's actual input
  class — a space, a hyphen, a leading digit — appears in neither.** That is
  the class this file and its `create_glue_pipeline` companion add.

Do NOTs honoured: 1 (no silent sanitization), 2 (the allowlist is neither
weakened nor bypassed), 3 (`validate_identifier` is not modified — this file
only reads it).

**Validates: Requirements 12.1, 12.2**
"""

import os
import string
import sys

import pytest

# Same convention as test_identifier_utils.py / test_step_names.py.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared"),
)

from identifier_utils import (
    MAX_IDENTIFIER_LENGTH,
    IdentifierValidationError,
    validate_identifier,
)

# The Non_Conforming_Column_Name class as the requirements glossary defines it:
# "a source column name that fails the Identifier_Allowlist — for example one
# containing a space, a hyphen, or a leading digit." These are the shapes W4
# renames, and they are the shapes that must keep failing here.
NON_CONFORMING_COLUMN_NAMES = [
    pytest.param("first name", id="embedded-space"),
    pytest.param("first-name", id="hyphen"),
    pytest.param("2024-total", id="leading-digit-and-hyphen"),
    pytest.param("1st_place", id="leading-digit"),
    pytest.param("total (usd)", id="space-and-parens"),
    pytest.param("order.date", id="dot-qualified"),
    pytest.param("qty/unit", id="slash"),
    pytest.param("price$", id="dollar-sign"),
    pytest.param("  padded  ", id="surrounding-whitespace"),
    pytest.param("col\tname", id="tab"),
    pytest.param("naïve", id="non-ascii"),
    pytest.param("", id="empty-header-cell"),
]

# Two distinct sink descriptions. Deliberately NOT the production strings —
# this file characterizes the validator's contract, and the contract is that
# whatever the caller passes is what the error reports. The production sink
# strings are pinned at their own sinks, in the create_glue_pipeline companion.
SINK_A = "DQDL rule field"
SINK_B = "MERGE statement merge_key"


class TestOffendingIdentifierAndSinkContextAreNamed:
    """R12.1 — the failure names the identifier and its sink context.

    **Validates: Requirements 12.1**
    """

    @pytest.mark.parametrize("name", NON_CONFORMING_COLUMN_NAMES)
    @pytest.mark.parametrize("context", [SINK_A, SINK_B], ids=["sink-a", "sink-b"])
    def test_error_reports_the_identifier_and_the_supplied_context(self, name, context):
        """Both values arrive on the exception, and both reach the message.

        `.name` and `.context` are asserted by identity, not equality: the
        documented contract is "verbatim, unmodified", and an implementation
        that normalised either before raising would still satisfy `==` for
        some inputs.
        """
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier(name, context)
        err = excinfo.value

        assert err.name is name
        assert err.context is context

        message = str(err)
        # The context is interpolated raw — verbatim, not quoted.
        assert context in message
        # The name is echoed through _display(), i.e. repr for anything at or
        # under the display cap. Asserted as repr rather than raw so a
        # control character can never mangle a log line unnoticed.
        assert repr(name) in message

    @pytest.mark.parametrize("name", NON_CONFORMING_COLUMN_NAMES)
    def test_the_sink_context_is_what_distinguishes_two_sinks(self, name):
        """ "Diagnosable from logs alone" requires the sink to be identifiable.

        The same offending identifier refused at two sinks must not produce
        two identical messages — otherwise a log line names the bad column but
        not which sink rejected it, and R12.1's "and its sink context" clause
        carries no information.
        """
        with pytest.raises(IdentifierValidationError) as a:
            validate_identifier(name, SINK_A)
        with pytest.raises(IdentifierValidationError) as b:
            validate_identifier(name, SINK_B)

        assert str(a.value) != str(b.value)
        assert a.value.context != b.value.context
        # And the difference is the sink, not the identifier: neither message
        # claims the other's sink.
        assert SINK_B not in str(a.value)
        assert SINK_A not in str(b.value)

    def test_sink_context_is_neither_truncated_nor_quoted_unlike_the_identifier(self):
        """The `_display` cap governs the identifier only, not the context.

        Pinned because W4-D2 mirrors `_DISPLAY_LIMIT`'s reasoning when it
        length-caps an emitted rename line, and because a future change that
        started capping the context would silently shorten the one part of the
        message that says which sink refused.
        """
        long_context = "MERGE statement merge_key via " + ("sink" * 200)
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier("first name", long_context)
        message = str(excinfo.value)
        assert long_context in message, "the sink context must survive in full"

        # The identifier, by contrast, is capped: a name long enough to exceed
        # the display cap is reported as a prefix plus its true length.
        long_name = "n" * 900
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier(long_name, SINK_A)
        message = str(excinfo.value)
        assert long_name not in message, "an oversized identifier must not be echoed in full"
        assert "900" in message, "the true length must still be reported"
        assert SINK_A in message, "the sink context is still named on the length path"

    @pytest.mark.parametrize("name", NON_CONFORMING_COLUMN_NAMES)
    def test_non_conforming_name_is_never_sanitized_or_defaulted_away(self, name):
        """do-NOT 1 in executable form: refusal, not repair.

        `job_builders.py:342-354` states it for the merge_key —
        "it is never sanitized or defaulted away" — and the module docstring
        states it generally: "no sanitize-and-continue". So the only two
        permitted outcomes are the input back verbatim, or a raise. Anything
        else (a sanitized name, a default, `None`) is a contract break.
        """
        with pytest.raises(IdentifierValidationError) as excinfo:
            validate_identifier(name, SINK_A)
        # Nothing was stripped, folded or substituted on the way to the raise.
        assert excinfo.value.name is name


class TestConformingIdentifierIsReturnedUnmodified:
    """R12.2 — an allowlist-satisfying identifier comes back unchanged.

    The validator sits inline inside an f-string at both sinks, so "returns
    its input" is load-bearing: `sql = f"SELECT {validate_identifier(col, …)}"`
    is only correct because the return value is the input. A validator that
    normalised — case-folded, stripped, truncated at 255 — would silently
    rename columns at every one of its call sites.

    **Validates: Requirements 12.2**
    """

    def test_every_length_from_one_to_the_maximum_is_returned_unmodified(self):
        """Swept rather than sampled, so no interior cap can hide.

        `test_identifier_utils.py`'s examples are single-character names and
        `"a" * 255`, both of which a validator capping at any length in
        between would still return intact-looking for the short case. Sweeping
        every length in range removes the question rather than arguing it.
        """
        for length in range(1, MAX_IDENTIFIER_LENGTH + 1):
            name = "c" + "o" * (length - 1)
            assert len(name) == length
            result = validate_identifier(name, SINK_A)
            assert result is name, f"length {length} was not returned unmodified"

    def test_every_allowlisted_character_survives_in_first_and_later_position(self):
        """No character in the allowed alphabet is rewritten or dropped.

        First position: `[A-Za-z_]`. Later positions: `[A-Za-z0-9_]`. Case is
        included on purpose — case folding is the most plausible accidental
        normalisation, and it is the one that breaks a case-sensitive target.
        """
        first_position = string.ascii_letters + "_"
        later_position = string.ascii_letters + string.digits + "_"

        for char in first_position:
            name = char + "tail"
            assert validate_identifier(name, SINK_A) is name

        for char in later_position:
            name = "head" + char
            assert validate_identifier(name, SINK_A) is name
            # And in the interior, not only at the end.
            name = "he" + char + "ad"
            assert validate_identifier(name, SINK_A) is name

    def test_the_context_argument_does_not_influence_the_returned_value(self):
        """The sink description is for the error message only.

        Pinned because W4 adds a rename step whose output feeds these same
        sinks; a validator that ever varied its return by sink would make the
        rename's correctness depend on which sink observed it.
        """
        name = "first_name"
        for context in (SINK_A, SINK_B, "", "Athena column", "unit test sink"):
            assert validate_identifier(name, context) is name
