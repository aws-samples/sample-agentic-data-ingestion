# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Properties of the non-printable character class the check emits (p64 R8.1, task 2d.2).

The example-based module beside this one pins named codepoints — NUL, BEL, ESC, tab,
DEL, the C1 range. That covers the boundaries a reader will ask about; it cannot
cover the claim, which is universal: **every** string containing a flagged control
character is flagged, and **no** string built only of permitted characters is.

The pattern is read out of ``_partials/_nonprintable_check.py.j2`` by regex rather
than restated here, so these properties measure what the template actually emits. A
reworded class fails them rather than quietly leaving them measuring a stale literal.

WHY PYTHON'S ``re`` STANDS IN FOR SPARK'S ``rlike``. The emitted class is a bracketed
range of ``\\xNN`` escapes, a construct Java and Python regex agree on exactly. The
alternative is a Spark session, which puts this in the e2e layer and out of reach of
every gate short of real AWS. The narrow overlap is the reason the substitution is
honest — nothing in these properties depends on regex behaviour the two engines
disagree about, and the module asserts the class definition, not Spark's evaluation
of it.

Expectations derive from R8.1's enumeration ("NUL, BEL, ESC and the rest of C0 except
tab/newline/carriage return"), not from the pattern's current text.

Validates: Requirements 8.1
"""

from __future__ import annotations

import os
import re

from hypothesis import given, settings
from hypothesis import strategies as st

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "pyspark_templates")
CHECK_PARTIAL = os.path.join(TEMPLATES_DIR, "_partials", "_nonprintable_check.py.j2")

#: R8.1's class, derived from the requirement rather than from the pattern: C0 is
#: 0x00-0x1F, less tab, newline and carriage return.
FLAGGED = sorted(set(range(0x00, 0x20)) - {0x09, 0x0A, 0x0D})

#: Characters a source file may legitimately carry. The three permitted C0 controls
#: are IN here on purpose — they are the exclusions most likely to be lost in a
#: rewording, and a generator that never produced them could not catch that.
PERMITTED = [chr(c) for c in (0x09, 0x0A, 0x0D)] + [chr(c) for c in range(0x20, 0x7F)]


def _pattern() -> str:
    with open(CHECK_PARTIAL) as handle:
        source = handle.read()
    match = re.search(r'_adi_np_pattern = r"([^"]+)"', source)
    assert match, "could not find the emitted pattern assignment in the partial"
    return match.group(1)


PATTERN = re.compile(_pattern())

_permitted_text = st.text(alphabet=PERMITTED, max_size=40)


@settings(max_examples=10)
@given(
    prefix=_permitted_text,
    control=st.sampled_from([chr(c) for c in FLAGGED]),
    suffix=_permitted_text,
)
def test_any_string_containing_a_flagged_control_is_detected(prefix: str, control: str, suffix: str) -> None:
    """Property 1: contamination anywhere in the value is found.

    Position is what this adds over the example tests. A class that somehow anchored
    — or an implementation that only inspected a prefix — would pass a
    single-character example and miss a control character buried in a trailing notes
    column, which is the shape the field report described.

    **Validates: Requirements 8.1**
    """
    assert PATTERN.search(prefix + control + suffix) is not None


@settings(max_examples=10)
@given(value=_permitted_text)
def test_no_string_of_permitted_characters_is_ever_flagged(value: str) -> None:
    """Property 2: no false positives, including on tab, newline and carriage return.

    The direction that matters for adoption. The check is off by default, so its cost
    is a report entry an operator reads — and a class that flagged embedded newlines
    would produce a finding on nearly every text-sourced pipeline, at which point the
    check gets turned off rather than read. R8.1's three exclusions exist for that
    reason and this is the property that keeps them.

    **Validates: Requirements 8.1**
    """
    assert PATTERN.search(value) is None
