# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""``FrameSource.provenance()`` and the lifetime of ``last_frame_source``.

Spec `p44-verify-against-real-source-data` task 5.2. Requirements 5.1, 6.7;
design §4.6.

Task 5.1 landed two things and nothing tested either of them — CodeGraph flags
``provenance`` with no callers and no covering tests, corroborated by grep across
``tests/`` for both names. This file is that coverage, and it is deliberately
narrow: the dict's shape, the three frame outcomes, and **when** the attribute is
set relative to the two things that end a run early.

**No Glue interactive session is created here, and none may be.** do-NOT 1 —
they bill. That fence needs stating rather than assuming, because task 5.1's own
execution walked into it: calling ``run_guarded_session`` in-process with
``frame_mode="sample"`` and a readable ``s3://`` URI reaches the real
``CreateSession`` **even with ``source_schema=None``**, because ``is_sample``
retires the absent-schema refusal arm (R9.4) and there is then nothing left to
refuse. It was rejected on a placeholder role ARN, so nothing billed. Two
measures here, not one:

* every runner is built with ``glue_session_runner.boto3.client`` patched, so
  ``self.client`` is a ``MagicMock`` and no real client exists to call; and
* every case that calls ``run_guarded_session`` asserts
  ``create_session.assert_not_called()``.

Which is satisfiable without weakening anything, because the paths this file is
about — the refusal return and the ``FrameBuildError`` raise — both happen
**before** ``CreateSession`` by construction. The session-reaching sample path is
already covered by ``test_sample_frame_dispatch.py``; nothing is duplicated here.

What is pinned
--------------
* **The key set is exactly six** (R6.7). Asserted as an equality, not a subset,
  so a seventh key added later fails *here* — where the reason is written down —
  rather than surfacing as an unexplained extra field in a DynamoDB item.
* **``FrameSource`` still has exactly five fields, and none is a row.** R6.7 is
  claimed to hold "by construction … there is nothing to filter"
  (``glue_session_runner.py:452-456``). That claim is only true while the
  dataclass carries no row, so the field set is asserted too: a sampled value
  cannot reach the dict without a new field first, and a new field fails this.
* **``row_bound`` is 100 on a sample and ``None`` otherwise**, asserted against
  ``_SAMPLE_FRAME_ROWS`` *and* against the literal ``100`` R3.1 names — the
  constant so the key cannot drift from the bound
  ``build_sample_frame_code`` applies, the literal so redefining the constant
  cannot quietly change the reported bound.
* **``unavailable_reason`` is present only on a fallen-back run.** Including the
  case that separates the two synthetic outcomes: an unrecognised ``frame_mode``
  is ``frame="synthetic"`` with no reason, not ``"sample_unavailable"``.
* **The attribute is set before the refusal is evaluated.** A run refused for an
  absent schema still has a provenance, and so does a run that raised
  ``FrameBuildError``. Both are paths on which no ``FrameGuardedRun`` exists —
  the raise has nothing to hang one off at all — and both are what task 6.1
  reads ``self.last_frame_source`` for.
