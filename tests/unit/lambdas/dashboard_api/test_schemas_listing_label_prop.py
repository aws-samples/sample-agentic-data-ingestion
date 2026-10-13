# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Property 1 — a Schema_Listing_Row carries a stored Display_Label, or omits the key.

p36-schema-naming task 1.3. **Validates: Requirements 1.1, 1.2, 4.1.**

**Expected state when this file lands: RED. Do not revert it, and do not "fix"
the assertion.** `GET /api/schemas` emits no Display_Label at all today —
`_collect_schema_rows`' uploaded block (`schemas.py:556-604`) sets `schemaId`,
`name`, `targetType`, `source`, `lastModified`, `version`, `fieldCount` and
`_object_key`, and nothing else — so there is nothing to characterize *from*.
Every expectation below is read off Requirements 1.1 and 1.2, which is what
Requirement 4.1 asks for and what design §6's red-window table records: this
property goes green when task 2.3's post-pass lands, and a reviewer who sees the
red and reverts has undone the only thing that would have caught the defect
(requirements risk 9).

The two halves of the partition, and why they are one test:

- **Requirement 1.1** — a row for an Annotated_Schema whose Context_Sidecar holds
  a **truthy** `display_name` carries that value **verbatim**. Untrimmed: the
  gate trims and the value does not (Requirement 2.4's phrasing, read from the
  storage side; `schemas.py:1622-1628` stores padding intact).
- **Requirement 1.2** — every other row omits the key entirely. **Absence, not
  `null`, and not `""`.** `LGR-44d601e-1` (**template / ratified**) settled this
  on the write side — clearing a label removes the key, and storing `""` was
  rejected because a stored `""` would have to be distinguished from absence at
  every reader — and `_schema_detail` already makes the same choice on the read
  side (`schemas.py:1083-1085`, gate at `:1115-1117`). A listing emitting `null`
  or `""` would be the third spelling of one fact.

Design §6 (What the reflection removed) is explicit that these are not two
properties: testing 1.1 alone would pass an implementation that emits `""` for
every schema.

**Where the expected key comes from, and why not from the producer.**
Requirement 4.7 forbids transcribing `"displayLabel"` into the assertion: a test
written from the producer's output cannot detect the producer drifting, because
the output is its own oracle (`LGR-28b4d75-5`, **template / ratified**). Its
failure mode — the UI seeing a shape its type does not declare, rendering nothing,
raising nothing — *is* item 1's defect one layer up. So the key is read at test
time out of the **consumer's** single definition of it: the `display_label:`
initialiser inside `normalizeSchema` (`use-schemas.ts:56`), the one place the
frontend states which wire keys it accepts. See `display_label_wire_keys` below
for what that buys and what it does not.

**Fixtures.** Everything about the world comes from task 1.2's shared
infrastructure in this directory's `conftest.py` — `label_worlds`,
`seed_label_world`, `label_world_env`, and `LabelWorld.expected_labels` /
`.unlabelled_ids`, which state Requirements 1.2 and 1.3's truthy gate rather than
importing it from the code under test. Both S3 addresses come from the handler's
own key functions, so this file never re-spells the Context_Sidecar's address
(`LGR-cad8032-1`, **template / ratified**).

