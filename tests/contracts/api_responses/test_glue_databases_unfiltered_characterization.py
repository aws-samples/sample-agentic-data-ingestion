# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""R4.2 — `GET /resources/glue/databases` serves every database, unfiltered.

p66 task 2.5 — the assertion that keeps O1's decision honest
---------------------------------------------------------------------------
O1 decided the eligibility rule of R1.1 is applied **client-side**, in
`dashboard-react/src/utils/glue-target-eligibility.ts`, and not in this endpoint.
R4.2 is the criterion that makes that decision checkable from the server side:
where the rule is *not* applied server-side, the endpoint keeps serving every
consumer unfiltered.

So this file pins the negative: a database whose name does **not** begin with
`agentic_data_ingestion_` is still returned, and the name is returned exactly as
Glue spelled it. Both are true today by omission — nothing filters and nothing
rewrites — which is precisely why they need an assertion. A later change that
moved the filter into the handler to "save shipping names the browser discards"
would break no other test in this suite.

Where the expectations come from
---------------------------------------------------------------------------
**Not from the handler's output.** R5.2 forbids that, and `LGR-cd58706-3` /
`LGR-28b4d75-5` give the reason: a test written from a producer's output cannot
detect that producer drifting, because the output is its own oracle. Here the
consequence is concrete — reading the expected database list off a response would
make this file pass against a *filtering* handler, since the filtered list would
appear on both sides of the comparison.

Each expectation is therefore derived:

* **Non-prefixed names are returned** — R4.2's second arm, read together with O1.
  The measured account in `requirements.md` has 93 databases of which 84 lack the
  prefix; the whole point of the client-side arm is that all 93 cross the wire.
* **The name is returned verbatim** — R3.5, which pins the array and `count` by
  name and type across the pagination change, and `design.md` §4.1's "change
  nothing else", which enumerates the projection as untouched. Neither permits
  the handler to rewrite a value, and stripping or normalising the prefix would
  be a rewrite.

What this file deliberately does not assert
---------------------------------------------------------------------------
**The `name` versus `database_name` spelling.** `GLUE_DATABASES_RESPONSE_SCHEMA`
(`tests/contracts/api_responses/schemas.py:239-262`) requires `database_name` per
item while the handler emits `name`, so wiring that schema up here would fail —
a live disagreement task 2.2 found and left alone as out of scope (R3.5, C11).
R4.2 is about the *prefix*, not the spelling, and the two are independent: the
endpoint could be unfiltered under either key. Recorded so the next reader does
not conflate them, and `test_database_uses_name_field` in the sibling file
already pins `name` itself.

Mock shape
---------------------------------------------------------------------------
Paginating, with `get_paginator` wired through a `side_effect` that rejects any
operation other than `get_databases`. A blanket `return_value` would serve these
pages to a handler that had drifted to the wrong paginator and still report
success (`LGR-28b4d75-5`).
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
# `AUDIT_TABLE` and `AGENT_TRACES_TABLE` are enumerated for the reason the two
# sibling Glue contract files write down: `handlers/common.py:42-43` resolves both
# with `require_env` at **module** scope, and this file reaches that module through
# `handlers.resources`. Without them the file is only collectable when some other
# module in the layer happens to be imported first and sets them — i.e. it would
# pass in a directory run and fail standalone. `setdefault` is the per-module
# idiom of this layer (`LGR-3e45e65-1`); `tests/contracts/conftest.py` also sets
# `AUDIT_TABLE`, which makes that one a no-op here rather than a dependency.
os.environ.setdefault("AUDIT_TABLE", "agentic_data_ingestion_audit_log")
os.environ.setdefault("AGENT_TRACES_TABLE", "agentic_data_ingestion_agent_traces")
os.environ.setdefault("AWS_REGION", "us-east-1")
os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AGENTIC_DATA_INGESTION_ENV", "test")

sys.modules.pop("lambda_function", None)

from handlers import resources as _resources
from handlers.resources import list_glue_databases

#: The prefix the Glue Catalog target type requires, spelled here as the *test's*
#: own expectation rather than imported from the handler. `_GLUE_DATABASE_PREFIX`
#: exists in `handlers/resources.py`, but it belongs to Detect_Schema_Preflight
#: (`resources.py:290-709`), which is a different check on a different endpoint;
#: importing it would tie this characterization to a constant it is not about.
#: The prefix/reason literals *are* pinned across the language boundary — by
#: parsing both sides — in `tests/contracts/test_glue_target_prefix_cross_language.py`
#: (task 1.4). That is the drift guard; this is a filtering guard.
TARGET_PREFIX = "agentic_data_ingestion_"

#: A mix modelled on the shape of the measured account in `requirements.md`:
#: pre-rename `agentglue_*` names, an unrelated analytics database, and prefixed
#: ones — interleaved, so an order-sensitive assertion is also an unfiltered one.
MIXED_DATABASES = [
    "agentglue_verify172_s3tables_73f77e82",
    "agentic_data_ingestion_jdbc_discovery_dev",
    "default",
    "agentic_data_ingestion_kjljlkj_ba061ab9",
    "agentglue_dfdfddffd_444b0651",
]


def _parse(response):
    return response["statusCode"], json.loads(response["body"])


@pytest.fixture
def glue_mock(monkeypatch):
    """Substitute the module-level `glue_client` with a paginating mock (R3.5).

    `monkeypatch.setattr` on the module attribute is the mechanism R3.5 names,
    and it keeps working because `list_glue_databases` passes `glue_client` to
    `_paginated_items` as an argument rather than closing over it, so the handle
    is read out of this module's namespace at call time.
    """
    mock_glue = MagicMock()
    monkeypatch.setattr(_resources, "glue_client", mock_glue)
    return mock_glue


