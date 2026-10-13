# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: ``schema_mapping.get_target_schema`` (p33 task 3.1).

``lambda/agentic_data_ingestion_schema_mapping/lambda_function.py:234-305`` is
the direct-path target-schema read that p33 task 4.2 migrates to the shared
resolver. Before this file it was **undocumented and untested**: codegraph
reported zero covering tests (a true zero, not one of the ``patch.object``
false negatives — a grep for the symbol across ``tests/`` returned nothing),
and ``why`` on ``:244`` returns only the initial import commit
(``362fb514dd``) with **no ledger records at all**. Both facts are why the
characterization comes first. (The commit's subject line is cited by hash
only, deliberately: it contains the pre-rename project name, which
``make name-check``'s blocking legacy-name scan rejects outside
its documented allow-list. The hash identifies the commit; the subject was
decoration.)

**Expectations here are derived from the requirements, not from current
output.** A test written from the producer's output cannot detect the producer
drifting, because the output is its own oracle (``LGR-28b4d75-5``,
template/ratified). So each assertion below cites the requirement clause it
encodes, and where the requirement and the current code disagree the test is
marked ``xfail(strict=True)`` rather than rewritten to match the code.

**Status after task 4.2.** Seven ``xfail(strict=True)`` markers stood here when
this file was written. **Six are gone**, closed by 4.2's two-part change — the
direct path delegating to ``shared/target_schema_resolution.resolve_target_schema``
and the blanket ``except Exception`` narrowed so an already-classified failure is
not relabelled (design D5 / D13, constraints C15 / C16). **One remains, red by
decision**: ``test_a_throttle_on_the_by_name_lookup_does_not_present_as_schema_not_found``
sits in the fallback chain 4.2 leaves untouched, so requirement 2.8 is only
partially satisfied by p33 and that marker is the standing record of the known
exception. ``strict=True`` cuts both ways: the suite fails if the marker is left
on a test that starts passing, so the marker cannot be quietly forgotten either.

Requirement clauses encoded:

- **3.7** — the direct path resolves ``target_schema_bucket`` +
  ``target_schema_s3_key`` together and returns
  ``(normalized_schema, sha256_hash)``; the Glue Catalog, S3 Tables and
  uploaded-schema-by-name fallbacks still fire for pipelines carrying no key
  (the chain at ``:262-301``).
- **2.7** — a key with no bucket fails naming BOTH attributes, and must not
  silently fall through to the by-name lookup.
- **2.8** — an infrastructure read failure (throttle, access denied)
  propagates as a classified failure and must not present as "schema not
  found".

Reference shape for what correct looks like:
``schema_compare.get_target_schema_from_s3`` (``:369-385``) and
``check_target_schema_changed`` (``:434-457``), governed by ``LGR-7c9378d-1``
(template/ratified). This spec does **not** modify ``schema_compare``; it is
read for reference only (design D5).

The pipeline-record fixture is the attribute set the writer actually produces:
``create_pipeline`` splits the wizard's single ``s3://`` upload location into
``target_schema_bucket`` + ``target_schema_s3_key`` and writes them together
or not at all (``LGR-724f88c-1``, backfilled — it names this Lambda's
"preferred direct-path lookup" as the reason).

