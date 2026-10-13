# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Characterization tests: ``test_code.get_pipeline_metadata`` (p33 task 3.2).

``lambda/agentic_data_ingestion_test_code/lambda_function.py:126-142`` is the
pipeline-record read that p33 task 4.3 makes its **first caller**. It is dead
code today: a module-wide grep finds only the definition at ``:126`` and the
classifier call at ``:137`` — no call site exists. That is the licence design
D8 relies on to change its public not-found return from ``{}`` to ``None``.

**Not uncovered, despite what the design's blast-radius table says.** codegraph
reports "no covering tests" for this symbol, but
``tests/unit/lambdas/test_code/test_read_path_errors.py`` already exercises it
(throttle → ``TransientStepFailure`` at ``:77``, access denied →
``StepFailure`` at ``:88``, missing item → ``{}`` at ``:96``). This is the same
false negative recorded in task 3.3 for ``aggregate_check_results`` and
``is_not_verified_check``: the test imports the module dynamically and calls
``_mod.get_pipeline_metadata(...)``, so no static call edge exists. Those three
assertions are **deliberately not repeated here**; this file covers what they
do not.

Note for task 4.3 / C11 — **DONE, and this note is kept as the record of it.**
``test_read_path_errors.py`` asserted the ``{}`` not-found return that D8
replaces with ``None``. Task 3.3 added it to task 4.9's amend list (item 6);
task 4.3 carried out the amendment. The assertion now reads ``is None`` inside
the renamed ``test_missing_item_returns_none``, with the reason recorded at the
assertion and the module docstring amended to match — not deleted. The three
line numbers quoted above (``:77``, ``:88``, ``:96``) are the pre-amendment
ones; the assertions moved down when that docstring grew, so re-locate rather
than trusting them.

**Expectations here are derived from the requirements, not from current
output.** A test written from the producer's own output cannot detect the
producer drifting, because the output is its own oracle (``LGR-28b4d75-5``,
template/ratified). Where a requirement-derived expectation disagrees with
current behaviour the test is marked ``xfail(strict=True)`` rather than
rewritten to match the code; those markers are the defects task 4.3 must close,
and ``strict=True`` means the suite fails the moment one starts passing, so 4.3
cannot land without flipping them.

Requirement clauses encoded:

- **2.8** — a target-schema read that fails for an infrastructure reason
  (throttle, access denied) propagates the error rather than degrading to the
  no-schema path and reporting the run as unverifiable. The pipeline-record
  read is the first hop of that read, so it is in scope. ``C2`` names
  ``raise_for_read_client_error`` (``shared/step_errors.py:160-190``) as the
  classifier and fixes its three-way split: not-found returns normally,
  transient raises ``TransientStepFailure``, everything else raises
  ``StepFailure``. Authority ``LGR-7c9378d-1`` (template/ratified) plus a
  verified read of the classifier.
- **2.6** — a pipeline with no target schema **at all** reports schema
  compliance as not verified. That is a legitimate state and must be reported,
  not failed.

**The thing this file exists to pin is that 2.6 and 2.8 do not collapse into
one path.** A genuinely missing pipeline record is a 2.8 lookup problem and
must fail the step. A record that exists and carries no target schema is the
2.6 path and must report not-verified. Today both come back as a ``dict`` from
which no ``target_schema_s3_key`` can be read, so the only discriminator
available to a caller is emptiness — and D8 rejects that, because ``{}`` would
make a missing record silently present as "no target schema" and report
``verified: False``: a run reported as unverifiable when the real problem is a
missing record. That is requirement 2.8's exact prohibition.

Ledger provenance is thin here and that is a gap, not an all-clear:
``why(:126)`` returns commit ``362fb514dd`` — cited by hash only, because its
subject line carries the pre-rename project name that
``make name-check``'s blocking scan rejects — with **no
records at all** — the same hole task 3.1 found for the analogous
``schema_mapping`` read and task 1 found for ``:980``. ``why(:137)`` adds only
``LGR-0176ccc-1``, a backfilled 634-file working-tree snapshot that matches
almost everything and says nothing about this function. **Nothing in the ledger
documents the ``{}`` return.**

**Validates: Requirements 2.6, 2.8**
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

# Set region/account before any boto3 imports
os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")
os.environ.setdefault("AWS_ACCOUNT_ID", "777788889999")

