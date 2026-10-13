# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Unit tests for the discovery cleanup janitor in handlers/jdbc_resources.py.

Covers the daily TTL janitor added by Tasks 4.3 / 4.4 of the P17 JDBC
data source spec — specifically:

    * ``cleanup_discovery_resources`` — the public janitor entry point
    * ``_is_expired_discovery_crawler``  — the per-crawler TTL/state/tag
      gate used by the janitor
    * ``lambda_function.lambda_handler`` — the EventBridge dispatch
      branch that invokes the janitor for ``source == "aws.events"``
      events and returns its summary directly (no HTTP envelope).

Test scope (per Task 4.7):
    1. EventBridge event-shape detection — ``aws.events`` events route
       to the janitor; everything else routes through ``route_request``.
    2. Tag filtering — only crawlers tagged ``Type=agentic-data-ingestion-discovery``
       are cleaned up; runtime-tagged and untagged crawlers are skipped.
    3. 7-day TTL logic — ``_is_expired_discovery_crawler`` returns True
       only for tagged + idle + ≥ 7-day-old crawlers; defends against
       missing CreationTime and RUNNING/STOPPING state.
    4. Idempotency — re-running the janitor against a cleaned-up state
       (or with already-deleted resources) is a safe no-op.

Validates: Requirements 22.1
"""

from __future__ import annotations

import importlib.util
import os
import sys
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub Lambda-layer dependencies BEFORE importing the handler module.
# Mirrors test_jdbc_resources.py so the dashboard API handler imports
# cleanly in a unit-test process without the real Lambda layer present.
# ---------------------------------------------------------------------------

sys.modules["eventbridge_utils"] = MagicMock()
sys.modules["eventbridge_utils"].get_eventbridge_publishers = lambda: (
    MagicMock(),
    MagicMock(),
    MagicMock(),
)

mock_env_utils = MagicMock()
mock_env_utils.require_env = lambda name, default=None: default or name
mock_env_utils.get_aws_account_id = lambda: "123456789012"


@pytest.fixture(autouse=True)
def _env_utils_stub(monkeypatch):
    """Install the env_utils stub for each test, and only for the test.

    The handler modules are imported inside the tests, so the stub must be in
    sys.modules then. Installed at module scope it leaked into every module
    collected after this one (LGR-c58f276-4, LGR-62c4f77-1).
    """
    monkeypatch.setitem(sys.modules, "env_utils", mock_env_utils)


os.environ.setdefault("AWS_DEFAULT_REGION", "us-west-2")
os.environ.setdefault("AWS_REGION", "us-west-2")

# Make ``shared/`` importable so ``handlers.jdbc_resources`` can resolve
# its ``from jdbc_helpers import ...`` line. In a real Lambda the shared
# layer is mounted at ``/opt/python``; for unit tests we point sys.path
# at the in-tree shared directory.
sys.path.insert(
    0,
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..", "shared"),
)

# Add handler package to path
sys.path.insert(
    0,
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    ),
)


_DASHBOARD_API_DIR = os.path.abspath(
    os.path.join(
        os.path.dirname(__file__),
        "..",
        "..",
        "..",
        "..",
        "lambda",
        "agentic_data_ingestion_dashboard_api",
    )
)


def _load_dashboard_lambda_function():
    """Load the dashboard API's ``lambda_function`` by absolute path.

    Multiple Lambdas in this repo all expose a top-level
    ``lambda_function.py``. ``import lambda_function`` is therefore
    unstable across the full test suite — whichever Lambda's
    ``lambda_function`` was imported last wins ``sys.modules``. Loading
    via :func:`importlib.util.spec_from_file_location` pins us to the
    dashboard API's file regardless of cache contamination, matching
    the pattern in ``test_dual_path_routing.py`` and others.
    """
    sys.modules.pop("lambda_function", None)
    spec = importlib.util.spec_from_file_location(
        "lambda_function",
        os.path.join(_DASHBOARD_API_DIR, "lambda_function.py"),
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    sys.modules["lambda_function"] = module
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_glue_exceptions(mock_glue: MagicMock) -> None:
    """Attach the Glue exception types we use directly to a mock client.

    boto3 surfaces service exceptions as classes hanging off
    ``client.exceptions.*``; tests need real classes (not MagicMocks)
    so ``except`` clauses match. We synthesise the minimal set the
    janitor catches.
    """
    mock_glue.exceptions.EntityNotFoundException = type("EntityNotFoundException", (Exception,), {})
    mock_glue.exceptions.CrawlerRunningException = type("CrawlerRunningException", (Exception,), {})
    mock_glue.exceptions.AlreadyExistsException = type("AlreadyExistsException", (Exception,), {})


def _crawler(
    name: str,
    *,
    tag_value: str | None = "agentic-data-ingestion-discovery",
    state: str = "READY",
    age_days: float | None = 8.0,
) -> dict:
    """Build a minimal Glue crawler payload with the ``_tags`` annotation.

    Matches the augmented shape that ``cleanup_discovery_resources``
    produces internally before calling ``_is_expired_discovery_crawler``.
    Set ``tag_value=None`` to simulate an untagged crawler and
    ``age_days=None`` to omit ``CreationTime`` entirely.
    """
    now = datetime.now(UTC)
    payload: dict = {"Name": name, "State": state}
    if tag_value is None:
        payload["_tags"] = {}
    else:
        payload["_tags"] = {"Type": tag_value}
    if age_days is not None:
        payload["CreationTime"] = now - timedelta(days=age_days)
    return payload


def _paginator_pages(*pages: dict) -> MagicMock:
    """Return a paginator mock whose ``.paginate()`` yields the given pages."""
    paginator = MagicMock()
    paginator.paginate.return_value = list(pages)
    return paginator


# ---------------------------------------------------------------------------
# 1. EventBridge event-shape detection (lambda_function.lambda_handler)
# ---------------------------------------------------------------------------


class TestEventBridgeDispatch:
    """The Lambda entry point routes EventBridge events to the janitor."""

    def test_aws_events_source_routes_to_cleanup_and_returns_summary_directly(
        self,
    ):
        """``source == 'aws.events'`` → janitor invoked, summary returned raw."""
        event = {
            "source": "aws.events",
            "detail-type": "Scheduled Event",
            "resources": ["arn:aws:events:us-west-2:123456789012:rule/agentic-data-ingestion-discovery-cleanup-dev"],
            "detail": {},
        }
        expected_summary = {
            "crawlers_deleted": 3,
            "connections_deleted": 2,
            "tables_deleted": 5,
            "errors": [],
        }

        with patch(
            "handlers.jdbc_resources.cleanup_discovery_resources",
            return_value=expected_summary,
        ) as mock_cleanup:
            lambda_function = _load_dashboard_lambda_function()
            with patch.object(lambda_function, "route_request") as mock_route:
                response = lambda_function.lambda_handler(event, None)

        # Janitor was called exactly once; the API Gateway router was not.
        mock_cleanup.assert_called_once_with()
        mock_route.assert_not_called()

        # The summary is returned verbatim — no HTTP envelope.
        assert response == expected_summary
        assert "statusCode" not in response
        assert "body" not in response

    def test_api_gateway_event_routes_through_route_request(self):
        """A plain proxy event bypasses the janitor and uses the router."""
        event = {
            "httpMethod": "GET",
            "path": "/api/health",
            "headers": {},
            "body": None,
        }
        api_response = {
            "statusCode": 200,
            "headers": {"Content-Type": "application/json"},
            "body": '{"status":"ok"}',
        }

        lambda_function = _load_dashboard_lambda_function()
        with (
            patch(
                "handlers.jdbc_resources.cleanup_discovery_resources",
            ) as mock_cleanup,
            patch.object(lambda_function, "route_request", return_value=api_response) as mock_route,
        ):
            response = lambda_function.lambda_handler(event, None)

        mock_cleanup.assert_not_called()
        mock_route.assert_called_once_with(event)
        assert response == api_response

    def test_event_without_source_field_routes_through_router(self):
        """Defensive: events lacking ``source`` must not invoke the janitor."""
        event = {"httpMethod": "POST", "path": "/api/pipelines"}

        lambda_function = _load_dashboard_lambda_function()
        with (
            patch(
                "handlers.jdbc_resources.cleanup_discovery_resources",
            ) as mock_cleanup,
            patch.object(lambda_function, "route_request", return_value={"statusCode": 202}) as mock_route,
        ):
            lambda_function.lambda_handler(event, None)

        mock_cleanup.assert_not_called()
        mock_route.assert_called_once_with(event)


# ---------------------------------------------------------------------------
# 2. & 4. cleanup_discovery_resources — tag filtering + idempotency
# ---------------------------------------------------------------------------


class TestCleanupDiscoveryResources:
    """End-to-end behaviour of the janitor entry point."""

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_tag_filter_skips_runtime_and_untagged_crawlers(self, mock_glue, mock_build_arn):
        """Only ``Type=agentic-data-ingestion-discovery`` crawlers are deleted."""
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_build_arn.side_effect = lambda name: f"arn:aws:glue:us-west-2:123456789012:crawler/{name}"

        # Mix of three discovery crawlers with the AgenticDataIngestion prefix:
        # one with the discovery tag (eligible), one with a runtime
        # tag (must be skipped), and one untagged (must be skipped).
        # The runtime + untagged crawlers carry the agentic-data-ingestion-discover-*
        # name prefix to prove the TAG (not the name) is the gate.
        now = datetime.now(UTC)
        old = now - timedelta(days=10)

        crawlers_page = {
            "Crawlers": [
                {
                    "Name": "agentic-data-ingestion-discover-aaa111bbb222",
                    "State": "READY",
                    "CreationTime": old,
                },
                {
                    "Name": "agentic-data-ingestion-discover-runtimexyz12",
                    "State": "READY",
                    "CreationTime": old,
                },
                {
                    "Name": "agentic-data-ingestion-discover-notagxyz1234",
                    "State": "READY",
                    "CreationTime": old,
                },
                # And an unrelated crawler whose name doesn't even
                # match our prefix — must be skipped without a get_tags
                # call (the janitor uses the prefix as a cheap pre-filter).
                {
                    "Name": "user-owned-other-crawler",
                    "State": "READY",
                    "CreationTime": old,
                },
            ]
        }
        mock_glue.get_paginator.return_value = _paginator_pages(crawlers_page)

        # Tag responses keyed by ARN suffix — only the discovery one
        # gets the cleanup-eligible tag.
        def _get_tags(ResourceArn: str) -> dict:
            if ResourceArn.endswith("aaa111bbb222"):
                return {"Tags": {"Type": "agentic-data-ingestion-discovery"}}
            if ResourceArn.endswith("runtimexyz12"):
                return {"Tags": {"Type": "agentic-data-ingestion-runtime"}}
            if ResourceArn.endswith("notagxyz1234"):
                return {"Tags": {}}
            raise AssertionError(f"Unexpected get_tags lookup for {ResourceArn}")

        mock_glue.get_tags.side_effect = _get_tags
        # No tables behind the discovery crawler in this test — keeps
        # the assertion focused on tag filtering.
        mock_glue.get_tables.return_value = {"TableList": []}

        summary = jdbc_resources.cleanup_discovery_resources()

        # Only the tagged discovery crawler had any delete called against it.
        mock_glue.delete_crawler.assert_called_once_with(Name="agentic-data-ingestion-discover-aaa111bbb222")
        mock_glue.delete_connection.assert_called_once_with(
            ConnectionName="agentic-data-ingestion-discover-aaa111bbb222"
        )
        # The non-AgenticDataIngestion-prefixed crawler was never tag-checked.
        all_arns = [c.kwargs.get("ResourceArn") for c in mock_glue.get_tags.call_args_list]
        assert all("user-owned-other-crawler" not in arn for arn in all_arns if arn)

        assert summary["crawlers_deleted"] == 1
        assert summary["connections_deleted"] == 1
        assert summary["tables_deleted"] == 0
        assert summary["errors"] == []

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_second_run_against_clean_state_is_a_no_op(self, mock_glue, mock_build_arn):
        """Idempotency: running the janitor on an empty account does nothing."""
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_build_arn.side_effect = lambda name: f"arn:aws:glue:us-west-2:123456789012:crawler/{name}"
        # Empty page — no crawlers exist (or a prior run cleaned them all).
        mock_glue.get_paginator.return_value = _paginator_pages({"Crawlers": []})

        summary = jdbc_resources.cleanup_discovery_resources()

        assert summary == {
            "crawlers_deleted": 0,
            "connections_deleted": 0,
            "tables_deleted": 0,
            "errors": [],
        }
        mock_glue.delete_crawler.assert_not_called()
        mock_glue.delete_connection.assert_not_called()
        mock_glue.delete_table.assert_not_called()

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_already_deleted_resources_are_treated_as_success(self, mock_glue, mock_build_arn):
        """EntityNotFoundException on delete_crawler/delete_connection is OK."""
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_build_arn.side_effect = lambda name: f"arn:aws:glue:us-west-2:123456789012:crawler/{name}"

        old = datetime.now(UTC) - timedelta(days=10)
        mock_glue.get_paginator.return_value = _paginator_pages(
            {
                "Crawlers": [
                    {
                        "Name": "agentic-data-ingestion-discover-stale1234abcd",
                        "State": "READY",
                        "CreationTime": old,
                    }
                ]
            }
        )
        mock_glue.get_tags.return_value = {"Tags": {"Type": "agentic-data-ingestion-discovery"}}
        # Both deletes simulate "already gone" — a partial prior run.
        mock_glue.delete_crawler.side_effect = mock_glue.exceptions.EntityNotFoundException()
        mock_glue.delete_connection.side_effect = mock_glue.exceptions.EntityNotFoundException()
        mock_glue.get_tables.return_value = {"TableList": []}

        summary = jdbc_resources.cleanup_discovery_resources()

        # Both deletes were attempted, neither incremented the counter,
        # neither raised, neither added an error entry — that's the
        # idempotency contract.
        mock_glue.delete_crawler.assert_called_once()
        mock_glue.delete_connection.assert_called_once()
        assert summary["crawlers_deleted"] == 0
        assert summary["connections_deleted"] == 0
        assert summary["errors"] == []

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_running_discovery_crawler_is_not_deleted(self, mock_glue, mock_build_arn):
        """Even a tagged + expired crawler is left alone if currently running."""
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_build_arn.side_effect = lambda name: f"arn:aws:glue:us-west-2:123456789012:crawler/{name}"

        old = datetime.now(UTC) - timedelta(days=10)
        mock_glue.get_paginator.return_value = _paginator_pages(
            {
                "Crawlers": [
                    {
                        "Name": "agentic-data-ingestion-discover-running12345",
                        "State": "RUNNING",
                        "CreationTime": old,
                    }
                ]
            }
        )
        mock_glue.get_tags.return_value = {"Tags": {"Type": "agentic-data-ingestion-discovery"}}

        summary = jdbc_resources.cleanup_discovery_resources()

        mock_glue.delete_crawler.assert_not_called()
        mock_glue.delete_connection.assert_not_called()
        assert summary["crawlers_deleted"] == 0


# ---------------------------------------------------------------------------
# 3. _is_expired_discovery_crawler — direct unit tests of the TTL gate
# ---------------------------------------------------------------------------


class TestIsExpiredDiscoveryCrawler:
    """Direct tests of the per-crawler TTL/state/tag gate.

    All cases use ``now = 2026-01-15T00:00:00Z`` for determinism.
    """

    NOW = datetime(2026, 1, 15, tzinfo=UTC)

    def test_eight_day_old_discovery_crawler_is_expired(self):
        """8 days > 7-day TTL → eligible."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler("agentic-data-ingestion-discover-eight12345678", age_days=8.0)
        # Override CreationTime so the assertion is anchored to NOW.
        crawler["CreationTime"] = self.NOW - timedelta(days=8)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is True

    def test_one_day_old_discovery_crawler_is_fresh(self):
        """1 day < 7-day TTL → not eligible."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler("agentic-data-ingestion-discover-fresh1234abcd", age_days=1.0)
        crawler["CreationTime"] = self.NOW - timedelta(days=1)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False

    def test_exactly_seven_day_old_crawler_is_not_expired(self):
        """Boundary: ``> _DISCOVERY_TTL`` is strict inequality at 7 days."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler("agentic-data-ingestion-discover-boundary1234", age_days=7.0)
        crawler["CreationTime"] = self.NOW - timedelta(days=7)
        # exactly 7 days should NOT be expired (strict >, not >=)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False

    def test_missing_creation_time_returns_false(self):
        """Defensive: no ``CreationTime`` → cannot prove expiry → False."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler("agentic-data-ingestion-discover-noctimexyz12", age_days=None)
        assert "CreationTime" not in crawler
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False

    def test_running_state_blocks_deletion_even_when_old(self):
        """RUNNING crawler is skipped regardless of age."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler(
            "agentic-data-ingestion-discover-runninglate",
            state="RUNNING",
            age_days=30.0,
        )
        crawler["CreationTime"] = self.NOW - timedelta(days=30)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False

    def test_stopping_state_blocks_deletion_even_when_old(self):
        """STOPPING (transient) crawler is skipped regardless of age."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler(
            "agentic-data-ingestion-discover-stopping1234",
            state="STOPPING",
            age_days=30.0,
        )
        crawler["CreationTime"] = self.NOW - timedelta(days=30)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False

    def test_runtime_tagged_crawler_is_not_eligible(self):
        """``Type=agentic-data-ingestion-runtime`` is explicitly NOT expired here."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler(
            "agentic-data-ingestion-discover-runtime12345",
            tag_value="agentic-data-ingestion-runtime",
            age_days=30.0,
        )
        crawler["CreationTime"] = self.NOW - timedelta(days=30)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False

    def test_untagged_crawler_is_not_eligible(self):
        """No ``Type`` tag → not eligible, even if very old."""
        from handlers.jdbc_resources import _is_expired_discovery_crawler

        crawler = _crawler(
            "agentic-data-ingestion-discover-untagged1234",
            tag_value=None,
            age_days=30.0,
        )
        crawler["CreationTime"] = self.NOW - timedelta(days=30)
        assert _is_expired_discovery_crawler(crawler, self.NOW) is False


