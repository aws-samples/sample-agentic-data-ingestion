# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Conftest for unit/lambdas/dashboard_api/ — sys.path bootstrap.

Many test files in this directory import ``handlers.pipelines`` from the
``agentic_data_ingestion_dashboard_api`` Lambda. ``handlers.pipelines`` in turn
imports ``jdbc_helpers`` and ``audit_utils`` from the ``shared/`` directory
(bundled into the Lambda layer at ``/opt/python/`` in production).

In serial test runs, the first test file to be collected typically inserts
``shared/`` and the Lambda package onto ``sys.path``, and subsequent files
inherit that state. Under pytest-xdist with ``--dist=loadfile`` each worker
process picks up files in arbitrary order, so any test file might be the
first to be imported. Several test files in this directory rely on the
implicit ordering and don't set up ``sys.path`` themselves.

Running this conftest at collection-start guarantees the import paths are
in place before any test module in this directory is imported, regardless
of which file lands on a given worker.
"""

import os
import sys

_HERE = os.path.dirname(__file__)
_REPO_ROOT = os.path.normpath(os.path.join(_HERE, "..", "..", "..", ".."))
_SHARED_DIR = os.path.join(_REPO_ROOT, "shared")
_HANDLER_DIR = os.path.join(_REPO_ROOT, "lambda", "agentic_data_ingestion_dashboard_api")

for path in (_SHARED_DIR, _HANDLER_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

# ═══════════════════════════════════════════════════════════════════════════
# p36-schema-naming task 1.2 — the shared label-world and the S3 call recorder
# ═══════════════════════════════════════════════════════════════════════════
# Consumed by tasks 1.3, 1.4, 1.5, 1.6 (Properties 1, 2, 3, 5), 2.6 (Property 4)
# and 2.7 (the deadline and 1000/100 shape examples). Design §6 "Recording
# per-object reads (Properties 3, 4, 5)" says "no new mocking machinery", so
# everything below is either a strategy, a seeder, or the pass-through spy the
# repo already uses.
#
# WHY THIS LIVES IN conftest.py RATHER THAN A HELPER MODULE
# --------------------------------------------------------
# Three reasons, in order of weight:
#
# 1. **`tests/unit/conftest.py` set this precedent and stated it.** Its
#    `call_prereq` is "exposed at module level as well as through the
#    `call_prereq_fn` fixture: a Hypothesis `@given` test cannot take a
#    function-scoped fixture, so the property tests import this directly", and it
#    notes it lives in conftest.py because `test_naming_convention_prop.py`
#    allows exactly `conftest.py`, `__init__.py`, `schemas.py` and `helpers.py`
#    as non-`test_*` filenames under `tests/unit/`. Properties 3–5 have exactly
#    the same shape: `@given` bodies that need a seeder and a recorder as plain
#    callables, plus one fixture for the moto backends.
#    `from tests.unit.lambdas.dashboard_api.conftest import …` is the form
#    `test_deploy_bootstrap_targets_prop.py:21` already uses one level up.
#
# 2. **The `sys.path` bootstrap above is what makes `handlers.schemas`
#    importable, and it runs at collection start.** The address helpers below
#    import from that module (see the next comment block). A separate helper
#    module doing that import at module scope would depend on some other file in
#    this directory having been collected first — which is the precise failure
#    the bootstrap above exists to remove.
#
# 3. Fixtures have to be here anyway to be discovered without an import.
#
# Nothing here creates a `moto.mock_aws()` or a boto3 client mock —
# `tests/unit/test_no_duplicate_mocking_prop.py` forbids it, and the shared
# `mock_s3_buckets` / `mock_lambda_env` fixtures from `tests/fixtures/conftest.py`
# are what the world is built on. Nothing here sets `AWS_REGION`: the unit layer
# pins `AWS_REGION` and `AWS_DEFAULT_REGION` by assignment in
# `tests/unit/conftest.py` (`LGR-3e45e65-1`, **template / ratified**), and every
# reader — `_read_context_sidecar:803` included — resolves it at call time, so a
# fixture that assigned its own region would fight that fix.

import contextlib
import json
import string
import time
from collections.abc import Iterator, Mapping
from typing import Any, NamedTuple

import boto3
import pytest
from botocore.client import BaseClient
from botocore.exceptions import ClientError
from hypothesis import strategies as st

#: The target-schemas bucket the shared `mock_s3_buckets` fixture creates.
#: Asserted against the fixture's own dict in `label_world_env`, so a rename in
#: `tests/fixtures/conftest.py`'s `S3_BUCKET_DEFINITIONS` fails loudly here
#: rather than seeding a bucket nothing reads.
TARGET_SCHEMAS_BUCKET = "agentic-data-ingestion-target-schemas"

#: Kept off STS on first resolution. `get_aws_account_id` memoizes into a module
#: global (`shared/env_utils.py:13`), so this may well be a no-op in a full-suite
#: run where an earlier test resolved it first — **which is fine and is why the
#: bucket is pinned through `TARGET_SCHEMAS_BUCKET` instead.** The account id
#: never enters the address the handler reads, so nothing here needs to reset
#: that global, and resetting it is exactly the "defeat the memoization" move to
#: avoid: it would make one test's account id depend on collection order.
ACCOUNT_ID = "123456789012"

#: Everything a stored schema id may carry — `_UPLOAD_NAME_UNSAFE`
#: (`handlers/schemas.py:1257`) inverted. `.` and `/` are excluded for the reason
#: that constant gives: `_collect_schema_rows` derives an id back out of a body
#: key by stripping exactly `shared/` and `.json` (`:570`), so an id carrying
#: either substring would round-trip to a *different* id and the generated world
#: would disagree with the world the handler enumerates.
_ID_ALPHABET = string.ascii_letters + string.digits + "_-"


def _schemas_handler():
    """Import `handlers.schemas` lazily and return it.

    Deliberately not a module-level import. `handlers/common.py:29-37` calls
    `require_env` for nine table names at **import** time, so importing this
    module before a test's env is in place raises `OSError`. Every consumer here
    is called from inside a test, by which point `mock_lambda_env` has run.

    Keeping it lazy also keeps this conftest's own import side-effect-free
    beyond the `sys.path` bootstrap, which is what its docstring above promises:
    a conftest that bound `handlers.schemas` at collection start would decide
    the module's import-time env for every file in this directory.
    """
    from handlers import schemas as schemas_handler

    return schemas_handler


# ---------------------------------------------------------------------------
# The two addresses and the two sidecar field names — imported, never spelled
# ---------------------------------------------------------------------------
# `LGR-cad8032-1` (**template / ratified**) exists because two definitions of one
# address silently disabled a feature once. A fixture that spelled
# `f"schemas/uploaded/{id}-metadata.json"` itself would be a second definition of
# the Context_Sidecar's address, and a world seeded at a second address is a
# world the handler reads as unannotated — which would make Properties 3 and 5
# pass vacuously while proving nothing.
#
# So both keys come from the handler's own functions, `_context_sidecar_key`
# (`schemas.py:776`) and `_schema_body_key` (`:1735`), and both sidecar field
# names from `_SIDECAR_FIELD_CONTEXTS` / `_SIDECAR_DISPLAY_NAME` (`:2190-2191`).
#
# **This is the one place a fixture may take an address from the code under
# test.** An address is not an oracle: the *label rule* below is written from
# Requirements 1.2 and 1.3, not imported, because that is the behaviour the
# properties judge.


def sidecar_key(schema_id: str) -> str:
    """`schemas/uploaded/{schema_id}-metadata.json`, from its one definition."""
    return _schemas_handler()._context_sidecar_key(schema_id)


def sidecar_prefix() -> str:
    """`schemas/uploaded/`, from its one definition.

    The `Prefix` `_annotated_schema_ids` lists under, taken from
    `_CONTEXT_SIDECAR_PREFIX` (`schemas.py:799`) rather than spelled again — the
    same `LGR-cad8032-1` reason as `sidecar_key`. Task 2.6 needs it to fail
    **that** listing and not the `shared/` body enumeration: the two are
    different scopes in Requirement 1.10 and a test that failed both would be
    unable to tell them apart.
    """
    return _schemas_handler()._CONTEXT_SIDECAR_PREFIX


def body_key(schema_id: str) -> str:
    """`shared/{schema_id}.json`, from its one definition."""
    return _schemas_handler()._schema_body_key(schema_id)


def sidecar_field_names() -> tuple[str, str]:
    """`("field_contexts", "display_name")`, from their one definition."""
    handler = _schemas_handler()
    return handler._SIDECAR_FIELD_CONTEXTS, handler._SIDECAR_DISPLAY_NAME


# ---------------------------------------------------------------------------
# The world
# ---------------------------------------------------------------------------
class LabelWorld(NamedTuple):
    """One target-schemas bucket world: N Uploaded_Schemas, some Annotated.

    `schema_ids` is every Uploaded_Schema — a `shared/{id}.json` body exists for
    each. `sidecars` maps a schema id to the Context_Sidecar **document** written
    for it; its keys are the Annotated_Schemas (glossary: an Uploaded_Schema for
    which a Context_Sidecar object exists), which is the set Requirement 1.8's
    bound is written against.

    Three things follow from that definition and each one is a case some
    property turns on:

    - `sidecars` may be a **strict** subset of `schema_ids`, may be **empty**,
      and may be **all** of them. Requirement 1.8's bound permits
      `|Annotated| == |Uploaded|` — that is the sizing case, not the 100-of-1000
      shape Requirement 1.9 describes.
    - an Annotated_Schema's sidecar may carry **no** `display_name`, or one that
      is falsy. It is still Annotated, because the object exists and a read of it
      is still a read Requirement 1.8 counts.
    - a sidecar may carry `field_contexts` beside `display_name`, because the
      real object does (`schemas.py:2132-2133`) and the label reader must not be
      confused by it.
    """

    schema_ids: tuple[str, ...]
    sidecars: dict[str, dict[str, Any]]

    @property
    def annotated_ids(self) -> frozenset[str]:
        """The Annotated_Schemas — every id with a sidecar object.

        The bound in Requirement 1.8 is against this set's size, **not** against
        `expected_labels`: a sidecar carrying only `field_contexts` still has to
        be read before anyone can know it holds no label.
        """
        return frozenset(self.sidecars)

    @property
    def expected_labels(self) -> dict[str, str]:
        """Every schema id that must surface a Display_Label, and which one.

        **The gate is truthy, and this is Requirements 1.2 and 1.3's rule rather
        than the code's.** `""` and any other falsy `display_name` is *absent*
        from this map, so a surface emitting `""` fails rather than passes. That
        is the whole reason a legacy `""` sidecar is generated: `_schema_detail`
        already gates on truthiness (`schemas.py:1115-1116`), so an
        `is not None` listing would emit `""` where the detail emits nothing —
        two surfaces disagreeing about one schema, which is exactly what
        Requirement 1.3 forbids and what makes it checkable.

        Written out here rather than imported from task 2.1's
        `_display_label_from_sidecar` on purpose: that function does not exist
        yet, and a test taking its expectation from the code under test cannot
        detect that code drifting (`LGR-28b4d75-5`, **template / ratified**).

        The value is the stored string **verbatim** — untrimmed. Padding is
        preserved because a Display_Label is stored as submitted; only the
        emptiness *gate* is a trim-like question, and `""` is the only falsy
        string, so no trimming happens here either.
        """
        _, display_name = sidecar_field_names()
        return {
            schema_id: sidecar[display_name]
            for schema_id, sidecar in self.sidecars.items()
            if sidecar.get(display_name)
        }

    @property
    def unlabelled_ids(self) -> frozenset[str]:
        """Every schema that must surface **no** label at all.

        Both halves of Requirement 1.2: schemas with no sidecar, and Annotated
        ones whose sidecar holds a falsy or absent `display_name`.
        """
        return frozenset(self.schema_ids) - frozenset(self.expected_labels)


# ── Strategies ─────────────────────────────────────────────────────────
#: Both id shapes this system stores. The generated shape is what
#: `_unique_schema_id` produces and what D1 keeps opaque
#: (`uploaded_schema_{epoch_ms}`); the free-form shape is what a user-supplied
#: name sanitises to. Bounded at 24 characters, well inside
#: `_UPLOAD_NAME_MAX_LENGTH` (200) — the length limit is `upload_schema`'s
#: business, not the listing's.
_SCHEMA_IDS = st.one_of(
    st.integers(min_value=1_500_000_000_000, max_value=2_000_000_000_000).map(lambda ms: f"uploaded_schema_{ms}"),
    st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=24),
)

#: Whitespace that is not a space. A `.strip()`-based gate handles the first
#: three and a `!= ""` gate handles none of them; the two non-breaking and
#: ideographic spaces are there because Python's `str.strip()` does treat
#: `\u00a0` and `\u3000` as whitespace while a hand-rolled `!= " "` check does
#: not.
_WHITESPACE = " \t\n\r\u00a0\u2007\u202f\u3000"

#: Labels a user would plausibly type, in scripts that exercise encoding rather
#: than logic. The listing carries these through JSON and into a React table, so
#: a non-ASCII label is an ordinary case, not an attack.
_UNICODE_LABELS = st.sampled_from(
    [
        "顧客マスタ",
        "Ünïcødé Ñame",
        "Клиенты",
        "מאסטר לקוחות",
        "Customer 📊 Master",
        "café-données",
    ]
)

_NON_EMPTY_LABELS = st.one_of(
    _UNICODE_LABELS,
    st.text(min_size=1, max_size=40).filter(lambda s: s.strip() != ""),
)

#: Every Display_Label case the properties turn on, as one strategy.
#:
#: `st.just("")` is a branch of its own and it is the most load-bearing one.
#: `_schema_detail`'s gate is truthy (`if display_label:`), so a legacy sidecar
#: holding `""` — writable before `LGR-44d601e-1` (**template / ratified**) made
#: an empty label the absence of one — must be absent from **both** surfaces.
#: That is what makes Requirement 1.3 checkable rather than a statement two
#: independently-written gates happen to satisfy.
#:
#: Padded values are generated because a Display_Label is stored verbatim: the
#: gate trims, the value does not (Property 7's phrasing, same rule read from the
#: storage side).
DISPLAY_LABELS = st.one_of(
    st.just(""),
    st.text(alphabet=_WHITESPACE, min_size=1, max_size=4),
    st.tuples(
        st.text(alphabet=_WHITESPACE, min_size=1, max_size=3),
        _NON_EMPTY_LABELS,
        st.text(alphabet=_WHITESPACE, min_size=0, max_size=3),
    ).map(lambda parts: "".join(parts)),
    _NON_EMPTY_LABELS,
)

#: A small `field_contexts` map — the sidecar's other tenant (D3,
#: `LGR-acca05d-3`). Generated so the label reader is seen not to be confused by
#: it, and so a sidecar that is Annotated but carries **no** label is reachable.
_FIELD_CONTEXTS = st.dictionaries(
    keys=st.text(alphabet=_ID_ALPHABET, min_size=1, max_size=12),
    values=st.text(max_size=30),
    min_size=1,
    max_size=3,
)


@st.composite
def label_worlds(draw, min_schemas: int = 1, max_schemas: int = 6):
    """A `LabelWorld`: N Uploaded_Schemas, an arbitrary Annotated subset.

    The annotated **fraction** is drawn from an explicit three-way choice rather
    than from a per-schema coin flip, because 0 and all are the two cases a coin
    flip reaches vanishingly often as N grows and they are the two the
    requirements name: 0 is Requirement 1.8's floor and the case where the
    listing must perform **no** sidecar read at all, and all is the bound's
    permitted ceiling (`|Annotated| == |Uploaded|`).

    `max_schemas` is small by default because every example is materialised into
    moto. Tasks needing a large world drive it explicitly — and Requirement 1.9's
    1000/100 shape is an **example** with a fast stub, per design §6, not a
    hundred iterations of a thousand-object moto world.
    """
    schema_ids = draw(
        st.lists(_SCHEMA_IDS, min_size=min_schemas, max_size=max_schemas, unique=True),
    )
    coverage = draw(st.sampled_from(("none", "all", "some")))
    if coverage == "none":
        annotated = []
    elif coverage == "all":
        annotated = list(schema_ids)
    else:
        keep = draw(st.lists(st.booleans(), min_size=len(schema_ids), max_size=len(schema_ids)))
        annotated = [schema_id for schema_id, wanted in zip(schema_ids, keep, strict=True) if wanted]

    contexts_field, display_field = sidecar_field_names()
    sidecars: dict[str, dict[str, Any]] = {}
    for schema_id in annotated:
        document: dict[str, Any] = {}
        # An Annotated_Schema whose sidecar carries no `display_name` is a real
        # object — `_apply_to_context_sidecar` writes `field_contexts` alone on a
        # context edit with no label (`schemas.py:1620-1621`) — and it is the
        # case that separates "was read" from "produced a label", which is what
        # Requirement 1.8's bound is stated in terms of.
        if draw(st.booleans()):
            document[display_field] = draw(DISPLAY_LABELS)
        if draw(st.booleans()):
            document[contexts_field] = draw(_FIELD_CONTEXTS)
        sidecars[schema_id] = document

    return LabelWorld(schema_ids=tuple(schema_ids), sidecars=sidecars)


# ---------------------------------------------------------------------------
# Materialising a world into the shared moto bucket
# ---------------------------------------------------------------------------
#: One valid schema body. Deliberately constant: the listing derives an id from
#: the **key** and never reads the body (DD4, `schemas.py:565-570`), so varying
#: the bytes would vary nothing the label properties observe. Varying the bytes
#: belongs to whichever property judges `fields`.
SCHEMA_BODY = {
    "type": "object",
    "properties": {"customer_id": {"type": "integer"}, "email": {"type": "string"}},
    "required": ["customer_id"],
}
SCHEMA_BYTES = (json.dumps(SCHEMA_BODY, sort_keys=True, indent=2) + "\n").encode("utf-8")


def s3_client():
    """An S3 client in exactly the region the handler will resolve.

    `os.environ.get("AWS_REGION", "us-west-2")` is `_read_context_sidecar:803`
    verbatim, resolved **at call time** — the same expression, read the same way,
    so the seeding and the handler cannot land in different backends. Nothing
    here assigns `AWS_REGION` (`LGR-3e45e65-1`).
    """
    return boto3.client("s3", region_name=os.environ.get("AWS_REGION", "us-west-2"))


def target_schemas_bucket() -> str:
    """The bucket the handler will read, taken from the env var it reads it from.

    `_read_context_sidecar:806` resolves
    `os.environ.get("TARGET_SCHEMAS_BUCKET", f"adi-target-schemas-{account_id}-{region}")`.
    `label_world_env` sets that variable, so reading it back here means the
    seeder and the handler share **one** definition of the bucket rather than two
    expressions that happen to agree — and the account-id default, with its
    memoized global, never enters the address at all.
    """
    return os.environ["TARGET_SCHEMAS_BUCKET"]


def wipe_target_schemas_bucket() -> None:
    """Empty the target-schemas bucket.

    Hypothesis reuses one fixture across every example, so without this a body
    or sidecar from an earlier example survives into the next and a property
    passes — or fails — for a world it was not given. Runs in the harness,
    outside any recorder window, so its calls are never under assertion.
    """
    s3 = s3_client()
    bucket = target_schemas_bucket()
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket):
        keys = [{"Key": obj["Key"]} for obj in page.get("Contents", [])]
        if keys:
            s3.delete_objects(Bucket=bucket, Delete={"Objects": keys})


def seed_label_world(world: LabelWorld) -> LabelWorld:
    """Wipe the bucket and write `world` into it. Returns `world` for chaining.

    One `shared/{id}.json` per Uploaded_Schema and one
    `schemas/uploaded/{id}-metadata.json` per Annotated_Schema, both addressed
    through the handler's own key functions (`LGR-cad8032-1`).
    """
    wipe_target_schemas_bucket()
    s3 = s3_client()
    bucket = target_schemas_bucket()
    for schema_id in world.schema_ids:
        s3.put_object(Bucket=bucket, Key=body_key(schema_id), Body=SCHEMA_BYTES)
    for schema_id, document in world.sidecars.items():
        s3.put_object(
            Bucket=bucket,
            Key=sidecar_key(schema_id),
            Body=json.dumps(document, ensure_ascii=False).encode("utf-8"),
        )
    return world


@pytest.fixture
def label_world_env(mock_s3_buckets, mock_lambda_env, monkeypatch) -> str:
    """The shared moto backends plus the two addresses the handler resolves.

    `mock_s3_buckets` and `mock_lambda_env` are the shared fixtures from
    `tests/fixtures/conftest.py`; there is no `moto.mock_aws()` and no boto3
    client mock anywhere in this file
    (`tests/unit/test_no_duplicate_mocking_prop.py`). `mock_lambda_env` is not
    optional here — `handlers/common.py:29-37` `require_env`s nine table names at
    import time, so it is what makes `handlers.schemas` importable at all.

    Two env vars are set on top of it, and both are addresses rather than
    behaviour:

    - `TARGET_SCHEMAS_BUCKET`, because `mock_lambda_env` sets
      `TARGET_SCHEMA_BUCKET` (singular) and the schemas handler reads
      `TARGET_SCHEMAS_BUCKET` (plural) — different variables, so the shared
      fixture alone leaves the handler resolving the account-id default and
      reading a bucket that does not exist.
    - `AWS_ACCOUNT_ID`, to keep `get_aws_account_id` off STS on first
      resolution. It is memoized in a module global
      (`shared/env_utils.py:13`), so in a full-suite run this may be a no-op —
      **which changes nothing**, because the bucket comes from
      `TARGET_SCHEMAS_BUCKET` and the account id never reaches the address.
      Nothing here resets that global: doing so would make the resolved account
      id a function of collection order.

    `AWS_REGION` is deliberately **not** set (`LGR-3e45e65-1`, **template /
    ratified**).

    Returns the bucket name.
    """
    assert TARGET_SCHEMAS_BUCKET in mock_s3_buckets, (
        f"{TARGET_SCHEMAS_BUCKET!r} is not one of the buckets mock_s3_buckets created "
        f"({sorted(mock_s3_buckets)}) — S3_BUCKET_DEFINITIONS in tests/fixtures/conftest.py "
        f"has been renamed and this fixture would seed a bucket nothing reads"
    )
    monkeypatch.setenv("TARGET_SCHEMAS_BUCKET", TARGET_SCHEMAS_BUCKET)
    monkeypatch.setenv("AWS_ACCOUNT_ID", ACCOUNT_ID)
    return TARGET_SCHEMAS_BUCKET


# ---------------------------------------------------------------------------
# The call recorder — `test_schema_rename_invariance_prop.py:245-275`'s shape
# ---------------------------------------------------------------------------
# A **pass-through spy, not a mock.** The wrapper appends `(operation, params)`
# and then calls the original, so every observed call is a real call into moto's
# backend and every response is moto's. It wraps
# `botocore.client.BaseClient._make_api_call`, which sits **above** moto's
# `before-send` stubber and above botocore's retry handler — which is what lets
# it see calls from clients the handler constructs itself (the label post-pass
# builds its own, design §4.2), makes an injected `ClientError` deterministic
# rather than absorbed by a retry, and makes asserting the *absence* of a call
# possible at all.
#
# The four existing users of this shape — `test_schema_rename_invariance_prop.py`,
# `test_field_context_roundtrip_prop.py`, `test_schema_delete_reference_check_prop.py`
# and `test_schema_delete_reference_check_incomplete_prop.py` — keep their own
# private copies. Two of them are named in design §6 as tests that must keep
# passing **without amendment**, so this is the shared copy for new files rather
# than a refactor of theirs.


def named_key(params: Mapping[str, Any]) -> str:
    """Whatever a call names, for a readable failure message.

    `test_schema_rename_invariance_prop.py:266-271` verbatim. A counterexample
    reading `GetObject(Key='schemas/uploaded/x-metadata.json')` says which schema
    the property tripped on; one reading `GetObject({...30 keys...})` does not.
    """
    for field in ("Key", "CopySource", "Delete"):
        if field in params:
            return f"{field}={params[field]!r}"
    return "<no key in params>"


@contextlib.contextmanager
def recorded_s3_calls(
    *,
    fail_keys: Mapping[str, str] | None = None,
    fail_list_prefixes: Mapping[str, str] | None = None,
    delay_seconds: float = 0.0,
    services: tuple[str, ...] = ("s3",),
) -> Iterator[list[tuple[str, dict[str, Any]]]]:
    """Record every S3 call made inside the block, optionally failing some.

    Yields the live `(operation, params)` list. Tasks 1.5 and 1.6 read `Key` out
    of it to bound and compare reads.

    Args:
        services: which botocore services to record. `("s3",)` is what the four
            existing users of this shape watch and what Properties 3–5 need.
            Task 2.8's assertion is that a label came from the sidecar and
            **nothing else** — no DynamoDB read, no `GetObjectTagging`, no object
            metadata — and the tagging and metadata halves are S3 operations, but
            the DynamoDB half is not. Widening the recorder is what lets that
            negative be one assertion over one list rather than a second piece of
            machinery, which design §6 rules out ("no new mocking machinery").
            Calls from services not listed still pass through untouched.
        fail_keys: `{Key: error_code}`. A `GetObject` against a listed key raises
            `ClientError` with that code instead of reaching moto, and the call
            is still recorded — the attempt is the thing Requirement 1.10 is
            about. Per-key rather than per-call so task 2.6 can fail a chosen
            subset of the Annotated_Schemas and assert the rest still carry
            labels. `"NoSuchKey"` is a legitimate value: Requirement 1.10 turns
            on absence and unreadability being **different**, so a test needs to
            be able to inject both through one door.
        fail_list_prefixes: `{Prefix: error_code}`. A `ListObjectsV2` whose
            `Prefix` is listed raises `ClientError` with that code instead of
            reaching moto, and the call is still recorded. Added for task 2.6:
            Requirement 1.10 has **two** distinct scopes — the sidecar prefix
            listing failing (no label is resolvable at all) and individual reads
            failing — and `fail_keys` can only produce the second. Keyed on the
            prefix rather than applied to every listing because the same
            operation enumerates the schema **bodies** under `shared/`
            (`schemas.py:570`): failing that one instead would empty the listing,
            and the label pass would return early with nothing to report.
        delay_seconds: sleep before each `GetObject` passes through, for task
            2.7's slow-read stub. Real time, so the deadline under test is
            crossed by real elapsed time; task 2.7 keeps the injected deadline
            short and asserts that `get_schemas` **returns**, never a wall-clock
            duration.
    """
    failures = dict(fail_keys or {})
    listing_failures = dict(fail_list_prefixes or {})
    recorded: list[tuple[str, dict[str, Any]]] = []
    original = BaseClient._make_api_call

    def recording(self, operation_name, api_params):
        service = self.meta.service_model.service_name
        if service not in services:
            return original(self, operation_name, api_params)
        recorded.append((operation_name, dict(api_params)))
        if service == "s3" and operation_name == "ListObjectsV2":
            code = listing_failures.get(api_params.get("Prefix"))
            if code is not None:
                raise ClientError(
                    {
                        "Error": {
                            "Code": code,
                            "Message": (
                                f"p36 task 1.2 harness: ListObjectsV2 on prefix "
                                f"{api_params.get('Prefix')!r} refused with {code}"
                            ),
                        },
                        "ResponseMetadata": {"HTTPStatusCode": 403 if code == "AccessDenied" else 404},
                    },
                    operation_name,
                )
        if service == "s3" and operation_name == "GetObject":
            code = failures.get(api_params.get("Key"))
            if code is not None:
                raise ClientError(
                    {
                        "Error": {
                            "Code": code,
                            "Message": (
                                f"p36 task 1.2 harness: GetObject on {api_params.get('Key')!r} refused with {code}"
                            ),
                        },
                        "ResponseMetadata": {"HTTPStatusCode": 403 if code == "AccessDenied" else 404},
                    },
                    operation_name,
                )
            if delay_seconds:
                time.sleep(delay_seconds)
        return original(self, operation_name, api_params)

    BaseClient._make_api_call = recording
    try:
        yield recorded
    finally:
        BaseClient._make_api_call = original


def read_keys(calls: list[tuple[str, dict[str, Any]]], *, operation: str = "GetObject") -> list[str]:
    """Every `Key` one operation was called with, in call order.

    A list rather than a set on purpose: Requirement 1.8's bound is on the
    **number** of reads, so a duplicate read of one key has to stay visible.
    """
    return [params["Key"] for name, params in calls if name == operation and "Key" in params]