# Add Lambda dir and shared dir to path (sibling pattern: test_read_path_errors.py)
_test_code_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_test_code")
)
sys.path.insert(0, _test_code_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

# Stub heavy Lambda-layer dependencies before import (sibling pattern)
_mods = {
    "strands": MagicMock(),
    "strands_mcp_factory": MagicMock(),
    "code_interpreter_runner": MagicMock(CodeInterpreterTestRunner=MagicMock()),
    "eventbridge_utils": MagicMock(
        get_eventbridge_publishers=MagicMock(return_value=(MagicMock(), MagicMock(), MagicMock()))
    ),
    "env_utils": MagicMock(
        get_aws_account_id=MagicMock(return_value="777788889999"),
        require_env=MagicMock(side_effect=lambda k, d=None: d or k),
    ),
    "prompt_template_loader": MagicMock(),
    "artifact_schema": MagicMock(create_output_data=MagicMock(return_value={})),
    "metrics_facade": MagicMock(PipelineMetrics=None),
}

with patch.dict("sys.modules", _mods):
    sys.modules.pop("lambda_function", None)
    import lambda_function as _mod

# ``test_code`` imports ``StepFailure`` but not its transient subclass, so the
# classified type requirement 2.8 turns on has to be reached another way.
#
# It is reached through the classifier's own module namespace rather than by
# ``from step_errors import TransientStepFailure``: ``patch.dict("sys.modules",
# ...)`` above restores the dict on exit, dropping any module first imported
# inside the block, so a re-import can yield a SECOND ``step_errors`` instance
# whose ``StepFailure`` is a different class object from ``_mod.StepFailure``.
# ``pytest.raises(_mod.StepFailure)`` then silently stops matching. Binding off
# ``raise_for_read_client_error.__globals__`` gives the exact class the
# production code path constructs.
TransientStepFailure = _mod.raise_for_read_client_error.__globals__["TransientStepFailure"]
assert issubclass(TransientStepFailure, _mod.StepFailure)


@pytest.fixture(autouse=True)
def _ensure_correct_module():
    sys.modules["lambda_function"] = _mod
    yield


PIPELINE_ID = "pipe-p33-metadata"
TARGET_BUCKET = "adi-target-schemas-777788889999-us-west-2"
TARGET_KEY = f"{PIPELINE_ID}/uploaded_schema_1785844048926.json"

#: A pipeline record carrying a target schema — the attribute pair
#: ``create_pipeline`` writes together or not at all, plus the surrounding
#: attributes a real record has. This is the input to the 2.1 path.
RECORD_WITH_TARGET_SCHEMA = {
    "pipeline_id": PIPELINE_ID,
    "pipeline_name": "testing",
    "status": "active",
    "target_schema_name": "uploaded_schema_1785844048926",
    "target_schema_bucket": TARGET_BUCKET,
    "target_schema_s3_key": TARGET_KEY,
    "target_type": "New S3 Bucket",
    "source_type": "S3_UPLOAD",
}

#: A pipeline record that exists and carries **no** target schema. This is
#: requirement 2.6's input: a legitimate state that must report not-verified.
#: DynamoDB always returns at least the key attribute, so a record that exists
#: is never literally empty — which is what makes ``{}`` available as a
#: not-found sentinel today, and what makes the collapse below possible.
RECORD_WITHOUT_TARGET_SCHEMA = {
    "pipeline_id": PIPELINE_ID,
    "pipeline_name": "testing",
    "status": "active",
    "source_type": "S3_UPLOAD",
}


def _client_error(code: str) -> ClientError:
    return ClientError({"Error": {"Code": code, "Message": f"simulated {code}"}}, "GetItem")


def _dynamodb_returning(response: dict) -> MagicMock:
    """A ``dynamodb`` resource stand-in whose table returns ``response``."""
    table = MagicMock()
    table.get_item.return_value = response
    dynamodb = MagicMock()
    dynamodb.Table.return_value = table
    return dynamodb


def _dynamodb_raising(exc: BaseException) -> MagicMock:
    table = MagicMock()
    table.get_item.side_effect = exc
    dynamodb = MagicMock()
    dynamodb.Table.return_value = table
    return dynamodb


class TestMissingRecordIsDistinguishableFromAKeylessRecord:
    """Requirements 2.6 and 2.8 must not collapse into one path (design D8).

    Requirement 2.8 makes a genuinely missing pipeline record a lookup problem
    that must fail the step. Requirement 2.6 makes a record that exists and
    carries no target schema a legitimate state that must report *not
    verified*. The two need different outcomes, so the read has to hand its
    caller enough to tell them apart.

    Every test in this class that is marked xfail is a **defect** on current
    code: ``:140-142`` returns ``{}`` for the missing record and the record
    itself for a keyless one, so both are ``dict`` instances carrying no
    ``target_schema_s3_key``, and the only discriminator left is emptiness.
    """

    # xfail(strict=True) REMOVED by p33 task 4.3: D8 landed, the not-found
    # return is `None`, and this requirement-derived expectation now holds. The
    # marker was the defect record; `strict=True` is what forced 4.3 to come
    # back and remove it rather than leaving a passing test marked as broken.
    def test_a_genuinely_missing_record_returns_the_not_found_sentinel(self):
        with patch.object(_mod, "dynamodb", _dynamodb_returning({})):
            assert _mod.get_pipeline_metadata(PIPELINE_ID) is None

    def test_a_record_that_exists_and_carries_no_target_schema_is_not_the_sentinel(self):
        """The 2.6 path must never look like a lookup failure.

        This half holds today and must keep holding: an existing record always
        carries at least its key attribute, so it is never the not-found value
        under either the current ``{}`` convention or D8's ``None``.
        """
        with patch.object(_mod, "dynamodb", _dynamodb_returning({"Item": RECORD_WITHOUT_TARGET_SCHEMA})):
            result = _mod.get_pipeline_metadata(PIPELINE_ID)

        assert result is not None
        assert result != {}
        assert result["pipeline_id"] == PIPELINE_ID

    # xfail(strict=True) REMOVED by p33 task 4.3: the collapse requirement 2.8
    # prohibits is closed. `None` for a missing record, the record itself for a
    # keyless one, and `test_code`'s handler raises StepFailure on the former.
    def test_the_two_states_are_separated_by_the_not_found_sentinel(self):
        with patch.object(_mod, "dynamodb", _dynamodb_returning({})):
            missing = _mod.get_pipeline_metadata(PIPELINE_ID)
        with patch.object(_mod, "dynamodb", _dynamodb_returning({"Item": RECORD_WITHOUT_TARGET_SCHEMA})):
            keyless = _mod.get_pipeline_metadata(PIPELINE_ID)

        # The discriminator D8 assigns: `is None` means "no record" (2.8),
        # anything else means "record exists" and the target-schema question is
        # then answered from its attributes (2.6 or 2.1).
        assert missing is None
        assert keyless is not None

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "STAYS RED BY CONSTRUCTION, and p33 task 4.3 deliberately left it "
            "so — reason text amended there because the original said 'task 4.3 "
            "must fix', which is wrong. This case IS D8's rejected alternative "
            "('keep the sentinel and distinguish on key presence alone'), and it "
            "is unsatisfiable under every sentinel: neither {} nor None carries "
            "a target_schema_s3_key, so a key-presence discriminator collapses "
            "the two states whatever the sentinel is. The xfail is the record "
            "that the alternative was tried and cannot work; strict=True will "
            "fail the suite if someone ever makes it pass, which would mean the "
            "sentinel started carrying a key and D8's premise had moved."
        ),
    )
    def test_key_presence_alone_does_not_separate_the_two_states(self):
        with patch.object(_mod, "dynamodb", _dynamodb_returning({})):
            missing = _mod.get_pipeline_metadata(PIPELINE_ID)
        with patch.object(_mod, "dynamodb", _dynamodb_returning({"Item": RECORD_WITHOUT_TARGET_SCHEMA})):
            keyless = _mod.get_pipeline_metadata(PIPELINE_ID)

        # Both are falsy on the key, so this discriminator collapses them. The
        # assertion states what a caller needs and cannot get today.
        assert (missing or {}).get("target_schema_s3_key") != (keyless or {}).get("target_schema_s3_key")