* **``CodeInterpreterTestRunner`` has no such attribute**, so the handler's
  ``getattr(runner, "last_frame_source", None)`` default is load-bearing rather
  than defensive. Read end to end at ``code_interpreter_runner.py:220-229``:
  ``region`` in, one ``bedrock-agentcore`` client, ``session_id`` — no
  ``frame_mode``, no ``source_data_uri``, no frame anything (grep over the file
  for all three plus ``s3://`` returns zero hits).

Expectations are taken from ``requirements.md`` R5.1 / R6.7 / R3.1 and design
§4.6, never read back from what ``provenance()`` currently returns —
``LGR-28b4d75-5`` (**template / ratified**): a test written from the producer's
output cannot detect the producer drifting, because the output is its own oracle.
"""

import dataclasses
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add Lambda source to path
sys.path.insert(
    0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
# `provenance()`'s dict crosses into `create_output_data`'s `summary` (R5.6) and
# thence to DynamoDB through this converter (task 14.1). That is the operative
# reason the return is a plain dict rather than the frozen dataclass, so the
# converter is imported and exercised rather than described.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# `_MAX_TEST_FRAME_COLUMNS` from its owner rather than through
# `glue_session_runner`'s re-export, matching `test_sample_frame_dispatch.py`: the
# width bound is a DynamoDB item-size fact about the state row, and it belongs to
# the module that derives it.
from code_interpreter_runner import _MAX_TEST_FRAME_COLUMNS, CodeInterpreterTestRunner
from glue_session_runner import (
    _SAMPLE_FRAME_ROWS,
    FRAME_MODE_SAMPLE,
    FRAME_MODE_SYNTHETIC,
    SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT,
    STAGE_TRANSFORM,
    FrameBuildError,
    FrameSource,
    GlueSessionTestRunner,
)

from decimal_utils import convert_floats_to_decimal

ROLE_ARN = "arn:aws:iam::123456789012:role/AgenticDataIngestion-VerificationSession-Role"
REGION = "us-west-2"

S3_URI = "s3://adi-source-files-123456789012-us-west-2/pipeline-a/orders.csv"

#: An ``s3://`` object URI whose format nothing recognises — the prefix shape
#: ``file_validation`` writes for a multi-file upload, which carries no
#: extension. The second fallback arm.
S3_PREFIX_URI = "s3://adi-source-files-123456789012-us-west-2/pipeline-a/"

#: ``schema_extract``'s fallback, verbatim in shape
#: (``schema_extract/lambda_function.py:747``). The first fallback arm, and the
#: ordinary case for a JDBC-sourced pipeline.
JDBC_URI = "jdbc://pipeline/pipeline-a"

#: The six keys design §4.6 specifies, transcribed from the design rather than
#: from ``provenance()``'s return (``LGR-28b4d75-5``).
PROVENANCE_KEYS = frozenset({"frame", "requested", "source_uri", "read_format", "row_bound", "unavailable_reason"})

#: ``FrameSource``'s five fields, transcribed from R6.7's own claim that there
#: are five and that none of them is a row.
FRAME_SOURCE_FIELDS = frozenset({"mode", "requested_mode", "source_uri", "read_format", "unavailable_reason"})

#: The third value of ``frame``, which is deliberately not a ``FRAME_MODE_*``
#: constant: it is not a mode ``resolve_frame_source`` can put in ``mode``, it is
#: a provenance distinction between two runs that both got a synthetic frame
#: (R5.4).
FRAME_SAMPLE_UNAVAILABLE = "sample_unavailable"

BUILDABLE_SCHEMA = {
    "properties": {
        "customer_id": {"type": "integer"},
        "name": {"type": "string"},
        "amount": {"type": "double"},
    }
}

#: ``geography`` is outside ``spark_type_for``'s three buckets, so the synthetic
#: builder refuses rather than mis-typing the column — the one thing that raises
#: ``FrameBuildError`` from the frame build today. A declared schema, so the
#: absent-schema refusal does not fire first and the raise is genuinely reached.
UNBUILDABLE_SCHEMA = {"properties": {"region": {"type": "geography"}}}


def _wide_schema(field_count: int) -> dict:
    return {"properties": {f"col_{index}": {"type": "string"} for index in range(field_count)}}


def _synthetic_source() -> FrameSource:
    """No sample was requested — the default, and not a fallback."""
    return FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=FRAME_MODE_SYNTHETIC)


def _sample_source() -> FrameSource:
    """A sample was requested and built."""
    return FrameSource(mode=FRAME_MODE_SAMPLE, requested_mode=FRAME_MODE_SAMPLE, source_uri=S3_URI, read_format="csv")


def _unavailable_source(source_uri: str, reason: str) -> FrameSource:
    """A sample was requested and could not be had."""
    return FrameSource(
        mode=FRAME_MODE_SYNTHETIC,
        requested_mode=FRAME_MODE_SAMPLE,
        source_uri=source_uri,
        unavailable_reason=reason,
    )


@pytest.fixture
def glue():
    """A Glue client double.

    Never wired for a happy path, unlike ``test_sample_frame_dispatch.py``'s
    fixture: no test in this file may reach ``CreateSession``, so the mock exists
    to be asserted *unused* rather than to answer.
    """
    return MagicMock()


@pytest.fixture
def make_runner(glue):
    """Build a runner over the client double, so no real Glue client is created."""

    def _make(**kwargs) -> GlueSessionTestRunner:
        with patch("glue_session_runner.boto3.client", return_value=glue):
            return GlueSessionTestRunner(REGION, role_arn=ROLE_ARN, **kwargs)

    return _make


def _assert_no_aws_call(glue: MagicMock) -> None:
    """do-NOT 1, as an assertion rather than as a comment.

    ``create_session`` first because it is the billable one; the rest because a
    path that reached any of them reached further than this file's cases are
    supposed to go.
    """
    glue.create_session.assert_not_called()
    glue.get_session.assert_not_called()
    glue.run_statement.assert_not_called()
    glue.get_statement.assert_not_called()
    glue.delete_session.assert_not_called()


# ---------------------------------------------------------------------------
# The dict's shape (R6.7)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "frame_source",
    [
        _synthetic_source(),
        _sample_source(),
        _unavailable_source(JDBC_URI, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE),
        _unavailable_source(S3_PREFIX_URI, SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT),
        FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode="Sample"),
    ],
    ids=["synthetic", "sample", "unavailable-not-s3", "unavailable-no-format", "unrecognised-mode"],
)
def test_the_key_set_is_exactly_the_six(frame_source):
    """An equality, so a seventh key fails here rather than downstream.

    The dict is what ``create_output_data``'s ``summary`` carries (R5.6) and what
    the report reads (R5.1), and neither of those validates it — ``summary`` is
    free-form and ``test_output_data_contract.py:145`` uses a ``>=`` shape, so an
    added key is inert for both. This is where an addition is noticed.
    """
    assert set(frame_source.provenance()) == PROVENANCE_KEYS


def test_the_frame_source_still_carries_five_fields_and_no_row():
    """R6.7's "by construction" claim, made falsifiable.

    ``provenance()``'s docstring says there is nothing to filter because
    ``FrameSource`` has five fields and none of them is a row. That is a claim
    about the dataclass, not about the method, so it is asserted against the
    dataclass: a sampled value can only reach the dict via a new field, and a new
    field fails here.
    """
    assert {field.name for field in dataclasses.fields(FrameSource)} == FRAME_SOURCE_FIELDS


@pytest.mark.parametrize(
    "frame_source",
    [
        _synthetic_source(),
        _sample_source(),
        _unavailable_source(JDBC_URI, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE),
    ],
    ids=["synthetic", "sample", "unavailable"],
)
def test_the_provenance_is_a_plain_dict_that_survives_the_dynamodb_converter(frame_source):
    """A plain dict, not the frozen dataclass, and this is why (design §4.6).

    ``convert_floats_to_decimal`` walks dicts and lists and returns anything else
    unchanged (``shared/decimal_utils.py:51-57``), so a frozen dataclass would
    pass through opaque on the way to the ``test_results`` item. The values here
    are strings, ``None`` and one ``int``, so the walk is an identity — which is
    the point: the payload needs no conversion and no special-casing.
    """
    provenance = frame_source.provenance()

    assert type(provenance) is dict
    assert convert_floats_to_decimal(provenance) == provenance
    # Reaches the report and an S3 object as well, both of which serialise.
    assert json.loads(json.dumps(provenance)) == provenance


# ---------------------------------------------------------------------------
# One case per frame outcome
# ---------------------------------------------------------------------------


def test_a_synthetic_run_reports_a_synthetic_frame_and_no_reason():
    """Nothing was requested, so nothing was unavailable.

    ``row_bound`` is ``None`` rather than 100: a synthetic frame has a height
    too, but this key answers *how much of the customer's own data was read*, and
    on this run the answer is none of it.
    """
    provenance = _synthetic_source().provenance()

    assert provenance == {
        "frame": FRAME_MODE_SYNTHETIC,
        "requested": FRAME_MODE_SYNTHETIC,
        "source_uri": None,
        "read_format": None,
        "row_bound": None,
        "unavailable_reason": None,
    }


def test_a_sample_run_reports_the_object_the_reader_and_the_bound():
    """R5.2's three facts: the frame, the object it came from, and the row bound."""
    provenance = _sample_source().provenance()

    assert provenance == {
        "frame": FRAME_MODE_SAMPLE,
        "requested": FRAME_MODE_SAMPLE,
        "source_uri": S3_URI,
        "read_format": "csv",
        "row_bound": _SAMPLE_FRAME_ROWS,
        "unavailable_reason": None,
    }


def test_the_sample_row_bound_is_a_hundred_and_comes_from_the_constant():
    """R3.1 names 100; the constant is what ``build_sample_frame_code`` applies.

    Both assertions, because they fail for different reasons. The literal catches
    the bound changing; the constant catches the *key* being written as a
    hard-coded number and then drifting from the ``.limit()`` Spark actually
    enforces.
    """
    assert _sample_source().provenance()["row_bound"] == 100
    assert _sample_source().provenance()["row_bound"] == _SAMPLE_FRAME_ROWS


@pytest.mark.parametrize(
    ("source_uri", "reason"),
    [
        (JDBC_URI, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE),
        (S3_PREFIX_URI, SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT),
    ],
    ids=["not-s3", "no-recognisable-format"],
)
def test_a_requested_sample_that_was_unavailable_is_its_own_frame_value(source_uri, reason):
    """R5.4: two runs both got a synthetic frame, and they are distinguishable.

    ``frame`` is ``"sample_unavailable"`` rather than ``"synthetic"``, and the
    recorded reason and the offending URI both survive — so the report can name
    *which* source had no sample and *why*, which is what makes the fallback
    legible instead of silent.
    """
    provenance = _unavailable_source(source_uri, reason).provenance()

    assert provenance["frame"] == FRAME_SAMPLE_UNAVAILABLE
    assert provenance["requested"] == FRAME_MODE_SAMPLE
    assert provenance["unavailable_reason"] == reason
    assert provenance["source_uri"] == source_uri
    # No sample was read, so no quantity of real rows was read either.
    assert provenance["row_bound"] is None
    assert provenance["read_format"] is None


@pytest.mark.parametrize("frame_mode", ["Sample", "SAMPLE", "sample ", "samples", "smaple", "", "glue"])
def test_an_unrecognised_mode_is_synthetic_and_carries_no_reason(frame_mode):
    """A typo is not a fallen-back sample, and the two must not read alike.

    Nothing was requested — ``requested_mode`` is outside the recognised pair —
    so nothing was unavailable, and ``frame`` is plainly ``"synthetic"``. The
    value is carried **verbatim**, unnormalised, which is what keeps a typo
    visible to a reader of the run rather than only to whoever was watching the
    log (``LGR-ad78946-1``, **template / ratified**, is the same posture one level
    up).
    """
    provenance = FrameSource(mode=FRAME_MODE_SYNTHETIC, requested_mode=frame_mode).provenance()

    assert provenance["frame"] == FRAME_MODE_SYNTHETIC
    assert provenance["requested"] == frame_mode
    assert provenance["unavailable_reason"] is None
    assert provenance["row_bound"] is None


def test_the_three_frame_values_are_mutually_exclusive():
    """The three outcomes partition the space, rather than doing so by convention."""
    frames = [
        _synthetic_source().provenance()["frame"],
        _sample_source().provenance()["frame"],
        _unavailable_source(JDBC_URI, SAMPLE_UNAVAILABLE_NOT_S3_SOURCE).provenance()["frame"],
    ]

    assert frames == [FRAME_MODE_SYNTHETIC, FRAME_MODE_SAMPLE, FRAME_SAMPLE_UNAVAILABLE]
    assert len(set(frames)) == 3


# ---------------------------------------------------------------------------
# The attribute's lifetime — set before the refusal is evaluated
# ---------------------------------------------------------------------------


def test_a_runner_has_no_provenance_before_it_runs(make_runner, glue):
    """``None`` before a run, so "not yet run" is not confused with a frame."""
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=S3_URI)

    assert runner.last_frame_source is None
    _assert_no_aws_call(glue)