No `moto.mock_aws()` and no boto3 client mock — the shared `mock_s3_buckets` /
`mock_lambda_env` fixtures are what the world is built on
(`tests/unit/test_no_duplicate_mocking_prop.py`; `LGR-adceb72-1`,
`LGR-aadd206-1`, both **backfilled**). `AWS_REGION` is deliberately not set
(`LGR-3e45e65-1`, **template / ratified**).
"""

import json
import re
from typing import Any

from hypothesis import HealthCheck, Phase, event, given, settings
from tests.unit.conftest import REPO_ROOT
from tests.unit.lambdas.dashboard_api.conftest import (
    LabelWorld,
    label_worlds,
    seed_label_world,
    sidecar_field_names,
)

# `label_world_env` is **not** imported: it is a fixture in this directory's
# `conftest.py`, so pytest resolves it by name in the signature below. The three
# names above are plain callables, which is what a Hypothesis `@given` body needs
# — a `@given` test cannot take a function-scoped fixture as a strategy source,
# which is the reason task 1.2 exposed them at module level in the first place.

# ---------------------------------------------------------------------------
# The expected key — one definition, and it is the consumer's
# ---------------------------------------------------------------------------
# `REPO_ROOT` is imported rather than recomputed for the same reason the S3 keys
# are: `tests/unit/conftest.py:55` already defines it and
# `test_deploy_interpreter_probe.py:21` already imports it.
_USE_SCHEMAS_TS = REPO_ROOT / "dashboard-react" / "src" / "hooks" / "use-schemas.ts"

#: The `display_label:` initialiser inside `normalizeSchema`. Anchored on the
#: property name followed by a colon so that prose mentioning
#: `s.display_label || s.schema_name` (`use-schemas.ts:296`) cannot match.
_DISPLAY_LABEL_INITIALISER = re.compile(r"^\s*display_label:\s*(?P<expr>.+?),\s*$", re.MULTILINE)

#: `raw.<identifier>` reads inside that initialiser, in source order.
_RAW_KEY_READ = re.compile(r"\braw\.(?P<key>\w+)")


def display_label_wire_keys() -> tuple[str, ...]:
    """Every wire key the frontend accepts a Display_Label under, in its order.

    Read out of `normalizeSchema`'s `display_label:` initialiser
    (`use-schemas.ts:56`), today
    ``(raw.displayLabel as string) ?? (raw.display_label as string) ?? undefined``,
    so this returns ``("displayLabel", "display_label")``.

    **Why the consumer and not the producer.** Requirement 4.7 says the expected
    key comes from the frontend `Schema` interface at test time rather than from
    the producer's output. `Schema.display_label` (`types.ts:156`) is the
    *TypeScript* field name and is not on the wire at all; the wire keys are
    stated exactly once, here, in the `??` chain that normalises a raw row. That
    chain is the single definition Requirement 4.7 is asking for — it is what
    every one of `schemaDisplayName`'s nine call sites ultimately reads through
    (requirements §2).

    **What this catches.** A producer that emitted `displayName`, or `label`, or
    stopped emitting anything, fails this property with a message naming the key
    the frontend was looking for. Transcribing `"displayLabel"` into the
    assertion would have caught none of those, because the assertion and the
    producer would have drifted together.

    **What it does not catch**, stated so nobody reads more into it: if the
    frontend and the producer were changed to a new key in the same commit, this
    property follows them. That is the intended coupling — the requirement is
    that the two agree and that the agreement live in one place, not that the
    string be frozen.

    The two structural assertions below are deliberate. Silently falling back to
    a literal if the regex missed would reintroduce exactly the transcription
    Requirement 4.7 forbids, so a shape change in `use-schemas.ts` has to fail
    loudly here instead.
    """
    source = _USE_SCHEMAS_TS.read_text(encoding="utf-8")
    initialisers = _DISPLAY_LABEL_INITIALISER.findall(source)
    assert len(initialisers) == 1, (
        f"Requirement 4.7 sources the expected Display_Label wire key from the one "
        f"`display_label:` initialiser in {_USE_SCHEMAS_TS.name} (`normalizeSchema`, ~:56), and "
        f"found {len(initialisers)} instead: {initialisers!r}. Either normalizeSchema was "
        f"restructured or a second definition of the key has appeared — resolve which one the API "
        f"is expected to satisfy rather than transcribing the key into this test."
    )
    keys = tuple(_RAW_KEY_READ.findall(initialisers[0]))
    assert keys, (
        f"No `raw.<key>` read found in {_USE_SCHEMAS_TS.name}'s `display_label:` initialiser "
        f"({initialisers[0]!r}). The frontend no longer states which wire key carries a "
        f"Display_Label, so there is nothing for the listing to be checked against."
    )
    return keys


def display_label_row_key() -> str:
    """The key a Schema_Listing_Row is expected to carry a Display_Label under.

    The **first** key of the `??` chain: the others are the fallbacks
    `normalizeSchema` tolerates from older producers, and a tolerant fallback is
    not a contract (`LGR-28b4d75-5`). `_schema_detail` emits the first spelling
    (`schemas.py:1115-1117`), and Requirement 1.3 requires the listing to agree
    with it.
    """
    return display_label_wire_keys()[0]


# ---------------------------------------------------------------------------
# Response helpers
# ---------------------------------------------------------------------------
def _listing_rows() -> list[dict[str, Any]]:
    """Call `get_schemas` and return its rows.

    `handlers.schemas` is imported here rather than at module scope:
    `handlers/common.py:29-37` `require_env`s nine table names at **import**
    time, so the import has to happen after `mock_lambda_env` has run. This
    directory's `conftest.py` documents the same constraint for the same reason.
    """
    from handlers.schemas import get_schemas

    response = get_schemas()
    assert response["statusCode"] == 200, (
        f"`GET /api/schemas` must be a 200 for a readable world. Requirement 1.10 degrades a "
        f"label, never the response. Got {response['statusCode']}: {response['body']}"
    )
    body = json.loads(response["body"])
    return body["schemas"]


def _describe(row: dict[str, Any]) -> str:
    """A row's keys and its label-ish values, for a counterexample worth reading."""
    return f"keys={sorted(row)} row={row!r}"