**Validates: Requirements 3.7, 2.7, 2.8**
"""

import contextlib
import hashlib
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Add Lambda dir and shared dir to path (sibling-test pattern)
_schema_mapping_dir = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_schema_mapping",
    )
)
sys.path.insert(0, _schema_mapping_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy Lambda-layer dependencies (mirrors test_read_path_errors.py)
sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(
    require_env=lambda name, default=None: default or name,
    get_aws_account_id=lambda: "777788889999",
)
sys.modules.setdefault("artifact_schema", MagicMock())
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))
sys.modules.setdefault("mapping_memory", MagicMock(MappingMemoryClient=None))

sys.modules.pop("lambda_function", None)
try:
    import lambda_function as _mod

    # p33 task 4.2 moved the direct path's S3 read out of this module and into the
    # shared resolver, so the module-level ``_mod.s3_client`` is no longer the seam
    # for it — it is still the seam for the fallback chain, which task 4.2 leaves
    # untouched. ``_patched`` below therefore injects the SAME mock at both seams, so
    # every assertion in this file keeps asserting on one client and keeps measuring
    # real behaviour rather than a stub of the delegation.
    import target_schema_resolution as _tsr

    # ``schema_mapping`` imports ``StepFailure`` but not its transient subclass, so
    # the classified type requirement 2.8 turns on is referenced from the shared
    # module directly.
    from step_errors import TransientStepFailure
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


PIPELINE_ID = "pipe-p33-target-schema"
TARGET_BUCKET = "adi-target-schemas-777788889999-us-west-2"
TARGET_KEY = f"{PIPELINE_ID}/uploaded_schema_1785844048926.json"

#: A JSON-Schema payload — the shape the wizard upload produces.
SCHEMA_BYTES = json.dumps(
    {"type": "object", "properties": {"id": {"type": "integer"}, "email": {"type": "string"}}}
).encode("utf-8")

#: The same schema in the custom ``fields`` array form, which requirement 3.7's
#: "normalized_schema" return contract has to put through ``normalize_schema_format``.
FIELDS_FORM_BYTES = json.dumps(
    {
        "fields": [
            {"name": "customer_id", "type": "int", "required": True},
            {"name": "signup_date", "type": "date"},
        ]
    }
).encode("utf-8")

#: The pipeline record as ``create_pipeline`` writes it for an uploaded target
#: schema (``LGR-724f88c-1``): the pair is present, plus the by-name attributes
#: that the fallback chain would use if the key were absent. Both are present on
#: a real record, which is exactly why a key-without-bucket record can fall
#: through to the by-name lookup unnoticed (requirement 2.7).
DIRECT_PATH_RECORD = {
    "pipeline_id": PIPELINE_ID,
    "pipeline_name": "testing",
    "status": "active",
    "target_schema_name": "uploaded_schema_1785844048926",
    "target_schema_bucket": TARGET_BUCKET,
    "target_schema_s3_key": TARGET_KEY,
    "target_type": "New S3 Bucket",
    "source_type": "S3_UPLOAD",
}


def _keyless(**overrides) -> dict:
    """A pipeline record carrying no ``target_schema_s3_key`` (fallback-chain input)."""
    record = {k: v for k, v in DIRECT_PATH_RECORD.items() if k not in ("target_schema_s3_key", "target_schema_bucket")}
    record.update(overrides)
    return record


def _s3_returning(content: bytes) -> MagicMock:
    s3 = MagicMock()
    body = MagicMock()
    body.read.return_value = content
    s3.get_object.return_value = {"Body": body}
    return s3


def _s3_raising(code: str) -> MagicMock:
    s3 = MagicMock()
    s3.get_object.side_effect = ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetObject")
    return s3


def _fallbacks() -> dict[str, MagicMock]:
    """Stand-ins for the three fallback branches at ``:262-301``."""
    return {
        "get_glue_catalog_schema": MagicMock(return_value=({"properties": {"g": {"type": "string"}}}, "hash-glue")),
        "get_s3_table_schema_from_config": MagicMock(
            return_value=({"properties": {"t": {"type": "string"}}}, "hash-s3t")
        ),
        "get_s3_table_schema": MagicMock(return_value=({"properties": {"l": {"type": "string"}}}, "hash-legacy")),
        "get_uploaded_json_schema": MagicMock(return_value=({"properties": {"u": {"type": "string"}}}, "hash-name")),
    }


@contextlib.contextmanager
def _patched(s3: MagicMock, fallbacks: dict[str, MagicMock]):
    with contextlib.ExitStack() as stack:
        # Fallback-chain seam (``get_uploaded_json_schema``, ``:308-361``).
        stack.enter_context(patch.object(_mod, "s3_client", s3))
        # Direct-path seam after the p33 4.2 delegation: the resolver builds its
        # own client lazily, so the same mock is injected there. Patching
        # ``_s3_client`` (the accessor) rather than the cache means the cache is
        # never consulted and no state leaks between tests.
        stack.enter_context(patch.object(_tsr, "_s3_client", lambda: s3))
        for name, stub in fallbacks.items():
            stack.enter_context(patch.object(_mod, name, stub))
        yield


class TestDirectPathResolvesThePairTogether:
    """Requirement 3.7: bucket and key resolved together, returning (schema, sha256)."""

    def test_reads_the_object_addressed_by_both_attributes_of_the_same_record(self):
        s3 = _s3_returning(SCHEMA_BYTES)
        fallbacks = _fallbacks()

        with _patched(s3, fallbacks):
            schema, schema_hash = _mod.get_target_schema(DIRECT_PATH_RECORD)

        s3.get_object.assert_called_once()
        assert s3.get_object.call_args.kwargs["Bucket"] == TARGET_BUCKET
        assert s3.get_object.call_args.kwargs["Key"] == TARGET_KEY
        assert schema == json.loads(SCHEMA_BYTES)
        assert schema_hash is not None

    def test_returns_the_sha256_of_the_raw_object_bytes(self):
        """C10 / ``LGR-dd80c6d-1``: sha256 over the bytes, never ``builtin hash()``."""
        s3 = _s3_returning(SCHEMA_BYTES)

        with _patched(s3, _fallbacks()):
            _, schema_hash = _mod.get_target_schema(DIRECT_PATH_RECORD)

        assert schema_hash == hashlib.sha256(SCHEMA_BYTES).hexdigest()

    def test_hash_is_stable_across_reads_and_moves_with_content(self):
        """Cross-invocation stability is what mapping reuse and drift detection rest on."""
        with _patched(_s3_returning(SCHEMA_BYTES), _fallbacks()):
            first = _mod.get_target_schema(DIRECT_PATH_RECORD)[1]
        with _patched(_s3_returning(SCHEMA_BYTES), _fallbacks()):
            second = _mod.get_target_schema(DIRECT_PATH_RECORD)[1]
        with _patched(_s3_returning(FIELDS_FORM_BYTES), _fallbacks()):
            other = _mod.get_target_schema(DIRECT_PATH_RECORD)[1]

        assert first == second
        assert first != other

    def test_returns_a_normalized_schema_for_the_fields_array_form(self):
        """Requirement 3.7's return contract is a *normalized* schema."""
        s3 = _s3_returning(FIELDS_FORM_BYTES)

        with _patched(s3, _fallbacks()):
            schema, _ = _mod.get_target_schema(DIRECT_PATH_RECORD)

        assert set(schema["properties"]) == {"customer_id", "signup_date"}
        assert schema["properties"]["customer_id"]["type"] == "integer"
        assert schema["required"] == ["customer_id"]

    def test_a_pipeline_carrying_the_pair_never_consults_a_fallback(self):
        """The by-name lookup is for keyless pipelines only — it must not shadow the pair."""
        fallbacks = _fallbacks()

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks):
            _mod.get_target_schema(DIRECT_PATH_RECORD)

        for stub in fallbacks.values():
            stub.assert_not_called()


