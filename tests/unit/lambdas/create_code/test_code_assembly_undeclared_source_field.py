# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""A mapping naming a source column the source schema does not declare fails codegen.

THE DEFECT THIS CLOSES
----------------------
A deployed run failed with ``KeyError: 'email'`` inside the pandas test proxy.
The generated transform read ``df_transformed["email"]``; the test frame, built
strictly from the declared source schema
(``test_code/code_interpreter_runner.declared_source_fields``), had no ``email``
column. The transform prompt is never shown the source schema — deliberately, and
pinned (``transform_generation.generate_transform_with_ai``'s docstring, p33
design R4; ``test_target_schema_in_transform_prompt.py:471``) — so the model's
only notion of a legal source column is ``mappings[*].source_field``. Nothing
validated those names against the declared source columns at any stage.

WHY IT FAILS RATHER THAN RECOVERS
---------------------------------
Two softer options were rejected and the reasons belong here, because a later
reader will be tempted by both.

* **Treat the entry as unsourced** — annotate it in the prompt and drop its
  Completeness rule. The run then goes GREEN with a silently-null column and no
  quality rule, converting a human's approval of ``email → email`` into nothing.
* **Annotate but keep the Completeness rule**, so DQDL fails the batch. That
  costs the one-predicate invariant at ``code_assembly.py:167-173`` — the
  annotated set and the Completeness-skipped set are the same set, by
  construction, and that coupling is what stops the prompt and the ruleset
  drifting apart.

Failing loudly generates no bad code at all, and it matches the repo's ratified
stance one function over: ``generate_transform_with_ai``'s docstring records that
the silent ``df_transformed = df`` fallback was REMOVED because it "shipped
untransformed data under a green pipeline" (P28 R3.3). Silently nulling a claimed
column is the same species of quiet.

Because the fix does not touch ``_unsourced_target_field_names`` at all, the
branch A coupling (annotated set == Completeness-skipped set) is preserved for
free. ``test_code_assembly_unsourced_target_fields.py``'s
``test_the_annotated_set_is_the_completeness_skipped_set`` is the fence on that
and must stay green.

ORDERING IS LOAD-BEARING
------------------------
p37 R2.7: "an unsourced field with a hostile name must STILL fail code generation
naming the identifier. Do not restructure into 'skip first, validate later'." So
the new check runs AFTER the two existing identifier sinks
(``target_config.merge_key`` and ``_generate_dqdl_rules``' per-field
``validate_identifier``) and before ``generate_transform``. An input that fails
both an existing sink and this check still produces the existing
``IdentifierValidationError``, unchanged — pinned below, and pinned from the
other side by ``test_the_coupling_has_one_boundary_the_two_functions_do_not_share``.

LEDGER STATUS
-------------
**No record governs this validation's absence.** ``why`` on ``code_assembly.py``
returns the p37 commit ``9e76249a33`` (80 files — broad background) and the p33
commit ``b2f79efe49``; the records whose bodies name this area are
``LGR-9e76249-4`` (the contradiction check records and never blocks),
``LGR-9e76249-5`` (AST, not substring matching), ``LGR-9e76249-7`` (the
schema_mapping-side reader reads both lists), ``LGR-b2f79ef-10`` (the prompt is
threaded through all three shims) and ``LGR-b2f79ef-1`` (``schema_compliance``
became a real gate). None of them speaks to validating ``source_field`` against
the declared columns. ``decisions(concept="error-handling")`` returns exactly one
record (``LGR-b2f79ef-13``) and it is about a blanket ``except`` in a different
Lambda. That is a gap in the ledger, recorded as a gap and not as permission.

