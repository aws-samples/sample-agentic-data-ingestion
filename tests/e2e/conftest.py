# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""E2E test layer conftest — real-AWS fixtures and helpers.

E2E tests run against a *deployed* AgenticDataIngestion environment. They are skipped
unless ``AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true`` (set by ``make test-e2e``) and valid
AWS credentials are available.

Resource names follow the deterministic CDK naming convention
(``agentic-data-ingestion-<purpose>-<account>-<region>`` / ``agentic_data_ingestion_<table>`` /
``agentic-data-ingestion-pipeline``) so they can be derived from the active account
and region without hard-coding stack outputs.
"""

import os
import time

import pytest
from tests.e2e._harness.approval import ApprovalDriver
from tests.e2e._harness.gating import (  # noqa: F401 — re-exported as fixtures
    cost_budget,
    requires_trigger,
)
from tests.e2e._harness.ledger import ResourceLedger
from tests.e2e._harness.provisioner import TestPipelineProvisioner
from tests.e2e._harness.rest import CognitoRestClient, acquire_cognito_id_token
from tests.e2e._harness.verify import TargetVerifier

_THIS_DIR = os.path.dirname(__file__)

E2E_DEFAULT_TIMEOUT = 420  # seconds — a full CSV pipeline run is ~4 min
STATE_MACHINE_NAME = "agentic-data-ingestion-pipeline"


def pytest_collection_modifyitems(items):
    """Automatically add the 'e2e' marker to every test in this directory."""
    for item in items:
        if os.path.commonpath([str(item.fspath), _THIS_DIR]) == _THIS_DIR:
            item.add_marker(pytest.mark.e2e)


def _real_aws_enabled() -> bool:
    return os.environ.get("AGENTIC_DATA_INGESTION_TEST_REAL_AWS", "").lower() == "true"


@pytest.fixture(scope="session", autouse=True)
def require_real_aws():
    """Skip the entire e2e layer unless real-AWS mode is explicitly enabled."""
    if not _real_aws_enabled():
        pytest.skip(
            "E2E tests require a deployed environment. Set AGENTIC_DATA_INGESTION_TEST_REAL_AWS=true (see `make test-e2e`)."
        )


@pytest.fixture(scope="session")
def boto3_session():
    import boto3

    region = os.environ.get("AWS_REGION", "us-west-2")
    return boto3.Session(region_name=region)


@pytest.fixture(scope="session")
def aws_region():
    return os.environ.get("AWS_REGION", "us-west-2")


@pytest.fixture(scope="session")
def account_id(boto3_session):
    return boto3_session.client("sts").get_caller_identity()["Account"]


@pytest.fixture(scope="session")
def resource_names(account_id, aws_region):
    """Deterministic deployed-resource names derived from account + region."""
    suffix = f"{account_id}-{aws_region}"
    return {
        "source_bucket": f"adi-source-files-{suffix}",
        "processed_bucket": f"adi-processed-files-{suffix}",
        "generated_code_bucket": f"adi-generated-code-{suffix}",
        "target_schemas_bucket": f"adi-target-schemas-{suffix}",
        "state_machine_arn": (f"arn:aws:states:{aws_region}:{account_id}:stateMachine:{STATE_MACHINE_NAME}"),
        "pipelines_table": "agentic_data_ingestion_pipelines",
        "executions_table": "agentic_data_ingestion_executions",
        "state_table": "agentic_data_ingestion_state",
    }


def _stack_output(boto3_session, stack_name: str, output_key: str) -> str | None:
    """Read a CloudFormation stack output, returning None when unavailable."""
    try:
        cfn = boto3_session.client("cloudformation")
        stacks = cfn.describe_stacks(StackName=stack_name).get("Stacks", [])
        for out in stacks[0].get("Outputs", []) if stacks else []:
            if out.get("OutputKey") == output_key:
                return out.get("OutputValue")
    except Exception:
        return None
    return None


@pytest.fixture(scope="session")
def environment_name():
    return os.environ.get("ADI_E2E_ENVIRONMENT", "dev")


@pytest.fixture(scope="session")
def dashboard_base_url(boto3_session, environment_name):
    """The CloudFront distribution URL — the only supported API entry point.

    The regional WAF blocks direct ``execute-api`` calls (X-Origin-Verify
    enforcement), so e2e traffic must go through CloudFront like the real
    dashboard. Override with ``ADI_E2E_BASE_URL`` if needed.
    """
    override = os.environ.get("ADI_E2E_BASE_URL")
    if override:
        return override.rstrip("/")
    url = _stack_output(boto3_session, f"AgenticDataIngestion-Hosting-{environment_name}", "CloudFrontUrl")
    if not url:
        pytest.skip("Hosting stack CloudFrontUrl output not found in this account/region")
    return url.rstrip("/")


@pytest.fixture(scope="session")
def cognito_e2e_auth(boto3_session, environment_name):
    """Session-scoped throwaway Cognito user + ID token for the e2e app client.

    Creates ``adi-e2e-<hex>@example.invalid`` with a random policy-compliant
    password via the admin APIs, authenticates through USER_PASSWORD_AUTH on
    the dedicated e2e app client (Auth stack ``E2eTestClientId`` output), and
    deletes the user at session teardown.
    """
    import secrets as _secrets
    import string
    import uuid as _uuid

    auth_stack = f"AgenticDataIngestion-Auth-{environment_name}"
    user_pool_id = os.environ.get("ADI_E2E_USER_POOL_ID") or _stack_output(boto3_session, auth_stack, "UserPoolId")
    client_id = os.environ.get("ADI_E2E_CLIENT_ID") or _stack_output(boto3_session, auth_stack, "E2eTestClientId")
    if not user_pool_id or not client_id:
        pytest.skip(
            "Auth stack UserPoolId/E2eTestClientId outputs not found — deploy the "
            "Auth stack with -c adi:deployE2eClient=true (which creates the e2e app "
            "client) before running API e2e tests."
        )

    # Random password satisfying the pool policy (16+ chars, all classes).
    alphabet = string.ascii_letters + string.digits
    password = "".join(_secrets.choice(alphabet) for _ in range(20)) + "aZ1!"
    username = f"adi-e2e-{_uuid.uuid4().hex[:12]}@example.invalid"

    idp = boto3_session.client("cognito-idp")
    idp.admin_create_user(
        UserPoolId=user_pool_id,
        Username=username,
        UserAttributes=[
            {"Name": "email", "Value": username},
            {"Name": "email_verified", "Value": "true"},
        ],
        MessageAction="SUPPRESS",
    )
    idp.admin_set_user_password(UserPoolId=user_pool_id, Username=username, Password=password, Permanent=True)
    try:
        token = acquire_cognito_id_token(boto3_session, client_id=client_id, username=username, password=password)
        yield {"id_token": token, "username": username, "user_pool_id": user_pool_id}
    finally:
        try:
            idp.admin_delete_user(UserPoolId=user_pool_id, Username=username)
        except Exception:
            pass


@pytest.fixture(scope="session")
def cognito_id_token(cognito_e2e_auth) -> str:
    """The raw Cognito ID token for Authorization headers and WS ?token=."""
    return cognito_e2e_auth["id_token"]


def find_execution_by_input(
    sfn_client,
    state_machine_arn,
    *,
    pipeline_id,
    started_after,
    timeout=120,
):
    """Find a Step Functions execution matching pipeline_id, returning as soon as found.

    Unlike poll_execution_by_input, this returns immediately once the execution
    is found — even if it's still RUNNING. Used for the two-phase pattern:
    find → release gates → poll for terminal.

    Returns:
        (status, execution_arn) where status may be RUNNING or terminal.

    Raises:
        TimeoutError: if no matching execution is found within timeout.
    """
    import json

    deadline = time.time() + timeout
    while time.time() < deadline:
        for status_filter in ("RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            for ex in sfn_client.list_executions(
                stateMachineArn=state_machine_arn,
                statusFilter=status_filter,
                maxResults=25,
            ).get("executions", []):
                if ex["startDate"].timestamp() < started_after:
                    continue
                arn = ex["executionArn"]
                desc = sfn_client.describe_execution(executionArn=arn)
                try:
                    inp = json.loads(desc.get("input", "{}"))
                except json.JSONDecodeError:
                    inp = {}
                if inp.get("pipeline_id") == pipeline_id or pipeline_id in arn:
                    return desc["status"], arn
        time.sleep(5)
    raise TimeoutError(f"No execution found for pipeline {pipeline_id} within {timeout}s")


def poll_execution_by_input(
    sfn_client,
    state_machine_arn,
    *,
    pipeline_id,
    started_after,
    timeout=E2E_DEFAULT_TIMEOUT,
):
    """Poll Step Functions for an execution started after ``started_after``
    whose input references ``pipeline_id``, returning its final status.

    The S3-upload execution name is derived from the filename (not the
    pipeline id), so matching is done on execution *input* + start time.

    Returns:
        (status, execution_arn) where status is SUCCEEDED / FAILED / TIMED_OUT
        / ABORTED.

    Raises:
        TimeoutError: if no matching execution reaches a terminal state in time.
    """
    import json

    deadline = time.time() + timeout
    matched_arn = None
    while time.time() < deadline:
        for status in ("RUNNING", "SUCCEEDED", "FAILED", "TIMED_OUT", "ABORTED"):
            for ex in sfn_client.list_executions(
                stateMachineArn=state_machine_arn,
                statusFilter=status,
                maxResults=25,
            ).get("executions", []):
                if ex["startDate"].timestamp() < started_after:
                    continue
                arn = ex["executionArn"]
                if matched_arn and arn != matched_arn:
                    continue
                desc = sfn_client.describe_execution(executionArn=arn)
                try:
                    inp = json.loads(desc.get("input", "{}"))
                except json.JSONDecodeError:
                    inp = {}
                if inp.get("pipeline_id") == pipeline_id or pipeline_id in arn:
                    matched_arn = arn
                    if desc["status"] != "RUNNING":
                        return desc["status"], arn
        time.sleep(10)
    raise TimeoutError(
        f"No terminal execution for pipeline {pipeline_id} within {timeout}s (matched_arn={matched_arn})."
    )


def find_pipelines(boto3_session, *, target_type=None, glue_status=None, status="active"):
    """Scan the pipelines table and return records matching the given filters."""
    table = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_pipelines")
    items = []
    scan_kwargs: dict = {}
    while True:
        resp = table.scan(**scan_kwargs)
        items.extend(resp.get("Items", []))
        if "LastEvaluatedKey" not in resp:
            break
        scan_kwargs["ExclusiveStartKey"] = resp["LastEvaluatedKey"]

    def _match(it):
        return (
            (not status or it.get("status") == status)
            and (not target_type or it.get("target_type") == target_type)
            and (not glue_status or it.get("glue_job_last_status") == glue_status)
        )

    return [it for it in items if _match(it)]


@pytest.fixture
def find_pipelines_fn(boto3_session):
    """Fixture: callable to find pipeline records by target_type/glue_status."""

    def _find(*, target_type=None, glue_status=None, status="active"):
        return find_pipelines(
            boto3_session,
            target_type=target_type,
            glue_status=glue_status,
            status=status,
        )

    return _find


def latest_successful_runpipeline(boto3_session, pipeline_id, *, run_id=None):
    """Return the most recent successful RunPipeline step record for a pipeline.

    Looks up the State table PipelineIndex GSI for RunPipeline steps and
    returns the newest one whose ``status`` is ``success``.

    Returns None if the pipeline has no successful RunPipeline step.
    """
    from boto3.dynamodb.conditions import Attr, Key

    state = boto3_session.resource("dynamodb").Table("agentic_data_ingestion_state")
    resp = state.query(
        IndexName="PipelineIndex",
        KeyConditionExpression=Key("pipeline_id").eq(pipeline_id),
        FilterExpression=Attr("step_name").is_in(["RunPipeline", "run_pipeline"]),
    )
    items = resp.get("Items", [])
    if run_id:
        items = [i for i in items if i.get("run_id") == run_id] or items
    items = [i for i in items if i.get("status") == "success"]
    if not items:
        return None
    items.sort(key=lambda i: i.get("timestamp", ""), reverse=True)
    return items[0]


def verify_target_data(boto3_session, pipeline_id, *, run_id=None):
    """Verify a pipeline run produced target data on real AWS.

    Strategy (target-agnostic, no Athena S3-Tables federation required):
    the RunPipeline step for a successful run records the Glue job outcome in
    the State table. A genuine target write is evidenced by:
      * step status == 'success'
      * a Glue ``job_run_id`` was recorded
      * no ``error_message``
      * ``completed_at`` is set and ``actual_cost`` is non-negative

    Returns the RunPipeline output_data dict on success.

    Raises:
        AssertionError: if no successful target write is evidenced.
    """
    record = latest_successful_runpipeline(boto3_session, pipeline_id, run_id=run_id)
    assert record is not None, f"No successful RunPipeline step recorded for pipeline {pipeline_id}"
    output = record.get("output_data", {}) or {}
    assert output, f"RunPipeline step for {pipeline_id} produced no output_data"
    assert output.get("job_run_id"), f"No Glue job_run_id recorded for {pipeline_id}"
    assert not output.get("error_message"), (
        f"RunPipeline reported an error for {pipeline_id}: {output.get('error_message')}"
    )
    assert output.get("completed_at"), f"RunPipeline never completed for {pipeline_id}"
    cost = output.get("actual_cost")
    if cost is not None:
        assert float(cost) >= 0, f"Negative actual_cost for {pipeline_id}"
    return output


@pytest.fixture
def verify_target_data_fn(boto3_session):
    """Fixture: callable that verifies a pipeline produced target data."""

    def _verify(pipeline_id, *, run_id=None):
        return verify_target_data(boto3_session, pipeline_id, run_id=run_id)

    return _verify


@pytest.fixture
def e2e_ledger(boto3_session):
    """Function-scoped cleanup ledger — tears down registered artifacts on exit.

    Registered teardowns execute in LIFO order regardless of test outcome.
    The boto3_session is pre-configured; rest_client can be set by tests
    that need pipeline archive/delete.
    """
    ledger = ResourceLedger()
    ledger.set_boto3_session(boto3_session)
    yield ledger
    ledger.cleanup()


@pytest.fixture(scope="session")
def rest_client(dashboard_base_url, cognito_id_token) -> CognitoRestClient:
    """Session-scoped Cognito-authenticated REST client for the dashboard API.

    Routes through CloudFront (the regional WAF blocks direct execute-api
    access) and sends the ID token as the raw Authorization header value.
    The token and throwaway user come from the ``cognito_e2e_auth`` fixture —
    fully automated, no env vars required (overrides: ADI_E2E_BASE_URL,
    ADI_E2E_USER_POOL_ID, ADI_E2E_CLIENT_ID).
    """
    return CognitoRestClient(dashboard_base_url, cognito_id_token)


@pytest.fixture
def provisioner(rest_client, e2e_ledger, boto3_session) -> TestPipelineProvisioner:
    """Function-scoped pipeline provisioner.

    Creates throwaway pipelines via POST /api/pipelines (Cognito-authenticated)
    and auto-registers each with the e2e_ledger for archive→delete cleanup.
    """
    # Ensure the ledger has the rest_client for pipeline teardown
    e2e_ledger.set_rest_client(rest_client)
    return TestPipelineProvisioner(
        rest_client=rest_client,
        ledger=e2e_ledger,
        boto3_session=boto3_session,
    )


@pytest.fixture
def approval_driver(rest_client, boto3_session, cost_budget) -> ApprovalDriver:
    """Function-scoped approval driver for releasing waitForTaskToken gates.

    Supports both schema-mapping and code-generation gates with three
    fallback release paths (REST → Lambda invoke → raw SFN).
    """
    return ApprovalDriver(
        rest_client=rest_client,
        boto3_session=boto3_session,
        cost_budget=cost_budget,
    )


@pytest.fixture
def target_verifier(boto3_session) -> TargetVerifier:
    """Function-scoped target verifier for asserting pipeline output.

    Dispatches by target type: RDS Data API, S3 Parquet (pyarrow),
    S3 Tables (Athena with capability detection), or the State-table
    fallback (verify_target_data).
    """

    def _verify_target_data_wrapper(pipeline_id, *, run_id=None):
        return verify_target_data(boto3_session, pipeline_id, run_id=run_id)

    return TargetVerifier(
        boto3_session=boto3_session,
        verify_target_data_fn=_verify_target_data_wrapper,
    )
