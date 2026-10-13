# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests: schema_extract failure path reliably writes its state record.

P28 R8.5 (task 14.2): the old error item lacked the state table's ``run_id``
partition key, so the write was a guaranteed ValidationException silently
swallowed by the surrounding except — the failure record NEVER landed. The
fixed path must:

- write the failed state record keyed ``(run_id, "schema_extract")`` with
  the canonical step name, "failed" status, and the shared step_sequence,
- write it BEFORE the StepFailure propagates, and
- never mask the original failure with a state-write error.

Validates: P28 Requirements 8.2, 8.5

p46 task 6.3 extends this file with the two new binary container formats. The
raise sites are new; the propagation is not, and nothing here changes it — see
the section comment above ``TestBinaryContainerFailuresAttributeToThisStep``.

Validates: p46 Requirement 4.3
"""

import importlib.util
import io
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pyarrow as pa
import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

_schema_extract_dir = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_schema_extract")
)
sys.path.insert(0, _schema_extract_dir)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"))

sys.modules.setdefault(
    "eventbridge_utils",
    MagicMock(get_eventbridge_publishers=lambda: (MagicMock(), MagicMock(), MagicMock())),
)
_prev_env_utils = sys.modules.get("env_utils")
# Installed unconditionally and restored below, so the import binds this stub
# whatever an earlier module left in sys.modules (LGR-c58f276-4).
sys.modules["env_utils"] = MagicMock(require_env=lambda name, default=None: default or name)
sys.modules.setdefault("metrics_facade", MagicMock(PipelineMetrics=None))

_spec = importlib.util.spec_from_file_location(
    "schema_extract_failure_record_lf",
    os.path.join(_schema_extract_dir, "lambda_function.py"),
)
_mod = importlib.util.module_from_spec(_spec)
try:
    _spec.loader.exec_module(_mod)

    from step_errors import StepFailure
    from step_names import STEP_SEQUENCE, StepName
finally:
    # Put env_utils back as it was before this module's stub. The imports
    # above have bound what they need; a stub left in sys.modules leaks into
    # every module collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    if _prev_env_utils is None:
        sys.modules.pop("env_utils", None)
    else:
        sys.modules["env_utils"] = _prev_env_utils

RUN_ID = "run-schema-extract-fail"


@pytest.fixture(autouse=True)
def _mock_tables():
    _mod.table = MagicMock()
    _mod.PipelineMetrics = None
    yield


def test_failure_writes_state_record_with_full_key_before_raise():
    """The failure record carries the table's FULL key (run_id + canonical
    step_name) — the pre-fix item had no run_id and could never be written."""
    # previous file_validation state present, but no usable source URI in it
    # → the handler raises inside the try body.
    _mod.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "original_pipeline_id": "pipe-1"}}

    with pytest.raises(StepFailure) as exc_info:
        _mod.lambda_handler({"run_id": RUN_ID, "original_pipeline_id": "pipe-1"}, None)

    writes = [c.kwargs["Item"] for c in _mod.table.put_item.call_args_list]
    assert len(writes) == 1
    item = writes[0]
    assert item["run_id"] == RUN_ID
    assert item["step_name"] == StepName.SCHEMA_EXTRACT
    assert item["step_name"] == "schema_extract"
    assert item["status"] == "failed"
    assert item["step_sequence"] == STEP_SEQUENCE[StepName.SCHEMA_EXTRACT]
    assert item["pipeline_id"] == "pipe-1"
    assert item["error_message"]

    assert exc_info.value.step_name == "schema_extract"


def test_state_write_error_never_masks_original_failure():
    """A failing state write is logged, and the original StepFailure still
    propagates with schema_extract attribution."""
    _mod.table.get_item.return_value = {"Item": {"run_id": RUN_ID, "original_pipeline_id": "pipe-1"}}
    _mod.table.put_item.side_effect = Exception("ProvisionedThroughputExceeded")

    with pytest.raises(StepFailure) as exc_info:
        _mod.lambda_handler({"run_id": RUN_ID, "original_pipeline_id": "pipe-1"}, None)

    assert exc_info.value.step_name == "schema_extract"


# ── p46 task 6.3 — Avro/ORC failures are attributed to THIS step (R4.3) ──
#
# Everything above this line is P28 and is unamended. What follows adds the two
# formats task 6.1 wired into the Extraction_Dispatch
# (``lambda_function.py:1814-1817``) to the failure-record cases already here,
# because they are the same failure path reached through a new door.
#
# **The propagation under test already worked before this task and is NOT
# reimplemented by it.** Nothing in ``shared/step_errors.py`` or
# ``orchestration-stack.ts`` is touched; this asserts the existing contract
# holds for the new raise sites:
#
#   1. the extractor raises on unreadable input (asserted by tasks 4.2 and 5.2,
#      over the fixtures — not re-asserted here),
#   2. the handler's own ``except Exception`` writes the failed state row keyed
#      ``(run_id, "schema_extract")`` and then raises
#      ``StepFailure("schema_extract", …)`` (``lambda_function.py:1948-1985``),
#      whose default ``error_type`` is ``"permanent"``
#      (``shared/step_errors.py:106``),
#   3. ``@step_handler("schema_extract")`` re-raises an already-classified
#      ``StepFailure`` as-is, preserving the class name Step Functions matches
#      ``ErrorEquals`` against (``shared/step_errors.py:282-284``),
#   4. the state's ``States.ALL`` Catch routes it to ``RecordFailure`` with the
#      originating step's error preserved (``orchestration-stack.ts:226-234``),
#      which recovers the structured fields by ``json.loads`` of the serialized
#      exception carried in the SFN ``Cause``
#      (``shared/step_errors.py`` module docstring).
#
# ``str(exc)`` is asserted directly because step 4 parses exactly that, so the
# serialized ``step_name`` is the field that decides which step the run is
# recorded as having failed at.
#
# **Why the step matters enough to have a test.** ``LGR-60c4985-7`` (template /
# ratified) rejected returning a success payload for an unreadable upload on
# precisely this ground: "the pipeline would continue over a file nothing
# downstream can parse and the failure would be recorded against SchemaExtract —
# the wrong step … Preserving the originating step's error is exactly what the
# ``States.ALL`` Catch exists to guarantee." p46's defect is the mirror image of
# that: an Avro upload was *handled*, so the run advanced to ``schema_mapping``
# with an Empty_Schema and reported success. Failing here, named, is the fix.
#
# **``error_type == "permanent"`` is part of what R4.3 means, not incidental.**
# ``stepRetry`` matches only ``TransientStepFailure`` and Lambda service errors
# (``orchestration-stack.ts:219-224``), and an unreadable object does not become
# readable on attempt two. A transient classification here would buy three
# retries of a guaranteed failure and delay the operator's answer.
#
# The write mechanics — full key, ``step_sequence``, write-before-raise, and a
# failing write never masking the original error — are already covered by the
# two P28 tests above and are not duplicated. The shared assertion helper
# re-checks the key and status only because "the failure row names
# ``schema_extract``" is the claim under test, and an unwritten row names
# nothing (P28 R8.5's bug was exactly a row that never landed).

#: Bytes that are not an Avro OCF and not an ORC file. Short and obviously
#: neither, so both readers fail on their own terms: the hand-written OCF parser
#: on the 4-byte magic, pyarrow on the ORC postscript/file size.
NOT_A_CONTAINER = b"not-a-binary-container-just-some-bytes"


def _file_validation_state(filename: str) -> dict:
    """The previous-step state row the handler reads, naming a source object.

    Distinct from the two P28 tests' fixture, which deliberately carries **no**
    source URI so the handler raises before reaching any extractor. Here the
    extractor must be reached — the raise under test is its own.
    """
    return {
        "Item": {
            "run_id": RUN_ID,
            "original_pipeline_id": "pipe-1",
            "source_s3_uri": f"s3://bucket/raw/{filename}",
        }
    }


def _assert_failure_is_recorded_against_schema_extract(exc_info, *, cause_type: type[Exception]) -> dict:
    """Assert the raise became a permanent ``schema_extract`` failure, recorded.

    Returns the state row written, so a caller can make format-specific
    assertions on it.
    """
    exc = exc_info.value

    # The exception Step Functions sees, and the fields RecordFailure recovers
    # from the Cause by json.loads.
    assert exc.step_name == "schema_extract"
    assert exc.error_type == "permanent"
    serialized = json.loads(str(exc))
    assert serialized["step_name"] == "schema_extract"
    assert serialized["error_type"] == "permanent"

    # The reader's own diagnosis survives into the message rather than being
    # flattened into a generic wrapper — that is what makes the failure
    # actionable rather than merely attributed.
    assert isinstance(exc.__cause__, cause_type), f"expected {cause_type.__name__}, got {type(exc.__cause__).__name__}"
    assert str(exc.__cause__) in exc.message

    # The failure row landed, and it names this step.
    writes = [c.kwargs["Item"] for c in _mod.table.put_item.call_args_list]
    assert len(writes) == 1, "expected exactly one state write — the failure row"
    item = writes[0]
    assert item["run_id"] == RUN_ID
    assert item["step_name"] == StepName.SCHEMA_EXTRACT
    assert item["step_name"] == "schema_extract"
    assert item["status"] == "failed"
    assert item["step_sequence"] == STEP_SEQUENCE[StepName.SCHEMA_EXTRACT]
    assert str(exc.__cause__) in item["error_message"]
    return item


class TestBinaryContainerFailuresAttributeToThisStep:
    """p46 R4.3: an unreadable Avro/ORC object fails the run at schema_extract.

    One test per format. Only the S3 transport is substituted in each — the
    extractor, the handler, ``@step_handler`` and ``StepFailure`` are all real,
    which is the point: the assertion is about the contract between them.
    """

    def test_unreadable_avro_fails_the_run_at_schema_extract(self, monkeypatch):
        """A non-OCF object reaches ``extract_schema_from_avro`` and fails HERE.

        Before task 6.1 these bytes took the ``else`` branch into the JSON
        reader. On a real Avro file that reader finds no complete records and
        returns an Empty_Schema (``lambda_function.py:312-314``) — a handled
        outcome, so the run advanced with zero fields and reported success.
        """
        _mod.table.get_item.return_value = _file_validation_state("data.avro")

        body = MagicMock()
        body.read.return_value = NOT_A_CONTAINER
        mock_s3 = MagicMock()
        mock_s3.get_object.return_value = {"Body": body}
        monkeypatch.setattr(_mod, "s3_client", mock_s3)

        with pytest.raises(StepFailure) as exc_info:
            _mod.lambda_handler(
                {
                    "run_id": RUN_ID,
                    "original_pipeline_id": "pipe-1",
                    "source_format": "avro",
                    "execution_arn": "arn:aws:states:us-west-2:123456789012:execution:sm:exec",
                },
                None,
            )

        item = _assert_failure_is_recorded_against_schema_extract(exc_info, cause_type=ValueError)
        # The hand-written OCF parser's own magic check is what refused it
        # (``_parse_avro_ocf_metadata``), so the operator is told the object is
        # not an Avro container rather than that some JSON did not parse.
        assert "Not an Avro Object Container File" in exc_info.value.message
        assert "Not an Avro Object Container File" in item["error_message"]
        assert item["execution_id"] == "arn:aws:states:us-west-2:123456789012:execution:sm:exec"

    def test_unreadable_orc_fails_the_run_at_schema_extract(self, monkeypatch):
        """A non-ORC object reaches ``extract_schema_from_orc`` and fails HERE.

        ``pyarrow.fs.S3FileSystem`` is patched to serve the bytes from memory
        over a real seekable ``NativeFile``; ``pyarrow.orc.ORCFile`` is real and
        raises ``OSError`` on them, as it does in the deployed layer. The
        extractor re-raises with pyarrow's own message rather than flattening it
        (``lambda_function.py:685-687``).
        """
        _mod.table.get_item.return_value = _file_validation_state("data.orc")

        # No boto3 S3 call is expected on this path at all — ORC reads through
        # the pyarrow handle — so a bare mock is installed to make any
        # get_object/download_file call visibly absent rather than real.
        mock_s3 = MagicMock()
        monkeypatch.setattr(_mod, "s3_client", mock_s3)

        class _StubFileSystem:
            def __init__(self, **kwargs):
                self.constructor_kwargs = kwargs

            def open_input_file(self, path):
                return pa.PythonFile(io.BytesIO(NOT_A_CONTAINER), mode="r")

        with (
            patch("pyarrow.fs.S3FileSystem", side_effect=lambda **kwargs: _StubFileSystem(**kwargs)),
            pytest.raises(StepFailure) as exc_info,
        ):
            _mod.lambda_handler(
                {
                    "run_id": RUN_ID,
                    "original_pipeline_id": "pipe-1",
                    "source_format": "orc",
                    "execution_arn": "arn:aws:states:us-west-2:123456789012:execution:sm:exec",
                },
                None,
            )

        _assert_failure_is_recorded_against_schema_extract(exc_info, cause_type=OSError)
        mock_s3.download_file.assert_not_called()
        mock_s3.get_object.assert_not_called()
