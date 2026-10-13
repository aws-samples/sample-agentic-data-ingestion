# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Contract test: GET /resources/glue/databases/{database_name}/tables.

p66 task 2.3 — first coverage for `list_glue_tables` (R3.2, R3.5, R5.1, R5.2)
---------------------------------------------------------------------------
`list_glue_tables` had **no covering test** before this file (`design.md` A2,
re-confirmed by codegraph, which still reports 1 caller in `router.py` and no
covering tests). R5.1 therefore applies, and R5.2 decides where the expectations
come from: **the requirements, not the handler's output.** A test written from a
producer's output cannot detect that producer drifting, because the output is its
own oracle (`LGR-cd58706-3`, `LGR-28b4d75-5`).

So each assertion below names its source, and none of them was read off a live
response:

* **Top-level `tables` array and `count` key** — R3.5, which pins both names and
  types so "the existing contract test and the two TypeScript response
  interfaces (`types.ts:1852-1868`) stay valid". `GlueTablesResponse` declares
  `{ tables?: RawTableInfo[]; count?: number }`.
* **`database_name` echoed at the top level** — `design.md` §4.1, "Change nothing
  else", which enumerates what `list_glue_tables` keeps across the pagination
  change.
* **Per-item field names** — the same §4.1 clause: "the projected field names,
  the `count` key, the `_normalize_aws_datetime` / `.isoformat()` treatment of
  `CreateTime`, the `except Exception` → 500 — is untouched". `RawTableInfo`
  (`types.ts:1837-1841`) independently declares `name` for this endpoint.
* **Every page enumerated, in order** — R3.2, through Paginated_Items (R3.3).

Two findings recorded rather than asserted
---------------------------------------------------------------------------
1. **There is no `GLUE_TABLES_RESPONSE_SCHEMA`.** `schemas.py` defines
   `GLUE_DATABASE_SCHEMA` / `GLUE_DATABASES_RESPONSE_SCHEMA` (`:239-262`) for the
   sibling endpoint and nothing for this one, so — unlike task 2.2, which found a
   live disagreement between that schema's `required: ["database_name"]` and the
   handler's `name` — there is no second declaration here to agree or disagree
   with. Adding one is outside this task: `.kiro/steering/contract-tests.md`
   scopes `schemas.py` edits to adding or renaming a response field, and p66
   renames nothing (R3.5, C11).

2. **`RawTableInfo` declares `database`; this handler emits `databaseName`.**
   `mapTable` (`use-resources.ts:70-75`) reads `t.database ?? ''`, so the
   database name reaches the UI as `''` for every Glue table. That is the same
   class as the databases-side `name` / `database_name` mismatch, it is latent
   rather than observed (no consumer reads `TableInfo.database` for this path),
   and R3.5 plus C11 forbid reshaping the response to close it. **This file
   asserts `databaseName`, the name §4.1 pins**, and does not assert the
   frontend's declared spelling — asserting `database` would fail today and
   asserting the mismatch away would hide it.