The convention this DOES follow is ratified: ``LGR-b2f79ef-11`` fails naming the
absent attribute rather than substituting a valid-looking placeholder, on the
ground that a fabricated value "matches nothing" downstream. An undeclared
``source_field`` is the same shape — a name that resolves to no column.
"""

import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_REGION", "us-east-1")

_CREATE_CODE_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_create_code")
)
_SHARED_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))
sys.path.insert(0, _SHARED_DIR)
sys.path.insert(0, _CREATE_CODE_DIR)

# `code_assembly` is imported DIRECTLY, for the reason
# `test_code_assembly_unsourced_target_fields.py` gives: it owns no AWS clients,
# reads no environment, and importing the handler module would put this file in
# the `sys.modules["lambda_function"]` tug-of-war.
import code_assembly as _ca

from identifier_utils import IdentifierValidationError
from step_errors import StepFailure

# ---------------------------------------------------------------------------
# Fixtures-by-hand. `pipeline_metadata` is the dict the handler assembles; it is
# a plain dict with no AWS in it, so the shared conftest fixtures (which exist
# for moto-backed DynamoDB/S3/SQS) are neither needed nor appropriate here.
# ---------------------------------------------------------------------------

_ABSENT = object()


def _entry(target_field: str, source_field=_ABSENT) -> dict:
    """One Field_Mapping_Entry; ``source_field`` omitted when ``_ABSENT``."""
    entry: dict = {"target_field": target_field, "confidence": 0.9}
    if source_field is not _ABSENT:
        entry["source_field"] = source_field
    return entry


def _object_schema(*names: str) -> dict:
    """The shape every live extractor produces: ``{type, properties}``.

    `schema_extract` writes this for CSV (S3 Select + `infer_schema_from_records`),
    JSON (Range GET + `infer_schema_from_records`), Parquet (PyArrow metadata) and
    JDBC (`_glue_columns_to_json_schema`).
    """
    return {"type": "object", "properties": {n: {"type": "string"} for n in names}}


def _pipeline_metadata(source_schema=_ABSENT) -> dict:
    metadata: dict = {
        "source_type": "S3_UPLOAD",
        "source_config": {},
        "target_config": {
            "type": "s3_tables",
            "bucket_name": "my-table-bucket",
            "namespace": "analytics",
            "table_name": "orders",
            "write_mode": "append",
            "source_format": "json",
        },
    }
    if source_schema is not _ABSENT:
        metadata["source_schema"] = source_schema
    return metadata


class _RecordingTransform:
    """Stands in for the injected AI transform generator and records whether it ran.

    The check must fire BEFORE the prompt is built, so "was this called" is a
    real assertion rather than a convenience.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, **kwargs) -> str:
        self.calls.append(kwargs)
        return "df_transformed = df"


def _generate(pipeline_metadata: dict, field_mappings: list, transform=None):
    transform = transform if transform is not None else _RecordingTransform()
    return (
        _ca.generate_code(
            pipeline_metadata=pipeline_metadata,
            mapping_result={"field_mappings": field_mappings},
            generate_transform=transform,
            quarantine_bucket="adi-quarantine-test",
        ),
        transform,
    )


# ===========================================================================
# The declared-columns read, and its precedence rule
# ===========================================================================


def test_declared_source_fields_reads_properties():
    assert set(_ca._declared_source_fields(_object_schema("id", "email"))) == {"id", "email"}


def test_declared_source_fields_falls_through_to_fields_when_properties_is_absent():
    """The legacy/alternate key. Only an ABSENT ``properties`` falls through."""
    schema = {"fields": {"id": {"type": "string"}, "email": {"type": "string"}}}

    assert set(_ca._declared_source_fields(schema)) == {"id", "email"}


def test_declared_source_fields_lets_an_empty_properties_win_over_a_populated_fields():
    """THE PRECEDENCE TRAP. ``properties`` wins whenever the KEY is present, even
    when its value is empty.

    That is p33 requirement 3.14's pinned behaviour, carried by
    ``test_code/code_interpreter_runner.declared_source_fields``: "``properties``
    wins whenever the KEY is present, even when its value is empty; only an absent
    ``properties`` falls through to ``fields``. That is the behaviour requirement
    3.14 pins, so it is reproduced rather than tidied."

    A ``dict.get(a, dict.get(b))`` tidied into ``schema.get("properties") or
    schema.get("fields", {})`` would silently invert this — hence the test.
    """
    schema = {"properties": {}, "fields": {"id": {"type": "string"}}}

    assert _ca._declared_source_fields(schema) == {}


@pytest.mark.parametrize("schema", [None, {}, {"type": "object"}])
def test_declared_source_fields_is_total_on_an_absent_or_bare_schema(schema):
    assert _ca._declared_source_fields(schema) == {}


def test_declared_source_fields_reads_nothing_from_an_array_shaped_schema():
    """A COVERAGE HOLE, recorded rather than papered over.

    ``schema_extract.extract_json_schema`` — the legacy path, still reachable as
    the Parquet fallback (``schema_extract/lambda_function.py:950``) and for a
    run with no ``source_format`` (``:969``) — returns ``{"type": "array",
    "items": {"type": "object", "properties": {...}}}`` for array-shaped input.
    The declared columns are nested under ``items``, so the top-level read finds
    nothing and the validation is INERT for those pipelines.

    Reproduced deliberately: the read must stay byte-equal to
    ``declared_source_fields``, which has the same hole and whose own consumer —
    the test frame builder — therefore also builds no columns for such a schema.
    Descending into ``items`` here without changing it there would make the
    guard refuse mappings the frame builder cannot represent either way.
    """
    schema = {"type": "array", "items": {"type": "object", "properties": {"id": {"type": "string"}}}}

    assert _ca._declared_source_fields(schema) == {}