class TestFallbackChainStillFiresForKeylessPipelines:
    """Requirement 3.7: Glue Catalog / S3 Tables / uploaded-by-name for no-key pipelines."""

    @pytest.mark.parametrize("target_type", ["Glue Data Catalog", "glue_catalog", "GLUE_CATALOG"])
    def test_glue_catalog_fallback(self, target_type):
        fallbacks = _fallbacks()
        record = _keyless(target_type=target_type)

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks):
            schema, schema_hash = _mod.get_target_schema(record)

        fallbacks["get_glue_catalog_schema"].assert_called_once_with(record["target_schema_name"])
        assert (schema, schema_hash) == fallbacks["get_glue_catalog_schema"].return_value

    @pytest.mark.parametrize("target_type", ["S3 Table Bucket", "s3_table", "EXISTING_S3_TABLE_BUCKET"])
    def test_s3_tables_fallback_uses_the_target_config(self, target_type):
        fallbacks = _fallbacks()
        record = _keyless(
            target_type=target_type,
            target_config={
                "table_name": "orders",
                "bucket_name": "my-table-bucket",
                "namespace": "sales",
            },
        )

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks):
            schema, schema_hash = _mod.get_target_schema(record)

        fallbacks["get_s3_table_schema_from_config"].assert_called_once_with("my-table-bucket", "orders", "", "sales")
        assert (schema, schema_hash) == fallbacks["get_s3_table_schema_from_config"].return_value

    def test_s3_tables_legacy_fallback_when_the_config_names_no_table(self):
        fallbacks = _fallbacks()
        record = _keyless(target_type="s3_table", target_config={})

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks):
            schema, schema_hash = _mod.get_target_schema(record)

        fallbacks["get_s3_table_schema"].assert_called_once_with(record["target_schema_name"])
        assert (schema, schema_hash) == fallbacks["get_s3_table_schema"].return_value

    def test_uploaded_by_name_fallback_is_the_default_and_is_scoped_to_the_pipeline(self):
        fallbacks = _fallbacks()
        record = _keyless(target_type="New S3 Bucket")

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks):
            schema, schema_hash = _mod.get_target_schema(record)

        fallbacks["get_uploaded_json_schema"].assert_called_once_with(record["target_schema_name"], PIPELINE_ID)
        assert (schema, schema_hash) == fallbacks["get_uploaded_json_schema"].return_value

    def test_a_pipeline_with_neither_a_key_nor_a_schema_name_fails(self):
        """No target-schema configuration at all is a configuration error, not a silent pass."""
        fallbacks = _fallbacks()
        record = _keyless()
        record.pop("target_schema_name")

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks), pytest.raises(ValueError, match="target_schema"):
            _mod.get_target_schema(record)

        for stub in fallbacks.values():
            stub.assert_not_called()


