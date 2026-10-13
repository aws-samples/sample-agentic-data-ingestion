# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""p53 task 5.1 — the Sample_Artifact flow, end to end, over moto.

**Validates: Requirements 1.3, 2.4**

WHAT THIS PROVES, AND WHY IT NEEDED ITS OWN TASK
------------------------------------------------
Tasks 2.1, 2.2, 3.1 and 3.2 each verified one link. Every one of them stubbed the
link on either side of it, so nothing yet asserted that the links join up:

* 2.1 asserted the emitted template text; 2.2 executed the emitted block against a
  recording fake and a byte-faithful stream, never against a bucket.
* 3.1 tested ``_probe_sample_artifact_uri`` against a ``MagicMock`` ``s3_client``,
  so nothing proved the key it HEADs is the key the template PUTs.
* 3.2 tested the callers' URI preference against hand-built DynamoDB items, so
  nothing proved the item shape came from ``store_schema_in_dynamodb``.

The bucket name is the sharpest of those seams and it is derived **twice, from two
different inputs, in two different components**: the generated Glue job derives
``adi-processed-files-<account>-<region>`` from segments 3 and 4 of
``--secret_arn``, and ``schema_extract`` derives the same name from segments 3 and
4 of ``context.invoked_function_arn``. Nothing but a flow test can catch those two
derivations disagreeing, and if they disagree the whole feature is silently inert:
the probe misses, the row omits ``sample_s3_uri``, and all four consumers fall back
to the ``jdbc://`` sentinel exactly as they do today. So the two derivations are
left to happen on their own — this file configures neither — and the object one
side writes is looked for by the other.

THE FLOW, IN THE ORDER IT IS EXERCISED
--------------------------------------
1. The **real** ``p53 C1`` block, sliced out of the **real** rendered
   ``glue_job_jdbc_source.py.j2``, executes with ``boto3`` bound to the module —
   so its ``put_object`` lands in the shared moto backend, in the bucket it
   derived from ``--secret_arn``.
2. ``handle_jdbc_schema_extract`` runs against that backend and probes; the probe
   resolves its bucket from ``context.invoked_function_arn`` and HEADs the key the
   template chose.
3. ``store_schema_in_dynamodb`` writes the state row; the row is read BACK from
   moto, not asserted against the in-memory dict that produced it.
4. All three Sample_Readers run over the URI the row carries, resolved by the
   production expression, and each must return real values.

**A test that asserts an empty dict here has proved nothing** — the point of the
feature is that real values reach the model, so every reader assertion below is an
equality against the exact expected ``dict[str, list[str]]``.

FIXTURES — SHARED ONLY
----------------------
``mock_dynamodb_tables``, ``mock_s3_buckets`` and ``mock_lambda_env`` from
``tests/fixtures/conftest.py``. There is no ``moto.mock_aws()`` call and no boto3
client mock for DynamoDB, S3 or SQS in this file; the two ``mock_*`` fixtures are
what put the moto backend in place, and ``adi-processed-files-…`` is **seeded**
into that backend with a real client because the fixture's bucket list is fixed
and does not include it. Both fixtures are requested together, which is the
established shape here — ``test_step_failure_chain.py:255`` and
``tests/unit/lambdas/dashboard_api/test_data_preview_latest_walk.py:168`` do the
same, and moto's nested ``mock_aws()`` shares one backend.

Region is ``us-east-1`` throughout, because that is what the shared fixtures build
in and what ``tests/integration/conftest.py`` pins. Per ``LGR-3e45e65-1``
(**template / ratified**) the integration layer takes the caller's region rather
than the unit layer's pinned one, so it is stated here rather than assumed.

TWO NON-AWS FAKES, AND WHY EACH IS NOT A SHARED-FIXTURE VIOLATION
-----------------------------------------------------------------
* **Glue.** ``handle_jdbc_schema_extract`` polls ``get_crawler`` until
  ``State == "READY"`` AND ``LastCrawl.Status`` is set. moto's crawler cannot reach
  that state — measured: after ``start_crawler`` it reports ``RUNNING``, and after
  ``stop_crawler`` it reports ``STOPPING`` forever — so the loop would run to its
  840-second deadline. Glue is also outside what the shared fixtures cover
  (DynamoDB, S3, SQS only, per ``tests/unit/test_no_duplicate_mocking_prop.py``'s
  own allowlist note). ``_FakeGlue`` below implements exactly the three calls the
  function makes, and nothing else.
* **The DataFrame.** No Spark. ``_FakeDataFrame`` exposes ``.limit(n).collect()``
  with rows answering ``.asDict(recursive=True)`` — the whole of the surface the
  emitted block touches — and ``limit`` slices as Spark's does. A real session
  would be billable (``tests/e2e/test_glue_*.py``) and would verify nothing more
  about the flow.

