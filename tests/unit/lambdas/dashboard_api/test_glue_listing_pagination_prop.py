# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Feature: p66-glue-catalog-target-selection, Property 5: A paginated listing is the
concatenation of its pages, with its shape unchanged.

*For any* sequence of ``get_databases`` / ``get_tables`` pages — any page count, any
items per page, including empty pages and pages missing the result key — the endpoint's
response array is the in-order concatenation of every page's items, ``count`` equals that
array's length, and the response's top-level key set and per-item field names are exactly
what they were before pagination.

Covers **both** endpoints, which is why every assertion below is parametrized over
``_ENDPOINTS`` rather than written twice: R3.1 and R3.2 are the same requirement against
two operations, and a property written for one of them proves nothing about the other.
``list_glue_tables`` had **no covering test at all** before p66 (codegraph: 1 caller in
``router.py``, no covering tests; ``design.md`` A2), so for that half this file is first
coverage rather than an addition to it.

The oracle is the generated pages, not the producer
---------------------------------------------------------------------------
Every expected value here is computed from the **page plan as it is drawn** — item names
are minted by a running counter while the pages are built, so the expected concatenation
is known before the handler runs and is order-sensitive by construction. Nothing is
re-derived from the handler's own output, and the handler is not run twice to compare
itself. A test written from the producer's output cannot detect the producer drifting
(``LGR-28b4d75-5``, ``LGR-cd58706-3``) — a first-page-only regression would simply move
both sides of such a comparison together.

For the same reason the shape half (R3.5) is written as **literal** key sets:

* Top-level keys come from R3.5 — the ``databases`` / ``tables`` array and the ``count``
  key "keep their current names and types" — plus ``database_name``, which the tables
  endpoint echoes from its own path parameter and which R3.5's cited TypeScript
  interfaces (``types.ts``: ``GlueDatabasesResponse``, ``GlueTablesResponse``) do not
  constrain either way.
* Per-item field names come from ``design.md`` §4.1, whose whole claim about 2.1 is that
  "the projected field names, the ``count`` key, the ``_normalize_aws_datetime`` /
  ``.isoformat()`` treatment of ``CreateTime`` … is untouched". ``name`` specifically is
  pinned by R3.5 and by ``test_glue_databases_response_contract.py``.

Reading either set off a response would make R3.5 unfalsifiable: the assertion would then
say "the handler returns whatever the handler returns".

No AWS call, and no client mocked but Glue
---------------------------------------------------------------------------
Pages are served through a ``MagicMock`` standing in for the module-level ``glue_client``,
substituted with ``monkeypatch.setattr(_resources, "glue_client", …)`` — the mechanism
R3.5 names, and it keeps working because 2.1 passes ``glue_client`` to ``_paginated_items``
as an **argument** rather than closing over it, so the handle is read out of the module
namespace at call time.

``get_paginator`` is wired with a ``side_effect`` that **raises on any other operation
name** rather than with a blanket ``return_value``, following the idiom
``test_glue_databases_response_contract.py`` establishes for 2.2. A blanket mock would
serve ``get_databases`` pages to a handler that had drifted to asking for ``get_tables``
and the test would still pass — the self-confirming-oracle class ``LGR-28b4d75-5``
records. Here a wrong operation is a loud failure.

No ``mock_aws()`` and no DynamoDB/S3/SQS client mock, so
``tests/unit/test_no_duplicate_mocking_prop.py`` is satisfied by construction rather than
by an allowlist entry (``LGR-b46a5bb-4``: "an allowlist entry is a door").

``pytest.MonkeyPatch.context()`` is used inside each example instead of the
``monkeypatch`` **fixture**: a function-scoped fixture is set up once for a whole
``@given`` run rather than per example, which Hypothesis rejects outright via
``HealthCheck.function_scoped_fixture``. The context manager is the same
``monkeypatch.setattr`` mechanism, undone after every example instead of after the last.