# ===========================================================================
# The happy path, and the three ways the check must stay out of the way
# ===========================================================================


def test_every_mapped_source_field_declared_generates_code():
    code, transform = _generate(
        _pipeline_metadata(_object_schema("id", "email_addr")),
        [_entry("cust_id", "id"), _entry("email", "email_addr")],
    )

    assert transform.calls, "the transform generator must still run on a clean mapping"
    assert "df_transformed = df" in code


def test_an_empty_source_schema_does_not_fail():
    """CONSTRAINT: an empty declared set skips the check entirely.

    ``{"type": "object", "properties": {}}`` is what ``schema_extract`` writes
    for a source it could parse but found no records in
    (``extract_schema_from_json``'s no-records branch, ``:315``;
    ``infer_schema_from_records``'s empty-records branch, ``:159``). Validating
    against an empty declared set would fail EVERY such pipeline — the single
    most likely way to turn this bugfix into an outage.
    """
    code, transform = _generate(
        _pipeline_metadata(_object_schema()),
        [_entry("cust_id", "id"), _entry("email", "email_addr")],
    )

    assert transform.calls
    assert "df_transformed = df" in code


def test_an_absent_source_schema_key_does_not_fail():
    """Same rule for the key being absent. ``generate_code`` reads
    ``pipeline_metadata.get("source_schema", {})``, and the handler omits the key
    when the ``schema_extract`` state row is missing and the event carries no
    schema either (``create_code/lambda_function.py:459-462``). Requirement 2.6
    governs the sibling ``target_schema`` case the same way.

    This is also what keeps every pre-existing ``generate_code`` test green —
    none of them supplies a ``source_schema``.
    """
    code, transform = _generate(
        _pipeline_metadata(),
        [_entry("cust_id", "id"), _entry("email", "email_addr")],
    )

    assert transform.calls
    assert "df_transformed = df" in code


@pytest.mark.parametrize(
    "empty", [None, {}, {"properties": {}}, {"fields": {}}, {"properties": {}, "fields": {"a": {}}}]
)
def test_no_declared_column_means_no_check(empty):
    code, transform = _generate(_pipeline_metadata(empty), [_entry("cust_id", "nowhere")])

    assert transform.calls
    assert "df_transformed = df" in code


@pytest.mark.parametrize("unsourced", [None, "", "   ", "\t\n ", 123, ["mobile"], {"n": "mobile"}, True])
def test_an_entry_with_no_usable_source_field_is_not_this_check_s_business(unsourced):
    """CONSTRAINT: ``_has_usable_source_field`` already classifies these as
    unsourced and C12 annotates them. This check applies only to an entry whose
    ``source_field`` IS a usable string the schema does not declare. Making an
    unsourced entry fail here would duplicate — and contradict — p37 R2.1/R2.2.
    """
    code, transform = _generate(
        _pipeline_metadata(_object_schema("id")),
        [_entry("cust_id", "id"), _entry("phone", unsourced)],
    )

    assert transform.calls
    assert "df_transformed = df" in code


def test_an_entry_with_an_absent_source_field_key_is_not_this_check_s_business():
    _, transform = _generate(_pipeline_metadata(_object_schema("id")), [_entry("phone")])

    assert transform.calls


@pytest.mark.parametrize("mapping", [None, 3, "phone", ["phone"], ()])
def test_a_non_dict_mapping_entry_is_not_an_offender(mapping):
    """A ``mappings`` list is model output parsed as returned, so an element can
    be a bare string. ``_has_usable_source_field`` is total on those and answers
    False, so they are not offenders and this check does not raise.

    Asserted on the validator rather than through ``generate_code``, because
    ``_generate_dqdl_rules`` is NOT total on a non-dict entry — ``mapping.get``
    at ``code_assembly.py:360`` raises ``AttributeError`` — and that is
    pre-existing behaviour this change deliberately leaves alone. Routing this
    through ``generate_code`` would assert a property the function does not have
    and has never had.
    """
    _ca._validate_mapped_source_fields([_entry("cust_id", "id"), mapping], _object_schema("id"))


def test_the_fields_only_schema_shape_is_validated_against():
    """A ``fields``-keyed schema still constrains the mapping."""
    schema = {"fields": {"id": {"type": "string"}}}

    with pytest.raises(StepFailure) as exc_info:
        _generate(_pipeline_metadata(schema), [_entry("email", "email_addr")])

    assert "email_addr" in exc_info.value.message