# ---------------------------------------------------------------------------
# Property 1
# ---------------------------------------------------------------------------
@given(world=label_worlds())
@settings(
    max_examples=100,
    # Each example wipes and reseeds a moto bucket and then runs the whole
    # three-source enumeration, which routinely exceeds Hypothesis's 200 ms
    # default. This property asserts a key's presence and its value, never
    # latency — Requirement 1.9's budget is task 2.7's subject and is
    # deliberately not a property (design §6).
    deadline=None,
    # One fixture serves every example by design; `seed_label_world` wipes the
    # bucket at the start of each one, so nothing an earlier example wrote is
    # visible to a later one.
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    # `Phase.explain` excluded, following `test_schema_rename_invariance_prop.py`'s
    # precedent and for the same measured reason: this property is RED from now
    # until task 2.3, and on every one of those runs the explain phase would
    # re-run several hundred further examples to annotate a failure whose cause
    # the assertion message already states verbatim. Shrinking is kept — a
    # minimal counterexample is exactly what is worth having when this fails for
    # a real reason after 2.3 lands.
    phases=[Phase.explicit, Phase.reuse, Phase.generate, Phase.target, Phase.shrink],
)
def test_a_row_carries_a_stored_label_and_omits_the_key_when_there_is_none(world: LabelWorld, label_world_env: str):
    """Property 1: a row carries a stored label, and omits the key when there is none.

    For any set of Uploaded_Schemas and any subset carrying a Context_Sidecar
    with a truthy `display_name`, every row for a schema in that subset carries
    the Display_Label key with the stored value verbatim, and every row outside
    it — including a schema whose sidecar holds `""` — has **no** Display_Label
    key at all.

    Feature: p36-schema-naming, Property 1: A row carries a stored label, and
    omits the key when there is none.

    **Validates: Requirements 1.1, 1.2, 4.1**
    """
    seed_label_world(world)
    key = display_label_row_key()

    rows = _listing_rows()
    uploaded = {row["schemaId"]: row for row in rows if row.get("source") == "uploaded"}

    # Not a clause of the property — a precondition for being able to state it.
    # A missing row would otherwise present as "no label", which is the one
    # confusion Requirement 1.2 is about.
    missing = sorted(set(world.schema_ids) - set(uploaded))
    assert not missing, (
        f"The listing dropped {len(missing)} seeded Uploaded_Schema(s) — {missing} — so there is "
        f"no row to judge a Display_Label on. Enumerated ids: {sorted(uploaded)}"
    )

    expected_labels = world.expected_labels
    event(f"schemas={len(world.schema_ids)} annotated={len(world.annotated_ids)} labelled={len(expected_labels)}")
    _, display_field = sidecar_field_names()
    if any(sidecar.get(display_field) == "" for sidecar in world.sidecars.values()):
        # The `""` sidecar is the case two independently written gates diverge on
        # and the reason Requirement 1.2 says absence rather than a value.
        event("world carries a legacy empty-string sidecar")

    # -- Requirement 1.1: the stored label, verbatim ---------------------------
    # Asserted first so that today's red names the missing label rather than
    # some downstream consequence of it.
    for schema_id, stored in sorted(expected_labels.items()):
        row = uploaded[schema_id]
        assert key in row, (
            f"Requirement 1.1: schema {schema_id!r} has {stored!r} stored as its Display_Label in "
            f"its Context_Sidecar, so its Schema_Listing_Row must carry {key!r}. It does not. "
            f"{_describe(row)}"
        )
        assert row[key] == stored, (
            f"Requirement 1.1: schema {schema_id!r}'s Display_Label must reach the listing "
            f"verbatim — untrimmed, unnormalised, as stored. "
            f"expected {stored!r}, got {row[key]!r}"
        )

    # -- Requirement 1.2: absence, not `null` and not `""` --------------------
    # Over every row rather than only over `world.unlabelled_ids`, so a row this
    # world did not seed cannot smuggle the key in either.
    for row in rows:
        if row.get("schemaId") in expected_labels:
            continue
        assert key not in row, (
            f"Requirement 1.2: schema {row.get('schemaId')!r} has no truthy `display_name` stored, "
            f"so its row must omit {key!r} entirely — absence, not {row[key]!r}. "
            f'A stored `""` clears a label rather than being one (`LGR-44d601e-1`), and a `null` '
            f"would be a third spelling of one fact. {_describe(row)}"
        )

    # The negative clause is only worth something if this world had somewhere to
    # go wrong; recorded rather than asserted, because a world of entirely
    # labelled schemas is a legitimate example (Requirement 1.8's ceiling).
    if world.unlabelled_ids:
        event(f"unlabelled schemas asserted absent: {len(world.unlabelled_ids)}")