MODULE LOADING — FOUR PRIVATE COPIES, NONE PUBLISHED
----------------------------------------------------
Four of the five modules under test are named ``lambda_function.py``, a name 24
Lambda directories share, and two of this directory's sibling test modules install
``MagicMock`` stubs into ``sys.modules`` at module scope. ``LGR-c58f276-4``
(**template / ratified**) records that defect class — pytest imports every test
module during collection, so what a module-scope import binds is decided by
collection order alone — and ratifies the remedy used here: load each file with
``importlib.util.spec_from_file_location`` under a private alias that is never
published, insert stubs key-by-key for the load only, and assert at import time
that what was bound is neither a mock nor vacuous.

``env_utils`` and ``eventbridge_utils`` are stubbed deliberately rather than left
to resolve, for that same reason: a sibling module may already have put a
``MagicMock`` under either name, and ``require_env`` returning a ``MagicMock``
would make ``dynamodb.Table(...)`` nonsense. The ``env_utils`` stub reads
``os.environ`` first and falls back to the default, which is ``require_env``'s
local-mode behaviour; the ``eventbridge_utils`` stub returns three no-ops, which
is what the real ``get_eventbridge_publishers`` returns under
``EVENTBRIDGE_ENABLED=false`` (``shared/eventbridge_utils.py:22-24``).

NOTHING HERE TOUCHES PRODUCTION CODE, and nothing here amends a pre-existing
assertion. No SQL is constructed (p53 C2, do-NOT 6, 7), no Glue session is
created, no credential value appears anywhere — the secret ARN below is an ARN and
nothing else, per ``LGR-819980f-1`` (**standing invariant**).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import textwrap
from typing import Any
from unittest.mock import MagicMock, NonCallableMock

import boto3
import jinja2
import pytest

pytestmark = pytest.mark.integration

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_HERE = os.path.dirname(__file__)
_REPO = os.path.abspath(os.path.join(_HERE, "..", "..", ".."))
TEMPLATES_DIR = os.path.join(_REPO, "pyspark_templates")
_SHARED = os.path.join(_REPO, "shared")
_SCHEMA_EXTRACT = os.path.join(_REPO, "lambda", "agentic_data_ingestion_schema_extract")
_CREATE_CODE = os.path.join(_REPO, "lambda", "agentic_data_ingestion_create_code")
_SCHEMA_MAPPING = os.path.join(_REPO, "lambda", "agentic_data_ingestion_schema_mapping")
_TEST_CODE = os.path.join(_REPO, "lambda", "agentic_data_ingestion_test_code")
_AGENT = os.path.join(_REPO, "agentcore", "complex_mapping_agent")

TEMPLATE_NAME = "glue_job_jdbc_source.py.j2"

# ---------------------------------------------------------------------------
# The one identity every link in the flow has to agree on
# ---------------------------------------------------------------------------

AWS_REGION = "us-east-1"
AWS_ACCOUNT = "123456789012"

#: A Secrets Manager ARN and nothing else — the generated job reads segments 3
#: and 4 of it to build the bucket name. No credential value exists in this file
#: (``LGR-819980f-1``, standing invariant).
SECRET_ARN = f"arn:aws:secretsmanager:{AWS_REGION}:{AWS_ACCOUNT}:secret:adi/jdbc/orders-AbCdEf"

#: The probe's other input: this Lambda's own invocation ARN. Segments 3 and 4
#: again — but of a DIFFERENT arn, in a different component. That the two
#: derivations land on one bucket is the thing this file exists to check, so the
#: expected name below is written out once and asserted against BOTH sides
#: rather than fed to either.
INVOKED_FUNCTION_ARN = f"arn:aws:lambda:{AWS_REGION}:{AWS_ACCOUNT}:function:agentic-data-ingestion-schema-extract-dev"

PIPELINE_ID = "jdbcsampling-5f1c2a90"
RUN_ID = "run-2026-08-27-10-15-00-5f1c2a90"
EXECUTION_ARN = f"arn:aws:states:{AWS_REGION}:{AWS_ACCOUNT}:execution:agentic-data-ingestion:{RUN_ID}"

ARTIFACT_BUCKET = f"adi-processed-files-{AWS_ACCOUNT}-{AWS_REGION}"
ARTIFACT_KEY = f"samples/{PIPELINE_ID}/source-sample.json"
ARTIFACT_URI = f"s3://{ARTIFACT_BUCKET}/{ARTIFACT_KEY}"

#: What ``file_validation`` writes for a JDBC source, and therefore what
#: ``json_s3_uri`` must still be after this flow (p53 do-NOT 11).
JDBC_SENTINEL_URI = "jdbc://postgres/public/orders"

STATE_TABLE_NAME = "agentic_data_ingestion_state"

