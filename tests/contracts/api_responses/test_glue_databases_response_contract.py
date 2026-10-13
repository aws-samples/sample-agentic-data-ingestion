# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /resources/glue/databases uses database_name field.

The frontend expects each database object to have a `database_name` field.
The backend currently returns `name` instead. This contract test catches
that field name inconsistency.

Task 7.2 — Requirements 4.6, 4.8

p66 task 2.2 — migrated off the single-call mock shape (R3.1, R3.5)
---------------------------------------------------------------------------
`list_glue_databases` now enumerates through `_paginated_items`, which calls
`client.get_paginator("get_databases").paginate()`. Every test here used to
drive the handler through `glue_client.get_databases.return_value`, and after
that migration **that mock shape does not fail loudly — it goes quiet.** A
`MagicMock` configures `__iter__`, so `list(MagicMock().get_paginator(op).paginate())`
is `[]` rather than a raise, and the handler returns `200 {"databases": [], "count": 0}`.
Replayed against that body, two of the five cases failed on `IndexError` and the
other three **passed while asserting nothing** (`"databases" in body`, `[] == []`,
`0 == 0`). A suite that green-lights an empty body cannot be used as evidence
that R3.1 landed, so the pages are supplied through `get_paginator` here and
`test_databases_span_multiple_pages` exists to make a first-page-only regression
fail rather than pass.
"""

import json
import os
import sys
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
LAMBDA_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "lambda", "agentic_data_ingestion_dashboard_api")
SHARED_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "..", "shared")
sys.path.insert(0, os.path.abspath(LAMBDA_DIR))
sys.path.insert(0, os.path.abspath(SHARED_DIR))

os.environ.setdefault("STATE_TABLE", "agentic_data_ingestion_state")
os.environ.setdefault("MAPPINGS_TABLE", "agentic_data_ingestion_schema_mappings")
os.environ.setdefault("PIPELINES_TABLE", "agentic_data_ingestion_pipelines")
os.environ.setdefault("GENERATED_CODE_TABLE", "agentic_data_ingestion_generated_code")
os.environ.setdefault("TEST_RESULTS_TABLE", "agentic_data_ingestion_test_results")
os.environ.setdefault("APPROVALS_TABLE", "agentic_data_ingestion_approvals")
os.environ.setdefault("EXECUTIONS_TABLE", "agentic_data_ingestion_executions")
os.environ.setdefault("TASK_TOKENS_TABLE", "agentic_data_ingestion_task_tokens")
# `AUDIT_TABLE` and `AGENT_TRACES_TABLE` are enumerated here for the reason
# `test_metrics_trend_seam.py:143-148` already writes down: `common.py:42-43`
# resolves both with `require_env`, at module scope, and this module reaches it
# through `handlers.resources`. Without them this file could only be collected
# when some *other* module in the layer happened to be imported first and set
# them — it raised `OSError: Required environment variable 'AGENT_TRACES_TABLE'
# is not set` on its own. `AUDIT_TABLE` is also set by `tests/contracts/conftest.py`,
# but depending on that is the same order-dependence in a wider costume, and
# `setdefault` makes the second write a no-op.
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
os.environ.setdefault("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from handlers import resources as _resources
from handlers.resources import list_glue_databases


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def glue_mock(monkeypatch):
    """Substitute the module-level `glue_client` with a paginating mock (R3.5).

    `monkeypatch.setattr` on the module attribute is the mechanism R3.5 names,
    and it keeps working because `list_glue_databases` passes `glue_client` to
    `_paginated_items` as an argument rather than closing over it — the handle is
    read out of this module's namespace at call time.
    """
    mock_glue = MagicMock()
    monkeypatch.setattr(_resources, "glue_client", mock_glue)
    return mock_glue


def _paginate_databases(glue_mock, *pages):
    """Serve `*pages` from `get_paginator("get_databases").paginate()`.

    Each page is a `DatabaseList` payload, exactly as Glue returns it, so the
    handler is exercised through the real call shape.

    `get_paginator` is wired with a `side_effect` that **raises on any other
    operation name** rather than with a blanket `return_value`. A blanket mock
    would serve these pages to a handler that had drifted to
    `get_paginator("get_tables")`, and the test would still pass — the same class
    of self-confirming oracle that `helpers.py` records under `LGR-28b4d75-5`.
    Here a wrong operation is a loud failure.
    """
    paginator = MagicMock()
    paginator.paginate.return_value = list(pages)

    def _get_paginator(operation):
        assert operation == "get_databases", (
            f"list_glue_databases asked for a {operation!r} paginator; this contract is pinned to "
            f"'get_databases' (R3.1)"
        )
        return paginator

    glue_mock.get_paginator.side_effect = _get_paginator
    return paginator


class TestGlueDatabasesResponseContract:
    """Validate GET /resources/glue/databases response shape."""

    def test_response_has_databases_and_count(self, glue_mock):
        _paginate_databases(
            glue_mock,
            {"DatabaseList": [{"Name": "my_database", "Description": "Test DB"}]},
        )
        status, body = _parse(list_glue_databases({}, None))
        assert status == 200
        assert "databases" in body
        assert "count" in body
        assert isinstance(body["databases"], list)
        # Asserted explicitly because the three passing cases in this class all
        # passed against an EMPTY body before the migration. A length check is
        # what separates "the keys are present" from "the keys carry the page".
        assert body["databases"], "the supplied page did not reach the response — the mock is not being read"
        assert body["count"] == 1

    def test_database_uses_name_field(self, glue_mock):
        """Backend currently returns 'name' — document this for contract awareness.

        The frontend normalizes both 'database_name' and 'name', but the
        canonical field per the TypeScript GlueDatabase interface should be
        'database_name'. This test documents the current backend behavior.
        """
        _paginate_databases(
            glue_mock,
            {"DatabaseList": [{"Name": "production_db", "Description": "Prod"}]},
        )
        _, body = _parse(list_glue_databases({}, None))
        db = body["databases"][0]
        # The backend currently uses 'name' — the frontend handles both
        assert "name" in db, "Backend should return 'name' field (frontend normalizes to database_name)"
        assert db["name"] == "production_db"

    def test_database_field_types(self, glue_mock):
        _paginate_databases(
            glue_mock,
            {
                "DatabaseList": [
                    {"Name": "test_db", "Description": "A test database", "LocationUri": "s3://bucket/path"},
                ]
            },
        )
        _, body = _parse(list_glue_databases({}, None))
        db = body["databases"][0]
        assert isinstance(db["name"], str)
        assert isinstance(db.get("description", ""), str)

    def test_empty_databases(self, glue_mock):
        """A genuinely empty account, asserted through a page that says so.

        Before the migration this passed for the wrong reason: the body was empty
        because the mock served no pages at all, not because the one page it
        served was empty. The distinction matters — it is the difference between
        this case and a broken enumeration.

        Uniquely in this class, the output alone cannot tell those two apart —
        `{"databases": [], "count": 0}` is what an empty account and a dead
        enumeration both produce, so this is the one case that stays green under
        a mock serving nothing. It therefore asserts the **mechanism** as well:
        the handler reached the paginator, and did not fall back to the single
        `get_databases()` call R3.1 replaced. Without those two lines this test
        is the last remaining way for the file to pass while asserting nothing.
        """
        paginator = _paginate_databases(glue_mock, {"DatabaseList": []})
        status, body = _parse(list_glue_databases({}, None))
        assert status == 200
        assert body["databases"] == []
        assert body["count"] == 0
        glue_mock.get_paginator.assert_called_once_with("get_databases")
        paginator.paginate.assert_called_once_with()
        glue_mock.get_databases.assert_not_called()

    def test_count_matches_array_length(self, glue_mock):
        _paginate_databases(
            glue_mock,
            {"DatabaseList": [{"Name": "db1"}, {"Name": "db2"}, {"Name": "db3"}]},
        )
        _, body = _parse(list_glue_databases({}, None))
        assert body["count"] == len(body["databases"])
        # `0 == 0` satisfied this assertion before the migration. Pinning the
        # value is what makes it evidence rather than a tautology.
        assert body["count"] == 3

    def test_databases_span_multiple_pages(self, glue_mock):
        """R3.1 — every page is enumerated, in order, and `count` covers all of them.

        This is the case that makes the file evidence that R3.1 landed rather
        than a suite that tolerates it: a handler reading only the first page
        returns 2 of 5 databases and fails here, while every other test in this
        class still passes on its single page. The middle page is deliberately
        empty and the last one omits `DatabaseList` entirely — both are shapes a
        paginator can hand back, and `_paginated_items` coalesces them to `[]`
        rather than raising.
        """
        _paginate_databases(
            glue_mock,
            {"DatabaseList": [{"Name": "page1_db1"}, {"Name": "page1_db2"}]},
            {"DatabaseList": []},
            {"DatabaseList": [{"Name": "page3_db1"}, {"Name": "page3_db2"}, {"Name": "page3_db3"}]},
            {},
        )
        status, body = _parse(list_glue_databases({}, None))
        assert status == 200
        assert [db["name"] for db in body["databases"]] == [
            "page1_db1",
            "page1_db2",
            "page3_db1",
            "page3_db2",
            "page3_db3",
        ], "the response is not the in-order concatenation of every page"
        assert body["count"] == 5
        # The single, unpaginated `get_databases()` call R3.1 replaced. Asserted
        # negatively because a handler that called BOTH would satisfy every
        # assertion above while still capping at one page in production.
        glue_mock.get_databases.assert_not_called()