def test_resolving_alone_does_not_record_a_provenance(make_runner, glue):
    """The attribute is ``run_guarded_session``'s side effect, not the resolver's.

    Worth pinning because it is the difference between "what a run was given" and
    "what this configuration would resolve to" — a reader who found the attribute
    populated by a bare ``resolve_frame_source()`` would take the first meaning
    from something that only means the second.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=S3_URI)

    resolved = runner.resolve_frame_source()

    assert resolved.is_sample is True
    assert runner.last_frame_source is None
    _assert_no_aws_call(glue)


def test_a_run_refused_for_an_absent_schema_still_has_a_provenance(make_runner, glue):
    """The set precedes the refusal, which is the whole of task 5.1's ordering.

    Synthetic mode, so the absent-schema arm is armed (R9.5) and the run returns
    before ``CreateSession``. Task 6.1 reads this attribute on exactly this kind
    of path.
    """
    runner = make_runner()

    run = runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])

    assert run.refused
    assert runner.last_frame_source is not None
    assert runner.last_frame_source.provenance()["frame"] == FRAME_MODE_SYNTHETIC
    _assert_no_aws_call(glue)


def test_a_refused_run_whose_sample_was_unavailable_says_so(make_runner, glue):
    """The case the ordering exists to make readable.

    A JDBC-sourced pipeline requested a sample, could not have one, fell back to a
    generated frame, and was then refused for having no declared schema. Three
    facts, and the refusal dict carries only the third — so without the
    provenance a reader cannot tell this from a run that never asked for a
    sample.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=JDBC_URI)

    run = runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])

    assert run.refused
    assert runner.last_frame_source is not None
    assert runner.last_frame_source.provenance() == {
        "frame": FRAME_SAMPLE_UNAVAILABLE,
        "requested": FRAME_MODE_SAMPLE,
        "source_uri": JDBC_URI,
        "read_format": None,
        "row_bound": None,
        "unavailable_reason": SAMPLE_UNAVAILABLE_NOT_S3_SOURCE,
    }
    _assert_no_aws_call(glue)