class TestHalfWrittenRecordIsNotAnAbsentSchema:
    """Requirement 2.7 / C1 (``LGR-7c9378d-1``, template/ratified).

    ``create_pipeline`` writes ``target_schema_bucket`` and
    ``target_schema_s3_key`` together or writes neither, so a key with no
    bucket is an incomplete record — not a pipeline that has no target schema.
    Every test in this class was a **defect** on the code as task 3.1 found it:
    the guard was ``if key and bucket:``, so a half-written record left the
    direct path and re-entered the fallback chain, where a real record's
    ``target_schema_name`` made the by-name lookup succeed and the run continued
    against a schema resolved from a different address than the one the record
    recorded.

    **CLOSED by p33 task 4.2.** The direct path now delegates to
    ``shared/target_schema_resolution.resolve_target_schema``, which reads the
    pair as a pair and raises naming both attributes. The three
    ``xfail(strict=True)`` markers that stood here were removed with the
    delegation — ``strict=True`` means they could not be left in place once the
    behaviour landed.
    """

    def test_key_without_bucket_does_not_fall_through_to_the_by_name_lookup(self):
        fallbacks = _fallbacks()
        record = {k: v for k, v in DIRECT_PATH_RECORD.items() if k != "target_schema_bucket"}

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks), contextlib.suppress(Exception):
            _mod.get_target_schema(record)

        fallbacks["get_uploaded_json_schema"].assert_not_called()

    def test_key_without_bucket_fails_naming_both_attributes(self):
        # The exception class is deliberately unconstrained — requirement 2.7
        # constrains what the failure must *name*, not its type.
        record = {k: v for k, v in DIRECT_PATH_RECORD.items() if k != "target_schema_bucket"}

        with (
            _patched(_s3_returning(SCHEMA_BYTES), _fallbacks()),
            pytest.raises(Exception, match="target_schema_bucket") as exc_info,
        ):
            _mod.get_target_schema(record)

        assert "target_schema_s3_key" in str(exc_info.value)

    def test_empty_bucket_string_is_rejected_like_an_absent_bucket(self):
        # The empty string is the value the live ``schema_compare`` failure
        # carried (`Invalid bucket name ""`, run run-2026-08-04-11-47-47-86d7b76c),
        # which is why it is asserted separately from an absent attribute.
        fallbacks = _fallbacks()
        record = {**DIRECT_PATH_RECORD, "target_schema_bucket": ""}

        with (
            _patched(_s3_returning(SCHEMA_BYTES), fallbacks),
            pytest.raises(Exception, match="target_schema_bucket"),
        ):
            _mod.get_target_schema(record)

        fallbacks["get_uploaded_json_schema"].assert_not_called()