**Validates: Requirements 3.1, 3.2, 3.5**
"""

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest
from hypothesis import example, given, settings
from hypothesis import strategies as st

# ---------------------------------------------------------------------------
# Import-time env — table names only.
#
# `sys.path` is already handled for this whole directory by
# `tests/unit/lambdas/dashboard_api/conftest.py:27-30`, which inserts `shared/`
# and the Lambda package at collection-start precisely so no module in here has
# to depend on which file was imported first.
#
# The table names are NOT handled there, and this module reaches
# `handlers/common.py:42-43` through `handlers.resources` — both `AUDIT_TABLE`
# and `AGENT_TRACES_TABLE` are resolved there with `require_env` at **module**
# scope. Without them this file could only be collected when some other module
# in the layer happened to be imported first and set them, which is the
# collection-order dependence `test_schemas_listing_pagination.py` and
# `test_glue_databases_response_contract.py` both write down. `setdefault` makes
# a second writer a no-op.
#
# `AWS_REGION` / `AWS_DEFAULT_REGION` are deliberately absent: `LGR-3e45e65-1`
# pins both by **assignment** in `tests/unit/conftest.py:35-36` so the whole
# unit layer agrees on one region, and a `setdefault` here would be a no-op that
# reads as if this module had a say.
# ---------------------------------------------------------------------------
for _var, _value in {
    "STATE_TABLE": "agentic_data_ingestion_state",
    "MAPPINGS_TABLE": "agentic_data_ingestion_schema_mappings",
    "PIPELINES_TABLE": "agentic_data_ingestion_pipelines",
    "GENERATED_CODE_TABLE": "agentic_data_ingestion_generated_code",
    "TEST_RESULTS_TABLE": "agentic_data_ingestion_test_results",
    "APPROVALS_TABLE": "agentic_data_ingestion_approvals",
    "EXECUTIONS_TABLE": "agentic_data_ingestion_executions",
    "TASK_TOKENS_TABLE": "agentic_data_ingestion_task_tokens",
    "AUDIT_TABLE": "agentic_data_ingestion_audit_log",
    "AGENT_TRACES_TABLE": "agentic_data_ingestion_agent_traces",
    "AGENTIC_DATA_INGESTION_ENV": "test",
}.items():
    os.environ.setdefault(_var, _value)

from handlers import resources as _resources
from handlers.resources import list_glue_databases, list_glue_tables

#: The database the tables endpoint is asked about. Fixed rather than generated:
#: R3.2's subject is the page sequence, and `test_glue_tables_response_contract.py`
#: (task 2.3) owns the path-parameter contract.
DATABASE_NAME = "agentic_data_ingestion_pagination_prop"


# ---------------------------------------------------------------------------
# The two endpoints under test, and the shape R3.5 freezes for each
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _Endpoint:
    """One listing endpoint, its Glue call shape, and its response shape.

    `top_level_keys` and `item_keys` are the R3.5 half — declared from the
    requirements and `design.md` §4.1, never read off a response.
    """

    label: str
    handler: Callable[[dict[str, Any], Any], dict[str, Any]]
    operation: str
    result_key: str
    array_key: str
    top_level_keys: frozenset[str]
    item_keys: frozenset[str]
    make_page_item: Callable[[str], dict[str, Any]]
    event: dict[str, Any]

    def __str__(self) -> str:  # keeps pytest ids readable
        return self.label


DATABASES = _Endpoint(
    label="GET /resources/glue/databases",
    handler=list_glue_databases,
    operation="get_databases",
    result_key="DatabaseList",
    array_key="databases",
    # R3.5: the `databases` array and the `count` key keep their current names.
    top_level_keys=frozenset({"databases", "count"}),
    # `design.md` §4.1: the projected field names are untouched by 2.1. `name` is
    # additionally pinned by R3.5 and by test_glue_databases_response_contract.py.
    item_keys=frozenset({"name", "description", "locationUri", "createTime"}),
    make_page_item=lambda name: {
        "Name": name,
        "Description": f"description of {name}",
        "LocationUri": f"s3://warehouse/{name}",
    },
    event={},
)

TABLES = _Endpoint(
    label="GET /resources/glue/databases/{database_name}/tables",
    handler=list_glue_tables,
    operation="get_tables",
    result_key="TableList",
    array_key="tables",
    # R3.5 for `tables` / `count`; `database_name` is the path parameter this
    # endpoint echoes, and it is part of the key set 2.1 must not have changed.
    top_level_keys=frozenset({"database_name", "tables", "count"}),
    item_keys=frozenset({"name", "databaseName", "description", "storageDescriptor", "createTime", "updateTime"}),
    make_page_item=lambda name: {
        "Name": name,
        "DatabaseName": DATABASE_NAME,
        "Description": f"description of {name}",
        "StorageDescriptor": {
            "Location": f"s3://warehouse/{DATABASE_NAME}/{name}",
            "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
        },
    },
    event={"pathParameters": {"database_name": DATABASE_NAME}},
)

_ENDPOINTS = (DATABASES, TABLES)

#: The nested key set inside a table's `storageDescriptor`, from `design.md` §4.1.
#: Checked separately because `item_keys` only reaches the top of each item, and a
#: rename one level down would otherwise pass.
_STORAGE_DESCRIPTOR_KEYS = frozenset({"location", "inputFormat", "outputFormat"})


# ---------------------------------------------------------------------------
# Strategy — a page plan, from which both the pages and the oracle are built
# ---------------------------------------------------------------------------
#: One element per page. `None` means "this page omits the result key entirely";
#: an int means "this page carries that many items", `0` included. Both shapes are
#: named by the task and both are ones a real paginator hands back —
#: `_paginated_items` coalesces them to `[]` via `page.get(result_key, []) or []`
#: rather than raising, and that coalescing is what this generator exercises.
#:
#: `min_size=1`: a boto3 paginator always yields at least one page. The zero-page
#: case is a mock artifact rather than a shape the property is about, and
#: `test_glue_databases_response_contract.py::test_empty_databases` already pins
#: that an empty body must come from an empty page rather than from no pages.
_PAGE_PLAN = st.lists(
    st.one_of(st.none(), st.integers(min_value=0, max_value=4)),
    min_size=1,
    max_size=6,
)


def _build_pages(endpoint: _Endpoint, plan: Iterable[int | None]) -> tuple[list[dict], list[str]]:
    """Materialise `plan` into Glue-shaped pages, and return the oracle beside them.

    The second return value is the expected item order, minted here by a running
    counter as each page is filled. That is the whole oracle discipline of this
    file: the names are unique and carry their global position, so a response
    that drops a page, reorders two items, or duplicates one cannot match — and
    none of it is derived from what the handler did with the pages.
    """
    pages: list[dict[str, Any]] = []
    expected_names: list[str] = []
    minted = 0

    for page_size in plan:
        if page_size is None:
            # A page with no result key at all.
            pages.append({})
            continue
        items = []
        for _ in range(page_size):
            # Zero-padded so lexical and positional order agree, which makes a
            # failure diff readable; the assertion itself compares lists, so it
            # is sensitive to order regardless.
            name = f"item_{minted:03d}"
            minted += 1
            items.append(endpoint.make_page_item(name))
            expected_names.append(name)
        pages.append({endpoint.result_key: items})

    return pages, expected_names


def _serve(endpoint: _Endpoint, pages: list[dict[str, Any]]) -> tuple[dict[str, Any], MagicMock]:
    """Run `endpoint` against `pages` and return the parsed body plus the Glue mock.

    Substitution is `monkeypatch.setattr` on the module attribute (R3.5), through
    `pytest.MonkeyPatch.context()` so it is undone per example rather than per
    test function.
    """
    mock_glue = MagicMock()
    paginator = MagicMock()
    paginator.paginate.return_value = list(pages)

    def _get_paginator(operation: str) -> MagicMock:
        assert operation == endpoint.operation, (
            f"{endpoint.label} asked for a {operation!r} paginator; this property is pinned to "
            f"{endpoint.operation!r} (R3.1/R3.2). A blanket mock would have served these pages "
            f"to the wrong operation and passed."
        )
        return paginator

    mock_glue.get_paginator.side_effect = _get_paginator

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_resources, "glue_client", mock_glue)
        response = endpoint.handler(dict(endpoint.event), None)

    assert response["statusCode"] == 200, f"{endpoint.label} did not return 200: {response}"
    return json.loads(response["body"]), mock_glue


# ---------------------------------------------------------------------------
# Property 5 — the concatenation half (R3.1, R3.2)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=str)
@given(plan=_PAGE_PLAN)
@settings(max_examples=10)
# The three shapes the task names, forced rather than hoped for. Hypothesis draws
# ten examples; `@example` guarantees these are among them on every run.
@example(plan=[2, 0, 3, None])  # the multi-page case, with an empty page and a keyless one
@example(plan=[0])  # a single empty page — an account with nothing in it
@example(plan=[None])  # a single page that omits the result key
@example(plan=[1, 1, 1, 1, 1, 1])  # six pages of one, so a first-page-only read returns 1 of 6
def test_the_response_array_is_the_in_order_concatenation_of_every_page(endpoint, plan):
    """R3.1/R3.2: every page is enumerated, in order, exactly once.

    The single assertion that a first-page-only regression cannot survive: with
    six pages of one item, a handler reading only the first returns
    ``["item_000"]`` against an expected six.

    **Validates: Requirements 3.1, 3.2**
    """
    pages, expected_names = _build_pages(endpoint, plan)
    body, _ = _serve(endpoint, pages)

    assert [item["name"] for item in body[endpoint.array_key]] == expected_names, (
        f"{endpoint.label} is not the in-order concatenation of its pages. "
        f"Pages carried {len(expected_names)} items across {len(pages)} pages; "
        f"the response carried {len(body[endpoint.array_key])}."
    )


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=str)
@given(plan=_PAGE_PLAN)
@settings(max_examples=10)
@example(plan=[2, 0, 3, None])
@example(plan=[0])
@example(plan=[None])
def test_count_equals_the_length_of_the_array_it_describes(endpoint, plan):
    """R3.1/R3.2: ``count`` covers every page, not just the first.

    Asserted against the **generated** total as well as against the array's own
    length. ``count == len(array)`` alone is satisfied by a handler that lost
    four pages and counted what remained; it is a consistency check, not a
    pagination one.

    **Validates: Requirements 3.1, 3.2**
    """
    pages, expected_names = _build_pages(endpoint, plan)
    body, _ = _serve(endpoint, pages)

    assert body["count"] == len(expected_names), (
        f"{endpoint.label} reported count={body['count']} for {len(expected_names)} generated items"
    )
    assert body["count"] == len(body[endpoint.array_key])


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=str)
@given(plan=_PAGE_PLAN)
@settings(max_examples=10)
@example(plan=[2, 0, 3, None])
def test_the_enumeration_goes_through_the_paginator_and_never_the_single_call(endpoint, plan):
    """R3.1/R3.2: the mechanism, asserted where the output cannot show it.

    A one-page plan produces a body that an unpaginated handler would also
    produce, so for those examples the assertions above hold either way. These
    two assert the call shape instead: the paginator was asked for, and the
    single ``get_databases()`` / ``get_tables()`` call 2.1 replaced was not made.
    A handler calling **both** would satisfy every other test in this file while
    still capping at one page in production.

    **Validates: Requirements 3.1, 3.2**
    """
    pages, _ = _build_pages(endpoint, plan)
    _, mock_glue = _serve(endpoint, pages)

    mock_glue.get_paginator.assert_called_once_with(endpoint.operation)
    assert not getattr(mock_glue, endpoint.operation).called, (
        f"{endpoint.label} still calls glue_client.{endpoint.operation}() directly — that returns "
        f"one page and silently under-reports past it (R3.1/R3.2)"
    )


def test_the_tables_paginator_receives_the_database_name_through_kwargs():
    """R3.2: ``DatabaseName`` reaches the paginator, not just the log line.

    Not a property — there is one correct value and no input to vary. It is here
    because it is the one thing pagination could have dropped that the response
    body would not reveal: ``database_name`` in the body is echoed from the path
    parameter, so it stays correct even if the Glue call lost the filter and
    enumerated the wrong database.

    **Validates: Requirement 3.2**
    """
    pages, expected_names = _build_pages(TABLES, [1, 1])
    body, mock_glue = _serve(TABLES, pages)

    mock_glue.get_paginator("get_tables").paginate.assert_called_with(DatabaseName=DATABASE_NAME)
    assert body["database_name"] == DATABASE_NAME
    assert [item["name"] for item in body["tables"]] == expected_names


# ---------------------------------------------------------------------------
# Property 5 — the shape half (R3.5)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=str)
@given(plan=_PAGE_PLAN)
@settings(max_examples=10)
@example(plan=[2, 0, 3, None])
@example(plan=[None])
def test_the_top_level_key_set_is_exactly_what_it_was_before_pagination(endpoint, plan):
    """R3.5: the response's top-level keys are unchanged by 2.1.

    Equality, not containment: a key **added** by pagination — a ``next_token``,
    a ``truncated`` flag — breaks the two TypeScript response interfaces R3.5
    names just as surely as a removed one, and R3.4's truncation signal is
    recorded as having **no producer** (task 2.5, O5), so its absence here is the
    posture rather than an oversight.

    **Validates: Requirement 3.5**
    """
    pages, _ = _build_pages(endpoint, plan)
    body, _ = _serve(endpoint, pages)

    assert set(body) == set(endpoint.top_level_keys), (
        f"{endpoint.label}'s top-level key set changed: expected {sorted(endpoint.top_level_keys)}, got {sorted(body)}"
    )


@pytest.mark.parametrize("endpoint", _ENDPOINTS, ids=str)
@given(plan=_PAGE_PLAN)
@settings(max_examples=10)
@example(plan=[2, 0, 3, None])
@example(plan=[4])
def test_every_item_carries_exactly_the_field_names_it_did_before_pagination(endpoint, plan):
    """R3.5: per-item field names are unchanged, on **every** item.

    Checked per item rather than on the first: pagination assembles the array
    across pages, so a projection applied on only one page is precisely the
    defect shape this could have. The nested ``storageDescriptor`` keys are
    checked too — ``item_keys`` stops at the top of each item, and a rename one
    level down would otherwise pass unnoticed.

    **Validates: Requirement 3.5**
    """
    pages, expected_names = _build_pages(endpoint, plan)
    body, _ = _serve(endpoint, pages)

    for position, item in enumerate(body[endpoint.array_key]):
        assert set(item) == set(endpoint.item_keys), (
            f"{endpoint.label} item {position} ({item.get('name')!r}) carries "
            f"{sorted(item)}, expected {sorted(endpoint.item_keys)}"
        )

    if endpoint is TABLES:
        for position, item in enumerate(body["tables"]):
            assert set(item["storageDescriptor"]) == set(_STORAGE_DESCRIPTOR_KEYS), (
                f"tables item {position} storageDescriptor carries "
                f"{sorted(item['storageDescriptor'])}, expected {sorted(_STORAGE_DESCRIPTOR_KEYS)}"
            )

    # The loop above is vacuous on a plan that generated nothing, so pair it with
    # the count. Without this, an all-empty plan would let the field-name
    # assertion pass by never running.
    assert len(body[endpoint.array_key]) == len(expected_names)