def test_a_run_refused_for_width_in_sample_mode_still_has_a_sample_provenance(make_runner, glue):
    """The width arm survives the retirement, and the provenance survives the arm.

    A sample-mode run refused before any session exists — the width refusal is
    the one arm ``sample_frame=True`` does not retire — so this is a
    ``frame="sample"`` provenance on a run that read nothing. Which is correct:
    the provenance records the decision, and the decision was reached before the
    refusal.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=S3_URI)

    run = runner.run_guarded_session(_wide_schema(_MAX_TEST_FRAME_COLUMNS + 1))

    assert run.refused
    assert runner.last_frame_source is not None
    assert runner.last_frame_source.provenance()["frame"] == FRAME_MODE_SAMPLE
    assert runner.last_frame_source.provenance()["row_bound"] == _SAMPLE_FRAME_ROWS
    _assert_no_aws_call(glue)


def test_a_run_that_raised_a_frame_build_error_still_has_a_provenance(make_runner, glue):
    """The path with no ``FrameGuardedRun`` at all, and the reason for the attribute.

    ``frame_source`` on the run object cannot help here — the raise leaves no run
    object to read it off. The declared type is outside ``spark_type_for``'s
    vocabulary, so the synthetic builder refuses rather than mis-typing the
    column, and it refuses **host-side, before any session exists**.
    """
    runner = make_runner()

    with pytest.raises(FrameBuildError, match="outside the known vocabulary"):
        runner.run_guarded_session(UNBUILDABLE_SCHEMA, [(STAGE_TRANSFORM, "df_t = df")])

    assert runner.last_frame_source is not None
    assert runner.last_frame_source.provenance()["frame"] == FRAME_MODE_SYNTHETIC
    _assert_no_aws_call(glue)


def test_a_frame_build_error_after_an_unavailable_sample_keeps_the_reason(make_runner, glue):
    """Both facts survive one raise: the sample was refused, and the schema was unbuildable.

    The combination task 6.1's ``except`` arm has to distinguish — it tests
    ``is_sample`` on this attribute, and here the answer is False even though a
    sample was requested.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=JDBC_URI)

    with pytest.raises(FrameBuildError):
        runner.run_guarded_session(UNBUILDABLE_SCHEMA, [(STAGE_TRANSFORM, "df_t = df")])

    assert runner.last_frame_source is not None
    assert runner.last_frame_source.is_sample is False
    assert runner.last_frame_source.provenance()["frame"] == FRAME_SAMPLE_UNAVAILABLE
    assert runner.last_frame_source.provenance()["unavailable_reason"] == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    _assert_no_aws_call(glue)