class TestRecordIsReturnedIntactWhenPresent:
    """Requirement 2.1's precondition: the canonical attributes survive the read.

    ``resolve_target_schema`` (task 4.1) resolves ``target_schema_bucket`` and
    ``target_schema_s3_key`` from this record, so anything the read drops or
    rewrites is a schema the run cannot be measured against. Holds today
    (``:141`` returns ``response["Item"]`` unchanged) and must keep holding.
    """

    def test_the_record_is_returned_unchanged(self):
        with patch.object(_mod, "dynamodb", _dynamodb_returning({"Item": RECORD_WITH_TARGET_SCHEMA})):
            result = _mod.get_pipeline_metadata(PIPELINE_ID)

        assert result == RECORD_WITH_TARGET_SCHEMA

    def test_the_target_schema_attribute_pair_survives_the_read(self):
        """C1 (``LGR-7c9378d-1``, template/ratified): the pair is resolved together."""
        with patch.object(_mod, "dynamodb", _dynamodb_returning({"Item": RECORD_WITH_TARGET_SCHEMA})):
            result = _mod.get_pipeline_metadata(PIPELINE_ID)

        assert result["target_schema_bucket"] == TARGET_BUCKET
        assert result["target_schema_s3_key"] == TARGET_KEY

    def test_the_record_is_read_by_its_pipeline_id_key(self):
        dynamodb = _dynamodb_returning({"Item": RECORD_WITH_TARGET_SCHEMA})
        with patch.object(_mod, "dynamodb", dynamodb):
            _mod.get_pipeline_metadata(PIPELINE_ID)

        dynamodb.Table.return_value.get_item.assert_called_once_with(Key={"pipeline_id": PIPELINE_ID})