class TestInfrastructureErrorsPropagateClassified:
    """Requirement 2.8 / C2 (``shared/step_errors.py:160-190``).

    ``raise_for_read_client_error`` is the classifier the requirement turns on:
    not-found returns normally, transient codes raise ``TransientStepFailure``
    so the Step Functions Retry policy fires, everything else raises
    ``StepFailure``.

    As task 3.1 found it, ``get_target_schema`` did not use the classifier at
    all — it called ``s3_client.get_object`` bare inside one broad ``try`` and
    converted every exception into ``ValueError``. **p33 task 4.2 closed this in
    two moves, and both were needed**: the direct path now goes through the
    shared resolver, which classifies; and the blanket ``except Exception`` that
    wrapped the whole function body was narrowed to ``except StepFailure: raise``
    plus the original ``ValueError`` for genuine configuration errors (design
    D13 / C15). The delegation alone would have produced the right class and
    then had it relabelled on the way out.

    The last test in this class is still ``xfail(strict=True)`` **by decision** —
    see its marker.
    """

    def test_direct_path_throttle_raises_transient_step_failure(self):
        with (
            _patched(_s3_raising("ThrottlingException"), _fallbacks()),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_target_schema(DIRECT_PATH_RECORD)

        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == "schema_mapping"

    def test_direct_path_access_denied_raises_step_failure(self):
        # Requirement 2.8: access denied must not be flattened into the same
        # ValueError as a configuration error.
        with (
            _patched(_s3_raising("AccessDenied"), _fallbacks()),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_target_schema(DIRECT_PATH_RECORD)

        assert exc_info.value.error_type == "permanent"

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "p33 DEFECT: the sharpest form of the 2.8 violation. "
            "get_uploaded_json_schema's per-candidate `except Exception: continue` "
            "(:349-352) swallows a throttle on every one of its six candidate "
            'keys and then raises "Could not find uploaded schema ... in any '
            'expected location" — a throttle presenting as schema-not-found. '
            "STAYS RED BY DECISION, and this marker is the standing record of it: "
            "the swallow is in the fallback chain at :262-301, which task 4.2 "
            "leaves untouched, and D13's narrowing does not reach it — by the time "
            "control arrives at the function's except clause there is no "
            "classified exception left, :349 already destroyed it. Requirement 2.8 "
            "is therefore only PARTIALLY satisfied by p33 and this is the known "
            "exception (design §Carried forward, tasks.md task 8). Do not remove "
            "the marker without retiring the carried-forward entry: strict=True "
            "means it also fails the suite if someone fixes the swallow and leaves "
            "the marker behind."
        ),
    )
    def test_a_throttle_on_the_by_name_lookup_does_not_present_as_schema_not_found(self):
        record = _keyless(target_type="New S3 Bucket")

        with (
            patch.object(_mod, "s3_client", _s3_raising("ThrottlingException")),
            patch.object(_mod, "get_aws_account_id", lambda: "777788889999"),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_target_schema(record)

        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert "not find" not in str(exc_info.value)

    def test_an_already_classified_step_failure_is_not_reclassified_on_the_way_out(self):
        # This was the migration's trap, and it is why the narrowing had to land
        # with the delegation (D13 / C15): the classified exception is raised from
        # a FALLBACK branch here, so it only survives because the clause wrapping
        # the whole function body now re-raises ``StepFailure`` untouched.
        fallbacks = _fallbacks()
        fallbacks["get_glue_catalog_schema"].side_effect = TransientStepFailure(
            "schema_mapping", "get_target_schema failed: ThrottlingException: simulated"
        )
        record = _keyless(target_type="glue_catalog")

        with _patched(_s3_returning(SCHEMA_BYTES), fallbacks), pytest.raises(_mod.StepFailure) as exc_info:
            _mod.get_target_schema(record)

        assert type(exc_info.value).__name__ == "TransientStepFailure"