#: The rows the source read produced. Chosen so the readers' output is worth
#: asserting: ``order_date`` carries the ambiguous ``MM/dd`` form p49's C1 check
#: exists to see, and every field has a repeat so de-duplication is exercised.
SOURCE_ROWS: list[dict[str, Any]] = [
    {"order_id": 1001, "order_date": "05/12/2024", "amount": 42.5},
    {"order_id": 1002, "order_date": "06/12/2024", "amount": 17.0},
    {"order_id": 1003, "order_date": "05/12/2024", "amount": 42.5},
]

#: What every Sample_Reader must return from those rows: field name -> unique
#: non-empty string values, in first-seen order. Written out in full rather than
#: derived, so a reader change has to disagree with a literal.
EXPECTED_SAMPLES: dict[str, list[str]] = {
    "order_id": ["1001", "1002", "1003"],
    "order_date": ["05/12/2024", "06/12/2024"],
    "amount": ["42.5", "17.0"],
}

#: The crawler's output, shaped to the rows above.
GLUE_COLUMNS: list[dict[str, str]] = [
    {"Name": "order_id", "Type": "int"},
    {"Name": "order_date", "Type": "string"},
    {"Name": "amount", "Type": "double"},
]


# ---------------------------------------------------------------------------
# Private module loading (see the docstring's "MODULE LOADING" section)
# ---------------------------------------------------------------------------

_MISSING = object()


class _ScopedStubs:
    """Insert stub modules for one load, then remove exactly what was inserted.

    Key-by-key rather than ``patch.dict("sys.modules", …)``: that helper's
    restore clears ``sys.modules`` and re-applies a snapshot, which evicts
    anything first imported inside the block and hands the next importer a
    second, distinct copy.
    """

    def __init__(self, stubs: dict[str, Any]) -> None:
        self._stubs = stubs
        self._prior: dict[str, Any] = {}

    def __enter__(self) -> _ScopedStubs:
        for name, module in self._stubs.items():
            self._prior[name] = sys.modules.get(name, _MISSING)
            sys.modules[name] = module
        return self

    def __exit__(self, *exc: object) -> bool:
        for name, stub in self._stubs.items():
            prior = self._prior[name]
            if prior is _MISSING:
                if sys.modules.get(name) is stub:
                    del sys.modules[name]
            else:
                sys.modules[name] = prior
        return False


class _WithoutForeignMocks:
    """Hide every ``Mock``-valued ``sys.modules`` entry for the duration of a load.

    MEASURED, not defensive. ``tests/integration/approval_flow/test_approval_system.py``
    installs ``Mock()`` modules at module scope, and pytest imports it during
    collection — before this file, alphabetically. One of those entries makes
    ``schema_extract``'s module-scope ``from artifact_schema import
    create_output_data`` bind a ``Mock``, so ``store_schema_in_dynamodb`` puts a
    ``Mock`` into the item and ``put_item`` dies with
    ``RecursionError: maximum recursion depth exceeded`` inside botocore's
    ``deepcopy`` of the request context. Eleven of this file's fifteen tests
    failed that way in the full-layer run while passing in isolation — the
    collection-order dependence ``LGR-c58f276-4`` (**template / ratified**)
    records, reaching a *different* name than the four modules that record
    names.

    So rather than enumerate the shared modules five production files import
    transitively — a list that would go stale — the rule is stated positively:
    **load against real modules, except the names stubbed on purpose.** Names in
    *keep* are left alone, since those are this file's deliberate stubs.

    Everything is restored on exit, so no sibling module's stub is disturbed:
    this widens nothing and narrows nothing outside the load.
    """

    def __init__(self, keep: set[str]) -> None:
        self._keep = keep
        self._removed: dict[str, Any] = {}

    def __enter__(self) -> _WithoutForeignMocks:
        for name, module in list(sys.modules.items()):
            if name in self._keep:
                continue
            if isinstance(module, NonCallableMock):
                self._removed[name] = module
                del sys.modules[name]
        return self

    def __exit__(self, *exc: object) -> bool:
        for name, module in self._removed.items():
            sys.modules.setdefault(name, module)
        return False


def _load_private(alias: str, path: str, stubs: dict[str, Any], search_paths: list[str]):
    """Exec *path* under *alias* without publishing it to ``sys.modules``."""
    added = [p for p in search_paths if p not in sys.path]
    for p in added:
        sys.path.insert(0, p)
    try:
        spec = importlib.util.spec_from_file_location(alias, path)
        assert spec is not None and spec.loader is not None, f"cannot load {path}"
        module = importlib.util.module_from_spec(spec)
        with _WithoutForeignMocks(keep=set(stubs)), _ScopedStubs(stubs):
            spec.loader.exec_module(module)
        return module
    finally:
        for p in added:
            if p in sys.path:
                sys.path.remove(p)