def test_the_provenance_reflects_the_latest_run_not_the_first(make_runner, glue):
    """Named ``last_frame_source``, and it means it.

    One runner, two guarded runs with different URIs — the attribute is a per-run
    observation, so the second overwrites the first. ``frame_mode`` says
    ``"sample"`` throughout, which is exactly why reading the configuration
    instead would answer the wrong question on both runs.
    """
    runner = make_runner(frame_mode=FRAME_MODE_SAMPLE, source_data_uri=JDBC_URI)
    runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])
    first = runner.last_frame_source

    runner.source_data_uri = S3_PREFIX_URI
    runner.run_guarded_session({}, [(STAGE_TRANSFORM, "df_t = df")])
    second = runner.last_frame_source

    assert first is not None and second is not None
    assert first.provenance()["unavailable_reason"] == SAMPLE_UNAVAILABLE_NOT_S3_SOURCE
    assert second.provenance()["unavailable_reason"] == SAMPLE_UNAVAILABLE_UNRECOGNISED_FORMAT
    assert runner.frame_mode == FRAME_MODE_SAMPLE
    _assert_no_aws_call(glue)


# ---------------------------------------------------------------------------
# The pandas runner has no such attribute (the handler's `getattr` default)
# ---------------------------------------------------------------------------


@pytest.fixture
def code_interpreter_runner():
    """A ``CodeInterpreterTestRunner`` over a client double."""
    with patch("code_interpreter_runner.boto3.client", return_value=MagicMock()):
        return CodeInterpreterTestRunner(REGION)