Mock shape
---------------------------------------------------------------------------
Paginating from the start, which is the shape task 2.2 migrated the sibling file
to. `get_paginator` is wired with a `side_effect` that **rejects any operation
name other than `get_tables`**: a blanket `return_value` would serve these pages
to a handler that had drifted to the wrong paginator and still pass, which is the
self-confirming-oracle class recorded under `LGR-28b4d75-5`.
"""

import json
import os
import sys
from datetime import datetime
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
# `AUDIT_TABLE` and `AGENT_TRACES_TABLE` are enumerated for the reason
# `test_metrics_trend_seam.py:143-148` writes down and the sibling
# `test_glue_databases_response_contract.py` repeats: `common.py:42-43` resolves
# both with `require_env` at **module** scope, and this file reaches that module
# through `handlers.resources`. Without them the file is only collectable when
# some other module in the layer happens to be imported first and sets them —
# i.e. it would pass in a directory run and fail standalone. `setdefault` is the
# per-module idiom this layer uses (`LGR-3e45e65-1`); `tests/contracts/conftest.py`
# also sets `AUDIT_TABLE`, which makes that one a no-op here rather than a
# dependency.
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
os.environ.setdefault("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from handlers import resources as _resources
from handlers.resources import list_glue_tables

DATABASE = "agentic_data_ingestion_kjljlkj_ba061ab9"


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


def _event(database_name=DATABASE):
    """A path-parameter event. `None` omits the key, exercising the 400 guard."""
    return {"pathParameters": {} if database_name is None else {"database_name": database_name}}


@pytest.fixture
def glue_mock(monkeypatch):
    """Substitute the module-level `glue_client` with a paginating mock (R3.5).

    `monkeypatch.setattr` on the module attribute is the mechanism R3.5 names,
    and it keeps working because `list_glue_tables` passes `glue_client` to
    `_paginated_items` as an argument rather than closing over it — the handle is
    read out of this module's namespace at call time.
    """
    mock_glue = MagicMock()
    monkeypatch.setattr(_resources, "glue_client", mock_glue)
    return mock_glue


def _paginate_tables(glue_mock, *pages):
    """Serve `*pages` from `get_paginator("get_tables").paginate(...)`.

    Each page is a `TableList` payload exactly as Glue returns it, so the handler
    is exercised through the real call shape rather than the single-call shape
    R3.2 replaced.

    The operation name is asserted inside the `side_effect` rather than trusted:
    a blanket `return_value` would make this helper serve `get_tables` pages to a
    `get_databases` paginator and report success (`LGR-28b4d75-5`).
    """
    paginator = MagicMock()
    paginator.paginate.return_value = list(pages)

    def _get_paginator(operation):
        assert operation == "get_tables", (
            f"list_glue_tables asked for a {operation!r} paginator; this contract is pinned to 'get_tables' (R3.2)"
        )
        return paginator

    glue_mock.get_paginator.side_effect = _get_paginator
    return paginator


class TestGlueTablesResponseContract:
    """Validate GET /resources/glue/databases/{database_name}/tables response shape."""

    def test_response_has_tables_and_count(self, glue_mock):
        """R3.5 — `tables` is an array, `count` is a number, and both are named that.

        `database_name` is asserted alongside them because `design.md` §4.1 keeps
        it: the response is `{database_name, tables, count}`, and R3.5 constrains
        the latter two by name and type.
        """
        _paginate_tables(glue_mock, {"TableList": [{"Name": "customers", "DatabaseName": DATABASE}]})
        status, body = _parse(list_glue_tables(_event(), None))
        assert status == 200
        assert set(body) == {"database_name", "tables", "count"}, (
            "the top-level key set changed; R3.5 pins `tables` and `count`, and §4.1 keeps `database_name`"
        )
        assert body["database_name"] == DATABASE
        assert isinstance(body["tables"], list)
        assert isinstance(body["count"], int)
        # Asserted explicitly, because every shape assertion above is also
        # satisfied by `{"tables": [], "count": 0}` — the body a mock serving no
        # pages produces. This is the line that separates "the keys are present"
        # from "the keys carry the page".
        assert body["tables"], "the supplied page did not reach the response — the mock is not being read"
        assert body["count"] == 1

    def test_table_uses_name_field(self, glue_mock):
        """The per-item key is `name` (`design.md` §4.1; `RawTableInfo.name`).

        Not `table_name`: `RawTableInfo` declares both and `mapTable`
        (`use-resources.ts:70-75`) normalises `t.table_name ?? t.name`, so the
        frontend accepts either while this endpoint emits `name`. Pinned so a
        rename on either side becomes a failure rather than an empty dropdown.
        """
        _paginate_tables(glue_mock, {"TableList": [{"Name": "orders", "DatabaseName": DATABASE}]})
        _, body = _parse(list_glue_tables(_event(), None))
        table = body["tables"][0]
        assert "name" in table
        assert table["name"] == "orders"

    def test_table_projected_field_names(self, glue_mock):
        """`design.md` §4.1 — the projected field names are untouched by R3.2.

        The full per-item key set, asserted as a set so that both a removal and
        an unrequested addition fail. `databaseName` is the spelling §4.1 pins;
        see this module's finding 2 on `RawTableInfo.database`.
        """
        _paginate_tables(
            glue_mock,
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "DatabaseName": DATABASE,
                        "Description": "Order facts",
                        "StorageDescriptor": {
                            "Location": "s3://bucket/orders/",
                            "InputFormat": "org.apache.hadoop.mapred.TextInputFormat",
                            "OutputFormat": "org.apache.hadoop.hive.ql.io.HiveIgnoreKeyTextOutputFormat",
                        },
                    }
                ]
            },
        )
        _, body = _parse(list_glue_tables(_event(), None))
        table = body["tables"][0]
        assert set(table) == {
            "name",
            "databaseName",
            "description",
            "storageDescriptor",
            "createTime",
            "updateTime",
        }
        assert set(table["storageDescriptor"]) == {"location", "inputFormat", "outputFormat"}
        assert table["databaseName"] == DATABASE
        assert table["description"] == "Order facts"
        assert table["storageDescriptor"]["location"] == "s3://bucket/orders/"

    def test_table_field_types(self, glue_mock):
        """Types for the projected fields, including the `.isoformat()` treatment.

        `design.md` §4.1 keeps `CreateTime`'s `.isoformat()` handling, so a
        datetime becomes a string and an absent value becomes `null` rather than
        `""`. Both arms are asserted: `CreateTime` present, `UpdateTime` absent.
        """
        _paginate_tables(
            glue_mock,
            {
                "TableList": [
                    {
                        "Name": "orders",
                        "DatabaseName": DATABASE,
                        "CreateTime": datetime(2026, 9, 17, 12, 0, 0),
                    }
                ]
            },
        )
        _, body = _parse(list_glue_tables(_event(), None))
        table = body["tables"][0]
        assert isinstance(table["name"], str)
        assert isinstance(table["databaseName"], str)
        assert isinstance(table["description"], str)
        assert isinstance(table["storageDescriptor"], dict)
        assert table["createTime"] == "2026-09-17T12:00:00"
        assert table["updateTime"] is None, "an absent timestamp is null, not an empty string"
        # Absent optional fields still default to a string, not to null.
        assert table["description"] == ""
        assert table["storageDescriptor"]["location"] == ""

    def test_empty_database(self, glue_mock):
        """A genuinely empty database, asserted through a page that says so.

        This is the one case in the class whose **output** an empty account and a
        dead enumeration both produce — `{"tables": [], "count": 0}` — so it is
        the one case that would stay green under a mock serving nothing at all.
        It therefore asserts the mechanism as well: the handler reached the
        `get_tables` paginator with the database as a kwarg, and did not fall
        back to the single `get_tables(...)` call R3.2 replaced.

        This is also the state behind the reported symptom (R2): six of the nine
        prefixed databases in the measured account return exactly this body.
        """
        paginator = _paginate_tables(glue_mock, {"TableList": []})
        status, body = _parse(list_glue_tables(_event(), None))
        assert status == 200
        assert body["tables"] == []
        assert body["count"] == 0
        assert body["database_name"] == DATABASE
        glue_mock.get_paginator.assert_called_once_with("get_tables")
        paginator.paginate.assert_called_once_with(DatabaseName=DATABASE)
        glue_mock.get_tables.assert_not_called()

    def test_count_matches_array_length(self, glue_mock):
        _paginate_tables(
            glue_mock,
            {"TableList": [{"Name": "t1"}, {"Name": "t2"}, {"Name": "t3"}]},
        )
        _, body = _parse(list_glue_tables(_event(), None))
        assert body["count"] == len(body["tables"])
        # Pinned to the value as well, because `0 == 0` satisfies the identity
        # above against an empty body.
        assert body["count"] == 3

    def test_tables_span_multiple_pages(self, glue_mock):
        """R3.2 — every page is enumerated, in order, and `count` covers all of them.

        The case that makes this file evidence that R3.2 landed rather than a
        suite that tolerates it: a handler reading only the first page returns 2
        of 5 tables and fails here, while every other test in this class still
        passes on its single page.

        The middle page is deliberately **empty** and the last one **omits
        `TableList` entirely** — both are shapes a paginator can hand back, and
        `_paginated_items` (`schemas.py:359-360`) coalesces both to `[]` via
        `page.get(result_key, []) or []` rather than raising. Pinned here for
        `get_tables` as the sibling file pins them for `get_databases`.
        """
        _paginate_tables(
            glue_mock,
            {"TableList": [{"Name": "page1_t1"}, {"Name": "page1_t2"}]},
            {"TableList": []},
            {"TableList": [{"Name": "page3_t1"}, {"Name": "page3_t2"}, {"Name": "page3_t3"}]},
            {},
        )
        status, body = _parse(list_glue_tables(_event(), None))
        assert status == 200
        assert [t["name"] for t in body["tables"]] == [
            "page1_t1",
            "page1_t2",
            "page3_t1",
            "page3_t2",
            "page3_t3",
        ], "the response is not the in-order concatenation of every page"
        assert body["count"] == 5
        # Asserted negatively because a handler that called BOTH the paginator and
        # the single `get_tables()` would satisfy every assertion above while
        # still capping at one page in production.
        glue_mock.get_tables.assert_not_called()

    def test_database_name_reaches_the_paginator(self, glue_mock):
        """R3.2 — `DatabaseName` is forwarded through `_paginated_items`' `**kwargs`.

        Its own test because it is the one argument that can be dropped without
        changing the response *shape* at all: a handler that paginated
        `get_tables` with no `DatabaseName` would enumerate the wrong thing and
        satisfy every other assertion in this class.
        """
        other = "agentic_data_ingestion_jdbc_discovery_dev"
        paginator = _paginate_tables(glue_mock, {"TableList": [{"Name": "appdb_customers"}]})
        _, body = _parse(list_glue_tables(_event(other), None))
        paginator.paginate.assert_called_once_with(DatabaseName=other)
        assert body["database_name"] == other

    def test_missing_database_name_returns_400(self, glue_mock):
        """The guard refuses before enumerating anything.

        A missing path parameter is a 400 naming the field, and no Glue call is
        made — asserted so that a future refactor cannot turn the refusal into an
        enumeration of every table in the account.
        """
        status, body = _parse(list_glue_tables(_event(None), None))
        assert status == 400
        assert "database_name" in body["error"]
        glue_mock.get_paginator.assert_not_called()
        glue_mock.get_tables.assert_not_called()