def _noop(*args: object, **kwargs: object) -> None:
    return None


#: ``require_env``'s local-mode behaviour: environment first, then the default.
_ENV_UTILS_STUB = MagicMock(
    require_env=lambda name, default=None: os.environ.get(name) or default or name,
    get_aws_account_id=lambda: AWS_ACCOUNT,
)

#: What ``get_eventbridge_publishers`` returns under ``EVENTBRIDGE_ENABLED=false``.
_EVENTBRIDGE_STUB = MagicMock(get_eventbridge_publishers=lambda: (_noop, _noop, _noop))


def _agentcore_stubs() -> dict[str, Any]:
    runtime = MagicMock()
    runtime.BedrockAgentCoreApp.return_value.entrypoint = lambda fn: fn
    package = MagicMock()
    package.runtime = runtime
    return {
        "bedrock_agentcore": package,
        "bedrock_agentcore.runtime": runtime,
        "env_utils": _ENV_UTILS_STUB,
    }


#: The probe and the row writer.
SCHEMA_EXTRACT = _load_private(
    "_p53flow_schema_extract_lambda_function",
    os.path.join(_SCHEMA_EXTRACT, "lambda_function.py"),
    {"env_utils": _ENV_UTILS_STUB, "eventbridge_utils": _EVENTBRIDGE_STUB},
    [_SHARED, _SCHEMA_EXTRACT],
)
#: Consumer 1 — the p49 date-format check's reader, reached via its seam.
CREATE_CODE = _load_private(
    "_p53flow_create_code_lambda_function",
    os.path.join(_CREATE_CODE, "lambda_function.py"),
    {"strands": MagicMock(), "env_utils": _ENV_UTILS_STUB},
    [_SHARED, _CREATE_CODE],
)
#: Consumer 2 — the mapping prompt's near-duplicate reader.
SCHEMA_MAPPING = _load_private(
    "_p53flow_schema_mapping_lambda_function",
    os.path.join(_SCHEMA_MAPPING, "lambda_function.py"),
    {"env_utils": _ENV_UTILS_STUB},
    [_SHARED, _SCHEMA_MAPPING],
)
#: Consumer 4 — the ancestor the other two descend from.
AGENT = _load_private(
    "_p53flow_complex_mapping_agent_main",
    os.path.join(_AGENT, "main.py"),
    _agentcore_stubs(),
    [_SHARED, _AGENT],
)
#: Consumer 3 — P44's Sample_Frame format resolution. Read-only here.
GLUE_SESSION_RUNNER = _load_private(
    "_p53flow_glue_session_runner",
    os.path.join(_TEST_CODE, "glue_session_runner.py"),
    {},
    [_SHARED, _TEST_CODE],
)

# Import-time non-vacuity guards, per ``LGR-c58f276-4``: fail at collection if
# what was bound is a mock or is missing what is under test, rather than passing
# vacuously in every test below.
for _label, _module, _attrs in (
    (
        "schema_extract",
        SCHEMA_EXTRACT,
        (
            "_probe_sample_artifact_uri",
            "_sample_artifact_bucket",
            "store_schema_in_dynamodb",
            "handle_jdbc_schema_extract",
        ),
    ),
    ("create_code", CREATE_CODE, ("read_source_samples", "_sample_reader_for")),
    ("schema_mapping", SCHEMA_MAPPING, ("read_source_samples",)),
    ("complex_mapping_agent", AGENT, ("read_sample_data",)),
    ("glue_session_runner", GLUE_SESSION_RUNNER, ("sample_read_format",)),
):
    assert not isinstance(_module, MagicMock), f"{_label} resolved to a mock, not the real module"
    for _attr in _attrs:
        assert hasattr(_module, _attr), f"the real {_label} has no {_attr}; the private load was defeated"

# ``hasattr`` alone is not a guard — a MagicMock has every attribute — so each
# loaded module is additionally asked a question with a known answer. This is the
# load-bearing half of ``LGR-c58f276-4``: bound to a mock, a "returns {} rather
# than raising" property passes vacuously, so a broken load reads as a healthy
# suite.
assert SCHEMA_EXTRACT._SAMPLE_ARTIFACT_KEY.format(pipeline_id=PIPELINE_ID) == ARTIFACT_KEY, (
    "the loaded schema_extract does not build the artifact key this flow expects; the key is a "
    "CONTRACT shared with glue_job_jdbc_source.py.j2 and one side has moved"
)
assert CREATE_CODE.read_source_samples("jdbc://pipeline/x") == {}, (
    "the loaded create_code reader does not decline a jdbc:// URI; it is not the real reader"
)
assert SCHEMA_MAPPING.read_source_samples("jdbc://pipeline/x") == {}, (
    "the loaded schema_mapping reader does not decline a jdbc:// URI; it is not the real reader"
)
assert AGENT.read_sample_data("jdbc://pipeline/x") == {}, (
    "the loaded complex_mapping_agent reader does not decline a jdbc:// URI; it is not the real reader"
)
assert GLUE_SESSION_RUNNER.sample_read_format(ARTIFACT_URI) == "json", (
    "the loaded sample_read_format does not resolve the artifact key to json; consumer 3's read "
    "would fall back to a synthetic frame"
)