def test_the_pandas_runner_has_no_frame_provenance(code_interpreter_runner):
    """So ``getattr(runner, "last_frame_source", None)`` is load-bearing (§4.6).

    The handler holds one local ``runner`` across three executor arms, and on the
    ``pandas`` and ``shadow`` arms it is this class. A plain attribute read there
    would be an ``AttributeError`` on every pandas run.
    """
    assert not hasattr(code_interpreter_runner, "last_frame_source")
    assert getattr(code_interpreter_runner, "last_frame_source", None) is None


def test_the_pandas_runner_has_no_frame_configuration_either(code_interpreter_runner):
    """It has no source read of any kind, which is why R1.6 pins shadow to synthetic.

    ``code_interpreter_runner.py:220-229`` end to end: ``region`` in, one
    ``bedrock-agentcore`` client, ``session_id``. Its frame is generated inside
    the interpreter from the *declared* schema, so giving the shadow comparison a
    real sample on one side is unimplementable rather than merely unimplemented.
    """
    for absent in ("frame_mode", "source_data_uri", "frame_source", "resolve_frame_source", "provenance"):
        assert not hasattr(code_interpreter_runner, absent), (
            f"CodeInterpreterTestRunner grew {absent!r} — R1.6's pin and §4.6's getattr both "
            f"rest on it having no frame anything"
        )


def test_the_pandas_runner_takes_only_a_region():
    """The constructor R1.6's reasoning cites, asserted rather than described."""
    import inspect

    parameters = list(inspect.signature(CodeInterpreterTestRunner.__init__).parameters)

    assert parameters == ["self", "region"]