# ===========================================================================
# The failure itself
# ===========================================================================


def test_one_undeclared_source_field_fails_naming_the_field():
    transform = _RecordingTransform()

    with pytest.raises(StepFailure) as exc_info:
        _generate(
            _pipeline_metadata(_object_schema("id", "name")),
            [_entry("cust_id", "id"), _entry("email", "email")],
            transform=transform,
        )

    failure = exc_info.value
    assert "email" in failure.message
    assert failure.step_name == "create_code"
    assert failure.error_type == "permanent"
    assert transform.calls == [], "no prompt may be built from a bad mapping"


def test_the_message_names_the_field_the_target_and_the_declared_columns():
    """Actionability. An operator hitting a false positive must be able to see,
    from the message alone, which mapping is wrong and what the source actually
    declares — without opening a log group."""
    with pytest.raises(StepFailure) as exc_info:
        _generate(_pipeline_metadata(_object_schema("id", "name")), [_entry("email", "email")])

    message = exc_info.value.message
    assert "email" in message  # the offending source_field
    assert "id" in message and "name" in message  # the declared columns


def test_the_failure_details_carry_the_offenders_and_the_declared_columns():
    with pytest.raises(StepFailure) as exc_info:
        _generate(
            _pipeline_metadata(_object_schema("id")),
            [_entry("email", "email"), _entry("phone", "mobile")],
        )

    details = exc_info.value.details
    assert details["undeclared_source_fields"] == ["email", "mobile"]
    assert details["declared_source_fields"] == ["id"]


def test_several_undeclared_source_fields_are_all_named():
    """No partial report: every offender is named in one failure, so one fix pass
    resolves the mapping instead of one-offender-per-run whack-a-mole."""
    with pytest.raises(StepFailure) as exc_info:
        _generate(
            _pipeline_metadata(_object_schema("id")),
            [_entry("cust_id", "id"), _entry("email", "email"), _entry("phone", "mobile"), _entry("fax", "faxno")],
        )

    message = exc_info.value.message
    for offender in ("email", "mobile", "faxno"):
        assert offender in message


def test_an_offender_is_named_once_even_when_two_entries_share_it():
    with pytest.raises(StepFailure) as exc_info:
        _generate(
            _pipeline_metadata(_object_schema("id")),
            [_entry("email", "contact"), _entry("email_alt", "contact")],
        )

    assert exc_info.value.details["undeclared_source_fields"] == ["contact"]


def test_an_offender_alongside_a_legitimately_unsourced_entry_still_fails():
    """The two conditions are independent. The unsourced ``phone`` is handled by
    C12 + the R2.1 rule skip and must not absorb the undeclared ``email``."""
    with pytest.raises(StepFailure) as exc_info:
        _generate(
            _pipeline_metadata(_object_schema("id")),
            [_entry("cust_id", "id"), _entry("phone", None), _entry("email", "email")],
        )

    details = exc_info.value.details
    assert details["undeclared_source_fields"] == ["email"]
    assert "phone" not in details["undeclared_source_fields"]


def test_membership_is_exact_and_does_not_case_fold():
    """CONSTRAINT: exact match. A near-miss is precisely the defect being caught,
    so ``Email`` against a declared ``email`` is an offender, not a match."""
    with pytest.raises(StepFailure) as exc_info:
        _generate(_pipeline_metadata(_object_schema("email")), [_entry("email", "Email")])

    assert exc_info.value.details["undeclared_source_fields"] == ["Email"]


def test_membership_does_not_match_on_a_prefix():
    with pytest.raises(StepFailure):
        _generate(_pipeline_metadata(_object_schema("email_address")), [_entry("email", "email")])


def test_a_whitespace_padded_source_field_is_an_offender():
    """``_has_usable_source_field`` accepts ``"  email  "`` — it strips only to
    decide non-emptiness, never to rewrite the value. The name is compared as
    given, so a padded value fails and the message shows the padding. That is the
    actionable outcome: the alternative (trimming here) would let
    ``df["email "]`` reach the proxy and reproduce the original ``KeyError`` one
    space over.
    """
    with pytest.raises(StepFailure) as exc_info:
        _generate(_pipeline_metadata(_object_schema("email")), [_entry("email", "  email  ")])

    assert exc_info.value.details["undeclared_source_fields"] == ["  email  "]


# ===========================================================================
# Precedence: the existing identifier sinks still win
# ===========================================================================