# ---------------------------------------------------------------------------
# Surface tests — ensure the discovered TTL constant matches the spec
# ---------------------------------------------------------------------------


class TestModuleConstants:
    """Catch accidental drift of the documented 7-day TTL contract."""

    def test_discovery_ttl_is_seven_days(self):
        """Requirement 4.11: discovery resources expire after 7 days."""
        from handlers.jdbc_resources import _DISCOVERY_TTL

        assert timedelta(days=7) == _DISCOVERY_TTL

    def test_discovery_tag_contract(self):
        """The janitor and the start endpoint must agree on tag key/value."""
        from handlers.jdbc_resources import (
            _DISCOVERY_TAG_KEY,
            _DISCOVERY_TAG_VALUE,
        )

        assert _DISCOVERY_TAG_KEY == "Type"
        assert _DISCOVERY_TAG_VALUE == "agentic-data-ingestion-discovery"


# ---------------------------------------------------------------------------
# 5. The connection-keyed orphan sweep. The crawler pass deletes a discovery
#    connection only with its crawler, so one whose crawler is already gone was
#    never revisited. Expectations follow the sweep's three conditions: exact
#    discovery name, no crawler of that name, and both timestamps past the TTL.
# ---------------------------------------------------------------------------


def _connection(name: str, *, created_days_ago: float | None, updated_days_ago: float | None) -> dict:
    now = datetime.now(UTC)
    conn: dict = {"Name": name, "ConnectionType": "JDBC"}
    if created_days_ago is not None:
        conn["CreationTime"] = now - timedelta(days=created_days_ago)
    if updated_days_ago is not None:
        conn["LastUpdatedTime"] = now - timedelta(days=updated_days_ago)
    return conn


