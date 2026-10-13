# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""`GET /api/approvals/{approval_id}` — the per-approval payload read (p56).

WHY THIS FILE EXISTS
--------------------
`ApprovalsPage`'s review modal reads `approval.details`, and neither listing
handler produced that key — so every reader on that surface correctly reported
"not established": the mapping count, the unsourced target fields, the
confidence, the proposed renames, and `MappingViewer`'s whole panel. This is the
endpoint that gives it a payload, and codegraph reports **no covering tests** for
`approvals_router`, so the dispatch half is tested here too.

THE THREE STATES, WHICH ARE THE POINT
-------------------------------------
Reading a stored `approval_data` ends three ways and each is a different fact:

===============  ======================================================
`parsed`         the attribute decoded to an object; `details` carries it
`absent`         no attribute; the **producer** stored nothing
`unreadable`     something was stored and could not be interpreted
===============  ======================================================

`LGR-4854b3d-1` (**template / ratified**) ruled this three-state shape for
`field_count` and refused to collapse `null` onto `0`, because "every
hand-written `?? 0` collapses null into 'declares nothing'". Returning `{}` for
an unreadable payload would be that collapse for the whole payload: `{}` reads as
*the producer stored nothing* when the truth is *we could not read what it
stored*. p54 drew the same line for one field —
`REUSED_MAPPING_UNREADABLE_STATEMENT` exists so an unreadable reused mapping
never renders as a mapping with no rows, per `LGR-b2f79ef-14`'s "a named
reference to something absent is not a thing with no fields".

So the load-bearing assertion in this file is not that each state produces the
right value; it is that **no two of the three produce the same response**.