class TestInfrastructureErrorsPropagateClassified:
    """Requirement 2.8 / C2 (``shared/step_errors.py:160-190``).

    ``raise_for_read_client_error`` is the classifier the requirement turns on.
    ``test_read_path_errors.py`` already pins one transient code and one
    permanent code; this class covers the parts it does not — the breadth of
    the transient set, the not-found boundary, a non-``ClientError``, and
    whether an already-classified failure survives the way out.
    """

    @pytest.mark.parametrize(
        "code",
        ["ProvisionedThroughputExceededException", "InternalServerError", "RequestTimeout"],
    )
    def test_transient_infra_codes_raise_transient_step_failure(self, code):
        """SFN Retry policies match on the class name, so the class is the contract."""
        with (
            patch.object(_mod, "dynamodb", _dynamodb_raising(_client_error(code))),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_pipeline_metadata(PIPELINE_ID)

        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"
        assert exc_info.value.step_name == "test_code"
        assert exc_info.value.details["operation"] == "get_pipeline_metadata"

    @pytest.mark.parametrize("code", ["ValidationException", "AccessDenied"])
    def test_other_client_errors_raise_permanent_step_failure(self, code):
        with (
            patch.object(_mod, "dynamodb", _dynamodb_raising(_client_error(code))),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_pipeline_metadata(PIPELINE_ID)

        assert exc_info.value.error_type == "permanent"
        assert exc_info.value.details["error_code"] == code

    def test_a_non_client_error_is_not_degraded_into_the_no_schema_path(self):
        """2.8's core demand, in its most general form.

        The ``except`` clause at ``:136`` is narrowed to ``ClientError``, so
        anything else propagates untouched. Holds today and must keep holding:
        this is precisely the property ``schema_mapping.get_target_schema``
        violates with its blanket ``except Exception`` at ``:303-305``.
        """
        with (
            patch.object(_mod, "dynamodb", _dynamodb_raising(RuntimeError("connection reset"))),
            pytest.raises(RuntimeError, match="connection reset"),
        ):
            _mod.get_pipeline_metadata(PIPELINE_ID)

    def test_an_already_classified_step_failure_is_not_reclassified_on_the_way_out(self):
        """The trap task 3.1 found in the analogous ``schema_mapping`` read.

        There, one broad ``try`` wraps the whole body and re-emits an
        already-classified ``TransientStepFailure`` as a bare ``ValueError``,
        destroying the classification. Here the ``try`` covers only the
        ``get_item`` call and catches only ``ClientError``, so the classified
        failure raised from inside the handler propagates intact. Pinned so a
        later widening of that ``except`` cannot silently reintroduce the trap.
        """
        with (
            patch.object(_mod, "dynamodb", _dynamodb_raising(_client_error("ThrottlingException"))),
            patch.object(
                _mod,
                "raise_for_read_client_error",
                side_effect=TransientStepFailure("test_code", "get_pipeline_metadata failed: ThrottlingException"),
            ),
            pytest.raises(_mod.StepFailure) as exc_info,
        ):
            _mod.get_pipeline_metadata(PIPELINE_ID)

        assert type(exc_info.value).__name__ == "TransientStepFailure"
        assert exc_info.value.error_type == "transient"

    # xfail(strict=True) REMOVED by p33 task 4.3: the not-found VALUE is now
    # `None`. The CLASSIFICATION was always correct and ratified (C2 /
    # LGR-7c9378d-1 put ResourceNotFoundException in
    # NOT_FOUND_CLIENT_ERROR_CODES, so the classifier returns normally here).
    # D8 correction 2: this path (missing TABLE) and the missing-record path now
    # both return `None` and both fail the step, so the StepFailure message
    # cannot tell them apart — the helper's `logger.error` is the only place the
    # table case stays diagnosable, which is why 4.3 kept it (P21 Req 6.4).
    def test_a_not_found_client_error_returns_the_not_found_sentinel(self):
        with patch.object(_mod, "dynamodb", _dynamodb_raising(_client_error("ResourceNotFoundException"))):
            assert _mod.get_pipeline_metadata(PIPELINE_ID) is None