#: ``item_str`` — the real production helper both ``read_source_samples`` callers
#: use to pull the URI off the row. Loaded rather than reimplemented so this
#: file's URI resolution is the production one, and loaded through the same
#: private path as everything else so ``shared/`` is not left on ``sys.path``.
DYNAMO_TYPING = _load_private(
    "_p53flow_dynamo_typing",
    os.path.join(_SHARED, "dynamo_typing.py"),
    {},
    [_SHARED],
)
item_str = DYNAMO_TYPING.item_str

assert item_str({"sample_s3_uri": "s3://b/k"}, "sample_s3_uri") == "s3://b/k"
assert item_str({}, "sample_s3_uri") == "", "the loaded item_str is not the real total helper"


# ---------------------------------------------------------------------------
# The writer: the real emitted block, sliced out of the real rendered template
# ---------------------------------------------------------------------------

_BLOCK_OPEN = "# === p53 C1:"
_BLOCK_CLOSE = "# === End p53 C1 ==="


def _render() -> str:
    """The canonical variable surface ``test_glue_job_jdbc_source.py:97`` uses."""
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        undefined=jinja2.StrictUndefined,
        keep_trailing_newline=True,
    )
    return env.get_template(TEMPLATE_NAME).render(
        engine="postgres",
        target_type="S3_TABLES",
        read_mode="full_table",
        write_mode="overwrite",
        partition_columns="",
        merge_key="id",
        initial_watermark="",
        transform_block="df_transformed = df",
    )


def _extract_block(rendered: str) -> str:
    """Slice the p53 C1 span out of *rendered* and dedent it to column 0."""
    lines = rendered.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(_BLOCK_OPEN))
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == _BLOCK_CLOSE)
    return textwrap.dedent("\n".join(lines[start : end + 1]))


SAMPLE_BLOCK_SOURCE = _extract_block(_render())
SAMPLE_BLOCK = compile(SAMPLE_BLOCK_SOURCE, f"<{TEMPLATE_NAME}:p53-C1>", "exec")

assert SAMPLE_BLOCK_SOURCE.count("put_object(") == 1, (
    "the extracted block is not the single guarded write; the slice or the template has moved"
)


class _FakeRow:
    """A Spark ``Row``, to the extent the emitted block uses one."""

    def __init__(self, values: dict[str, Any]) -> None:
        self._values = values

    def asDict(self, recursive: bool = False) -> dict[str, Any]:
        return dict(self._values)