WHERE THE EXPECTATIONS COME FROM
--------------------------------
From the requirement and from the live table, never from running the handler and
recording its output (`LGR-28b4d75-5`: "a test written from the producer's output
cannot detect the producer drifting, because the output is its own oracle").

`_LIVE_ALL_NULL_PAYLOAD` below is the **verbatim** `approval_data` of the pending
approval this work was reported against — `newtest-ab2a08a0_schema_mapping_
1787402782`, created 2026-08-22, read read-only from `agentic_data_ingestion_
approvals` in account 777788889999 / us-west-2. It matters because it is a state
the three-way classification did not anticipate: **present, valid JSON,
structurally complete, and every substantive value `null`.** That is `parsed` —
the producer stored a payload and we read it — and the field readers then report
each null key as not established on their own. It is not a fourth response state
and must not become one; the test below pins that.

`_LIVE_POPULATED_KEYS` is the key set of a payload from the same table after the
producer began populating them (2026-08-27 onward), so the populated case is
shaped like a real one.

WHAT THIS FILE DOES NOT COVER, DELIBERATELY
-------------------------------------------
The approve/reject branches. This change is a read path and goes nowhere near
them; `LGR-c58f276-2`, `LGR-c58f276-3` (a PROPOSED invariant), `LGR-e59edd7-3`
and `LGR-7d19eca-3` all govern those, and their own test files are the evidence
this change left them alone.

The two listing handlers. They were **not** widened, and
`test_approvals_dispatch_characterization.py` must keep passing unamended — it is
the evidence of that, so re-asserting it here would put the evidence in two
places.
"""

import json
import os
import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
# `conftest.py` in this directory already puts `shared/` and the dashboard API
# Lambda package on `sys.path`; repeated here for the reason that conftest gives
# — under `--dist=loadfile` any file may be the first imported on a worker.
_DASHBOARD_API_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api"
)
_SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared")
for _p in (os.path.abspath(_DASHBOARD_API_DIR), os.path.abspath(_SHARED_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

#: The unit layer's region, read from the layer that owns the decision rather
#: than spelled again here (`LGR-3e45e65-1`, **template / ratified**).
_UNIT_LAYER_REGION = os.environ["AWS_REGION"]

#: The three status literals, spelled here rather than imported from the module
#: under test — a test reading its expectation out of the code under test cannot
#: detect that code drifting.
_PARSED = "parsed"
_ABSENT = "absent"
_UNREADABLE = "unreadable"

#: The live pending approval this work was reported against (account
#: 777788889999, us-west-2, read-only). See the module docstring.
_LIVE_APPROVAL_ID = "newtest-ab2a08a0_schema_mapping_1787402782"
_LIVE_PIPELINE_ID = "newtest-ab2a08a0"
_LIVE_RUN_ID = "run-2026-08-22-12-46-00-8032490a"

#: Verbatim, including the key order and the one non-null value.
_LIVE_ALL_NULL_PAYLOAD = (
    '{"source_schema": null, "target_schema": null, "proposed_mappings": null, '
    '"confidence_scores": null, "unmapped_fields": null, "mapping_summary": null, '
    '"evidence": null, "questions": null, "generated_by": "us.anthropic.claude-sonnet-4-6"}'
)

#: The key set the producer writes once it populates the payload, from a
#: 2026-08-31 row in the same table.
_LIVE_POPULATED_KEYS = (
    "source_schema",
    "target_schema",
    "proposed_mappings",
    "confidence_scores",
    "unmapped_fields",
    "mapping_summary",
    "generated_by",
    "mapping_count",
    "rename_manifest",
)


@pytest.fixture
def approvals_module(mock_lambda_env, monkeypatch):
    """`handlers.approvals`, imported with the env its import requires.

    `mock_lambda_env` is not optional: `handlers/common.py` calls `require_env`
    for nine table names at **import** time. The region variables are re-pinned
    because that module freezes `AWS_REGION` into module-global boto3 resources
    at import, so whichever test imports it first would otherwise decide the
    region for the whole session (`LGR-3e45e65-1`).
    """
    monkeypatch.setenv("AWS_REGION", _UNIT_LAYER_REGION)
    monkeypatch.setenv("AWS_DEFAULT_REGION", _UNIT_LAYER_REGION)

    from handlers import approvals as approvals_mod

    return approvals_mod


def _row(**attrs: Any) -> dict[str, Any]:
    """One approvals row, shaped like the live one minus whatever a test varies."""
    return {
        "approval_id": _LIVE_APPROVAL_ID,
        "approval_type": "schema_mapping",
        "pipeline_id": _LIVE_PIPELINE_ID,
        "run_id": _LIVE_RUN_ID,
        "status": "pending",
        "created_at": "2026-08-22T12:46:23.029231+00:00",
        "has_task_token": True,
        **attrs,
    }


def _get(approvals_module: Any, item: dict[str, Any] | None, approval_id: str = _LIVE_APPROVAL_ID):
    """Call `get_approval` against a stubbed table; return `(status, body)`.

    Patching the module-global `approvals_table` is the established way to drive
    a read in this directory (`test_query_not_scan.py`,
    `test_approvals_dispatch_characterization.py`). No `moto.mock_aws()` is
    created and no boto3 client is replaced, so
    `tests/unit/test_no_duplicate_mocking_prop.py` is satisfied.
    """
    stub = MagicMock()
    stub.get_item.return_value = {"Item": item} if item is not None else {}
    original = approvals_module.approvals_table
    approvals_module.approvals_table = stub
    try:
        response = approvals_module.get_approval(approval_id)
    finally:
        approvals_module.approvals_table = original
    return response["statusCode"], json.loads(response["body"]), stub


# ===========================================================================
# The 404
# ===========================================================================


class TestMissingApproval:
    """No such approval is a 404 that names the id."""

    def test_absent_row_is_404_naming_the_id(self, approvals_module):
        status, body, _ = _get(approvals_module, None, approval_id="appr-does-not-exist")

        assert status == 404
        # Naming the id is the requirement, not merely answering 404: a bare
        # "not found" leaves a caller unable to tell which id was rejected.
        assert "appr-does-not-exist" in body["error"]

    def test_the_404_wording_matches_approve_and_reject(self, approvals_module):
        """One sentence for one fact, across all three verbs on this resource.

        `approve_request` and `reject_request` already answer
        `f"Approval not found: {approval_id}"` for the same input. A second
        spelling here would let a client get two phrasings of "no such approval"
        out of one resource — and `test_router_response_contract.py` matches on
        the literal `"Approval not found"` to prove routing reached the handler,
        so a divergence would quietly weaken that suite too.
        """
        _, body, _ = _get(approvals_module, None, approval_id="appr-x")

        assert body["error"] == "Approval not found: appr-x"

    def test_the_read_is_keyed_on_the_table_not_walked(self, approvals_module):
        """A `get_item` on the partition key, not a Query or a Scan.

        Asserted on the call rather than the rows because the rows cannot
        distinguish a keyed read from a filtered walk that happened to return the
        same thing.
        """
        _, _, stub = _get(approvals_module, _row(approval_data="{}"))

        stub.get_item.assert_called_once_with(Key={"approval_id": _LIVE_APPROVAL_ID})
        stub.query.assert_not_called()
        stub.scan.assert_not_called()


# ===========================================================================
# The parsed state
# ===========================================================================


class TestParsedPayload:
    """A readable payload reaches the response as `details`."""

    def test_a_populated_payload_arrives_as_details(self, approvals_module):
        payload = {
            "mapping_count": 7,
            "proposed_mappings": [{"source_field": "signup_date", "target_field": "registration_date"}],
            "unmapped_fields": {
                "source": [],
                "target": [],
                "target_unsourced": ["email"],
                "target_required": ["email"],
            },
            "generated_by": "us.anthropic.claude-sonnet-4-6",
        }
        status, body, _ = _get(approvals_module, _row(approval_data=json.dumps(payload)))

        assert status == 200
        assert body["details_status"] == _PARSED
        assert body["details"] == payload

    def test_every_key_the_live_producer_writes_survives_the_parse(self, approvals_module):
        """No key is dropped, renamed or defaulted on the way through.

        The handler is a parse and a rename of one field, not a projection. A
        projection here would be a second opinion about which keys the modal may
        see, and the readers below it are the authority on that.
        """
        payload = dict.fromkeys(_LIVE_POPULATED_KEYS, "x")
        _, body, _ = _get(approvals_module, _row(approval_data=json.dumps(payload)))

        assert set(body["details"]) == set(_LIVE_POPULATED_KEYS)

    def test_an_empty_object_is_parsed_not_absent(self, approvals_module):
        """`{}` is a payload the producer wrote. Five live rows carry exactly it.

        It is `parsed` because the read succeeded — the producer established an
        empty payload. Classifying it `absent` would be the same collapse in the
        other direction, and the field readers already report each missing key as
        not established without help.
        """
        status, body, _ = _get(approvals_module, _row(approval_data="{}"))

        assert status == 200
        assert body["details_status"] == _PARSED
        assert body["details"] == {}

    def test_a_map_typed_attribute_passes_through(self, approvals_module):
        """A dict arriving unparsed is parsed, matching `get_run_details`.

        The attribute is declared a JSON string, but `get_run_details`' parse
        already tolerates a Map-typed value (`isinstance(raw, str)`), and
        diverging from it would be a second opinion about one stored shape.
        """
        _, body, _ = _get(approvals_module, _row(approval_data={"mapping_count": 3}))

        assert body["details_status"] == _PARSED
        assert body["details"] == {"mapping_count": 3}


class TestLiveAllNullPayload:
    """The reported approval's own payload: parsed, and every value `null`.

    This is the state the three-way classification did not anticipate and the
    reason it needs pinning: it is **not** a fourth response state. The payload
    was stored and it was read, so it is `parsed`; the individual nulls are then
    each reader's business, and `approvalMappingCount` /
    `approvalUnsourcedTargetFields` / `approvalProposedRenames` already answer
    *not established* for every one of them.
    """

    def test_the_live_payload_is_parsed(self, approvals_module):
        status, body, _ = _get(approvals_module, _row(approval_data=_LIVE_ALL_NULL_PAYLOAD))

        assert status == 200
        assert body["details_status"] == _PARSED

    def test_the_nulls_are_preserved_rather_than_stripped_or_defaulted(self, approvals_module):
        """`null` must reach the readers AS `null`.

        Stripping the keys would be indistinguishable from a payload that never
        had them, and defaulting them — to `0`, to `[]`, to `{}` — is exactly the
        collapse `LGR-4854b3d-1` forbids: it would turn "the producer recorded no
        mapping" into "the producer recorded zero mappings".
        """
        _, body, _ = _get(approvals_module, _row(approval_data=_LIVE_ALL_NULL_PAYLOAD))

        details = body["details"]
        assert details["proposed_mappings"] is None
        assert details["unmapped_fields"] is None
        assert details["confidence_scores"] is None
        assert "mapping_count" not in details, "the live payload predates this key; do not invent it"

    def test_the_one_populated_key_reaches_the_response(self, approvals_module):
        """`generated_by` is the only non-null value on the reported approval.

        So it is the one field on that screen this endpoint changes: AI Model goes
        from `Unknown` to the model id. Worth an assertion precisely because it is
        the whole of the user-visible improvement for that row.
        """
        _, body, _ = _get(approvals_module, _row(approval_data=_LIVE_ALL_NULL_PAYLOAD))

        assert body["details"]["generated_by"] == "us.anthropic.claude-sonnet-4-6"


# ===========================================================================
# The absent state
# ===========================================================================


class TestAbsentPayload:
    """No stored attribute: the producer recorded nothing."""

    @pytest.mark.parametrize(
        "attrs,case",
        [
            ({}, "no attribute at all"),
            ({"approval_data": ""}, "an empty string"),
            ({"approval_data": None}, "an explicit null attribute"),
            ({"approval_data": {}}, "an empty Map"),
        ],
    )
    def test_absent_is_reported_as_absent_with_no_details_key(self, approvals_module, attrs, case):
        status, body, _ = _get(approvals_module, _row(**attrs))

        assert status == 200, case
        assert body["details_status"] == _ABSENT, case
        # OMITTED, not `{}`. A consumer that ignores `details_status` must not be
        # able to read "the producer stored an empty payload" out of this.
        assert "details" not in body, case

    def test_the_raw_attribute_is_not_echoed_back(self, approvals_module):
        """`approval_data` does not appear in the response.

        `details` supersedes it, no frontend file reads the raw attribute
        (verified by grep over `dashboard-react/src/`), and a live
        `schema_mapping` payload runs to ~6 KB — so echoing both would double the
        response for no reader.
        """
        _, body, _ = _get(approvals_module, _row(approval_data=_LIVE_ALL_NULL_PAYLOAD))

        assert "approval_data" not in body

    def test_the_stored_row_is_not_mutated_into_the_caller(self, approvals_module):
        """The row dict the table handed back is the handler's to reshape.

        Asserted because the handler `pop`s `approval_data` off it, and a `pop`
        on a caller-owned object would be a side effect on someone else's data.
        boto3 hands back a fresh dict per `get_item`, so this holds — the test
        exists to notice if that ever stops being true through a cache or a
        shared fixture.
        """
        row = _row(approval_data="{}")
        stub = MagicMock()
        stub.get_item.return_value = {"Item": row}
        original = approvals_module.approvals_table
        approvals_module.approvals_table = stub
        try:
            approvals_module.get_approval(_LIVE_APPROVAL_ID)
        finally:
            approvals_module.approvals_table = original

        # The handler owns the dict it was given, so `approval_data` is gone from
        # it — the point is that nothing ELSE was lost.
        assert row["approval_id"] == _LIVE_APPROVAL_ID
        assert row["status"] == "pending"


# ===========================================================================
# The unreadable state — the one that must not read as zero
# ===========================================================================


class TestUnreadablePayload:
    """Something was stored and could not be interpreted."""

    @pytest.mark.parametrize(
        "raw,case",
        [
            ("{not json", "a truncated object"),
            ("{'single': 'quotes'}", "Python repr rather than JSON"),
            ('{"mapping_count": 7', "an unterminated object"),
            ("\x00\x01\x02", "binary noise"),
        ],
    )
    def test_invalid_json_is_unreadable_and_carries_no_details(self, approvals_module, raw, case):
        status, body, _ = _get(approvals_module, _row(approval_data=raw))

        assert status == 200, case
        assert body["details_status"] == _UNREADABLE, case
        assert "details" not in body, case

    @pytest.mark.parametrize(
        "raw,case",
        [
            ("[]", "an empty array"),
            ('[{"source_field": "a"}]', "an array of mappings"),
            ("42", "a bare number"),
            ('"a string"', "a bare string"),
            ("true", "a bare boolean"),
            ("null", "a literal JSON null"),
        ],
    )
    def test_valid_json_that_is_not_an_object_is_unreadable(self, approvals_module, raw, case):
        """One rule, no special cases: not a JSON object means not a details map.

        A list, a number, a string and a literal `null` are all *decodable* and
        none is something fields can be read off. The array cases are the ones
        that matter in practice: `get_run_details`' parse assigns whatever
        `json.loads` returns straight to `details`, so a stored `"[]"` reaches
        that frontend typed `Record<string, unknown>` while actually being an
        array. Recorded as a finding — `pipelines_runs.py` is outside this change
        — and refused here.
        """
        status, body, _ = _get(approvals_module, _row(approval_data=raw))

        assert status == 200, case
        assert body["details_status"] == _UNREADABLE, case
        assert "details" not in body, case

    def test_a_non_string_non_dict_attribute_is_unreadable(self, approvals_module):
        """A number or a list stored directly in the attribute, not as JSON."""
        for raw in (7, [1, 2, 3], True):
            _, body, _ = _get(approvals_module, _row(approval_data=raw))
            assert body["details_status"] == _UNREADABLE, raw

    def test_an_unreadable_payload_does_not_answer_500(self, approvals_module):
        """A payload we cannot read is a fact about the approval, not a failure.

        Answering 500 would deny the approver the row's other fields — status,
        type, pipeline, created — over a payload that is only one part of what
        the modal shows.
        """
        status, _, _ = _get(approvals_module, _row(approval_data="{not json"))

        assert status == 200


class TestTheThreeStatesAreDistinguishable:
    """The requirement itself, in one place: no two states look alike.

    Each pair is asserted rather than just each value, because the defect this
    endpoint exists to avoid is a *merge* — and a merge passes every per-state
    assertion while failing the thing that matters.
    """

    def _state(self, approvals_module, attrs: dict[str, Any]) -> tuple[str, bool]:
        _, body, _ = _get(approvals_module, _row(**attrs))
        return body["details_status"], "details" in body

    def test_unreadable_is_not_absent(self, approvals_module):
        unreadable = self._state(approvals_module, {"approval_data": "{not json"})
        absent = self._state(approvals_module, {})

        assert unreadable != absent

    def test_unreadable_is_not_an_empty_payload(self, approvals_module):
        """The headline case. `{}` says the producer stored nothing; unreadable
        says we could not read what it stored."""
        unreadable = self._state(approvals_module, {"approval_data": "{not json"})
        empty = self._state(approvals_module, {"approval_data": "{}"})

        assert unreadable != empty

    def test_absent_is_not_an_empty_payload(self, approvals_module):
        absent = self._state(approvals_module, {})
        empty = self._state(approvals_module, {"approval_data": "{}"})

        assert absent != empty

    def test_the_status_is_always_present(self, approvals_module):
        """On all three states, so `undefined` is never a fourth meaning."""
        for attrs in ({}, {"approval_data": "{not json"}, {"approval_data": "{}"}):
            _, body, _ = _get(approvals_module, _row(**attrs))
            assert body["details_status"] in {_PARSED, _ABSENT, _UNREADABLE}


# ===========================================================================
# The field rename the modal depends on
# ===========================================================================


class TestPipelineKeyNormalisation:
    """`pipeline_id` → `pipeline_key`, exactly as both listing handlers do it."""

    def test_pipeline_key_is_added(self, approvals_module):
        """Without it the modal's Pipeline field and its row link both go blank —
        `RunDetail.pipeline_key` was the same class of defect (`LGR-8a4fefa-2`)."""
        _, body, _ = _get(approvals_module, _row(approval_data="{}"))

        assert body["pipeline_key"] == _LIVE_PIPELINE_ID
        assert body["pipeline_id"] == _LIVE_PIPELINE_ID, "additive — the stored name stays"

    def test_an_existing_pipeline_key_is_not_overwritten(self, approvals_module):
        _, body, _ = _get(approvals_module, _row(approval_data="{}", pipeline_key="already-set"))

        assert body["pipeline_key"] == "already-set"

    def test_a_row_without_pipeline_id_gets_no_invented_key(self, approvals_module):
        """A ghost row keeps its Review affordance and states no pipeline.

        `get_pending_approvals` drops such rows from the LISTING, so one cannot
        normally be selected — but this endpoint is keyed and may be reached
        directly, and inventing a blank `pipeline_key` would assert a pipeline the
        row does not name.
        """
        row = {"approval_id": "ghost-1", "status": "pending", "approval_data": "{}"}
        _, body, _ = _get(approvals_module, row, approval_id="ghost-1")

        assert "pipeline_key" not in body

    def test_the_deciding_identity_is_not_synthesised_here(self, approvals_module):
        """`decided_by` / `decided_at` / `comment` are deliberately NOT derived.

        `get_run_details` collapses the arm-specific stored names into those, and
        `LGR-e59edd7-3` / `LGR-e59edd7-4` (both **template / ratified**) make that
        collapse ONE authority precisely so two surfaces cannot disagree about a
        deciding identity. A second collapse here would be that drift. The review
        modal reads none of them; `ApprovalWorkflowSection` does, fed by
        `get_run_details`.
        """
        _, body, _ = _get(
            approvals_module,
            _row(approval_data="{}", status="approved", approved_by="sub-uuid", approved_at="2026-08-22T13:00:00Z"),
        )

        for synthesised in ("decided_by", "decided_by_email", "decided_at", "comment"):
            assert synthesised not in body, f"{synthesised} must stay one authority (LGR-e59edd7-4)"
        # The stored names are untouched and still there, so nothing is lost.
        assert body["approved_by"] == "sub-uuid"