def _paginators(crawlers: list[dict], connections: list[dict]):
    """``get_paginator`` side effect: one pager per operation."""
    pagers = {
        "get_crawlers": _paginator_pages({"Crawlers": crawlers}),
        "get_connections": _paginator_pages({"ConnectionList": connections}),
    }
    return lambda operation: pagers[operation]


class TestOrphanedDiscoveryConnectionSweep:
    ORPHAN = "agentic-data-ingestion-discover-0123456789ab"

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_an_old_connection_with_no_crawler_is_deleted(self, mock_glue, mock_build_arn):
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_glue.get_paginator.side_effect = _paginators(
            [], [_connection(self.ORPHAN, created_days_ago=20, updated_days_ago=9)]
        )

        summary = jdbc_resources.cleanup_discovery_resources()

        mock_glue.delete_connection.assert_called_once_with(ConnectionName=self.ORPHAN)
        mock_glue.delete_crawler.assert_not_called()
        assert summary["connections_deleted"] == 1
        assert summary["errors"] == []

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_the_listing_reads_jdbc_connections_with_passwords_hidden(self, mock_glue, mock_build_arn):
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        pagers = {
            "get_crawlers": _paginator_pages({"Crawlers": []}),
            "get_connections": _paginator_pages({"ConnectionList": []}),
        }
        mock_glue.get_paginator.side_effect = lambda operation: pagers[operation]

        jdbc_resources.cleanup_discovery_resources()

        pagers["get_connections"].paginate.assert_called_once_with(Filter={"ConnectionType": "JDBC"}, HidePassword=True)

    @pytest.mark.parametrize(
        ("created", "updated"),
        [
            (20, 1),  # re-detected yesterday: a detection may be in flight
            (3, None),  # created three days ago, never updated
            (None, None),  # no timestamp at all: cannot prove it is old
        ],
    )
    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_a_connection_not_past_the_ttl_on_both_timestamps_is_kept(
        self, mock_glue, mock_build_arn, created, updated
    ):
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_glue.get_paginator.side_effect = _paginators(
            [], [_connection(self.ORPHAN, created_days_ago=created, updated_days_ago=updated)]
        )

        summary = jdbc_resources.cleanup_discovery_resources()

        mock_glue.delete_connection.assert_not_called()
        assert summary["connections_deleted"] == 0

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_a_connection_whose_crawler_exists_is_left_to_the_crawler_pass(self, mock_glue, mock_build_arn):
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_build_arn.side_effect = lambda name: f"arn:aws:glue:us-west-2:123456789012:crawler/{name}"
        # The crawler is fresh, so the crawler pass keeps the pair; the sweep
        # must not delete the connection out from under it.
        fresh_crawler = {"Name": self.ORPHAN, "State": "READY", "CreationTime": datetime.now(UTC)}
        mock_glue.get_tags.return_value = {"Tags": {"Type": "agentic-data-ingestion-discovery"}}
        mock_glue.get_paginator.side_effect = _paginators(
            [fresh_crawler], [_connection(self.ORPHAN, created_days_ago=20, updated_days_ago=20)]
        )

        jdbc_resources.cleanup_discovery_resources()

        mock_glue.delete_connection.assert_not_called()

    @pytest.mark.parametrize(
        "name",
        [
            "warehouse-conn",
            "agentic-data-ingestion-mylatesttest-773284d9-source",
            "agentic-data-ingestion-mynew233-503c3151-rds",
            "agentic-data-ingestion-test-fixtures-mysql-source",
            "agentic-data-ingestion-discover-0123456789",  # 10 hex, not 12
            "agentic-data-ingestion-discover-0123456789AB",  # uppercase, never minted
        ],
    )
    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_only_exact_discovery_names_are_swept(self, mock_glue, mock_build_arn, name):
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        mock_glue.get_paginator.side_effect = _paginators(
            [], [_connection(name, created_days_ago=60, updated_days_ago=60)]
        )

        jdbc_resources.cleanup_discovery_resources()

        mock_glue.delete_connection.assert_not_called()

    @patch("handlers.jdbc_resources._build_crawler_arn")
    @patch("handlers.jdbc_resources.glue_client")
    def test_a_failed_connection_listing_is_reported_and_does_not_raise(self, mock_glue, mock_build_arn):
        from botocore.exceptions import ClientError
        from handlers import jdbc_resources

        _make_glue_exceptions(mock_glue)
        failing = MagicMock()
        failing.paginate.side_effect = ClientError(
            {"Error": {"Code": "AccessDeniedException", "Message": "denied"}}, "GetConnections"
        )
        pagers = {"get_crawlers": _paginator_pages({"Crawlers": []}), "get_connections": failing}
        mock_glue.get_paginator.side_effect = lambda operation: pagers[operation]

        summary = jdbc_resources.cleanup_discovery_resources()

        assert [e.get("phase") for e in summary["errors"]] == ["list_connections"]
        mock_glue.delete_connection.assert_not_called()


if __name__ == "__main__":  # pragma: no cover
    pytest.main([__file__, "-v"])