class _FakeDataFrame:
    """``.limit(n).collect()`` and nothing else. ``limit`` slices, as Spark's does."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    def limit(self, num: int) -> _FakeDataFrame:
        return _FakeDataFrame(self._rows[:num])

    def collect(self) -> list[_FakeRow]:
        return [_FakeRow(row) for row in self._rows]


class _Logger:
    """``logger.info`` / ``logger.warning``, recorded."""

    def __init__(self) -> None:
        self.infos: list[str] = []
        self.warnings: list[str] = []

    def info(self, message: object) -> None:
        self.infos.append(str(message))

    def warning(self, message: object) -> None:
        self.warnings.append(str(message))


def run_generated_sample_write(rows: list[dict[str, Any]]) -> _Logger:
    """Execute the generated job's own p53 C1 block against the moto backend.

    ``boto3`` is the real module, so the block's ``boto3.client("s3")`` resolves
    to a moto-backed client and its ``put_object`` really lands in the bucket the
    block derived from ``--secret_arn``. This function deliberately does NOT tell
    it where to write.
    """
    logger = _Logger()
    namespace: dict[str, Any] = {
        "args": {"secret_arn": SECRET_ARN, "pipeline_id": PIPELINE_ID},
        "df": _FakeDataFrame(rows),
        "json": json,
        "boto3": boto3,
        "logger": logger,
    }
    # exec is deliberate: this runs the generated job's own emitted block.
    exec(SAMPLE_BLOCK, namespace)
    return logger


# ---------------------------------------------------------------------------
# The Glue fake — exactly the three calls handle_jdbc_schema_extract makes
# ---------------------------------------------------------------------------


class _CrawlerRunningException(Exception):
    """Stand-in for ``boto3.client('glue').exceptions.CrawlerRunningException``."""


class _EntityNotFoundException(Exception):
    """Stand-in for ``boto3.client('glue').exceptions.EntityNotFoundException``."""


class _FakeGlueExceptions:
    CrawlerRunningException = _CrawlerRunningException
    EntityNotFoundException = _EntityNotFoundException


class _FakeGlue:
    """A crawler that has already finished. See the docstring for why not moto."""

    def __init__(self, table_name: str, columns: list[dict[str, str]]) -> None:
        self.exceptions = _FakeGlueExceptions()
        self.started: list[str] = []
        self.table_queries: list[tuple[str, str]] = []
        self._table_name = table_name
        self._columns = columns

    # The three signatures below use boto3's own PascalCase parameter names
    # because the production calls pass them by keyword.
    def start_crawler(self, Name: str) -> dict:
        self.started.append(Name)
        return {}

    def get_crawler(self, Name: str) -> dict:
        return {"Crawler": {"Name": Name, "State": "READY", "LastCrawl": {"Status": "SUCCEEDED"}}}

    def get_tables(self, DatabaseName: str, Expression: str) -> dict:
        self.table_queries.append((DatabaseName, Expression))
        return {
            "TableList": [
                {
                    "Name": self._table_name,
                    "StorageDescriptor": {"Columns": self._columns},
                    "PartitionKeys": [],
                }
            ]
        }


class _Context:
    """The one attribute ``_sample_artifact_bucket`` reads off a Lambda context."""

    def __init__(self, invoked_function_arn: str) -> None:
        self.invoked_function_arn = invoked_function_arn


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def flow(mock_dynamodb_tables, mock_s3_buckets, mock_lambda_env, monkeypatch):
    """The moto world, with every module handle bound to it.

    ``mock_dynamodb_tables`` and ``mock_s3_buckets`` are the shared fixtures; they
    are what puts the moto backend in place, and the artifact bucket is seeded
    into that backend here because their bucket list is fixed and does not include
    it.

    ``PROCESSED_BUCKET`` is deliberately REMOVED rather than set: the probe must
    fall through to deriving the bucket from ``context.invoked_function_arn``,
    which is the derivation the generated job's own has to agree with. A test that
    set the variable would configure the agreement it is supposed to be checking.
    """
    monkeypatch.delenv("PROCESSED_BUCKET", raising=False)

    s3 = boto3.client("s3", region_name=AWS_REGION)
    # The Glue job's own bucket. Seeded, not named to either side.
    s3.create_bucket(Bucket=ARTIFACT_BUCKET)

    state_table = mock_dynamodb_tables[STATE_TABLE_NAME]
    glue = _FakeGlue(f"agentic_data_ingestion_{PIPELINE_ID}_orders", GLUE_COLUMNS)

    monkeypatch.setattr(SCHEMA_EXTRACT, "s3_client", s3)
    monkeypatch.setattr(SCHEMA_EXTRACT, "table", state_table)
    monkeypatch.setattr(SCHEMA_EXTRACT, "glue_client", glue)
    # The three readers read the artifact through their own module-level clients.
    monkeypatch.setattr(CREATE_CODE, "s3_client", s3)
    monkeypatch.setattr(SCHEMA_MAPPING, "s3_client", s3)
    monkeypatch.setattr(AGENT, "_s3_client", s3)

    # What file_validation (Task 6.2) wrote for this JDBC run. `json_s3_uri` is
    # taken from this row, so it is the origin of the value do-NOT 11 protects.
    state_table.put_item(
        Item={
            "run_id": RUN_ID,
            "step_name": "file_validation",
            "step_sequence": 1,
            "status": "success",
            "pipeline_id": PIPELINE_ID,
            "original_pipeline_id": PIPELINE_ID,
            "original_s3_uri": JDBC_SENTINEL_URI,
        }
    )

    return {"s3": s3, "state_table": state_table, "glue": glue}


def _extract_event() -> dict[str, str]:
    return {"run_id": RUN_ID, "original_pipeline_id": PIPELINE_ID, "execution_arn": EXECUTION_ARN}


def _schema_row(state_table) -> dict[str, Any]:
    """Read the schema_extract row BACK out of moto."""
    response = state_table.get_item(Key={"run_id": RUN_ID, "step_name": "schema_extract"})
    assert "Item" in response, "schema_extract wrote no state row"
    return response["Item"]


def _resolved_reader_uri(item: dict[str, Any]) -> str:
    """The production preference, over the real row, through the real helper.

    This is the expression at ``create_code/lambda_function.py:703`` and
    ``schema_mapping/lambda_function.py:2104``. ``item_str`` is imported from
    ``shared/`` rather than reimplemented, and the value it produces is
    cross-checked below against the ``source_sample_uri`` the JDBC arm resolved
    for itself — so this helper cannot silently disagree with production.
    """
    return item_str(item, "sample_s3_uri") or item_str(item, "json_s3_uri")


# ---------------------------------------------------------------------------
# Link 1 — the writer and the probe agree on one location, unconfigured
# ---------------------------------------------------------------------------


class TestTheWriterAndTheProbeAgreeOnOneLocation:
    """Two independent derivations of one bucket name, from two different ARNs.

    **Validates: Requirements 1.3, 1.4**
    """

    def test_the_probe_finds_the_object_the_generated_job_wrote(self, flow) -> None:
        logger = run_generated_sample_write(SOURCE_ROWS)

        # The job says where it wrote. Nothing configured this.
        assert logger.warnings == [], f"the sample write reported a failure: {logger.warnings}"
        assert ARTIFACT_URI in logger.infos[0], (
            f"the job wrote somewhere other than {ARTIFACT_URI}: {logger.infos[0]!r}"
        )

        found = SCHEMA_EXTRACT._probe_sample_artifact_uri(PIPELINE_ID, _Context(INVOKED_FUNCTION_ARN))

        assert found == ARTIFACT_URI, (
            f"the probe resolved {found!r} but the job wrote {ARTIFACT_URI!r}. The two derive the "
            f"bucket from different ARNs (--secret_arn vs invoked_function_arn) and the key from a "
            f"shared format string; one side has moved and the feature is silently inert"
        )

    def test_the_two_sides_derive_the_same_bucket_from_different_arns(self, flow) -> None:
        """The derivations, isolated — so a failure above localises to one side."""
        run_generated_sample_write(SOURCE_ROWS)

        probe_bucket = SCHEMA_EXTRACT._sample_artifact_bucket(_Context(INVOKED_FUNCTION_ARN))
        objects = flow["s3"].list_objects_v2(Bucket=ARTIFACT_BUCKET).get("Contents", [])

        assert probe_bucket == ARTIFACT_BUCKET
        assert [obj["Key"] for obj in objects] == [ARTIFACT_KEY], (
            "the generated job wrote a different key, or more than one object"
        )

    def test_the_probe_is_empty_before_the_job_has_ever_run(self, flow) -> None:
        """The First_Run_Gap (p53 C4). The bucket exists; the object does not."""
        assert SCHEMA_EXTRACT._probe_sample_artifact_uri(PIPELINE_ID, _Context(INVOKED_FUNCTION_ARN)) == ""

    def test_the_processed_bucket_env_var_reaches_the_same_object(self, flow, monkeypatch) -> None:
        """The other arm of ``_sample_artifact_bucket``, over the same artifact.

        A future deploy that sets ``PROCESSED_BUCKET`` on this Lambda must find
        what the job wrote, not shadow it.
        """
        run_generated_sample_write(SOURCE_ROWS)
        monkeypatch.setenv("PROCESSED_BUCKET", ARTIFACT_BUCKET)

        assert SCHEMA_EXTRACT._probe_sample_artifact_uri(PIPELINE_ID, _Context("")) == ARTIFACT_URI


# ---------------------------------------------------------------------------
# Link 2 — the row records the sample URI and leaves json_s3_uri alone
# ---------------------------------------------------------------------------


class TestTheRowRecordsTheSampleUri:
    """``handle_jdbc_schema_extract`` end to end, row read back out of moto.

    **Validates: Requirements 2.6, 1.4**
    """

    def test_the_row_carries_sample_s3_uri_and_json_s3_uri_is_untouched(self, flow) -> None:
        run_generated_sample_write(SOURCE_ROWS)

        result = SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))

        assert result["statusCode"] == 200
        assert flow["glue"].started == [f"agentic-data-ingestion-{PIPELINE_ID}-source-discover"]

        item = _schema_row(flow["state_table"])
        assert item["sample_s3_uri"] == ARTIFACT_URI
        # p53 do-NOT 11: three other readers take json_s3_uri to mean the
        # canonical source URI. It is the sentinel here and must stay so.
        assert item["json_s3_uri"] == JDBC_SENTINEL_URI

        # The envelope resolves the preference in Python (ASL has no coalesce).
        assert result["sample_s3_uri"] == ARTIFACT_URI
        assert result["json_s3_uri"] == JDBC_SENTINEL_URI
        assert result["source_sample_uri"] == ARTIFACT_URI

    def test_the_row_omits_the_key_entirely_on_run_one(self, flow) -> None:
        """No artifact: the attribute is ABSENT, not empty (p53 C3)."""
        result = SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))

        item = _schema_row(flow["state_table"])
        assert "sample_s3_uri" not in item, (
            "an empty-string attribute would make the key's presence stop meaning 'there is a sample'"
        )
        assert item["json_s3_uri"] == JDBC_SENTINEL_URI
        assert result["sample_s3_uri"] == ""
        assert result["source_sample_uri"] == JDBC_SENTINEL_URI

    def test_the_production_preference_and_the_envelope_agree(self, flow) -> None:
        """Cross-check: this file's ``_resolved_reader_uri`` == production's own.

        The callers' preference expression is inline in two ``lambda_handler``
        bodies, so it cannot be imported. Asserting it against the
        ``source_sample_uri`` the JDBC arm resolved for itself is what keeps this
        file's copy honest instead of merely plausible.
        """
        run_generated_sample_write(SOURCE_ROWS)
        result = SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))

        assert _resolved_reader_uri(_schema_row(flow["state_table"])) == result["source_sample_uri"]


# ---------------------------------------------------------------------------
# Link 3 — every Sample_Reader gets real values
# ---------------------------------------------------------------------------

#: The three Sample_Readers, each named by its consumer and reached the way its
#: own caller reaches it. ``create_code`` goes through ``_sample_reader_for``,
#: the injection seam its caller uses (BC9, ``LGR-1e135d2-2``); the other two are
#: called directly, as their callers call them.
READERS = [
    pytest.param(lambda uri: CREATE_CODE._sample_reader_for(uri)(), id="create_code-via-seam"),
    pytest.param(lambda uri: SCHEMA_MAPPING.read_source_samples(uri), id="schema_mapping"),
    pytest.param(lambda uri: AGENT.read_sample_data(uri), id="complex_mapping_agent"),
]


class TestEverySampleReaderGetsRealValues:
    """The point of the whole feature: real values reach the model.

    **Validates: Requirements 1.3, 2.1, 2.2, 2.4**
    """

    @pytest.mark.parametrize("read", READERS)
    def test_the_reader_returns_the_sampled_values_from_the_recorded_uri(self, flow, read) -> None:
        run_generated_sample_write(SOURCE_ROWS)
        result = SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))
        uri = _resolved_reader_uri(_schema_row(flow["state_table"]))
        assert uri == result["source_sample_uri"] == ARTIFACT_URI

        samples = read(uri)

        assert samples == EXPECTED_SAMPLES, (
            f"the reader returned {samples!r}. An empty dict here means a JDBC pipeline is still "
            f"inert and the whole flow has failed somewhere upstream"
        )
        # The contract the callers depend on, checked on the populated path
        # rather than only on the declining one (R2.1).
        assert all(isinstance(k, str) and isinstance(v, list) for k, v in samples.items())
        assert all(isinstance(value, str) for values in samples.values() for value in values)

    @pytest.mark.parametrize("read", READERS)
    def test_the_same_reader_is_inert_on_run_one(self, flow, read) -> None:
        """R2.5: with no artifact the reader is handed ``json_s3_uri`` as before.

        This is the fallback still working, not a regression — the ``jdbc://``
        decline is deliberate, asserted elsewhere, and must stay green.
        """
        SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))
        uri = _resolved_reader_uri(_schema_row(flow["state_table"]))

        assert uri == JDBC_SENTINEL_URI
        assert read(uri) == {}

    def test_the_sample_frame_reader_resolves_a_format_for_the_recorded_uri(self, flow) -> None:
        """Consumer 3's half of R1.3, without a Glue session.

        P44's ``_SAMPLE_READERS["json"]`` is ``multiLine=True``, which needs an
        array; ``sample_read_format`` returning ``None`` for the URI would send
        the run to a synthetic frame instead. Both are checked against the URI
        the row actually recorded.
        """
        run_generated_sample_write(SOURCE_ROWS)
        SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))
        uri = _resolved_reader_uri(_schema_row(flow["state_table"]))

        assert GLUE_SESSION_RUNNER.sample_read_format(uri) == "json"

        body = flow["s3"].get_object(Bucket=ARTIFACT_BUCKET, Key=ARTIFACT_KEY)["Body"].read()
        assert json.loads(body) == SOURCE_ROWS, "the artifact in the bucket is not the rows the job read"
        assert body.decode("utf-8").startswith("["), "a non-array body would read as JSONL, which multiLine rejects"

    def test_a_sampling_failure_leaves_the_flow_where_run_one_leaves_it(self, flow) -> None:
        """R1.6 / Property 7, over the flow rather than over the block alone.

        The bucket is deleted before the job writes, so ``put_object`` fails. The
        run must still produce a schema row, and every consumer must land on the
        documented fallback rather than on an error.
        """
        flow["s3"].delete_bucket(Bucket=ARTIFACT_BUCKET)

        logger = run_generated_sample_write(SOURCE_ROWS)
        assert logger.warnings, "the failed write logged nothing; risk N1 needs it diagnosable"

        result = SCHEMA_EXTRACT.handle_jdbc_schema_extract(_extract_event(), _Context(INVOKED_FUNCTION_ARN))

        assert result["statusCode"] == 200
        assert result["sample_s3_uri"] == ""
        assert result["source_sample_uri"] == JDBC_SENTINEL_URI
        assert "sample_s3_uri" not in _schema_row(flow["state_table"])