def test_identifier_validation_still_wins_over_the_new_check():
    """CONSTRAINT (p37 R2.7): an input that fails BOTH the existing per-field
    identifier sink and this check must still produce the existing
    ``IdentifierValidationError``, unchanged.

    ``bad;name`` is hostile AND ``nowhere`` is undeclared. The identifier error
    is the one that must surface, because P28 R12.1's injection sink is the more
    fundamental fact about the input.
    """
    with pytest.raises(IdentifierValidationError) as exc_info:
        _generate(_pipeline_metadata(_object_schema("id")), [{"target_field": "bad;name", "source_field": "nowhere"}])

    assert exc_info.value.name == "bad;name"
    assert "DQDL rule field" in str(exc_info.value)


def test_identifier_validation_wins_even_when_the_offender_comes_first():
    """Precedence is global, not per-entry: the offending mapping is earlier in
    the list than the hostile one and the identifier error still wins."""
    with pytest.raises(IdentifierValidationError):
        _generate(
            _pipeline_metadata(_object_schema("id")),
            [_entry("email", "nowhere"), {"target_field": "bad;name", "source_field": "id"}],
        )


def test_the_hostile_merge_key_sink_still_wins():
    """The other identifier sink. A hostile ``target_config.merge_key`` alongside
    an undeclared ``source_field`` still fails naming the merge_key."""
    metadata = _pipeline_metadata(_object_schema("id"))
    metadata["target_config"]["merge_key"] = "bad;key"

    with pytest.raises(IdentifierValidationError) as exc_info:
        _generate(metadata, [_entry("email", "nowhere")])

    assert "merge_key" in str(exc_info.value)


def test_the_pinned_blank_target_field_divergence_is_unchanged():
    """The wave-1 pin, re-asserted through ``generate_code`` WITH a populated
    source schema — the configuration this change introduces.

    ``test_the_coupling_has_one_boundary_the_two_functions_do_not_share`` pins
    that ``target_field="   "`` reaches ``validate_identifier`` and raises.
    ``source_field=None`` makes the entry unsourced, so the new check skips it and
    the ``IdentifierValidationError`` still wins.
    """
    with pytest.raises(IdentifierValidationError):
        _generate(_pipeline_metadata(_object_schema("id")), [{"target_field": "   ", "source_field": None}])


# ===========================================================================
# The validator in isolation
# ===========================================================================


def test_the_validator_is_a_no_op_on_an_empty_mapping_list():
    _ca._validate_mapped_source_fields(None, _object_schema("id"))
    _ca._validate_mapped_source_fields([], _object_schema("id"))


def test_the_validator_raises_directly():
    with pytest.raises(StepFailure):
        _ca._validate_mapped_source_fields([_entry("email", "nowhere")], _object_schema("id"))


def test_the_validator_preserves_first_seen_order_of_offenders():
    with pytest.raises(StepFailure) as exc_info:
        _ca._validate_mapped_source_fields(
            [_entry("c", "zeta"), _entry("a", "alpha"), _entry("b", "zeta")],
            _object_schema("id"),
        )

    assert exc_info.value.details["undeclared_source_fields"] == ["zeta", "alpha"]


# ===========================================================================
# The diagnostic is bounded
# ===========================================================================


def test_a_wide_source_schema_does_not_put_every_column_in_the_failure():
    """``_bounded_names``, asserted directly. CodeGraph reports no covering tests
    for it, and it is only reachable transitively, so it gets its own case.

    The declared-column list travels in the ``StepFailure`` message and details,
    which reach the SFN Cause, the errors table and the dashboard. A source schema
    may legitimately carry hundreds of columns (``_MAX_TEST_FRAME_COLUMNS`` is
    512 one Lambda over), so the diagnostic is capped for the reason
    ``_bound_stderr`` bounds stderr: a diagnostic must not be the thing that makes
    a write fail.
    """
    wide = _object_schema(*[f"col_{i}" for i in range(600)])

    with pytest.raises(StepFailure) as exc_info:
        _ca._validate_mapped_source_fields([_entry("email", "nowhere")], wide)

    details = exc_info.value.details
    declared = details["declared_source_fields"]
    assert len(declared) == _ca._MAX_NAMES_IN_FAILURE + 1
    assert declared[-1] == "... and 550 more"
    # The true width is still reported, so nothing is lost — only truncated.
    assert details["declared_source_field_count"] == 600
    # The offender is never truncated away: it is the actionable part.
    assert details["undeclared_source_fields"] == ["nowhere"]


def test_a_short_declared_list_is_passed_through_unchanged():
    assert _ca._bounded_names(["a", "b"]) == ["a", "b"]
    assert _ca._bounded_names([]) == []