def _paginate_databases(glue_mock, *pages):
    """Serve `*pages` from `get_paginator("get_databases").paginate(...)`.

    The operation name is asserted inside the `side_effect` rather than trusted:
    a blanket `return_value` would serve these pages to a `get_tables` paginator
    and report success (`LGR-28b4d75-5`).
    """
    paginator = MagicMock()
    paginator.paginate.return_value = list(pages)

    def _get_paginator(operation):
        assert operation == "get_databases", (
            f"list_glue_databases asked for a {operation!r} paginator; this characterization is "
            "pinned to 'get_databases' (R3.1)"
        )
        return paginator

    glue_mock.get_paginator.side_effect = _get_paginator
    return paginator


class TestGlueDatabasesEndpointIsUnfiltered:
    """R4.2 — the filter is client-side (O1), and this is what keeps it there."""

    def test_databases_without_the_target_prefix_are_still_returned(self, glue_mock):
        """Every database Glue reports reaches the response, prefix or not.

        The expected list is `MIXED_DATABASES` — the plan this test *wrote*, not
        the handler's output — so a handler that filtered would produce a
        shorter array against a fixed expectation and fail loudly. Order is
        asserted alongside membership because the names are interleaved: a
        filter-then-reassemble implementation would satisfy a set comparison
        while reordering, and R3.5's array is a list.

        Three of the five names here lack the prefix, which is the case R4.2
        exists for: in the measured account 84 of 93 databases lack it, and the
        client-side arm of O1 requires all of them to cross the wire so
        `partitionGlueTargetDatabases` can tell "none eligible" from "none at
        all" (R1.5).

        **Validates: Requirement 4.2**
        """
        _paginate_databases(glue_mock, {"DatabaseList": [{"Name": name} for name in MIXED_DATABASES]})

        status, body = _parse(list_glue_databases({}, None))

        assert status == 200
        assert [database["name"] for database in body["databases"]] == MIXED_DATABASES, (
            "`GET /resources/glue/databases` no longer returns every database Glue reported. "
            "R4.2 keeps this endpoint unfiltered because O1 put the eligibility rule on the client "
            "(`utils/glue-target-eligibility.ts`); filtering here would also break R1.5, which needs "
            "the unfiltered total to distinguish 'databases exist but none is eligible' from 'this "
            "account has no Glue databases'."
        )
        assert body["count"] == len(MIXED_DATABASES), (
            "`count` no longer matches the unfiltered array — R3.5 pins it to the array's length."
        )

        non_prefixed = [name for name in MIXED_DATABASES if not name.startswith(TARGET_PREFIX)]
        returned = {database["name"] for database in body["databases"]}
        assert set(non_prefixed) <= returned, (
            f"{sorted(set(non_prefixed) - returned)} were dropped for lacking the "
            f"{TARGET_PREFIX!r} prefix. That is the Glue Catalog *target type's* rule, applied on "
            "the client by decision O1 — this endpoint's name promises every Glue database in the "
            "account, and it is reached by consumers other than the target picker."
        )

    def test_the_name_is_returned_verbatim(self, glue_mock):
        """The handler echoes Glue's `Name`; it does not strip or rewrite the prefix.

        The second half of "returns a name without the prefix": not only is a
        non-prefixed database kept, a prefixed one keeps its prefix. A handler
        that returned `kjljlkj_ba061ab9` for
        `agentic_data_ingestion_kjljlkj_ba061ab9` — plausible as a display
        convenience — would break the client-side predicate outright, since
        `isEligibleGlueTargetDatabase` tests for exactly that prefix, and would
        also break the wire value the wizard stores in
        `targetConfig.database_name`.

        Derived from R3.5 and `design.md` §4.1's "change nothing else", which
        keep the projection as it stands; neither licenses rewriting a value.

        **Validates: Requirement 4.2**
        """
        prefixed = f"{TARGET_PREFIX}kjljlkj_ba061ab9"
        _paginate_databases(glue_mock, {"DatabaseList": [{"Name": prefixed}]})

        _, body = _parse(list_glue_databases({}, None))

        assert [database["name"] for database in body["databases"]] == [prefixed], (
            f"the endpoint rewrote {prefixed!r}. The name is a wire value the wizard stores as "
            "`targetConfig.database_name` and the client tests the prefix on "
            "(`isEligibleGlueTargetDatabase`); stripping or normalising it breaks both."
        )

    def test_unfiltered_across_a_page_boundary(self, glue_mock):
        """The two facts compose: pagination does not become a filter.

        R3.1 made this endpoint walk every page. A filter applied per page, or a
        page-boundary bug that dropped the tail, would look identical to a
        prefix filter from the outside — both return a subset. Asserting the
        concatenation of a mixed two-page plan is what separates them, and it is
        cheap enough to be worth having alongside the single-page case.

        **Validates: Requirements 3.1, 4.2**
        """
        first, second = MIXED_DATABASES[:2], MIXED_DATABASES[2:]
        _paginate_databases(
            glue_mock,
            {"DatabaseList": [{"Name": name} for name in first]},
            {"DatabaseList": [{"Name": name} for name in second]},
        )

        _, body = _parse(list_glue_databases({}, None))

        assert [database["name"] for database in body["databases"]] == first + second, (
            "the paginated response is not the in-order concatenation of both pages' databases, "
            "unfiltered (R3.1, R4.2)."
        )
